"""通话录音 Phase 1：上传 / 列表 / 回放(含 Range) / 改成交状态 / 删除 / 校验。"""
import io
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from local_app.api import create_app
from local_app.db import connect, init_db
from local_app.routes.calls import normalize_phone

_WAV = b"RIFF\x24\x00\x00\x00WAVEfmt " + b"\x00" * 28   # 够当一段"录音"字节


class NormalizePhoneTest(unittest.TestCase):
    def test_strip_and_country_code(self):
        self.assertEqual(normalize_phone(" 138 0013 8000 "), "13800138000")
        self.assertEqual(normalize_phone("+8613800138000"), "13800138000")
        self.assertEqual(normalize_phone("045186888888"), "45186888888")  # 去前导0
        self.assertEqual(normalize_phone(""), "")
        self.assertEqual(normalize_phone(None), "")


class CallUploadTest(unittest.TestCase):
    def _client(self, tmp):
        db = Path(tmp) / "clinic.sqlite3"
        init_db(db)
        with connect(db) as conn:
            conn.execute("insert into patients(patient_identity, display_name, updated_at, current_hash) "
                         "values ('p1','测试患者','x','h1')")
            conn.execute(
                "insert into return_visits(return_visit_id, patient_identity, "
                "due_time, item_name, revision) values "
                "('rv1', 'p1', '2026-07-09', 'Synthetic context', 1)"
            )
            conn.commit()
        return db, TestClient(create_app(db, images_dir=Path(tmp) / "img"))

    def _upload(self, client, **form):
        data = {"direction": "outbound", "peer_number": "13800138000",
                "started_at": "2026-07-09 10:00:00", "duration_sec": "71",
                "deal_status": "未成交", "linked_return_visit_id": "rv1",
                "expected_revision": "1"}
        data.update(form)
        return client.post("/api/patients/p1/calls",
                           files={"file": ("rec.wav", io.BytesIO(_WAV), "audio/wav")},
                           data=data)

    def test_upload_then_list(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, client = self._client(tmp)
            r = self._upload(client)
            self.assertEqual(r.status_code, 200)
            self.assertTrue(r.json()["call_id"].startswith("local-call-"))

            listed = client.get("/api/patients/p1/calls").json()
            self.assertEqual(listed["totalcount"], 1)
            c = listed["calls"][0]
            self.assertEqual(c["direction"], "outbound")
            self.assertEqual(c["peer_norm"], "13800138000")
            self.assertEqual(c["duration_sec"], 71)
            self.assertEqual(c["deal_status"], "未成交")
            self.assertEqual(c["match_status"], "manual")

    def test_recording_playback_and_range(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, client = self._client(tmp)
            call_id = self._upload(client).json()["call_id"]
            # 完整回放
            g = client.get(f"/api/calls/{call_id}/recording")
            self.assertEqual(g.status_code, 200)
            self.assertEqual(g.content, _WAV)
            # Range 拖动进度 → 206 部分内容
            part = client.get(f"/api/calls/{call_id}/recording", headers={"Range": "bytes=0-3"})
            self.assertEqual(part.status_code, 206)
            self.assertEqual(part.content, _WAV[:4])

    def test_patch_deal_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, client = self._client(tmp)
            call_id = self._upload(client).json()["call_id"]
            r = client.patch(
                f"/api/calls/{call_id}",
                json={"expected_revision": 1, "deal_status": "已成交"},
            )
            self.assertEqual(r.status_code, 200)
            c = client.get("/api/patients/p1/calls").json()["calls"][0]
            self.assertEqual(c["deal_status"], "已成交")

    def test_patch_empty_400(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, client = self._client(tmp)
            call_id = self._upload(client).json()["call_id"]
            self.assertEqual(client.patch(f"/api/calls/{call_id}", json={}).status_code, 400)

    def test_delete_removes_row_and_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, client = self._client(tmp)
            call_id = self._upload(client).json()["call_id"]
            self.assertEqual(
                client.request(
                    "DELETE", f"/api/calls/{call_id}",
                    json={"expected_revision": 1},
                ).status_code,
                200,
            )
            self.assertEqual(client.get("/api/patients/p1/calls").json()["totalcount"], 0)
            self.assertEqual(client.get(f"/api/calls/{call_id}/recording").status_code, 404)

    def test_bad_ext_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, client = self._client(tmp)
            r = client.post("/api/patients/p1/calls",
                            files={"file": ("rec.txt", io.BytesIO(b"hi"), "text/plain")},
                            data={"direction": "outbound"})
            self.assertEqual(r.status_code, 400)
            fake = client.post(
                "/api/patients/p1/calls",
                files={"file": ("rec.wav", io.BytesIO(b"not audio"), "audio/wav")},
                data={"direction": "outbound", "linked_return_visit_id": "rv1",
                      "expected_revision": "1"},
            )
            self.assertEqual(fake.status_code, 400)

    def test_bad_direction_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, client = self._client(tmp)
            r = self._upload(client, direction="sideways")
            self.assertEqual(r.status_code, 400)

    def test_unknown_patient_404(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, client = self._client(tmp)
            self.assertEqual(client.get("/api/patients/nope/calls").status_code, 404)
            r = client.post("/api/patients/nope/calls",
                            files={"file": ("rec.wav", io.BytesIO(_WAV), "audio/wav")},
                            data={"direction": "outbound", "linked_return_visit_id": "rv1",
                                  "expected_revision": "1"})
            self.assertEqual(r.status_code, 404)

    def test_missing_business_table_keeps_tracked_operation_until_schema_recovers(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "clinic.sqlite3"
            init_db(db)
            with connect(db) as conn:
                conn.execute("insert into patients(patient_identity, display_name, updated_at, current_hash) "
                             "values ('p1','测试患者','x','h1')")
                conn.execute(
                    "insert into return_visits(return_visit_id, patient_identity, "
                    "due_time, item_name, revision) values "
                    "('rv1', 'p1', '2026-07-09', 'Synthetic context', 1)"
                )
                conn.execute("drop table calls")   # 让 insert into calls 抛错,但 require_patient(读 patients)仍通过
                conn.commit()
            img = Path(tmp) / "img"
            client = TestClient(create_app(db, images_dir=img), raise_server_exceptions=False)
            r = client.post("/api/patients/p1/calls",
                            files={"file": ("rec.wav", io.BytesIO(_WAV), "audio/wav")},
                            data={"direction": "outbound", "linked_return_visit_id": "rv1",
                                  "expected_revision": "1"})
            self.assertEqual(r.status_code, 500)   # 明确报错,不 silent success
            with connect(db) as conn:
                self.assertEqual(
                    conn.execute("select count(*) from attachment_file_ops").fetchone()[0],
                    1,
                )
            self.assertTrue(list((img / "call-recordings").rglob("*.wav")))
            init_db(db)
            from local_app.attachment_file_ops import recover_attachment_file_ops
            recover_attachment_file_ops(db, img)
            self.assertEqual(list((img / "call-recordings").rglob("*.wav")), [])
            with connect(db) as conn:
                self.assertEqual(
                    conn.execute("select count(*) from attachment_file_ops").fetchone()[0],
                    0,
                )

    def test_delete_tombstone_move_failure_keeps_business_and_recoverable_log(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as tmp:
            db, client = self._client(tmp)
            call_id = self._upload(client).json()["call_id"]
            with patch("pathlib.Path.unlink", side_effect=OSError("boom")):
                r = client.request(
                    "DELETE", f"/api/calls/{call_id}",
                    json={"expected_revision": 1},
                )
            self.assertEqual(r.status_code, 500)
            self.assertEqual(client.get("/api/patients/p1/calls").json()["totalcount"], 0)
            with connect(db) as conn:
                self.assertEqual(
                    conn.execute("select status from attachment_file_ops").fetchone()[0],
                    "staging_delete",
                )
                self.assertEqual(
                    conn.execute("select count(*) from calls where call_id=?", (call_id,)).fetchone()[0],
                    1,
                )
            from local_app.attachment_file_ops import recover_attachment_file_ops
            recover_attachment_file_ops(db, Path(tmp) / "img")
            with connect(db) as conn:
                self.assertEqual(conn.execute("select count(*) from attachment_file_ops").fetchone()[0], 0)
            self.assertEqual(client.get("/api/patients/p1/calls").json()["totalcount"], 1)


if __name__ == "__main__":
    unittest.main()
