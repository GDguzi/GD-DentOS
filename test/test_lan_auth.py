import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from local_app.api import create_app
from local_app.db import init_db


def _db(tmp):
    p = Path(tmp) / "clinic.sqlite3"
    init_db(p)
    return p


class LanAuthTest(unittest.TestCase):
    def test_no_password_means_no_gate(self):
        # 默认/只本机：不传口令 → 无门禁，接口直接可用(不破坏现有部署/测试)
        with tempfile.TemporaryDirectory() as tmp:
            client = TestClient(create_app(_db(tmp)))
            self.assertEqual(client.get("/api/consent-templates").status_code, 200)

    def test_gate_blocks_without_login(self):
        # 带口令(局域网) + 非本机(TestClient host=testclient) → 未登录被挡
        with tempfile.TemporaryDirectory() as tmp:
            client = TestClient(create_app(_db(tmp), access_password="secret"))
            # API 未登录 → 401
            self.assertEqual(client.get("/api/consent-templates").status_code, 401)
            # 页面未登录 → 返回登录页(200, 含"访问口令")
            r = client.get("/")
            self.assertEqual(r.status_code, 200)
            self.assertIn("访问口令", r.text)

    def test_login_wrong_password(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = TestClient(create_app(_db(tmp), access_password="secret"))
            self.assertEqual(client.post("/api/login", json={"password": "wrong"}).status_code, 401)

    def test_login_then_access(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = TestClient(create_app(_db(tmp), access_password="secret"))
            r = client.post("/api/login", json={"password": "secret"})
            self.assertEqual(r.status_code, 200)
            self.assertIn("dental_auth", r.cookies)
            # 登录后(TestClient 带 cookie) → 接口放行
            self.assertEqual(client.get("/api/consent-templates").status_code, 200)

    def test_repeated_wrong_password_never_locks(self):
        # 2D.2：撤除口令限速——连错任意次都是 401，随后正确口令立刻成功
        with tempfile.TemporaryDirectory() as tmp:
            client = TestClient(create_app(_db(tmp), access_password="secret"))
            for i in range(10):
                r = client.post("/api/login", json={"password": "x"})
                self.assertEqual(r.status_code, 401, f"第{i+1}次")
                self.assertEqual(r.json()["detail"], "访问口令错误")
            r = client.post("/api/login", json={"password": "secret"})
            self.assertEqual(r.status_code, 200)
            self.assertIn("dental_auth", r.cookies)

    def test_non_str_password_never_matches(self):
        # 口令是字符串 "1"：JSON 数字 1 / 布尔 / 数组 / 对象都不得被强转后命中,只能 401
        with tempfile.TemporaryDirectory() as tmp:
            client = TestClient(create_app(_db(tmp), access_password="1"))
            for pw in [1, 0, True, False, ["1"], {"k": "1"}]:
                r = client.post("/api/login", json={"password": pw})
                self.assertEqual(r.status_code, 401, f"{pw!r} -> {r.status_code} {r.text}")
            self.assertEqual(client.post("/api/login", json={"password": "1"}).status_code, 200)

    def test_login_page_reachable_without_auth(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = TestClient(create_app(_db(tmp), access_password="secret"))
            self.assertEqual(client.get("/__login").status_code, 200)


if __name__ == "__main__":
    unittest.main()
