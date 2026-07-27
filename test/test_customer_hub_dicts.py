"""Access V3 partition and transaction tests for customer-hub dictionaries."""
import json
import sqlite3
import unittest

from customer_hub_access_fixture import CustomerHubAccessFixture


class CustomerHubDictionaryAccessTest(unittest.TestCase):
    def setUp(self):
        self.fixture = CustomerHubAccessFixture()

    def tearDown(self):
        self.fixture.close()

    def _write_raw_dict_value(self, key, raw_value):
        conn = sqlite3.connect(self.fixture.db_path)
        try:
            conn.execute(
                "update app_settings set value = ? where key = ?",
                (raw_value, key),
            )
            conn.commit()
        finally:
            conn.close()

    def _read_raw_dict_value(self, key):
        conn = sqlite3.connect(self.fixture.db_path)
        try:
            return conn.execute(
                "select value from app_settings where key = ?", (key,)
            ).fetchone()[0]
        finally:
            conn.close()

    def test_get_fails_closed_for_invalid_or_non_list_stored_json(self):
        client = self.fixture.client_with_permissions(("return_visit.view",))
        corrupt_values = (
            "{invalid-json",
            json.dumps({"not": "a list"}),
        )
        for raw_value in corrupt_values:
            with self.subTest(raw_value=raw_value):
                self._write_raw_dict_value("return_type", raw_value)
                response = client.get("/api/settings/customer-hub-dicts")
                self.assertEqual(response.status_code, 500)
                self.assertEqual(
                    response.json(),
                    {"detail": "customer_hub_dictionary_unavailable"},
                )
                self.assertNotIn(raw_value, response.text)

    def test_put_does_not_overwrite_or_audit_corrupt_stored_dictionary(self):
        client = self.fixture.client_with_permissions(("return_visit.manage",))
        corrupt_values = (
            "{invalid-json",
            json.dumps({"not": "a list"}),
        )
        for raw_value in corrupt_values:
            with self.subTest(raw_value=raw_value):
                self._write_raw_dict_value("return_type", raw_value)
                response = client.put(
                    "/api/settings/customer-hub-dicts",
                    json={"return_type": ["不得覆盖损坏存储"]},
                )
                self.assertEqual(response.status_code, 500)
                self.assertEqual(
                    response.json(),
                    {"detail": "customer_hub_dictionary_unavailable"},
                )
                self.assertNotIn(raw_value, response.text)
                self.assertEqual(
                    self._read_raw_dict_value("return_type"), raw_value
                )
                self.assertEqual(self.fixture.count_audits(), 0)

    def test_put_rejects_unencodable_string_without_side_effects(self):
        before = self.fixture.read_customer_hub_dicts()
        client = self.fixture.client_with_permissions(("return_visit.manage",))
        response = client.put(
            "/api/settings/customer-hub-dicts",
            content='{"return_type":["\\ud800"]}',
            headers={"content-type": "application/json"},
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json(), {"detail": "invalid_dictionary_value"})
        self.assertEqual(self.fixture.read_customer_hub_dicts(), before)
        self.assertEqual(self.fixture.count_audits(), 0)

    def test_get_returns_only_authorized_dictionary_partition(self):
        expected = self.fixture.read_customer_hub_dicts()
        cases = (
            (
                ("return_visit.view",),
                {"return_type", "visit_content", "visit_result"},
            ),
            (("communication.view",), {"comm_reason"}),
            (
                ("return_visit.view", "communication.view"),
                set(expected),
            ),
        )
        for permissions, expected_keys in cases:
            with self.subTest(permissions=permissions):
                client = self.fixture.client_with_permissions(permissions)
                response = client.get("/api/settings/customer-hub-dicts")
                self.assertEqual(response.status_code, 200)
                self.assertEqual(set(response.json()), expected_keys)
                self.assertEqual(
                    response.json(),
                    {key: expected[key] for key in expected_keys},
                )

        denied = self.fixture.client_with_permissions(("appointment.view",))
        response = denied.get("/api/settings/customer-hub-dicts")
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json(), {"detail": "access_denied"})

    def test_put_enforces_partition_permissions_and_writes_one_audit(self):
        return_payload = {
            "return_type": ["合成回访类型-新值"],
            "visit_content": ["合成回访内容-新值"],
            "visit_result": ["合成回访结果-新值"],
        }
        return_client = self.fixture.client_with_permissions(
            ("return_visit.manage",)
        )
        response = return_client.put(
            "/api/settings/customer-hub-dicts", json=return_payload
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.fixture.count_audits(), 1)
        stored = self.fixture.read_customer_hub_dicts()
        for key, value in return_payload.items():
            self.assertEqual(stored[key], value)

        communication_client = self.fixture.client_with_permissions(
            ("communication.manage",)
        )
        response = communication_client.put(
            "/api/settings/customer-hub-dicts",
            json={"comm_reason": ["合成沟通原因-新值"]},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.fixture.count_audits(), 2)

    def test_mixed_put_requires_both_permissions_and_rolls_back_whole_request(self):
        before = self.fixture.read_customer_hub_dicts()
        client = self.fixture.client_with_permissions(("return_visit.manage",))
        response = client.put(
            "/api/settings/customer-hub-dicts",
            json={
                "return_type": ["不得落库的回访类型"],
                "comm_reason": ["不得落库的沟通原因"],
            },
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json(), {"detail": "access_denied"})
        self.assertEqual(self.fixture.read_customer_hub_dicts(), before)
        self.assertEqual(self.fixture.count_audits(), 0)

        allowed = self.fixture.client_with_permissions(
            ("return_visit.manage", "communication.manage")
        )
        response = allowed.put(
            "/api/settings/customer-hub-dicts",
            json={
                "return_type": ["合成混合回访类型"],
                "comm_reason": ["合成混合沟通原因"],
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.fixture.count_audits(), 1)

    def test_put_rechecks_revoked_permission_after_authentication_before_lock(self):
        from local_app.routes import app_settings

        before = self.fixture.read_customer_hub_dicts()
        client = self.fixture.client_with_permissions(("return_visit.manage",))
        response = self.fixture.request_with_revocation_before_begin(
            client,
            "PUT",
            "/api/settings/customer-hub-dicts",
            "return_visit.manage",
            begin_module=app_settings,
            json={"return_type": ["竞态不得落库"]},
        )
        self.assertTrue(self.fixture.revocation_callback_hit)
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json(), {"detail": "access_denied"})
        self.assertEqual(self.fixture.read_customer_hub_dicts(), before)
        self.assertEqual(self.fixture.count_audits(), 0)


if __name__ == "__main__":
    unittest.main()
