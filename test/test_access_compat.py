"""Compatibility tests for legacy auth helpers inside the v3 target runtime."""
import asyncio
import sqlite3
import unittest
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from fastapi import HTTPException

from local_app import access_authorization as authorization
from local_app import auth
from local_app import db
from local_app.access_policy import ROUTE_POLICY_BY_KEY


TARGET_PERMISSIONS = frozenset(
    {
        "patient.profile.view",
        "billing.view",
        "audit.view",
        "report.finance.view",
        "communication.view",
        "call_record.view",
        "not.in.catalog",
    }
)

TARGET_PRINCIPAL = authorization.AccessPrincipal(
    user_id="user-target",
    username="target-user",
    display_name="Target User",
    staff_id="staff-target",
    is_system_admin=False,
    primary_role="front",
    role_keys=("front", "finance"),
    permissions=TARGET_PERMISSIONS,
    role_permissions=TARGET_PERMISSIONS,
)


@contextmanager
def target_context(principal=TARGET_PRINCIPAL):
    active_token = authorization._access_authorizer_active.set(True)
    principal_token = authorization._current_access_principal.set(principal)
    policy_token = authorization._current_authorized_policy.set(
        ROUTE_POLICY_BY_KEY[("GET", "/api/appointments")]
    )
    try:
        yield
    finally:
        authorization._current_authorized_policy.reset(policy_token)
        authorization._current_access_principal.reset(principal_token)
        authorization._access_authorizer_active.reset(active_token)


def create_audit_database(path, *, target):
    conn = sqlite3.connect(path)
    conn.execute(
        "create table audit_logs("
        "audit_id integer primary key autoincrement, "
        "entity_type text not null, entity_id text not null, action text not null, "
        "old_json text, new_json text, operator text, created_at text not null"
        + (", actor_user_id text" if target else "")
        + ")"
    )
    conn.execute("create table widgets(id text primary key, value text)")
    conn.commit()
    conn.close()


class TargetLegacyHelperTests(unittest.TestCase):
    def test_current_user_exposes_real_target_principal_without_fake_admin(self):
        with target_context():
            current = auth.current_user()
            required = auth.require_login_user()

        expected = {
            "id": "user-target",
            "username": "target-user",
            "display_name": "Target User",
            "staff_id": "staff-target",
            "role": "front",
            "roles": ["front", "finance"],
            "permissions": [
                "audit.view",
                "billing.view",
                "call_record.view",
                "communication.view",
                "patient.profile.view",
                "report.finance.view",
            ],
            "is_system_admin": False,
        }
        self.assertEqual(current, expected)
        self.assertEqual(required, expected)
        self.assertNotEqual(current["role"], "admin")
        self.assertNotIn("not.in.catalog", current["permissions"])

    def test_target_require_perm_unmapped_action_is_policy_compat_shell(self):
        # 无 v3 对应物的旧动作名:放行,交路由策略层把关(每路由必有策略,守卫测试锁定)
        with target_context():
            from_perm = auth.require_perm("unknown.old.permission")
        self.assertEqual(from_perm["id"], "user-target")

    def test_target_require_admin_hard_gate_rejects_non_admin(self):
        # 审计Y20:v3 下 require_admin 不再是兼容壳——患者合并/储值退现等"仅管理员"硬闸保留,
        # 普通角色(front/finance)即便过了路由策略也 403
        with target_context(), self.assertRaises(HTTPException) as caught:
            auth.require_admin()
        self.assertEqual(caught.exception.status_code, 403)

    def test_target_without_real_principal_fails_closed(self):
        with target_context(None):
            self.assertIsNone(auth.current_user())
            self.assertFalse(auth.has_perm("patient.view"))
            for guard in (
                auth.require_login_user,
                auth.require_admin,
                lambda: auth.require_perm("patient.view"),
                auth.audit_operator,
            ):
                with self.subTest(guard=guard), self.assertRaises(
                    HTTPException
                ) as caught:
                    guard()
                self.assertEqual(caught.exception.status_code, 401)

    def test_target_has_perm_uses_only_explicit_safe_aliases(self):
        expected_aliases = (
            "patient.view",
            "billing.view",
            "audit.view",
            "report.view",
            "communication.view",
            "call.view",
        )
        with target_context():
            for permission in expected_aliases:
                with self.subTest(permission=permission):
                    self.assertTrue(auth.has_perm(permission))
            for permission in (
                "patient.edit",
                "medical_record.view",
                "unknown.old.permission",
            ):
                with self.subTest(permission=permission):
                    self.assertFalse(auth.has_perm(permission))

    def test_target_audit_operator_is_username_and_never_local_user(self):
        with target_context():
            self.assertEqual(auth.audit_operator(), "target-user")
        with target_context(None), self.assertRaises(HTTPException) as caught:
            auth.audit_operator()
        self.assertEqual(caught.exception.status_code, 401)

    def test_legacy_context_and_local_user_behavior_are_unchanged(self):
        legacy = {
            "id": "legacy-user",
            "username": "legacy-name",
            "display_name": "Legacy",
            "role": "admin",
            "staff_id": "",
            "permissions": ["*"],
        }
        token = auth._current_user.set(legacy)
        try:
            self.assertIs(auth.current_user(), legacy)
            self.assertIs(auth.require_login_user(), legacy)
            self.assertIs(auth.require_admin(), legacy)
            self.assertIs(auth.require_perm("anything"), legacy)
            self.assertTrue(auth.has_perm("anything"))
            self.assertEqual(auth.audit_operator(), "legacy-name")
        finally:
            auth._current_user.reset(token)
        self.assertEqual(auth.audit_operator(), "local_user")


class TargetAuditWriteTests(unittest.TestCase):
    def test_auth_audit_write_records_target_actor_without_committing(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "target.sqlite3"
            create_audit_database(path, target=True)
            conn = sqlite3.connect(path)
            conn.row_factory = sqlite3.Row
            try:
                with target_context():
                    auth.audit_write(conn, "widget", "w1", "target.change")
                self.assertTrue(conn.in_transaction)
                row = conn.execute(
                    "select operator, actor_user_id from audit_logs"
                ).fetchone()
                self.assertEqual(tuple(row), ("target-user", "user-target"))
                conn.rollback()
            finally:
                conn.close()

            check = sqlite3.connect(path)
            try:
                self.assertEqual(
                    check.execute("select count(*) from audit_logs").fetchone()[0],
                    0,
                )
            finally:
                check.close()

    def test_db_audit_write_supports_target_actor_and_legacy_seven_columns(self):
        with TemporaryDirectory() as directory:
            target_path = Path(directory) / "target.sqlite3"
            legacy_path = Path(directory) / "legacy.sqlite3"
            create_audit_database(target_path, target=True)
            create_audit_database(legacy_path, target=False)

            target = sqlite3.connect(target_path)
            db.audit_write(
                target,
                "widget",
                "w1",
                "target.direct",
                operator="target-user",
                actor_user_id="user-target",
            )
            target_row = target.execute(
                "select operator, actor_user_id from audit_logs"
            ).fetchone()
            target.close()

            legacy = sqlite3.connect(legacy_path)
            db.audit_write(
                legacy,
                "widget",
                "w1",
                "legacy.direct",
                operator="legacy-name",
            )
            legacy_row = legacy.execute(
                "select operator from audit_logs"
            ).fetchone()
            legacy.close()

        self.assertEqual(target_row, ("target-user", "user-target"))
        self.assertEqual(legacy_row, ("legacy-name",))


class TargetContextMiddleware:
    def __init__(self, app, principal):
        self.app = app
        self.principal = principal

    async def __call__(self, scope, receive, send):
        with target_context(self.principal):
            return await self.app(scope, receive, send)


async def receive_empty_request():
    return {"type": "http.request", "body": b"", "more_body": False}


class OperationAuditTargetTests(unittest.TestCase):
    def run_request(self, path, *, principal, self_audit=False, target=True):
        async def inner(scope, receive, send):
            with db.connect(path) as conn:
                conn.execute(
                    "insert into widgets(id, value) values (?, ?)",
                    ("w1", "changed"),
                )
                if self_audit:
                    auth.audit_write(
                        conn, "widget", "w1", "route.self_audited"
                    )
                conn.commit()
            await send(
                {
                    "type": "http.response.start",
                    "status": 200,
                    "headers": [],
                }
            )
            await send({"type": "http.response.body", "body": b"{}"})

        middleware = auth.OperationAuditMiddleware(inner, path)
        app = (
            TargetContextMiddleware(middleware, principal)
            if target
            else middleware
        )
        sent = []

        async def send(message):
            sent.append(message)

        asyncio.run(
            app(
                {
                    "type": "http",
                    "method": "POST",
                    "path": "/api/widgets/w1",
                    "headers": [(b"cookie", b"dl_session=legacy-token")],
                },
                receive_empty_request,
                send,
            )
        )
        return sent

    def audit_rows(self, path, *, target):
        conn = sqlite3.connect(path)
        try:
            columns = "action, operator"
            if target:
                columns += ", actor_user_id"
            return conn.execute(
                "select " + columns + " from audit_logs order by audit_id"
            ).fetchall()
        finally:
            conn.close()

    def test_target_generic_audit_uses_principal_and_never_parses_legacy_cookie(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "target.sqlite3"
            create_audit_database(path, target=True)
            with mock.patch.object(
                auth,
                "_cookie_from_scope",
                side_effect=AssertionError("target must not parse dl_session"),
            ):
                self.run_request(path, principal=TARGET_PRINCIPAL)
            rows = self.audit_rows(path, target=True)

        self.assertEqual(
            rows,
            [("post_widgets", "target-user", "user-target")],
        )

    def test_target_self_audit_is_not_duplicated_and_keeps_actor(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "target.sqlite3"
            create_audit_database(path, target=True)
            self.run_request(
                path, principal=TARGET_PRINCIPAL, self_audit=True
            )
            rows = self.audit_rows(path, target=True)

        self.assertEqual(
            rows,
            [("route.self_audited", "target-user", "user-target")],
        )

    def test_target_without_principal_never_writes_local_user(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "target.sqlite3"
            create_audit_database(path, target=True)
            with self.assertLogs("local_app", level="ERROR"):
                self.run_request(path, principal=None)
            rows = self.audit_rows(path, target=True)

        self.assertEqual(rows, [])

    def test_legacy_generic_audit_still_writes_local_user_seven_columns(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "legacy.sqlite3"
            create_audit_database(path, target=False)
            with mock.patch.object(auth, "user_by_token", return_value=None):
                self.run_request(path, principal=None, target=False)
            rows = self.audit_rows(path, target=False)

        self.assertEqual(rows, [("post_widgets", "local_user")])


if __name__ == "__main__":
    unittest.main()
