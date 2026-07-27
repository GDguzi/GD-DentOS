import json
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from local_app.db import connect as real_connect
from local_app.db import init_db
from local_app.routes import today_inspection


def _all_keys(value):
    if isinstance(value, dict):
        keys = set(value)
        for child in value.values():
            keys.update(_all_keys(child))
        return keys
    if isinstance(value, list):
        keys = set()
        for child in value:
            keys.update(_all_keys(child))
        return keys
    return set()


class SectionedTodayInspectionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = Path(self.tmp.name) / "clinic.sqlite3"
        init_db(self.db_path)
        with real_connect(self.db_path) as conn:
            conn.execute(
                "insert into patients(patient_identity, display_name, phone, updated_at, current_hash) "
                "values ('p1', '测试患者', '13800000000', '2026-07-16 08:00:00', 'h1')"
            )
            conn.execute(
                "insert into appointments(appointment_id, patient_identity, start_time, "
                "doctor_name, item_name, status, updated_at) "
                "values ('a1', 'p1', '2026-07-16 09:00:00', '医生A', '根管治疗', "
                "'已到诊', '2026-07-16 08:00:00')"
            )
            conn.execute(
                "insert into appointments(appointment_id, patient_identity, start_time, "
                "doctor_name, item_name, status, updated_at) "
                "values ('a2', 'p1', '2026-07-16 10:00:00', '医生A', '复诊', "
                "'0', '2026-07-16 08:00:00')"
            )
            conn.execute(
                "insert into appointments(appointment_id, patient_identity, start_time, "
                "doctor_name, item_name, status, updated_at) "
                "values ('a-cancelled', 'p1', '2026-07-16 11:00:00', '医生A', "
                "'复诊', '3', '2026-07-16 08:00:00')"
            )
            conn.execute(
                "insert into treatment_orders(order_id, order_no, patient_identity, "
                "order_date, doctor_name, diagnosis, status, bill_id, updated_at) "
                "values ('o1', 'CZ1', 'p1', '2026-07-16 09:10:00', '医生A', "
                "'龋齿', 'paid', 'b1', '2026-07-16 09:10:00')"
            )
            conn.execute(
                "insert into treatment_items(treatment_item_id, order_id, patient_identity, "
                "bill_id, item_name, unit_price, quantity, total_fee, updated_at) "
                "values ('ti1', 'o1', 'p1', 'b1', '根管治疗', 100, 1, 100, "
                "'2026-07-16 09:10:00')"
            )
            conn.execute(
                "insert into bills(bill_id, patient_identity, bill_no, bill_time, "
                "total_fee, paid_fee, state, updated_at) "
                "values ('b1', 'p1', 'B001', '2026-07-16 09:20:00', 100, 100, "
                "'paid', '2026-07-16 09:20:00')"
            )
            conn.execute(
                "insert into patients(patient_identity, display_name, phone, updated_at, current_hash) "
                "values ('p2', '仅账单患者', '13900000000', '2026-07-16 08:00:00', 'h3')"
            )
            conn.execute(
                "insert into bills(bill_id, patient_identity, bill_no, bill_time, "
                "total_fee, paid_fee, state, updated_at) "
                "values ('b2', 'p2', 'B002', '2026-07-16 12:00:00', 50, 0, "
                "'pending', '2026-07-16 12:00:00')"
            )
            conn.execute(
                "insert into payments(payment_id, patient_identity, bill_id, pay_time, "
                "amount, state, pay_type, source_json, updated_at) "
                "values ('pay1', 'p1', 'b1', '2026-07-16 09:30:00', 100, 'paid', "
                "'现金', '{}', '2026-07-16 09:30:00')"
            )
            conn.execute(
                "insert into medical_records(record_id, patient_identity, visit_time, "
                "doctor_name, record_type, draft_status, current_hash) "
                "values ('mr1', 'p1', '2026-07-16 09:15:00', '医生A', '初诊', '', 'h2')"
            )
            conn.execute(
                "insert into return_visits(return_visit_id, patient_identity, due_time, "
                "item_name, status, updated_at) "
                "values ('rv1', 'p1', '2026-07-16 17:00:00', '术后回访', "
                "'待回访', '2026-07-16 08:00:00')"
            )
            conn.commit()

    def _request(self, permissions):
        statements = []

        def traced_connect(db_path):
            conn = real_connect(db_path)
            conn.set_trace_callback(statements.append)
            return conn

        app = FastAPI()
        app.include_router(today_inspection.create_today_inspection_router(self.db_path))
        with (
            mock.patch.object(today_inspection, "require_access", create=True) as required,
            mock.patch.object(
                today_inspection,
                "has_access",
                create=True,
                side_effect=lambda permission: permission in permissions,
            ) as has_access,
            mock.patch.object(today_inspection, "connect", side_effect=traced_connect),
        ):
            response = TestClient(app).get(
                "/api/today-inspection", params={"date": "2026-07-16"}
            )

        required.assert_called_once_with("patient.profile.view")
        self.assertEqual(
            [call.args[0] for call in has_access.call_args_list],
            ["billing.view", "audit.view"],
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json(), "\n".join(statements).lower()

    def test_patient_entry_denial_happens_before_database_connect(self):
        app = FastAPI()
        app.include_router(today_inspection.create_today_inspection_router(self.db_path))
        with (
            mock.patch.object(
                today_inspection,
                "require_access",
                create=True,
                side_effect=HTTPException(status_code=403, detail="forbidden"),
            ),
            mock.patch.object(today_inspection, "has_access", create=True) as has_access,
            mock.patch.object(today_inspection, "connect") as connect,
        ):
            response = TestClient(app).get("/api/today-inspection")

        self.assertEqual(response.status_code, 403)
        has_access.assert_not_called()
        connect.assert_not_called()

    def test_patient_only_never_queries_or_returns_finance_or_audit_sections(self):
        payload, sql = self._request({"patient.profile.view"})

        self.assertNotIn("from bills", sql)
        self.assertNotIn("from payments", sql)
        self.assertNotIn("total_fee", sql)
        self.assertNotIn("bill_id", sql)
        self.assertEqual(payload["appointments"][0]["display_name"], "测试患者")
        self.assertEqual(payload["orders"][0]["item_summary"], "根管治疗")
        self.assertNotIn("status", payload["orders"][0])
        self.assertNotIn("paid", json.dumps(payload, ensure_ascii=False))
        order_select = re.search(
            r"select\s+o\.order_id.*?\n\s+from treatment_orders", sql, re.S
        ).group(0)
        self.assertNotIn("o.status", order_select)
        self.assertTrue(
            {
                "bills",
                "payments",
                "bill_id",
                "item_total",
                "total_fee",
                "paid_fee",
                "unpaid_fee",
                "checks",
                "overall",
                "summary",
            }.isdisjoint(_all_keys(payload))
        )

    def test_billing_without_audit_returns_money_but_no_checks_or_summary(self):
        payload, sql = self._request(
            {"patient.profile.view", "billing.view"}
        )

        self.assertIn("from bills", sql)
        self.assertIn("from payments", sql)
        self.assertEqual(payload["bills"][0]["total_fee"], 100)
        self.assertEqual(payload["payments"][0]["amount"], 100)
        self.assertEqual(payload["orders"][0]["item_total"], 100)
        self.assertEqual(payload["orders"][0]["status"], "paid")
        self.assertTrue(
            {"checks", "overall", "summary"}.isdisjoint(_all_keys(payload))
        )

    def test_audit_without_billing_has_nonfinancial_checks_and_zero_money_sql(self):
        payload, sql = self._request(
            {"patient.profile.view", "audit.view"}
        )

        self.assertNotIn("from bills", sql)
        self.assertNotIn("from payments", sql)
        self.assertNotIn("total_fee", sql)
        self.assertNotIn("bill_id", sql)
        self.assertIn("summary", payload)
        self.assertIn("checks", payload["appointments"][0])
        check_names = {
            check["check"]
            for group in ("appointments", "orders")
            for item in payload[group]
            for check in item["checks"]
        }
        self.assertTrue({"账单", "收款", "金额"}.isdisjoint(check_names))
        self.assertTrue(
            {
                "bills",
                "payments",
                "bill_id",
                "item_total",
                "total_fee",
                "paid_fee",
                "unpaid_fee",
            }.isdisjoint(_all_keys(payload))
        )
        self.assertNotIn("bills", payload["summary"])

    def test_billing_and_audit_keep_money_consistency_checks(self):
        payload, sql = self._request(
            {"patient.profile.view", "billing.view", "audit.view"}
        )

        self.assertIn("from bills", sql)
        self.assertIn("from payments", sql)
        self.assertEqual(payload["summary"]["error"], 0)
        order_check_names = {
            check["check"] for check in payload["orders"][0]["checks"]
        }
        self.assertIn("金额", order_check_names)
        self.assertIn("收款", order_check_names)

    def test_cancelled_appointment_is_filtered_and_arrival_checks_stay_exact(self):
        payload, _ = self._request(
            {"patient.profile.view", "audit.view"}
        )

        by_id = {item["appointment_id"]: item for item in payload["appointments"]}
        self.assertNotIn("a-cancelled", by_id)
        arrived = {check["check"]: check for check in by_id["a1"]["checks"]}
        not_arrived = {check["check"]: check for check in by_id["a2"]["checks"]}
        self.assertEqual(arrived["到诊"]["status"], "ok")
        self.assertEqual(not_arrived["到诊"]["status"], "warn")

    def test_bill_only_walkin_exists_only_with_billing_permission(self):
        patient_payload, _ = self._request({"patient.profile.view"})
        billing_payload, _ = self._request(
            {"patient.profile.view", "billing.view"}
        )

        self.assertNotIn(
            "p2", {item["patient_identity"] for item in patient_payload["walkins"]}
        )
        self.assertIn(
            "p2", {item["patient_identity"] for item in billing_payload["walkins"]}
        )


if __name__ == "__main__":
    unittest.main()
