"""过去的日子查"今日工作"：预约表可能已改期/取消(号变了),但人已经看过病/交过钱——
真到店要按病历/账单算。unlinked_visits 补出"预约表没体现的到店"，只对过去的日期算，
不影响今天队列(避免动到今天队列按钮依赖的预约行为)。合成数据，不碰真实库。"""
import datetime as dt
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from local_app.api import create_app
from local_app.db import connect, init_db


class TodayWorkUnlinkedVisitsTest(unittest.TestCase):
    def _seed(self, tmp):
        db = Path(tmp) / "clinic.sqlite3"
        init_db(db)
        past = "2026-06-28"
        with connect(db) as conn:
            for i in range(1, 4):
                conn.execute(
                    "insert into patients(patient_identity, display_name, current_hash) "
                    "values (?,?,?)", (f"p{i}", f"患者{i}", f"h{i}"))
            # p1: 有预约(covered) + 有病历 → 不该出现在 unlinked_visits
            conn.execute(
                "insert into appointments(appointment_id, patient_identity, start_time, status, updated_at) "
                "values ('a1','p1',?,?,'x')", (f"{past} 09:00:00", "已确认"))
            conn.execute(
                "insert into medical_records(record_id, patient_identity, visit_time, current_hash) "
                "values ('r1','p1',?,?)", (f"{past} 09:10:00", "rh1"))
            # p2: 没预约,但有病历(那天真来看过病) → 应该出现在 unlinked_visits
            conn.execute(
                "insert into medical_records(record_id, patient_identity, visit_time, current_hash) "
                "values ('r2','p2',?,?)", (f"{past} 10:00:00", "rh2"))
            # p3: 没预约,但有账单(那天真交过钱) → 应该出现在 unlinked_visits
            conn.execute(
                "insert into bills(bill_id, patient_identity, bill_time) values ('b1','p3',?)",
                (f"{past} 11:00:00",))
            conn.commit()
        return db, past

    def test_past_date_surfaces_visits_missing_from_appointment_table(self):
        with tempfile.TemporaryDirectory() as tmp:
            db, past = self._seed(tmp)
            client = TestClient(create_app(db))
            resp = client.get("/api/today-work", params={"date": past})
            self.assertEqual(resp.status_code, 200)
            body = resp.json()
            unlinked_ids = {r["patient_identity"] for r in body["unlinked_visits"]}
            self.assertEqual(unlinked_ids, {"p2", "p3"})   # p1 已被预约覆盖,不重复列出

    def test_today_does_not_compute_unlinked_visits(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "clinic.sqlite3"
            init_db(db)
            today = dt.date.today().isoformat()
            with connect(db) as conn:
                conn.execute(
                    "insert into patients(patient_identity, display_name, current_hash) "
                    "values ('p1','患者1','h1')")
                conn.execute(
                    "insert into medical_records(record_id, patient_identity, visit_time, current_hash) "
                    "values ('r1','p1',?,?)", (f"{today} 09:00:00", "rh1"))
                conn.commit()
            client = TestClient(create_app(db))
            resp = client.get("/api/today-work", params={"date": today})
            self.assertEqual(resp.json()["unlinked_visits"], [])   # 今天不算,避免动到当天队列口径


if __name__ == "__main__":
    unittest.main()
