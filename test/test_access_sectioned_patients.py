"""SECTIONED 患者入口：区块权限必须在查库前分流。"""

import sqlite3
import tempfile
import unittest
import inspect
from pathlib import Path
from unittest.mock import patch

from local_app.routes import customer_hub, patients


DAY = "2026-07-10"


def _endpoint(router, path):
    return next(route.endpoint for route in router.routes if route.path == path)


def _database(tmp, statements):
    path = Path(tmp) / "sectioned.sqlite3"
    with sqlite3.connect(path) as conn:
        conn.executescript(statements)
    return path


class CustomerHubSectionedTest(unittest.TestCase):
    def test_no_section_permission_means_no_section_query_or_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = _database(tmp, "create table sentinel(value text);")
            required = []
            checked = []

            with (
                patch.object(customer_hub, "require_access", side_effect=required.append),
                patch.object(
                    customer_hub,
                    "has_access",
                    side_effect=lambda permission: checked.append(permission) or False,
                ),
            ):
                result = _endpoint(
                    customer_hub.create_customer_hub_router(db),
                    "/api/customer-hub/today",
                )(date=DAY)

            self.assertEqual(required, ["patient.profile.view"])
            self.assertEqual(
                checked,
                ["return_visit.view", "communication.view", "call_record.view"],
            )
            self.assertEqual(result, {"date": DAY})

    def test_each_granted_section_uses_its_own_permission(self):
        cases = (
            (
                "return_visit.view",
                "return_visits",
                """
                create table patients(
                    patient_identity text, display_name text, phone text,
                    responsible_doctor text, is_deleted integer
                );
                create table return_visits(
                    return_visit_id text, patient_identity text, due_time text,
                    actual_date text, item_name text, status text, note text,
                    return_doctor text, visitor text, return_type text,
                    return_result text, channel text, revision integer, is_deleted integer
                );
                insert into patients values ('p1', '张三', '13800000000', '主治医生', 0);
                insert into return_visits values
                    ('rv1', 'p1', '2026-07-10 09:00', null, '复查', '待回访', '',
                     '王医生', '小李', '', '', 'phone', 1, 0);
                """,
            ),
            (
                "communication.view",
                "communications",
                """
                create table patients(patient_identity text, display_name text, is_deleted integer);
                create table communications(
                    communication_id text, patient_identity text, channel text,
                    direction text, reason text,
                    contacted_at text, operator text, content text,
                    deal_status text, revision integer, created_at text
                );
                insert into patients values ('p1', '张三', 0);
                insert into communications values
                    ('c1', 'p1', 'wechat', '', '咨询', '2026-07-10 10:00',
                     '小李', '询价', '', 1, '2026-07-10 10:00');
                """,
            ),
            (
                "call_record.view",
                "calls",
                """
                create table patients(patient_identity text, display_name text, is_deleted integer);
                create table calls(
                    call_id text, patient_identity text, direction text,
                    peer_norm text, started_at text, duration_sec integer,
                    deal_status text, created_at text, revision integer
                );
                create table attachment_file_ops(
                    parent_type text, parent_id text, attachment_id text, status text
                );
                insert into patients values ('p1', '张三', 0);
                insert into calls values
                    ('k1', 'p1', 'outbound', '13800000000', '2026-07-10 11:00',
                     30, '', '2026-07-10 11:00', 7);
                """,
            ),
        )
        all_keys = {"return_visits", "communications", "calls"}

        for permission, expected_key, schema in cases:
            with self.subTest(permission=permission), tempfile.TemporaryDirectory() as tmp:
                db = _database(tmp, schema)
                with (
                    patch.object(customer_hub, "require_access"),
                    patch.object(
                        customer_hub,
                        "has_access",
                        side_effect=lambda key, granted=permission: key == granted,
                    ),
                ):
                    result = _endpoint(
                        customer_hub.create_customer_hub_router(db),
                        "/api/customer-hub/today",
                    )(date=DAY)

                self.assertIn(expected_key, result)
                self.assertTrue(all(key not in result for key in all_keys - {expected_key}))
                if expected_key == "calls":
                    self.assertEqual(result["calls"][0]["revision"], 7)


class PatientsSectionedTest(unittest.TestCase):
    @staticmethod
    def _detail_schema(include_finance=False):
        schema = """
            create table patients(patient_identity text, display_name text);
            create table medical_records(
                patient_identity text, draft_status text, visit_time text,
                updated_at text, created_at text
            );
            create table appointments(
                patient_identity text, source_json text, start_time text, updated_at text
            );
            create table return_visits(
                patient_identity text, is_deleted integer, due_time text, updated_at text
            );
            create table local_notes(patient_identity text, updated_at text);
            insert into patients values ('p1', '张三');
        """
        if not include_finance:
            return schema
        return schema + """
            create table bills(
                bill_id text, patient_identity text, source_json text, state text,
                total_fee real, bill_time text, updated_at text
            );
            create table treatment_items(
                patient_identity text, item_name text, quantity real, unit_price real,
                total_fee real, fee_type text, doctor_name text, bill_id text,
                is_deleted integer
            );
            create table payments(
                payment_id text, patient_identity text, source_json text,
                pay_time text, updated_at text
            );
            insert into bills values ('b1', 'p1', '{}', 'open', 100, '2026-07-10', '2026-07-10');
            insert into treatment_items values ('p1', '洁牙', 1, 100, 100, '', '王医生', 'b1', 0);
            insert into payments values ('pay1', 'p1', '{}', '2026-07-10', '2026-07-10');
        """

    def test_list_uses_new_patient_profile_entry_permission(self):
        calls = []

        class FakeResult:
            def __init__(self, value):
                self.value = value

            def fetchone(self):
                return (self.value,)

            def fetchall(self):
                return []

        class FakeConnection:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def execute(self, sql, _params=()):
                return FakeResult(0 if "count(*)" in sql else None)

        with (
            patch.object(patients, "require_access", side_effect=calls.append),
            patch.object(patients, "connect", return_value=FakeConnection()),
        ):
            result = _endpoint(
                patients.create_patients_router("unused.sqlite3"), "/api/patients"
            )()

        self.assertEqual(calls, ["patient.profile.view"])
        self.assertEqual(result["list"], [])

    def test_detail_without_billing_permission_never_queries_finance(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = _database(tmp, self._detail_schema())
            required = []
            checked = []
            with (
                patch.object(patients, "require_access", side_effect=required.append),
                patch.object(
                    patients,
                    "has_access",
                    side_effect=lambda permission: checked.append(permission) or False,
                ),
            ):
                result = _endpoint(
                    patients.create_patients_router(db),
                    "/api/patients/{patient_identity}",
                )("p1")

            self.assertEqual(required, ["patient.profile.view"])
            self.assertEqual(checked, ["billing.view"])
            self.assertEqual(result["patient"]["display_name"], "张三")
            for key in ("bills", "treatment_items", "payments"):
                self.assertNotIn(key, result)

    def test_detail_with_billing_permission_queries_and_returns_finance(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = _database(tmp, self._detail_schema(include_finance=True))
            with (
                patch.object(patients, "require_access"),
                patch.object(patients, "has_access", return_value=True),
            ):
                result = _endpoint(
                    patients.create_patients_router(db),
                    "/api/patients/{patient_identity}",
                )("p1")

            self.assertEqual(len(result["bills"]), 1)
            self.assertEqual(len(result["treatment_items"]), 1)
            self.assertEqual(len(result["payments"]), 1)

    def test_sectioned_endpoints_do_not_call_legacy_permission_helpers(self):
        targets = (
            _endpoint(
                customer_hub.create_customer_hub_router("unused.sqlite3"),
                "/api/customer-hub/today",
            ),
            _endpoint(patients.create_patients_router("unused.sqlite3"), "/api/patients"),
            _endpoint(
                patients.create_patients_router("unused.sqlite3"),
                "/api/patients/{patient_identity}",
            ),
        )
        for target in targets:
            source = inspect.getsource(target)
            self.assertNotIn("require_perm(", source)
            self.assertNotIn("has_perm(", source)


if __name__ == "__main__":
    unittest.main()
