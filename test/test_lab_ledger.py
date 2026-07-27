"""P1-11 在制技工台账 + 超期提醒：跨患者列在制(status=sent)技工单，按送出日排序，超期标红。"""
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


def _lab(c, pid, project, sent_date, return_date=""):
    return c.post(f"/api/patients/{pid}/lab-orders", json={
        "project_type": project, "sent_date": sent_date, "return_date": return_date})


class LabLedgerTest(unittest.TestCase):
    def test_cross_patient_in_progress_only_excludes_returned(self):
        with tempfile.TemporaryDirectory() as tmp:
            db, c = _client(tmp)
            p1 = _patient(c, "甲患者", "13700000001")
            p2 = _patient(c, "乙患者", "13700000002")
            _lab(c, p1, "全瓷冠", "2020-01-01")              # 在制·超期
            _lab(c, p2, "活动义齿", "2099-01-01")             # 在制·未超期(未来日期)
            _lab(c, p1, "正畸保持器", "2020-02-01", return_date="2020-02-20")  # 已返回→排除
            r = c.get("/api/lab-orders/in-progress")
            self.assertEqual(r.status_code, 200, r.text)
            data = r.json()
            self.assertEqual(data["count"], 2)                # 仅在制，排除已返回
            names = {row["patient_name"] for row in data["rows"]}
            self.assertEqual(names, {"甲患者", "乙患者"})       # 跨患者 + 患者名 join

    def test_overdue_flag_and_sort_oldest_first(self):
        with tempfile.TemporaryDirectory() as tmp:
            db, c = _client(tmp)
            p1 = _patient(c, "甲", "13700000003")
            _lab(c, p1, "全瓷冠", "2020-01-01")               # 远超期
            _lab(c, p1, "嵌体", "2099-01-01")                 # 未超期
            data = c.get("/api/lab-orders/in-progress", params={"overdue_days": 7}).json()
            self.assertEqual(data["rows"][0]["sent_date"], "2020-01-01")  # 最早(最该催)在前
            self.assertTrue(data["rows"][0]["overdue"])
            self.assertFalse(data["rows"][1]["overdue"])
            self.assertEqual(data["overdue_count"], 1)


if __name__ == "__main__":
    unittest.main()
