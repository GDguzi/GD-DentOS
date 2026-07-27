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


def _sample_exam():
    return {
        "exam_date": "2026-06-10",
        "exam_doctor": "王医生",
        "health_level": "一般",
        "findings": [
            {"tooth": "36", "symptom": "深龋", "severity": "中等"},
            {"tooth": "11", "symptom": "牙结石", "severity": "较轻"},
        ],
    }


class OralExamTest(unittest.TestCase):
    def test_create_then_list(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path, client = _client(tmpdir)
            resp = client.post("/api/patients/p1/oral-exams", json=_sample_exam())
            self.assertEqual(resp.status_code, 200)
            body = resp.json()
            self.assertTrue(body["exam_id"].startswith("local-exam-"))
            self.assertEqual(body["finding_count"], 2)

            listed = client.get("/api/patients/p1/oral-exams").json()
            self.assertEqual(listed["totalcount"], 1)
            exam = listed["exams"][0]
            self.assertEqual(exam["health_level"], "一般")
            teeth = {f["tooth"] for f in exam["findings"]}
            self.assertEqual(teeth, {"36", "11"})

    def test_blank_symptom_findings_skipped(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path, client = _client(tmpdir)
            client.post(
                "/api/patients/p1/oral-exams",
                json={"health_level": "良好", "findings": [
                    {"tooth": "36", "symptom": "深龋"},
                    {"tooth": "37", "symptom": "  "},
                ]},
            )
            exam = client.get("/api/patients/p1/oral-exams").json()["exams"][0]
        self.assertEqual(len(exam["findings"]), 1)

    def test_invalid_health_level_400(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            _, client = _client(tmpdir)
            resp = client.post(
                "/api/patients/p1/oral-exams",
                json={"health_level": "超级好", "findings": []},
            )
        self.assertEqual(resp.status_code, 400)

    def test_invalid_severity_400(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            _, client = _client(tmpdir)
            resp = client.post(
                "/api/patients/p1/oral-exams",
                json={"findings": [{"symptom": "深龋", "severity": "致命"}]},
            )
        self.assertEqual(resp.status_code, 400)

    def test_unknown_patient_404(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            _, client = _client(tmpdir)
            self.assertEqual(
                client.get("/api/patients/nope/oral-exams").status_code, 404
            )
            self.assertEqual(
                client.post("/api/patients/nope/oral-exams", json=_sample_exam()).status_code,
                404,
            )


if __name__ == "__main__":
    unittest.main()
