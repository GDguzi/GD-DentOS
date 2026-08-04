"""同意书模板维护:签署弹窗内直接新建(POST /api/consent-templates)。"""
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


class ConsentTemplateCreateTest(unittest.TestCase):
    def test_create_then_list_and_get(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, client = _client(tmp)
            resp = client.post("/api/consent-templates", json={
                "name": "拔牙知情同意书", "category": "外科",
                "body": "术前告知:……\n患者已知晓风险。",
            })
            self.assertEqual(resp.status_code, 200, resp.text)
            tid = resp.json()["template_id"]
            listed = client.get("/api/consent-templates").json()["templates"]
            self.assertEqual([t["name"] for t in listed], ["拔牙知情同意书"])
            got = client.get(f"/api/consent-templates/{tid}").json()
            self.assertEqual(got["category"], "外科")
            self.assertIn("已知晓风险", got["body"])
            self.assertEqual(got["source"], "local")

    def test_create_validates_required_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, client = _client(tmp)
            for payload in ({"name": "", "category": "外科", "body": "x"},
                            {"name": "a", "category": "", "body": "x"},
                            {"name": "a", "category": "外科", "body": " "}):
                self.assertEqual(
                    client.post("/api/consent-templates", json=payload).status_code, 400, payload)

    def test_duplicate_name_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, client = _client(tmp)
            client.post("/api/consent-templates", json={"name": "冠修复", "category": "修复", "body": "x"})
            resp = client.post("/api/consent-templates", json={"name": "冠修复", "category": "修复", "body": "y"})
            self.assertEqual(resp.status_code, 409)

    def test_route_registered_in_access_policy(self):
        from local_app.access_policy import ROUTE_POLICY_BY_KEY
        policy = ROUTE_POLICY_BY_KEY.get(("POST", "/api/consent-templates"))
        self.assertIsNotNone(policy, "新建模板路由必须登记")
        self.assertEqual(policy.permissions, ("consent.manage",))


if __name__ == "__main__":
    unittest.main()
