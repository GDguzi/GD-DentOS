"""Central v3 authorization runtime tests using only synthetic SQLite data."""
import asyncio
import ast
import inspect
import sqlite3
import unittest
from dataclasses import FrozenInstanceError
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from fastapi import Depends, FastAPI, HTTPException
from fastapi.testclient import TestClient

from local_app import access_auth
from local_app import access_authorization as authorization
from local_app import access_service
from local_app import auth as legacy_auth
from local_app.access_policy import ALL_PERMISSIONS, RoutePolicy
from local_app.db import connect as legacy_connect
from local_app.db import init_db


UTC = timezone.utc
NOW = datetime(2026, 7, 16, 12, 0, 0, tzinfo=UTC)


def policy(policy_type, permissions=(), *, method="GET", path="/synthetic"):
    return RoutePolicy(
        method,
        path,
        "synthetic.py",
        "synthetic_endpoint",
        policy_type,
        tuple(permissions),
        None,
    )


class SyntheticAuthorizationDatabase(unittest.TestCase):
    def setUp(self):
        self.temp_dir = TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "synthetic access.sqlite"
        conn = sqlite3.connect(self.db_path)
        conn.executescript(
            """
            create table staff_members (
                staff_id text primary key,
                name text not null,
                employment_status text not null
            );
            create table users (
                id text primary key,
                username text not null,
                display_name text not null,
                staff_id text,
                is_active integer not null,
                is_system_admin integer not null,
                account_kind text not null
            );
            create table staff_role_assignments (
                staff_id text not null,
                role_key text not null,
                is_primary integer not null default 0,
                primary key (staff_id, role_key)
            );
            create table role_permissions (
                role_key text not null,
                perm_key text not null,
                primary key (role_key, perm_key)
            );
            """
        )
        conn.close()

    def tearDown(self):
        self.temp_dir.cleanup()

    def add_staff_user(
        self,
        *,
        user_id="u1",
        username="alice",
        display_name="Alice",
        staff_id="s1",
        is_active=1,
        is_system_admin=0,
        employment_status="employed",
        account_kind="staff",
    ):
        conn = sqlite3.connect(self.db_path)
        if staff_id is not None:
            conn.execute(
                "insert into staff_members(staff_id, name, employment_status) "
                "values (?, ?, ?)",
                (staff_id, display_name, employment_status),
            )
        conn.execute(
            "insert into users(id, username, display_name, staff_id, is_active, "
            "is_system_admin, account_kind) values (?, ?, ?, ?, ?, ?, ?)",
            (
                user_id,
                username,
                display_name,
                staff_id,
                is_active,
                is_system_admin,
                account_kind,
            ),
        )
        conn.commit()
        conn.close()

    def grant(self, staff_id, role_key, *permissions, is_primary=False):
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "insert into staff_role_assignments(staff_id, role_key, is_primary) "
            "values (?, ?, ?)",
            (staff_id, role_key, 1 if is_primary else 0),
        )
        conn.executemany(
            "insert into role_permissions(role_key, perm_key) values (?, ?)",
            [(role_key, permission) for permission in permissions],
        )
        conn.commit()
        conn.close()


class PrincipalLoadingTests(SyntheticAuthorizationDatabase):
    def test_role_permissions_are_required_for_manual_principals(self):
        with self.assertRaises(TypeError):
            authorization.AccessPrincipal(
                user_id="u1",
                username="alice",
                display_name="Alice",
                staff_id="s1",
                is_system_admin=False,
                primary_role="front",
                role_keys=("front",),
                permissions=frozenset({"staff.view"}),
            )

    def test_loads_frozen_principal_and_reuses_effective_permission_union(self):
        self.add_staff_user()
        self.grant(
            "s1", "front", "appointment.view", "not.in.catalog", is_primary=True
        )
        self.grant("s1", "finance", "billing.view")

        with mock.patch.object(
            access_service,
            "effective_permissions",
            wraps=access_service.effective_permissions,
        ) as effective:
            principal = authorization.load_access_principal(self.db_path, "u1")

        effective.assert_called_once()
        self.assertEqual(principal.user_id, "u1")
        self.assertEqual(principal.username, "alice")
        self.assertEqual(principal.display_name, "Alice")
        self.assertEqual(principal.staff_id, "s1")
        self.assertIs(principal.is_system_admin, False)
        self.assertEqual(principal.primary_role, "front")
        self.assertEqual(principal.role_keys, ("front", "finance"))
        self.assertEqual(
            principal.permissions, frozenset({"appointment.view", "billing.view"})
        )
        self.assertEqual(
            principal.role_permissions,
            frozenset({"appointment.view", "billing.view"}),
        )
        self.assertIs(type(principal.permissions), frozenset)
        self.assertIs(type(principal.role_permissions), frozenset)
        with self.assertRaises(FrozenInstanceError):
            principal.username = "changed"

    def test_staff_system_admin_keeps_role_union_separate_from_authorization(self):
        self.add_staff_user(is_system_admin=1)
        self.grant(
            "s1", "front", "staff.view", "not.in.catalog", is_primary=True
        )

        with mock.patch.object(
            access_service,
            "effective_permissions",
            wraps=access_service.effective_permissions,
        ) as effective:
            principal = authorization.load_access_principal(self.db_path, "u1")

        effective.assert_called_once()
        self.assertIs(principal.is_system_admin, True)
        self.assertEqual(principal.permissions, ALL_PERMISSIONS)
        self.assertEqual(
            principal.role_permissions,
            frozenset({"staff.view"}),
        )

    def test_staffless_system_admin_receives_all_normal_permissions(self):
        self.add_staff_user(
            staff_id=None,
            is_system_admin=1,
            account_kind="independent_admin",
        )

        principal = authorization.load_access_principal(self.db_path, "u1")

        self.assertIs(principal.is_system_admin, True)
        self.assertIsNone(principal.staff_id)
        self.assertIsNone(principal.primary_role)
        self.assertEqual(principal.role_keys, ())
        self.assertEqual(principal.permissions, ALL_PERMISSIONS)
        self.assertEqual(principal.role_permissions, frozenset())

    def test_inactive_left_archived_and_staffless_non_admin_are_invalid(self):
        cases = (
            {"user_id": "inactive", "staff_id": "si", "is_active": 0},
            {
                "user_id": "left",
                "staff_id": "sl",
                "employment_status": "left",
            },
            {
                "user_id": "archived",
                "staff_id": "sa",
                "employment_status": "archived",
            },
            {
                "user_id": "staffless",
                "staff_id": None,
                "account_kind": "staff",
            },
        )
        for index, values in enumerate(cases):
            values.setdefault("username", "user%d" % index)
            self.add_staff_user(**values)

        for user_id in ("inactive", "left", "archived", "staffless", "missing"):
            with self.subTest(user_id=user_id):
                self.assertIsNone(
                    authorization.load_access_principal(self.db_path, user_id)
                )

    def test_missing_database_is_not_created_and_fails_closed(self):
        missing = Path(self.temp_dir.name) / "missing.sqlite"

        with self.assertRaises(authorization.AccessAuthorizationError) as caught:
            authorization.load_access_principal(missing, "u1")

        self.assertEqual(caught.exception.status_code, 500)
        self.assertEqual(caught.exception.code, "authorization_unavailable")
        self.assertFalse(missing.exists())

    def test_principal_loader_connection_cannot_write_and_data_is_unchanged(self):
        self.add_staff_user()
        before = self.db_path.read_bytes()
        original = access_service.effective_permissions

        def write_probe(conn, user_id):
            with self.assertRaises(sqlite3.OperationalError):
                conn.execute(
                    "update users set display_name = ? where id = ?",
                    ("MUTATED", user_id),
                )
            return original(conn, user_id)

        with mock.patch.object(
            access_service, "effective_permissions", side_effect=write_probe
        ):
            principal = authorization.load_access_principal(self.db_path, "u1")

        self.assertEqual(principal.display_name, "Alice")
        self.assertEqual(self.db_path.read_bytes(), before)

    def test_malformed_role_context_fails_closed(self):
        cases = {
            "duplicate": [
                {"role_key": "front", "is_primary": 0},
                {"role_key": "front", "is_primary": 0},
            ],
            "two-primary": [
                {"role_key": "front", "is_primary": 1},
                {"role_key": "finance", "is_primary": 1},
            ],
            "bad-primary": [{"role_key": "front", "is_primary": 2}],
            "blank-key": [{"role_key": "   ", "is_primary": 0}],
            "surrogate-key": [{"role_key": "\ud800", "is_primary": 0}],
        }

        class FakeResult:
            def __init__(self, rows):
                self.rows = rows

            def fetchall(self):
                return self.rows

        class FakeConnection:
            def __init__(self, rows):
                self.rows = rows

            def execute(self, statement, parameters):
                self.statement = statement
                self.parameters = parameters
                return FakeResult(self.rows)

        for name, rows in cases.items():
            with self.subTest(name=name), self.assertRaises(
                authorization.AccessAuthorizationError
            ) as caught:
                authorization._load_role_context(FakeConnection(rows), "s1")
            self.assertEqual(caught.exception.status_code, 500)


class PurePolicyAuthorizationTests(unittest.TestCase):
    def principal(self, *permissions, admin=False):
        return authorization.AccessPrincipal(
            user_id="u1",
            username="alice",
            display_name="Alice",
            staff_id="s1",
            is_system_admin=admin,
            primary_role="front",
            role_keys=("front",),
            permissions=frozenset(permissions),
            role_permissions=frozenset(permissions),
        )

    def assert_denied(self, expected_status, target_policy, principal):
        with self.assertRaises(authorization.AccessAuthorizationError) as caught:
            authorization.authorize_policy(target_policy, principal)
        self.assertEqual(caught.exception.status_code, expected_status)

    def test_public_and_public_auth_are_anonymous(self):
        for policy_type in ("PUBLIC", "PUBLIC_AUTH"):
            with self.subTest(policy_type=policy_type):
                self.assertIsNone(
                    authorization.authorize_policy(policy(policy_type), None)
                )

    def test_all_three_authenticated_policy_types_require_login_only(self):
        for policy_type in (
            "SELF_AUTH",
            "AUTHENTICATED_COMMON",
            "AUTHENTICATED_DIRECTORY",
        ):
            with self.subTest(policy_type=policy_type):
                self.assert_denied(401, policy(policy_type), None)
                self.assertIsNone(
                    authorization.authorize_policy(
                        policy(policy_type), self.principal()
                    )
                )

    def test_permission_requires_its_single_key(self):
        target = policy("PERMISSION", ("billing.view",))
        self.assert_denied(403, target, self.principal("appointment.view"))
        self.assertIsNone(
            authorization.authorize_policy(target, self.principal("billing.view"))
        )

    def test_all_of_requires_every_key(self):
        target = policy(
            "ALL_OF", ("medical_record.view", "medical_record.ai.review")
        )
        self.assert_denied(403, target, self.principal("medical_record.view"))
        self.assertIsNone(
            authorization.authorize_policy(
                target,
                self.principal(
                    "medical_record.view", "medical_record.ai.review"
                ),
            )
        )

    def test_system_admin_requires_literal_true(self):
        target = policy("SYSTEM_ADMIN")
        self.assert_denied(403, target, self.principal(admin=False))
        malformed = self.principal(admin=1)
        self.assert_denied(403, target, malformed)
        self.assertIsNone(
            authorization.authorize_policy(target, self.principal(admin=True))
        )

    def test_stats_sectioned_accepts_any_partition_permission(self):
        target = policy(
            "SECTIONED",
            ("patient.profile.view", "billing.view", "audit.view"),
            method="GET",
            path="/api/stats",
        )
        self.assertIsNone(
            authorization.authorize_policy(target, self.principal("billing.view"))
        )
        self.assert_denied(403, target, self.principal("appointment.view"))

    def test_other_sectioned_routes_require_the_first_entry_permission(self):
        target = policy(
            "SECTIONED",
            ("patient.profile.view", "billing.view"),
            path="/api/patients",
        )
        self.assert_denied(403, target, self.principal("billing.view"))
        self.assertIsNone(
            authorization.authorize_policy(
                target, self.principal("patient.profile.view")
            )
        )

    def test_conditional_requires_at_least_one_candidate_at_boundary(self):
        target = policy(
            "CONDITIONAL",
            ("appointment.edit", "appointment.status", "appointment.cancel"),
        )
        self.assertIsNone(
            authorization.authorize_policy(
                target, self.principal("appointment.status")
            )
        )
        self.assert_denied(403, target, self.principal("appointment.view"))

    def test_system_admin_bypasses_all_normal_permission_policies(self):
        admin = self.principal(admin=True)
        for target in (
            policy("PERMISSION", ("billing.view",)),
            policy("ALL_OF", ("billing.view", "payment.refund")),
            policy(
                "SECTIONED",
                ("patient.profile.view", "billing.view"),
                path="/api/patients",
            ),
            policy("CONDITIONAL", ("appointment.edit", "appointment.cancel")),
        ):
            with self.subTest(policy_type=target.policy_type):
                self.assertIsNone(authorization.authorize_policy(target, admin))

    def test_malformed_policy_is_a_configuration_500(self):
        malformed = (
            policy("UNKNOWN"),
            policy("PERMISSION"),
            policy("PERMISSION", ("not.in.catalog",)),
            policy("PUBLIC", ("billing.view",)),
            policy("ALL_OF", ("billing.view", "billing.view")),
        )
        for target in malformed:
            with self.subTest(target=target):
                self.assert_denied(500, target, self.principal(admin=True))


class FastAPIAuthorizerTests(SyntheticAuthorizationDatabase):
    def setUp(self):
        super().setUp()
        self.add_staff_user()
        self.grant("s1", "front", "appointment.view", "billing.view")

    def make_app(self):
        app = FastAPI(
            dependencies=[Depends(authorization.build_access_authorizer(self.db_path))]
        )

        @app.get("/api/appointments/{appointment_id}")
        def appointment(appointment_id: str):
            principal = authorization.current_access_principal()
            target_policy = authorization.current_authorized_policy()
            return {
                "appointment_id": appointment_id,
                "user_id": principal.user_id,
                "policy_path": target_policy.path,
            }

        @app.get("/api/health")
        def health():
            return {"ok": True}

        @app.get("/api/stats")
        def stats():
            return {
                "billing": authorization.has_access("billing.view"),
                "patient": authorization.has_access("patient.profile.view"),
                "unknown": authorization.has_access("not.in.catalog"),
            }

        @app.get("/api/unmapped")
        def unmapped():
            return {"unsafe": True}

        @app.get("/__login")
        @app.get("/api2")
        def non_api_page():
            return {
                "principal": authorization.current_access_principal(),
                "policy": authorization.current_authorized_policy(),
            }

        @app.get("/api")
        def bare_api_root():
            return {"unsafe": True}

        return app

    def current_user_patch(self, user=None):
        if user is None:
            user = {"id": "u1", "username": "untrusted-browser-value"}
        return mock.patch.object(access_auth, "current_access_user", return_value=user)

    def test_path_parameter_uses_route_template_and_context_is_reset(self):
        app = self.make_app()
        with self.current_user_patch(), TestClient(app) as client:
            response = client.get("/api/appointments/a-real-id")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["user_id"], "u1")
        self.assertEqual(
            response.json()["policy_path"], "/api/appointments/{appointment_id}"
        )
        self.assertIsNone(authorization.current_access_principal())
        self.assertIsNone(authorization.current_authorized_policy())

    def test_anonymous_authenticated_route_is_401(self):
        app = self.make_app()
        with self.current_user_patch(user={}), TestClient(app) as client:
            response = client.get("/api/appointments/1")
        self.assertEqual(response.status_code, 401)

    def test_logged_in_user_without_permission_is_403(self):
        conn = sqlite3.connect(self.db_path)
        conn.execute("delete from role_permissions")
        conn.commit()
        conn.close()
        app = self.make_app()
        with self.current_user_patch(), TestClient(app) as client:
            response = client.get("/api/appointments/1")
        self.assertEqual(response.status_code, 403)

    def test_public_route_skips_account_database_loading(self):
        app = self.make_app()
        with mock.patch.object(
            authorization, "load_access_principal", side_effect=AssertionError
        ), self.current_user_patch(user={}), TestClient(app) as client:
            response = client.get("/api/health")
        self.assertEqual(response.status_code, 200)

    def test_route_without_policy_fails_closed_with_500(self):
        app = self.make_app()
        with self.current_user_patch(), TestClient(app) as client:
            response = client.get("/api/unmapped")
        self.assertEqual(response.status_code, 500)
        self.assertFalse(response.json().get("unsafe", False))

    def test_non_api_routes_pass_through_with_empty_reset_context(self):
        app = self.make_app()
        with self.current_user_patch(), TestClient(app) as client:
            for path in ("/__login", "/api2"):
                with self.subTest(path=path):
                    response = client.get(path)
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(
                        response.json(), {"principal": None, "policy": None}
                    )
        self.assertIsNone(authorization.current_access_principal())
        self.assertIsNone(authorization.current_authorized_policy())

    def test_bare_api_root_without_policy_is_configuration_500(self):
        app = self.make_app()
        with self.current_user_patch(), TestClient(app) as client:
            response = client.get("/api")
        self.assertEqual(response.status_code, 500)
        self.assertFalse(response.json().get("unsafe", False))

    def test_has_access_uses_current_principal_and_catalog(self):
        app = self.make_app()
        with self.current_user_patch(), TestClient(app) as client:
            response = client.get("/api/stats")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {"billing": True, "patient": False, "unknown": False},
        )


class HandlerGuardTests(SyntheticAuthorizationDatabase):
    def setUp(self):
        super().setUp()
        self.add_staff_user()
        self.grant("s1", "front", "billing.view")

    def build_guard_app(self, guard):
        app = FastAPI(
            dependencies=[Depends(authorization.build_access_authorizer(self.db_path))]
        )

        @app.get("/api/stats")
        def stats():
            guard()
            return {"ok": True}

        return app

    def context_patches(self, *, session=None, now=NOW):
        if session is None:
            session = {
                "created_at": "2026-07-16T11:50:00Z",
                "reauthenticated_at": "2026-07-16T11:59:00Z",
            }
        return (
            mock.patch.object(
                access_auth, "current_access_user", return_value={"id": "u1"}
            ),
            mock.patch.object(
                access_auth, "current_access_session", return_value=session
            ),
            mock.patch.object(
                access_auth, "current_access_request_time", return_value=now
            ),
        )

    def test_require_access_denies_missing_known_permission(self):
        app = self.build_guard_app(
            lambda: authorization.require_access("patient.profile.view")
        )
        user_patch, session_patch, time_patch = self.context_patches()
        with user_patch, session_patch, time_patch, TestClient(app) as client:
            response = client.get("/api/stats")
        self.assertEqual(response.status_code, 403)

    def test_require_access_unknown_key_is_configuration_500(self):
        app = self.build_guard_app(
            lambda: authorization.require_access("not.in.catalog")
        )
        user_patch, session_patch, time_patch = self.context_patches()
        with user_patch, session_patch, time_patch, TestClient(app) as client:
            response = client.get("/api/stats")
        self.assertEqual(response.status_code, 500)

    def test_require_recent_reauth_reuses_session_service_check(self):
        app = self.build_guard_app(authorization.require_recent_reauth)
        session = {
            "created_at": "2026-07-16T11:50:00Z",
            "reauthenticated_at": "2026-07-16T11:59:00Z",
        }
        user_patch, session_patch, time_patch = self.context_patches(
            session=session
        )
        with mock.patch.object(
            authorization.session_service, "is_reauth_fresh", return_value=True
        ) as fresh, user_patch, session_patch, time_patch, TestClient(app) as client:
            response = client.get("/api/stats")

        self.assertEqual(response.status_code, 200)
        fresh.assert_called_once_with(
            session["reauthenticated_at"], created_at=session["created_at"], now=NOW
        )

    def test_stale_recent_reauth_is_403(self):
        app = self.build_guard_app(authorization.require_recent_reauth)
        user_patch, session_patch, time_patch = self.context_patches()
        with mock.patch.object(
            authorization.session_service, "is_reauth_fresh", return_value=False
        ), user_patch, session_patch, time_patch, TestClient(app) as client:
            response = client.get("/api/stats")
        self.assertEqual(response.status_code, 403)

    def test_missing_session_is_401(self):
        app = self.build_guard_app(authorization.require_recent_reauth)
        user_patch, session_patch, time_patch = self.context_patches(session={})
        with user_patch, session_patch, time_patch, TestClient(app) as client:
            response = client.get("/api/stats")
        self.assertEqual(response.status_code, 401)


class SourceSafetyTests(unittest.TestCase):
    def test_runtime_has_no_duplicate_role_permission_join_or_default_db(self):
        source = inspect.getsource(authorization)
        self.assertNotIn("join role_permissions", source.lower())
        self.assertNotIn("DEFAULT_DB_PATH", source)
        self.assertIn("effective_permissions", source)

    def test_every_sql_execute_statement_is_a_literal_with_bound_parameters(self):
        tree = ast.parse(inspect.getsource(authorization))
        execute_calls = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "execute"
        ]
        self.assertTrue(execute_calls)
        for call in execute_calls:
            with self.subTest(line=call.lineno):
                self.assertIsInstance(call.args[0], ast.Constant)
                self.assertIsInstance(call.args[0].value, str)
                if "?" in call.args[0].value:
                    self.assertGreaterEqual(len(call.args), 2)


class LegacyBridgeTests(unittest.TestCase):
    def make_legacy_db(self, directory):
        db_path = Path(directory) / "legacy.sqlite3"
        init_db(db_path)
        return db_path

    def test_setup_auth_explicitly_enables_old_no_login_permissions(self):
        with TemporaryDirectory() as directory:
            db_path = self.make_legacy_db(directory)
            app = FastAPI()

            @app.get("/api/legacy-probe")
            def probe():
                authorization.require_access("patient.profile.view")
                return {
                    "has_patient": authorization.has_access(
                        "patient.profile.view"
                    )
                }

            legacy_auth.setup_auth(app, db_path, require_login=False)
            response = TestClient(app).get("/api/legacy-probe")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"has_patient": True})

    def test_old_bridge_uses_only_explicit_aliases(self):
        with TemporaryDirectory() as directory:
            db_path = self.make_legacy_db(directory)
            app = FastAPI()

            @app.get("/api/legacy-unmapped-has")
            def unmapped_has():
                return {"allowed": authorization.has_access("appointment.view")}

            @app.get("/api/legacy-unmapped-require")
            def unmapped_require():
                authorization.require_access("appointment.view")
                return {"unsafe": True}

            legacy_auth.setup_auth(app, db_path, require_login=False)
            client = TestClient(app)
            has_response = client.get("/api/legacy-unmapped-has")
            require_response = client.get("/api/legacy-unmapped-require")

        self.assertEqual(has_response.status_code, 200)
        self.assertEqual(has_response.json(), {"allowed": False})
        self.assertEqual(require_response.status_code, 403)

    def test_old_login_bridge_distinguishes_legacy_role_permissions(self):
        with TemporaryDirectory() as directory:
            db_path = self.make_legacy_db(directory)
            legacy_auth.ensure_seed_roles(db_path)
            with legacy_connect(db_path) as conn:
                conn.execute(
                    "insert into roles(role_key, name, is_system, sort) "
                    "values ('empty-role', 'Empty', 0, 999)"
                )
                legacy_auth.create_user(
                    conn, "doctor-user", "Doctor", "pw", role="doctor"
                )
                legacy_auth.create_user(
                    conn, "empty-user", "Empty", "pw", role="empty-role"
                )
                conn.commit()

            app = FastAPI()

            @app.get("/api/legacy-patient")
            def patient_probe():
                authorization.require_access("patient.profile.view")
                return {"ok": True}

            legacy_auth.setup_auth(app, db_path, require_login=True)
            doctor = TestClient(app)
            empty = TestClient(app)
            self.assertEqual(
                doctor.post(
                    "/api/auth/login",
                    json={"username": "doctor-user", "password": "pw"},
                ).status_code,
                200,
            )
            self.assertEqual(
                empty.post(
                    "/api/auth/login",
                    json={"username": "empty-user", "password": "pw"},
                ).status_code,
                200,
            )
            self.assertEqual(doctor.get("/api/legacy-patient").status_code, 200)
            self.assertEqual(empty.get("/api/legacy-patient").status_code, 403)

    def test_guard_without_new_context_or_explicit_bridge_fails_closed(self):
        app = FastAPI()

        @app.get("/direct-has")
        def direct_has():
            return {"allowed": authorization.has_access("patient.profile.view")}

        @app.get("/direct-require")
        def direct_require():
            authorization.require_access("patient.profile.view")
            return {"unsafe": True}

        client = TestClient(app)
        self.assertEqual(client.get("/direct-has").json(), {"allowed": False})
        self.assertEqual(client.get("/direct-require").status_code, 401)

    def test_target_policy_context_never_falls_back_to_old_bridge(self):
        with TemporaryDirectory() as directory:
            db_path = self.make_legacy_db(directory)
            app = FastAPI(
                dependencies=[
                    Depends(authorization.build_access_authorizer(db_path))
                ]
            )

            @app.get("/api/health")
            def target_guard():
                authorization.require_access("patient.profile.view")
                return {"unsafe": True}

            legacy_auth.setup_auth(app, db_path, require_login=False)
            response = TestClient(app).get("/api/health")

        self.assertEqual(response.status_code, 401)
        self.assertFalse(response.json().get("unsafe", False))

    def test_non_api_global_authorizer_never_falls_back_to_old_bridge(self):
        with TemporaryDirectory() as directory:
            db_path = self.make_legacy_db(directory)
            app = FastAPI(
                dependencies=[
                    Depends(authorization.build_access_authorizer(db_path))
                ]
            )

            @app.get("/legacy-looking-has")
            def legacy_looking_has():
                return {
                    "policy_is_none": (
                        authorization.current_authorized_policy() is None
                    ),
                    "principal_is_none": (
                        authorization.current_access_principal() is None
                    ),
                    "allowed": authorization.has_access(
                        "patient.profile.view"
                    ),
                }

            @app.get("/legacy-looking-require")
            def legacy_looking_require():
                authorization.require_access("patient.profile.view")
                return {"unsafe": True}

            legacy_auth.setup_auth(app, db_path, require_login=False)
            client = TestClient(app)
            has_response = client.get("/legacy-looking-has")
            require_response = client.get("/legacy-looking-require")

        self.assertEqual(has_response.status_code, 200)
        self.assertEqual(
            has_response.json(),
            {
                "policy_is_none": True,
                "principal_is_none": True,
                "allowed": False,
            },
        )
        self.assertEqual(require_response.status_code, 401)
        self.assertFalse(require_response.json().get("unsafe", False))

    def test_authorizer_active_context_resets_after_endpoint_exception(self):
        observed_after_request = []

        class ObserveAfterRequest:
            def __init__(self, app):
                self.app = app

            async def __call__(self, scope, receive, send):
                try:
                    return await self.app(scope, receive, send)
                finally:
                    observed_after_request.append(
                        authorization._access_authorizer_active.get()
                    )

        with TemporaryDirectory() as directory:
            db_path = self.make_legacy_db(directory)
            app = FastAPI(
                dependencies=[
                    Depends(authorization.build_access_authorizer(db_path))
                ]
            )

            @app.get("/legacy-looking-boom")
            def boom():
                self.assertFalse(
                    authorization.has_access("patient.profile.view")
                )
                raise RuntimeError("synthetic endpoint failure")

            legacy_auth.setup_auth(app, db_path, require_login=False)
            response = TestClient(
                ObserveAfterRequest(app), raise_server_exceptions=False
            ).get("/legacy-looking-boom")

        self.assertEqual(response.status_code, 500)
        self.assertEqual(observed_after_request, [False])

    def test_bridge_context_resets_after_success_and_exception(self):
        seen = []

        async def inner(scope, receive, send):
            seen.append(authorization.has_access("patient.profile.view"))
            if scope["path"] == "/boom":
                raise RuntimeError("synthetic boom")

        wrapped = authorization.LegacyAccessBridgeMiddleware(
            inner,
            legacy_require_perm=lambda _permission: None,
            legacy_has_perm=lambda _permission: True,
        )

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(_message):
            return None

        async def scenario():
            with self.assertRaises(RuntimeError):
                await wrapped(
                    {"type": "http", "path": "/boom"}, receive, send
                )
            self.assertFalse(authorization.has_access("patient.profile.view"))
            with self.assertRaises(HTTPException) as raised:
                authorization.require_access("patient.profile.view")
            self.assertEqual(raised.exception.status_code, 401)
            await wrapped({"type": "http", "path": "/ok"}, receive, send)
            self.assertFalse(authorization.has_access("patient.profile.view"))

        asyncio.run(scenario())
        self.assertEqual(seen, [True, True])


if __name__ == "__main__":
    unittest.main()
