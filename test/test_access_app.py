"""access-v3 目标应用装配：显式开关、页面网关与完整登录授权闭环。"""

import json
import sqlite3
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from fastapi.testclient import TestClient

from local_app import access_auth
from local_app import api as api_module
from local_app.api import create_app
from local_app.db import init_db
from local_app.passwords import hash_password


CLINIC_NAME = "合成牙科"
USERNAME = "desk-user"
PASSWORD = "pw"


def _build_target_database(root):
    """只在临时目录构造业务 schema + 已迁到 v3 的访问 schema。"""
    db_path = Path(root) / "target.sqlite3"
    init_db(db_path)
    target_schema = (
        Path(__file__).parents[1]
        / "local_app"
        / "personnel_access_schema.sql"
    ).read_text(encoding="utf-8")

    conn = sqlite3.connect(db_path)
    try:
        conn.execute("pragma foreign_keys = off")
        conn.executescript(
            """
            drop table sessions;
            drop table users;
            drop table staff_members;
            drop table audit_logs;
            """
        )
        conn.executescript(target_schema)
        conn.execute(
            "insert into roles(role_key, name, is_system, sort) "
            "values ('front_desk', '前台', 0, 10)"
        )
        conn.execute(
            "insert into role_permissions(role_key, perm_key) values (?, ?)",
            ("front_desk", "master_data.view"),
        )
        conn.execute(
            "insert into staff_members(staff_id, name, employment_status) "
            "values ('staff-desk', '合成前台', 'employed')"
        )
        conn.execute(
            "insert into staff_role_assignments"
            "(staff_id, role_key, is_primary, created_at) "
            "values ('staff-desk', 'front_desk', 1, '2026-07-16 09:00:00')"
        )
        conn.execute(
            "insert into users"
            "(id, username, display_name, password_hash, staff_id, is_active, "
            "is_system_admin, account_kind) values (?, ?, ?, ?, ?, 1, 0, 'staff')",
            ("user-desk", USERNAME, "合成前台", hash_password(PASSWORD), "staff-desk"),
        )
        conn.execute(
            "insert or replace into app_settings(key, value, updated_at) "
            "values ('clinic', ?, '2026-07-16 09:00:00')",
            (json.dumps({"name": CLINIC_NAME}, ensure_ascii=False),),
        )
        conn.commit()
    finally:
        conn.close()
    return db_path


def _route_count(app, method, path):
    return sum(
        1
        for route in app.routes
        if getattr(route, "path", None) == path
        and method in getattr(route, "methods", set())
    )


def _cookie_was_deleted(response, name):
    return any(
        value.startswith(f"{name}=") and "Max-Age=0" in value
        for value in response.headers.get_list("set-cookie")
    )


class AccessV3AppConstructionTest(unittest.TestCase):
    def test_target_requires_explicit_database(self):
        with self.assertRaises(ValueError):
            create_app(access_v3=True)

    def test_target_rejects_legacy_access_password(self):
        with TemporaryDirectory() as tmp:
            missing = Path(tmp) / "missing.sqlite3"
            with self.assertRaises(ValueError):
                create_app(
                    missing,
                    access_v3=True,
                    access_password="legacy-password",
                )
            self.assertFalse(missing.exists())

    def test_target_construction_does_not_create_or_initialize_database(self):
        with TemporaryDirectory() as tmp:
            missing = Path(tmp) / "missing.sqlite3"
            with mock.patch("local_app.auth.setup_auth") as legacy_setup:
                app = create_app(missing, access_v3=True)
            self.assertIsNotNone(app)
            self.assertFalse(missing.exists())
            legacy_setup.assert_not_called()
            login_page = TestClient(app).get("/")
            self.assertEqual(login_page.status_code, 200)
            self.assertFalse(missing.exists())


class AccessV3AppTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = TemporaryDirectory()
        self.db_path = _build_target_database(self.temp_dir.name)
        self.app = create_app(self.db_path, access_v3=True)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_target_registers_only_four_unique_auth_routes_and_expected_middleware(self):
        expected = {
            ("POST", "/api/auth/login"),
            ("POST", "/api/auth/logout"),
            ("GET", "/api/auth/me"),
            ("POST", "/api/auth/reauth"),
        }
        for method, path in expected:
            with self.subTest(method=method, path=path):
                self.assertEqual(_route_count(self.app, method, path), 1)

        access_auth_routes = {
            (method, route.path)
            for route in self.app.routes
            if getattr(
                getattr(route, "endpoint", None), "__module__", None
            ) == access_auth.__name__
            for method in getattr(route, "methods", set())
        }
        self.assertEqual(access_auth_routes, expected)
        self.assertEqual(_route_count(self.app, "GET", "/api/auth/activity"), 0)

        middleware_names = [
            middleware.cls.__name__ for middleware in self.app.user_middleware
        ]
        self.assertEqual(
            middleware_names[:3],
            [
                "AccessAuthContextMiddleware",
                "AccessPageGateMiddleware",
                "OperationAuditMiddleware",
            ],
        )
        self.assertNotIn("AuthContextMiddleware", middleware_names)
        self.assertNotIn("LegacyAccessBridgeMiddleware", middleware_names)

    def test_anonymous_pages_api_health_and_static_assets(self):
        client = TestClient(self.app)
        for method, path in (
            ("GET", "/"),
            ("HEAD", "/"),
            ("GET", "/index.html"),
            ("HEAD", "/index.html"),
            ("GET", "/__login"),
            ("HEAD", "/__login"),
            ("GET", "/login"),
            ("HEAD", "/login"),
        ):
            with self.subTest(method=method, path=path):
                response = client.request(method, path)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.headers["cache-control"], "no-store")
                if method == "GET":
                    self.assertIn(CLINIC_NAME, response.text)
                    self.assertIn('id="loginForm"', response.text)
                    self.assertNotIn('class="app-shell"', response.text)

        denied = client.get("/api/handle-items")
        self.assertEqual(denied.status_code, 401)
        self.assertEqual(client.get("/api/health").status_code, 200)

        with mock.patch.object(
            access_auth.session_service,
            "resolve_session",
            side_effect=AssertionError("static asset parsed a session"),
        ) as resolve:
            for path in (
                "/app.js",
                "/styles.css",
                "/icon-180.png",
                "/icon.svg",
                "/manifest.webmanifest",
                "/favicon.ico",
            ):
                with self.subTest(path=path):
                    response = client.get(
                        path,
                        headers={
                            "cookie": (
                                f"{access_auth.ACCESS_COOKIE_NAME}={'a' * 64}"
                            )
                        },
                    )
                    self.assertIn(response.status_code, (200, 404))
        resolve.assert_not_called()

    def test_login_shell_me_authorization_and_logout_end_to_end(self):
        client = TestClient(self.app)
        login = client.post(
            "/api/auth/login",
            json={"username": USERNAME, "password": PASSWORD},
        )
        self.assertEqual(login.status_code, 200, login.text)
        self.assertIn(
            f"{access_auth.ACCESS_COOKIE_NAME}=", login.headers["set-cookie"]
        )

        shell = client.get("/")
        self.assertEqual(shell.status_code, 200)
        self.assertIn('class="app-shell"', shell.text)
        self.assertNotIn('id="loginForm"', shell.text)

        me = client.get("/api/auth/me")
        self.assertEqual(me.status_code, 200, me.text)
        self.assertEqual(
            me.json(),
            {
                "user": {
                    "id": "user-desk",
                    "username": USERNAME,
                    "display_name": "合成前台",
                    "staff_id": "staff-desk",
                    "is_system_admin": False,
                    "primary_role": "front_desk",
                    "role_keys": ["front_desk"],
                    "permissions": ["master_data.view"],
                }
            },
        )
        self.assertNotIn("session", me.text)

        allowed = client.get("/api/handle-items")
        self.assertEqual(allowed.status_code, 200, allowed.text)
        denied = client.get("/api/audit-logs")
        self.assertEqual(denied.status_code, 403, denied.text)

        fixed_login = client.get("/login")
        self.assertIn('id="loginForm"', fixed_login.text)
        self.assertNotIn('class="app-shell"', fixed_login.text)

        logout = client.post("/api/auth/logout")
        self.assertEqual(logout.status_code, 200, logout.text)
        self.assertEqual(logout.json(), {"ok": True})
        self.assertIn(
            f"{access_auth.ACCESS_COOKIE_NAME}=", logout.headers["set-cookie"]
        )
        self.assertIn('id="loginForm"', client.get("/").text)
        self.assertEqual(client.get("/api/auth/me").status_code, 401)

    def test_handle_item_management_requires_master_data_manage(self):
        """收费项目管理归 master_data.manage：有权即可全流程，撤权后路由策略先拦、不进 handler。"""
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                "insert into role_permissions(role_key, perm_key) values (?, ?)",
                ("front_desk", "master_data.manage"),
            )
            conn.execute(
                "insert into handle_items"
                "(handle_id, code, name, unit, price, fee_type, stopped) "
                "values ('synthetic-active', 'ACTIVE-100', '合成在用项目', '次', 100, '治疗费', 0)"
            )
            conn.commit()
        finally:
            conn.close()

        valid_item = {
            "name": "合成收费项目",
            "code": "SYNTHETIC-200",
            "unit": "次",
            "price": "200.00",
            "fee_type": "治疗费",
        }

        # 有 master_data.manage 的普通账号(非系统管理员)可走完管理全流程
        manager = TestClient(create_app(self.db_path, access_v3=True))
        login = manager.post(
            "/api/auth/login",
            json={"username": USERNAME, "password": PASSWORD},
        )
        self.assertEqual(login.status_code, 200, login.text)
        self.assertFalse(manager.get("/api/auth/me").json()["user"]["is_system_admin"])

        created = manager.post("/api/handle-items", json=valid_item)
        self.assertEqual(created.status_code, 200, created.text)
        handle_id = created.json()["handle_id"]

        managed = manager.get("/api/handle-items/manage")
        self.assertEqual(managed.status_code, 200, managed.text)
        self.assertIn(handle_id, {item["handle_id"] for item in managed.json()["items"]})

        updated = manager.put(
            f"/api/handle-items/{handle_id}",
            json={**valid_item, "name": "合成收费项目（已修改）", "price": "268.00"},
        )
        self.assertEqual(updated.status_code, 200, updated.text)
        stopped = manager.delete(f"/api/handle-items/{handle_id}")
        self.assertEqual(stopped.status_code, 200, stopped.text)
        restored = manager.post(f"/api/handle-items/{handle_id}/restore")
        self.assertEqual(restored.status_code, 200, restored.text)

        conn = sqlite3.connect(self.db_path)
        try:
            audits = conn.execute(
                "select action, operator, actor_user_id from audit_logs "
                "where entity_type = 'handle_item' and entity_id = ? "
                "order by audit_id",
                (handle_id,),
            ).fetchall()
        finally:
            conn.close()
        self.assertEqual(
            audits,
            [
                ("create_handle_item", USERNAME, "user-desk"),
                ("update_handle_item", USERNAME, "user-desk"),
                ("stop_handle_item", USERNAME, "user-desk"),
                ("restore_handle_item", USERNAME, "user-desk"),
            ],
        )

        # 撤掉 master_data.manage：五个端点全 403,且路由策略先拦下,handler 守卫不得被调用
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                "delete from role_permissions where role_key = ? and perm_key = ?",
                ("front_desk", "master_data.manage"),
            )
            conn.commit()
        finally:
            conn.close()

        revoked = TestClient(
            create_app(self.db_path, access_v3=True), raise_server_exceptions=False
        )
        login = revoked.post(
            "/api/auth/login",
            json={"username": USERNAME, "password": PASSWORD},
        )
        self.assertEqual(login.status_code, 200, login.text)
        self.assertEqual(revoked.get("/api/handle-items").status_code, 200)

        with mock.patch(
            "local_app.routes.master_data.require_perm",
            side_effect=AssertionError("route policy must reject before handler"),
        ) as handler_guard:
            denied = (
                revoked.get("/api/handle-items/manage"),
                revoked.post("/api/handle-items", json=valid_item),
                revoked.put("/api/handle-items/synthetic-active", json=valid_item),
                revoked.delete("/api/handle-items/synthetic-active"),
                revoked.post(f"/api/handle-items/{handle_id}/restore"),
            )
        for response in denied:
            with self.subTest(response=response):
                self.assertEqual(response.status_code, 403, response.text)
                self.assertEqual(response.json(), {"detail": "access_denied"})
        handler_guard.assert_not_called()

    def test_system_admin_me_exposes_exact_role_permissions(self):
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                "insert into role_permissions(role_key, perm_key) values (?, ?)",
                ("front_desk", "staff.view"),
            )
            conn.execute(
                "update users set is_system_admin = 1 where id = 'user-desk'"
            )
            conn.commit()
        finally:
            conn.close()

        client = TestClient(self.app)
        login = client.post(
            "/api/auth/login",
            json={"username": USERNAME, "password": PASSWORD},
        )
        self.assertEqual(login.status_code, 200, login.text)

        me = client.get("/api/auth/me")
        self.assertEqual(me.status_code, 200, me.text)
        user = me.json()["user"]
        self.assertIs(user["is_system_admin"], True)
        self.assertEqual(
            user["permissions"],
            ["master_data.view", "staff.view"],
        )
        self.assertNotIn("role_permissions", user)

        admin_only_route = client.get("/api/role-permissions")
        self.assertEqual(admin_only_route.status_code, 200, admin_only_route.text)

    def test_target_app_uses_target_staff_account_and_role_routes(self):
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                "update users set is_system_admin = 1 where id = 'user-desk'"
            )
            conn.commit()
        finally:
            conn.close()

        client = TestClient(create_app(self.db_path, access_v3=True))
        login = client.post(
            "/api/auth/login",
            json={"username": USERNAME, "password": PASSWORD},
        )
        self.assertEqual(login.status_code, 200, login.text)

        staff = client.get("/api/staff-members")
        self.assertEqual(staff.status_code, 200, staff.text)
        self.assertEqual(staff.json()["members"][0]["staff_id"], "staff-desk")
        self.assertNotIn("active", staff.json()["members"][0])

        users = client.get("/api/users")
        self.assertEqual(users.status_code, 200, users.text)
        self.assertEqual(users.json()["users"][0]["id"], "user-desk")
        self.assertNotIn("role", users.json()["users"][0])

        roles = client.get("/api/role-permissions")
        self.assertEqual(roles.status_code, 200, roles.text)
        self.assertEqual(len(roles.json()["perms"]), 99)
        self.assertNotIn(
            "admin", {role["role_key"] for role in roles.json()["roles"]}
        )

    def test_high_risk_routes_stop_before_handlers_until_password_is_recent(self):
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                "update users set is_system_admin = 1 where id = 'user-desk'"
            )
            conn.commit()
        finally:
            conn.close()

        client = TestClient(self.app)
        login = client.post(
            "/api/auth/login",
            json={"username": USERNAME, "password": PASSWORD},
        )
        self.assertEqual(login.status_code, 200, login.text)

        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                "update sessions set reauthenticated_at = "
                "'2000-01-01T00:00:00Z'"
            )
            audit_count = conn.execute(
                "select count(*) from audit_logs"
            ).fetchone()[0]
            conn.commit()
        finally:
            conn.close()

        blocked = (
            client.delete("/api/appointments/missing"),
            client.post(
                "/api/bills/missing/refund",
                json={"refund_reason": "合成测试", "request_id": "risk-1"},
            ),
            client.post(
                "/api/recycle-bin/restore",
                json={"entity_type": "bill", "entity_id": "missing"},
            ),
        )
        for response in blocked:
            with self.subTest(response=response):
                self.assertEqual(response.status_code, 403, response.text)
                self.assertEqual(
                    response.json(),
                    {"detail": "recent_reauthentication_required"},
                )

        conn = sqlite3.connect(self.db_path)
        try:
            self.assertEqual(
                conn.execute("select count(*) from audit_logs").fetchone()[0],
                audit_count,
            )
        finally:
            conn.close()

        reauth = client.post(
            "/api/auth/reauth", json={"password": PASSWORD}
        )
        self.assertEqual(reauth.status_code, 200, reauth.text)
        self.assertEqual(reauth.json(), {"ok": True})

        self.assertEqual(
            client.delete("/api/appointments/missing").status_code, 404
        )
        self.assertEqual(
            client.post(
                "/api/bills/missing/refund",
                json={"refund_reason": "合成测试", "request_id": "risk-2"},
            ).status_code,
            404,
        )
        self.assertEqual(
            client.post(
                "/api/recycle-bin/restore",
                json={"entity_type": "bill", "entity_id": "missing"},
            ).status_code,
            404,
        )

    def test_assistant_cannot_open_finance_or_performance_data(self):
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                "insert into roles(role_key, name, is_system, sort) "
                "values ('assistant', '助理', 0, 20)"
            )
            conn.execute(
                "delete from staff_role_assignments "
                "where staff_id = 'staff-desk'"
            )
            conn.execute(
                "insert into staff_role_assignments"
                "(staff_id, role_key, is_primary, created_at) "
                "values ('staff-desk', 'assistant', 1, '2026-07-16 09:00:00')"
            )
            conn.execute(
                "insert into role_permissions(role_key, perm_key) values (?, ?)",
                ("assistant", "patient.profile.view"),
            )
            conn.commit()
        finally:
            conn.close()

        client = TestClient(self.app)
        login = client.post(
            "/api/auth/login",
            json={"username": USERNAME, "password": PASSWORD},
        )
        self.assertEqual(login.status_code, 200, login.text)
        me = client.get("/api/auth/me")
        self.assertEqual(me.status_code, 200, me.text)
        self.assertEqual(me.json()["user"]["primary_role"], "assistant")
        self.assertEqual(
            me.json()["user"]["permissions"], ["patient.profile.view"]
        )
        self.assertEqual(client.get("/api/patients").status_code, 200)

        for path in (
            "/api/bills",
            "/api/reports/income",
            "/api/reports/staff-workload",
            "/api/store-stats/daily-settlement",
        ):
            with self.subTest(path=path):
                denied = client.get(path)
                self.assertEqual(denied.status_code, 403, denied.text)
                self.assertEqual(denied.json(), {"detail": "access_denied"})

    def test_departure_immediately_disables_login_session_and_records_actor(self):
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                "update users set is_system_admin = 1 where id = 'user-desk'"
            )
            conn.execute(
                "insert into roles(role_key, name, is_system, sort) "
                "values ('assistant', '助理', 0, 20)"
            )
            conn.execute(
                "insert into staff_members"
                "(staff_id, name, employment_status) "
                "values ('staff-assistant', '合成助理', 'employed')"
            )
            conn.execute(
                "insert into staff_role_assignments"
                "(staff_id, role_key, is_primary, created_at) "
                "values ('staff-assistant', 'assistant', 1, "
                "'2026-07-16 09:00:00')"
            )
            conn.execute(
                "insert into users"
                "(id, username, display_name, password_hash, staff_id, "
                "is_active, is_system_admin, account_kind) "
                "values ('user-assistant', 'assistant-user', '合成助理', ?, "
                "'staff-assistant', 1, 0, 'staff')",
                (hash_password("assistant-pw"),),
            )
            conn.commit()
        finally:
            conn.close()

        admin = TestClient(self.app)
        assistant = TestClient(self.app)
        self.assertEqual(
            admin.post(
                "/api/auth/login",
                json={"username": USERNAME, "password": PASSWORD},
            ).status_code,
            200,
        )
        self.assertEqual(
            assistant.post(
                "/api/auth/login",
                json={"username": "assistant-user", "password": "assistant-pw"},
            ).status_code,
            200,
        )

        departure = admin.request(
            "DELETE",
            "/api/staff-members/staff-assistant",
            json={"reason": "合成验收离职"},
        )
        self.assertEqual(departure.status_code, 200, departure.text)
        self.assertTrue(departure.json()["account_disabled"])
        self.assertEqual(assistant.get("/api/auth/me").status_code, 401)
        self.assertEqual(
            assistant.post(
                "/api/auth/login",
                json={"username": "assistant-user", "password": "assistant-pw"},
            ).status_code,
            401,
        )

        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            member = conn.execute(
                "select employment_status, left_reason from staff_members "
                "where staff_id = 'staff-assistant'"
            ).fetchone()
            user = conn.execute(
                "select is_active from users where id = 'user-assistant'"
            ).fetchone()
            sessions = conn.execute(
                "select count(*) from sessions where user_id = 'user-assistant'"
            ).fetchone()[0]
            audit = conn.execute(
                "select action, operator, actor_user_id, created_at, reason "
                "from audit_logs where action = 'staff.employment.left'"
            ).fetchone()
        finally:
            conn.close()
        self.assertEqual(member["employment_status"], "left")
        self.assertEqual(member["left_reason"], "合成验收离职")
        self.assertEqual(user["is_active"], 0)
        self.assertEqual(sessions, 0)
        self.assertEqual(audit["action"], "staff.employment.left")
        self.assertEqual(audit["operator"], USERNAME)
        self.assertEqual(audit["actor_user_id"], "user-desk")
        self.assertTrue(audit["created_at"])
        self.assertEqual(audit["reason"], "合成验收离职")

    def test_refund_records_who_when_and_what_in_target_audit(self):
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                "update users set is_system_admin = 1 where id = 'user-desk'"
            )
            conn.execute(
                "insert into patients"
                "(patient_identity, display_name, updated_at, current_hash) "
                "values ('patient-refund', '合成患者', "
                "'2026-07-16 09:00:00', 'synthetic')"
            )
            conn.execute(
                "insert into bills"
                "(bill_id, patient_identity, total_fee, paid_fee, state, updated_at) "
                "values ('local-bill-acceptance', 'patient-refund', 88, 88, "
                "'paid', '2026-07-16 09:00:00')"
            )
            conn.commit()
        finally:
            conn.close()

        client = TestClient(self.app)
        self.assertEqual(
            client.post(
                "/api/auth/login",
                json={"username": USERNAME, "password": PASSWORD},
            ).status_code,
            200,
        )
        refund = client.post(
            "/api/bills/local-bill-acceptance/refund",
            json={
                "refund_reason": "合成验收退费",
                "refund_method": "现金",
                "request_id": "acceptance-refund-1",
            },
        )
        self.assertEqual(refund.status_code, 200, refund.text)
        self.assertEqual(refund.json()["refund_amount"], 88)

        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            audit = conn.execute(
                "select action, operator, actor_user_id, created_at, new_json "
                "from audit_logs where entity_type = 'bill' "
                "and entity_id = 'local-bill-acceptance' and action = 'refund'"
            ).fetchone()
        finally:
            conn.close()
        self.assertEqual(audit["action"], "refund")
        self.assertEqual(audit["operator"], USERNAME)
        self.assertEqual(audit["actor_user_id"], "user-desk")
        self.assertTrue(audit["created_at"])
        self.assertIn("合成验收退费", audit["new_json"])

    def test_legacy_and_expired_new_cookies_are_rejected_and_cleared_on_page(self):
        legacy = TestClient(self.app).get(
            "/",
            headers={"cookie": "dl_session=legacy; dental_auth=older"},
        )
        self.assertEqual(legacy.status_code, 200)
        self.assertIn('id="loginForm"', legacy.text)
        legacy_headers = legacy.headers.get_list("set-cookie")
        self.assertTrue(
            _cookie_was_deleted(legacy, "dl_session"),
            legacy_headers,
        )
        self.assertTrue(
            _cookie_was_deleted(legacy, "dental_auth"),
            legacy_headers,
        )

        expired = TestClient(self.app).get(
            "/",
            headers={
                "cookie": f"{access_auth.ACCESS_COOKIE_NAME}={'f' * 64}"
            },
        )
        self.assertEqual(expired.status_code, 200)
        self.assertIn('id="loginForm"', expired.text)
        self.assertTrue(
            _cookie_was_deleted(expired, access_auth.ACCESS_COOKIE_NAME),
            expired.headers.get_list("set-cookie"),
        )

        legacy_api = TestClient(self.app).get(
            "/api/handle-items",
            headers={"cookie": "dl_session=legacy"},
        )
        self.assertEqual(legacy_api.status_code, 401)
        self.assertTrue(_cookie_was_deleted(legacy_api, "dl_session"))


class AccessV3DefaultModeRegressionTest(unittest.TestCase):
    def test_default_mode_still_installs_legacy_auth_only(self):
        with TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "legacy.sqlite3"
            init_db(db_path)
            app = create_app(db_path)
        self.assertEqual(_route_count(app, "POST", "/api/auth/login"), 1)
        self.assertEqual(_route_count(app, "POST", "/api/auth/logout"), 1)
        self.assertEqual(_route_count(app, "GET", "/api/auth/me"), 1)
        self.assertEqual(_route_count(app, "POST", "/api/auth/reauth"), 0)
        middleware_names = [
            middleware.cls.__name__ for middleware in app.user_middleware
        ]
        self.assertIn("AuthContextMiddleware", middleware_names)
        self.assertIn("LegacyAccessBridgeMiddleware", middleware_names)
        self.assertNotIn("AccessAuthContextMiddleware", middleware_names)


if __name__ == "__main__":
    unittest.main()
