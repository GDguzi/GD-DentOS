"""Target-schema account routes, isolated from the legacy users.role model."""
from __future__ import annotations

import asyncio
import sqlite3
import threading
import time
import unittest
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import httpx
from fastapi import Depends, FastAPI, HTTPException
from fastapi.testclient import TestClient

from local_app import access_auth, access_service
from local_app import access_authorization as authorization
from local_app import access_session_service as session_service
from local_app.access_authorization import build_access_authorizer
from local_app.passwords import hash_password, verify_password
from local_app.routes.users import create_users_router


NOW = datetime(2026, 7, 16, 9, 0, tzinfo=timezone.utc)
ADMIN_PASSWORD = "admin-pw"
SELF_PASSWORD = "old-pw"
SURROGATE_JSON = r"\ud800"


def _create_target_database(path):
    schema = (
        Path(__file__).parents[1] / "local_app" / "personnel_access_schema.sql"
    ).read_text(encoding="utf-8")
    conn = sqlite3.connect(path)
    try:
        conn.execute("pragma foreign_keys = on")
        conn.executescript(
            """
            create table roles(role_key text primary key);
            create table role_permissions(
                role_key text not null references roles(role_key),
                perm_key text not null,
                primary key(role_key, perm_key)
            );
            """
        )
        conn.executescript(schema)
        conn.executemany(
            "insert into roles(role_key) values (?)",
            [("front",), ("doctor",)],
        )
        conn.executemany(
            "insert into role_permissions(role_key, perm_key) values (?, ?)",
            [
                ("front", "account.view"),
                ("front", "account.open"),
                ("front", "account.security"),
                ("doctor", "patient.profile.view"),
            ],
        )
        conn.executemany(
            "insert into staff_members(staff_id, name, employment_status) "
            "values (?, ?, ?)",
            [
                ("staff-self", "Alice", "employed"),
                ("staff-new", "New Doctor", "employed"),
                ("staff-second", "Second Staff", "employed"),
                ("staff-left", "Former Staff", "left"),
            ],
        )
        conn.executemany(
            "insert into staff_role_assignments"
            "(staff_id, role_key, is_primary, created_at) values (?, ?, ?, ?)",
            [
                ("staff-self", "front", 1, "2026-07-16 09:00:00"),
                ("staff-new", "doctor", 1, "2026-07-16 09:00:00"),
                ("staff-new", "front", 0, "2026-07-16 09:00:00"),
                ("staff-second", "front", 1, "2026-07-16 09:00:00"),
            ],
        )
        conn.executemany(
            "insert into users(id, username, display_name, password_hash, "
            "staff_id, is_active, is_system_admin, account_kind, created_at, "
            "updated_at) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    "user-admin",
                    "admin",
                    "System Admin",
                    hash_password(ADMIN_PASSWORD),
                    None,
                    1,
                    1,
                    "independent_admin",
                    "2026-07-16 09:00:00",
                    "2026-07-16 09:00:00",
                ),
                (
                    "user-self",
                    "alice",
                    "Alice",
                    hash_password(SELF_PASSWORD),
                    "staff-self",
                    1,
                    0,
                    "staff",
                    "2026-07-16 09:00:00",
                    "2026-07-16 09:00:00",
                ),
            ],
        )
        conn.commit()
    finally:
        conn.close()


def _create_target_app(db_path):
    app = FastAPI(
        dependencies=[Depends(build_access_authorizer(db_path))]
    )
    access_auth.register_access_auth_routes(app, db_path)
    app.include_router(create_users_router(db_path, access_v3=True))
    access_auth.setup_access_auth_context(
        app, db_path, now_provider=lambda: NOW
    )
    return app


def _login(client, username, password):
    response = client.post(
        "/api/auth/login",
        json={"username": username, "password": password},
    )
    if response.status_code != 200:
        raise AssertionError(response.text)
    return client.cookies.get(access_auth.ACCESS_COOKIE_NAME)


def _raw_post(client, path, body):
    return client.post(
        path,
        content=body.encode("utf-8"),
        headers={"content-type": "application/json"},
    )


def _fetchone(path, sql, params=()):
    conn = sqlite3.connect(path)
    try:
        return conn.execute(sql, params).fetchone()
    finally:
        conn.close()


def _fetchall(path, sql, params=()):
    conn = sqlite3.connect(path)
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def _execute(path, sql, params=()):
    conn = sqlite3.connect(path)
    try:
        conn.execute(sql, params)
        conn.commit()
    finally:
        conn.close()


def _endpoint(router, method, path):
    return next(
        route.endpoint
        for route in router.routes
        if route.path == path and method in route.methods
    )


def _principal(
    *,
    user_id="user-admin",
    username="admin",
    staff_id=None,
    system_admin=True,
    permissions=(),
):
    return authorization.AccessPrincipal(
        user_id=user_id,
        username=username,
        display_name=username,
        staff_id=staff_id,
        is_system_admin=system_admin,
        primary_role=None,
        role_keys=(),
        permissions=frozenset(permissions),
        role_permissions=frozenset(permissions),
    )


@contextmanager
def _as(principal):
    token = authorization._current_access_principal.set(principal)
    try:
        yield
    finally:
        authorization._current_access_principal.reset(token)


class AccessTargetUsersTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "target.sqlite3"
        _create_target_database(self.db_path)
        self.app = _create_target_app(self.db_path)
        self.admin = TestClient(self.app)
        _login(self.admin, "admin", ADMIN_PASSWORD)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_list_uses_target_fields_and_staff_role_assignments(self):
        response = self.admin.get("/api/users")
        self.assertEqual(response.status_code, 200, response.text)
        users = {user["id"]: user for user in response.json()["users"]}
        self.assertEqual(response.json()["totalcount"], 2)
        self.assertEqual(
            users["user-admin"],
            {
                "id": "user-admin",
                "username": "admin",
                "display_name": "System Admin",
                "staff_id": None,
                "is_active": True,
                "is_system_admin": True,
                "account_kind": "independent_admin",
                "primary_role": None,
                "role_keys": [],
            },
        )
        self.assertEqual(users["user-self"]["primary_role"], "front")
        self.assertEqual(users["user-self"]["role_keys"], ["front"])
        self.assertNotIn("role", users["user-self"])
        self.assertNotIn("password_hash", users["user-self"])

    def test_system_admin_grant_then_revoke_returns_fixed_shape_and_audits(self):
        for action, expected in (("grant", True), ("revoke", False)):
            response = self.admin.post(
                f"/api/users/user-self/system-admin/{action}",
                json={"reason": f"{action} reason"},
            )
            with self.subTest(action=action):
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(
                    response.json(),
                    {
                        "ok": True,
                        "user_id": "user-self",
                        "is_system_admin": expected,
                    },
                )

        self.assertEqual(
            _fetchone(
                self.db_path,
                "select is_system_admin from users where id='user-self'",
            )[0],
            0,
        )
        self.assertEqual(
            _fetchall(
                self.db_path,
                "select action, operator, actor_user_id, reason, risk_level, "
                "created_at from audit_logs where entity_id='user-self' "
                "and action like 'system_admin.%' order by audit_id",
            ),
            [
                (
                    "system_admin.grant",
                    "admin",
                    "user-admin",
                    "grant reason",
                    "high",
                    "2026-07-16 17:00:00",
                ),
                (
                    "system_admin.revoke",
                    "admin",
                    "user-admin",
                    "revoke reason",
                    "high",
                    "2026-07-16 17:00:00",
                ),
            ],
        )

    def test_system_admin_routes_reject_non_admin_with_account_permissions(self):
        alice = TestClient(self.app)
        _login(alice, "alice", SELF_PASSWORD)
        before = _fetchone(
            self.db_path,
            "select is_system_admin from users where id='user-self'",
        )[0]
        before_audits = _fetchone(
            self.db_path,
            "select count(*) from audit_logs where action like 'system_admin.%'",
        )[0]

        for action, target in (("grant", "user-self"), ("revoke", "user-admin")):
            response = alice.post(
                f"/api/users/{target}/system-admin/{action}",
                json={"reason": "must not run"},
            )
            with self.subTest(action=action):
                self.assertEqual(response.status_code, 403, response.text)
                self.assertEqual(response.json(), {"detail": "access_denied"})

        self.assertEqual(
            _fetchone(
                self.db_path,
                "select is_system_admin from users where id='user-self'",
            )[0],
            before,
        )
        self.assertEqual(
            _fetchone(
                self.db_path,
                "select count(*) from audit_logs where action like 'system_admin.%'",
            )[0],
            before_audits,
        )

    def test_system_admin_routes_require_fresh_reauthentication_without_writes(self):
        _execute(
            self.db_path,
            "update sessions set reauthenticated_at = ? where user_id = ?",
            ("2026-07-16T08:54:59Z", "user-admin"),
        )
        before_audits = _fetchone(
            self.db_path,
            "select count(*) from audit_logs where action like 'system_admin.%'",
        )[0]

        for action in ("grant", "revoke"):
            response = self.admin.post(
                f"/api/users/user-self/system-admin/{action}",
                json={"reason": "stale request"},
            )
            with self.subTest(action=action):
                self.assertEqual(response.status_code, 403, response.text)
                self.assertEqual(
                    response.json(),
                    {"detail": "recent_reauthentication_required"},
                )

        self.assertEqual(
            _fetchone(
                self.db_path,
                "select is_system_admin from users where id='user-self'",
            )[0],
            0,
        )
        self.assertEqual(
            _fetchone(
                self.db_path,
                "select count(*) from audit_logs where action like 'system_admin.%'",
            )[0],
            before_audits,
        )

    def test_system_admin_routes_reject_bad_body_shapes_before_service(self):
        bad_requests = (
            ("missing_body", {}, "invalid_request"),
            (
                "null_body",
                {
                    "content": b"null",
                    "headers": {"content-type": "application/json"},
                },
                "invalid_request",
            ),
            ("list_body", {"json": []}, "invalid_request"),
            ("string_body", {"json": "reason"}, "invalid_request"),
            ("empty_object", {"json": {}}, "invalid_request"),
            (
                "extra_field",
                {"json": {"reason": "valid", "actor_user_id": "forged"}},
                "invalid_request",
            ),
            ("empty_reason", {"json": {"reason": ""}}, "reason_required"),
            (
                "blank_reason",
                {"json": {"reason": "   "}},
                "reason_required",
            ),
            (
                "non_string_reason",
                {"json": {"reason": 123}},
                "reason_invalid",
            ),
            (
                "surrogate_reason",
                {
                    "content": ('{"reason":"%s"}' % SURROGATE_JSON).encode(
                        "utf-8"
                    ),
                    "headers": {"content-type": "application/json"},
                },
                "reason_invalid",
            ),
            (
                "deeply_nested_json",
                {
                    "content": (
                        "[" * 1200 + "0" + "]" * 1200
                    ).encode("utf-8"),
                    "headers": {"content-type": "application/json"},
                },
                "invalid_request",
            ),
            (
                "duplicate_reason",
                {
                    "content": b'{"reason":"one","reason":"two"}',
                    "headers": {"content-type": "application/json"},
                },
                "invalid_request",
            ),
        )
        before_audits = _fetchone(
            self.db_path,
            "select count(*) from audit_logs where action like 'system_admin.%'",
        )[0]
        client = TestClient(self.app, raise_server_exceptions=False)
        client.cookies.update(self.admin.cookies)

        for action in ("grant", "revoke"):
            for name, kwargs, detail in bad_requests:
                response = client.post(
                    f"/api/users/user-self/system-admin/{action}",
                    **kwargs,
                )
                with self.subTest(action=action, body=name):
                    self.assertEqual(response.status_code, 400, response.text)
                    self.assertEqual(response.json(), {"detail": detail})

        self.assertEqual(
            _fetchone(
                self.db_path,
                "select is_system_admin from users where id='user-self'",
            )[0],
            0,
        )
        self.assertEqual(
            _fetchone(
                self.db_path,
                "select count(*) from audit_logs where action like 'system_admin.%'",
            )[0],
            before_audits,
        )

    def test_system_admin_routes_reject_non_json_media_types_without_writes(self):
        cases = (
            ("missing", {"origin": "http://127.0.0.1:9999"}),
            (
                "text_plain_cross_origin",
                {
                    "content-type": "text/plain",
                    "origin": "http://127.0.0.1:9999",
                },
            ),
        )
        for action in ("grant", "revoke"):
            for name, headers in cases:
                with self.subTest(action=action, media_type=name), TemporaryDirectory() as directory:
                    path = Path(directory) / "target.sqlite3"
                    _create_target_database(path)
                    if action == "revoke":
                        _execute(
                            path,
                            "update users set is_system_admin=1 "
                            "where id='user-self'",
                        )
                    client = TestClient(_create_target_app(path))
                    _login(client, "admin", ADMIN_PASSWORD)
                    before_flag = _fetchone(
                        path,
                        "select is_system_admin from users "
                        "where id='user-self'",
                    )[0]

                    response = client.post(
                        f"/api/users/user-self/system-admin/{action}",
                        content=b'{"reason":"must not run"}',
                        headers=headers,
                    )

                    self.assertEqual(response.status_code, 400, response.text)
                    self.assertEqual(
                        response.json(),
                        {"detail": "invalid_request"},
                    )
                    self.assertEqual(
                        _fetchone(
                            path,
                            "select is_system_admin from users "
                            "where id='user-self'",
                        )[0],
                        before_flag,
                    )
                    self.assertEqual(
                        _fetchone(
                            path,
                            "select count(*) from audit_logs "
                            "where action like 'system_admin.%'",
                        )[0],
                        0,
                    )

    def test_system_admin_routes_accept_json_media_type_parameters(self):
        for action in ("grant", "revoke"):
            for media_type in (
                "application/json",
                "application/json; charset=utf-8",
            ):
                with self.subTest(action=action, media_type=media_type), TemporaryDirectory() as directory:
                    path = Path(directory) / "target.sqlite3"
                    _create_target_database(path)
                    if action == "revoke":
                        _execute(
                            path,
                            "update users set is_system_admin=1 "
                            "where id='user-self'",
                        )
                    client = TestClient(_create_target_app(path))
                    _login(client, "admin", ADMIN_PASSWORD)

                    response = client.post(
                        f"/api/users/user-self/system-admin/{action}",
                        content=b'{"reason":"approved"}',
                        headers={"content-type": media_type},
                    )

                    self.assertEqual(response.status_code, 200, response.text)
                    self.assertEqual(
                        response.json(),
                        {
                            "ok": True,
                            "user_id": "user-self",
                            "is_system_admin": action == "grant",
                        },
                    )

    def test_system_admin_services_do_not_block_the_event_loop(self):
        @self.app.get("/system-admin-event-loop-probe")
        async def event_loop_probe():
            return {"ok": True}

        async def exercise():
            transport = httpx.ASGITransport(app=self.app)
            cookie = self.admin.cookies.get(access_auth.ACCESS_COOKIE_NAME)
            results = []
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://testserver",
                cookies={access_auth.ACCESS_COOKIE_NAME: cookie},
            ) as client:
                for action in ("grant", "revoke"):
                    entered = threading.Event()

                    def blocking_service(*_args, **_kwargs):
                        entered.set()
                        time.sleep(0.4)

                    with patch(
                        "local_app.routes.users.access_service."
                        f"{action}_system_admin",
                        side_effect=blocking_service,
                    ):
                        started = time.monotonic()
                        admin_task = asyncio.create_task(
                            client.post(
                                "/api/users/user-self/system-admin/" + action,
                                json={"reason": "threadpool probe"},
                            )
                        )
                        service_started = await asyncio.to_thread(
                            entered.wait,
                            1.0,
                        )
                        probe_response = await client.get(
                            "/system-admin-event-loop-probe"
                        )
                        probe_elapsed = time.monotonic() - started
                        admin_response = await admin_task
                    results.append(
                        (
                            action,
                            service_started,
                            probe_response,
                            probe_elapsed,
                            admin_response,
                        )
                    )
            return results

        for action, started, probe, elapsed, admin in asyncio.run(exercise()):
            with self.subTest(action=action):
                self.assertTrue(started)
                self.assertEqual(probe.status_code, 200, probe.text)
                self.assertLess(
                    elapsed,
                    0.3,
                    f"{action} blocked the event loop for {elapsed:.3f}s",
                )
                self.assertEqual(admin.status_code, 200, admin.text)

    def test_system_admin_target_not_found_maps_to_404_without_writes(self):
        before_audits = _fetchone(
            self.db_path,
            "select count(*) from audit_logs where action like 'system_admin.%'",
        )[0]
        response = self.admin.post(
            "/api/users/missing/system-admin/grant",
            json={"reason": "missing target"},
        )
        self.assertEqual(response.status_code, 404, response.text)
        self.assertEqual(response.json(), {"detail": "target_not_found"})
        self.assertEqual(
            _fetchone(
                self.db_path,
                "select count(*) from audit_logs where action like 'system_admin.%'",
            )[0],
            before_audits,
        )

    def test_system_admin_domain_conflicts_map_to_409_without_writes(self):
        cases = (
            ("target_not_eligible", "grant", "user-admin", "admin", ()),
            (
                "target_inactive",
                "grant",
                "user-self",
                "admin",
                ("update users set is_active=0 where id='user-self'",),
            ),
            (
                "target_already_admin",
                "grant",
                "user-self",
                "admin",
                ("update users set is_system_admin=1 where id='user-self'",),
            ),
            ("target_not_admin", "revoke", "user-self", "admin", ()),
            (
                "cannot_revoke_self",
                "revoke",
                "user-self",
                "alice",
                ("update users set is_system_admin=1 where id='user-self'",),
            ),
            (
                "admin_floor_violation",
                "revoke",
                "user-self",
                "admin",
                (
                    "update users set account_kind='break_glass' "
                    "where id='user-admin'",
                    "update users set is_system_admin=1 where id='user-self'",
                ),
            ),
        )
        passwords = {"admin": ADMIN_PASSWORD, "alice": SELF_PASSWORD}

        for code, action, target, actor, setup_sql in cases:
            with self.subTest(code=code), TemporaryDirectory() as directory:
                path = Path(directory) / "target.sqlite3"
                _create_target_database(path)
                for statement in setup_sql:
                    _execute(path, statement)
                client = TestClient(_create_target_app(path))
                _login(client, actor, passwords[actor])
                before_flag = _fetchone(
                    path,
                    "select is_system_admin from users where id=?",
                    (target,),
                )[0]
                before_audits = _fetchone(
                    path,
                    "select count(*) from audit_logs "
                    "where action like 'system_admin.%'",
                )[0]

                response = client.post(
                    f"/api/users/{target}/system-admin/{action}",
                    json={"reason": f"exercise {code}"},
                )

                self.assertEqual(response.status_code, 409, response.text)
                self.assertEqual(response.json(), {"detail": code})
                self.assertEqual(
                    _fetchone(
                        path,
                        "select is_system_admin from users where id=?",
                        (target,),
                    )[0],
                    before_flag,
                )
                self.assertEqual(
                    _fetchone(
                        path,
                        "select count(*) from audit_logs "
                        "where action like 'system_admin.%'",
                    )[0],
                    before_audits,
                )

    def test_system_admin_transaction_failure_maps_to_sanitized_500_and_rolls_back(self):
        _execute(
            self.db_path,
            "create trigger block_system_admin_audit before insert on audit_logs "
            "when new.action like 'system_admin.%' "
            "begin select raise(abort, 'sql-secret-path'); end",
        )
        client = TestClient(self.app, raise_server_exceptions=False)
        client.cookies.update(self.admin.cookies)
        response = client.post(
            "/api/users/user-self/system-admin/grant",
            json={"reason": "private reason must not leak"},
        )

        self.assertEqual(response.status_code, 500, response.text)
        self.assertEqual(
            response.json(),
            {"detail": "system_admin_change_unavailable"},
        )
        self.assertNotIn("sql-secret-path", response.text)
        self.assertNotIn("private reason must not leak", response.text)
        self.assertNotIn(str(self.db_path), response.text)
        self.assertEqual(
            _fetchone(
                self.db_path,
                "select is_system_admin from users where id='user-self'",
            )[0],
            0,
        )
        self.assertEqual(
            _fetchone(
                self.db_path,
                "select count(*) from audit_logs where action like 'system_admin.%'",
            )[0],
            0,
        )

    def test_unknown_system_admin_service_error_is_sanitized(self):
        with patch(
            "local_app.routes.users.access_service.grant_system_admin",
            side_effect=access_service.AccessServiceError("future_service_error"),
        ):
            response = self.admin.post(
                "/api/users/user-self/system-admin/grant",
                json={"reason": "safe reason"},
            )

        self.assertEqual(response.status_code, 500, response.text)
        self.assertEqual(
            response.json(),
            {"detail": "system_admin_change_unavailable"},
        )
        self.assertNotIn("future_service_error", response.text)
        self.assertEqual(
            _fetchone(
                self.db_path,
                "select is_system_admin from users where id='user-self'",
            )[0],
            0,
        )

    def test_system_admin_service_authorization_errors_map_stably(self):
        cases = (
            ("actor_not_authorized", "access_denied"),
            ("reauth_invalid", "recent_reauthentication_required"),
            ("reauth_expired", "recent_reauthentication_required"),
        )
        for code, detail in cases:
            with self.subTest(code=code), patch(
                "local_app.routes.users.access_service.grant_system_admin",
                side_effect=access_service.AccessServiceError(code),
            ):
                response = self.admin.post(
                    "/api/users/user-self/system-admin/grant",
                    json={"reason": "safe reason"},
                )
                self.assertEqual(response.status_code, 403, response.text)
                self.assertEqual(response.json(), {"detail": detail})

        self.assertEqual(
            _fetchone(
                self.db_path,
                "select is_system_admin from users where id='user-self'",
            )[0],
            0,
        )

    def test_create_is_staff_only_and_preserves_password_text(self):
        response = self.admin.post(
            "/api/users",
            json={
                "username": "new-doctor",
                "display_name": "New Account",
                "password": " 短 ",
                "staff_id": "staff-new",
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["staff_id"], "staff-new")
        self.assertFalse(body["is_system_admin"])
        self.assertEqual(body["account_kind"], "staff")
        self.assertEqual(body["primary_role"], "doctor")
        self.assertEqual(body["role_keys"], ["doctor", "front"])

        row = _fetchone(
            self.db_path,
            "select password_hash, is_system_admin, account_kind from users "
            "where username='new-doctor'",
        )
        self.assertTrue(verify_password(" 短 ", row[0]))
        self.assertFalse(verify_password("短", row[0]))
        self.assertEqual(row[1:], (0, "staff"))
        other = TestClient(self.app)
        self.assertEqual(
            other.post(
                "/api/auth/login",
                json={"username": "new-doctor", "password": " 短 "},
            ).status_code,
            200,
        )

    def test_create_rejects_client_role_or_admin_controls_without_writes(self):
        for forbidden, value in (
            ("role", "admin"),
            ("is_system_admin", True),
            ("account_kind", "independent_admin"),
        ):
            with self.subTest(forbidden=forbidden):
                payload = {
                    "username": "blocked",
                    "password": "pw",
                    "staff_id": "staff-new",
                    forbidden: value,
                }
                response = self.admin.post("/api/users", json=payload)
                self.assertEqual(response.status_code, 400, response.text)
                self.assertEqual(
                    _fetchone(
                        self.db_path,
                        "select count(*) from users where username='blocked'",
                    )[0],
                    0,
                )

    def test_create_requires_employed_unused_staff_and_unique_username(self):
        cases = (
            (
                {"username": "alice", "password": "pw", "staff_id": "staff-new"},
                409,
            ),
            (
                {"username": "unused", "password": "pw", "staff_id": "staff-self"},
                409,
            ),
            (
                {"username": "former", "password": "pw", "staff_id": "staff-left"},
                400,
            ),
            (
                {"username": "missing", "password": "pw", "staff_id": "no-such"},
                404,
            ),
        )
        for payload, status in cases:
            with self.subTest(payload=payload):
                response = self.admin.post("/api/users", json=payload)
                self.assertEqual(response.status_code, status, response.text)
        self.assertEqual(
            _fetchone(self.db_path, "select count(*) from users")[0], 2
        )

    def test_update_changes_only_ordinary_fields_and_rejects_role_controls(self):
        response = self.admin.put(
            "/api/users/user-self",
            json={"username": "alice-2", "display_name": "Alice Two"},
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["username"], "alice-2")
        self.assertEqual(response.json()["display_name"], "Alice Two")
        before = _fetchone(
            self.db_path,
            "select username, display_name, staff_id, is_system_admin, "
            "account_kind from users where id='user-self'",
        )
        for forbidden, value in (
            ("role", "doctor"),
            ("staff_id", "staff-new"),
            ("is_system_admin", True),
            ("account_kind", "independent_admin"),
        ):
            with self.subTest(forbidden=forbidden):
                response = self.admin.put(
                    "/api/users/user-self", json={forbidden: value}
                )
                self.assertEqual(response.status_code, 400, response.text)
                self.assertEqual(
                    _fetchone(
                        self.db_path,
                        "select username, display_name, staff_id, "
                        "is_system_admin, account_kind from users "
                        "where id='user-self'",
                    ),
                    before,
                )
        duplicate = self.admin.put(
            "/api/users/user-self", json={"username": "admin"}
        )
        self.assertEqual(duplicate.status_code, 409, duplicate.text)

    def test_enable_requires_employed_staff_and_keeps_unlinked_admins_exempt(self):
        former_password = "former-pw"
        _execute(
            self.db_path,
            "insert into users(id, username, display_name, password_hash, "
            "staff_id, is_active, is_system_admin, account_kind) values "
            "('user-left', 'former', 'Former Staff', ?, 'staff-left', "
            "0, 0, 'staff')",
            (hash_password(former_password),),
        )
        enable_audits_before = _fetchone(
            self.db_path,
            "select count(*) from audit_logs where entity_id='user-left' "
            "and action='account.status.enable'",
        )[0]

        response = self.admin.put(
            "/api/users/user-left", json={"is_active": True}
        )

        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(response.json()["detail"], "只有在职员工账号可以启用")
        self.assertEqual(
            _fetchone(
                self.db_path,
                "select is_active from users where id='user-left'",
            )[0],
            0,
        )
        self.assertEqual(
            _fetchone(
                self.db_path,
                "select count(*) from audit_logs where entity_id='user-left' "
                "and action='account.status.enable'",
            )[0],
            enable_audits_before,
        )
        self.assertEqual(
            TestClient(self.app).post(
                "/api/auth/login",
                json={"username": "former", "password": former_password},
            ).status_code,
            401,
        )

        _execute(
            self.db_path,
            "update staff_members set employment_status='employed' "
            "where staff_id='staff-left'",
        )
        self.assertEqual(
            _fetchone(
                self.db_path,
                "select is_active from users where id='user-left'",
            )[0],
            0,
        )
        self.assertEqual(
            TestClient(self.app).post(
                "/api/auth/login",
                json={"username": "former", "password": former_password},
            ).status_code,
            401,
        )

        response = self.admin.put(
            "/api/users/user-left", json={"is_active": True}
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()["is_active"])
        self.assertEqual(
            TestClient(self.app).post(
                "/api/auth/login",
                json={"username": "former", "password": former_password},
            ).status_code,
            200,
        )

        conn = sqlite3.connect(self.db_path)
        try:
            conn.executemany(
                "insert into users(id, username, display_name, password_hash, "
                "staff_id, is_active, is_system_admin, account_kind) "
                "values (?, ?, ?, ?, null, 0, 1, ?)",
                [
                    (
                        "user-independent",
                        "independent",
                        "Independent Admin",
                        hash_password("independent-pw"),
                        "independent_admin",
                    ),
                    (
                        "user-break-glass",
                        "emergency",
                        "Emergency Admin",
                        hash_password("emergency-pw"),
                        "break_glass",
                    ),
                ],
            )
            conn.commit()
        finally:
            conn.close()
        for user_id in ("user-independent", "user-break-glass"):
            with self.subTest(account=user_id):
                response = self.admin.put(
                    f"/api/users/{user_id}", json={"is_active": True}
                )
                self.assertEqual(response.status_code, 200, response.text)
                self.assertTrue(response.json()["is_active"])

    def test_disable_clears_target_sessions_blocks_login_and_audits_actor(self):
        alice = TestClient(self.app)
        raw_token = _login(alice, "alice", SELF_PASSWORD)
        self.assertIsNotNone(
            session_service.resolve_session(
                self.db_path, raw_token, now=NOW, renew_activity=False
            )
        )

        response = self.admin.put(
            "/api/users/user-self", json={"is_active": False}
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertFalse(response.json()["is_active"])
        self.assertIsNone(
            session_service.resolve_session(
                self.db_path, raw_token, now=NOW, renew_activity=False
            )
        )
        self.assertEqual(
            TestClient(self.app).post(
                "/api/auth/login",
                json={"username": "alice", "password": SELF_PASSWORD},
            ).status_code,
            401,
        )
        audit = _fetchone(
            self.db_path,
            "select operator, actor_user_id from audit_logs "
            "where action='account.status.disable'",
        )
        self.assertEqual(audit, ("admin", "user-admin"))

    def test_cannot_disable_self(self):
        before_sessions = _fetchone(
            self.db_path,
            "select count(*) from sessions where user_id='user-admin'",
        )[0]
        response = self.admin.put(
            "/api/users/user-admin", json={"is_active": False}
        )
        self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(
            _fetchone(
                self.db_path,
                "select is_active from users where id='user-admin'",
            )[0],
            1,
        )
        self.assertEqual(
            _fetchone(
                self.db_path,
                "select count(*) from sessions where user_id='user-admin'",
            )[0],
            before_sessions,
        )

    def test_break_glass_cannot_disable_the_only_regular_system_admin(self):
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                "insert into users(id, username, display_name, password_hash, "
                "staff_id, is_active, is_system_admin, account_kind) "
                "values ('user-break-glass', 'emergency', 'Emergency', ?, "
                "null, 1, 1, 'break_glass')",
                (hash_password("emergency-pw"),),
            )
            conn.commit()
        finally:
            conn.close()

        emergency = TestClient(self.app)
        _login(emergency, "emergency", "emergency-pw")
        before_sessions = _fetchone(
            self.db_path,
            "select count(*) from sessions where user_id='user-admin'",
        )[0]
        before_audits = _fetchone(
            self.db_path,
            "select count(*) from audit_logs where entity_id='user-admin' "
            "and action='account.status.disable'",
        )[0]

        response = emergency.put(
            "/api/users/user-admin", json={"is_active": False}
        )

        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(
            _fetchone(
                self.db_path,
                "select is_active from users where id='user-admin'",
            )[0],
            1,
        )
        self.assertEqual(
            _fetchone(
                self.db_path,
                "select count(*) from sessions where user_id='user-admin'",
            )[0],
            before_sessions,
        )
        self.assertEqual(
            _fetchone(
                self.db_path,
                "select count(*) from audit_logs where entity_id='user-admin' "
                "and action='account.status.disable'",
            )[0],
            before_audits,
        )
        self.assertEqual(
            _fetchone(
                self.db_path,
                "select count(*) from users where is_active=1 "
                "and is_system_admin=1 and account_kind!='break_glass'",
            )[0],
            1,
        )

    def test_reset_password_clears_sessions_and_audits_actor(self):
        alice = TestClient(self.app)
        raw_token = _login(alice, "alice", SELF_PASSWORD)
        response = self.admin.post(
            "/api/users/user-self/reset-password",
            json={"password": "new-reset"},
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIsNone(
            session_service.resolve_session(
                self.db_path, raw_token, now=NOW, renew_activity=False
            )
        )
        other = TestClient(self.app)
        self.assertEqual(
            other.post(
                "/api/auth/login",
                json={"username": "alice", "password": SELF_PASSWORD},
            ).status_code,
            401,
        )
        self.assertEqual(
            other.post(
                "/api/auth/login",
                json={"username": "alice", "password": "new-reset"},
            ).status_code,
            200,
        )
        self.assertEqual(
            _fetchone(
                self.db_path,
                "select operator, actor_user_id from audit_logs "
                "where action='account.password.reset'",
            ),
            ("admin", "user-admin"),
        )

    def test_change_password_clears_all_own_sessions_and_audits_self(self):
        alice_one = TestClient(self.app)
        raw_one = _login(alice_one, "alice", SELF_PASSWORD)
        alice_two = TestClient(self.app)
        raw_two = _login(alice_two, "alice", SELF_PASSWORD)

        response = alice_one.post(
            "/api/auth/change-password",
            json={"old_password": SELF_PASSWORD, "new_password": "new-self"},
        )
        self.assertEqual(response.status_code, 200, response.text)
        for raw_token in (raw_one, raw_two):
            self.assertIsNone(
                session_service.resolve_session(
                    self.db_path, raw_token, now=NOW, renew_activity=False
                )
            )
        other = TestClient(self.app)
        self.assertEqual(
            other.post(
                "/api/auth/login",
                json={"username": "alice", "password": SELF_PASSWORD},
            ).status_code,
            401,
        )
        self.assertEqual(
            other.post(
                "/api/auth/login",
                json={"username": "alice", "password": "new-self"},
            ).status_code,
            200,
        )
        self.assertEqual(
            _fetchone(
                self.db_path,
                "select operator, actor_user_id from audit_logs "
                "where action='account.password.change'",
            ),
            ("alice", "user-self"),
        )

    def test_surrogate_passwords_are_400_with_no_side_effects(self):
        create_response = _raw_post(
            self.admin,
            "/api/users",
            '{"username":"bad","staff_id":"staff-new",'
            '"password":"%s"}' % SURROGATE_JSON,
        )
        self.assertEqual(create_response.status_code, 400, create_response.text)
        self.assertEqual(
            _fetchone(
                self.db_path,
                "select count(*) from users where username='bad'",
            )[0],
            0,
        )

        alice = TestClient(self.app)
        raw_token = _login(alice, "alice", SELF_PASSWORD)
        before = _fetchone(
            self.db_path,
            "select password_hash from users where id='user-self'",
        )[0]
        reset_response = _raw_post(
            self.admin,
            "/api/users/user-self/reset-password",
            '{"password":"%s"}' % SURROGATE_JSON,
        )
        self.assertEqual(reset_response.status_code, 400, reset_response.text)
        change_response = _raw_post(
            alice,
            "/api/auth/change-password",
            '{"old_password":"%s","new_password":"%s"}'
            % (SELF_PASSWORD, SURROGATE_JSON),
        )
        self.assertEqual(change_response.status_code, 400, change_response.text)
        self.assertEqual(
            _fetchone(
                self.db_path,
                "select password_hash from users where id='user-self'",
            )[0],
            before,
        )
        self.assertIsNotNone(
            session_service.resolve_session(
                self.db_path, raw_token, now=NOW, renew_activity=False
            )
        )

    def test_high_risk_changes_roll_back_when_same_transaction_audit_fails(self):
        for operation in ("disable", "reset", "change"):
            with self.subTest(operation=operation):
                with TemporaryDirectory() as directory:
                    path = Path(directory) / "target.sqlite3"
                    _create_target_database(path)
                    app = _create_target_app(path)
                    admin = TestClient(app, raise_server_exceptions=False)
                    _login(admin, "admin", ADMIN_PASSWORD)
                    alice = TestClient(app, raise_server_exceptions=False)
                    _login(alice, "alice", SELF_PASSWORD)
                    before_hash = _fetchone(
                        path,
                        "select password_hash from users where id='user-self'",
                    )[0]
                    before_sessions = _fetchone(
                        path,
                        "select count(*) from sessions where user_id='user-self'",
                    )[0]
                    conn = sqlite3.connect(path)
                    try:
                        conn.execute(
                            "create trigger block_account_audit before insert "
                            "on audit_logs when new.action like 'account.%' "
                            "begin select raise(abort, 'blocked'); end"
                        )
                        conn.commit()
                    finally:
                        conn.close()

                    if operation == "disable":
                        response = admin.put(
                            "/api/users/user-self", json={"is_active": False}
                        )
                    elif operation == "reset":
                        response = admin.post(
                            "/api/users/user-self/reset-password",
                            json={"password": "new-reset"},
                        )
                    else:
                        response = alice.post(
                            "/api/auth/change-password",
                            json={
                                "old_password": SELF_PASSWORD,
                                "new_password": "new-self",
                            },
                        )
                    self.assertEqual(response.status_code, 500, response.text)
                    self.assertEqual(
                        _fetchone(
                            path,
                            "select is_active, password_hash from users "
                            "where id='user-self'",
                        ),
                        (1, before_hash),
                    )
                    self.assertEqual(
                        _fetchone(
                            path,
                            "select count(*) from sessions "
                            "where user_id='user-self'",
                        )[0],
                        before_sessions,
                    )

    def test_create_rechecks_disabled_or_revoked_actor_inside_write_transaction(self):
        router = create_users_router(self.db_path, access_v3=True)
        create = _endpoint(router, "POST", "/api/users")

        def state():
            return (
                _fetchall(
                    self.db_path,
                    "select id, username, display_name, staff_id, is_active, "
                    "is_system_admin, account_kind, created_at, updated_at "
                    "from users order by id",
                ),
                _fetchone(self.db_path, "select count(*) from sessions")[0],
                _fetchone(self.db_path, "select count(*) from audit_logs")[0],
            )

        _execute(
            self.db_path,
            "update users set is_active = 0 where id = 'user-admin'",
        )
        before = state()
        with _as(_principal()), self.assertRaises(HTTPException) as caught:
            create(
                {
                    "username": "late-user",
                    "password": "pw",
                    "staff_id": "staff-new",
                }
            )
        self.assertEqual(caught.exception.status_code, 401)
        self.assertEqual(state(), before)

        _execute(
            self.db_path,
            "update users set is_active = 1 where id = 'user-admin'",
        )
        _execute(
            self.db_path,
            "delete from role_permissions "
            "where role_key = 'front' and perm_key = 'account.open'",
        )
        before = state()
        stale_actor = _principal(
            user_id="user-self",
            username="alice",
            staff_id="staff-self",
            system_admin=False,
            permissions=("account.open",),
        )
        with _as(stale_actor), self.assertRaises(HTTPException) as caught:
            create(
                {
                    "username": "late-user",
                    "password": "pw",
                    "staff_id": "staff-new",
                }
            )
        self.assertEqual(caught.exception.status_code, 403)
        self.assertEqual(state(), before)

    def test_update_rechecks_disabled_or_revoked_actor_inside_write_transaction(self):
        router = create_users_router(self.db_path, access_v3=True)
        update = _endpoint(router, "PUT", "/api/users/{user_id}")

        def state():
            return (
                _fetchone(
                    self.db_path,
                    "select username, display_name, staff_id, is_active, "
                    "is_system_admin, account_kind, created_at, updated_at "
                    "from users where id = 'user-self'",
                ),
                _fetchone(self.db_path, "select count(*) from users")[0],
                _fetchone(self.db_path, "select count(*) from sessions")[0],
                _fetchone(self.db_path, "select count(*) from audit_logs")[0],
            )

        _execute(
            self.db_path,
            "update users set is_active = 0 where id = 'user-admin'",
        )
        before = state()
        with _as(_principal()), self.assertRaises(HTTPException) as caught:
            update("user-self", {"display_name": "停用后迟到请求"})
        self.assertEqual(caught.exception.status_code, 401)
        self.assertEqual(state(), before)

        _execute(
            self.db_path,
            "update users set is_active = 1 where id = 'user-admin'",
        )
        _execute(
            self.db_path,
            "delete from role_permissions "
            "where role_key = 'front' and perm_key = 'account.open'",
        )
        before = state()
        stale_actor = _principal(
            user_id="user-self",
            username="alice",
            staff_id="staff-self",
            system_admin=False,
            permissions=("account.open",),
        )
        with _as(stale_actor), self.assertRaises(HTTPException) as caught:
            update("user-self", {"display_name": "撤权后迟到请求"})
        self.assertEqual(caught.exception.status_code, 403)
        self.assertEqual(state(), before)

    def test_reset_password_rechecks_disabled_or_revoked_actor_inside_write_transaction(self):
        router = create_users_router(self.db_path, access_v3=True)
        reset = _endpoint(
            router,
            "POST",
            "/api/users/{user_id}/reset-password",
        )
        alice = TestClient(self.app)
        _login(alice, "alice", SELF_PASSWORD)

        def state():
            return (
                _fetchone(
                    self.db_path,
                    "select password_hash, updated_at from users "
                    "where id = 'user-self'",
                ),
                _fetchone(self.db_path, "select count(*) from sessions")[0],
                _fetchone(self.db_path, "select count(*) from audit_logs")[0],
            )

        _execute(
            self.db_path,
            "update users set is_active = 0 where id = 'user-admin'",
        )
        before = state()
        with _as(_principal()), self.assertRaises(HTTPException) as caught:
            reset("user-self", {"password": "late-pw"})
        self.assertEqual(caught.exception.status_code, 401)
        self.assertEqual(state(), before)

        _execute(
            self.db_path,
            "update users set is_active = 1 where id = 'user-admin'",
        )
        _execute(
            self.db_path,
            "delete from role_permissions "
            "where role_key = 'front' and perm_key = 'account.security'",
        )
        before = state()
        stale_actor = _principal(
            user_id="user-self",
            username="alice",
            staff_id="staff-self",
            system_admin=False,
            permissions=("account.security",),
        )
        with _as(stale_actor), self.assertRaises(HTTPException) as caught:
            reset("user-self", {"password": "late-pw"})
        self.assertEqual(caught.exception.status_code, 403)
        self.assertEqual(state(), before)


if __name__ == "__main__":
    unittest.main()
