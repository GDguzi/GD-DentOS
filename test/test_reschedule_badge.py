import json
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from local_app.api import create_app
from local_app.db import connect, init_db


class RescheduleBadgeTest(unittest.TestCase):
    def _setup(self, tmp):
        db = Path(tmp) / "clinic.sqlite3"
        init_db(db)
        with connect(db) as conn:
            conn.execute("insert into patients(patient_identity, display_name, updated_at, current_hash) "
                         "values ('p1','张三','x','h1')")
            # 模拟 外部导入的预约：source_json 带原约时间 ScheduleStartTime
            conn.execute(
                "insert into appointments(appointment_id, patient_identity, start_time, status, source_json, updated_at) "
                "values ('a1','p1','2026-06-18 16:30','0', ?, 'x')",
                (json.dumps({"ScheduleStartTime": "2026-06-18 16:30"}),),
            )
            conn.commit()
        return db, TestClient(create_app(db))

    def _appt(self, client):
        return next(a for a in client.get("/api/patients/p1").json()["appointments"] if a["appointment_id"] == "a1")

    def test_unchanged_not_rescheduled(self):
        # 没本地改时间 → original_start_time 与 start_time 一致(前端不显示改约)
        with tempfile.TemporaryDirectory() as tmp:
            _, client = self._setup(tmp)
            a = self._appt(client)
            self.assertEqual(a["original_start_time"][:16], a["start_time"][:16])

    def test_local_reschedule_exposes_original(self):
        # 本地改预约时间 → start_time 变,original_start_time 仍是 外部系统 原约(前端据此标改约)
        with tempfile.TemporaryDirectory() as tmp:
            _, client = self._setup(tmp)
            r = client.put("/api/appointments/a1", json={"start_time": "2026-06-18 10:00"})
            self.assertEqual(r.status_code, 200)
            a = self._appt(client)
            self.assertEqual(a["start_time"][:16], "2026-06-18 10:00")
            self.assertEqual(a["original_start_time"][:16], "2026-06-18 16:30")  # 原约保留
            self.assertNotEqual(a["start_time"][:16], a["original_start_time"][:16])  # =改约


if __name__ == "__main__":
    unittest.main()
