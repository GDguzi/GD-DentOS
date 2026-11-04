"""Uploads use available resources instead of fixed byte or SQLite row cutoffs."""
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

from local_app import auth
from local_app.api import create_app
from local_app.db import connect, init_db
from local_app.patient_import import parse_table
from local_app.routes import calls


class UploadSizeLimitsTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db = self.root / "test.sqlite3"
        self.images = self.root / "images"
        init_db(self.db)
        with connect(self.db) as conn:
            auth.create_user(conn, "test-admin", "Synthetic admin", "test-password", role="admin")
            conn.execute(
                "insert into patients(patient_identity, display_name, updated_at, current_hash) "
                "values ('p1', 'Synthetic patient', '2026-08-04', 'test-hash')"
            )
            conn.commit()
        self.client = TestClient(create_app(self.db, images_dir=self.images, require_login=True))
        self.addCleanup(self.client.close)
        login = self.client.post("/api/auth/login", json={
            "username": "test-admin", "password": "test-password",
        })
        self.assertEqual(login.status_code, 200, login.text)

    def _png(self, size):
        return b"\x89PNG\r\n\x1a\n" + b"x" * (size - 8)

    def test_patient_photo_above_old_8mb_is_saved_in_full(self):
        payload = self._png(8 * 1024 * 1024 + 1)
        response = self.client.post("/api/patients/p1/images", data={"slot": "本地上传"},
                                    files={"file": ("test.png", payload, "image/png")})
        self.assertEqual(response.status_code, 200, response.text)
        with connect(self.db) as conn:
            row = conn.execute("select rel_path, size_bytes from patient_images where image_id=?",
                               (response.json()["image_id"],)).fetchone()
        self.assertEqual(row["size_bytes"], len(payload))
        self.assertEqual((self.images / row["rel_path"]).read_bytes(), payload)

    def test_avatar_above_old_8mb_can_be_read_back(self):
        payload = self._png(8 * 1024 * 1024 + 1)
        response = self.client.post("/api/patients/p1/avatar",
                                    files={"file": ("test.png", payload, "image/png")})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.client.get("/api/patients/p1/avatar").content, payload)

    def test_communication_screenshot_above_old_8mb_can_be_read_back(self):
        created = self.client.post("/api/patients/p1/communications", json={
            "channel": "wechat", "content": "Synthetic communication",
        })
        self.assertEqual(created.status_code, 200, created.text)
        parent_id = created.json()["communication_id"]
        payload = self._png(8 * 1024 * 1024 + 1)
        response = self.client.post(f"/api/communications/{parent_id}/images",
                                    data={"expected_revision": "1"},
                                    files={"file": ("test.png", payload, "image/png")})
        self.assertEqual(response.status_code, 200, response.text)
        image_id = response.json()["image_id"]
        self.assertEqual(self.client.get(
            f"/api/communications/{parent_id}/images/{image_id}/file").content, payload)

    def test_return_visit_screenshot_above_old_8mb_can_be_read_back(self):
        created = self.client.post("/api/patients/p1/return-visits", json={
            "due_time": "2026-08-04", "item_name": "Synthetic follow-up", "channel": "wechat",
        })
        self.assertEqual(created.status_code, 200, created.text)
        parent_id = created.json()["return_visit_id"]
        payload = self._png(8 * 1024 * 1024 + 1)
        response = self.client.post(f"/api/return-visits/{parent_id}/images",
                                    data={"expected_revision": "1"},
                                    files={"file": ("test.png", payload, "image/png")})
        self.assertEqual(response.status_code, 200, response.text)
        image_id = response.json()["image_id"]
        self.assertEqual(self.client.get(
            f"/api/return-visits/{parent_id}/images/{image_id}/file").content, payload)

    def test_lab_image_above_old_20mb_can_be_read_back(self):
        created = self.client.post("/api/patients/p1/lab-orders", json={"project_type": "Synthetic crown"})
        self.assertEqual(created.status_code, 200, created.text)
        payload = self._png(20 * 1024 * 1024 + 1)
        response = self.client.post(f"/api/lab-orders/{created.json()['order_id']}/images",
                                    files={"file": ("test.png", payload, "image/png")})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.client.get(response.json()["url"]).content, payload)

    def test_recording_ignores_former_byte_cutoff_and_keeps_content(self):
        created = self.client.post("/api/patients/p1/communications", json={
            "channel": "phone", "direction": "outbound", "content": "Synthetic call",
        })
        self.assertEqual(created.status_code, 200, created.text)
        payload = b"RIFF\x24\x00\x00\x00WAVEfmt " + b"x" * 64
        with mock.patch.object(calls, "AUDIO_MAX_BYTES", 16, create=True):
            response = self.client.post("/api/patients/p1/calls",
                                        data={"direction": "outbound", "expected_revision": "1",
                                              "linked_communication_id": created.json()["communication_id"]},
                                        files={"file": ("test.wav", payload, "audio/wav")})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.client.get(
            f"/api/calls/{response.json()['call_id']}/recording").content, payload)

    def test_csv_above_old_10mb_previews_and_commits_every_row(self):
        row_count = 128
        payload = ("name,phone,备注\n" + "".join(
            f"Synthetic {i},7000000{i:04d},{'x' * 90000}\n" for i in range(row_count)
        )).encode("utf-8")
        self.assertGreater(len(payload), 10 * 1024 * 1024)
        for mode in ("preview", "commit"):
            with self.subTest(mode=mode):
                response = self.client.post("/api/patients/import", data={"mode": mode},
                                            files={"file": ("test.csv", payload, "text/csv")})
                self.assertEqual(response.status_code, 200, response.text)
                key = "importable" if mode == "preview" else "created"
                self.assertEqual(response.json()[key], row_count)
        with connect(self.db) as conn:
            self.assertEqual(conn.execute(
                "select count(*) from patients where patient_identity != 'p1'").fetchone()[0], row_count)
            self.assertEqual(conn.execute(
                "select min(length(remark)) from patients where patient_identity != 'p1'").fetchone()[0], 90000)


class SqliteRowLimitsTest(unittest.TestCase):
    def test_sqlite_reads_row_100001_without_truncating(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "source.sqlite3"
            with sqlite3.connect(path) as conn:
                conn.execute("create table patients(name text, phone text)")
                conn.executemany("insert into patients values (?, ?)", (
                    (f"Synthetic {i}", f"700{i:08d}") for i in range(100001)
                ))
            rows = parse_table("source.sqlite3", path.read_bytes())
        self.assertEqual(len(rows), 100002)
        self.assertEqual(rows[0], ["name", "phone"])
        self.assertEqual(rows[-1], ["Synthetic 100000", "70000100000"])
