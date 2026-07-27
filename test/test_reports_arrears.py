"""P1-4 独立欠费报表 /api/reports/arrears：全量未结单(非当日限制)，区间/最低欠费额/患者关键字筛选，按欠费倒序。
口径：arrears=total_fee-paid_fee>0；排除 voided/refunded。"""
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
        conn.commit()
    client = TestClient(create_app(db))
    client.post("/api/auth/login", json={"username": "admin", "password": "admin123"})
    return db, client


def _patient(c, name, phone):
    return c.post("/api/patients", json={"display_name": name, "phone": phone}).json()["patient_identity"]


def _bill(c, pid, total):
    oid = c.post(f"/api/patients/{pid}/treatment-orders", json={
        "items": [{"item_name": "处置", "unit_price": total, "quantity": 1}]}).json()["order_id"]
    return c.post(f"/api/treatment-orders/{oid}/price", json={}).json()["bill_id"]


class ArrearsReportTest(unittest.TestCase):
    def test_lists_unpaid_excludes_paid_sorted_desc(self):
        with tempfile.TemporaryDirectory() as tmp:
            db, c = _client(tmp)
            pid = _patient(c, "欠费王", "13900000001")
            _bill(c, pid, 1000)                       # 全欠 1000
            b2 = _bill(c, pid, 500)
            c.post(f"/api/bills/{b2}/pay", json={"methods": [{"method": "现金", "amount": 500}], "request_id": uuid.uuid4().hex})   # 结清 → 不应出现
            b3 = _bill(c, pid, 800)
            c.post(f"/api/bills/{b3}/pay", json={"methods": [{"method": "现金", "amount": 300}], "request_id": uuid.uuid4().hex})   # 欠 500
            r = c.get("/api/reports/arrears")
            self.assertEqual(r.status_code, 200, r.text)
            data = r.json()
            arrs = [row["arrears"] for row in data["rows"]]
            self.assertEqual(arrs, [1000, 500])       # 倒序，排除已结清
            self.assertEqual(data["total_arrears"], 1500)
            self.assertEqual(data["count"], 2)

    def test_excludes_refunded(self):
        with tempfile.TemporaryDirectory() as tmp:
            db, c = _client(tmp)
            pid = _patient(c, "退费李", "13900000002")
            b = _bill(c, pid, 600)
            c.post(f"/api/bills/{b}/pay", json={"methods": [{"method": "现金", "amount": 600}], "request_id": uuid.uuid4().hex})
            c.post(f"/api/bills/{b}/refund", json={"refund_reason": "取消", "request_id": uuid.uuid4().hex})  # 退费后 paid=0,total=600
            r = c.get("/api/reports/arrears").json()
            # 退费单不算欠费（state=refunded 排除），否则会假性显示 600 欠款
            self.assertTrue(all(row["bill_id"] != b for row in r["rows"]))

    def test_min_arrears_filter(self):
        with tempfile.TemporaryDirectory() as tmp:
            db, c = _client(tmp)
            pid = _patient(c, "甲", "13900000003")
            _bill(c, pid, 100)
            _bill(c, pid, 2000)
            r = c.get("/api/reports/arrears", params={"min_arrears": 500}).json()
            self.assertEqual(r["count"], 1)
            self.assertEqual(r["rows"][0]["arrears"], 2000)

    def test_keyword_filter_by_patient(self):
        with tempfile.TemporaryDirectory() as tmp:
            db, c = _client(tmp)
            p1 = _patient(c, "张三", "13900000004")
            p2 = _patient(c, "李四", "13900000005")
            _bill(c, p1, 300)
            _bill(c, p2, 400)
            r = c.get("/api/reports/arrears", params={"q": "张三"}).json()
            self.assertEqual(r["count"], 1)
            self.assertEqual(r["rows"][0]["display_name"], "张三")


if __name__ == "__main__":
    unittest.main()
