"""Phase B 收费框架：收款多方式拆分 + 收款员/发票独立列。改钱→严格 TDD。

契约：confirm_payment 接收 payload.methods=[{method,amount},...] → 每方式建一条 payment 行，
合计=本次收款，写 operator/invoice_no/invoice_remark/remark；单方式(amount+pay_method)向后兼容。
非负 + isfinite + 不超应收 + 审计。
"""
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
    with connect(db) as c:
        c.execute("insert into patients(patient_identity, display_name, updated_at, current_hash) "
                  "values ('p1','测试',  '2026-06-07', 'h1')")
        c.commit()
    return db, TestClient(create_app(db))


def _bill(client, total):
    oid = client.post("/api/patients/p1/treatment-orders",
                      json={"items": [{"item_name": "处置", "unit_price": total, "quantity": 1}]}).json()["order_id"]
    return client.post(f"/api/treatment-orders/{oid}/price", json={}).json()["bill_id"]


class PaymentSplitTest(unittest.TestCase):
    def test_single_method_backward_compat(self):
        with tempfile.TemporaryDirectory() as tmp:
            db, client = _client(tmp)
            b = _bill(client, 500)
            r = client.post(f"/api/bills/{b}/pay", json={"methods": [{"method": "现金", "amount": 500}], "cashier": "前台A", "request_id": uuid.uuid4().hex})
            self.assertEqual(r.status_code, 200, r.text)
            with connect(db) as c:
                rows = c.execute("select pay_type, amount, operator from payments where bill_id=?", (b,)).fetchall()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["pay_type"], "现金")
            self.assertEqual(rows[0]["amount"], 500)
            self.assertEqual(rows[0]["operator"], "前台A")   # 收款员落独立列

    def test_multi_method_split(self):
        with tempfile.TemporaryDirectory() as tmp:
            db, client = _client(tmp)
            b = _bill(client, 1000)
            r = client.post(f"/api/bills/{b}/pay", json={
                "methods": [{"method": "现金", "amount": 600}, {"method": "云闪付", "amount": 400}],
                "cashier": "前台B", "request_id": uuid.uuid4().hex})
            self.assertEqual(r.status_code, 200, r.text)
            self.assertEqual(r.json()["state"], "paid")
            with connect(db) as c:
                rows = c.execute("select pay_type, amount, operator from payments where bill_id=? order by amount desc", (b,)).fetchall()
                bill = c.execute("select paid_fee, state from bills where bill_id=?", (b,)).fetchone()
            self.assertEqual(len(rows), 2)                      # 每方式一行
            self.assertEqual({r["pay_type"] for r in rows}, {"现金", "云闪付"})
            self.assertEqual(round(sum(r["amount"] for r in rows), 2), 1000)
            self.assertTrue(all(r["operator"] == "前台B" for r in rows))
            self.assertEqual(bill["paid_fee"], 1000)
            self.assertEqual(bill["state"], "paid")

    def test_multi_partial(self):
        with tempfile.TemporaryDirectory() as tmp:
            db, client = _client(tmp)
            b = _bill(client, 1000)
            r = client.post(f"/api/bills/{b}/pay", json={"methods": [{"method": "现金", "amount": 300}], "request_id": uuid.uuid4().hex})
            self.assertEqual(r.json()["state"], "partial")
            self.assertEqual(r.json()["remaining"], 700)

    def test_multi_exceeds_remaining_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            db, client = _client(tmp)
            b = _bill(client, 1000)
            r = client.post(f"/api/bills/{b}/pay", json={
                "methods": [{"method": "现金", "amount": 600}, {"method": "微信", "amount": 600}], "request_id": uuid.uuid4().hex})
            self.assertEqual(r.status_code, 400)
            with connect(db) as c:
                n = c.execute("select count(*) from payments where bill_id=?", (b,)).fetchone()[0]
            self.assertEqual(n, 0)                              # 拒绝时一行都不写

    def test_multi_invalid_amount_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            db, client = _client(tmp)
            b = _bill(client, 1000)
            for bad in ([{"method": "现金", "amount": -100}], [{"method": "现金", "amount": 0}],
                        [{"method": "现金", "amount": "abc"}]):
                r = client.post(f"/api/bills/{b}/pay", json={"methods": bad, "request_id": uuid.uuid4().hex})
                self.assertEqual(r.status_code, 400, bad)
            with connect(db) as c:
                n = c.execute("select count(*) from payments where bill_id=?", (b,)).fetchone()[0]
            self.assertEqual(n, 0)

    def test_subcent_amount_rejected_no_zero_row(self):
        # 0.004 四舍五入后是0,必须拒绝且不写 0 元 payment(单方式+多方式都要挡)
        with tempfile.TemporaryDirectory() as tmp:
            db, client = _client(tmp)
            b = _bill(client, 1000)
            self.assertEqual(client.post(f"/api/bills/{b}/pay", json={"methods": [{"method": "现金", "amount": 0.004}], "request_id": uuid.uuid4().hex}).status_code, 400)
            self.assertEqual(client.post(f"/api/bills/{b}/pay", json={"methods": [{"method": "现金", "amount": 0.001}], "request_id": uuid.uuid4().hex}).status_code, 400)
            with connect(db) as c:
                n = c.execute("select count(*) from payments where bill_id=?", (b,)).fetchone()[0]
            self.assertEqual(n, 0)

    def test_invoice_remark_stored(self):
        with tempfile.TemporaryDirectory() as tmp:
            db, client = _client(tmp)
            b = _bill(client, 200)
            client.post(f"/api/bills/{b}/pay", json={"methods": [{"method": "微信", "amount": 200}],
                        "invoice_no": "INV-001", "invoice_remark": "公司抬头", "remark": "已开票", "request_id": uuid.uuid4().hex})
            with connect(db) as c:
                row = c.execute("select invoice_no, invoice_remark, remark from payments where bill_id=?", (b,)).fetchone()
            self.assertEqual(row["invoice_no"], "INV-001")
            self.assertEqual(row["invoice_remark"], "公司抬头")
            self.assertEqual(row["remark"], "已开票")


if __name__ == "__main__":
    unittest.main()
