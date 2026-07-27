"""本地划价生成的账单应有账单编号(bill_no)——此前写空串，导致收费单"没有账单编号"。
编号按 外部系统 习惯 年月日(YYMMDD)+当日4位流水。"""
import re
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from local_app.api import create_app
from local_app.db import connect, init_db


def _client(tmp):
    db = Path(tmp) / "clinic.sqlite3"
    init_db(db)
    return db, TestClient(create_app(db))


def _patient(c, name, phone):
    return c.post("/api/patients", json={"display_name": name, "phone": phone}).json()["patient_identity"]


def _price_bill(c, pid, total):
    oid = c.post(f"/api/patients/{pid}/treatment-orders", json={
        "items": [{"item_name": "处置", "unit_price": total, "quantity": 1}]}).json()["order_id"]
    return c.post(f"/api/treatment-orders/{oid}/price", json={}).json()["bill_id"]


class BillNoTest(unittest.TestCase):
    def test_local_bill_has_number(self):
        with tempfile.TemporaryDirectory() as tmp:
            db, c = _client(tmp)
            pid = _patient(c, "编号测试", "13600000001")
            bid = _price_bill(c, pid, 500)
            with connect(db) as conn:
                no = conn.execute("select bill_no from bills where bill_id=?", (bid,)).fetchone()[0]
            self.assertTrue(no, "本地账单 bill_no 不能为空")
            self.assertRegex(no, r"^\d{10}$")          # YYMMDD + 4位流水

    def test_daily_sequence_increments(self):
        with tempfile.TemporaryDirectory() as tmp:
            db, c = _client(tmp)
            pid = _patient(c, "甲", "13600000002")
            b1 = _price_bill(c, pid, 100)
            b2 = _price_bill(c, pid, 200)
            with connect(db) as conn:
                n1 = conn.execute("select bill_no from bills where bill_id=?", (b1,)).fetchone()[0]
                n2 = conn.execute("select bill_no from bills where bill_id=?", (b2,)).fetchone()[0]
            self.assertNotEqual(n1, n2)
            self.assertEqual(int(n2) - int(n1), 1)     # 当日流水 +1


    def test_no_collision_with_gap(self):
        # 当日已有非连续编号(如缺号)时，新单取 max+1 不撞号
        with tempfile.TemporaryDirectory() as tmp:
            db, c = _client(tmp)
            pid = _patient(c, "缺号测试", "13600000009")
            b1 = _price_bill(c, pid, 100)
            with connect(db) as conn:
                no1 = conn.execute("select bill_no from bills where bill_id=?", (b1,)).fetchone()[0]
                # 人为制造一个更大的当日编号(模拟撤单/历史留洞)
                bigger = no1[:6] + f"{int(no1[6:]) + 2:04d}"
                conn.execute("insert into bills(bill_id, patient_identity, bill_no, total_fee, paid_fee, state, updated_at) "
                             "values ('local-bill-gap', ?, ?, 0, 0, 'pending', '2026-06-19 00:00:00')", (pid, bigger))
                conn.commit()
            b3 = _price_bill(c, pid, 300)
            with connect(db) as conn:
                no3 = conn.execute("select bill_no from bills where bill_id=?", (b3,)).fetchone()[0]
            self.assertEqual(int(no3), int(bigger) + 1)   # max+1，不与已有撞号


if __name__ == "__main__":
    unittest.main()
