"""Atomic communication/call writes using only synthetic Access V3 data."""
from __future__ import annotations

import concurrent.futures
import io
import json
import sqlite3
import threading
import unittest
from unittest import mock

from fastapi.testclient import TestClient

from customer_hub_access_fixture import CustomerHubAccessFixture


WRITE_PERMISSIONS = (
    "patient.profile.view",
    "communication.view",
    "communication.manage",
    "communication.delete",
    "call_record.view",
    "call_record.manage",
    "call_record.delete",
)

_WAV = b"RIFF\x24\x00\x00\x00WAVEfmt " + b"\x00" * 28


class CommunicationCallTransactionFixture:
    def __init__(self):
        self.access = CustomerHubAccessFixture()
        self._seed()
        self.client = self.access.client_with_permissions(WRITE_PERMISSIONS)
        self.safe_client = TestClient(
            self.access.app, raise_server_exceptions=False
        )
        response = self.safe_client.post(
            "/api/auth/login",
            json={
                "username": self.access.USERNAME,
                "password": self.access.PASSWORD,
            },
        )
        if response.status_code != 200:
            raise AssertionError("synthetic Access V3 login failed")
        self.access.clients.append(self.safe_client)

    @property
    def db_path(self):
        return self.access.db_path

    def _seed(self):
        conn = sqlite3.connect(self.access.db_path)
        try:
            conn.executemany(
                "insert into patients(patient_identity, display_name, is_deleted, "
                "current_hash, source_json) values (?, ?, ?, 'hash', '{}')",
                (
                    ("patient-active", "Synthetic active patient", 0),
                    ("patient-other", "Synthetic other patient", 0),
                    ("patient-deleted", "Synthetic deleted patient", 1),
                ),
            )
            conn.executemany(
                "insert into communications(communication_id, patient_identity, "
                "channel, direction, reason, contacted_at, operator, content, "
                "deal_status, note, revision, created_at, updated_at) "
                "values (?, ?, ?, ?, 'Synthetic reason', '2026-07-20 09:00:00', "
                "'Synthetic operator', ?, 'open', 'Synthetic note', 1, "
                "'2026-07-20 09:00:00', '2026-07-20 09:00:00')",
                (
                    ("comm-edit", "patient-active", "phone", "outbound", "Before"),
                    ("comm-delete", "patient-active", "wechat", "", "Delete me"),
                    ("comm-with-call", "patient-active", "phone", "outbound", "Parent"),
                    ("comm-race-delete", "patient-active", "phone", "outbound", "Race delete"),
                    ("comm-race-direction", "patient-active", "phone", "outbound", "Race direction"),
                    ("comm-other", "patient-other", "phone", "inbound", "Other"),
                    ("comm-deleted-patient", "patient-deleted", "phone", "inbound", "Hidden"),
                ),
            )
            conn.executemany(
                "insert into calls(call_id, patient_identity, direction, source, "
                "linked_communication_id, deal_status, region, note, revision, "
                "created_at) values (?, ?, ?, 'manual_import', ?, 'open', "
                "'Synthetic region', ?, 1, '2026-07-20 09:00:00')",
                (
                    ("call-edit", "patient-active", "outbound", "comm-edit", "Before"),
                    ("call-delete", "patient-active", "outbound", "comm-edit", "Delete me"),
                    ("call-direction-lock", "patient-active", "outbound", "comm-with-call", "Lock"),
                    ("call-other", "patient-other", "inbound", "comm-other", "Other"),
                    ("call-deleted-patient", "patient-deleted", "inbound", "comm-deleted-patient", "Hidden"),
                ),
            )
            conn.commit()
        finally:
            conn.close()

    def close(self):
        self.access.close()

    def row(self, table, id_column, entity_id):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            return conn.execute(
                f"select * from {table} where {id_column} = ?", (entity_id,)
            ).fetchone()
        finally:
            conn.close()

    def audits(self, entity_type, entity_id=None):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            sql = "select * from audit_logs where entity_type = ?"
            params = [entity_type]
            if entity_id is not None:
                sql += " and entity_id = ?"
                params.append(entity_id)
            sql += " order by audit_id"
            return conn.execute(sql, tuple(params)).fetchall()
        finally:
            conn.close()

    def upload_call(
        self, *, client=None, patient_identity="patient-active", **form
    ):
        data = {
            "direction": "outbound",
            "source": "manual_import",
            "started_at": "2026-07-20 10:00:00",
            "expected_revision": "1",
        }
        if not any(
            key in form
            for key in (
                "linked_communication_id",
                "linked_return_visit_id",
                "linked_consult_id",
            )
        ):
            data["linked_communication_id"] = "comm-race-direction"
        data.update(form)
        return (client or self.client).post(
            f"/api/patients/{patient_identity}/calls",
            files={"file": ("synthetic.wav", io.BytesIO(_WAV), "audio/wav")},
            data=data,
        )

    def call_files(self):
        root = self.access.images_dir / "call-recordings"
        return sorted(root.rglob("*.wav")) if root.exists() else []

    def call_count(self):
        conn = sqlite3.connect(self.db_path)
        try:
            return conn.execute("select count(*) from calls").fetchone()[0]
        finally:
            conn.close()

    def upload_with_mutation_before_begin(self, mutation, **form):
        from local_app import call_transactions as transactions
        from local_app.routes import calls as calls_route

        real_begin = transactions.begin_immediate
        mutated = False

        def mutate_then_begin(conn):
            nonlocal mutated
            if not mutated:
                mutated = True
                mutation()
            return real_begin(conn)

        with mock.patch.object(
            transactions, "begin_immediate", side_effect=mutate_then_begin
        ), mock.patch.object(
            calls_route,
            "begin_immediate",
            side_effect=mutate_then_begin,
            create=True,
        ):
            response = self.upload_call(**form)
        return response, mutated

    def digest(self):
        conn = sqlite3.connect(self.db_path)
        try:
            communications = conn.execute(
                "select communication_id, patient_identity, channel, direction, "
                "reason, contacted_at, operator, content, deal_status, note, "
                "revision from communications order by communication_id"
            ).fetchall()
            calls = conn.execute(
                "select call_id, patient_identity, direction, deal_status, region, "
                "note, revision from calls order by call_id"
            ).fetchall()
            audits = conn.execute(
                "select entity_type, entity_id, action, old_json, new_json, "
                "operator, actor_user_id from audit_logs "
                "where entity_type in ('communication', 'call') order by audit_id"
            ).fetchall()
            return communications, calls, audits
        finally:
            conn.close()

    def restore_actor(self):
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                "update users set is_active = 1 where id = ?",
                (self.access.USER_ID,),
            )
            conn.execute(
                "update staff_members set employment_status = 'employed' "
                "where staff_id = ?",
                (self.access.STAFF_ID,),
            )
            conn.execute(
                "delete from role_permissions where role_key = ?",
                (self.access.ROLE_KEY,),
            )
            conn.executemany(
                "insert into role_permissions(role_key, perm_key) values (?, ?)",
                [(self.access.ROLE_KEY, permission) for permission in WRITE_PERMISSIONS],
            )
            conn.commit()
        finally:
            conn.close()

    def request_with_mutation_before_begin(
        self, client, method, path, mutation, **kwargs
    ):
        if path.startswith("/api/calls/"):
            from local_app import call_transactions as transactions
        else:
            from local_app import communication_transactions as transactions

        real_begin = transactions.begin_immediate
        callback_hit = False

        def mutate_then_begin(conn):
            nonlocal callback_hit
            callback_hit = True
            mutation()
            return real_begin(conn)

        with mock.patch.object(
            transactions, "begin_immediate", side_effect=mutate_then_begin
        ):
            response = client.request(method, path, **kwargs)
        return response, callback_hit


class CommunicationCallAtomicTransactionTest(unittest.TestCase):
    def setUp(self):
        self.fixture = CommunicationCallTransactionFixture()

    def tearDown(self):
        self.fixture.close()

    def test_create_rejects_client_operator_and_contacted_at_without_side_effects(self):
        for forbidden in (
            {"operator": "Forged operator"},
            {"contacted_at": "2099-01-01 00:00:00"},
        ):
            with self.subTest(forbidden=tuple(forbidden)):
                before = self.fixture.digest()
                response = self.fixture.client.post(
                    "/api/patients/patient-active/communications",
                    json={
                        "channel": "phone",
                        "direction": "outbound",
                        "content": "Synthetic content",
                        **forbidden,
                    },
                )
                self.assertEqual(response.status_code, 400)
                self.assertEqual(self.fixture.digest(), before)

    def test_create_uses_locked_display_name_server_time_and_stable_actor_id(self):
        with mock.patch(
            "local_app.communication_transactions.timeutil.now_str",
            return_value="2026-07-20 18:30:45",
        ):
            response = self.fixture.client.post(
                "/api/patients/patient-active/communications",
                json={
                    "channel": "phone",
                    "direction": "outbound",
                    "content": "  合法 Unicode 内容  ",
                },
            )
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["revision"], 1)
        row = self.fixture.row(
            "communications", "communication_id", body["communication_id"]
        )
        self.assertEqual(row["operator"], "合成客户通用户")
        self.assertEqual(row["contacted_at"], "2026-07-20 18:30:45")
        self.assertEqual(row["content"], "  合法 Unicode 内容  ")
        audit = self.fixture.audits("communication", body["communication_id"])[0]
        self.assertEqual(audit["actor_user_id"], self.fixture.access.USER_ID)
        self.assertEqual(json.loads(audit["new_json"])["revision"], 1)

    def test_communication_direction_rules_are_fail_closed(self):
        cases = (
            {"channel": "phone", "content": "missing direction"},
            {"channel": "phone", "direction": "sideways", "content": "bad"},
            {"channel": "wechat", "direction": "inbound", "content": "bad"},
        )
        for payload in cases:
            with self.subTest(payload=payload):
                before = self.fixture.digest()
                response = self.fixture.client.post(
                    "/api/patients/patient-active/communications", json=payload
                )
                self.assertEqual(response.status_code, 400)
                self.assertEqual(self.fixture.digest(), before)

        accepted = self.fixture.client.post(
            "/api/patients/patient-active/communications",
            json={"channel": "wechat", "direction": "   ", "content": "ok"},
        )
        self.assertEqual(accepted.status_code, 200)
        row = self.fixture.row(
            "communications", "communication_id", accepted.json()["communication_id"]
        )
        self.assertEqual(row["direction"], "")

    def test_patch_and_delete_require_exact_revision_and_noop_is_silent(self):
        cases = (
            ("PATCH", "/api/communications/comm-edit", {"content": "Changed"}),
            ("PATCH", "/api/communications/comm-edit", {"content": "Changed", "expected_revision": True}),
            ("DELETE", "/api/communications/comm-delete", None),
            ("DELETE", "/api/communications/comm-delete", {"expected_revision": 0}),
            ("PATCH", "/api/calls/call-edit", {"note": "Changed"}),
            ("PATCH", "/api/calls/call-edit", {"note": "Changed", "expected_revision": True}),
            ("DELETE", "/api/calls/call-delete", None),
            ("DELETE", "/api/calls/call-delete", {"expected_revision": "1"}),
        )
        for method, path, payload in cases:
            with self.subTest(method=method, path=path, payload=payload):
                before = self.fixture.digest()
                kwargs = {} if payload is None else {"json": payload}
                response = self.fixture.client.request(method, path, **kwargs)
                self.assertEqual(response.status_code, 400)
                self.assertEqual(self.fixture.digest(), before)

        for method, path, payload in (
            ("PATCH", "/api/communications/comm-edit", {"expected_revision": 2, "content": "Changed"}),
            ("DELETE", "/api/communications/comm-delete", {"expected_revision": 2}),
            ("PATCH", "/api/calls/call-edit", {"expected_revision": 2, "note": "Changed"}),
            ("DELETE", "/api/calls/call-delete", {"expected_revision": 2}),
        ):
            with self.subTest(stale=path):
                before = self.fixture.digest()
                response = self.fixture.client.request(method, path, json=payload)
                self.assertEqual(response.status_code, 409)
                self.assertEqual(self.fixture.digest(), before)

        before_audits = len(self.fixture.audits("communication", "comm-edit"))
        response = self.fixture.client.patch(
            "/api/communications/comm-edit",
            json={"expected_revision": 1, "content": "Before"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["communication"]["revision"], 1)
        self.assertEqual(len(self.fixture.audits("communication", "comm-edit")), before_audits)

        response = self.fixture.client.patch(
            "/api/calls/call-edit",
            json={"expected_revision": 1, "note": "Before"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["call"]["revision"], 1)
        self.assertEqual(len(self.fixture.audits("call", "call-edit")), 0)

    def test_successful_edits_increment_revision_and_audit_old_new_rows(self):
        response = self.fixture.client.patch(
            "/api/communications/comm-edit",
            json={"expected_revision": 1, "content": "After"},
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["communication"]["revision"], 2)
        audit = self.fixture.audits("communication", "comm-edit")[0]
        self.assertEqual(json.loads(audit["old_json"])["content"], "Before")
        self.assertEqual(json.loads(audit["new_json"])["content"], "After")
        self.assertEqual(json.loads(audit["new_json"])["revision"], 2)

        response = self.fixture.client.patch(
            "/api/calls/call-edit",
            json={"expected_revision": 1, "note": "After"},
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["call"]["revision"], 2)
        audit = self.fixture.audits("call", "call-edit")[0]
        self.assertEqual(json.loads(audit["old_json"])["note"], "Before")
        self.assertEqual(json.loads(audit["new_json"])["note"], "After")

    def test_hard_deletes_increment_revision_and_audit_neutral_tombstones(self):
        cases = (
            (
                "communication",
                "communications",
                "communication_id",
                "comm-delete",
                "/api/communications/comm-delete",
            ),
            (
                "call",
                "calls",
                "call_id",
                "call-delete",
                "/api/calls/call-delete",
            ),
        )
        for entity_type, table, id_column, entity_id, path in cases:
            with self.subTest(entity_type=entity_type):
                response = self.fixture.client.request(
                    "DELETE", path, json={"expected_revision": 1}
                )
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.json()["revision"], 2)
                self.assertIsNone(
                    self.fixture.row(table, id_column, entity_id)
                )
                audit = self.fixture.audits(entity_type, entity_id)[0]
                self.assertEqual(json.loads(audit["old_json"])["revision"], 1)
                self.assertEqual(
                    json.loads(audit["new_json"]),
                    {id_column: entity_id, "deleted": True, "revision": 2},
                )

    def test_communication_delete_rejects_linked_calls_without_orphans(self):
        before = self.fixture.digest()
        response = self.fixture.client.request(
            "DELETE",
            "/api/communications/comm-edit",
            json={"expected_revision": 1},
        )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(
            response.json(), {"detail": "communication_has_linked_calls"}
        )
        self.assertEqual(self.fixture.digest(), before)

        edited = self.fixture.client.patch(
            "/api/calls/call-edit",
            json={"expected_revision": 1, "note": "Still editable"},
        )
        self.assertEqual(edited.status_code, 200, edited.text)
        removed = self.fixture.client.request(
            "DELETE",
            "/api/calls/call-delete",
            json={"expected_revision": 1},
        )
        self.assertEqual(removed.status_code, 200, removed.text)
        self.assertIsNotNone(
            self.fixture.row("calls", "call_id", "call-edit")
        )

        removed = self.fixture.client.request(
            "DELETE",
            "/api/calls/call-edit",
            json={"expected_revision": 2},
        )
        self.assertEqual(removed.status_code, 200, removed.text)
        response = self.fixture.client.request(
            "DELETE",
            "/api/communications/comm-edit",
            json={"expected_revision": 1},
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["revision"], 2)
        self.assertIsNone(
            self.fixture.row(
                "communications", "communication_id", "comm-edit"
            )
        )

    def test_direction_changes_cannot_conflict_with_linked_recordings(self):
        before = self.fixture.digest()
        response = self.fixture.client.patch(
            "/api/communications/comm-with-call",
            json={"expected_revision": 1, "direction": "inbound"},
        )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.fixture.digest(), before)

        response = self.fixture.client.patch(
            "/api/calls/call-edit",
            json={"expected_revision": 1, "direction": "inbound"},
        )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.fixture.digest(), before)

    def test_call_upload_and_parent_delete_are_serialized(self):
        from local_app import communication_transactions as transactions
        from local_app.routes import calls as calls_route

        reached = threading.Event()
        release = threading.Event()
        first_lock = True
        first_lock_guard = threading.Lock()
        real_begin = transactions.begin_immediate

        def pause_first_begin(conn):
            nonlocal first_lock
            with first_lock_guard:
                pause = first_lock
                first_lock = False
            if pause:
                reached.set()
                if not release.wait(timeout=5):
                    raise AssertionError("upload was not released")
            return real_begin(conn)

        before_calls = self.fixture.call_count()
        upload_client = self.fixture.access.client_with_permissions(
            WRITE_PERMISSIONS
        )
        with mock.patch.object(
            transactions, "begin_immediate", side_effect=pause_first_begin
        ), mock.patch.object(
            calls_route,
            "begin_immediate",
            side_effect=pause_first_begin,
            create=True,
        ):
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(
                    self.fixture.upload_call,
                    client=upload_client,
                    linked_communication_id="comm-race-delete",
                )
                self.assertTrue(reached.wait(timeout=5))
                deleted = self.fixture.client.request(
                    "DELETE",
                    "/api/communications/comm-race-delete",
                    json={"expected_revision": 1},
                )
                release.set()
                uploaded = future.result(timeout=10)

        self.assertEqual(deleted.status_code, 200, deleted.text)
        self.assertIn(uploaded.status_code, (404, 409), uploaded.text)
        self.assertIsNone(
            self.fixture.row(
                "communications", "communication_id", "comm-race-delete"
            )
        )
        self.assertEqual(self.fixture.call_count(), before_calls)
        self.assertEqual(self.fixture.call_files(), [])

        created = self.fixture.client.post(
            "/api/patients/patient-active/communications",
            json={"channel": "phone", "direction": "outbound"},
        )
        self.assertEqual(created.status_code, 200, created.text)
        communication_id = created.json()["communication_id"]
        reached.clear()
        release.clear()
        first_lock = True
        delete_client = self.fixture.access.client_with_permissions(
            WRITE_PERMISSIONS
        )
        with mock.patch.object(
            transactions, "begin_immediate", side_effect=pause_first_begin
        ), mock.patch.object(
            calls_route,
            "begin_immediate",
            side_effect=pause_first_begin,
            create=True,
        ):
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(
                    delete_client.request,
                    "DELETE",
                    f"/api/communications/{communication_id}",
                    json={"expected_revision": 1},
                )
                self.assertTrue(reached.wait(timeout=5))
                uploaded = self.fixture.upload_call(
                    linked_communication_id=communication_id
                )
                release.set()
                rejected_delete = future.result(timeout=10)
        self.assertEqual(uploaded.status_code, 200, uploaded.text)
        self.assertEqual(rejected_delete.status_code, 409)
        self.assertEqual(
            rejected_delete.json(),
            {"detail": "communication_stale"},
        )

    def test_call_upload_and_parent_direction_change_are_serialized(self):
        from local_app import communication_transactions as transactions
        from local_app.routes import calls as calls_route

        reached = threading.Event()
        release = threading.Event()
        first_lock = True
        first_lock_guard = threading.Lock()
        real_begin = transactions.begin_immediate

        def pause_first_begin(conn):
            nonlocal first_lock
            with first_lock_guard:
                pause = first_lock
                first_lock = False
            if pause:
                reached.set()
                if not release.wait(timeout=5):
                    raise AssertionError("upload was not released")
            return real_begin(conn)

        before_calls = self.fixture.call_count()
        upload_client = self.fixture.access.client_with_permissions(
            WRITE_PERMISSIONS
        )
        with mock.patch.object(
            transactions, "begin_immediate", side_effect=pause_first_begin
        ), mock.patch.object(
            calls_route,
            "begin_immediate",
            side_effect=pause_first_begin,
            create=True,
        ):
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(
                    self.fixture.upload_call,
                    client=upload_client,
                    linked_communication_id="comm-race-direction",
                )
                self.assertTrue(reached.wait(timeout=5))
                changed = self.fixture.client.patch(
                    "/api/communications/comm-race-direction",
                    json={"expected_revision": 1, "direction": "inbound"},
                )
                release.set()
                uploaded = future.result(timeout=10)

        self.assertEqual(changed.status_code, 200, changed.text)
        self.assertEqual(changed.json()["communication"]["direction"], "inbound")
        self.assertEqual(uploaded.status_code, 409, uploaded.text)
        self.assertEqual(self.fixture.call_count(), before_calls)
        self.assertEqual(self.fixture.call_files(), [])

    def test_call_upload_rejects_opposite_or_undefined_parent_direction(self):
        before_calls = self.fixture.call_count()
        opposite = self.fixture.upload_call(
            direction="inbound", linked_communication_id="comm-edit"
        )
        self.assertEqual(opposite.status_code, 409, opposite.text)

        conn = sqlite3.connect(self.fixture.db_path)
        try:
            conn.execute(
                "insert into communications(communication_id, patient_identity, "
                "channel, direction, revision) values "
                "('comm-legacy-empty', 'patient-active', 'phone', '', 1)"
            )
            conn.execute(
                "insert into calls(call_id, patient_identity, direction, source, "
                "linked_communication_id, revision, created_at) values "
                "('call-legacy-direction', 'patient-active', 'outbound', "
                "'manual_import', 'comm-legacy-empty', 1, '2026-07-20 09:00:00')"
            )
            conn.commit()
        finally:
            conn.close()

        undefined = self.fixture.upload_call(
            direction="outbound", linked_communication_id="comm-legacy-empty"
        )
        self.assertEqual(undefined.status_code, 409, undefined.text)
        self.assertEqual(
            undefined.json(), {"detail": "call_parent_direction_required"}
        )
        self.assertEqual(self.fixture.call_count(), before_calls + 1)
        self.assertEqual(self.fixture.call_files(), [])

        repaired = self.fixture.client.patch(
            "/api/communications/comm-legacy-empty",
            json={"expected_revision": 1, "direction": "outbound"},
        )
        self.assertEqual(repaired.status_code, 200, repaired.text)
        edited = self.fixture.client.patch(
            "/api/calls/call-legacy-direction",
            json={"expected_revision": 1, "note": "recovered"},
        )
        self.assertEqual(edited.status_code, 200, edited.text)
        deleted = self.fixture.client.request(
            "DELETE",
            "/api/calls/call-legacy-direction",
            json={"expected_revision": 2},
        )
        self.assertEqual(deleted.status_code, 200, deleted.text)

    def test_call_upload_rechecks_all_parents_and_active_patient_under_lock(self):
        cases = (
            (
                "deleted return visit",
                {"linked_return_visit_id": "rv-race"},
                "insert into return_visits(return_visit_id, patient_identity, "
                "is_deleted, revision) values "
                "('rv-race', 'patient-active', 0, 1)",
                "update return_visits set is_deleted = 1 "
                "where return_visit_id = 'rv-race'",
                404,
            ),
            (
                "deleted patient",
                {},
                "select 1",
                "update patients set is_deleted = 1 "
                "where patient_identity = 'patient-active'",
                404,
            ),
        )
        for label, form, setup_sql, mutation_sql, expected in cases:
            with self.subTest(label=label):
                fixture = CommunicationCallTransactionFixture()
                try:
                    conn = sqlite3.connect(fixture.db_path)
                    try:
                        conn.execute(setup_sql)
                        conn.commit()
                    finally:
                        conn.close()

                    def mutate():
                        conn = sqlite3.connect(fixture.db_path)
                        try:
                            conn.execute(mutation_sql)
                            conn.commit()
                        finally:
                            conn.close()

                    before_calls = fixture.call_count()
                    response, mutated = fixture.upload_with_mutation_before_begin(
                        mutate, **form
                    )
                    self.assertTrue(mutated)
                    self.assertEqual(response.status_code, expected, response.text)
                    self.assertEqual(fixture.call_count(), before_calls)
                    self.assertEqual(fixture.call_files(), [])
                finally:
                    fixture.close()

    def test_call_upload_rechecks_locked_actor_and_compensates_file(self):
        mutations = (
            (
                "permission",
                "delete from role_permissions where role_key = ? "
                "and perm_key = 'call_record.manage'",
                (self.fixture.access.ROLE_KEY,),
            ),
            (
                "account",
                "update users set is_active = 0 where id = ?",
                (self.fixture.access.USER_ID,),
            ),
            (
                "employment",
                "update staff_members set employment_status = 'left' "
                "where staff_id = ?",
                (self.fixture.access.STAFF_ID,),
            ),
        )
        for label, sql, params in mutations:
            with self.subTest(label=label):
                fixture = CommunicationCallTransactionFixture()
                try:
                    def mutate():
                        conn = sqlite3.connect(fixture.db_path)
                        try:
                            conn.execute(sql, params)
                            conn.commit()
                        finally:
                            conn.close()

                    before_calls = fixture.call_count()
                    before_audits = len(fixture.audits("call"))
                    response, mutated = fixture.upload_with_mutation_before_begin(
                        mutate
                    )
                    self.assertTrue(mutated)
                    self.assertEqual(response.status_code, 403, response.text)
                    self.assertEqual(fixture.call_count(), before_calls)
                    self.assertEqual(len(fixture.audits("call")), before_audits)
                    self.assertEqual(fixture.call_files(), [])
                finally:
                    fixture.close()

        successful = self.fixture.upload_call()
        self.assertEqual(successful.status_code, 200, successful.text)
        audit = self.fixture.audits("call", successful.json()["call_id"])[0]
        self.assertEqual(audit["actor_user_id"], self.fixture.access.USER_ID)

    def test_call_upload_audit_failure_rolls_back_row_and_removes_file(self):
        conn = sqlite3.connect(self.fixture.db_path)
        try:
            conn.execute(
                "create trigger fail_call_upload_audit before insert on audit_logs "
                "begin select raise(abort, 'synthetic audit failure'); end"
            )
            conn.commit()
        finally:
            conn.close()
        before_calls = self.fixture.call_count()
        before_audits = len(self.fixture.audits("call"))
        response = self.fixture.upload_call(client=self.fixture.safe_client)
        self.assertEqual(response.status_code, 500, response.text)
        self.assertEqual(response.json(), {"detail": "call_write_failed"})
        self.assertEqual(self.fixture.call_count(), before_calls)
        self.assertEqual(len(self.fixture.audits("call")), before_audits)
        self.assertEqual(self.fixture.call_files(), [])

    def test_each_write_rechecks_revocation_in_the_write_lock(self):
        cases = (
            ("POST", "/api/patients/patient-active/communications", "communication.manage", {"channel": "phone", "direction": "outbound", "content": "Denied"}),
            ("PATCH", "/api/communications/comm-edit", "communication.manage", {"expected_revision": 1, "content": "Denied"}),
            ("DELETE", "/api/communications/comm-delete", "communication.delete", {"expected_revision": 1}),
            ("PATCH", "/api/calls/call-edit", "call_record.manage", {"expected_revision": 1, "note": "Denied"}),
            ("DELETE", "/api/calls/call-delete", "call_record.delete", {"expected_revision": 1}),
        )
        for method, path, permission, payload in cases:
            with self.subTest(path=path):
                self.fixture.restore_actor()
                client = self.fixture.access.client_with_permissions(WRITE_PERMISSIONS)
                before = self.fixture.digest()

                def revoke(permission=permission):
                    self.fixture.access.revoke(permission)

                response, callback_hit = self.fixture.request_with_mutation_before_begin(
                    client, method, path, revoke, json=payload
                )
                self.assertTrue(callback_hit)
                self.assertEqual(response.status_code, 403)
                self.assertEqual(response.json(), {"detail": "access_denied"})
                self.assertEqual(self.fixture.digest(), before)

    def test_each_write_accepts_its_exact_access_v3_permission(self):
        cases = (
            (
                "POST",
                "/api/patients/patient-active/communications",
                "communication.manage",
                {"channel": "phone", "direction": "outbound", "content": "Allowed"},
            ),
            (
                "PATCH",
                "/api/communications/comm-edit",
                "communication.manage",
                {"expected_revision": 1, "content": "Allowed"},
            ),
            (
                "DELETE",
                "/api/communications/comm-delete",
                "communication.delete",
                {"expected_revision": 1},
            ),
            (
                "PATCH",
                "/api/calls/call-edit",
                "call_record.manage",
                {"expected_revision": 1, "note": "Allowed"},
            ),
            (
                "DELETE",
                "/api/calls/call-delete",
                "call_record.delete",
                {"expected_revision": 1},
            ),
        )
        for method, path, permission, payload in cases:
            with self.subTest(path=path, permission=permission):
                self.fixture.restore_actor()
                client = self.fixture.access.client_with_permissions((permission,))
                response = client.request(method, path, json=payload)
                self.assertEqual(response.status_code, 200, response.text)

    def test_each_write_rechecks_inactive_and_left_actor(self):
        actions = (
            ("POST", "/api/patients/patient-active/communications", {"channel": "phone", "direction": "outbound", "content": "Denied"}),
            ("PATCH", "/api/communications/comm-edit", {"expected_revision": 1, "content": "Denied"}),
            ("DELETE", "/api/communications/comm-delete", {"expected_revision": 1}),
            ("PATCH", "/api/calls/call-edit", {"expected_revision": 1, "note": "Denied"}),
            ("DELETE", "/api/calls/call-delete", {"expected_revision": 1}),
        )
        mutations = (
            ("update users set is_active = 0 where id = ?", self.fixture.access.USER_ID),
            ("update staff_members set employment_status = 'left' where staff_id = ?", self.fixture.access.STAFF_ID),
        )
        for method, path, payload in actions:
            for statement, value in mutations:
                with self.subTest(path=path, statement=statement):
                    self.fixture.restore_actor()
                    client = self.fixture.access.client_with_permissions(WRITE_PERMISSIONS)
                    before = self.fixture.digest()

                    def mutate(statement=statement, value=value):
                        conn = sqlite3.connect(self.fixture.db_path)
                        try:
                            conn.execute(statement, (value,))
                            conn.commit()
                        finally:
                            conn.close()

                    response, callback_hit = self.fixture.request_with_mutation_before_begin(
                        client, method, path, mutate, json=payload
                    )
                    self.assertTrue(callback_hit)
                    self.assertEqual(response.status_code, 403)
                    self.assertEqual(self.fixture.digest(), before)

    def test_audit_failure_rolls_back_each_business_write_and_revision(self):
        actions = (
            ("POST", "/api/patients/patient-active/communications", {"channel": "phone", "direction": "outbound", "content": "Rollback"}),
            ("PATCH", "/api/communications/comm-edit", {"expected_revision": 1, "content": "Rollback"}),
            ("DELETE", "/api/communications/comm-delete", {"expected_revision": 1}),
            ("PATCH", "/api/calls/call-edit", {"expected_revision": 1, "note": "Rollback"}),
            ("DELETE", "/api/calls/call-delete", {"expected_revision": 1}),
        )
        for method, path, payload in actions:
            with self.subTest(path=path):
                conn = sqlite3.connect(self.fixture.db_path)
                try:
                    conn.execute(
                        "create trigger fail_customer_hub_audit before insert on audit_logs "
                        "begin select raise(abort, 'synthetic failure'); end"
                    )
                    conn.commit()
                finally:
                    conn.close()
                before = self.fixture.digest()
                response = self.fixture.safe_client.request(method, path, json=payload)
                conn = sqlite3.connect(self.fixture.db_path)
                try:
                    conn.execute("drop trigger fail_customer_hub_audit")
                    conn.commit()
                finally:
                    conn.close()
                self.assertEqual(response.status_code, 500)
                self.assertEqual(self.fixture.digest(), before)

    def test_text_fields_reject_non_string_null_and_invalid_utf8(self):
        cases = (
            ("POST", "/api/patients/patient-active/communications", {"channel": "phone", "direction": "outbound", "content": True}),
            ("PATCH", "/api/communications/comm-edit", {"expected_revision": 1, "content": None}),
            ("PATCH", "/api/calls/call-edit", {"expected_revision": 1, "note": []}),
        )
        for method, path, payload in cases:
            with self.subTest(path=path):
                before = self.fixture.digest()
                response = self.fixture.client.request(method, path, json=payload)
                self.assertEqual(response.status_code, 400)
                self.assertEqual(self.fixture.digest(), before)

        raw_cases = (
            (
                "POST",
                "/api/patients/patient-active/communications",
                b'{"channel":"phone","direction":"outbound","content":"\\ud800"}',
            ),
            (
                "PATCH",
                "/api/communications/comm-edit",
                b'{"expected_revision":1,"content":"\\ud800"}',
            ),
            (
                "PATCH",
                "/api/calls/call-edit",
                b'{"expected_revision":1,"note":"\\ud800"}',
            ),
        )
        for method, path, content in raw_cases:
            with self.subTest(raw=path):
                before = self.fixture.digest()
                response = self.fixture.safe_client.request(
                    method,
                    path,
                    content=content,
                    headers={"content-type": "application/json"},
                )
                self.assertEqual(response.status_code, 400)
                self.assertEqual(self.fixture.digest(), before)

    def test_unknown_fields_and_explicit_null_are_rejected(self):
        cases = (
            ("POST", "/api/patients/patient-active/communications", {"channel": "phone", "direction": "outbound", "content": "x", "unknown": "x"}),
            ("PATCH", "/api/communications/comm-edit", {"expected_revision": 1, "operator": "forged"}),
            ("PATCH", "/api/communications/comm-edit", {"expected_revision": 1, "content": None}),
            ("PATCH", "/api/calls/call-edit", {"expected_revision": 1, "unknown": "x"}),
        )
        for method, path, payload in cases:
            with self.subTest(path=path, payload=payload):
                before = self.fixture.digest()
                response = self.fixture.client.request(method, path, json=payload)
                self.assertEqual(response.status_code, 400)
                self.assertEqual(self.fixture.digest(), before)

    def test_missing_array_delete_bodies_enter_handler_after_actor_lock(self):
        for path in (
            "/api/communications/comm-delete",
            "/api/calls/call-delete",
        ):
            for kwargs in ({}, {"json": []}):
                with self.subTest(path=path, kwargs=kwargs):
                    hit = False

                    def mark():
                        nonlocal hit
                        hit = True

                    before = self.fixture.digest()
                    response, callback_hit = self.fixture.request_with_mutation_before_begin(
                        self.fixture.client, "DELETE", path, mark, **kwargs
                    )
                    self.assertTrue(callback_hit)
                    self.assertTrue(hit)
                    self.assertEqual(response.status_code, 400)
                    self.assertEqual(response.json(), {"detail": "invalid_request_body"})
                    self.assertEqual(self.fixture.digest(), before)

    def test_deleted_patient_and_cross_patient_parent_are_rejected(self):
        cases = (
            ("PATCH", "/api/communications/comm-deleted-patient", {"expected_revision": 1, "content": "No"}),
            ("DELETE", "/api/communications/comm-deleted-patient", {"expected_revision": 1}),
            ("PATCH", "/api/calls/call-deleted-patient", {"expected_revision": 1, "note": "No"}),
            ("DELETE", "/api/calls/call-deleted-patient", {"expected_revision": 1}),
        )
        for method, path, payload in cases:
            with self.subTest(path=path):
                before = self.fixture.digest()
                response = self.fixture.client.request(method, path, json=payload)
                self.assertEqual(response.status_code, 404)
                self.assertEqual(self.fixture.digest(), before)

        conn = sqlite3.connect(self.fixture.db_path)
        try:
            conn.execute(
                "update calls set linked_communication_id = 'comm-other' "
                "where call_id = 'call-edit'"
            )
            conn.commit()
        finally:
            conn.close()
        before = self.fixture.digest()
        response = self.fixture.client.patch(
            "/api/calls/call-edit",
            json={"expected_revision": 1, "note": "No"},
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.fixture.digest(), before)

        before = self.fixture.digest()
        response = self.fixture.client.request(
            "DELETE",
            "/api/communications/comm-other",
            json={"expected_revision": 1},
        )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(
            response.json(), {"detail": "communication_has_linked_calls"}
        )
        self.assertEqual(self.fixture.digest(), before)

        conn = sqlite3.connect(self.fixture.db_path)
        try:
            conn.execute(
                "insert into return_visits(return_visit_id, patient_identity, "
                "is_deleted, revision) values ('rv-deleted-parent', "
                "'patient-active', 1, 1)"
            )
            conn.execute(
                "update calls set linked_communication_id = '', "
                "linked_return_visit_id = 'rv-deleted-parent' "
                "where call_id = 'call-edit'"
            )
            conn.commit()
        finally:
            conn.close()
        before = self.fixture.digest()
        response = self.fixture.client.patch(
            "/api/calls/call-edit",
            json={"expected_revision": 1, "note": "No"},
        )
        self.assertEqual(response.status_code, 404)
        self.assertEqual(self.fixture.digest(), before)

    def test_two_clients_compete_on_same_revision_without_sleep(self):
        from local_app import communication_transactions as transactions

        clients = (
            self.fixture.access.client_with_permissions(WRITE_PERMISSIONS),
            self.fixture.access.client_with_permissions(WRITE_PERMISSIONS),
        )
        barrier = threading.Barrier(2)
        real_begin = transactions.begin_immediate

        def synchronized_begin(conn):
            barrier.wait(timeout=5)
            return real_begin(conn)

        def update(client, content):
            return client.patch(
                "/api/communications/comm-edit",
                json={"expected_revision": 1, "content": content},
            )

        with mock.patch.object(
            transactions, "begin_immediate", side_effect=synchronized_begin
        ):
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                futures = (
                    pool.submit(update, clients[0], "Concurrent A"),
                    pool.submit(update, clients[1], "Concurrent B"),
                )
                responses = [future.result(timeout=10) for future in futures]
        self.assertEqual(sorted(response.status_code for response in responses), [200, 409])
        self.assertEqual(
            self.fixture.row("communications", "communication_id", "comm-edit")["revision"],
            2,
        )
        self.assertEqual(len(self.fixture.audits("communication", "comm-edit")), 1)

    def test_all_five_write_routes_require_an_authenticated_session(self):
        anonymous = TestClient(self.fixture.access.app)
        cases = (
            (
                "POST",
                "/api/patients/patient-active/communications",
                {"channel": "phone", "direction": "outbound"},
            ),
            (
                "PATCH",
                "/api/communications/comm-edit",
                {"expected_revision": 1, "content": "Denied"},
            ),
            (
                "DELETE",
                "/api/communications/comm-delete",
                {"expected_revision": 1},
            ),
            (
                "PATCH",
                "/api/calls/call-edit",
                {"expected_revision": 1, "note": "Denied"},
            ),
            (
                "DELETE",
                "/api/calls/call-delete",
                {"expected_revision": 1},
            ),
        )
        try:
            for method, path, payload in cases:
                with self.subTest(path=path):
                    response = anonymous.request(method, path, json=payload)
                    self.assertEqual(response.status_code, 401)
        finally:
            anonymous.close()

    def test_delete_revision_update_and_delete_failures_roll_back(self):
        cases = (
            (
                "communications",
                "update",
                "/api/communications/comm-delete",
            ),
            (
                "communications",
                "delete",
                "/api/communications/comm-delete",
            ),
            ("calls", "update", "/api/calls/call-delete"),
            ("calls", "delete", "/api/calls/call-delete"),
        )
        for table, operation, path in cases:
            with self.subTest(table=table, operation=operation):
                fixture = CommunicationCallTransactionFixture()
                trigger = f"fail_{table}_{operation}"
                try:
                    conn = sqlite3.connect(fixture.db_path)
                    try:
                        conn.execute(
                            f"create trigger {trigger} before {operation} on {table} "
                            "begin select raise(abort, 'synthetic failure'); end"
                        )
                        conn.commit()
                    finally:
                        conn.close()
                    before = fixture.digest()
                    response = fixture.safe_client.request(
                        "DELETE", path, json={"expected_revision": 1}
                    )
                    conn = sqlite3.connect(fixture.db_path)
                    try:
                        conn.execute(f"drop trigger {trigger}")
                        conn.commit()
                    finally:
                        conn.close()
                    self.assertEqual(response.status_code, 500)
                    self.assertEqual(fixture.digest(), before)
                finally:
                    fixture.close()

    def test_transaction_connections_close_on_success_and_failure(self):
        from local_app import call_transactions, communication_transactions

        real_connect = communication_transactions.connect

        class TrackingConnection:
            def __init__(self, wrapped):
                object.__setattr__(self, "wrapped", wrapped)
                object.__setattr__(self, "closed", False)

            def __getattr__(self, name):
                return getattr(self.wrapped, name)

            def __setattr__(self, name, value):
                if name in ("wrapped", "closed"):
                    object.__setattr__(self, name, value)
                else:
                    setattr(self.wrapped, name, value)

            def close(self):
                self.wrapped.close()
                object.__setattr__(self, "closed", True)

        tracked = []

        def tracked_connect(db_path):
            wrapped = TrackingConnection(real_connect(db_path))
            tracked.append(wrapped)
            return wrapped

        success_cases = (
            (
                "POST",
                "/api/patients/patient-active/communications",
                {"channel": "phone", "direction": "outbound"},
            ),
            (
                "PATCH",
                "/api/communications/comm-edit",
                {"expected_revision": 1, "content": "Closed success"},
            ),
            (
                "DELETE",
                "/api/communications/comm-delete",
                {"expected_revision": 1},
            ),
            (
                "PATCH",
                "/api/calls/call-edit",
                {"expected_revision": 1, "note": "Closed success"},
            ),
            (
                "DELETE",
                "/api/calls/call-delete",
                {"expected_revision": 1},
            ),
        )
        with mock.patch.object(
            communication_transactions, "connect", side_effect=tracked_connect
        ), mock.patch.object(
            call_transactions, "connect", side_effect=tracked_connect
        ):
            for method, path, payload in success_cases:
                with self.subTest(path=path):
                    success = self.fixture.client.request(
                        method, path, json=payload
                    )
                    self.assertEqual(success.status_code, 200, success.text)
                    self.assertTrue(tracked[-1].closed)
            conn = sqlite3.connect(self.fixture.db_path)
            try:
                conn.execute(
                    "create trigger fail_customer_hub_audit before insert on audit_logs "
                    "begin select raise(abort, 'synthetic failure'); end"
                )
                conn.commit()
            finally:
                conn.close()
            failure = self.fixture.safe_client.patch(
                "/api/calls/call-direction-lock",
                json={"expected_revision": 1, "note": "Closed failure"},
            )
            conn = sqlite3.connect(self.fixture.db_path)
            try:
                conn.execute("drop trigger fail_customer_hub_audit")
                conn.commit()
            finally:
                conn.close()
            self.assertEqual(failure.status_code, 500)
            self.assertTrue(tracked[-1].closed)
        self.assertEqual(len(tracked), 6)
        self.assertTrue(all(connection.closed for connection in tracked))


if __name__ == "__main__":
    unittest.main()
