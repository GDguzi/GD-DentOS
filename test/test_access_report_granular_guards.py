"""v3 细分报表权限 handler 守卫 + pre-v3 旧模式兼容测试（全部 TemporaryDirectory 合成库）。

背景：收银/欠款/绩效/运营报表 handler 旧守卫 require_perm("report.view") 在 v3 目标模式下
经 auth._TARGET_HAS_PERM_ALIASES 被翻译成 report.finance.view——路由策略层本已按细分键
（report.cashier.view 等）放行的用户，到 handler 被 403 误拦（双重标准）。
修复：handler 改用 require_access(细分新键)，与 access_policy.ROUTE_POLICY 逐路由对齐；
pre-v3 旧模式经 _LEGACY_PERMISSION_ALIASES 把细分新键翻回 report.view 旧键，持旧键用户不受影响。
"""
import sqlite3
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from fastapi import FastAPI
from fastapi.testclient import TestClient

from local_app import access_authorization as authorization
from local_app import auth as legacy_auth
from local_app.api import create_app
from local_app.db import connect, init_db
from local_app.passwords import hash_password


# (路由, 策略层/handler 应对齐的细分权限键)——与 access_policy.ROUTE_POLICY 一致
REPORT_ROUTES = (
    ("/api/reports/income", "report.finance.view"),
    ("/api/reports/income-monthly", "report.finance.view"),
    ("/api/store-stats/daily-settlement", "report.finance.view"),
    ("/api/reports/arrears", "report.arrears.view"),
    ("/api/reports/cashier", "report.cashier.view"),
    ("/api/reports/cashier-query", "report.cashier.view"),
    ("/api/reports/today-collection", "report.cashier.view"),
    ("/api/reports/staff-workload", "report.performance.view"),
    ("/api/store-stats/role-perf", "report.performance.view"),
    ("/api/reports/handle-category", "report.operations.view"),
    ("/api/store-stats/reconcile-calendar", "report.operations.view"),
    ("/api/store-stats/clinic-log", "report.operations.view"),
)

GRANULAR_REPORT_KEYS = (
    "report.finance.view",
    "report.arrears.view",
    "report.cashier.view",
    "report.performance.view",
    "report.operations.view",
)


def _build_target_database(root):
    """业务 schema + v3 访问 schema（同 test_access_app 的装配口径）。"""
    db_path = Path(root) / "target.sqlite3"
    init_db(db_path)
    target_schema = (
        Path(__file__).parents[1] / "local_app" / "personnel_access_schema.sql"
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
        conn.commit()
    finally:
        conn.close()
    return db_path


def _add_target_user(db_path, username, role_key, permissions, *, system_admin=False):
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "insert into roles(role_key, name, is_system, sort) values (?, ?, 0, 99)",
            (role_key, role_key),
        )
        conn.executemany(
            "insert into role_permissions(role_key, perm_key) values (?, ?)",
            [(role_key, permission) for permission in permissions],
        )
        conn.execute(
            "insert into staff_members(staff_id, name, employment_status) "
            "values (?, ?, 'employed')",
            ("staff-" + username, username),
        )
        conn.execute(
            "insert into staff_role_assignments"
            "(staff_id, role_key, is_primary, created_at) "
            "values (?, ?, 1, '2026-07-18 09:00:00')",
            ("staff-" + username, role_key),
        )
        conn.execute(
            "insert into users"
            "(id, username, display_name, password_hash, staff_id, is_active, "
            "is_system_admin, account_kind) values (?, ?, ?, ?, ?, 1, ?, 'staff')",
            (
                "user-" + username,
                username,
                username,
                hash_password("pw-" + username),
                "staff-" + username,
                1 if system_admin else 0,
            ),
        )
        conn.commit()
    finally:
        conn.close()


class ReportGranularGuardV3Test(unittest.TestCase):
    """v3 目标模式：handler 守卫必须与路由策略层同键，不得双重标准。"""

    USERS = (
        ("cashier-user", "cashier-role", ("report.cashier.view",), False),
        ("arrears-user", "arrears-role", ("report.arrears.view",), False),
        ("finance-user", "finance-role", ("report.finance.view",), False),
        ("perf-user", "perf-role", ("report.performance.view",), False),
        ("ops-user", "ops-role", ("report.operations.view",), False),
        ("plain-user", "plain-role", ("patient.profile.view",), False),
        ("boss-user", "boss-role", (), True),
    )

    def setUp(self):
        self.temp_dir = TemporaryDirectory()
        self.db_path = _build_target_database(self.temp_dir.name)
        for username, role_key, permissions, system_admin in self.USERS:
            _add_target_user(
                self.db_path,
                username,
                role_key,
                permissions,
                system_admin=system_admin,
            )
        self.app = create_app(self.db_path, access_v3=True)
        self.clients = {}
        for username, _role, _permissions, _admin in self.USERS:
            client = TestClient(self.app)
            login = client.post(
                "/api/auth/login",
                json={"username": username, "password": "pw-" + username},
            )
            self.assertEqual(login.status_code, 200, login.text)
            self.clients[username] = client

    def tearDown(self):
        self.temp_dir.cleanup()

    def assert_allowed(self, username, path):
        response = self.clients[username].get(path)
        self.assertEqual(response.status_code, 200, f"{username} GET {path}: {response.text}")

    def assert_denied(self, username, path):
        response = self.clients[username].get(path)
        self.assertEqual(response.status_code, 403, f"{username} GET {path}: {response.text}")
        # 与路由策略层同一拒绝口径，不得出现 handler 守卫的第二套标准
        self.assertEqual(response.json(), {"detail": "access_denied"})

    def test_cashier_grant_opens_only_cashier_routes(self):
        for path, key in REPORT_ROUTES:
            with self.subTest(path=path):
                if key == "report.cashier.view":
                    self.assert_allowed("cashier-user", path)
                else:
                    self.assert_denied("cashier-user", path)

    def test_arrears_grant_opens_only_arrears_route(self):
        self.assert_allowed("arrears-user", "/api/reports/arrears")
        self.assert_denied("arrears-user", "/api/reports/cashier")
        self.assert_denied("arrears-user", "/api/reports/income")

    def test_finance_grant_opens_finance_routes_but_not_granular_ones(self):
        # 细分互不把门：财务报表本体归 report.finance.view，不能顺带开收银/欠款
        self.assert_allowed("finance-user", "/api/reports/income")
        self.assert_allowed("finance-user", "/api/reports/income-monthly")
        self.assert_allowed("finance-user", "/api/store-stats/daily-settlement")
        self.assert_denied("finance-user", "/api/reports/cashier")
        self.assert_denied("finance-user", "/api/reports/arrears")

    def test_performance_grant_opens_only_performance_routes(self):
        self.assert_allowed("perf-user", "/api/reports/staff-workload")
        self.assert_allowed("perf-user", "/api/store-stats/role-perf")
        self.assert_denied("perf-user", "/api/reports/cashier")

    def test_operations_grant_opens_only_operations_routes(self):
        for path, key in REPORT_ROUTES:
            with self.subTest(path=path):
                if key == "report.operations.view":
                    self.assert_allowed("ops-user", path)
                else:
                    self.assert_denied("ops-user", path)

    def test_user_without_any_report_key_denied_everywhere(self):
        for path, _key in REPORT_ROUTES:
            with self.subTest(path=path):
                self.assert_denied("plain-user", path)

    def test_system_admin_passes_all_report_routes(self):
        for path, _key in REPORT_ROUTES:
            with self.subTest(path=path):
                self.assert_allowed("boss-user", path)


class ReportLegacyAliasBridgeTest(unittest.TestCase):
    """_LEGACY_PERMISSION_ALIASES：细分报表新键在 pre-v3 旧模式必须翻回 report.view。

    require_access 在旧模式遇映射表外的新键会 403 fail-closed（拒全）——
    本任务用到的每个新键都必须有映射，不得静默拒全，也不得静默放行。
    """

    def test_every_granular_report_key_maps_to_legacy_report_view(self):
        for key in GRANULAR_REPORT_KEYS:
            with self.subTest(key=key):
                self.assertEqual(
                    authorization._LEGACY_PERMISSION_ALIASES.get(key),
                    "report.view",
                )

    def test_bridge_require_access_distinguishes_legacy_report_view_holders(self):
        with TemporaryDirectory() as directory:
            db_path = Path(directory) / "legacy.sqlite3"
            init_db(db_path)
            legacy_auth.ensure_seed_roles(db_path)
            with connect(db_path) as conn:
                conn.execute(
                    "insert into roles(role_key, name, is_system) "
                    "values ('reporter', '报表员', 0)"
                )
                conn.execute(
                    "insert into role_permissions(role_key, perm_key) "
                    "values ('reporter', 'report.view')"
                )
                legacy_auth.create_user(conn, "rep", "报表员", "pw", role="reporter")
                conn.execute(
                    "insert into roles(role_key, name, is_system) "
                    "values ('clerk', '前台', 0)"
                )
                legacy_auth.create_user(conn, "clerk", "前台", "pw", role="clerk")
                conn.commit()

            app = FastAPI()

            @app.get("/api/legacy-report-probe")
            def probe(key: str):
                authorization.require_access(key)
                return {"ok": True}

            legacy_auth.setup_auth(app, db_path, require_login=True)
            rep = TestClient(app)
            clerk = TestClient(app)
            self.assertEqual(
                rep.post(
                    "/api/auth/login",
                    json={"username": "rep", "password": "pw"},
                ).status_code,
                200,
            )
            self.assertEqual(
                clerk.post(
                    "/api/auth/login",
                    json={"username": "clerk", "password": "pw"},
                ).status_code,
                200,
            )

            for key in GRANULAR_REPORT_KEYS:
                with self.subTest(key=key):
                    # 持 report.view 旧键 → 别名将新键翻回旧键 → 放行
                    self.assertEqual(
                        rep.get("/api/legacy-report-probe", params={"key": key}).status_code,
                        200,
                    )
                    # 未持旧键 → 403（不得静默放行）
                    self.assertEqual(
                        clerk.get("/api/legacy-report-probe", params={"key": key}).status_code,
                        403,
                    )


class ReportGuardLegacyCompatTest(unittest.TestCase):
    """pre-v3 旧模式端到端：handler 改 require_access 后，持 report.view 旧键用户不受影响。"""

    def _db(self, root):
        db_path = Path(root) / "legacy.sqlite3"
        init_db(db_path)
        legacy_auth.ensure_seed_roles(db_path)
        with connect(db_path) as conn:
            legacy_auth.create_user(conn, "admin", "管理员", "admin123", role="admin")
            conn.execute(
                "insert into roles(role_key, name, is_system) "
                "values ('reporter', '报表员', 0)"
            )
            conn.execute(
                "insert into role_permissions(role_key, perm_key) "
                "values ('reporter', 'report.view')"
            )
            legacy_auth.create_user(conn, "rep", "报表员", "pw123456", role="reporter")
            conn.execute(
                "insert into roles(role_key, name, is_system) "
                "values ('clerk', '前台', 0)"
            )
            conn.execute(
                "insert into role_permissions(role_key, perm_key) "
                "values ('clerk', 'patient.view')"
            )
            legacy_auth.create_user(conn, "clerk", "前台", "pw123456", role="clerk")
            conn.commit()
        return db_path

    def _login(self, db_path, username, password):
        client = TestClient(create_app(db_path, require_login=True))
        login = client.post(
            "/api/auth/login",
            json={"username": username, "password": password},
        )
        self.assertEqual(login.status_code, 200, login.text)
        return client

    def test_legacy_report_view_holder_keeps_all_report_routes(self):
        with TemporaryDirectory() as directory:
            db_path = self._db(directory)
            rep = self._login(db_path, "rep", "pw123456")
            for path, _key in REPORT_ROUTES:
                with self.subTest(path=path):
                    response = rep.get(path)
                    self.assertEqual(response.status_code, 200, f"GET {path}: {response.text}")

    def test_legacy_role_without_report_view_still_denied(self):
        with TemporaryDirectory() as directory:
            db_path = self._db(directory)
            clerk = self._login(db_path, "clerk", "pw123456")
            for path in (
                "/api/reports/cashier",
                "/api/reports/arrears",
                "/api/reports/income",
                "/api/store-stats/role-perf",
            ):
                with self.subTest(path=path):
                    self.assertEqual(clerk.get(path).status_code, 403)

    def test_legacy_admin_passes(self):
        with TemporaryDirectory() as directory:
            db_path = self._db(directory)
            admin = self._login(db_path, "admin", "admin123")
            for path in ("/api/reports/cashier", "/api/reports/arrears"):
                with self.subTest(path=path):
                    self.assertEqual(admin.get(path).status_code, 200)


if __name__ == "__main__":
    unittest.main()
