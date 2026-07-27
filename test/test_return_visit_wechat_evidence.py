"""Wechat return-visit evidence stays server-derived and actor-bound."""
from __future__ import annotations

import json
import sqlite3
import unittest

from return_visit_transaction_fixture import (
    ReturnVisitTransactionFixture,
)


class ReturnVisitWechatEvidenceTest(unittest.TestCase):
    def setUp(self):
        self.fixture = ReturnVisitTransactionFixture()

    def tearDown(self):
        self.fixture.close()

    def test_wechat_channel_is_recorded_but_client_evidence_and_actor_are_ignored(self):
        response = self.fixture.client.post(
            "/api/patients/patient-active/return-visits",
            json={
                "due_time": "2026-08-05",
                "item_name": "Synthetic wechat follow-up",
                "channel": "wechat",
                "operator": "forged-client-operator",
                "has_screenshot": True,
                "images": [{"image_id": "forged-image"}],
                "revision": 99,
                "is_deleted": 1,
                "origin": "forged-origin",
                "created_at": "1900-01-01 00:00:00",
                "updated_at": "1900-01-01 00:00:00",
            },
        )
        self.assertEqual(response.status_code, 200)
        return_visit_id = response.json()["return_visit_id"]
        row = self.fixture.row(return_visit_id)
        self.assertEqual(row["channel"], "wechat")
        self.assertEqual(row["revision"], 1)
        self.assertEqual(row["is_deleted"], 0)
        self.assertEqual(row["origin"], "local")
        self.assertNotEqual(row["created_at"], "1900-01-01 00:00:00")
        self.assertNotEqual(row["updated_at"], "1900-01-01 00:00:00")

        versions = self.fixture.versions(return_visit_id)
        snapshot = json.loads(versions[-1]["snapshot_json"])
        self.assertEqual(snapshot["channel"], "wechat")
        self.assertNotIn("operator", snapshot)
        self.assertNotIn("has_screenshot", snapshot)
        self.assertNotIn("images", snapshot)

        audits = self.fixture.audits(return_visit_id)
        self.assertEqual(audits[-1]["operator"], self.fixture.access.USERNAME)
        self.assertEqual(
            audits[-1]["actor_user_id"], self.fixture.access.USER_ID
        )
        self.assertNotIn("forged-client-operator", audits[-1]["new_json"])

        detail = self.fixture.client.get(
            f"/api/return-visits/{return_visit_id}"
        )
        self.assertEqual(detail.status_code, 200)
        self.assertEqual(detail.json()["channel"], "wechat")
        self.assertEqual(detail.json()["images"], [])

        conn = sqlite3.connect(self.fixture.db_path)
        try:
            image_count = conn.execute(
                "select count(*) from return_visit_images "
                "where return_visit_id = ?",
                (return_visit_id,),
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(image_count, 0)

    def test_edit_can_change_channel_but_not_server_owned_fields(self):
        response = self.fixture.client.put(
            "/api/return-visits/rv-edit",
            json={
                "expected_revision": 1,
                "channel": "wechat",
                "operator": "forged-client-operator",
                "has_screenshot": True,
                "images": [{"image_id": "forged-image"}],
                "revision": 77,
                "is_deleted": 1,
            },
        )
        self.assertEqual(response.status_code, 200)
        row = self.fixture.row("rv-edit")
        self.assertEqual(row["channel"], "wechat")
        self.assertEqual(row["revision"], 2)
        self.assertEqual(row["is_deleted"], 0)
        self.assertEqual(self.fixture.client.get(
            "/api/return-visits/rv-edit"
        ).json()["images"], [])
        audit = self.fixture.audits("rv-edit")[-1]
        self.assertEqual(audit["operator"], self.fixture.access.USERNAME)
        self.assertNotIn("forged-client-operator", audit["new_json"])


if __name__ == "__main__":
    unittest.main()
