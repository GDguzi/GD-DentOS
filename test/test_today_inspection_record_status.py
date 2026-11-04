"""Only existing formal records satisfy the daily medical-record check."""
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from local_app import auth
from local_app.api import create_app
from local_app.db import connect, init_db


class TodayInspectionRecordStatusTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.db = root / "test.sqlite3"
        init_db(self.db)
        with connect(self.db) as conn:
            auth.create_user(conn, "test-admin", "Synthetic admin", "test-password", role="admin")
            conn.execute(
                "insert into patients(patient_identity, display_name, updated_at, current_hash) "
                "values ('p1', 'Synthetic patient', '2026-08-04', 'test-hash')"
            )
            conn.execute(
                "insert into treatment_orders(order_id, order_no, patient_identity, order_date, status) "
                "values ('order1', 'TEST-1', 'p1', '2026-08-04 09:00', 'recorded')"
            )
            conn.commit()
        self.client = TestClient(create_app(self.db, images_dir=root / "images", require_login=True))
        self.addCleanup(self.client.close)
        response = self.client.post("/api/auth/login", json={
            "username": "test-admin", "password": "test-password",
        })
        self.assertEqual(response.status_code, 200, response.text)

    def _record(self, record_id, draft_status):
        with connect(self.db) as conn:
            conn.execute(
                "insert into medical_records(record_id, patient_identity, visit_time, "
                "doctor_name, record_type, draft_status, current_hash) values "
                "(?, 'p1', '2026-08-04 09:00', 'Synthetic doctor', '初诊', ?, 'test-hash')",
                (record_id, draft_status),
            )
            conn.commit()

    def _check(self, expected):
        response = self.client.get("/api/today-inspection", params={"date": "2026-08-04"})
        self.assertEqual(response.status_code, 200, response.text)
        orders = response.json()["orders"]
        self.assertEqual(len(orders), 1)
        checks = [check for check in orders[0]["checks"] if check["check"] == "病历"]
        self.assertEqual(len(checks), 1)
        self.assertEqual(checks[0]["status"], expected)

    def test_ai_draft_alone_does_not_satisfy_record_check(self):
        self._record("draft", "ai_draft")
        self._check("warn")

    def test_discarded_draft_alone_does_not_satisfy_record_check(self):
        self._record("discarded", "discarded")
        self._check("warn")

    def test_formal_record_satisfies_record_check(self):
        self._record("formal", "")
        self._check("ok")

    def test_formal_record_still_counts_alongside_drafts(self):
        self._record("formal", "")
        self._record("draft", "ai_draft")
        self._record("discarded", "discarded")
        self._check("ok")
