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


class ConsultTest(unittest.TestCase):
    def test_create_then_list(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            _, client = _client(tmpdir)
            resp = client.post(
                "/api/patients/p1/consults",
                json={
                    "consult_date": "2026-06-10",
                    "consult_doctor": "李咨询",
                    "consult_type": "初诊咨询",
                    "chief_complaint": "想做种植",
                    "plan": "建议先拍CT评估骨量",
                    "communication": "患者关注价格与周期",
                },
            )
            self.assertEqual(resp.status_code, 200)
            self.assertTrue(resp.json()["consult_id"].startswith("local-consult-"))

            listed = client.get("/api/patients/p1/consults").json()
            self.assertEqual(listed["totalcount"], 1)
            c = listed["consults"][0]
            self.assertEqual(c["consult_type"], "初诊咨询")
            self.assertEqual(c["chief_complaint"], "想做种植")

    def test_all_empty_content_400(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            _, client = _client(tmpdir)
            resp = client.post(
                "/api/patients/p1/consults",
                json={"consult_doctor": "李咨询", "consult_type": "回访"},
            )
        self.assertEqual(resp.status_code, 400)

    def test_unknown_patient_404(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            _, client = _client(tmpdir)
            self.assertEqual(client.get("/api/patients/nope/consults").status_code, 404)
            self.assertEqual(
                client.post("/api/patients/nope/consults", json={"plan": "x"}).status_code,
                404,
            )

    def test_bad_consult_date_400(self):
        # 非法 consult_date 应 400(与回访日期校验一致)
        with tempfile.TemporaryDirectory() as tmpdir:
            _, client = _client(tmpdir)
            resp = client.post(
                "/api/patients/p1/consults",
                json={"plan": "复查", "consult_date": "下周一"},
            )
            self.assertEqual(resp.status_code, 400)


if __name__ == "__main__":
    unittest.main()
