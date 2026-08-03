import json
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from local_app.api import create_app
from local_app.db import connect, init_db
from local_app.versioning import stable_hash

RAW_SOURCE = '{"customerid":"c1","display_name":"旧姓名","原始字段":"SaaS原样"}'


def _client(tmpdir):
    db_path = Path(tmpdir) / "clinic.sqlite3"
    init_db(db_path)
    with connect(db_path) as conn:
        conn.execute(
            """
            insert into patients(
                patient_identity, source_customer_id, display_name,
                phone, sex, birthday, address, updated_at, current_hash, source_json
            )
            values ('p1', 'c1', '旧姓名', '100', '男', '2000-01-01', '旧地址',
                    '2026-06-07 00:00:00', 'h1', ?)
            """,
            (RAW_SOURCE,),
        )
        conn.commit()
    return db_path, TestClient(create_app(db_path))


class SourceJsonProtectionTest(unittest.TestCase):
    """本地编辑不得污染镜像层 source_json（SaaS 原始 payload）。"""

    def test_update_does_not_touch_source_json(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path, client = _client(tmpdir)
            resp = client.put("/api/patients/p1", json={"address": "新地址"})
            self.assertEqual(resp.status_code, 200)
            with connect(db_path) as conn:
                row = conn.execute(
                    "select source_json, address from patients where patient_identity='p1'"
                ).fetchone()
            self.assertEqual(row["source_json"], RAW_SOURCE)
            self.assertEqual(row["address"], "新地址")

    def test_repeated_edits_do_not_bloat_source_json(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path, client = _client(tmpdir)
            for i in range(6):
                resp = client.put("/api/patients/p1", json={"address": f"地址{i}"})
                self.assertEqual(resp.status_code, 200)
            with connect(db_path) as conn:
                size = conn.execute(
                    "select length(source_json) n from patients where patient_identity='p1'"
                ).fetchone()["n"]
            self.assertEqual(size, len(RAW_SOURCE))

    def test_version_hash_matches_stored_snapshot(self):
        """版本行的 version_hash 必须由其存储的 source_json 本身算出（哈希链可校验）。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path, client = _client(tmpdir)
            client.put("/api/patients/p1", json={"address": "新地址"})
            with connect(db_path) as conn:
                row = conn.execute(
                    "select version_hash, source_json from patient_versions "
                    "where patient_identity='p1'"
                ).fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(row["version_hash"], stable_hash(json.loads(row["source_json"])))

    def test_noop_update_writes_no_version_or_audit(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path, client = _client(tmpdir)
            client.put("/api/patients/p1", json={"address": "新地址"})
            resp = client.put("/api/patients/p1", json={"address": "新地址"})
            self.assertEqual(resp.status_code, 200)
            with connect(db_path) as conn:
                versions = conn.execute(
                    "select count(*) n from patient_versions where patient_identity='p1'"
                ).fetchone()["n"]
                audits = conn.execute(
                    "select count(*) n from audit_logs where entity_id='p1'"
                ).fetchone()["n"]
            self.assertEqual(versions, 1)
            self.assertEqual(audits, 1)


class ProfileValidationTest(unittest.TestCase):
    def test_cannot_clear_existing_name_or_phone(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path, client = _client(tmpdir)
            for field in ("display_name", "phone"):
                with self.subTest(field=field):
                    resp = client.put("/api/patients/p1", json={field: ""})
                    self.assertEqual(resp.status_code, 400)

    def test_legacy_empty_phone_patient_can_still_edit(self):
        """历史同步行 phone 本来为空：整表单提交（含空 phone）不能被 400 卡死。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path, client = _client(tmpdir)
            with connect(db_path) as conn:
                conn.execute("update patients set phone='' where patient_identity='p1'")
                conn.commit()
            resp = client.put(
                "/api/patients/p1",
                json={"display_name": "旧姓名", "phone": "", "address": "改地址"},
            )
            self.assertEqual(resp.status_code, 200)

    def test_json_null_not_stored_as_none_string(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path, client = _client(tmpdir)
            resp = client.put("/api/patients/p1", json={"address": None})
            self.assertEqual(resp.status_code, 200)
            with connect(db_path) as conn:
                row = conn.execute(
                    "select address from patients where patient_identity='p1'"
                ).fetchone()
            self.assertEqual(row["address"], "")


if __name__ == "__main__":
    unittest.main()
