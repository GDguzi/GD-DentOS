import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

from local_app.api import create_app
from local_app.db import connect as real_connect
from local_app.db import init_db
from local_app.routes import patient_assets


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


class SectionedPatientAssetsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = Path(self.tmp.name) / "clinic.sqlite3"
        init_db(self.db_path)
        with real_connect(self.db_path) as conn:
            conn.execute(
                "insert into patients(patient_identity, display_name, updated_at, current_hash) "
                "values ('p1', '测试患者', '2026-07-16 08:00:00', 'h1')"
            )
            conn.execute(
                "insert into medical_records(record_id, patient_identity, visit_time, "
                "draft_status, record_type, doctor_name, current_hash) "
                "values ('mr1', 'p1', '2026-07-16 09:00:00', '', '初诊', '医生A', 'h2')"
            )
            conn.execute(
                "insert into appointments(appointment_id, patient_identity, start_time, "
                "doctor_name, item_name, status, arrived_at) "
                "values ('a1', 'p1', '2026-07-16 09:30:00', '医生A', '洁牙', 1, "
                "'2026-07-16 09:25:00')"
            )
            conn.execute(
                "insert into return_visits(return_visit_id, patient_identity, due_time, "
                "item_name, status, updated_at) "
                "values ('rv1', 'p1', '2026-07-17 09:00:00', '术后回访', 0, "
                "'2026-07-16 08:00:00')"
            )
            conn.execute(
                "insert into local_notes(note_id, patient_identity, note_text) "
                "values ('n1', 'p1', '测试备注')"
            )
            conn.execute(
                "insert into patient_images(image_id, patient_identity, file_name, rel_path) "
                "values ('img1', 'p1', 'a.jpg', 'p1/a.jpg')"
            )
            conn.execute(
                "insert into bills(bill_id, patient_identity, bill_no, bill_time, "
                "total_fee, paid_fee) "
                "values ('b1', 'p1', 'B001', '2026-07-16 10:00:00', 200, 150)"
            )
            conn.execute(
                "insert into payments(payment_id, patient_identity, amount, state, "
                "source_json, updated_at) "
                "values ('pay1', 'p1', 150, 'paid', '{}', '2026-07-16 10:01:00')"
            )
            conn.execute(
                "insert into treatment_items(treatment_item_id, patient_identity, bill_id, "
                "item_name, item_code, doctor_name, unit_price, quantity, total_fee, "
                "fee_type, is_refund, is_deleted) "
                "values ('ti1', 'p1', 'b1', '洁牙', 'T001', '医生A', 100, 2, 200, "
                "'治疗', 0, 0)"
            )
            conn.commit()

    def _request(self, path, *, billing):
        statements = []

        def traced_connect(db_path):
            conn = real_connect(db_path)
            conn.set_trace_callback(statements.append)
            return conn

        with (
            mock.patch.object(
                patient_assets, "require_access", create=True
            ) as require_access,
            mock.patch.object(
                patient_assets,
                "has_access",
                create=True,
                return_value=billing,
            ) as has_access,
            mock.patch.object(
                patient_assets, "connect", side_effect=traced_connect
            ),
        ):
            response = TestClient(create_app(self.db_path)).get(path)

        require_access.assert_called_once_with("patient.profile.view")
        has_access.assert_called_once_with("billing.view")
        self.assertEqual(response.status_code, 200, response.text)
        return response.json(), "\n".join(statements).lower()

    def test_treatments_without_billing_never_selects_or_returns_money(self):
        payload, sql = self._request(
            "/api/patients/p1/treatments", billing=False
        )

        self.assertNotIn("unit_price", sql)
        self.assertNotIn("total_fee", sql)
        self.assertTrue(payload["visits"])
        self.assertEqual(payload["visits"][0]["items"][0]["item_name"], "洁牙")
        self.assertTrue(
            {"unit_price", "total_fee", "bill_total_fee"}.isdisjoint(
                _all_keys(payload)
            )
        )

    def test_treatments_with_billing_keeps_real_money_values(self):
        payload, sql = self._request(
            "/api/patients/p1/treatments", billing=True
        )

        self.assertIn("unit_price", sql)
        self.assertIn("total_fee", sql)
        visit = payload["visits"][0]
        self.assertEqual(visit["bill_total_fee"], 200)
        self.assertEqual(visit["items"][0]["unit_price"], 100)
        self.assertEqual(visit["items"][0]["total_fee"], 200)

    def test_visits_without_billing_never_selects_or_returns_money(self):
        payload, sql = self._request("/api/patients/p1/visits", billing=False)

        self.assertNotIn("total_fee", sql)
        self.assertNotIn("paid_fee", sql)
        self.assertTrue(payload["visits"])
        self.assertTrue(
            {"total_fee", "paid_fee"}.isdisjoint(_all_keys(payload))
        )

    def test_visits_with_billing_keeps_real_money_values(self):
        payload, sql = self._request("/api/patients/p1/visits", billing=True)

        self.assertIn("total_fee", sql)
        self.assertIn("paid_fee", sql)
        bills = [bill for day in payload["visits"] for bill in day["bills"]]
        treatments = [
            item for day in payload["visits"] for item in day["treatments"]
        ]
        self.assertEqual(bills[0]["total_fee"], 200)
        self.assertEqual(bills[0]["paid_fee"], 150)
        self.assertEqual(treatments[0]["total_fee"], 200)

    def test_summary_without_billing_never_reads_or_returns_finance_sections(self):
        payload, sql = self._request("/api/patients/p1/summary", billing=False)

        self.assertNotIn("from bills", sql)
        self.assertNotIn("from payments", sql)
        self.assertNotIn("from treatment_items", sql)
        self.assertEqual(payload["counts"]["medical_records"], 1)
        self.assertTrue(
            {"bills", "payments", "treatment_items"}.isdisjoint(
                payload["counts"]
            )
        )
        self.assertNotIn("total_spend", payload["business"])

    def test_summary_with_billing_keeps_real_finance_values(self):
        payload, sql = self._request("/api/patients/p1/summary", billing=True)

        self.assertIn("from bills", sql)
        self.assertIn("from payments", sql)
        self.assertIn("from treatment_items", sql)
        self.assertEqual(payload["counts"]["bills"], 1)
        self.assertEqual(payload["counts"]["payments"], 1)
        self.assertEqual(payload["counts"]["treatment_items"], 1)
        self.assertEqual(payload["business"]["total_spend"], 150.0)


if __name__ == "__main__":
    unittest.main()
