"""按用户个性化设置：登录守卫 + 读写 + key 白名单。"""
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from local_app import auth
from local_app.api import create_app
from local_app.db import connect, init_db


class UserSettingsTest(unittest.TestCase):
    def _client(self, tmp):
        db = Path(tmp) / "clinic.sqlite3"
        init_db(db)
        with connect(db) as conn:
            auth.create_user(conn, "u", "U", "pw123456", role="reception")
            conn.commit()
        client = TestClient(create_app(db))
        client.post("/api/auth/login", json={"username": "u", "password": "pw123456"})
        return client

    def test_requires_login(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "clinic.sqlite3"
            init_db(db)
            client = TestClient(create_app(db))
            self.assertEqual(client.get("/api/my-settings/queue_columns").status_code, 401)

    def test_get_default_null_then_save(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = self._client(tmp)
            self.assertIsNone(client.get("/api/my-settings/queue_columns").json()["value"])
            cols = ["name", "actions", "status"]
            r = client.put("/api/my-settings/queue_columns", json={"value": cols})
            self.assertEqual(r.status_code, 200)
            self.assertEqual(client.get("/api/my-settings/queue_columns").json()["value"], cols)

    def test_bad_key_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = self._client(tmp)
            self.assertEqual(client.get("/api/my-settings/whatever").status_code, 400)
            self.assertEqual(client.put("/api/my-settings/whatever", json={"value": 1}).status_code, 400)

    def test_per_user_isolation(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "clinic.sqlite3"
            init_db(db)
            with connect(db) as conn:
                auth.create_user(conn, "a", "A", "pw123456")
                auth.create_user(conn, "b", "B", "pw123456")
                conn.commit()
            ca = TestClient(create_app(db)); ca.post("/api/auth/login", json={"username": "a", "password": "pw123456"})
            cb = TestClient(create_app(db)); cb.post("/api/auth/login", json={"username": "b", "password": "pw123456"})
            ca.put("/api/my-settings/queue_columns", json={"value": ["name"]})
            self.assertEqual(ca.get("/api/my-settings/queue_columns").json()["value"], ["name"])
            self.assertIsNone(cb.get("/api/my-settings/queue_columns").json()["value"])  # b 不受 a 影响


if __name__ == "__main__":
    unittest.main()
