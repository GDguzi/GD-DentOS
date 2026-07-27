import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from local_app.api import create_app
from local_app.db import connect, init_db


def _client(tmpdir):
    db_path = Path(tmpdir) / "clinic.sqlite3"
    init_db(db_path)
    with connect(db_path) as conn:
        conn.execute(
            "insert into patients(patient_identity, display_name, updated_at, current_hash) "
            "values ('p1', '测试患者', '2026-06-07 00:00:00', 'h1')"
        )
        conn.commit()
    return db_path, TestClient(create_app(db_path))


class SurgeryTest(unittest.TestCase):
    def test_create_then_list(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            _, client = _client(tmpdir)
            resp = client.post(
                "/api/patients/p1/surgeries",
                json={
                    "surgery_date": "2026-06-10",
                    "surgeon": "王医生",
                    "surgery_name": "智齿拔除",
                    "tooth": "38",
                    "anesthesia": "局部麻醉",
                    "process": "翻瓣去骨分牙拔除",
                    "postop_advice": "24小时勿漱口",
                },
            )
            self.assertEqual(resp.status_code, 200)
            self.assertTrue(resp.json()["surgery_id"].startswith("local-surgery-"))

            listed = client.get("/api/patients/p1/surgeries").json()
            self.assertEqual(listed["totalcount"], 1)
            s = listed["surgeries"][0]
            self.assertEqual(s["surgery_name"], "智齿拔除")
            self.assertEqual(s["tooth"], "38")

    def test_empty_name_400(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            _, client = _client(tmpdir)
            resp = client.post(
                "/api/patients/p1/surgeries", json={"surgeon": "王医生"}
            )
        self.assertEqual(resp.status_code, 400)

    def test_unknown_patient_404(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            _, client = _client(tmpdir)
            self.assertEqual(client.get("/api/patients/nope/surgeries").status_code, 404)
            self.assertEqual(
                client.post("/api/patients/nope/surgeries",
                            json={"surgery_name": "x"}).status_code,
                404,
            )

    def test_bad_surgery_date_400(self):
        # 非法 surgery_date 应 400
        with tempfile.TemporaryDirectory() as tmpdir:
            _, client = _client(tmpdir)
            resp = client.post(
                "/api/patients/p1/surgeries",
                json={"surgery_name": "拔牙", "surgery_date": "明天"},
            )
            self.assertEqual(resp.status_code, 400)


if __name__ == "__main__":
    unittest.main()
