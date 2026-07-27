"""Phase D 处置大类：收银查询同源数据按 fee_type 分组,合计须与收银查询的类别透视一致。"""
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
    with connect(db) as c:
        auth.create_user(c, "admin", "管理员", "admin123", role="admin")  # P1-13：CSV 导出仅 admin
        c.execute("insert into patients(patient_identity, display_name, updated_at, current_hash) "
                  "values ('p1','张三','2026-06-07','h1')")
        c.commit()
    client = TestClient(create_app(db))
    client.post("/api/auth/login", json={"username": "admin", "password": "admin123"})
    return db, client


def _bill(client, items):
    oid = client.post("/api/patients/p1/treatment-orders", json={"doctor_name": "测试医生B", "items": items}).json()["order_id"]
    return client.post(f"/api/treatment-orders/{oid}/price", json={}).json()["bill_id"]


class HandleCategoryTest(unittest.TestCase):
    def test_category_totals_match_cashier(self):
        with tempfile.TemporaryDirectory() as tmp:
            db, client = _client(tmp)
            b = _bill(client, [{"item_name": "种植体", "unit_price": 600, "quantity": 1, "fee_type": "种植费"},
                               {"item_name": "拔牙", "unit_price": 400, "quantity": 1, "fee_type": "拔牙费"}])
            client.post(f"/api/bills/{b}/pay", json={"methods": [{"method": "现金", "amount": 1000}], "request_id": uuid.uuid4().hex})
            q = "date_from=2026-01-01&date_to=2030-01-01"
            hc = client.get(f"/api/reports/handle-category?{q}").json()
            cashier = client.get(f"/api/reports/cashier-query?{q}").json()
            cats = {c["fee_type"]: c["amount"] for c in hc["categories"]}
            self.assertEqual(cats["种植费"], 600)
            self.assertEqual(cats["拔牙费"], 400)
            self.assertEqual(hc["total_amount"], 1000)
            self.assertEqual(cats, cashier["by_category"])     # 与收银查询类别透视一致

    def test_csv_export(self):
        with tempfile.TemporaryDirectory() as tmp:
            db, client = _client(tmp)
            b = _bill(client, [{"item_name": "x", "unit_price": 300, "quantity": 1, "fee_type": "补牙费"}])
            client.post(f"/api/bills/{b}/pay", json={"methods": [{"method": "微信", "amount": 300}], "request_id": uuid.uuid4().hex})
            r = client.get("/api/reports/handle-category.csv?date_from=2026-01-01&date_to=2030-01-01")
            self.assertEqual(r.status_code, 200)
            text = r.content.decode("utf-8-sig")
            self.assertIn("处置类别", text.splitlines()[0])
            self.assertIn("补牙费", text)
            self.assertIn("总合计", text)


if __name__ == "__main__":
    unittest.main()
