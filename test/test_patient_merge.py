"""患者合并：次患者数据重指到主患者 + 档案字段补全 + 标记 + 审计 + 列表排除。"""
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from local_app import auth
from local_app.api import create_app
from local_app.db import connect, init_db


class PatientMergeTest(unittest.TestCase):
    def _client(self, tmp, login="boss"):
        db = Path(tmp) / "clinic.sqlite3"
        init_db(db)
        with connect(db) as conn:
            # 合并需管理员；建管理员 boss + 前台 qt(qt 用于 403 用例)
            auth.create_user(conn, "boss", "院长", "admin123", role="admin")
            auth.create_user(conn, "qt", "前台", "pw123456", role="reception")
            # 主患者(无电话) + 次患者(同名,有电话) = 同一个人重复建档
            conn.execute("insert into patients(patient_identity, display_name, phone, updated_at, current_hash) "
                         "values ('keep','张三','','x','h1')")
            conn.execute("insert into patients(patient_identity, display_name, phone, updated_at, current_hash) "
                         "values ('dup','张三','13800001111','x','h2')")
            # 次患者有预约 + 账单
            conn.execute("insert into appointments(appointment_id, patient_identity, start_time, doctor_name, item_name, status, updated_at) "
                         "values ('a1','dup','2026-06-10 09:00','王','洁牙','2','x')")
            conn.execute("insert into bills(bill_id, patient_identity, bill_no, bill_time, total_fee, paid_fee, state) "
                         "values ('b1','dup','B1','2026-06-10', 100, 100, 'paid')")
            conn.commit()
        client = TestClient(create_app(db))
        if login:
            pw = {"boss": "admin123", "qt": "pw123456"}[login]
            client.post("/api/auth/login", json={"username": login, "password": pw})
        return db, client

    def test_merge_fill_writes_version_and_hash(self):
        # 合并补全主患者字段要留 patient_versions 快照 + 更新 current_hash
        with tempfile.TemporaryDirectory() as tmp:
            db, client = self._client(tmp)
            r = client.post("/api/patients/keep/merge", json={"secondary": "dup", "reason": "重复建档"})
            self.assertEqual(r.status_code, 200)
            with connect(db) as conn:
                nver = conn.execute("select count(*) from patient_versions where patient_identity='keep'").fetchone()[0]
                ch = conn.execute("select current_hash from patients where patient_identity='keep'").fetchone()[0]
            self.assertGreaterEqual(nver, 1)   # 补全留了版本快照
            self.assertTrue(ch and ch != "h1")  # current_hash 已更新(不再是初始 h1)
            # current_hash 必须 == 写库后整行快照(否则版本链不自洽)
            from local_app.snapshots import patient_profile_snapshot
            from local_app.versioning import stable_hash
            with connect(db) as conn:
                row = conn.execute("select * from patients where patient_identity='keep'").fetchone()
            self.assertEqual(ch, stable_hash(patient_profile_snapshot(row)))

    def test_non_admin_cannot_merge(self):
        # 前台(reception)合并患者 → 403,数据不变
        with tempfile.TemporaryDirectory() as tmp:
            db, client = self._client(tmp, login="qt")
            r = client.post("/api/patients/keep/merge", json={"secondary": "dup", "reason": "x"})
            self.assertEqual(r.status_code, 403)
            with connect(db) as conn:
                self.assertEqual(conn.execute("select patient_identity from bills where bill_id='b1'").fetchone()[0], "dup")

    def test_merge_repoints_data_fills_and_marks(self):
        with tempfile.TemporaryDirectory() as tmp:
            db, client = self._client(tmp)
            r = client.post("/api/patients/keep/merge", json={"secondary": "dup", "reason": "重复建档"})
            self.assertEqual(r.status_code, 200)
            with connect(db) as conn:
                # 预约/账单重指到 keep
                self.assertEqual(conn.execute("select patient_identity from appointments where appointment_id='a1'").fetchone()[0], "keep")
                self.assertEqual(conn.execute("select patient_identity from bills where bill_id='b1'").fetchone()[0], "keep")
                # 主患者空电话被次患者补上
                self.assertEqual(conn.execute("select phone from patients where patient_identity='keep'").fetchone()[0], "13800001111")
                # 次患者标记已合并
                row = conn.execute("select merged_into, is_deleted from patients where patient_identity='dup'").fetchone()
                self.assertEqual(row[0], "keep")
                self.assertEqual(row[1], 1)
                # 审计两条
                n = conn.execute("select count(*) from audit_logs where action like 'merge_patient_%'").fetchone()[0]
                self.assertEqual(n, 2)

    def test_merged_patient_excluded_from_list(self):
        with tempfile.TemporaryDirectory() as tmp:
            db, client = self._client(tmp)
            client.post("/api/patients/keep/merge", json={"secondary": "dup", "reason": "x"})
            ids = {p["patient_identity"] for p in client.get("/api/patients").json()["list"]}
            self.assertIn("keep", ids)
            self.assertNotIn("dup", ids)   # 已合并的次患者不在列表

    def _seed_member(self, db, pid, balance):
        with connect(db) as conn:
            conn.execute("insert into member_accounts(patient_identity, balance, created_at, updated_at) "
                         "values (?, ?, 'x', 'x')", (pid, balance))
            conn.execute("insert into member_transactions(txn_id, patient_identity, txn_type, amount, "
                         "balance_after, note, bill_id, operator, created_at) "
                         "values (?, ?, 'topup', ?, ?, '', '', 'boss', 'x')",
                         (f"mtxn-{pid}", pid, balance, balance))
            conn.commit()

    def test_merge_moves_member_balance_when_primary_has_no_account(self):
        # #审计1：次患者储值不能随软删成孤儿——主无账户时次账户改指主，余额与流水都到主名下
        with tempfile.TemporaryDirectory() as tmp:
            db, client = self._client(tmp)
            self._seed_member(db, "dup", 200)
            r = client.post("/api/patients/keep/merge", json={"secondary": "dup", "reason": "重复建档"})
            self.assertEqual(r.status_code, 200)
            with connect(db) as conn:
                bal = conn.execute("select balance from member_accounts where patient_identity='keep'").fetchone()
                self.assertIsNotNone(bal, "主患者应有储值账户")
                self.assertEqual(round(bal[0], 2), 200)
                self.assertIsNone(conn.execute("select 1 from member_accounts where patient_identity='dup'").fetchone(),
                                  "次患者账户应已改指,不再挂 dup")
                txn_owner = conn.execute("select patient_identity from member_transactions where txn_id='mtxn-dup'").fetchone()[0]
                self.assertEqual(txn_owner, "keep", "储值流水应重指到主患者")

    def test_merge_sums_member_balance_when_both_have_accounts(self):
        # #审计1：主次都有储值账户 → 余额相加,次账户删除,不撞主键不丢钱
        with tempfile.TemporaryDirectory() as tmp:
            db, client = self._client(tmp)
            self._seed_member(db, "keep", 50)
            self._seed_member(db, "dup", 200)
            r = client.post("/api/patients/keep/merge", json={"secondary": "dup", "reason": "重复建档"})
            self.assertEqual(r.status_code, 200)
            self.assertEqual(round(r.json()["moved"].get("member_balance_moved", 0), 2), 200)
            with connect(db) as conn:
                bal = conn.execute("select balance from member_accounts where patient_identity='keep'").fetchone()[0]
                self.assertEqual(round(bal, 2), 250, "主余额应为两账户之和")
                self.assertIsNone(conn.execute("select 1 from member_accounts where patient_identity='dup'").fetchone(),
                                  "次账户应被删除")

    def test_merge_records_balance_migration_txn(self):
        # 回归主次都有账户合并后,必须有一笔流水(balance_after=合并后余额)解释当前余额,否则改钱审计断链
        with tempfile.TemporaryDirectory() as tmp:
            db, client = self._client(tmp)
            self._seed_member(db, "keep", 50)
            self._seed_member(db, "dup", 200)
            r = client.post("/api/patients/keep/merge", json={"secondary": "dup", "reason": "重复建档"})
            self.assertEqual(r.status_code, 200)
            with connect(db) as conn:
                mig = conn.execute("select amount, balance_after from member_transactions "
                                   "where patient_identity='keep' and txn_type='merge_in'").fetchone()
                self.assertIsNotNone(mig, "应有一笔合并入账流水")
                self.assertEqual(round(mig[0], 2), 200, "迁移金额=次余额")
                self.assertEqual(round(mig[1], 2), 250, "balance_after=合并后余额,可解释当前250")
                latest = conn.execute("select balance_after from member_transactions where patient_identity='keep' "
                                      "order by rowid desc limit 1").fetchone()[0]   # merge_in 最后插入,rowid 最大
                self.assertEqual(round(latest, 2), 250, "最新流水 balance_after 对上账户余额(审计链闭合)")

    def test_merge_validation(self):
        with tempfile.TemporaryDirectory() as tmp:
            db, client = self._client(tmp)
            self.assertEqual(client.post("/api/patients/keep/merge", json={"secondary": "dup"}).status_code, 400)  # 缺原因
            self.assertEqual(client.post("/api/patients/keep/merge", json={"secondary": "keep", "reason": "x"}).status_code, 400)  # 自己
            self.assertEqual(client.post("/api/patients/keep/merge", json={"secondary": "nope", "reason": "x"}).status_code, 404)  # 次不存在
            # 合并后再合并次患者 → 409
            client.post("/api/patients/keep/merge", json={"secondary": "dup", "reason": "x"})
            self.assertEqual(client.post("/api/patients/keep/merge", json={"secondary": "dup", "reason": "x"}).status_code, 409)

    def test_merge_moves_client_communication_tables(self):
        # 客户沟通三表(calls/communications/communication_images)必须随合并改指主患者,
        # 否则合并重复档案后沟通记录/录音丢在旧身份下(患者资料归属错误)
        with tempfile.TemporaryDirectory() as tmp:
            db, client = self._client(tmp)
            with connect(db) as conn:
                conn.execute("insert into communications(communication_id, patient_identity, channel, created_at) "
                             "values ('c1','dup','phone','x')")
                conn.execute("insert into communication_images(image_id, communication_id, patient_identity, created_at) "
                             "values ('i1','c1','dup','x')")
                conn.execute("insert into calls(call_id, patient_identity, created_at) values ('k1','dup','x')")
                conn.commit()
            self.assertEqual(client.post("/api/patients/keep/merge",
                                         json={"secondary": "dup", "reason": "重复"}).status_code, 200)
            with connect(db) as conn:
                for tbl in ("communications", "communication_images", "calls"):
                    dup_n = conn.execute(f"select count(*) c from {tbl} where patient_identity='dup'").fetchone()["c"]
                    keep_n = conn.execute(f"select count(*) c from {tbl} where patient_identity='keep'").fetchone()["c"]
                    self.assertEqual(dup_n, 0, f"{tbl} 次患者不应有残留")
                    self.assertEqual(keep_n, 1, f"{tbl} 应改指主患者")


    def test_merge_moves_return_visit_images(self):
        # 回访截图表(return_visit_images)带 patient_identity,必须随合并改指主患者,
        # 否则次患者的回访截图合并后挂在旧身份下从 UI 消失(与 同类漏表)
        with tempfile.TemporaryDirectory() as tmp:
            db, client = self._client(tmp)
            with connect(db) as conn:
                conn.execute("insert into return_visits(return_visit_id, patient_identity, due_time) "
                             "values ('rv1','dup','2026-07-01 10:00')")
                conn.execute("insert into return_visit_images(image_id, return_visit_id, patient_identity, rel_path, created_at) "
                             "values ('rvi1','rv1','dup','x/a.png','x')")
                conn.commit()
            self.assertEqual(client.post("/api/patients/keep/merge",
                                         json={"secondary": "dup", "reason": "重复"}).status_code, 200)
            with connect(db) as conn:
                self.assertEqual(conn.execute(
                    "select count(*) from return_visit_images where patient_identity='dup'").fetchone()[0], 0,
                    "次患者不应残留回访截图")
                self.assertEqual(conn.execute(
                    "select patient_identity from return_visit_images where image_id='rvi1'").fetchone()[0], "keep")

    def test_merge_moves_image_sets_and_survives_date_collision(self):
        # 影像日期分组随合并搬家:两边同日期不撞主键,次患者独有日期补给主患者
        with tempfile.TemporaryDirectory() as tmp:
            db, client = self._client(tmp)
            with connect(db) as conn:
                conn.execute("insert into patient_image_sets(patient_identity, image_date, created_at) "
                             "values ('keep','2026-07-01','x')")
                conn.execute("insert into patient_image_sets(patient_identity, image_date, created_at) "
                             "values ('dup','2026-07-01','x')")   # 同日期,盲改必撞主键
                conn.execute("insert into patient_image_sets(patient_identity, image_date, created_at) "
                             "values ('dup','2026-07-05','x')")   # 次患者独有日期
                conn.commit()
            self.assertEqual(client.post("/api/patients/keep/merge",
                                         json={"secondary": "dup", "reason": "重复"}).status_code, 200)
            with connect(db) as conn:
                keep_dates = sorted(r[0] for r in conn.execute(
                    "select image_date from patient_image_sets where patient_identity='keep'"))
                self.assertEqual(keep_dates, ["2026-07-01", "2026-07-05"], "主患者应有两天的分组")
                self.assertEqual(conn.execute(
                    "select count(*) from patient_image_sets where patient_identity='dup'").fetchone()[0], 0)

    def test_merge_moves_tags_and_dedups_by_name(self):
        # 标签随合并搬家,同名标签去重(产品决策)
        with tempfile.TemporaryDirectory() as tmp:
            db, client = self._client(tmp)
            with connect(db) as conn:
                conn.execute("insert into patient_tags(tag_id, patient_identity, name) values ('t1','keep','VIP')")
                conn.execute("insert into patient_tags(tag_id, patient_identity, name) values ('t2','dup','VIP')")
                conn.execute("insert into patient_tags(tag_id, patient_identity, name) values ('t3','dup','正畸意向')")
                conn.commit()
            self.assertEqual(client.post("/api/patients/keep/merge",
                                         json={"secondary": "dup", "reason": "重复"}).status_code, 200)
            with connect(db) as conn:
                names = sorted(r[0] for r in conn.execute(
                    "select name from patient_tags where patient_identity='keep'"))
                self.assertEqual(names, ["VIP", "正畸意向"], "标签应搬家且同名去重")
                self.assertEqual(conn.execute(
                    "select count(*) from patient_tags where patient_identity='dup'").fetchone()[0], 0)

    def test_merge_tables_cover_all_patient_identity_tables(self):
        # 治本守卫:schema 里凡带 patient_identity 列的表,必须要么进 _MERGE_TABLES,
        # 要么在下面的豁免清单里写明原因——新表忘登记合并清单直接红(已漏过两次)
        import re
        from local_app.routes.patient_merge import _MERGE_TABLES
        exempt = {
            "patients",             # 患者本体,合并的主语不重指
            "member_accounts",      # patient_identity 是主键,盲改撞主键,循环外按余额特例合并
            "patient_image_sets",   # 已修:主键(patient_identity,image_date)不能盲改,循环外特例合并(补缺+删次)
            "patient_tags",         # 已修:循环外特例合并(重指+按name去重),不走盲改循环
        }
        schema = (Path(__file__).resolve().parent.parent / "local_app" / "schema.sql").read_text()
        with_pid = set()
        for m in re.finditer(r"create table if not exists (\w+)\s*\((.*?)\);", schema, re.S | re.I):
            if re.search(r"^\s*patient_identity\b", m.group(2), re.M):
                with_pid.add(m.group(1))
        missing = with_pid - set(_MERGE_TABLES) - exempt
        self.assertFalse(missing, f"这些表带 patient_identity 但没进合并清单也没豁免: {sorted(missing)}")
        stale = set(_MERGE_TABLES) - with_pid
        self.assertFalse(stale, f"合并清单里这些表在 schema 中不存在或已无 patient_identity 列: {sorted(stale)}")


if __name__ == "__main__":
    unittest.main()
