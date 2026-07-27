"""忘记密码救援 CLI：重置后旧密码失效、新密码可登、停用账号被重新启用。"""
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from local_app import auth
from local_app.api import create_app
from local_app.db import connect, init_db
from local_app.reset_password import reset


class ResetPasswordTest(unittest.TestCase):
    def _db(self, tmp):
        db = Path(tmp) / "clinic.sqlite3"
        init_db(db)
        with connect(db) as conn:
            auth.create_user(conn, "admin", "管理员", "old12345", role="admin")
            conn.commit()
        return db

    def test_reset_changes_login(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = self._db(tmp)
            ok, _ = reset(db, "admin", "new12345")
            self.assertTrue(ok)
            client = TestClient(create_app(db))
            self.assertEqual(client.post("/api/auth/login", json={"username": "admin", "password": "old12345"}).status_code, 401)
            self.assertEqual(client.post("/api/auth/login", json={"username": "admin", "password": "new12345"}).status_code, 200)

    def test_reset_reactivates_and_clears_sessions(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = self._db(tmp)
            with connect(db) as conn:
                uid = conn.execute("select id from users where username='admin'").fetchone()[0]
                conn.execute("update users set is_active=0 where id=?", (uid,))
                conn.execute("insert into sessions(token, user_id, created_at, expires_at) values ('t', ?, '2026-01-01', '2099-01-01')", (uid,))
                conn.commit()
            reset(db, "admin", "new12345")
            with connect(db) as conn:
                self.assertEqual(conn.execute("select is_active from users where username='admin'").fetchone()[0], 1)
                self.assertEqual(conn.execute("select count(*) from sessions where user_id=?", (uid,)).fetchone()[0], 0)

    def test_reset_errors(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = self._db(tmp)
            self.assertFalse(reset(db, "nope", "123456")[0])   # 用户不存在

    def test_reset_single_char_password(self):
        # 2D.3：只要求原文含非空白字符,单字符合法且真能登
        with tempfile.TemporaryDirectory() as tmp:
            db = self._db(tmp)
            self.assertTrue(reset(db, "admin", "短")[0])
            client = TestClient(create_app(db))
            self.assertEqual(client.post("/api/auth/login", json={"username": "admin", "password": "短"}).status_code, 200)

    def test_reset_rejects_blank_without_changing_hash(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = self._db(tmp)
            with connect(db) as conn:
                before = conn.execute("select password_hash from users where username='admin'").fetchone()[0]
            for pw in ["", " ", "   ", "\t", "\n", " \t\n ", None]:
                self.assertFalse(reset(db, "admin", pw)[0], repr(pw))
            with connect(db) as conn:
                self.assertEqual(conn.execute("select password_hash from users where username='admin'").fetchone()[0],
                                 before, "拒绝时不得改 hash")

    def test_reset_rejects_non_str_without_changing_hash(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = self._db(tmp)
            with connect(db) as conn:
                before = conn.execute("select password_hash from users where username='admin'").fetchone()[0]
            for pw in [1, 0, True, False, ["x"], {"k": "x"}, object()]:
                ok, msg = reset(db, "admin", pw)
                self.assertFalse(ok, repr(pw))
                self.assertNotIn(repr(pw), msg, "报错不得回显密码")
            with connect(db) as conn:
                self.assertEqual(conn.execute("select password_hash from users where username='admin'").fetchone()[0],
                                 before, "拒绝时不得改 hash")

    def test_reset_does_not_trim(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = self._db(tmp)
            self.assertTrue(reset(db, "admin", " x ")[0])
            client = TestClient(create_app(db))
            self.assertEqual(client.post("/api/auth/login", json={"username": "admin", "password": "x"}).status_code, 401)
            self.assertEqual(client.post("/api/auth/login", json={"username": "admin", "password": " x "}).status_code, 200)


if __name__ == "__main__":
    unittest.main()
