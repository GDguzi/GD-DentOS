import sqlite3
import tempfile
import threading
import time
import unittest
import uuid
from pathlib import Path

from fastapi.testclient import TestClient

from local_app.routes import medical_records as apimod   # 病历更新已搬家,竞态钩子跟着指过去
from local_app import auth
from local_app.api import create_app
from local_app.db import connect, init_db
from local_app.routes import ai_drafts as aid
from local_app.routes import record_items as ridmod
from local_app.routes import billing_ops as bops
from local_app.routes import warehouse_stock_in as wsi
from local_app.routes import warehouse_stock_out as wso


class DBConcurrencyTest(unittest.TestCase):
    def test_wal_mode_enabled(self):
        # connect() 应把库切到 WAL，允许读写并发。
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "clinic.sqlite3"
            init_db(db_path)
            with connect(db_path) as conn:
                mode = conn.execute("pragma journal_mode").fetchone()[0]
            self.assertEqual(mode.lower(), "wal")

    def test_second_writer_waits_not_locked(self):
        # 一个写者持写锁 ~0.4s，另一个写者在此期间发起写。
        # 有 busy_timeout 时第二个写者排队等待并最终成功，而非立刻 database is locked。
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "clinic.sqlite3"
            init_db(db_path)
            with connect(db_path) as conn:
                conn.execute("create table t(id integer primary key, n integer)")
                conn.execute("insert into t(id, n) values (1, 0)")
                conn.commit()

            hold_started = threading.Event()

            def holder():
                with connect(db_path) as c:
                    c.execute("begin immediate")  # 抢写锁
                    c.execute("update t set n = n + 1 where id = 1")
                    hold_started.set()
                    time.sleep(0.4)  # 持锁 0.4s 后释放
                    c.commit()

            errors = []

            def writer():
                hold_started.wait(2)  # 确保 holder 先拿到锁
                try:
                    with connect(db_path) as c:
                        c.execute("update t set n = n + 1 where id = 1")
                        c.commit()
                except sqlite3.OperationalError as e:
                    errors.append(str(e))

            th = threading.Thread(target=holder)
            tw = threading.Thread(target=writer)
            th.start()
            tw.start()
            th.join()
            tw.join()

            self.assertEqual(errors, [], f"第二个写者不应撞锁报错: {errors}")
            with connect(db_path) as conn:
                n = conn.execute("select n from t where id = 1").fetchone()[0]
            self.assertEqual(n, 2, "两次写都应成功落库")


class ConfirmRaceTest(unittest.TestCase):
    """双确认竞态：并发(或双击)确认同一张草稿入库单，库存只能 +1 次，不能翻倍。
    busy_timeout 防不住这个——两个请求的守卫读都先成功了；真正的修复是
    confirm 入口 begin_immediate 抢写锁(db.begin_immediate)，把读-改-写串行化。"""

    def test_double_confirm_does_not_double_stock(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "clinic.sqlite3"
            init_db(db_path)
            app = create_app(db_path)
            client = TestClient(app)

            # seed 物品 + 一张 qty=100 的草稿入库单
            cat_id = client.post("/api/stock-categories", json={"name": "耗材"}).json()["id"]
            item_id = client.post(
                "/api/stock-items",
                json={"code": "RACE-001", "name": "手套", "category_id": cat_id, "unit": "盒"},
            ).json()["id"]
            stock_in_id = client.post(
                "/api/stock-in",
                json={"type": "purchase", "items": [{"stock_item_id": item_id, "qty": 100}]},
            ).json()["id"]

            # 拓宽竞态窗口：首个 change_balance 时 set 事件并 sleep 0.5s，
            # 让第二个 confirm 在"已过守卫读、尚未提交"的窗口内并发进来。
            real_change_balance = wsi.change_balance
            in_critical = threading.Event()
            first = threading.Lock()
            first_done = {"v": False}

            def slow_change_balance(*a, **kw):
                with first:
                    is_first = not first_done["v"]
                    first_done["v"] = True
                if is_first:
                    in_critical.set()
                    time.sleep(0.5)
                return real_change_balance(*a, **kw)

            wsi.change_balance = slow_change_balance
            self.addCleanup(lambda: setattr(wsi, "change_balance", real_change_balance))

            results = {}

            def confirm(tag, wait=None):
                if wait is not None:
                    wait.wait(2)
                resp = TestClient(app).post(f"/api/stock-in/{stock_in_id}/confirm")
                results[tag] = resp.status_code

            t1 = threading.Thread(target=confirm, args=("A",))
            t2 = threading.Thread(target=confirm, args=("B", in_critical))
            t1.start()
            t2.start()
            t1.join()
            t2.join()

            with connect(db_path) as conn:
                qty = conn.execute(
                    "select coalesce(sum(qty), 0) from stock_balance where stock_item_id = ?",
                    (item_id,),
                ).fetchone()[0]

            self.assertEqual(
                qty, 100, f"库存应只 +100 一次，不翻倍；实际 {qty}；HTTP 结果 {results}"
            )
            self.assertEqual(
                sorted(results.values()), [200, 409],
                f"应恰好一个成功一个 409(已确认)；实际 {results}",
            )


class AiDraftConfirmRaceTest(unittest.TestCase):
    """AI 草稿确认的双确认竞态(非仓库路径)：并发/双击确认同一草稿，
    只能生效一次——否则后到者用旧基线静默覆盖先到者的病历内容(丢更新)，
    并多灌一条版本+一条审计。验证 ai_drafts.py confirm 的 begin_immediate 真生效。"""

    def test_double_confirm_no_lost_update(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "clinic.sqlite3"
            init_db(db_path)
            app = create_app(db_path)
            with connect(db_path) as conn:
                conn.execute(
                    "insert into patients(patient_identity, display_name, updated_at, current_hash) "
                    "values ('p1', '测试患者', '2026-06-07 00:00:00', 'h1')"
                )
                conn.execute(
                    "insert into medical_records(record_id, patient_identity, visit_time, "
                    "doctor_name, tooth_json, content_json, draft_status, origin, ai_meta_json, "
                    "created_at, updated_at, current_hash) "
                    "values ('d1', 'p1', '2026-06-10 09:00:00', '王医生', '{}', "
                    "'{\"PC\": \"AI主诉\"}', 'ai_draft', 'ai_voice', '{}', "
                    "datetime('now','localtime'), datetime('now','localtime'), 'h-d1')"
                )
                conn.commit()

            # 拓宽窗口：首个 stable_hash(守卫读之后才调用)时 set 事件并 sleep，
            # 让第二个 confirm 在"已过 ai_draft 守卫、尚未提交"窗口内并发进来。
            real_stable_hash = aid.stable_hash
            in_critical = threading.Event()
            first = threading.Lock()
            first_done = {"v": False}

            def slow_stable_hash(*a, **kw):
                with first:
                    is_first = not first_done["v"]
                    first_done["v"] = True
                if is_first:
                    in_critical.set()
                    time.sleep(0.5)
                return real_stable_hash(*a, **kw)

            aid.stable_hash = slow_stable_hash
            self.addCleanup(lambda: setattr(aid, "stable_hash", real_stable_hash))

            results = {}

            def confirm(tag, content, wait=None):
                if wait is not None:
                    wait.wait(2)
                resp = TestClient(app).post(
                    "/api/ai-drafts/d1/confirm", json={"content_json": {"PC": content}})
                results[tag] = resp.status_code

            t1 = threading.Thread(target=confirm, args=("A", "A版"))
            t2 = threading.Thread(target=confirm, args=("B", "B版", in_critical))
            t1.start()
            t2.start()
            t1.join()
            t2.join()

            with connect(db_path) as conn:
                audit_n = conn.execute(
                    "select count(*) from audit_logs where action = 'confirm_ai_draft' and entity_id = 'd1'"
                ).fetchone()[0]
                ver_n = conn.execute(
                    "select count(*) from medical_record_versions where record_id = 'd1'"
                ).fetchone()[0]

            self.assertEqual(
                sorted(results.values()), [200, 404],
                f"应恰好一个成功一个 404(草稿已确认)；实际 {results}",
            )
            self.assertEqual(audit_n, 1, f"confirm 审计只能一条，不重复；实际 {audit_n}")
            self.assertLessEqual(ver_n, 1, f"版本快照不应重复灌；实际 {ver_n}")


class AiDraftConfirmVsDiscardRaceTest(unittest.TestCase):
    """跨端点竞态：同一草稿 confirm 与 discard 并发,
    只能一个生效——否则'确认成功又被废弃'(状态覆盖)。验证两端点都加了 begin_immediate。"""

    def test_confirm_and_discard_one_wins(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "clinic.sqlite3"
            init_db(db_path)
            app = create_app(db_path)
            with connect(db_path) as conn:
                conn.execute("insert into patients(patient_identity, display_name, updated_at, current_hash) "
                             "values ('p1','测试','2026-06-07 00:00:00','h1')")
                conn.execute(
                    "insert into medical_records(record_id, patient_identity, visit_time, doctor_name, "
                    "tooth_json, content_json, draft_status, origin, ai_meta_json, created_at, updated_at, current_hash) "
                    "values ('d1','p1','2026-06-10 09:00:00','王','{}','{\"PC\":\"x\"}','ai_draft','ai_voice','{}',"
                    "'2026-06-10 09:00:00','2026-06-10 09:00:00','h-d1')")
                conn.commit()

            real_hash = aid.stable_hash
            in_critical = threading.Event()
            first = threading.Lock()
            done = {"v": False}

            def slow_hash(*a, **kw):
                with first:
                    is_first = not done["v"]
                    done["v"] = True
                if is_first:
                    in_critical.set()
                    time.sleep(0.5)
                return real_hash(*a, **kw)

            aid.stable_hash = slow_hash
            self.addCleanup(lambda: setattr(aid, "stable_hash", real_hash))

            results = {}

            def confirm():
                results["confirm"] = TestClient(app).post(
                    "/api/ai-drafts/d1/confirm", json={"content_json": {"PC": "确认版"}}).status_code

            def discard():
                in_critical.wait(2)   # 等 confirm 进入临界区(持锁)后再发起
                results["discard"] = TestClient(app).post("/api/ai-drafts/d1/discard").status_code

            t1 = threading.Thread(target=confirm)
            t2 = threading.Thread(target=discard)
            t1.start()
            t2.start()
            t1.join()
            t2.join()

            with connect(db_path) as conn:
                status = conn.execute("select draft_status from medical_records where record_id='d1'").fetchone()[0]
            # confirm 先持锁→confirm 成功(draft_status=''),discard 醒来读到非 ai_draft→404
            self.assertEqual(sorted(results.values()), [200, 404],
                             f"应恰好一个成功一个404,不能都改;实际 {results}")
            self.assertEqual(status, "", "confirm 应胜出,病历不应被 discard 覆盖成 discarded")


class BillingPayRaceTest(unittest.TestCase):
    """收款双确认竞态：并发/双击对同一张待收费单收款，只能记一笔——
    否则 payments 表插两条(实收按流水翻倍)，而 bill.paid_fee 只记一次=账实不符。
    验证 billing_ops.confirm_payment 的 begin_immediate 真生效。"""

    def test_double_pay_records_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "clinic.sqlite3"
            init_db(db_path)
            app = create_app(db_path)
            with connect(db_path) as conn:
                conn.execute(
                    "insert into patients(patient_identity, display_name, updated_at, current_hash) "
                    "values ('p1', '测试患者', '2026-06-07 00:00:00', 'h1')"
                )
                conn.execute(
                    "insert into bills(bill_id, patient_identity, total_fee, paid_fee, state, updated_at) "
                    "values ('local-bill-x', 'p1', 100, 0, 'pending', '2026-06-07 00:00:00')"
                )
                conn.commit()

            # 拓宽窗口：守卫读之后、commit 之前调用的 audit_operator 首次 set 事件并 sleep
            # (audit 收编进 auth.audit_write 后,audit_operator 从 auth 模块解析,钩子跟着指过去)
            real_audit_operator = auth.audit_operator
            in_critical = threading.Event()
            first = threading.Lock()
            first_done = {"v": False}

            def slow_audit_operator(*a, **kw):
                with first:
                    is_first = not first_done["v"]
                    first_done["v"] = True
                if is_first:
                    in_critical.set()
                    time.sleep(0.5)
                return real_audit_operator(*a, **kw)

            auth.audit_operator = slow_audit_operator
            self.addCleanup(lambda: setattr(auth, "audit_operator", real_audit_operator))

            results = {}

            def pay(tag, wait=None):
                if wait is not None:
                    wait.wait(2)
                resp = TestClient(app).post(
                    "/api/bills/local-bill-x/pay", json={"methods": [{"method": "现金", "amount": 100}], "request_id": uuid.uuid4().hex})
                results[tag] = resp.status_code

            t1 = threading.Thread(target=pay, args=("A",))
            t2 = threading.Thread(target=pay, args=("B", in_critical))
            t1.start()
            t2.start()
            t1.join()
            t2.join()

            with connect(db_path) as conn:
                pay_n = conn.execute(
                    "select count(*) from payments where bill_id = 'local-bill-x'"
                ).fetchone()[0]
                paid = conn.execute(
                    "select paid_fee from bills where bill_id = 'local-bill-x'"
                ).fetchone()[0]

            self.assertEqual(pay_n, 1, f"收款流水只能一条，不翻倍；实际 {pay_n} 条；HTTP {results}")
            self.assertEqual(paid, 100, f"已收应为 100；实际 {paid}")
            self.assertIn(409, results.values(), f"第二次应被 409 挡住；实际 {results}")


class StockOutConfirmRaceTest(unittest.TestCase):
    """出库双确认竞态：并发/双击确认同一张出库单，库存只能扣一次，不能超扣。"""

    def test_double_confirm_does_not_over_deduct(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "clinic.sqlite3"
            init_db(db_path)
            app = create_app(db_path)
            client = TestClient(app)

            cat_id = client.post("/api/stock-categories", json={"name": "耗材"}).json()["id"]
            item_id = client.post(
                "/api/stock-items",
                json={"code": "OUT-RACE", "name": "缝合线", "category_id": cat_id, "unit": "包"},
            ).json()["id"]
            si = client.post(
                "/api/stock-in",
                json={"type": "purchase", "items": [{"stock_item_id": item_id, "qty": 10, "batch_no": "B1"}]},
            ).json()["id"]
            client.post(f"/api/stock-in/{si}/confirm")
            so = client.post(
                "/api/stock-out",
                json={"type": "领用", "receiver": "诊室", "items": [{"stock_item_id": item_id, "qty": 4}]},
            ).json()["id"]

            real_deduct = wso.deduct_balance
            in_critical = threading.Event()
            first = threading.Lock()
            first_done = {"v": False}

            def slow_deduct(*a, **kw):
                with first:
                    is_first = not first_done["v"]
                    first_done["v"] = True
                if is_first:
                    in_critical.set()
                    time.sleep(0.5)
                return real_deduct(*a, **kw)

            wso.deduct_balance = slow_deduct
            self.addCleanup(lambda: setattr(wso, "deduct_balance", real_deduct))

            results = {}

            def confirm(tag, wait=None):
                if wait is not None:
                    wait.wait(2)
                resp = TestClient(app).post(f"/api/stock-out/{so}/confirm")
                results[tag] = resp.status_code

            t1 = threading.Thread(target=confirm, args=("A",))
            t2 = threading.Thread(target=confirm, args=("B", in_critical))
            t1.start()
            t2.start()
            t1.join()
            t2.join()

            with connect(db_path) as conn:
                qty = conn.execute(
                    "select coalesce(sum(qty), 0) from stock_balance where stock_item_id = ?",
                    (item_id,),
                ).fetchone()[0]

            self.assertEqual(qty, 6, f"库存应只扣一次(10-4=6)，不超扣；实际 {qty}；HTTP {results}")
            self.assertEqual(sorted(results.values()), [200, 409],
                             f"应恰好一个成功一个 409；实际 {results}")


class MedicalRecordUpdateRaceTest(unittest.TestCase):
    """改病历并发竞态(api.py 网页编辑端点)：iPad+PC 同改/双击保存同一份病历，
    无锁时两者都以同一旧快照(H0)为基准——后写者整篇覆盖先写者且其内容从未进版本表归档(丢更新)，
    且两条版本行都是同一 H0 快照(重复归档)。加锁后串行化：后写者基于已提交的新状态归档，版本 hash 各不相同。
    验证 api.py update_medical_record 的 begin_immediate 真生效。"""

    def test_concurrent_edit_no_lost_update(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "clinic.sqlite3"
            init_db(db_path)
            app = create_app(db_path)
            with connect(db_path) as conn:
                conn.execute(
                    "insert into patients(patient_identity, display_name, updated_at, current_hash) "
                    "values ('p1', '测试患者', '2026-06-07 00:00:00', 'h1')"
                )
                conn.execute(
                    "insert into medical_records(record_id, patient_identity, visit_time, "
                    "doctor_name, tooth_json, content_json, draft_status, origin, ai_meta_json, "
                    "created_at, updated_at, current_hash) "
                    "values ('m1', 'p1', '2026-06-10 09:00:00', '王医生', '{}', "
                    "'{\"PC\": \"原始\"}', '', 'manual', '{}', "
                    "'2026-06-10 09:00:00', '2026-06-10 09:00:00', 'h-m1')"
                )
                conn.commit()

            # 守卫读之后调用的 stable_hash 首次 set 事件并 sleep，拓宽竞态窗口
            real_stable_hash = apimod.stable_hash
            in_critical = threading.Event()
            first = threading.Lock()
            first_done = {"v": False}

            def slow_stable_hash(*a, **kw):
                with first:
                    is_first = not first_done["v"]
                    first_done["v"] = True
                if is_first:
                    in_critical.set()
                    time.sleep(0.5)
                return real_stable_hash(*a, **kw)

            apimod.stable_hash = slow_stable_hash
            self.addCleanup(lambda: setattr(apimod, "stable_hash", real_stable_hash))

            results = {}

            def edit(tag, content, wait=None):
                if wait is not None:
                    wait.wait(2)
                resp = TestClient(app).put(
                    "/api/medical-records/m1", json={"content_json": {"PC": content}})
                results[tag] = resp.status_code

            t1 = threading.Thread(target=edit, args=("A", "A版"))
            t2 = threading.Thread(target=edit, args=("B", "B版", in_critical))
            t1.start()
            t2.start()
            t1.join()
            t2.join()

            with connect(db_path) as conn:
                total = conn.execute(
                    "select count(*) from medical_record_versions where record_id = 'm1'"
                ).fetchone()[0]
                distinct = conn.execute(
                    "select count(distinct version_hash) from medical_record_versions where record_id = 'm1'"
                ).fetchone()[0]

            self.assertEqual(
                total, distinct,
                f"版本归档不能重复同一旧快照(丢更新标志)；总{total}条/去重{distinct}条；HTTP {results}",
            )


class PlanDeleteVsBillRaceTest(unittest.TestCase):
    """跨端点竞态：同一计划 delete 与转账单(/bill)并发。begin_immediate 串行化——
    delete 先拿锁则 bill 读到 is_deleted=1 → 404，绝不给已删计划凭空出账单(无 torn 状态)。"""

    def test_delete_first_blocks_bill(self):
        from local_app.routes import treatment_plans as tp
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "clinic.sqlite3"
            init_db(db_path)
            app = create_app(db_path)
            with connect(db_path) as conn:
                conn.execute("insert into patients(patient_identity, display_name, updated_at, current_hash) "
                             "values ('p1','测试','x','h1')")
                conn.execute("insert into treatment_plans(plan_id, patient_identity, status, is_deleted, updated_at) "
                             "values ('pl1','p1','confirmed',0,'x')")
                conn.commit()

            real_now = tp.now_str
            in_critical = threading.Event()
            first = threading.Lock()
            done = {"v": False}

            def slow_now(*a, **kw):
                with first:
                    is_first = not done["v"]
                    done["v"] = True
                if is_first:
                    in_critical.set()
                    time.sleep(0.5)   # delete 持写锁停留，让 bill 在锁外排队
                return real_now(*a, **kw)

            tp.now_str = slow_now
            self.addCleanup(lambda: setattr(tp, "now_str", real_now))

            results = {}

            def do_delete():
                results["delete"] = TestClient(app).delete("/api/treatment-plans/pl1").status_code

            def do_bill():
                in_critical.wait(2)   # 等 delete 进临界区(持锁)再发起
                results["bill"] = TestClient(app).post("/api/treatment-plans/pl1/bill", json={}).status_code

            t1 = threading.Thread(target=do_delete)
            t2 = threading.Thread(target=do_bill)
            t1.start()
            t2.start()
            t1.join()
            t2.join()

            with connect(db_path) as conn:
                deleted = conn.execute("select is_deleted from treatment_plans where plan_id='pl1'").fetchone()[0]
                n_bills = conn.execute("select count(*) from bills where patient_identity='p1'").fetchone()[0]
            self.assertEqual(results["delete"], 200)
            self.assertEqual(results["bill"], 404, f"delete 先拿锁，bill 应 404；实际 {results}")
            self.assertEqual(deleted, 1)
            self.assertEqual(n_bills, 0, "已删计划不应凭空出账单")


class SterilizeSubmitVsUpdateRaceTest(unittest.TestCase):
    """跨端点竞态：送消单 submit 与 update 并发。begin_immediate 串行化——
    submit 先拿锁→update 读到 status=submitted → 409，已送审不被旧草稿请求改头(无 torn)。"""

    def test_submit_first_blocks_update(self):
        from local_app.routes import sterilize_dispatch as sd
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "clinic.sqlite3"
            init_db(db_path)
            app = create_app(db_path)
            with connect(db_path) as conn:
                conn.execute("insert into sterilize_dispatch(id, dispatch_no, department, dispatcher, status, "
                             "is_deleted, created_at, updated_at) values ('d1','SD1','口腔科','张三','draft',0,'x','x')")
                conn.commit()

            real_now = sd.now_str
            in_critical = threading.Event()
            first = threading.Lock()
            done = {"v": False}

            def slow_now(*a, **kw):
                with first:
                    is_first = not done["v"]
                    done["v"] = True
                if is_first:
                    in_critical.set()
                    time.sleep(0.5)   # submit 持写锁停留，让 update 在锁外排队
                return real_now(*a, **kw)

            sd.now_str = slow_now
            self.addCleanup(lambda: setattr(sd, "now_str", real_now))

            results = {}

            def do_submit():
                results["submit"] = TestClient(app).post("/api/dispatch/d1/submit").status_code

            def do_update():
                in_critical.wait(2)   # 等 submit 进临界区(持锁)再发起
                results["update"] = TestClient(app).put(
                    "/api/dispatch/d1", json={"department": "改了", "dispatcher": "李四"}).status_code

            t1 = threading.Thread(target=do_submit)
            t2 = threading.Thread(target=do_update)
            t1.start()
            t2.start()
            t1.join()
            t2.join()

            with connect(db_path) as conn:
                row = conn.execute("select status, department from sterilize_dispatch where id='d1'").fetchone()
            self.assertEqual(results["submit"], 200)
            self.assertEqual(results["update"], 409, f"submit 先拿锁，update 应 409；实际 {results}")
            self.assertEqual(row["status"], "submitted")
            self.assertEqual(row["department"], "口腔科", "已送审不应被旧草稿 update 改头")


if __name__ == "__main__":
    unittest.main()


class RecordItemsReplaceRaceTest(unittest.TestCase):
    """并发替换病历牙位条目——begin_immediate 抢锁把读-改-写串行化,
    否则第二个保存读到过期旧值,审计链断档(old_json 指向被覆盖前的状态)。"""

    def test_concurrent_replace_no_audit_chain_gap(self):
        import json as _json
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "clinic.sqlite3"
            init_db(db_path)
            app = create_app(db_path)
            with connect(db_path) as conn:
                conn.execute("insert into patients(patient_identity, display_name, updated_at, current_hash) "
                             "values ('p1','测试','2026-06-07 00:00:00','h1')")
                conn.execute(
                    "insert into medical_records(record_id, patient_identity, visit_time, doctor_name, "
                    "tooth_json, content_json, draft_status, origin, ai_meta_json, created_at, updated_at, current_hash) "
                    "values ('r1','p1','2026-06-10 09:00:00','王','{}','{}','','manual','{}',"
                    "'2026-06-10 09:00:00','2026-06-10 09:00:00','h-r1')")
                conn.commit()

            in_critical = threading.Event()
            lock = threading.Lock()
            state = {"first": True}
            real_json = ridmod.json

            class _JsonProxy:
                def dumps(self, *a, **k):
                    with lock:
                        f = state["first"]; state["first"] = False
                    if f:
                        in_critical.set(); time.sleep(0.5)   # 首个写入持锁时拉宽窗口
                    return _json.dumps(*a, **k)
                def loads(self, *a, **k):
                    return _json.loads(*a, **k)

            ridmod.json = _JsonProxy()
            self.addCleanup(lambda: setattr(ridmod, "json", real_json))

            def put(content, wait=None):
                if wait is not None:
                    wait.wait(2)
                TestClient(app).put("/api/medical-records/r1/items",
                                    json={"items": [{"item_type": "exam", "content": content, "tooth": "11"}]})

            t1 = threading.Thread(target=put, args=("A",))
            t2 = threading.Thread(target=put, args=("B", in_critical))
            t1.start(); t2.start(); t1.join(); t2.join()

            with connect(db_path) as conn:
                rows = conn.execute(
                    "select old_json, new_json from audit_logs where action='update_record_items' "
                    "and entity_id='r1' order by created_at, rowid").fetchall()
            # 找到"写入B"那条审计,它的 old_json 必须是先落库的 A(串行化),而非过期的空
            b_rows = [r for r in rows if '"B"' in (r["new_json"] or "")]
            self.assertTrue(b_rows, "应有写入B的审计")
            self.assertIn('"A"', b_rows[0]["old_json"] or "",
                          f"begin_immediate 失效:B 的审计 old_json 未读到已落库的 A,链断档 → {b_rows[0]['old_json']!r}")
