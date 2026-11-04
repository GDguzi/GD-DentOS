"""Existing deleted-patient state must hide old file and appointment URLs."""
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from local_app import auth
from local_app.api import create_app
from local_app.db import connect, init_db


class DeletedPatientReadsTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.db = self.root / "test.sqlite3"
        init_db(self.db)
        with connect(self.db) as conn:
            auth.create_user(conn, "test-admin", "Synthetic admin", "test-password", role="admin")
            conn.execute(
                "insert into patients(patient_identity, display_name, phone, updated_at, current_hash) "
                "values ('p1', 'Synthetic patient', '70000000001', '2026-08-04', 'test-hash')"
            )
            conn.commit()
        self.client = TestClient(create_app(
            self.db, images_dir=self.root / "images", require_login=True,
        ))
        self.addCleanup(self.client.close)
        response = self.client.post("/api/auth/login", json={
            "username": "test-admin", "password": "test-password",
        })
        self.assertEqual(response.status_code, 200, response.text)

    def _mark_patient_deleted(self):
        # Simulate existing historical/merged patient state using the original schema.
        with connect(self.db) as conn:
            conn.execute("update patients set is_deleted=1 where patient_identity='p1'")
            conn.commit()

    def test_old_image_url_returns_404_after_patient_is_marked_deleted(self):
        payload = b"\x89PNG\r\n\x1a\nsynthetic-image"
        upload = self.client.post("/api/patients/p1/images", data={"slot": "本地上传"},
                                  files={"file": ("test.png", payload, "image/png")})
        self.assertEqual(upload.status_code, 200, upload.text)
        url = f"/api/patients/p1/images/{upload.json()['image_id']}/file"
        before = self.client.get(url)
        self.assertEqual(before.status_code, 200, before.text)
        self.assertEqual(before.content, payload)
        self._mark_patient_deleted()
        self.assertEqual(self.client.get(url).status_code, 404)

    def test_old_avatar_url_returns_404_after_patient_is_marked_deleted(self):
        payload = b"\x89PNG\r\n\x1a\nsynthetic-avatar"
        upload = self.client.post("/api/patients/p1/avatar",
                                  files={"file": ("test.png", payload, "image/png")})
        self.assertEqual(upload.status_code, 200, upload.text)
        before = self.client.get("/api/patients/p1/avatar")
        self.assertEqual(before.status_code, 200, before.text)
        self.assertEqual(before.content, payload)
        self._mark_patient_deleted()
        self.assertEqual(self.client.get("/api/patients/p1/avatar").status_code, 404)

    def test_old_appointment_returns_404_after_patient_is_marked_deleted(self):
        created = self.client.post("/api/appointments", json={
            "patient_identity": "p1", "start_time": "2026-08-04 09:00",
            "item_name": "Synthetic appointment", "doctor_name": "Synthetic doctor",
        })
        self.assertEqual(created.status_code, 200, created.text)
        url = f"/api/appointments/{created.json()['appointment']['appointment_id']}"
        before = self.client.get(url)
        self.assertEqual(before.status_code, 200, before.text)
        self.assertEqual(before.json()["appointment"]["display_name"], "Synthetic patient")
        self.assertEqual(before.json()["appointment"]["phone"], "70000000001")
        self._mark_patient_deleted()
        self.assertEqual(self.client.get(url).status_code, 404)

    def test_orphan_appointment_still_returns_its_existing_details(self):
        with connect(self.db) as conn:
            # Historical orphan rows can predate foreign-key enforcement.
            conn.execute("pragma foreign_keys=off")
            conn.execute(
                "insert into appointments(appointment_id, patient_identity, start_time, item_name) "
                "values ('orphan', 'missing-patient', '2026-08-04 09:00', 'Synthetic orphan')"
            )
            conn.commit()
        response = self.client.get("/api/appointments/orphan")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["appointment"]["item_name"], "Synthetic orphan")
        self.assertIsNone(response.json()["appointment"]["display_name"])
