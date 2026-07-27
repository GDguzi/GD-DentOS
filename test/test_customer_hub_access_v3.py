"""Shared locked authorization and customer-hub version boundaries."""
import sqlite3
import unittest
from pathlib import Path

from fastapi import HTTPException

from local_app.access_authorization import AccessPrincipal
from local_app import api as api_module
from local_app.db import begin_immediate, connect
from customer_hub_access_fixture import CustomerHubAccessFixture


class CustomerHubLockedActorTest(unittest.TestCase):
    def setUp(self):
        self.fixture = CustomerHubAccessFixture()

    def tearDown(self):
        self.fixture.close()

    def test_fixture_binds_images_to_its_temporary_directory(self):
        temp_root = Path(self.fixture.temp_dir.name).resolve()
        images_dir = self.fixture.images_dir.resolve()
        default_images = (api_module.APP_DIR / "data" / "images").resolve()
        self.assertTrue(images_dir.is_relative_to(temp_root))
        self.assertEqual(images_dir, temp_root / "images")
        self.assertNotEqual(images_dir, default_images)

    def test_locked_guard_reloads_current_access_principal_after_revocation(self):
        self.fixture.client_with_permissions(("return_visit.manage",))
        actor = self.fixture.principal()
        self.assertIsInstance(actor, AccessPrincipal)
        self.fixture.revoke("return_visit.manage")

        from local_app.customer_hub_security import require_locked_human_actor

        with connect(self.fixture.db_path) as conn:
            begin_immediate(conn)
            with self.assertRaises(HTTPException) as raised:
                require_locked_human_actor(
                    conn, actor, ("return_visit.manage",)
                )
        self.assertEqual(raised.exception.status_code, 403)
        self.assertEqual(raised.exception.detail, "access_denied")


class CustomerHubVersionAccessTest(unittest.TestCase):
    def setUp(self):
        self.fixture = CustomerHubAccessFixture()
        self._seed_entities()

    def tearDown(self):
        self.fixture.close()

    def _seed_entities(self):
        conn = sqlite3.connect(self.fixture.db_path)
        try:
            patients = (
                ("patient-active", 0),
                ("patient-deleted", 1),
                ("patient-medical-deleted", 1),
                ("patient-appointment-deleted", 1),
                ("patient-return-deleted", 1),
            )
            conn.executemany(
                "insert into patients"
                "(patient_identity, display_name, is_deleted, current_hash, source_json) "
                "values (?, '合成患者', ?, 'hash', '{}')",
                patients,
            )
            conn.execute(
                "insert into medical_records"
                "(record_id, patient_identity, current_hash) "
                "values ('record-active', 'patient-active', 'hash')"
            )
            conn.execute(
                "insert into medical_records"
                "(record_id, patient_identity, current_hash) "
                "values ('record-deleted-parent', 'patient-medical-deleted', 'hash')"
            )
            conn.execute(
                "insert into appointments(appointment_id, patient_identity) "
                "values ('appointment-active', 'patient-active')"
            )
            conn.execute(
                "insert into appointments(appointment_id, patient_identity) "
                "values ('appointment-deleted-parent', 'patient-appointment-deleted')"
            )
            conn.execute(
                "insert into return_visits(return_visit_id, patient_identity) "
                "values ('return-active', 'patient-active')"
            )
            conn.execute(
                "insert into return_visits(return_visit_id, patient_identity, is_deleted) "
                "values ('return-deleted', 'patient-active', 1)"
            )
            conn.execute(
                "insert into return_visits(return_visit_id, patient_identity) "
                "values ('return-deleted-parent', 'patient-return-deleted')"
            )
            conn.execute(
                "insert into patient_versions"
                "(patient_identity, version_hash, source_json, changed_at) "
                "values ('patient-active', 'p1', '{}', '2026-07-20 09:00:00')"
            )
            conn.execute(
                "insert into medical_record_versions"
                "(record_id, version_hash, snapshot_json, changed_at) "
                "values ('record-active', 'm1', '{}', '2026-07-20 09:00:00')"
            )
            conn.execute(
                "insert into appointment_versions"
                "(appointment_id, version_hash, snapshot_json, changed_at) "
                "values ('appointment-active', 'a1', '{}', '2026-07-20 09:00:00')"
            )
            conn.execute(
                "insert into return_visit_versions"
                "(return_visit_id, version_hash, snapshot_json, changed_at) "
                "values ('return-active', 'r1', '{}', '2026-07-20 09:00:00')"
            )
            conn.commit()
        finally:
            conn.close()

    def _get(self, permission, entity_type, entity_id, **params):
        client = self.fixture.client_with_permissions((permission,))
        query = {"entity_type": entity_type, "entity_id": entity_id, **params}
        return client.get("/api/versions", params=query)

    def test_versions_require_the_exact_entity_permission(self):
        cases = (
            ("patient.profile.view", "patient", "patient-active"),
            ("medical_record.view", "medical_record", "record-active"),
            ("appointment.view", "appointment", "appointment-active"),
            ("return_visit.view", "return_visit", "return-active"),
        )
        for permission, entity_type, entity_id in cases:
            with self.subTest(entity_type=entity_type):
                response = self._get(permission, entity_type, entity_id)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()["totalcount"], 1)

        wrong = self._get(
            "communication.view", "patient", "patient-active"
        )
        self.assertEqual(wrong.status_code, 403)
        self.assertEqual(wrong.json(), {"detail": "access_denied"})

    def test_versions_hide_deleted_patient_owners_and_deleted_return_visit(self):
        cases = (
            ("patient.profile.view", "patient", "patient-deleted"),
            (
                "medical_record.view",
                "medical_record",
                "record-deleted-parent",
            ),
            (
                "appointment.view",
                "appointment",
                "appointment-deleted-parent",
            ),
            (
                "return_visit.view",
                "return_visit",
                "return-deleted-parent",
            ),
            ("return_visit.view", "return_visit", "return-deleted"),
        )
        for permission, entity_type, entity_id in cases:
            with self.subTest(entity_type=entity_type, entity_id=entity_id):
                response = self._get(permission, entity_type, entity_id)
                self.assertEqual(response.status_code, 404)

    def test_return_visit_version_number_is_scoped_before_pagination(self):
        conn = sqlite3.connect(self.fixture.db_path)
        try:
            conn.execute(
                "insert into return_visits(return_visit_id, patient_identity) "
                "values ('return-b', 'patient-active')"
            )
            conn.execute("delete from return_visit_versions")
            conn.execute(
                "insert into return_visit_versions"
                "(return_visit_id, version_hash, snapshot_json, changed_at) "
                "values ('return-active', 'a1', '{}', '2026-07-20 09:00:00')"
            )
            conn.execute(
                "insert into return_visit_versions"
                "(return_visit_id, version_hash, snapshot_json, changed_at) "
                "values ('return-b', 'b1', '{}', '2026-07-20 10:00:00')"
            )
            conn.execute(
                "insert into return_visit_versions"
                "(return_visit_id, version_hash, snapshot_json, changed_at) "
                "values ('return-active', 'a2', '{}', '2026-07-20 11:00:00')"
            )
            conn.commit()
        finally:
            conn.close()

        client = self.fixture.client_with_permissions(("return_visit.view",))
        pages = []
        for pageno in (1, 2):
            response = client.get(
                "/api/versions",
                params={
                    "entity_type": "return_visit",
                    "entity_id": "return-active",
                    "pageno": pageno,
                    "pagesize": 1,
                },
            )
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["totalcount"], 2)
            pages.append(
                [item["version_number"] for item in response.json()["list"]]
            )
        self.assertEqual(pages, [[2], [1]])


if __name__ == "__main__":
    unittest.main()
