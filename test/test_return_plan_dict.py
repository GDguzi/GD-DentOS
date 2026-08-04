"""回访计划(ReturnPlan 字典)维护:新增/停用,天数+回访内容;仅 master_data.manage,类型白名单。"""
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from local_app import auth
from local_app.api import create_app
from local_app.db import connect, init_db


def _client(tmp):
    db = Path(tmp) / "clinic.sqlite3"
    init_db(db)
    with connect(db) as conn:
        auth.create_user(conn, "boss", "院长", "admin123", role="admin")
        conn.commit()
    client = TestClient(create_app(db))
    client.post("/api/auth/login", json={"username": "boss", "password": "admin123"})
    return db, client


class ReturnPlanDictTest(unittest.TestCase):
    def test_create_then_visible_in_dictionaries(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, client = _client(tmp)
            resp = client.post("/api/dictionaries", json={
                "dict_type": "ReturnPlan", "name": "拔牙术后关怀",
                "value": 1, "describe": "询问出血/疼痛情况,叮嘱冷敷与进食注意",
            })
            self.assertEqual(resp.status_code, 200, resp.text)
            items = client.get("/api/dictionaries?type=ReturnPlan").json()["items"]
            self.assertEqual(len(items), 1)
            self.assertEqual(items[0]["name"], "拔牙术后关怀")
            self.assertEqual(int(items[0]["value"]), 1)   # 字典 value 列为文本,消费方已兼容
            self.assertIn("冷敷", items[0]["describe"])

    def test_stop_hides_from_list(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, client = _client(tmp)
            did = client.post("/api/dictionaries", json={
                "dict_type": "ReturnPlan", "name": "种植复查", "value": 180, "describe": "复查骨结合",
            }).json()["dict_id"]
            self.assertEqual(client.post(f"/api/dictionaries/{did}/stop").status_code, 200)
            items = client.get("/api/dictionaries?type=ReturnPlan").json()["items"]
            self.assertEqual(items, [])

    def test_validation_and_type_whitelist(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, client = _client(tmp)
            self.assertEqual(client.post("/api/dictionaries", json={
                "dict_type": "ReturnPlan", "name": "", "value": 1}).status_code, 400)
            self.assertEqual(client.post("/api/dictionaries", json={
                "dict_type": "ReturnPlan", "name": "x", "value": 0}).status_code, 400)
            self.assertEqual(client.post("/api/dictionaries", json={
                "dict_type": "PayType", "name": "x", "value": 1}).status_code, 400,
                "写接口只放开回访计划,不给全字典开口子")

    def test_routes_registered_in_access_policy(self):
        from local_app.access_policy import ROUTE_POLICY_BY_KEY
        for key in (("POST", "/api/dictionaries"), ("POST", "/api/dictionaries/{dict_id}/stop")):
            policy = ROUTE_POLICY_BY_KEY.get(key)
            self.assertIsNotNone(policy, key)
            self.assertEqual(policy.permissions, ("master_data.manage",), key)


if __name__ == "__main__":
    unittest.main()
