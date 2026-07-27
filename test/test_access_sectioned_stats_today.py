"""SECTIONED 统计与今日工作台：无权区块不查库也不返回占位键。"""

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException

from local_app.db import connect as db_connect, init_db
from local_app.routes import stats, today_work


DAY = "2026-07-10"


def _endpoint(router, path):
    return next(route.endpoint for route in router.routes if route.path == path)


def _database(tmp, statements):
    path = Path(tmp) / "sectioned.sqlite3"
    with sqlite3.connect(path) as conn:
        conn.executescript(statements)
    return path


class _RecordingConnection:
    def __init__(self, connection, statements):
        self._connection = connection
        self._statements = statements

    def __enter__(self):
        self._connection.__enter__()
        return self

    def __exit__(self, *args):
        return self._connection.__exit__(*args)

    def execute(self, sql, parameters=()):
        self._statements.append(" ".join(sql.lower().split()))
        return self._connection.execute(sql, parameters)


class StatsSectionedTest(unittest.TestCase):
    def test_no_partition_permission_rejects_before_connect(self):
        router = stats.create_stats_router(Path("unused.sqlite3"))

        with (
            patch.object(stats, "has_access", return_value=False, create=True),
            patch.object(
                stats,
                "require_access",
                side_effect=HTTPException(status_code=403, detail="access_denied"),
                create=True,
            ),
            patch.object(
                stats,
                "connect",
                side_effect=AssertionError("permission denial must not touch the database"),
            ),
            self.assertRaises(HTTPException) as raised,
        ):
            _endpoint(router, "/api/stats")()

        self.assertEqual(raised.exception.status_code, 403)

    def test_each_partition_queries_and_returns_only_its_own_data(self):
        cases = (
            (
                "patient.profile.view",
                """
                create table patients(id integer);
                create table medical_records(id integer);
                create table appointments(id integer);
                create table return_visits(id integer);
                create table local_notes(id integer);
                create table patient_versions(id integer);
                create table medical_record_versions(id integer);
                create table appointment_versions(id integer);
                create table return_visit_versions(id integer);
                insert into patients values (1);
                """,
                {"counts"},
                {
                    "patients",
                    "medical_records",
                    "appointments",
                    "return_visits",
                    "local_notes",
                    "patient_versions",
                    "medical_record_versions",
                    "appointment_versions",
                    "return_visit_versions",
                },
            ),
            (
                "billing.view",
                """
                create table bills(id integer);
                create table payments(id integer);
                create table treatment_items(id integer);
                insert into bills values (1);
                """,
                {"counts"},
                {"bills", "payments", "treatment_items"},
            ),
            (
                "report.finance.view",
                """
                create table bills(total_fee real, paid_fee real, state text);
                create table payments(amount real);
                insert into bills values (100, 40, 'open');
                insert into payments values (40);
                """,
                {"money"},
                set(),
            ),
            (
                "audit.view",
                """
                create table audit_logs(id integer);
                insert into audit_logs values (1);
                """,
                {"counts"},
                {"audit_logs"},
            ),
        )

        for permission, schema, expected_top_keys, expected_count_keys in cases:
            with self.subTest(permission=permission), tempfile.TemporaryDirectory() as tmp:
                db = _database(tmp, schema)
                with (
                    patch.object(
                        stats,
                        "has_access",
                        side_effect=lambda key, granted=permission: key == granted,
                        create=True,
                    ),
                    patch.object(stats, "require_access", create=True),
                ):
                    result = _endpoint(stats.create_stats_router(db), "/api/stats")()

                self.assertEqual(set(result), expected_top_keys)
                self.assertNotIn("latest_batches", result)
                if "counts" in result:
                    self.assertEqual(set(result["counts"]), expected_count_keys)
                if permission == "report.finance.view":
                    self.assertEqual(result["money"]["bill_total_fee"], 100)
                    self.assertEqual(result["money"]["bill_paid_fee"], 40)
                    self.assertEqual(result["money"]["payment_amount"], 40)


class TodayWorkSectionedTest(unittest.TestCase):
    def _db(self, tmp):
        path = Path(tmp) / "today.sqlite3"
        init_db(path)
        return path

    def _call(self, db, permissions, statements=None, *, date=DAY):
        statements = statements if statements is not None else []

        def recording_connect(path):
            return _RecordingConnection(db_connect(path), statements)

        with (
            patch.object(today_work, "require_access", create=True),
            patch.object(
                today_work,
                "has_access",
                side_effect=lambda permission: permission in permissions,
                create=True,
            ),
            patch.object(today_work, "today_str", return_value=DAY),
            patch.object(today_work, "connect", side_effect=recording_connect),
        ):
            result = _endpoint(
                today_work.create_today_work_router(db), "/api/today-work"
            )(date=date)
        return result, statements

    def test_patient_permission_is_required_before_connect(self):
        router = today_work.create_today_work_router(Path("unused.sqlite3"))
        with (
            patch.object(
                today_work,
                "require_access",
                side_effect=HTTPException(status_code=403, detail="access_denied"),
                create=True,
            ) as require_access,
            patch.object(today_work, "has_access", return_value=False, create=True),
            patch.object(
                today_work,
                "connect",
                side_effect=AssertionError("permission denial must not touch the database"),
            ),
            self.assertRaises(HTTPException) as raised,
        ):
            _endpoint(router, "/api/today-work")(date=DAY)

        self.assertEqual(raised.exception.status_code, 403)
        require_access.assert_called_once_with("patient.profile.view")

    def test_without_optional_permissions_skips_finance_and_audit_queries_and_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            result, statements = self._call(
                self._db(tmp), {"patient.profile.view"}
            )

        sql = "\n".join(statements)
        self.assertNotIn("bills", sql)
        self.assertNotIn("payments", sql)
        self.assertNotIn("audit_logs", sql)
        for key in (
            "today_receivable",
            "today_paid",
            "today_cash_in",
            "today_settlements",
            "unpaid_bills",
            "today_unpaid_amount",
            "recent_audits",
        ):
            self.assertNotIn(key, result["summary"])
        for key in (
            "unpaid_bills",
            "today_settlements",
            "today_pay_methods",
            "recent_audits",
        ):
            self.assertNotIn(key, result)

    def test_past_date_without_billing_does_not_query_finance_for_unlinked_visits(self):
        with tempfile.TemporaryDirectory() as tmp:
            result, statements = self._call(
                self._db(tmp),
                {"patient.profile.view"},
                date="2026-07-09",
            )

        sql = "\n".join(statements)
        self.assertNotIn("bills", sql)
        self.assertNotIn("payments", sql)
        self.assertEqual(result["unlinked_visits"], [])

    def test_billing_permission_enables_finance_queries_and_response(self):
        with tempfile.TemporaryDirectory() as tmp:
            result, statements = self._call(
                self._db(tmp), {"patient.profile.view", "billing.view"}
            )

        sql = "\n".join(statements)
        self.assertIn(" from bills", sql)
        self.assertIn(" from payments", sql)
        self.assertNotIn("audit_logs", sql)
        for key in (
            "today_receivable",
            "today_paid",
            "today_cash_in",
            "today_settlements",
            "unpaid_bills",
            "today_unpaid_amount",
        ):
            self.assertIn(key, result["summary"])
        for key in ("unpaid_bills", "today_settlements", "today_pay_methods"):
            self.assertIn(key, result)
        self.assertNotIn("recent_audits", result)

    def test_audit_permission_enables_only_audit_queries_and_response(self):
        with tempfile.TemporaryDirectory() as tmp:
            result, statements = self._call(
                self._db(tmp), {"patient.profile.view", "audit.view"}
            )

        sql = "\n".join(statements)
        self.assertIn("audit_logs", sql)
        self.assertNotIn("bills", sql)
        self.assertNotIn("payments", sql)
        self.assertIn("recent_audits", result["summary"])
        self.assertIn("recent_audits", result)
        self.assertNotIn("today_receivable", result["summary"])
        self.assertNotIn("unpaid_bills", result)

    def test_without_billing_omits_unpaid_amount_but_keeps_paid_today_boolean(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = self._db(tmp)
            with db_connect(db) as conn:
                conn.execute(
                    "insert into patients(patient_identity, display_name, current_hash) "
                    "values ('p1', '张三', 'h1')"
                )
                conn.execute(
                    "insert into appointments(appointment_id, patient_identity, start_time, status) "
                    "values ('a1', 'p1', ?, '0')",
                    (DAY + " 09:00:00",),
                )
                conn.execute(
                    "insert into payments(payment_id, patient_identity, pay_time, amount, state) "
                    "values ('pay1', 'p1', ?, 100, '0')",
                    (DAY + " 09:05:00",),
                )
                conn.commit()

            result, statements = self._call(db, {"patient.profile.view"})

        self.assertEqual(len(result["appointments"]), 1)
        appointment = result["appointments"][0]
        self.assertNotIn("unpaid_amount", appointment)
        self.assertTrue(appointment["paid_today"])
        self.assertFalse(any(" from bills" in sql for sql in statements))


if __name__ == "__main__":
    unittest.main()
