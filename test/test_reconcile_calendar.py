"""对账日历：某月每天 收/支/合 + 月合计。用户拍板 支=当日退费。"""
import json
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from local_app import auth
from local_app.api import create_app
from local_app.db import connect, init_db


def _client(tmp):
    db = Path(tmp) / "clinic.sqlite3"
    init_db(db)
    with connect(db) as c:
        auth.create_user(c, "admin", "管理员", "admin123", role="admin")
        c.execute("insert into patients(patient_identity, display_name, updated_at, current_hash) "
                  "values ('p1','张三','2026-07-01','h1')")
        c.commit()
    client = TestClient(create_app(db))
    client.post("/api/auth/login", json={"username": "admin", "password": "admin123"})
    return db, client


def _pay(c, payid, day, amount, cancel_mark=None):
    sj = json.dumps({"CancelMark": cancel_mark} if cancel_mark else {})
    c.execute("insert into payments(payment_id, patient_identity, bill_id, pay_time, amount, state, "
              "pay_type, source_json, updated_at) values (?,?,?,?,?,?,?,?,?)",
              (payid, "p1", "b1", day + " 10:00:00", amount, "paid", "现金", sj, day))


class ReconcileCalendarTest(unittest.TestCase):
    def test_income_expense_net(self):
        with tempfile.TemporaryDirectory() as tmp:
            db, client = _client(tmp)
            with connect(db) as c:
                _pay(c, "p1", "2026-07-02", 5000)       # 收
                _pay(c, "p2", "2026-07-02", 1000)       # 收(同日累加)
                _pay(c, "p3", "2026-07-05", -800)       # 退费→支
                _pay(c, "p4", "2026-07-05", 200)        # 当日也有收
                _pay(c, "pc", "2026-07-05", 999, cancel_mark="选错")  # 作废原单排除
                c.commit()
            d = client.get("/api/store-stats/reconcile-calendar?month=2026-07").json()
            self.assertEqual(d["days"]["2026-07-02"]["income"], 6000.0)
            self.assertEqual(d["days"]["2026-07-02"]["expense"], 0.0)
            self.assertEqual(d["days"]["2026-07-02"]["net"], 6000.0)
            self.assertEqual(d["days"]["2026-07-05"]["income"], 200.0)
            self.assertEqual(d["days"]["2026-07-05"]["expense"], 800.0)
            self.assertEqual(d["days"]["2026-07-05"]["net"], -600.0)
            self.assertEqual(d["total_income"], 6200.0)
            self.assertEqual(d["total_expense"], 800.0)
            self.assertEqual(d["total_net"], 5400.0)

    def test_month_boundary_and_meta(self):
        with tempfile.TemporaryDirectory() as tmp:
            db, client = _client(tmp)
            with connect(db) as c:
                _pay(c, "pin", "2026-07-15", 300)
                _pay(c, "pout", "2026-08-01", 500)      # 8月不计入7月
                c.commit()
            d = client.get("/api/store-stats/reconcile-calendar?month=2026-07").json()
            self.assertEqual(d["year"], 2026)
            self.assertEqual(d["month_num"], 7)
            self.assertEqual(d["total_income"], 300.0)
            self.assertNotIn("2026-08-01", d["days"])

    def test_bad_month(self):
        with tempfile.TemporaryDirectory() as tmp:
            db, client = _client(tmp)
            self.assertEqual(client.get("/api/store-stats/reconcile-calendar?month=2026-13").status_code, 400)
            self.assertEqual(client.get("/api/store-stats/reconcile-calendar?month=badmonth").status_code, 400)


if __name__ == "__main__":
    unittest.main()
