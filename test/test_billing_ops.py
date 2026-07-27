import tempfile
import unittest
import uuid
from pathlib import Path

from fastapi.testclient import TestClient

from local_app.api import create_app
from local_app.db import connect, init_db


def _client(tmp):
    db = Path(tmp) / "clinic.sqlite3"
    init_db(db)
    with connect(db) as conn:
        conn.execute(
            "insert into patients(patient_identity, display_name, updated_at, current_hash) "
            "values ('p1', '测试患者', '2026-06-07 00:00:00', 'h1')"
        )
        conn.commit()
    return db, TestClient(create_app(db))


def _make_bill(client, total=1000):
    # 新增处置 → 划价 生成一张 pending 待收费单
    oid = client.post("/api/patients/p1/treatment-orders", json={
        "items": [{"item_name": "处置", "unit_price": total, "quantity": 1}]}).json()["order_id"]
    return client.post(f"/api/treatment-orders/{oid}/price", json={}).json()["bill_id"]


class BillDetailTest(unittest.TestCase):
    def test_get_bill_returns_items_and_totals(self):
        # GET /api/bills/{id} 返回账单+明细(单价/数量/总价/减免)
        with tempfile.TemporaryDirectory() as tmp:
            db, client = _client(tmp)
            oid = client.post("/api/patients/p1/treatment-orders", json={
                "items": [{"item_name": "树脂", "unit_price": 200, "quantity": 2}]}).json()["order_id"]
            items = client.get("/api/patients/p1/treatment-orders").json()["orders"][0]["items"]
            # 树脂原价400，直填320 → 减免80
            bill = client.post(f"/api/treatment-orders/{oid}/price", json={
                "items": [{"item_id": items[0]["item_id"], "amount": 320}]}).json()["bill_id"]
            d = client.get(f"/api/bills/{bill}").json()
            self.assertEqual(d["total_fee"], 320)
            self.assertEqual(d["remaining"], 320)
            self.assertEqual(len(d["items"]), 1)
            it = d["items"][0]
            self.assertEqual(it["gross"], 400)
            self.assertEqual(it["line_fee"], 320)
            self.assertEqual(it["reduce"], 80)

    def test_get_bill_404(self):
        with tempfile.TemporaryDirectory() as tmp:
            db, client = _client(tmp)
            self.assertEqual(client.get("/api/bills/nope").status_code, 404)

    def test_cashier_stored_in_payment(self):
        # 收银员落到 payment.source_json
        with tempfile.TemporaryDirectory() as tmp:
            db, client = _client(tmp)
            bill = _make_bill(client, 500)
            client.post(f"/api/bills/{bill}/pay", json={"methods": [{"method": "微信", "amount": 500}], "cashier": "前台小王", "request_id": uuid.uuid4().hex})
            with connect(db) as conn:
                sj = conn.execute("select source_json from payments where bill_id=?", (bill,)).fetchone()["source_json"]
            self.assertIn("前台小王", sj)


class BillingPayTest(unittest.TestCase):
    def test_full_payment_marks_paid(self):
        with tempfile.TemporaryDirectory() as tmp:
            db, client = _client(tmp)
            bill = _make_bill(client, 1000)
            resp = client.post(f"/api/bills/{bill}/pay", json={"methods": [{"method": "现金", "amount": 1000}], "request_id": uuid.uuid4().hex})
            self.assertEqual(resp.status_code, 200)
            body = resp.json()
            self.assertEqual(body["paid_fee"], 1000)
            self.assertEqual(body["state"], "paid")
            self.assertEqual(body["remaining"], 0)
            with connect(db) as conn:
                pay = conn.execute("select count(*) c, coalesce(sum(amount),0) s from payments where bill_id=?", (bill,)).fetchone()
            self.assertEqual(pay["c"], 1)
            self.assertEqual(pay["s"], 1000)

    def test_partial_then_full(self):
        with tempfile.TemporaryDirectory() as tmp:
            db, client = _client(tmp)
            bill = _make_bill(client, 1000)
            r1 = client.post(f"/api/bills/{bill}/pay", json={"methods": [{"method": "现金", "amount": 400}], "request_id": uuid.uuid4().hex})
            self.assertEqual(r1.json()["state"], "partial")
            self.assertEqual(r1.json()["remaining"], 600)
            r2 = client.post(f"/api/bills/{bill}/pay", json={"methods": [{"method": "现金", "amount": 600}], "request_id": uuid.uuid4().hex})
            self.assertEqual(r2.json()["state"], "paid")
            self.assertEqual(r2.json()["remaining"], 0)

    def test_overpay_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            db, client = _client(tmp)
            bill = _make_bill(client, 1000)
            resp = client.post(f"/api/bills/{bill}/pay", json={"methods": [{"method": "现金", "amount": 1500}], "request_id": uuid.uuid4().hex})
            self.assertEqual(resp.status_code, 400)

    def test_zero_or_negative_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            db, client = _client(tmp)
            bill = _make_bill(client, 1000)
            self.assertEqual(client.post(f"/api/bills/{bill}/pay", json={"methods": [{"method": "现金", "amount": 0}], "request_id": uuid.uuid4().hex}).status_code, 400)
            self.assertEqual(client.post(f"/api/bills/{bill}/pay", json={"methods": [{"method": "现金", "amount": -5}], "request_id": uuid.uuid4().hex}).status_code, 400)

    def test_pay_paid_bill_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            db, client = _client(tmp)
            bill = _make_bill(client, 100)
            client.post(f"/api/bills/{bill}/pay", json={"methods": [{"method": "现金", "amount": 100}], "request_id": uuid.uuid4().hex})
            resp = client.post(f"/api/bills/{bill}/pay", json={"methods": [{"method": "现金", "amount": 10}], "request_id": uuid.uuid4().hex})
            self.assertEqual(resp.status_code, 409)

    def test_nan_inf_amount_rejected(self):
        # nan/inf 非有限金额返回 400 而非 500
        with tempfile.TemporaryDirectory() as tmp:
            db, client = _client(tmp)
            bill = _make_bill(client, 100)
            self.assertEqual(client.post(f"/api/bills/{bill}/pay", json={"methods": [{"method": "现金", "amount": "nan"}], "request_id": uuid.uuid4().hex}).status_code, 400)
            self.assertEqual(client.post(f"/api/bills/{bill}/pay", json={"methods": [{"method": "现金", "amount": "inf"}], "request_id": uuid.uuid4().hex}).status_code, 400)
            with connect(db) as conn:
                self.assertEqual(conn.execute("select count(*) from payments").fetchone()[0], 0)

    def test_pay_method_recorded(self):
        # 收费方式落库
        with tempfile.TemporaryDirectory() as tmp:
            db, client = _client(tmp)
            bill = _make_bill(client, 100)
            client.post(f"/api/bills/{bill}/pay", json={"methods": [{"method": "微信", "amount": 100}], "request_id": uuid.uuid4().hex})
            with connect(db) as conn:
                sj = conn.execute("select source_json from payments where bill_id=?", (bill,)).fetchone()["source_json"]
            self.assertIn("微信", sj)

    def test_bill_not_found(self):
        with tempfile.TemporaryDirectory() as tmp:
            db, client = _client(tmp)
            self.assertEqual(client.post("/api/bills/nope/pay", json={"methods": [{"method": "现金", "amount": 1}], "request_id": uuid.uuid4().hex}).status_code, 404)

    def test_payment_writes_audit(self):
        with tempfile.TemporaryDirectory() as tmp:
            db, client = _client(tmp)
            bill = _make_bill(client, 100)
            client.post(f"/api/bills/{bill}/pay", json={"methods": [{"method": "现金", "amount": 100}], "request_id": uuid.uuid4().hex})
            with connect(db) as conn:
                actions = [r["action"] for r in conn.execute(
                    "select action from audit_logs where entity_type='bill' and entity_id=?", (bill,)).fetchall()]
            self.assertIn("confirm_payment", actions)


class ImportedDiscountRemainingTest(unittest.TestCase):
    """外部导入打折单(DiscountFee 带千分位逗号)的收费弹框 remaining 必须扣折扣,
    与欠费清单 bill_net_arrears_sql 同口径——已结清的打折单不得显示虚欠。"""

    def test_get_bill_remaining_deducts_import_discount(self):
        with tempfile.TemporaryDirectory() as tmp:
            db, client = _client(tmp)
            with connect(db) as conn:
                conn.execute(
                    "insert into bills(bill_id, patient_identity, total_fee, paid_fee, state, "
                    "bill_time, source_json, updated_at) values "
                    "('imported-disc-1', 'p1', 5000, 3600, '0', '2026-07-01 10:00:00', "
                    "'{\"DiscountFee\": \"1,400.00\"}', '2026-07-01 10:00:00')")
                conn.commit()
            d = client.get("/api/bills/imported-disc-1").json()
            self.assertEqual(d["remaining"], 0, f"5000-1400折扣-3600实收=0,不得虚欠: {d}")
            self.assertEqual(d["discount"], 1400)

    def test_get_bill_local_bill_unchanged(self):
        # 本地自建单(无 DiscountFee)行为不变:remaining=total-paid
        with tempfile.TemporaryDirectory() as tmp:
            db, client = _client(tmp)
            bill = _make_bill(client, 1000)
            client.post(f"/api/bills/{bill}/pay", json={"methods": [{"method": "现金", "amount": 400}], "request_id": uuid.uuid4().hex})
            d = client.get(f"/api/bills/{bill}").json()
            self.assertEqual(d["remaining"], 600)


class PaymentIdempotencyTest(unittest.TestCase):
    """收退款幂等——同号同载荷重放返回首次结果,同号异载荷 409,缺号 400。"""

    def _pay_body(self, amount, rid, method="现金"):
        return {"methods": [{"method": method, "amount": amount}], "request_id": rid}

    def test_same_request_replayed_lands_single_payment(self):
        # 同号同载荷 POST 两次:只落一笔流水,paid_fee 只加一次,响应完全一致(含 payment_id)
        with tempfile.TemporaryDirectory() as tmp:
            db, client = _client(tmp)
            bill = _make_bill(client, 100)
            rid = uuid.uuid4().hex
            r1 = client.post(f"/api/bills/{bill}/pay", json=self._pay_body(40, rid))
            r2 = client.post(f"/api/bills/{bill}/pay", json=self._pay_body(40, rid))
            self.assertEqual(r1.status_code, 200, r1.text)
            self.assertEqual(r2.status_code, 200, r2.text)
            self.assertEqual(r1.json(), r2.json())
            with connect(db) as conn:
                cnt, paid = conn.execute(
                    "select count(*), coalesce(sum(amount),0) from payments where bill_id=?", (bill,)).fetchone()
                pf = conn.execute("select paid_fee from bills where bill_id=?", (bill,)).fetchone()[0]
            self.assertEqual(cnt, 1)
            self.assertEqual(paid, 40)
            self.assertEqual(pf, 40)

    def test_same_request_id_different_payload_409(self):
        with tempfile.TemporaryDirectory() as tmp:
            db, client = _client(tmp)
            bill = _make_bill(client, 100)
            rid = uuid.uuid4().hex
            self.assertEqual(client.post(f"/api/bills/{bill}/pay", json=self._pay_body(40, rid)).status_code, 200)
            r2 = client.post(f"/api/bills/{bill}/pay", json=self._pay_body(50, rid))
            self.assertEqual(r2.status_code, 409)
            with connect(db) as conn:
                pf = conn.execute("select paid_fee from bills where bill_id=?", (bill,)).fetchone()[0]
            self.assertEqual(pf, 40)   # 异载荷被拒,一分没多收

    def test_missing_request_id_400(self):
        with tempfile.TemporaryDirectory() as tmp:
            db, client = _client(tmp)
            bill = _make_bill(client, 100)
            self.assertEqual(client.post(f"/api/bills/{bill}/pay",
                             json={"methods": [{"method": "现金", "amount": 40}]}).status_code, 400)
            self.assertEqual(client.post(f"/api/bills/{bill}/pay",
                             json={"methods": [{"method": "现金", "amount": 40}], "request_id": "  "}).status_code, 400)

    def test_failed_validation_does_not_burn_request_id(self):
        # 校验失败(超额)不记请求号:改对金额后同号重试应成功
        with tempfile.TemporaryDirectory() as tmp:
            db, client = _client(tmp)
            bill = _make_bill(client, 100)
            rid = uuid.uuid4().hex
            self.assertEqual(client.post(f"/api/bills/{bill}/pay", json=self._pay_body(999, rid)).status_code, 400)
            self.assertEqual(client.post(f"/api/bills/{bill}/pay", json=self._pay_body(100, rid)).status_code, 200)

    def test_refund_replay_lands_single_refund(self):
        # 退费同套幂等:同号重放只退一笔;缺号 400
        with tempfile.TemporaryDirectory() as tmp:
            db, client = _client(tmp)
            from local_app import auth as _auth
            with connect(db) as conn:
                _auth.create_user(conn, "admin", "管理员", "admin123", role="admin")
                conn.commit()
            client.post("/api/auth/login", json={"username": "admin", "password": "admin123"})
            bill = _make_bill(client, 100)
            client.post(f"/api/bills/{bill}/pay", json=self._pay_body(100, uuid.uuid4().hex))
            self.assertEqual(client.post(f"/api/bills/{bill}/refund",
                             json={"refund_reason": "x"}).status_code, 400)   # 缺号
            rid = uuid.uuid4().hex
            body = {"refund_reason": "重试退", "refund_amount": 30, "request_id": rid}
            r1 = client.post(f"/api/bills/{bill}/refund", json=body)
            r2 = client.post(f"/api/bills/{bill}/refund", json=body)
            self.assertEqual(r1.status_code, 200, r1.text)
            self.assertEqual(r1.json(), r2.json())
            with connect(db) as conn:
                cnt, s = conn.execute("select count(*), coalesce(sum(amount),0) from payments "
                                      "where bill_id=? and payment_record_type='refund'", (bill,)).fetchone()
            self.assertEqual(cnt, 1)
            self.assertEqual(s, -30)


class EmptyPayMethodTest(unittest.TestCase):
    """空付款方式必拒——空方式落库进不了任何现金/微信桶,账目对不上。"""

    def test_empty_method_400(self):
        with tempfile.TemporaryDirectory() as tmp:
            db, client = _client(tmp)
            bill = _make_bill(client, 100)
            for bad in ("", "  ", None):
                r = client.post(f"/api/bills/{bill}/pay", json={
                    "methods": [{"method": bad, "amount": 100}], "request_id": uuid.uuid4().hex})
                self.assertEqual(r.status_code, 400, f"method={bad!r} 应 400")
                self.assertIn("付款方式必填", r.json()["detail"])

    def test_mixed_rows_one_empty_rejects_all(self):
        # 拆分两行一行空:整单拒,一分不落库
        with tempfile.TemporaryDirectory() as tmp:
            db, client = _client(tmp)
            bill = _make_bill(client, 100)
            r = client.post(f"/api/bills/{bill}/pay", json={
                "methods": [{"method": "现金", "amount": 60}, {"method": "", "amount": 40}],
                "request_id": uuid.uuid4().hex})
            self.assertEqual(r.status_code, 400)
            with connect(db) as conn:
                cnt = conn.execute("select count(*) from payments where bill_id=?", (bill,)).fetchone()[0]
                pf = conn.execute("select paid_fee from bills where bill_id=?", (bill,)).fetchone()[0]
            self.assertEqual(cnt, 0)
            self.assertEqual(pf, 0)


class IdempotencyCrossTargetTest(unittest.TestCase):
    """同请求号复用到别的账单/动作不得重放,须 409。"""

    def test_same_request_id_different_bill_409(self):
        with tempfile.TemporaryDirectory() as tmp:
            db, client = _client(tmp)
            bA, bB = _make_bill(client, 100), _make_bill(client, 100)
            rid = uuid.uuid4().hex
            body = {"methods": [{"method": "现金", "amount": 100}], "request_id": rid}
            self.assertEqual(client.post(f"/api/bills/{bA}/pay", json=body).status_code, 200)
            rB = client.post(f"/api/bills/{bB}/pay", json=body)
            self.assertEqual(rB.status_code, 409)   # 不能把 A 的响应重放给 B
            with connect(db) as conn:
                pf = conn.execute("select paid_fee from bills where bill_id=?", (bB,)).fetchone()[0]
            self.assertEqual(pf, 0)   # B 未被误判已收
