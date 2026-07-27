"""端到端集成测试：处置单 → 划价(免费/打折/优惠) → 收费(部分/结清) 全流水线。
覆盖跨端点的状态流转与金额口径一致性，防止集成回归。"""
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


class ClinicalFlowE2ETest(unittest.TestCase):
    def test_record_price_pay_full_chain(self):
        with tempfile.TemporaryDirectory() as tmp:
            db, client = _client(tmp)

            # 1) 新增处置（待划价，不出收费单）
            create = client.post("/api/patients/p1/treatment-orders", json={
                "doctor_name": "王医生", "diagnosis": "牙髓炎",
                "items": [
                    {"item_name": "根管治疗", "tooth": "16", "unit_price": 800, "quantity": 1},
                    {"item_name": "树脂充填", "tooth": "16", "unit_price": 200, "quantity": 2},
                ],
            })
            self.assertEqual(create.status_code, 200)
            oid = create.json()["order_id"]
            orders = client.get("/api/patients/p1/treatment-orders").json()["orders"]
            self.assertEqual(orders[0]["effective_status"], "recorded")
            items = orders[0]["items"]
            root = [i for i in items if i["item_name"] == "根管治疗"][0]
            resin = [i for i in items if i["item_name"] == "树脂充填"][0]

            # 2) 划价：根管免费(0)，树脂8折(200*2*0.8=320)，整单优惠20 → 300
            priced = client.post(f"/api/treatment-orders/{oid}/price", json={
                "discount": 20,
                "items": [
                    {"item_id": root["item_id"], "free": True},
                    {"item_id": resin["item_id"], "amount": 320},   # 200*2打8折

                ],
            })
            self.assertEqual(priced.status_code, 200)
            self.assertEqual(priced.json()["total_fee"], 300)
            bill_id = priced.json()["bill_id"]
            o = client.get("/api/patients/p1/treatment-orders").json()["orders"][0]
            self.assertEqual(o["effective_status"], "priced")
            self.assertEqual(o["bill_remaining"], 300)

            # 3) 收费：先收100(部分) → partial，再收200 → 结清 paid
            r1 = client.post(f"/api/bills/{bill_id}/pay", json={"methods": [{"method": "现金", "amount": 100}], "request_id": uuid.uuid4().hex})
            self.assertEqual(r1.json()["state"], "partial")
            self.assertEqual(r1.json()["remaining"], 200)
            r2 = client.post(f"/api/bills/{bill_id}/pay", json={"methods": [{"method": "微信", "amount": 200}], "request_id": uuid.uuid4().hex})
            self.assertEqual(r2.json()["state"], "paid")
            self.assertEqual(r2.json()["remaining"], 0)

            # 4) 处置单派生状态变 已收费；收款明细2条共300；收费方式落库
            o2 = client.get("/api/patients/p1/treatment-orders").json()["orders"][0]
            self.assertEqual(o2["effective_status"], "paid")
            with connect(db) as conn:
                pay = conn.execute("select count(*) c, coalesce(sum(amount),0) s from payments where bill_id=?", (bill_id,)).fetchone()
                methods = " ".join(r["source_json"] for r in conn.execute("select source_json from payments where bill_id=?", (bill_id,)).fetchall())
            self.assertEqual(pay["c"], 2)
            self.assertEqual(pay["s"], 300)
            self.assertIn("现金", methods)
            self.assertIn("微信", methods)

    def test_all_free_order_settles_without_payment(self):
        with tempfile.TemporaryDirectory() as tmp:
            db, client = _client(tmp)
            oid = client.post("/api/patients/p1/treatment-orders", json={
                "items": [{"item_name": "复查", "unit_price": 0, "quantity": 1}]}).json()["order_id"]
            priced = client.post(f"/api/treatment-orders/{oid}/price", json={})
            self.assertEqual(priced.json()["total_fee"], 0)
            o = client.get("/api/patients/p1/treatment-orders").json()["orders"][0]
            self.assertEqual(o["effective_status"], "paid")  # 0元单直接结清，无需收款

    def test_void_unpaid_then_blocked_after_paid(self):
        with tempfile.TemporaryDirectory() as tmp:
            db, client = _client(tmp)
            # 未划价可撤
            o1 = client.post("/api/patients/p1/treatment-orders", json={"items": [{"item_name": "A", "unit_price": 100, "quantity": 1}]}).json()["order_id"]
            self.assertEqual(client.post(f"/api/treatment-orders/{o1}/void", json={"reason": "录错"}).status_code, 200)
            # 已收费不可撤
            o2 = client.post("/api/patients/p1/treatment-orders", json={"items": [{"item_name": "B", "unit_price": 100, "quantity": 1}]}).json()["order_id"]
            b = client.post(f"/api/treatment-orders/{o2}/price", json={}).json()["bill_id"]
            client.post(f"/api/bills/{b}/pay", json={"methods": [{"method": "现金", "amount": 100}], "request_id": uuid.uuid4().hex})
            self.assertEqual(client.post(f"/api/treatment-orders/{o2}/void", json={"reason": "试图撤销"}).status_code, 409)
