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
        conn.execute(
            "insert into appointments(appointment_id, patient_identity, start_time, status) "
            "values ('a1', 'p1', '2026-06-10 09:00:00', '2')"
        )
        conn.execute(
            "insert into return_visits(return_visit_id, patient_identity, due_time, "
            "item_name, status, is_deleted) values ('rv1', 'p1', '2026-06-20', '复查', '0', 0)"
        )
        conn.execute(
            "insert into medical_records(record_id, patient_identity, visit_time, "
            "content_json, current_hash, updated_at) "
            "values ('m1', 'p1', '2026-06-10 09:00:00', '{\"PC\":\"主诉\"}', 'hm1', "
            "'2026-06-10 09:00:00')"
        )
        conn.commit()
    return db_path, TestClient(create_app(db_path))


class CompactDateParamRejectedTest(unittest.TestCase):
    """Python 3.11+ fromisoformat 放行 '20260613' 等紧凑格式，
    但库内按 substr(...,1,10) 横杠格式过滤——必须 400 而不是静默全 0。"""

    def test_today_work_rejects_compact_date(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path, client = _client(tmpdir)
            for bad in ("20260613", "2026-W24-5"):
                with self.subTest(date=bad):
                    resp = client.get(f"/api/today-work?date={bad}")
                    self.assertEqual(resp.status_code, 400)

    def test_list_endpoints_reject_compact_date(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path, client = _client(tmpdir)
            for url in (
                "/api/appointments?date=20260613",
                "/api/bills?date=20260613",
                "/api/return-visits?date=20260613",
                "/api/patients?birthday_on=20260613",
                "/api/reports/income?date_from=20260613&date_to=20260613",
            ):
                with self.subTest(url=url):
                    self.assertEqual(client.get(url).status_code, 400)


class WriteSideTimeValidationTest(unittest.TestCase):
    def test_appointment_rejects_garbage_time(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path, client = _client(tmpdir)
            for bad in ("明天", "2026/06/13", "2026-13-99 09:00:00"):
                with self.subTest(value=bad):
                    resp = client.put(
                        "/api/appointments/a1", json={"start_time": bad}
                    )
                    self.assertEqual(resp.status_code, 400)
            resp = client.put(
                "/api/appointments/a1", json={"start_time": "2026-06-11 10:00:00"}
            )
            self.assertEqual(resp.status_code, 200)

    def test_return_visit_rejects_garbage_due_time(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path, client = _client(tmpdir)
            resp = client.put("/api/return-visits/rv1", json={
                "expected_revision": 1, "due_time": "下周",
            })
            self.assertEqual(resp.status_code, 400)
            resp = client.post(
                "/api/patients/p1/return-visits",
                json={"due_time": "2026/07/01", "item_name": "复查"},
            )
            self.assertEqual(resp.status_code, 400)

    def test_medical_record_rejects_garbage_visit_time(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path, client = _client(tmpdir)
            resp = client.post(
                "/api/medical-records",
                json={"patient_identity": "p1", "visit_time": "昨天"},
            )
            self.assertEqual(resp.status_code, 400)
            resp = client.put("/api/medical-records/m1", json={"visit_time": "昨天"})
            self.assertEqual(resp.status_code, 400)


class JsonNullSafetyTest(unittest.TestCase):
    def test_null_content_json_does_not_wipe_record(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path, client = _client(tmpdir)
            resp = client.put(
                "/api/medical-records/m1",
                json={"doctor_name": "李医生", "content_json": None},
            )
            self.assertEqual(resp.status_code, 200)
            with connect(db_path) as conn:
                content = conn.execute(
                    "select content_json from medical_records where record_id='m1'"
                ).fetchone()["content_json"]
            self.assertIn("主诉", content)

    def test_null_text_fields_not_stored_as_none_string(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path, client = _client(tmpdir)
            resp = client.put(
                "/api/appointments/a1",
                json={"doctor_name": None, "item_name": "洁牙"},
            )
            self.assertEqual(resp.status_code, 200)
            with connect(db_path) as conn:
                doc = conn.execute(
                    "select doctor_name from appointments where appointment_id='a1'"
                ).fetchone()["doctor_name"]
            self.assertEqual(doc, "")


class NonDictListItemsRejectedTest(unittest.TestCase):
    def test_oral_exam_string_finding_400(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path, client = _client(tmpdir)
            resp = client.post(
                "/api/patients/p1/oral-exams",
                json={"health_level": "良好", "findings": ["蛀牙"]},
            )
            self.assertEqual(resp.status_code, 400)

    def test_treatment_plan_string_item_400(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path, client = _client(tmpdir)
            resp = client.post(
                "/api/patients/p1/treatment-plans",
                json={"plan_name": "修复计划", "groups": [{"group_type": "all", "items": ["补牙"]}]},
            )
            self.assertEqual(resp.status_code, 400)


if __name__ == "__main__":
    unittest.main()


class ImportedMicrosecondTimeTest(unittest.TestCase):
    def test_imported_microsecond_visit_time_accepted(self):
        """外部导入病历 visit_time 形如 09:43:00.000000，编辑保存不得被 400 拦下。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path, client = _client(tmpdir)
            resp = client.put(
                "/api/medical-records/m1",
                json={"visit_time": "2025-12-21 09:43:00.000000"},
            )
            self.assertEqual(resp.status_code, 200)
