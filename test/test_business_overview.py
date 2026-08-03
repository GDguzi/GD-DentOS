import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from local_app import auth
from local_app.api import create_app
from local_app.db import connect, init_db


def _setup(tmp):
    db = Path(tmp) / "clinic.sqlite3"
    init_db(db)
    with connect(db) as conn:
        auth.create_user(conn, "admin", "管理员", "admin123", role="admin")
        conn.execute("insert into patients(patient_identity, display_name, updated_at, current_hash) "
                     "values ('P','张三','x','h')")
        # 累计消费口径(#276)：有效收款1000计入;作废原单(CancelMark且正500)排除;退费-200冲减 → 净800
        conn.execute("insert into payments(payment_id, patient_identity, amount, pay_time, source_json) "
                     "values ('y1','P',1000,'2026-01-01 10:00','{}')")
        conn.execute("insert into payments(payment_id, patient_identity, amount, pay_time, source_json) "
                     "values ('y2','P',500,'2026-01-02 10:00','{\"CancelMark\":\"1\"}')")
        conn.execute("insert into payments(payment_id, patient_identity, amount, pay_time, source_json) "
                     "values ('y3','P',-200,'2026-01-03 10:00','{}')")
        conn.commit()
    return db


def _login(c):
    assert c.post("/api/auth/login", json={"username": "admin", "password": "admin123"}).status_code == 200


class BusinessOverviewTest(unittest.TestCase):
    def test_total_spend_excludes_voided_keeps_refund(self):
        with tempfile.TemporaryDirectory() as tmp:
            c = TestClient(create_app(_setup(tmp)))
            _login(c)
            biz = c.get("/api/patients/P/summary").json()["business"]
            self.assertEqual(biz["total_spend"], 800.0)   # 1000 - 200，作废500排除


if __name__ == "__main__":
    unittest.main()
