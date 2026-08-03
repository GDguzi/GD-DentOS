"""P0-1 整单退费写入路径 + P1-12 缴费 pay_type/payment_record_type。
红线：只对本地单(local-bill- 前缀)退费；退费=负数 payment 流水自动冲减报表实收。"""
import tempfile
import unittest
import uuid
from pathlib import Path

from fastapi.testclient import TestClient

from local_app import auth
from local_app.api import create_app
from local_app.db import connect, init_db


def _client(tmp):
    db = Path(tmp) / "clinic.sqlite3"
    init_db(db)
    with connect(db) as conn:
        auth.create_user(conn, "admin", "管理员", "admin123", role="admin")  # P1-13：退费仅 admin
        conn.execute(
            "insert into patients(patient_identity, display_name, updated_at, current_hash) "
            "values ('p1', '测试患者', '2026-06-07 00:00:00', 'h1')"
        )
        conn.commit()
    client = TestClient(create_app(db))
    client.post("/api/auth/login", json={"username": "admin", "password": "admin123"})
    return db, client


def _make_paid_bill(client, total=1000, pay_method="现金"):
    oid = client.post("/api/patients/p1/treatment-orders", json={
        "items": [{"item_name": "处置", "unit_price": total, "quantity": 1}]}).json()["order_id"]
    bill = client.post(f"/api/treatment-orders/{oid}/price", json={}).json()["bill_id"]
    client.post(f"/api/bills/{bill}/pay", json={"methods": [{"method": pay_method, "amount": total}], "request_id": uuid.uuid4().hex})
    return bill


class PayTypeTest(unittest.TestCase):
    def test_charge_records_pay_type_and_record_type(self):
        # P1-12：收款写 pay_type(支付方式) + payment_record_type='charge'
        with tempfile.TemporaryDirectory() as tmp:
            db, client = _client(tmp)
            bill = _make_paid_bill(client, 500, pay_method="微信")
            with connect(db) as conn:
                row = conn.execute(
                    "select pay_type, payment_record_type from payments where bill_id=?", (bill,)
                ).fetchone()
            self.assertEqual(row["pay_type"], "微信")
            self.assertEqual(row["payment_record_type"], "charge")


class RefundTest(unittest.TestCase):
    def test_full_refund_reverses_bill(self):
        # ①负数流水+reason+record_type ②total/paid 不可变+state 推进 ③payments 净额=0(报表冲减) ④is_refund 置位
        with tempfile.TemporaryDirectory() as tmp:
            db, client = _client(tmp)
            bill = _make_paid_bill(client, 1000)
            resp = client.post(f"/api/bills/{bill}/refund", json={"refund_reason": "患者取消治疗", "request_id": uuid.uuid4().hex})
            self.assertEqual(resp.status_code, 200, resp.text)
            self.assertEqual(resp.json()["refunded_fee"], 1000)
            self.assertEqual(resp.json()["refundable"], 0)
            with connect(db) as conn:
                rows = conn.execute(
                    "select amount, payment_record_type, source_json from payments where bill_id=? order by pay_time", (bill,)
                ).fetchall()
                net = conn.execute("select coalesce(sum(amount),0) s from payments where bill_id=?", (bill,)).fetchone()["s"]
                b = conn.execute("select total_fee, paid_fee, state from bills where bill_id=?", (bill,)).fetchone()
                refunded_items = conn.execute(
                    "select count(*) c from treatment_items where bill_id=? and is_refund=1", (bill,)).fetchone()["c"]
            self.assertEqual(len(rows), 2)                       # 1 收 + 1 退
            self.assertEqual(rows[-1]["amount"], -1000)          # 负数冲减流水
            self.assertEqual(rows[-1]["payment_record_type"], "refund")
            self.assertIn("患者取消治疗", rows[-1]["source_json"])
            self.assertEqual(net, 0)                             # 报表实收净额冲减为 0
            # #813①:退款不再冲减 total/paid(历史应收/实收不可变),只推进 state
            self.assertEqual(b["total_fee"], 1000)
            self.assertEqual(b["paid_fee"], 1000)
            self.assertEqual(b["state"], "refunded")
            self.assertGreaterEqual(refunded_items, 1)

    def test_refund_writes_audit(self):
        with tempfile.TemporaryDirectory() as tmp:
            db, client = _client(tmp)
            bill = _make_paid_bill(client, 200)
            client.post(f"/api/bills/{bill}/refund", json={"refund_reason": "x", "request_id": uuid.uuid4().hex})
            with connect(db) as conn:
                actions = [r["action"] for r in conn.execute(
                    "select action from audit_logs where entity_type='bill' and entity_id=?", (bill,)).fetchall()]
            self.assertIn("refund", actions)

    def test_refund_requires_reason(self):
        with tempfile.TemporaryDirectory() as tmp:
            db, client = _client(tmp)
            bill = _make_paid_bill(client, 200)
            self.assertEqual(client.post(f"/api/bills/{bill}/refund", json={"request_id": uuid.uuid4().hex}).status_code, 400)
            self.assertEqual(client.post(f"/api/bills/{bill}/refund", json={"refund_reason": "  ", "request_id": uuid.uuid4().hex}).status_code, 400)

    def test_refund_unpaid_bill_rejected(self):
        # 未结清单不可整单退费
        with tempfile.TemporaryDirectory() as tmp:
            db, client = _client(tmp)
            oid = client.post("/api/patients/p1/treatment-orders", json={
                "items": [{"item_name": "处置", "unit_price": 300, "quantity": 1}]}).json()["order_id"]
            bill = client.post(f"/api/treatment-orders/{oid}/price", json={}).json()["bill_id"]  # pending
            self.assertEqual(client.post(f"/api/bills/{bill}/refund", json={"refund_reason": "x", "request_id": uuid.uuid4().hex}).status_code, 409)

    def test_refund_non_local_bill_rejected(self):
        # 红线：SaaS 同步单(非 local-bill- 前缀)绝不可退费
        with tempfile.TemporaryDirectory() as tmp:
            db, client = _client(tmp)
            with connect(db) as conn:
                conn.execute(
                    "insert into bills(bill_id, patient_identity, total_fee, paid_fee, state, updated_at) "
                    "values ('saas-bill-001', 'p1', 500, 500, 'paid', '2026-06-07 00:00:00')")
                conn.commit()
            self.assertEqual(
                client.post("/api/bills/saas-bill-001/refund", json={"refund_reason": "x", "request_id": uuid.uuid4().hex}).status_code, 409)

    def test_refund_not_found(self):
        with tempfile.TemporaryDirectory() as tmp:
            db, client = _client(tmp)
            self.assertEqual(client.post("/api/bills/nope/refund", json={"refund_reason": "x", "request_id": uuid.uuid4().hex}).status_code, 404)

    def test_partial_refund_keeps_paid_state(self):
        # 部分退款：已收1000退300 → 负数流水-300，可退降到700，state仍 paid，paid_fee 不动
        with tempfile.TemporaryDirectory() as tmp:
            db, client = _client(tmp)
            bill = _make_paid_bill(client, 1000)
            r = client.post(f"/api/bills/{bill}/refund",
                            json={"refund_reason": "部分退", "refund_amount": 300, "refund_method": "微信", "request_id": uuid.uuid4().hex})
            self.assertEqual(r.status_code, 200, r.text)
            self.assertEqual(r.json()["state"], "paid")
            self.assertEqual(r.json()["refunded_fee"], 300)
            self.assertEqual(r.json()["refundable"], 700)
            with connect(db) as conn:
                row = conn.execute("select amount, pay_type, source_json from payments "
                                   "where bill_id=? and payment_record_type='refund'", (bill,)).fetchone()
            self.assertEqual(row["amount"], -300)
            self.assertEqual(row["pay_type"], "微信")          # 退款方式落库

    def test_refund_by_item(self):
        # 按处置：两项各500，只退其中一项300 → 可退降到700，负数流水-300
        with tempfile.TemporaryDirectory() as tmp:
            db, client = _client(tmp)
            oid = client.post("/api/patients/p1/treatment-orders", json={"items": [
                {"item_name": "甲处置", "unit_price": 500, "quantity": 1},
                {"item_name": "乙处置", "unit_price": 500, "quantity": 1}]}).json()["order_id"]
            bill = client.post(f"/api/treatment-orders/{oid}/price", json={}).json()["bill_id"]
            client.post(f"/api/bills/{bill}/pay", json={"methods": [{"method": "现金", "amount": 1000}], "request_id": uuid.uuid4().hex})
            d = client.get(f"/api/bills/{bill}").json()
            iid = d["items"][0]["treatment_item_id"]
            r = client.post(f"/api/bills/{bill}/refund", json={
                "refund_reason": "退一项", "items": [{"treatment_item_id": iid, "amount": 300}], "request_id": uuid.uuid4().hex})
            self.assertEqual(r.status_code, 200, r.text)
            self.assertEqual(r.json()["refundable"], 700)
            self.assertEqual(r.json()["state"], "paid")
            with connect(db) as conn:
                neg = conn.execute("select amount from payments where bill_id=? and payment_record_type='refund'", (bill,)).fetchone()[0]
                refunded = conn.execute("select count(*) from treatment_items where bill_id=? and is_refund=1", (bill,)).fetchone()[0]
                part = conn.execute("select refunded_fee, is_refund from treatment_items where treatment_item_id=?", (iid,)).fetchone()
            self.assertEqual(neg, -300)
            # #573:部分退(500只退300)记账不锁死——refunded_fee 累计,is_refund 仍 0,剩余 200 还能按项退
            self.assertEqual(refunded, 0)
            self.assertEqual(part["refunded_fee"], 300)
            self.assertEqual(part["is_refund"] or 0, 0)

    def test_partial_item_refund_stays_refundable_573(self):
        # #573:部分退后剩余额度可继续按项退;超剩余额度拒;退满才标已退
        with tempfile.TemporaryDirectory() as tmp:
            db, client = _client(tmp)
            oid = client.post("/api/patients/p1/treatment-orders", json={"items": [
                {"item_name": "甲处置", "unit_price": 500, "quantity": 1},
                {"item_name": "乙处置", "unit_price": 500, "quantity": 1}]}).json()["order_id"]
            bill = client.post(f"/api/treatment-orders/{oid}/price", json={}).json()["bill_id"]
            client.post(f"/api/bills/{bill}/pay", json={"methods": [{"method": "现金", "amount": 1000}], "request_id": uuid.uuid4().hex})
            items = client.get(f"/api/bills/{bill}").json()["items"]
            iid = next(i["treatment_item_id"] for i in items if i["item_name"] == "甲处置")
            # 第一笔退 300 → 未锁死
            r1 = client.post(f"/api/bills/{bill}/refund", json={
                "refund_reason": "先退300", "items": [{"treatment_item_id": iid, "amount": 300}], "request_id": uuid.uuid4().hex})
            self.assertEqual(r1.status_code, 200, r1.text)
            # 超剩余额度(200)再退300 → 400 拒(防跨次超退)
            r2 = client.post(f"/api/bills/{bill}/refund", json={
                "refund_reason": "超退", "items": [{"treatment_item_id": iid, "amount": 300}], "request_id": uuid.uuid4().hex})
            self.assertEqual(r2.status_code, 400, r2.text)
            # 恰好退掉剩余200 → 成功且该项标已退
            r3 = client.post(f"/api/bills/{bill}/refund", json={
                "refund_reason": "退尾款", "items": [{"treatment_item_id": iid, "amount": 200}], "request_id": uuid.uuid4().hex})
            self.assertEqual(r3.status_code, 200, r3.text)
            with connect(db) as conn:
                row = conn.execute("select refunded_fee, is_refund from treatment_items "
                                   "where treatment_item_id=?", (iid,)).fetchone()
            self.assertEqual(row["refunded_fee"], 500)
            self.assertEqual(row["is_refund"], 1)   # 退满出局
            # 已退满的项再退 → 400「不属于该账单或已退」
            r4 = client.post(f"/api/bills/{bill}/refund", json={
                "refund_reason": "再退", "items": [{"treatment_item_id": iid, "amount": 1}], "request_id": uuid.uuid4().hex})
            self.assertEqual(r4.status_code, 400)

    def test_partial_refund_no_phantom_arrears(self):
        # #143→#813①：部分退款不能让该单冒出"欠费"(退费≠欠费)。total/paid 均不动,欠款式恒为 0。
        with tempfile.TemporaryDirectory() as tmp:
            db, client = _client(tmp)
            bill = _make_paid_bill(client, 1000)
            client.post(f"/api/bills/{bill}/refund", json={"refund_reason": "部分退", "refund_amount": 300, "request_id": uuid.uuid4().hex})
            with connect(db) as conn:
                tot, pf = conn.execute("select total_fee, paid_fee from bills where bill_id=?", (bill,)).fetchone()
            self.assertEqual(tot, 1000)           # #813①:应收史实不可变
            self.assertEqual(pf, 1000)
            arr = client.get("/api/reports/arrears").json()
            self.assertFalse(any(r["bill_id"] == bill for r in arr["rows"]), "部分退款单不应出现在欠费清单")

    def test_item_refund_rejects_foreign_item(self):
        # #144：items 里混入非本单项目(即使金额0)也要拒，不能静默忽略
        with tempfile.TemporaryDirectory() as tmp:
            db, client = _client(tmp)
            bill = _make_paid_bill(client, 500)
            iid = client.get(f"/api/bills/{bill}").json()["items"][0]["treatment_item_id"]
            r = client.post(f"/api/bills/{bill}/refund", json={"refund_reason": "x", "items": [
                {"treatment_item_id": iid, "amount": 100},
                {"treatment_item_id": "foreign-xxx", "amount": 0}], "request_id": uuid.uuid4().hex})
            self.assertEqual(r.status_code, 400)

    def _two_item_paid_bill(self, client):
        oid = client.post("/api/patients/p1/treatment-orders", json={"items": [
            {"item_name": "甲", "unit_price": 500, "quantity": 1},
            {"item_name": "乙", "unit_price": 500, "quantity": 1}]}).json()["order_id"]
        bill = client.post(f"/api/treatment-orders/{oid}/price", json={}).json()["bill_id"]
        client.post(f"/api/bills/{bill}/pay", json={"methods": [{"method": "现金", "amount": 1000}], "request_id": uuid.uuid4().hex})
        return bill, client.get(f"/api/bills/{bill}").json()["items"]

    def test_item_refund_cannot_double_refund_same_item(self):
        # CRITICAL：对同一项目重复退款必须被拦(否则500的项退1000,真实损失)
        with tempfile.TemporaryDirectory() as tmp:
            db, client = _client(tmp)
            bill, items = self._two_item_paid_bill(client)
            a = items[0]["treatment_item_id"]
            r1 = client.post(f"/api/bills/{bill}/refund", json={
                "refund_reason": "退甲", "items": [{"treatment_item_id": a, "amount": 500}], "request_id": uuid.uuid4().hex})
            self.assertEqual(r1.status_code, 200)
            # 同一项再退 → 400(已退),退款流水保持-500不被再冲
            r2 = client.post(f"/api/bills/{bill}/refund", json={
                "refund_reason": "再退甲", "items": [{"treatment_item_id": a, "amount": 500}], "request_id": uuid.uuid4().hex})
            self.assertEqual(r2.status_code, 400)
            with connect(db) as conn:
                neg = conn.execute("select coalesce(sum(amount),0) from payments "
                                   "where bill_id=? and payment_record_type='refund'", (bill,)).fetchone()[0]
            self.assertEqual(neg, -500)   # 只退了甲500,不会被退成-1000

    def test_item_refund_same_request_duplicate_rows_capped(self):
        # 看板#183：同一请求里同一 item 拆两行(300+300)累计600超过该项500,必须拒绝且一分不退
        with tempfile.TemporaryDirectory() as tmp:
            db, client = _client(tmp)
            bill, items = self._two_item_paid_bill(client)
            a = items[0]["treatment_item_id"]
            r = client.post(f"/api/bills/{bill}/refund", json={
                "refund_reason": "拆行超退", "items": [
                    {"treatment_item_id": a, "amount": 300},
                    {"treatment_item_id": a, "amount": 300}], "request_id": uuid.uuid4().hex})
            self.assertEqual(r.status_code, 400)
            with connect(db) as conn:
                paid = conn.execute("select paid_fee from bills where bill_id=?", (bill,)).fetchone()[0]
                neg = conn.execute("select count(*) from payments where bill_id=? and amount<0", (bill,)).fetchone()[0]
            self.assertEqual(paid, 1000)   # 一分没退
            self.assertEqual(neg, 0)       # 没有负数流水

    def test_item_refund_cannot_exceed_item_amount(self):
        # CRITICAL：单项退款不能超过该项目自身金额(500的项不能退600)
        with tempfile.TemporaryDirectory() as tmp:
            db, client = _client(tmp)
            bill, items = self._two_item_paid_bill(client)
            r = client.post(f"/api/bills/{bill}/refund", json={
                "refund_reason": "超额", "items": [{"treatment_item_id": items[0]["treatment_item_id"], "amount": 600}], "request_id": uuid.uuid4().hex})
            self.assertEqual(r.status_code, 400)

    def test_refund_method_credits_real_method_not_unfilled(self):
        # 审查#11/#27：现金退费冲减"现金"桶净额,不归入"未填"负数桶
        with tempfile.TemporaryDirectory() as tmp:
            db, client = _client(tmp)
            bill = _make_paid_bill(client, 500, pay_method="现金")
            client.post(f"/api/bills/{bill}/refund", json={"refund_reason": "退", "refund_method": "现金", "request_id": uuid.uuid4().hex})
            bm = {m["method"]: m["amount"] for m in client.get("/api/reports/today-collection").json()["by_method"]}
            self.assertNotIn("未填", bm)            # 退费不落未填桶
            self.assertEqual(bm.get("现金", 0), 0)   # 现金净额=收500-退500=0

    def test_arrears_no_phantom_from_float_residue(self):
        # 审查#9：total=100.0 paid=99.999 浮点残差不应冒"幻影欠费"
        with tempfile.TemporaryDirectory() as tmp:
            db, client = _client(tmp)
            with connect(db) as conn:
                conn.execute("insert into bills(bill_id, patient_identity, bill_no, bill_time, total_fee, paid_fee, state) "
                             "values ('local-bill-ph','p1','B','2026-06-10', 100.0, 99.999, 'paid')")
                conn.commit()
            arr = client.get("/api/reports/arrears").json()
            self.assertFalse(any(r["bill_id"] == "local-bill-ph" for r in arr["rows"]))

    def test_over_refund_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            db, client = _client(tmp)
            bill = _make_paid_bill(client, 500)
            self.assertEqual(client.post(f"/api/bills/{bill}/refund",
                             json={"refund_reason": "x", "refund_amount": 800, "request_id": uuid.uuid4().hex}).status_code, 400)
            self.assertEqual(client.post(f"/api/bills/{bill}/refund",
                             json={"refund_reason": "x", "refund_amount": 0, "request_id": uuid.uuid4().hex}).status_code, 400)


class RefundImmutableHistoryTest(unittest.TestCase):
    """#813①:退款只追加负数流水,bills.total_fee/paid_fee 史实不可变。"""

    def test_cross_period_refund_freezes_history_report(self):
        # 跨账期全退:截止日已出的历史收入/欠款报表不得因未来退款变化(修前:应收100→0,期末欠款0→-100)
        with tempfile.TemporaryDirectory() as tmp:
            db, client = _client(tmp)
            bill = _make_paid_bill(client, 100)
            d1 = "2026-07-10"
            with connect(db) as conn:
                conn.execute("update bills set bill_time=? where bill_id=?", (d1 + " 10:00:00", bill))
                conn.execute("update payments set pay_time=? where bill_id=?", (d1 + " 10:05:00", bill))
                conn.commit()
            before = client.get(f"/api/reports/income?date_from={d1}&date_to={d1}").json()
            self.assertEqual(client.post(f"/api/bills/{bill}/refund",
                                         json={"refund_reason": "跨账期全退", "request_id": uuid.uuid4().hex}).status_code, 200)
            after = client.get(f"/api/reports/income?date_from={d1}&date_to={d1}").json()
            self.assertEqual(before, after)

    def test_refundable_cap_across_partial_refunds(self):
        # 100收满→退40再退40→可退20;退30拒;不传金额收口20→refunded;退完再退409
        with tempfile.TemporaryDirectory() as tmp:
            db, client = _client(tmp)
            bill = _make_paid_bill(client, 100)
            r1 = client.post(f"/api/bills/{bill}/refund", json={"refund_reason": "退1", "refund_amount": 40, "request_id": uuid.uuid4().hex})
            self.assertEqual(r1.json()["refundable"], 60)
            r2 = client.post(f"/api/bills/{bill}/refund", json={"refund_reason": "退2", "refund_amount": 40, "request_id": uuid.uuid4().hex})
            self.assertEqual(r2.json()["refundable"], 20)
            self.assertEqual(client.post(f"/api/bills/{bill}/refund",
                             json={"refund_reason": "超退", "refund_amount": 30, "request_id": uuid.uuid4().hex}).status_code, 400)
            r4 = client.post(f"/api/bills/{bill}/refund", json={"refund_reason": "收口", "request_id": uuid.uuid4().hex})
            self.assertEqual(r4.json()["refund_amount"], 20)
            self.assertEqual(r4.json()["state"], "refunded")
            self.assertEqual(client.post(f"/api/bills/{bill}/refund",
                                         json={"refund_reason": "再退", "request_id": uuid.uuid4().hex}).status_code, 409)
            d = client.get(f"/api/bills/{bill}").json()
            self.assertEqual((d["total_fee"], d["paid_fee"]), (100, 100))
            self.assertEqual((d["refunded_fee"], d["refundable"]), (100, 0))

    def test_migration_restores_old_style_refunded_bill(self):
        # #813①回填:老口径被冲减过的单一次性加回(total/paid 同加,欠款差0);标记锁死不翻倍
        from local_app.migrations import restore_refunded_bill_amounts
        with tempfile.TemporaryDirectory() as tmp:
            db, client = _client(tmp)
            with connect(db) as conn:
                conn.execute(
                    "insert into bills(bill_id, patient_identity, bill_time, total_fee, paid_fee, state, updated_at) "
                    "values ('local-bill-old', 'p1', '2026-06-01 10:00:00', 700, 700, 'paid', '2026-06-01')")
                conn.execute(
                    "insert into payments(payment_id, patient_identity, bill_id, pay_time, amount, "
                    "payment_record_type, updated_at) values ('local-refund-old', 'p1', 'local-bill-old', "
                    "'2026-06-02 10:00:00', -300, 'refund', '2026-06-02')")
                conn.commit()
            self.assertTrue(restore_refunded_bill_amounts(db))
            with connect(db) as conn:
                tot, pf = conn.execute(
                    "select total_fee, paid_fee from bills where bill_id='local-bill-old'").fetchone()
            self.assertEqual((tot, pf), (1000, 1000))
            self.assertFalse(restore_refunded_bill_amounts(db))   # 二次调用直接跳过
            with connect(db) as conn:
                tot2 = conn.execute("select total_fee from bills where bill_id='local-bill-old'").fetchone()[0]
            self.assertEqual(tot2, 1000)


if __name__ == "__main__":
    unittest.main()


class RefundItemAttributionTest(unittest.TestCase):
    """#802:退款在落库时确定项目归属(refund_items),分类报表精确记类别。"""

    def _two_type_bill(self, client, fees=((700, "治疗费", "治疗"), (300, "检查费", "检查"))):
        oid = client.post("/api/patients/p1/treatment-orders", json={"items": [
            {"item_name": name, "unit_price": amt, "quantity": 1, "fee_type": ft}
            for amt, ft, name in fees]}).json()["order_id"]
        bill = client.post(f"/api/treatment-orders/{oid}/price", json={}).json()["bill_id"]
        total = sum(a for a, _, _ in fees)
        client.post(f"/api/bills/{bill}/pay", json={
            "methods": [{"method": "现金", "amount": total}], "request_id": uuid.uuid4().hex})
        return bill

    def test_item_refund_exact_category_not_proportional(self):
        # 治疗700+检查300 只退检查300 → 分类报表退款行=检查费-300(修前按比例 治疗-210/检查-90)
        with tempfile.TemporaryDirectory() as tmp:
            db, client = _client(tmp)
            bill = self._two_type_bill(client)
            iid = next(i["treatment_item_id"] for i in client.get(f"/api/bills/{bill}").json()["items"]
                       if i["fee_type"] == "检查费")
            r = client.post(f"/api/bills/{bill}/refund", json={
                "refund_reason": "只退检查", "items": [{"treatment_item_id": iid, "amount": 300}],
                "request_id": uuid.uuid4().hex})
            self.assertEqual(r.status_code, 200, r.text)
            q = client.get("/api/reports/cashier-query?date_from=2026-01-01&date_to=2030-01-01").json()
            neg = [row for row in q["rows"] if row["amount"] < 0]
            self.assertEqual(len(neg), 1)
            self.assertEqual(neg[0]["categories"], {"检查费": -300.0})

    def test_whole_partial_refund_allocates_to_items(self):
        # 甲60+乙40 整单部分退两次40 → refunded_fee 按剩余额比例累计合计80;流水带 refund_items;
        # 恒等式:Σ(line_fee−refunded_fee) = paid − Σ退款流水
        with tempfile.TemporaryDirectory() as tmp:
            db, client = _client(tmp)
            bill = self._two_type_bill(client, fees=((60, "治疗费", "甲"), (40, "检查费", "乙")))
            for i in range(2):
                r = client.post(f"/api/bills/{bill}/refund", json={
                    "refund_reason": f"部分退{i}", "refund_amount": 40, "request_id": uuid.uuid4().hex})
                self.assertEqual(r.status_code, 200, r.text)
            with connect(db) as conn:
                srefunded = conn.execute(
                    "select round(coalesce(sum(refunded_fee),0),2) from treatment_items where bill_id=?",
                    (bill,)).fetchone()[0]
                sline = conn.execute(
                    "select round(coalesce(sum(total_fee),0),2) from treatment_items where bill_id=?",
                    (bill,)).fetchone()[0]
                flows = conn.execute(
                    "select round(coalesce(sum(amount),0),2) from payments "
                    "where bill_id=? and payment_record_type='refund'", (bill,)).fetchone()[0]
                sj = conn.execute(
                    "select source_json from payments where bill_id=? and payment_record_type='refund' "
                    "order by payment_id limit 1", (bill,)).fetchone()[0]
            self.assertEqual(srefunded, 80)
            self.assertEqual(round(sline - srefunded, 2), round(100 + flows, 2))   # 20 == 100-80
            self.assertIn("refund_items", sj)
            d = client.get(f"/api/bills/{bill}").json()
            self.assertEqual(d["refundable"], 20)
            self.assertTrue(all("refunded_fee" in it for it in d["items"]))

    def test_full_refund_marks_items_and_fills_refunded_fee(self):
        # 整单全退:每项 refunded_fee=总价 且 is_refund=1(旧口径只标 is_refund 不记金额)
        with tempfile.TemporaryDirectory() as tmp:
            db, client = _client(tmp)
            bill = self._two_type_bill(client, fees=((60, "治疗费", "甲"), (40, "检查费", "乙")))
            r = client.post(f"/api/bills/{bill}/refund",
                            json={"refund_reason": "全退", "request_id": uuid.uuid4().hex})
            self.assertEqual(r.status_code, 200, r.text)
            with connect(db) as conn:
                rows = conn.execute("select total_fee, refunded_fee, is_refund from treatment_items "
                                    "where bill_id=?", (bill,)).fetchall()
            for row in rows:
                self.assertEqual(row["refunded_fee"], row["total_fee"])
                self.assertEqual(row["is_refund"], 1)


class RefundAllocationRoundingTest(unittest.TestCase):
    """#802 P2:小额整单退款分摊到多项时,Σ分摊必须精确等于退款额,每项不超剩余额。"""

    def test_tiny_refund_across_items_sums_exact(self):
        with tempfile.TemporaryDirectory() as tmp:
            db, client = _client(tmp)
            oid = client.post("/api/patients/p1/treatment-orders", json={"items": [
                {"item_name": f"i{i}", "unit_price": 1, "quantity": 1, "fee_type": f"f{i}"}
                for i in range(4)]}).json()["order_id"]
            bill = client.post(f"/api/treatment-orders/{oid}/price", json={}).json()["bill_id"]
            client.post(f"/api/bills/{bill}/pay", json={
                "methods": [{"method": "现金", "amount": 4}], "request_id": uuid.uuid4().hex})
            # 退 0.02:旧逻辑独立四舍五入落 3 分(0.03)超退;修后精确 0.02
            r = client.post(f"/api/bills/{bill}/refund", json={
                "refund_reason": "小额分摊", "refund_amount": 0.02, "request_id": uuid.uuid4().hex})
            self.assertEqual(r.status_code, 200, r.text)
            with connect(db) as conn:
                item_sum = conn.execute(
                    "select round(coalesce(sum(refunded_fee),0),2) from treatment_items where bill_id=?",
                    (bill,)).fetchone()[0]
                sj = conn.execute("select source_json from payments where bill_id=? and "
                                  "payment_record_type='refund'", (bill,)).fetchone()[0]
            import json as _json
            ri_sum = round(sum(x["amount"] for x in _json.loads(sj)["refund_items"]), 2)
            self.assertEqual(item_sum, 0.02)   # 项目累计 == 退款额,不超退
            self.assertEqual(ri_sum, 0.02)     # 流水归属合计 == 退款额


class RefundedBillClosesAllItemsTest(unittest.TestCase):
    """#802 P1:账单退到 refunded 态 ⟺ 全部项目退满(is_refund=1, refunded_fee=total)。"""

    def test_multi_item_full_refund_via_amounts_closes_all(self):
        # 两项 60/40,分次整单部分退到全清 → 两项都应 is_refund=1 且 refunded_fee=total
        with tempfile.TemporaryDirectory() as tmp:
            db, client = _client(tmp)
            oid = client.post("/api/patients/p1/treatment-orders", json={"items": [
                {"item_name": "甲", "unit_price": 60, "quantity": 1, "fee_type": "治疗费"},
                {"item_name": "乙", "unit_price": 40, "quantity": 1, "fee_type": "检查费"}]}).json()["order_id"]
            bill = client.post(f"/api/treatment-orders/{oid}/price", json={}).json()["bill_id"]
            client.post(f"/api/bills/{bill}/pay", json={
                "methods": [{"method": "现金", "amount": 100}], "request_id": uuid.uuid4().hex})
            for amt in (40, 40, 20):
                r = client.post(f"/api/bills/{bill}/refund", json={
                    "refund_reason": "分次退", "refund_amount": amt, "request_id": uuid.uuid4().hex})
                self.assertEqual(r.status_code, 200, r.text)
            with connect(db) as conn:
                rows = conn.execute("select total_fee, refunded_fee, is_refund from treatment_items "
                                    "where bill_id=?", (bill,)).fetchall()
                state = conn.execute("select state from bills where bill_id=?", (bill,)).fetchone()[0]
            self.assertEqual(state, "refunded")
            for row in rows:
                self.assertEqual(row["is_refund"], 1)
                self.assertEqual(row["refunded_fee"], row["total_fee"])

    def test_migration_closes_items_for_legacy_full_refund(self):
        # 老口径整单退:账单被冲减到 paid=0/state=refunded,items refunded_fee=0/is_refund=0。
        # 回填迁移应把项目退满写实。
        from local_app.migrations import restore_refunded_bill_amounts
        with tempfile.TemporaryDirectory() as tmp:
            db, client = _client(tmp)
            with connect(db) as conn:
                conn.execute("insert into bills(bill_id, patient_identity, bill_time, total_fee, paid_fee, "
                             "state, updated_at) values ('local-bill-lg', 'p1', '2026-05-01', 0, 0, "
                             "'refunded', '2026-05-02')")
                conn.execute("insert into treatment_items(treatment_item_id, bill_id, patient_identity, "
                             "item_name, total_fee, refunded_fee, is_refund) "
                             "values ('ti-lg', 'local-bill-lg', 'p1', '处置', 500, 0, 0)")
                conn.execute("insert into payments(payment_id, patient_identity, bill_id, pay_time, amount, "
                             "payment_record_type, updated_at) values ('r-lg','p1','local-bill-lg',"
                             "'2026-05-02',-500,'refund','2026-05-02')")
                conn.commit()
            self.assertTrue(restore_refunded_bill_amounts(db))
            with connect(db) as conn:
                tot, pf = conn.execute("select total_fee, paid_fee from bills where bill_id='local-bill-lg'").fetchone()
                it = conn.execute("select refunded_fee, is_refund from treatment_items "
                                  "where treatment_item_id='ti-lg'").fetchone()
            self.assertEqual((tot, pf), (500, 500))          # 账单总额/已收回填
            self.assertEqual(it["is_refund"], 1)             # 项目标退清(维持不变式)
            self.assertEqual(it["refunded_fee"], 500)        # 按实退流水(500)回填,非折扣单=行价


class DiscountedBillRefundTest(unittest.TestCase):
    """#802 三审 P1:整单折扣单全退,项目 refunded_fee 记实退净额,不虚增到折前行价。"""

    def test_full_refund_on_discounted_bill_no_inflation(self):
        # 两项各 100(行价合计 200)+整单优惠 40 → 应收/已收=160。全退返 160。
        # item.total_fee 是折前 100,但每项实退净额=100*160/200=80,不能顶到 100。
        with tempfile.TemporaryDirectory() as tmp:
            db, client = _client(tmp)
            oid = client.post("/api/patients/p1/treatment-orders", json={"items": [
                {"item_name": "甲", "unit_price": 100, "quantity": 1, "fee_type": "治疗费"},
                {"item_name": "乙", "unit_price": 100, "quantity": 1, "fee_type": "检查费"}]}).json()["order_id"]
            bill = client.post(f"/api/treatment-orders/{oid}/price",
                               json={"discount": 40}).json()["bill_id"]
            client.post(f"/api/bills/{bill}/pay", json={
                "methods": [{"method": "现金", "amount": 160}], "request_id": uuid.uuid4().hex})
            r = client.post(f"/api/bills/{bill}/refund",
                            json={"refund_reason": "全退", "request_id": uuid.uuid4().hex})
            self.assertEqual(r.status_code, 200, r.text)
            self.assertEqual(r.json()["refund_amount"], 160)   # 只退实际已收
            with connect(db) as conn:
                rows = conn.execute("select total_fee, refunded_fee, is_refund from treatment_items "
                                    "where bill_id=? order by treatment_item_id", (bill,)).fetchall()
                flows = conn.execute("select round(coalesce(sum(-amount),0),2) from payments "
                                     "where bill_id=? and payment_record_type='refund'", (bill,)).fetchone()[0]
            for row in rows:
                self.assertEqual(row["is_refund"], 1)              # 退清标记
                self.assertEqual(row["refunded_fee"], 80)         # 实退净额,非折前 100
                self.assertLess(row["refunded_fee"], row["total_fee"])
            # Σ项目实退 == 退款流水额 == 已收
            self.assertEqual(round(sum(x["refunded_fee"] for x in rows), 2), flows)
            self.assertEqual(flows, 160)


class LegacyPartialRefundBackfillTest(unittest.TestCase):
    """#802 五审 P1:老口径整单部分退未落项目,迁移按实退额比例回填,后续按项退不再超退/负类别。"""

    def test_legacy_whole_partial_then_item_refund_stays_consistent(self):
        from local_app.migrations import restore_refunded_bill_amounts
        with tempfile.TemporaryDirectory() as tmp:
            db, client = _client(tmp)
            # 老数据:两类各50(行价合计100),老口径整单退80→账单被减到 total=20/paid=20,项目未标
            with connect(db) as conn:
                conn.execute("insert into bills(bill_id, patient_identity, bill_time, total_fee, paid_fee, "
                             "state, source_json, updated_at) values ('local-bill-lp','p1','2026-05-01',"
                             "20,20,'paid','{\"origin\":\"local_order\"}','2026-05-02')")
                conn.execute("insert into treatment_items(treatment_item_id, bill_id, patient_identity, "
                             "item_name, total_fee, fee_type, refunded_fee, is_refund) values "
                             "('ti-a','local-bill-lp','p1','甲',50,'治疗费',0,0),"
                             "('ti-b','local-bill-lp','p1','乙',50,'检查费',0,0)")
                conn.execute("insert into payments(payment_id, patient_identity, bill_id, pay_time, amount, "
                             "pay_type, payment_record_type, updated_at) values "
                             "('pay-lp','p1','local-bill-lp','2026-05-01',100,'现金','charge','2026-05-01'),"
                             "('r-lp','p1','local-bill-lp','2026-05-02',-80,'现金','refund','2026-05-02')")
                conn.commit()
            self.assertTrue(restore_refunded_bill_amounts(db))
            with connect(db) as conn:
                rows = {r["treatment_item_id"]: r["refunded_fee"] for r in conn.execute(
                    "select treatment_item_id, refunded_fee from treatment_items where bill_id='local-bill-lp'")}
                tot, pf = conn.execute(
                    "select total_fee, paid_fee from bills where bill_id='local-bill-lp'").fetchone()
            self.assertEqual((tot, pf), (100, 100))          # 账单史实回填
            self.assertEqual(rows["ti-a"], 40)               # 80 按剩余额均分 → 各 40
            self.assertEqual(rows["ti-b"], 40)
            # 现在按项退:甲剩余额=50-40=10,退 20 应被封顶拒(否则报表出负类别)
            iid_a = "ti-a"
            r_over = client.post("/api/bills/local-bill-lp/refund", json={
                "refund_reason": "超退甲", "items": [{"treatment_item_id": iid_a, "amount": 20}],
                "request_id": uuid.uuid4().hex})
            self.assertEqual(r_over.status_code, 400)
            # 退 10(=剩余额)成功,甲退满
            r_ok = client.post("/api/bills/local-bill-lp/refund", json={
                "refund_reason": "退甲尾", "items": [{"treatment_item_id": iid_a, "amount": 10}],
                "request_id": uuid.uuid4().hex})
            self.assertEqual(r_ok.status_code, 200, r_ok.text)
            with connect(db) as conn:
                a = conn.execute("select refunded_fee, is_refund from treatment_items "
                                 "where treatment_item_id='ti-a'").fetchone()
            self.assertEqual(a["refunded_fee"], 50)
            self.assertEqual(a["is_refund"], 1)


class MigrationSkipsNewStyleRefundTest(unittest.TestCase):
    """#802/#813① 六审 P1:回填迁移只加回老口径退款(已冲减账单);新口径退款(未冲减)不得再加。"""

    def test_new_style_refund_then_migration_keeps_totals(self):
        from local_app.migrations import restore_refunded_bill_amounts
        with tempfile.TemporaryDirectory() as tmp:
            db, client = _client(tmp)
            # 新口径:当前接口生成 100 已收单并部分退 40 → total/paid 仍 100/100(不冲减)
            oid = client.post("/api/patients/p1/treatment-orders", json={
                "items": [{"item_name": "x", "unit_price": 100, "quantity": 1}]}).json()["order_id"]
            bill = client.post(f"/api/treatment-orders/{oid}/price", json={}).json()["bill_id"]
            client.post(f"/api/bills/{bill}/pay", json={
                "methods": [{"method": "现金", "amount": 100}], "request_id": uuid.uuid4().hex})
            client.post(f"/api/bills/{bill}/refund", json={
                "refund_reason": "新口径退", "refund_amount": 40, "request_id": uuid.uuid4().hex})
            with connect(db) as conn:
                before = tuple(conn.execute(
                    "select total_fee, paid_fee from bills where bill_id=?", (bill,)).fetchone())
            # 手插一张老口径单(无 refund_items)确认迁移仍会修老单
            with connect(db) as conn:
                conn.execute("insert into bills(bill_id, patient_identity, bill_time, total_fee, paid_fee, "
                             "state, updated_at) values ('local-bill-old2','p1','2026-05-01',20,20,'paid','2026-05-02')")
                conn.execute("insert into payments(payment_id, patient_identity, bill_id, pay_time, amount, "
                             "payment_record_type, source_json, updated_at) values ('r-old2','p1',"
                             "'local-bill-old2','2026-05-02',-80,'refund','{}','2026-05-02')")
                conn.commit()
            self.assertTrue(restore_refunded_bill_amounts(db))
            with connect(db) as conn:
                after = tuple(conn.execute(
                    "select total_fee, paid_fee from bills where bill_id=?", (bill,)).fetchone())
                old = tuple(conn.execute(
                    "select total_fee, paid_fee from bills where bill_id='local-bill-old2'").fetchone())
            self.assertEqual(after, before)       # 新口径单不被迁移改动(不虚增)
            self.assertEqual(after, (100.0, 100.0))
            self.assertEqual(old, (100.0, 100.0))  # 老口径单仍正常回填
