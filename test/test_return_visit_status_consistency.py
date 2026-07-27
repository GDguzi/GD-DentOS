"""Revision and server-managed return-visit completion-date contracts."""
from __future__ import annotations

import json
import unittest
from unittest import mock

from return_visit_transaction_fixture import (
    ReturnVisitTransactionFixture,
)


class ReturnVisitStatusConsistencyTest(unittest.TestCase):
    def setUp(self):
        self.fixture = ReturnVisitTransactionFixture()

    def tearDown(self):
        self.fixture.close()

    def test_revision_token_is_strict_positive_builtin_int(self):
        for payload in (
            {"note": "missing"},
            {"expected_revision": True, "note": "bool"},
            {"expected_revision": 0, "note": "zero"},
            {"expected_revision": "1", "note": "string"},
        ):
            with self.subTest(payload=payload):
                before = self.fixture.digest()
                response = self.fixture.client.put(
                    "/api/return-visits/rv-edit", json=payload
                )
                self.assertEqual(response.status_code, 400)
                self.assertEqual(self.fixture.digest(), before)

    def test_stale_edit_and_single_delete_have_no_side_effects(self):
        cases = (
            (
                "PUT",
                "/api/return-visits/rv-edit",
                {"expected_revision": 2, "note": "stale"},
            ),
            (
                "DELETE",
                "/api/return-visits/rv-delete",
                {"expected_revision": 2},
            ),
        )
        for method, path, payload in cases:
            with self.subTest(method=method):
                before = self.fixture.digest()
                response = self.fixture.client.request(
                    method, path, json=payload
                )
                self.assertEqual(response.status_code, 409)
                self.assertEqual(
                    response.json(), {"detail": "return_visit_stale"}
                )
                self.assertEqual(self.fixture.digest(), before)

    def test_single_delete_requires_json_body_and_token(self):
        for kwargs in ({}, {"json": {}}, {"json": {"expected_revision": False}}):
            with self.subTest(kwargs=kwargs):
                before = self.fixture.digest()
                response = self.fixture.client.request(
                    "DELETE", "/api/return-visits/rv-delete", **kwargs
                )
                self.assertEqual(response.status_code, 400)
                self.assertEqual(self.fixture.digest(), before)

    def test_client_actual_date_is_always_rejected_without_side_effects(self):
        before = self.fixture.digest()
        created = self.fixture.client.post(
            "/api/patients/patient-active/return-visits",
            json={
                "due_time": "2026-08-04",
                "item_name": "Bad date create",
                "actual_date": "2026-07-20",
            },
        )
        self.assertEqual(created.status_code, 400)
        self.assertEqual(self.fixture.digest(), before)

        edited = self.fixture.client.put(
            "/api/return-visits/rv-edit",
            json={"expected_revision": 1, "actual_date": "2026-07-20"},
        )
        self.assertEqual(edited.status_code, 400)
        self.assertEqual(self.fixture.digest(), before)

    def test_server_sets_clears_and_preserves_actual_date_by_shared_done_state(self):
        with mock.patch(
            "local_app.timeutil.today_str", return_value="2026-07-20"
        ):
            completed = self.fixture.client.put(
                "/api/return-visits/rv-edit",
                json={"expected_revision": 1, "status": "已回访"},
            )
        self.assertEqual(completed.status_code, 200)
        first = self.fixture.row("rv-edit")
        self.assertEqual(first["actual_date"], "2026-07-20")
        self.assertEqual(first["revision"], 2)

        still_done = self.fixture.client.put(
            "/api/return-visits/rv-edit",
            json={"expected_revision": 2, "note": "other done field"},
        )
        self.assertEqual(still_done.status_code, 200)
        second = self.fixture.row("rv-edit")
        self.assertEqual(second["actual_date"], "2026-07-20")
        self.assertEqual(second["revision"], 3)

        pending = self.fixture.client.put(
            "/api/return-visits/rv-edit",
            json={"expected_revision": 3, "status": "待回访"},
        )
        self.assertEqual(pending.status_code, 200)
        third = self.fixture.row("rv-edit")
        self.assertEqual(third["actual_date"], "")
        self.assertEqual(third["revision"], 4)

    def test_done_state_uses_frozen_query_conflict_precedence(self):
        with mock.patch(
            "local_app.timeutil.today_str", return_value="2026-07-20"
        ):
            pending_prefix = self.fixture.client.put(
                "/api/return-visits/rv-edit",
                json={
                    "expected_revision": 1,
                    "status": "待回访",
                    "item_name": "已回访：synthetic prefix",
                },
            )
        self.assertEqual(pending_prefix.status_code, 200)
        self.assertEqual(self.fixture.row("rv-edit")["actual_date"], "")

        with mock.patch(
            "local_app.timeutil.today_str", return_value="2026-07-20"
        ):
            legacy_done = self.fixture.client.put(
                "/api/return-visits/rv-edit",
                json={
                    "expected_revision": 2,
                    "status": "other",
                    "note": "已回访：synthetic legacy marker",
                },
            )
        self.assertEqual(legacy_done.status_code, 200)
        self.assertEqual(
            self.fixture.row("rv-edit")["actual_date"], "2026-07-20"
        )

    def test_noop_keeps_revision_versions_and_audits_unchanged(self):
        before = self.fixture.digest()
        response = self.fixture.client.put(
            "/api/return-visits/rv-edit",
            json={"expected_revision": 1, "note": ""},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["return_visit"]["revision"], 1)
        self.assertEqual(self.fixture.digest(), before)

    def test_edit_and_delete_store_final_current_snapshot(self):
        edited = self.fixture.client.put(
            "/api/return-visits/rv-edit",
            json={"expected_revision": 1, "note": "new final value"},
        )
        self.assertEqual(edited.status_code, 200)
        edit_snapshot = json.loads(
            self.fixture.versions("rv-edit")[-1]["snapshot_json"]
        )
        self.assertEqual(edit_snapshot["note"], "new final value")
        self.assertEqual(edit_snapshot["revision"], 2)
        self.assertEqual(edit_snapshot["is_deleted"], 0)

        deleted = self.fixture.client.request(
            "DELETE",
            "/api/return-visits/rv-delete",
            json={"expected_revision": 1},
        )
        self.assertEqual(deleted.status_code, 200)
        delete_snapshot = json.loads(
            self.fixture.versions("rv-delete")[-1]["snapshot_json"]
        )
        self.assertEqual(delete_snapshot["revision"], 2)
        self.assertEqual(delete_snapshot["is_deleted"], 1)


if __name__ == "__main__":
    unittest.main()
