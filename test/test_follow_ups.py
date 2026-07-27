import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from local_app import auth
from local_app.api import create_app
from local_app.db import connect, init_db

DAY = "2026-06-25"


def _setup(tmp):
    db = Path(tmp) / "clinic.sqlite3"
    init_db(db)
    with connect(db) as conn:
        auth.create_user(conn, "admin", "管理员", "admin123", role="admin")
        # A:累计1.2万(高)+今天到期回访;C:累计1千(普通)+今天到期回访
        # B:只待缴费无回访(不该出现);D:回访在过去(不该出现)
        for pid, name, phone, doc in [("A", "张三", "13800000001", "王医生"),
                                      ("C", "王五", "13800000003", ""),
                                      ("B", "李四", "13800000002", ""),
                                      ("D", "赵六", "13800000004", "")]:
            conn.execute("insert into patients(patient_identity, display_name, phone, responsible_doctor, "
                         "updated_at, current_hash) values (?,?,?,?,?,?)",
                         (pid, name, phone, doc, "x", "h" + pid))
        conn.execute("insert into payments(payment_id, patient_identity, amount, state) values ('p1','A',12000,'0')")
        conn.execute("insert into payments(payment_id, patient_identity, amount, state) values ('p2','C',1000,'0')")
        # 今天到期回访(未回访)
        conn.execute("insert into return_visits(return_visit_id, patient_identity, due_time, item_name, "
                     "actual_date, is_deleted, return_doctor) values ('r1','A',?,'复查\\n内容:略','',0,'王医生')", (DAY + " 09:00",))
        conn.execute("insert into return_visits(return_visit_id, patient_identity, due_time, item_name, "
                     "actual_date, is_deleted) values ('r2','C',?,'戴牙复查','',0)", (DAY + " 10:00",))
        # 过去到期(积压,不算今日)
        conn.execute("insert into return_visits(return_visit_id, patient_identity, due_time, item_name, "
                     "actual_date, is_deleted) values ('r3','D','2026-06-01 09:00','拔牙后','',0)")
        # B 只有待缴费账单,无回访 → 不进今日待跟进
        conn.execute("insert into bills(bill_id, patient_identity, total_fee, paid_fee, state) values ('b1','B',500,100,'0')")
        conn.commit()
    return db


def _login(c):
    assert c.post("/api/auth/login", json={"username": "admin", "password": "admin123"}).status_code == 200


class FollowUpsTest(unittest.TestCase):
    def test_today_due_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            c = TestClient(create_app(_setup(tmp)))
            _login(c)
            d = c.get(f"/api/today-follow-ups?date={DAY}").json()
            # 只今天到期的 A、C;B(只待缴费)/D(过去回访)都不出现
            self.assertEqual(d["total"], 2)
            self.assertEqual(d["counts"]["high"], 1)
            self.assertEqual(d["counts"]["normal"], 1)
            names = {p["name"] for t in ("high", "mid", "normal") for p in d["groups"][t]}
            self.assertEqual(names, {"张三", "王五"})
            a = d["groups"]["high"][0]
            self.assertEqual(a["name"], "张三")
            self.assertEqual(a["assignee"], "王医生")
            due = a["reasons"][0]
            self.assertEqual(due["type"], "回访到期")
            self.assertIn("复查", due["detail"])     # 取首段短标签
            self.assertNotIn("内容", due["detail"])

    def test_past_due_excluded_and_filters(self):
        with tempfile.TemporaryDirectory() as tmp:
            c = TestClient(create_app(_setup(tmp)))
            _login(c)
            # 切到过去那天 → 只有 D
            d0 = c.get("/api/today-follow-ups?date=2026-06-01").json()
            self.assertEqual(d0["total"], 1)
            self.assertEqual(d0["groups"]["normal"][0]["name"], "赵六")
            # 搜索 张 → 仅 A
            self.assertEqual(c.get(f"/api/today-follow-ups?date={DAY}&q=张").json()["total"], 1)
            # 跟进人 王医生 → 仅 A
            d4 = c.get(f"/api/today-follow-ups?date={DAY}&assignee=王医生").json()
            self.assertEqual(d4["total"], 1)
            self.assertEqual(d4["groups"]["high"][0]["name"], "张三")


if __name__ == "__main__":
    unittest.main()
