"""账号系统 P1：密码、登录/会话/登出、门禁、种子 admin、操作归属默认。"""
import datetime as dt
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from local_app import auth
from local_app.api import create_app
from local_app.db import connect, init_db


def _db(tmp):
    db = Path(tmp) / "clinic.sqlite3"
    init_db(db)
    return db


class PasswordTest(unittest.TestCase):
    def test_hash_verify_roundtrip(self):
        h = auth.hash_password("secret123")
        self.assertIn("$", h)
        self.assertTrue(auth.verify_password("secret123", h))
        self.assertFalse(auth.verify_password("wrong", h))

    def test_verify_bad_stored(self):
        self.assertFalse(auth.verify_password("x", ""))
        self.assertFalse(auth.verify_password("x", "nodollar"))


class AuthFlowTest(unittest.TestCase):
    def test_login_me_logout(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = _db(tmp)
            with connect(db) as conn:
                auth.create_user(conn, "qiantai", "前台小王", "pw123456", role="reception")
                conn.commit()
            client = TestClient(create_app(db))
            self.assertIsNone(client.get("/api/auth/me").json()["user"])
            r = client.post("/api/auth/login", json={"username": "qiantai", "password": "pw123456"})
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r.json()["user"]["username"], "qiantai")
            me = client.get("/api/auth/me").json()["user"]
            self.assertEqual(me["display_name"], "前台小王")
            self.assertEqual(me["role"], "reception")
            self.assertEqual(client.post("/api/auth/logout").status_code, 200)
            self.assertIsNone(client.get("/api/auth/me").json()["user"])

    def test_wrong_password_401(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = _db(tmp)
            with connect(db) as conn:
                auth.create_user(conn, "u", "U", "right-pw")
                conn.commit()
            client = TestClient(create_app(db))
            self.assertEqual(client.post("/api/auth/login", json={"username": "u", "password": "bad"}).status_code, 401)
            self.assertEqual(client.post("/api/auth/login", json={"username": "nope", "password": "x"}).status_code, 401)


class GateTest(unittest.TestCase):
    def test_require_login_blocks_then_allows(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = _db(tmp)
            auth.ensure_seed_roles(db)
            with connect(db) as conn:
                # #482:样例接口 /api/patients 现按 patient.view 守卫;本例只验登录网关(非RBAC),
                # 用 admin(全通过)避免与角色权限耦合。
                auth.create_user(conn, "u", "U", "pw123456", role="admin")
                conn.commit()
            client = TestClient(create_app(db, require_login=True))
            # 未登录访问受保护 API → 401
            self.assertEqual(client.get("/api/patients").status_code, 401)
            # 页面 → 返回登录页(#808:含静态裸路径 /index.html,防止 StaticFiles 绕过登录墙)
            for path in ("/", "/index.html", "/styles.css"):
                page = client.get(path)
                self.assertEqual(page.status_code, 200)
                self.assertIn("请登录", page.text)
                self.assertNotIn("导航", page.text)
            # 登录接口豁免、health 豁免
            self.assertEqual(client.get("/api/health").status_code, 200)
            # 登录后放行
            self.assertEqual(client.post("/api/auth/login", json={"username": "u", "password": "pw123456"}).status_code, 200)
            self.assertEqual(client.get("/api/patients").status_code, 200)

    def test_html_no_store_808(self):
        # #808:HTML 文档必须 no-store——否则浏览器启发式缓存会在登出后回放登录期骨架;
        # js/css 保持可缓存(靠 ?v= 版本号失效),不得误伤。
        with tempfile.TemporaryDirectory() as tmp:
            db = _db(tmp)
            auth.ensure_seed_roles(db)
            with connect(db) as conn:
                auth.create_user(conn, "u", "U", "pw123456", role="admin")
                conn.commit()
            client = TestClient(create_app(db, require_login=True))
            # 未登录:登录页也 no-store
            self.assertEqual(client.get("/index.html").headers.get("cache-control"), "no-store")
            client.post("/api/auth/login", json={"username": "u", "password": "pw123456"})
            # 登录后:骨架 HTML no-store,js/css 不受影响
            for path in ("/", "/index.html"):
                self.assertEqual(client.get(path).headers.get("cache-control"), "no-store", path)
            self.assertIsNone(client.get("/styles.css").headers.get("cache-control"))

    def test_no_gate_by_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = _db(tmp)
            client = TestClient(create_app(db))  # 不开门禁
            self.assertEqual(client.get("/api/patients").status_code, 200)


class SessionTest(unittest.TestCase):
    def test_expired_session_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = _db(tmp)
            with connect(db) as conn:
                uid = auth.create_user(conn, "u", "U", "pw")
                past = (dt.datetime.now() - dt.timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")
                conn.execute("insert into sessions(token, user_id, created_at, expires_at) values ('tok-old', ?, ?, ?)",
                             (uid, "2020-01-01 00:00:00", past))
                conn.commit()
                self.assertIsNone(auth.user_by_token(conn, "tok-old"))

    def test_remember_longer_ttl(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = _db(tmp)
            with connect(db) as conn:
                uid = auth.create_user(conn, "u", "U", "pw")
                _, ttl_short = auth.create_session(conn, uid, remember=False)
                _, ttl_long = auth.create_session(conn, uid, remember=True)
                conn.commit()
            self.assertEqual(ttl_short, auth.SESSION_TTL_DAYS)
            self.assertEqual(ttl_long, auth.SESSION_TTL_DAYS_REMEMBER)
            self.assertGreater(ttl_long, ttl_short)


class SeedAdminTest(unittest.TestCase):
    def test_seed_admin_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = _db(tmp)
            seeded = auth.ensure_seed_admin(db)
            self.assertEqual(seeded[0], "admin")
            self.assertTrue((Path(tmp) / ".admin_initial_password").exists())
            # 再次调用不重复建
            self.assertIsNone(auth.ensure_seed_admin(db))
            with connect(db) as conn:
                self.assertEqual(conn.execute("select count(*) from users where username='admin'").fetchone()[0], 1)


class AuditOperatorTest(unittest.TestCase):
    def test_default_operator_is_local_user(self):
        # 无登录态(测试默认)：保持既有 'local_user'
        self.assertEqual(auth.audit_operator(), "local_user")

    def test_operation_attributed_to_logged_in_user(self):
        # 登录后做操作 → audit_logs.operator 记为该用户(contextvar 传到深层端点)
        with tempfile.TemporaryDirectory() as tmp:
            db = _db(tmp)
            auth.ensure_seed_roles(db)  # 医生角色默认权限(含 patient.create)入表，require_perm 才放行
            with connect(db) as conn:
                auth.create_user(conn, "yisheng", "王医生", "pw123456", role="doctor")
                conn.commit()
            client = TestClient(create_app(db))
            client.post("/api/auth/login", json={"username": "yisheng", "password": "pw123456"})
            r = client.post("/api/patients", json={"display_name": "测试患者", "phone": "13800000000"})
            self.assertEqual(r.status_code, 200)
            with connect(db) as conn:
                op = conn.execute(
                    "select operator from audit_logs where action='create_patient' order by audit_id desc limit 1"
                ).fetchone()[0]
            self.assertEqual(op, "yisheng")

    def test_operation_without_login_stays_local_user(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = _db(tmp)
            client = TestClient(create_app(db))
            r = client.post("/api/patients", json={"display_name": "测试患者", "phone": "13800000000"})
            self.assertEqual(r.status_code, 200)
            with connect(db) as conn:
                op = conn.execute(
                    "select operator from audit_logs where action='create_patient' order by audit_id desc limit 1"
                ).fetchone()[0]
            self.assertEqual(op, "local_user")


class UserManagementTest(unittest.TestCase):
    def _admin_client(self, tmp):
        db = _db(tmp)
        with connect(db) as conn:
            auth.create_user(conn, "admin", "管理员", "admin123", role="admin")
            conn.commit()
        client = TestClient(create_app(db))
        client.post("/api/auth/login", json={"username": "admin", "password": "admin123"})
        return db, client

    def test_non_admin_forbidden(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = _db(tmp)
            with connect(db) as conn:
                auth.create_user(conn, "qt", "前台", "pw123456", role="reception")
                conn.commit()
            client = TestClient(create_app(db, require_login=True))   # 复现生产配置
            self.assertEqual(client.get("/api/users").status_code, 401)  # 未登录→网关拦
            client.post("/api/auth/login", json={"username": "qt", "password": "pw123456"})
            self.assertEqual(client.get("/api/users").status_code, 403)  # 前台无 user.manage

    def test_admin_create_and_list(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, client = self._admin_client(tmp)
            r = client.post("/api/users", json={"username": "wang", "display_name": "王医生",
                                                "password": "doc12345", "role": "doctor"})
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r.json()["role"], "doctor")
            users = client.get("/api/users").json()["users"]
            self.assertEqual({u["username"] for u in users}, {"admin", "wang"})
            # 重复用户名 409、空密码 400、非法角色 400
            self.assertEqual(client.post("/api/users", json={"username": "wang", "password": "x123456"}).status_code, 409)
            self.assertEqual(client.post("/api/users", json={"username": "z", "password": ""}).status_code, 400)
            self.assertEqual(client.post("/api/users", json={"username": "z", "password": "123456", "role": "boss"}).status_code, 400)

    def test_update_and_disable_clears_sessions(self):
        with tempfile.TemporaryDirectory() as tmp:
            db, client = self._admin_client(tmp)
            uid = client.post("/api/users", json={"username": "zhu", "password": "pw123456", "role": "assistant"}).json()["id"]
            # 该用户登录建会话
            other = TestClient(create_app(db))
            other.post("/api/auth/login", json={"username": "zhu", "password": "pw123456"})
            self.assertIsNotNone(other.get("/api/auth/me").json()["user"])
            # 管理员停用 → 会话被清
            self.assertEqual(client.put(f"/api/users/{uid}", json={"is_active": False}).status_code, 200)
            with connect(db) as conn:
                self.assertEqual(conn.execute("select count(*) from sessions where user_id=?", (uid,)).fetchone()[0], 0)

    def test_cannot_disable_self(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, client = self._admin_client(tmp)
            me = client.get("/api/auth/me").json()["user"]
            self.assertEqual(client.put(f"/api/users/{me['id']}", json={"is_active": False}).status_code, 400)
            self.assertEqual(client.put(f"/api/users/{me['id']}", json={"role": "reception"}).status_code, 400)

    def test_reset_password(self):
        with tempfile.TemporaryDirectory() as tmp:
            db, client = self._admin_client(tmp)
            uid = client.post("/api/users", json={"username": "u", "password": "old12345"}).json()["id"]
            self.assertEqual(client.post(f"/api/users/{uid}/reset-password", json={"password": "new12345"}).status_code, 200)
            other = TestClient(create_app(db))
            self.assertEqual(other.post("/api/auth/login", json={"username": "u", "password": "old12345"}).status_code, 401)
            self.assertEqual(other.post("/api/auth/login", json={"username": "u", "password": "new12345"}).status_code, 200)

    def test_change_own_password(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = _db(tmp)
            with connect(db) as conn:
                auth.create_user(conn, "u", "U", "old12345", role="reception")
                conn.commit()
            client = TestClient(create_app(db))
            client.post("/api/auth/login", json={"username": "u", "password": "old12345"})
            self.assertEqual(client.post("/api/auth/change-password", json={"old_password": "bad", "new_password": "new12345"}).status_code, 400)
            self.assertEqual(client.post("/api/auth/change-password", json={"old_password": "old12345", "new_password": "new12345"}).status_code, 200)

    def test_change_own_password_clears_sessions(self):
        # 动态扫#18：自助改密后清该用户全部会话(同管理员重置),防旧会话继续有效
        with tempfile.TemporaryDirectory() as tmp:
            db = _db(tmp)
            with connect(db) as conn:
                auth.create_user(conn, "u", "U", "old12345", role="reception")
                conn.commit()
            client = TestClient(create_app(db))
            client.post("/api/auth/login", json={"username": "u", "password": "old12345"})
            with connect(db) as conn:
                uid = conn.execute("select id from users where username='u'").fetchone()[0]
                self.assertGreaterEqual(conn.execute("select count(*) from sessions where user_id=?", (uid,)).fetchone()[0], 1)
            client.post("/api/auth/change-password", json={"old_password": "old12345", "new_password": "new12345"})
            with connect(db) as conn:
                self.assertEqual(conn.execute("select count(*) from sessions where user_id=?", (uid,)).fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()


class NoLoginThrottleTest(unittest.TestCase):
    """2D.2:本地部署撤除登录限速——连错任意次都是 401,正确密码立刻成功,永不 429。"""

    def test_repeated_wrong_password_never_locks(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = _db(tmp)
            with connect(db) as conn:
                auth.create_user(conn, "alice", "A", "pw123456")
                conn.commit()
            c = TestClient(create_app(db))
            for i in range(12):
                r = c.post("/api/auth/login", json={"username": "alice", "password": "wrong"})
                self.assertEqual(r.status_code, 401, f"第{i+1}次")
                self.assertEqual(r.json()["detail"], "用户名或密码错误")
            self.assertEqual(c.post("/api/auth/login",
                             json={"username": "alice", "password": "pw123456"}).status_code, 200)

    def test_no_account_enumeration_after_many_failures(self):
        # 存在账号+错密码 与 不存在账号:超过旧阈值后 status/detail 仍完全一致,且都不是 429
        with tempfile.TemporaryDirectory() as tmp:
            db = _db(tmp)
            with connect(db) as conn:
                auth.create_user(conn, "alice", "A", "pw123456")
                conn.commit()
            c = TestClient(create_app(db))
            for _ in range(12):
                r1 = c.post("/api/auth/login", json={"username": "alice", "password": "wrong"})
                r2 = c.post("/api/auth/login", json={"username": "ghost", "password": "wrong"})
                self.assertEqual((r1.status_code, r1.json()["detail"]),
                                 (r2.status_code, r2.json()["detail"]))
                self.assertEqual(r1.status_code, 401)

    def test_state_has_no_fail_buckets(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = create_app(_db(tmp))
            self.assertFalse(hasattr(app.state, "_login_fails"))


class AuditWriteContractTest(unittest.TestCase):
    """#746：数据层 audit_write 的 operator 必填(漏传 TypeError,不许静默兜底成 local_user)；
    auth 薄壳缺省自动填当前登录人/无会话 local_user；两层都不 commit(事务边界归调用方)。"""

    def test_db_layer_missing_operator_raises(self):
        from local_app.db import audit_write
        with tempfile.TemporaryDirectory() as tmp:
            with connect(_db(tmp)) as conn:
                with self.assertRaises(TypeError):
                    audit_write(conn, "probe", "x", "act")   # 漏 operator 必须炸,不许落 local_user

    def test_db_layer_explicit_operator_lands_and_no_commit(self):
        from local_app.db import audit_write
        with tempfile.TemporaryDirectory() as tmp:
            db = _db(tmp)
            conn1 = connect(db)
            try:
                audit_write(conn1, "backup", "f.db", "pre_sync_backup", operator="auto-sync")
                with connect(db) as conn2:   # helper 不 commit → 第二连接看不到
                    self.assertEqual(conn2.execute(
                        "select count(*) from audit_logs where entity_type='backup'").fetchone()[0], 0)
                conn1.commit()
            finally:
                conn1.close()
            with connect(db) as conn2:
                row = conn2.execute(
                    "select operator from audit_logs where entity_type='backup'").fetchone()
                self.assertEqual(row["operator"], "auto-sync")   # 显式 operator 原样落库

    def test_auth_layer_defaults_operator(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = _db(tmp)
            with connect(db) as conn:
                auth.audit_write(conn, "probe", "x", "act")   # 无会话上下文 → local_user
                conn.commit()
                self.assertEqual(conn.execute(
                    "select operator from audit_logs where entity_type='probe'").fetchone()["operator"],
                    "local_user")


class AdminInitialPasswordFileTest(unittest.TestCase):
    """#583:首启 admin 明文初始密码文件在 admin 改密成功后必须删除,不让凭据常驻磁盘。"""

    def test_change_password_removes_initial_password_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = _db(tmp)
            seeded = auth.ensure_seed_admin(db)
            self.assertIsNotNone(seeded)
            username, pw = seeded
            self.assertEqual(pw, "admin", "出厂默认口令 admin(2026-08-03 拍板,取代 #583 随机口令)")
            f = Path(tmp) / ".admin_initial_password"
            self.assertTrue(f.is_file(), "首启应写初始密码文件")
            c = TestClient(create_app(db))
            self.assertEqual(c.post("/api/auth/login",
                                    json={"username": username, "password": pw}).status_code, 200)
            r = c.post("/api/auth/change-password",
                       json={"old_password": pw, "new_password": "newpw12345"})
            self.assertEqual(r.status_code, 200, r.text)
            self.assertFalse(f.exists(), "#583:admin 改密成功后明文初始密码文件必须被删除")

    def test_admin_reset_password_also_removes_file(self):
        # #783:管理员经 reset-password 路径改 admin 密码,明文初始密码文件同样删除
        with tempfile.TemporaryDirectory() as tmp:
            db = _db(tmp)
            username, pw = auth.ensure_seed_admin(db)
            f = Path(tmp) / ".admin_initial_password"
            self.assertTrue(f.is_file())
            c = TestClient(create_app(db))
            c.post("/api/auth/login", json={"username": username, "password": pw})
            with connect(db) as conn:
                uid = conn.execute("select id from users where username='admin'").fetchone()["id"]
            r = c.post(f"/api/users/{uid}/reset-password", json={"password": "resetpw123"})
            self.assertEqual(r.status_code, 200, r.text)
            self.assertFalse(f.exists(), "#783:重置路径改 admin 密码也必须删明文文件")


# 2D.3：全系统只剩一条密码规则——原文至少含一个非空白字符。不做长度/复杂度校验，
# 也绝不 strip 后再哈希(" x " 只能用 " x " 登录)。
BLANK_PASSWORDS = ["", " ", "   ", "\t", "\n", " \t\n "]


class _StrSub(str):
    """str 子类:能覆写 strip/__str__,不算「真正的文本密码」。"""


# 「密码必须是内建 str」：非 str 一律拒绝,不得被 str() 强转后当密码用。
NON_STRING_PASSWORDS = [1, 0, True, False, ["x"], {"k": "x"}, object(), _StrSub("x")]
# 上面能过 JSON 的子集(object()/str 子类没有 JSON 表示)
JSON_NON_STRING_PASSWORDS = [1, 0, True, False, ["x"], {"k": "x"}]

# 孤立 Unicode 代理项：是 str、strip 后非空,但 UTF-8 编码即抛 → 不是合法文本密码。
# 只能用原始 JSON 送进来(json.dumps 也会产出这些转义),TestClient 的 json= 会先编码而抛。
SURROGATE_PASSWORDS = ["\ud800", "a\udfffb", " \ud800 "]
SURROGATE_JSON_ESCAPES = ['\\ud800', 'a\\udfffb', ' \\ud800 ']


def _raw_json_post(client, path, body):
    return client.post(path, content=body.encode("utf-8"),
                       headers={"content-type": "application/json"})


class PasswordRuleTest(unittest.TestCase):
    def test_helper_accepts_single_char_rejects_blank(self):
        for pw in ["x", "短", " x ", "0"]:
            self.assertTrue(auth.password_is_nonblank(pw), pw)
        for pw in BLANK_PASSWORDS + [None]:
            self.assertFalse(auth.password_is_nonblank(pw), repr(pw))

    def test_create_user_fail_closed_on_blank(self):
        # auth.create_user 是内部公共写入口,必须自己拒绝空/纯空白(防绕过 HTTP)
        with tempfile.TemporaryDirectory() as tmp:
            db = _db(tmp)
            with connect(db) as conn:
                for pw in BLANK_PASSWORDS:
                    with self.assertRaises(ValueError, msg=repr(pw)):
                        auth.create_user(conn, "u", "U", pw, role="reception")
                self.assertEqual(conn.execute("select count(*) from users").fetchone()[0], 0,
                                 "拒绝时不得建账号")
                auth.create_user(conn, "u", "U", "短", role="reception")
                conn.commit()
            client = TestClient(create_app(db))
            self.assertEqual(client.post("/api/auth/login",
                                         json={"username": "u", "password": "短"}).status_code, 200)

    def test_hash_password_still_accepts_blank_for_login_check(self):
        # 登录校验会拿空密码算 hash 比对,hash_password 本身不得抛
        self.assertFalse(auth.verify_password("", auth.hash_password("x")))

    def test_helper_rejects_non_str(self):
        # 只认内建 str:数字/布尔/数组/对象/str 子类都不是「密码原文」
        for pw in NON_STRING_PASSWORDS:
            self.assertFalse(auth.password_is_nonblank(pw), repr(pw))

    def test_create_user_fail_closed_on_non_str(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = _db(tmp)
            with connect(db) as conn:
                for pw in NON_STRING_PASSWORDS:
                    with self.assertRaises(ValueError, msg=repr(pw)):
                        auth.create_user(conn, "u", "U", pw, role="reception")
                self.assertEqual(conn.execute("select count(*) from users").fetchone()[0], 0,
                                 "拒绝时不得建账号")

    def test_verify_password_non_str_is_false_not_raise(self):
        h = auth.hash_password("1")
        for pw in NON_STRING_PASSWORDS:
            self.assertFalse(auth.verify_password(pw, h), repr(pw))
        self.assertFalse(auth.verify_password("1", None))
        self.assertTrue(auth.verify_password("1", h))

    def test_api_create_user_rejects_non_str_without_side_effect(self):
        with tempfile.TemporaryDirectory() as tmp:
            db, client = self._admin_client(tmp)
            for pw in JSON_NON_STRING_PASSWORDS:
                r = client.post("/api/users", json={"username": "u", "password": pw})
                self.assertEqual(r.status_code, 400, f"{pw!r} -> {r.text}")
                self.assertEqual(r.json()["detail"], "密码不能为空")
            with connect(db) as conn:
                self.assertEqual(conn.execute("select count(*) from users where username='u'").fetchone()[0], 0)

    def test_api_reset_password_rejects_non_str_without_side_effect(self):
        with tempfile.TemporaryDirectory() as tmp:
            db, client = self._admin_client(tmp)
            uid = client.post("/api/users", json={"username": "u", "password": "old12345"}).json()["id"]
            other = TestClient(create_app(db))
            other.post("/api/auth/login", json={"username": "u", "password": "old12345"})
            with connect(db) as conn:
                before = conn.execute("select password_hash from users where id=?", (uid,)).fetchone()[0]
            for pw in JSON_NON_STRING_PASSWORDS:
                r = client.post(f"/api/users/{uid}/reset-password", json={"password": pw})
                self.assertEqual(r.status_code, 400, f"{pw!r} -> {r.text}")
            with connect(db) as conn:
                self.assertEqual(conn.execute("select password_hash from users where id=?", (uid,)).fetchone()[0],
                                 before, "拒绝时不得改 hash")
                self.assertEqual(conn.execute("select count(*) from sessions where user_id=?", (uid,)).fetchone()[0], 1,
                                 "拒绝时不得清会话")

    def test_api_change_password_rejects_non_str_without_side_effect(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = _db(tmp)
            with connect(db) as conn:
                auth.create_user(conn, "u", "U", "old12345", role="reception")
                conn.commit()
            client = TestClient(create_app(db))
            client.post("/api/auth/login", json={"username": "u", "password": "old12345"})
            with connect(db) as conn:
                before = conn.execute("select password_hash from users where username='u'").fetchone()[0]
            for pw in JSON_NON_STRING_PASSWORDS:
                r = client.post("/api/auth/change-password",
                                json={"old_password": "old12345", "new_password": pw})
                self.assertEqual(r.status_code, 400, f"{pw!r} -> {r.text}")
                self.assertEqual(r.json()["detail"], "新密码不能为空")
            with connect(db) as conn:
                self.assertEqual(conn.execute("select password_hash from users where username='u'").fetchone()[0],
                                 before, "拒绝时不得改 hash")
            self.assertIsNotNone(client.get("/api/auth/me").json()["user"], "拒绝时不得清会话")

    def test_login_non_str_password_is_generic_401(self):
        # 字符串密码 "1" 的账号,不得被 JSON 数字 1 / 布尔 / 数组 / 对象登进来,也不得 500
        with tempfile.TemporaryDirectory() as tmp:
            db = _db(tmp)
            with connect(db) as conn:
                auth.create_user(conn, "u", "U", "1", role="reception")
                conn.commit()
            client = TestClient(create_app(db))
            for pw in JSON_NON_STRING_PASSWORDS:
                r = client.post("/api/auth/login", json={"username": "u", "password": pw})
                self.assertEqual(r.status_code, 401, f"{pw!r} -> {r.status_code} {r.text}")
                self.assertEqual(r.json()["detail"], "用户名或密码错误")
            self.assertEqual(client.post("/api/auth/login",
                                         json={"username": "u", "password": "1"}).status_code, 200)

    def test_helper_rejects_surrogates(self):
        # 孤立代理项不可 UTF-8 编码 → 不是合法文本;合法 Unicode/首尾空格照旧放行
        for pw in SURROGATE_PASSWORDS:
            self.assertFalse(auth.password_is_nonblank(pw), repr(pw))
        for pw in ["x", "短", " x ", "🦷", "é"]:
            self.assertTrue(auth.password_is_nonblank(pw), repr(pw))

    def test_verify_password_surrogate_is_false_not_raise(self):
        # 不可编码的候选密码 / 畸形存储值都只返回 False,不得抛 UnicodeEncodeError
        h = auth.hash_password("1")
        for pw in SURROGATE_PASSWORDS:
            self.assertFalse(auth.verify_password(pw, h), repr(pw))
        self.assertFalse(auth.verify_password("1", "\ud800$abcd"))
        self.assertTrue(auth.verify_password("1", h))

    def test_create_user_fail_closed_on_surrogate(self):
        # 直接/CLI 入口(绕过 HTTP)共用同一条规则:拒绝且不写库
        with tempfile.TemporaryDirectory() as tmp:
            db = _db(tmp)
            with connect(db) as conn:
                for pw in SURROGATE_PASSWORDS:
                    with self.assertRaises(ValueError, msg=repr(pw)):
                        auth.create_user(conn, "u", "U", pw, role="reception")
                self.assertEqual(conn.execute("select count(*) from users").fetchone()[0], 0,
                                 "拒绝时不得建账号")

    def test_login_surrogate_password_is_generic_401(self):
        # 原始 JSON 里的孤立代理项:通用 401,不得 500、不泄露原因、不建会话
        with tempfile.TemporaryDirectory() as tmp:
            db = _db(tmp)
            with connect(db) as conn:
                auth.create_user(conn, "u", "U", "1", role="reception")
                conn.commit()
            client = TestClient(create_app(db))
            for esc in SURROGATE_JSON_ESCAPES:
                r = _raw_json_post(client, "/api/auth/login",
                                   '{"username": "u", "password": "%s"}' % esc)
                self.assertEqual(r.status_code, 401, f"{esc} -> {r.status_code} {r.text}")
                self.assertEqual(r.json()["detail"], "用户名或密码错误")
            with connect(db) as conn:
                self.assertEqual(conn.execute("select count(*) from sessions").fetchone()[0], 0,
                                 "登录失败不得建会话")
            self.assertEqual(client.post("/api/auth/login",
                                         json={"username": "u", "password": "1"}).status_code, 200)

    def test_api_create_user_rejects_surrogate_without_side_effect(self):
        with tempfile.TemporaryDirectory() as tmp:
            db, client = self._admin_client(tmp)
            for esc in SURROGATE_JSON_ESCAPES:
                r = _raw_json_post(client, "/api/users",
                                   '{"username": "u", "password": "%s"}' % esc)
                self.assertEqual(r.status_code, 400, f"{esc} -> {r.status_code} {r.text}")
            with connect(db) as conn:
                self.assertEqual(conn.execute("select count(*) from users where username='u'").fetchone()[0], 0)

    def test_api_reset_password_rejects_surrogate_without_side_effect(self):
        with tempfile.TemporaryDirectory() as tmp:
            db, client = self._admin_client(tmp)
            uid = client.post("/api/users", json={"username": "u", "password": "old12345"}).json()["id"]
            other = TestClient(create_app(db))
            other.post("/api/auth/login", json={"username": "u", "password": "old12345"})
            with connect(db) as conn:
                before = conn.execute("select password_hash from users where id=?", (uid,)).fetchone()[0]
            for esc in SURROGATE_JSON_ESCAPES:
                r = _raw_json_post(client, f"/api/users/{uid}/reset-password", '{"password": "%s"}' % esc)
                self.assertEqual(r.status_code, 400, f"{esc} -> {r.status_code} {r.text}")
            with connect(db) as conn:
                self.assertEqual(conn.execute("select password_hash from users where id=?", (uid,)).fetchone()[0],
                                 before, "拒绝时不得改 hash")
                self.assertEqual(conn.execute("select count(*) from sessions where user_id=?", (uid,)).fetchone()[0], 1,
                                 "拒绝时不得清会话")

    def test_api_change_password_rejects_surrogate_without_side_effect(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = _db(tmp)
            with connect(db) as conn:
                auth.create_user(conn, "u", "U", "old12345", role="reception")
                conn.commit()
            client = TestClient(create_app(db))
            client.post("/api/auth/login", json={"username": "u", "password": "old12345"})
            with connect(db) as conn:
                before = conn.execute("select password_hash from users where username='u'").fetchone()[0]
            for esc in SURROGATE_JSON_ESCAPES:
                r = _raw_json_post(client, "/api/auth/change-password",
                                   '{"old_password": "old12345", "new_password": "%s"}' % esc)
                self.assertEqual(r.status_code, 400, f"{esc} -> {r.status_code} {r.text}")
                self.assertEqual(r.json()["detail"], "新密码不能为空")
            with connect(db) as conn:
                self.assertEqual(conn.execute("select password_hash from users where username='u'").fetchone()[0],
                                 before, "拒绝时不得改 hash")
            self.assertIsNotNone(client.get("/api/auth/me").json()["user"], "拒绝时不得清会话")

    def _admin_client(self, tmp):
        db = _db(tmp)
        auth.ensure_seed_roles(db)
        with connect(db) as conn:
            auth.create_user(conn, "admin", "管理员", "admin123", role="admin")
            conn.commit()
        client = TestClient(create_app(db))
        client.post("/api/auth/login", json={"username": "admin", "password": "admin123"})
        return db, client

    def test_api_create_user_single_char_logs_in(self):
        with tempfile.TemporaryDirectory() as tmp:
            db, client = self._admin_client(tmp)
            r = client.post("/api/users", json={"username": "u", "password": "短", "role": "reception"})
            self.assertEqual(r.status_code, 200, r.text)
            other = TestClient(create_app(db))
            self.assertEqual(other.post("/api/auth/login",
                                        json={"username": "u", "password": "短"}).status_code, 200)

    def test_api_create_user_rejects_blank_without_side_effect(self):
        with tempfile.TemporaryDirectory() as tmp:
            db, client = self._admin_client(tmp)
            for pw in BLANK_PASSWORDS:
                r = client.post("/api/users", json={"username": "u", "password": pw})
                self.assertEqual(r.status_code, 400, f"{pw!r} -> {r.text}")
            with connect(db) as conn:
                self.assertEqual(conn.execute("select count(*) from users where username='u'").fetchone()[0], 0)

    def test_api_reset_password_single_char_and_blank(self):
        with tempfile.TemporaryDirectory() as tmp:
            db, client = self._admin_client(tmp)
            uid = client.post("/api/users", json={"username": "u", "password": "old12345"}).json()["id"]
            with connect(db) as conn:
                before = conn.execute("select password_hash from users where id=?", (uid,)).fetchone()[0]
            for pw in BLANK_PASSWORDS:
                r = client.post(f"/api/users/{uid}/reset-password", json={"password": pw})
                self.assertEqual(r.status_code, 400, f"{pw!r} -> {r.text}")
            with connect(db) as conn:
                self.assertEqual(conn.execute("select password_hash from users where id=?", (uid,)).fetchone()[0],
                                 before, "拒绝时不得改 hash")
            self.assertEqual(client.post(f"/api/users/{uid}/reset-password", json={"password": "短"}).status_code, 200)
            other = TestClient(create_app(db))
            self.assertEqual(other.post("/api/auth/login",
                                        json={"username": "u", "password": "短"}).status_code, 200)

    def test_api_reset_password_blank_keeps_sessions(self):
        with tempfile.TemporaryDirectory() as tmp:
            db, client = self._admin_client(tmp)
            uid = client.post("/api/users", json={"username": "u", "password": "old12345"}).json()["id"]
            other = TestClient(create_app(db))
            other.post("/api/auth/login", json={"username": "u", "password": "old12345"})
            self.assertEqual(client.post(f"/api/users/{uid}/reset-password", json={"password": "  "}).status_code, 400)
            with connect(db) as conn:
                self.assertEqual(conn.execute("select count(*) from sessions where user_id=?", (uid,)).fetchone()[0], 1,
                                 "拒绝时不得清会话")

    def test_api_change_password_single_char_and_blank(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = _db(tmp)
            with connect(db) as conn:
                auth.create_user(conn, "u", "U", "old12345", role="reception")
                conn.commit()
            client = TestClient(create_app(db))
            client.post("/api/auth/login", json={"username": "u", "password": "old12345"})
            for pw in BLANK_PASSWORDS:
                r = client.post("/api/auth/change-password",
                                json={"old_password": "old12345", "new_password": pw})
                self.assertEqual(r.status_code, 400, f"{pw!r} -> {r.text}")
            self.assertIsNotNone(client.get("/api/auth/me").json()["user"], "拒绝时不得清会话")
            self.assertEqual(client.post("/api/auth/change-password",
                                         json={"old_password": "old12345", "new_password": "短"}).status_code, 200)
            other = TestClient(create_app(db))
            self.assertEqual(other.post("/api/auth/login",
                                        json={"username": "u", "password": "短"}).status_code, 200)

    def test_password_is_validated_not_trimmed(self):
        # 「验证但不裁剪」：" x " 建号 → 只有带空格原文能登,"x" 不行
        with tempfile.TemporaryDirectory() as tmp:
            db, client = self._admin_client(tmp)
            uid = client.post("/api/users", json={"username": "u", "password": " x "}).json()["id"]
            other = TestClient(create_app(db))
            self.assertEqual(other.post("/api/auth/login", json={"username": "u", "password": "x"}).status_code, 401)
            self.assertEqual(other.post("/api/auth/login", json={"username": "u", "password": " x "}).status_code, 200)
            # 改密路径同口径
            self.assertEqual(client.post(f"/api/users/{uid}/reset-password", json={"password": " y "}).status_code, 200)
            self.assertEqual(other.post("/api/auth/login", json={"username": "u", "password": "y"}).status_code, 401)
            self.assertEqual(other.post("/api/auth/login", json={"username": "u", "password": " y "}).status_code, 200)
