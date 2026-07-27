import json
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from local_app.api import create_app
from local_app.db import connect, init_db

TOOTH = '{"RT":["6"]}'


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


def _insert_draft(db_path, record_id="d1", tooth_json=TOOTH):
    with connect(db_path) as conn:
        conn.execute(
            "insert into medical_records(record_id, patient_identity, visit_time, "
            "doctor_name, tooth_json, content_json, draft_status, origin, ai_meta_json, "
            "created_at, updated_at, current_hash) "
            "values (?, 'p1', '2026-06-10 09:00:00', '王医生', ?, ?, 'ai_draft', "
            "'ai_voice', '{}', datetime('now','localtime'), datetime('now','localtime'), 'h-d1')",
            (record_id, tooth_json, json.dumps({"PC": "AI主诉"}, ensure_ascii=False)),
        )
        conn.commit()


def _insert_unclaimed(db_path, unclaimed_id="u1", ai_meta=None):
    with connect(db_path) as conn:
        conn.execute(
            "insert into ai_unclaimed_drafts(unclaimed_id, code_name, visit_time, room, "
            "doctor_name, content_json, tooth_json, ai_meta_json, source_file, status, created_at) "
            "values (?, '代号A', '2026-06-10', '诊室1', '王医生', ?, ?, ?, 'f.md', 'pending', "
            "datetime('now','localtime'))",
            (
                unclaimed_id,
                json.dumps({"PC": "无主主诉"}, ensure_ascii=False),
                TOOTH,
                json.dumps(ai_meta or {}, ensure_ascii=False),
            ),
        )
        conn.commit()


class ConfirmToothProtectionTest(unittest.TestCase):
    def test_drafts_list_returns_tooth_json(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path, client = _client(tmpdir)
            _insert_draft(db_path)
            body = client.get("/api/ai-drafts").json()
            self.assertEqual(body["drafts"][0]["tooth_json"], TOOTH)
            self.assertIn("record_type", body["drafts"][0])

    def test_confirm_with_empty_tooth_clears(self):
        """架构铁律#禁止兼容层：旧前端「固定回传{}」的防误清分支已删——
        现前端从DOM带回完整牙位，回传{}即医生故意清空，必须生效。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path, client = _client(tmpdir)
            _insert_draft(db_path)
            resp = client.post(
                "/api/ai-drafts/d1/confirm",
                json={"content_json": {"PC": "AI主诉"}, "tooth_json": {}},
            )
            self.assertEqual(resp.status_code, 200)
            with connect(db_path) as conn:
                tooth = conn.execute(
                    "select tooth_json from medical_records where record_id='d1'"
                ).fetchone()["tooth_json"]
            self.assertNotIn("RT", tooth)

    def test_confirm_can_change_tooth_to_other_value(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path, client = _client(tmpdir)
            _insert_draft(db_path)
            resp = client.post(
                "/api/ai-drafts/d1/confirm",
                json={"tooth_json": {"LB": ["4"]}},
            )
            self.assertEqual(resp.status_code, 200)
            with connect(db_path) as conn:
                tooth = conn.execute(
                    "select tooth_json from medical_records where record_id='d1'"
                ).fetchone()["tooth_json"]
            self.assertIn("LB", tooth)


class EditTierAllFieldsTest(unittest.TestCase):
    def test_tooth_only_change_is_minor_not_none(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path, client = _client(tmpdir)
            _insert_draft(db_path)
            resp = client.post(
                "/api/ai-drafts/d1/confirm",
                json={"content_json": {"PC": "AI主诉"}, "tooth_json": {"LB": ["4"]}},
            )
            self.assertEqual(resp.json()["edit_tier"], "minor")

    def test_doctor_only_change_is_minor_not_none(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path, client = _client(tmpdir)
            _insert_draft(db_path)
            resp = client.post(
                "/api/ai-drafts/d1/confirm",
                json={"content_json": {"PC": "AI主诉"}, "doctor_name": "李医生"},
            )
            self.assertEqual(resp.json()["edit_tier"], "minor")

    def test_true_noop_confirm_stays_none(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path, client = _client(tmpdir)
            _insert_draft(db_path)
            resp = client.post(
                "/api/ai-drafts/d1/confirm",
                json={
                    "content_json": {"PC": "AI主诉"},
                    "tooth_json": json.loads(TOOTH),
                    "doctor_name": "王医生",
                },
            )
            self.assertEqual(resp.json()["edit_tier"], "none")


class ClaimAtomicityTest(unittest.TestCase):
    def test_second_claim_rejected_and_single_record(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path, client = _client(tmpdir)
            _insert_unclaimed(db_path)
            first = client.post(
                "/api/ai-unclaimed/u1/claim", json={"patient_identity": "p1"}
            )
            second = client.post(
                "/api/ai-unclaimed/u1/claim", json={"patient_identity": "p1"}
            )
            self.assertEqual(first.status_code, 200)
            self.assertEqual(second.status_code, 404)
            with connect(db_path) as conn:
                count = conn.execute(
                    "select count(*) n from medical_records where origin='ai_voice'"
                ).fetchone()["n"]
            self.assertEqual(count, 1)

    def test_claim_inherits_extras_meta(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path, client = _client(tmpdir)
            _insert_unclaimed(db_path, ai_meta={"extras": {"收费明细": "根管 800"}})
            resp = client.post(
                "/api/ai-unclaimed/u1/claim", json={"patient_identity": "p1"}
            )
            record_id = resp.json()["record_id"]
            with connect(db_path) as conn:
                meta = json.loads(conn.execute(
                    "select ai_meta_json from medical_records where record_id=?",
                    (record_id,),
                ).fetchone()["ai_meta_json"])
            self.assertEqual(meta.get("extras", {}).get("收费明细"), "根管 800")
            self.assertEqual(meta.get("claimed_from"), "u1")


class DiscardStateMachineTest(unittest.TestCase):
    def test_claimed_draft_cannot_be_discarded(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path, client = _client(tmpdir)
            _insert_unclaimed(db_path)
            client.post("/api/ai-unclaimed/u1/claim", json={"patient_identity": "p1"})
            resp = client.post("/api/ai-unclaimed/u1/discard")
            self.assertEqual(resp.status_code, 409)
            with connect(db_path) as conn:
                status = conn.execute(
                    "select status from ai_unclaimed_drafts where unclaimed_id='u1'"
                ).fetchone()["status"]
            self.assertEqual(status, "claimed")

    def test_pending_draft_can_be_discarded_once(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path, client = _client(tmpdir)
            _insert_unclaimed(db_path)
            self.assertEqual(client.post("/api/ai-unclaimed/u1/discard").status_code, 200)
            self.assertEqual(client.post("/api/ai-unclaimed/u1/discard").status_code, 409)


if __name__ == "__main__":
    unittest.main()
