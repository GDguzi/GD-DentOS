"""目标人员 schema：岗位归一、离职撤权、复职和普通账号开户。"""

import json
import sqlite3
import unittest
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from local_app import access_authorization as authorization
from local_app import access_session_service
from local_app.passwords import hash_password, verify_password
from local_app.routes import staff


ROLE_ROWS = (
    ("admin", "管理员"),
    ("doctor", "医生"),
    ("nurse", "护士"),
    ("reception", "前台"),
)


def _database(root):
    db_path = Path(root) / "target.sqlite3"
    schema = (
        Path(__file__).parents[1]
        / "local_app"
        / "personnel_access_schema.sql"
    ).read_text(encoding="utf-8")
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "create table roles(role_key text primary key, name text not null unique)"
        )
        conn.execute(
            "create table role_permissions("
            "role_key text not null, perm_key text not null, "
            "primary key(role_key, perm_key))"
        )
        conn.executemany("insert into roles(role_key, name) values (?, ?)", ROLE_ROWS)
        conn.executescript(schema)
        conn.execute(
            "insert into staff_members"
            "(staff_id, name, employment_status) "
            "values ('staff-actor', '系统管理员', 'employed')"
        )
        conn.execute(
            "insert into staff_role_assignments"
            "(staff_id, role_key, is_primary) values ('staff-actor', 'admin', 1)"
        )
        conn.execute(
            "insert into users"
            "(id, username, display_name, password_hash, staff_id, is_active, "
            "is_system_admin, account_kind) "
            "values ('user-actor', 'admin', '系统管理员', ?, 'staff-actor', 1, 1, 'staff')",
            (hash_password("admin-pw"),),
        )
        conn.commit()
    finally:
        conn.close()
    return db_path


def _add_staff(
    db_path,
    *,
    staff_id="staff-target",
    name="张医生",
    status="employed",
    primary="doctor",
    secondary=(),
    with_account=False,
):
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "insert into staff_members"
            "(staff_id, name, note, job_no, phone, id_card, license_no, "
            "employment_status, left_reason) values (?, ?, '档案备注', 'D001', "
            "'13800000000', '990108199001010000', 'LIC-1', ?, '')",
            (staff_id, name, status),
        )
        conn.execute(
            "insert into staff_role_assignments"
            "(staff_id, role_key, is_primary) values (?, ?, 1)",
            (staff_id, primary),
        )
        for role_key in secondary:
            conn.execute(
                "insert into staff_role_assignments"
                "(staff_id, role_key, is_primary) values (?, ?, 0)",
                (staff_id, role_key),
            )
        if with_account:
            conn.execute(
                "insert into users"
                "(id, username, display_name, password_hash, staff_id, is_active, "
                "is_system_admin, account_kind) "
                "values (?, ?, ?, ?, ?, 1, 0, 'staff')",
                (
                    "user-" + staff_id,
                    "user-" + staff_id,
                    name,
                    hash_password("pw"),
                    staff_id,
                ),
            )
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
    user_id="user-actor",
    username="admin",
    staff_id="staff-actor",
    system_admin=True,
    permissions=(),
):
    return authorization.AccessPrincipal(
        user_id=user_id,
        username=username,
        display_name=username,
        staff_id=staff_id,
        is_system_admin=system_admin,
        primary_role="admin" if staff_id else None,
        role_keys=("admin",) if staff_id else (),
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


class TargetStaffTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = TemporaryDirectory()
        self.db_path = _database(self.temp_dir.name)
        self.router = staff.create_staff_router(self.db_path, access_v3=True)

    def tearDown(self):
        self.temp_dir.cleanup()

    def call(self, method, path, *args, **kwargs):
        endpoint = _endpoint(self.router, method, path)
        with _as(_principal()):
            return endpoint(*args, **kwargs)

    def test_directory_is_employed_lightweight_and_roles_are_stable(self):
        _add_staff(self.db_path, secondary=("nurse",))
        _add_staff(
            self.db_path,
            staff_id="staff-left",
            name="已离职",
            status="left",
            primary="nurse",
        )

        result = self.call("GET", "/api/staff-members", role="")
        target = next(
            member
            for member in result["members"]
            if member["staff_id"] == "staff-target"
        )
        self.assertEqual(
            target,
            {
                "staff_id": "staff-target",
                "name": "张医生",
                "primary_role": "doctor",
                "role_keys": ["doctor", "nurse"],
                "role": "医生",
                "roles": ["医生", "护士"],
            },
        )
        self.assertNotIn("staff-left", {m["staff_id"] for m in result["members"]})
        self.assertNotIn("phone", target)
        self.assertNotIn("id_card", target)

        for exact in ("nurse", "护士"):
            filtered = self.call("GET", "/api/staff-members", role=exact)
            self.assertIn(
                "staff-target", {m["staff_id"] for m in filtered["members"]}
            )
        guessed = self.call("GET", "/api/staff-members", role="护")
        self.assertEqual(guessed["members"], [])

    def test_admin_list_returns_profile_roles_and_account_summary(self):
        _add_staff(self.db_path, with_account=True)
        result = self.call("GET", "/api/staff-admin/list")
        target = next(
            member
            for member in result["members"]
            if member["staff_id"] == "staff-target"
        )
        self.assertEqual(target["phone"], "13800000000")
        self.assertEqual(target["id_card"], "990108199001010000")
        self.assertEqual(target["primary_role"], "doctor")
        self.assertEqual(target["role_keys"], ["doctor"])
        self.assertEqual(
            target["account"],
            {
                "user_id": "user-staff-target",
                "username": "user-staff-target",
                "is_active": True,
                "is_system_admin": False,
                "account_kind": "staff",
            },
        )

    def test_create_and_update_replace_assignments_atomically(self):
        created = self.call(
            "POST",
            "/api/staff-members",
            {
                "name": "李医生",
                "primary_role": "doctor",
                "role_keys": ["nurse", "doctor"],
                "phone": "13900000000",
            },
        )
        staff_id = created["staff_id"]
        self.assertEqual(created["role_keys"], ["doctor", "nurse"])

        updated = self.call(
            "PUT",
            "/api/staff-members/{staff_id}",
            staff_id,
            {
                "name": "李护士",
                "primary_role": "nurse",
                "role_keys": ["nurse"],
                "note": "转岗",
            },
        )
        self.assertEqual(updated["primary_role"], "nurse")
        self.assertEqual(updated["role_keys"], ["nurse"])

        conn = sqlite3.connect(self.db_path)
        try:
            rows = conn.execute(
                "select role_key, is_primary from staff_role_assignments "
                "where staff_id = ? order by role_key",
                (staff_id,),
            ).fetchall()
            name = conn.execute(
                "select name from staff_members where staff_id = ?", (staff_id,)
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(rows, [("nurse", 1)])
        self.assertEqual(name, "李护士")

    def test_legacy_role_names_require_exact_roles_lookup(self):
        created = self.call(
            "POST",
            "/api/staff-members",
            {"name": "双岗", "role": "医生", "roles": ["护士", "医生"]},
        )
        self.assertEqual(created["primary_role"], "doctor")
        self.assertEqual(created["role_keys"], ["doctor", "nurse"])

        with self.assertRaises(HTTPException) as caught:
            self.call(
                "POST",
                "/api/staff-members",
                {"name": "猜岗位", "role": "医", "roles": ["医"]},
            )
        self.assertEqual(caught.exception.status_code, 400)

    def test_role_replacement_failure_rolls_back_profile_and_assignments(self):
        _add_staff(self.db_path)
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                "create trigger reject_nurse before insert on staff_role_assignments "
                "when new.staff_id = 'staff-target' and new.role_key = 'nurse' "
                "begin select raise(abort, 'reject'); end"
            )
            conn.commit()
        finally:
            conn.close()

        with self.assertRaises(sqlite3.IntegrityError):
            self.call(
                "PUT",
                "/api/staff-members/{staff_id}",
                "staff-target",
                {
                    "name": "不应落库",
                    "primary_role": "nurse",
                    "role_keys": ["nurse"],
                },
            )

        conn = sqlite3.connect(self.db_path)
        try:
            name = conn.execute(
                "select name from staff_members where staff_id = 'staff-target'"
            ).fetchone()[0]
            roles = conn.execute(
                "select role_key from staff_role_assignments "
                "where staff_id = 'staff-target'"
            ).fetchall()
        finally:
            conn.close()
        self.assertEqual(name, "张医生")
        self.assertEqual(roles, [("doctor",)])

    def test_departure_revokes_account_sessions_and_records_real_actor(self):
        _add_staff(self.db_path, with_account=True)
        raw_token, _context = access_session_service.authenticate_and_create_session(
            self.db_path,
            username="user-staff-target",
            password="pw",
            device_name="desk",
            user_agent="test",
            ip_address="127.0.0.1",
            request_id="a" * 16,
        )
        result = self.call(
            "DELETE",
            "/api/staff-members/{staff_id}",
            "staff-target",
            {"reason": "主动离职"},
        )
        self.assertTrue(result["account_disabled"])

        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            member = conn.execute(
                "select employment_status, left_at, left_reason from staff_members "
                "where staff_id = 'staff-target'"
            ).fetchone()
            user = conn.execute(
                "select is_active from users where staff_id = 'staff-target'"
            ).fetchone()
            sessions = conn.execute(
                "select count(*) from sessions where user_id = 'user-staff-target'"
            ).fetchone()[0]
            audit = conn.execute(
                "select operator, actor_user_id, reason from audit_logs "
                "where action = 'staff.employment.left'"
            ).fetchone()
        finally:
            conn.close()
        self.assertEqual(member["employment_status"], "left")
        self.assertTrue(member["left_at"])
        self.assertEqual(member["left_reason"], "主动离职")
        self.assertEqual(user["is_active"], 0)
        self.assertEqual(sessions, 0)
        self.assertEqual(audit["operator"], "admin")
        self.assertEqual(audit["actor_user_id"], "user-actor")
        self.assertEqual(audit["reason"], "主动离职")
        self.assertIsNone(
            access_session_service.resolve_session(self.db_path, raw_token)
        )

    def test_reinstate_preserves_departure_audit_and_keeps_access_disabled(self):
        _add_staff(self.db_path, with_account=True)
        access_session_service.authenticate_and_create_session(
            self.db_path,
            username="user-staff-target",
            password="pw",
            device_name="desk",
            user_agent="test",
            ip_address="127.0.0.1",
            request_id="c" * 16,
        )
        self.call(
            "DELETE",
            "/api/staff-members/{staff_id}",
            "staff-target",
            {"reason": "原离职原因"},
        )

        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            departed = conn.execute(
                "select employment_status, left_at, left_reason, updated_at "
                "from staff_members where staff_id = 'staff-target'"
            ).fetchone()
            account_updated_at = conn.execute(
                "select updated_at from users where id = 'user-staff-target'"
            ).fetchone()["updated_at"]
        finally:
            conn.close()
        expected_old = {
            "employment_status": departed["employment_status"],
            "left_at": departed["left_at"],
            "left_reason": departed["left_reason"],
        }

        result = self.call(
            "POST",
            "/api/staff-members/{staff_id}/reinstate",
            "staff-target",
            {"reason": "返聘复职"},
        )
        self.assertEqual(
            result,
            {
                "ok": True,
                "staff_id": "staff-target",
                "employment_status": "employed",
                "account_reenabled": False,
            },
        )

        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            member = conn.execute(
                "select employment_status, left_at, left_reason, updated_at "
                "from staff_members where staff_id = 'staff-target'"
            ).fetchone()
            user = conn.execute(
                "select is_active, updated_at from users "
                "where id = 'user-staff-target'"
            ).fetchone()
            user_count = conn.execute(
                "select count(*) as n from users "
                "where staff_id = 'staff-target'"
            ).fetchone()["n"]
            sessions = conn.execute(
                "select count(*) as n from sessions "
                "where user_id = 'user-staff-target'"
            ).fetchone()["n"]
            audit = conn.execute(
                "select entity_type, entity_id, action, old_json, new_json, "
                "operator, created_at, actor_user_id, result, reason, risk_level "
                "from audit_logs where action = 'staff.employment.reinstate'"
            ).fetchone()
        finally:
            conn.close()
        self.assertEqual(member["employment_status"], "employed")
        self.assertIsNone(member["left_at"])
        self.assertEqual(member["left_reason"], "")
        self.assertEqual(user["is_active"], 0)
        self.assertEqual(user["updated_at"], account_updated_at)
        self.assertEqual(user_count, 1)
        self.assertEqual(sessions, 0)
        self.assertEqual(audit["entity_type"], "staff")
        self.assertEqual(audit["entity_id"], "staff-target")
        self.assertEqual(audit["action"], "staff.employment.reinstate")
        self.assertEqual(json.loads(audit["old_json"]), expected_old)
        self.assertEqual(
            json.loads(audit["new_json"]),
            {
                "employment_status": "employed",
                "left_at": None,
                "left_reason": "",
            },
        )
        self.assertEqual(audit["operator"], "admin")
        self.assertEqual(audit["actor_user_id"], "user-actor")
        self.assertEqual(audit["result"], "success")
        self.assertEqual(audit["reason"], "返聘复职")
        self.assertEqual(audit["risk_level"], "high")
        self.assertEqual(audit["created_at"], member["updated_at"])

    def test_reinstate_rechecks_disabled_or_revoked_actor_inside_write_transaction(self):
        _add_staff(self.db_path, status="left")
        _add_staff(
            self.db_path,
            staff_id="staff-target-2",
            name="第二名离职人员",
            status="left",
        )
        reinstate = _endpoint(
            self.router,
            "POST",
            "/api/staff-members/{staff_id}/reinstate",
        )

        def state(staff_id):
            conn = sqlite3.connect(self.db_path)
            try:
                return (
                    conn.execute(
                        "select employment_status, left_at, left_reason, updated_at "
                        "from staff_members where staff_id = ?",
                        (staff_id,),
                    ).fetchone(),
                    conn.execute(
                        "select count(*) from audit_logs "
                        "where action = 'staff.employment.reinstate'"
                    ).fetchone()[0],
                )
            finally:
                conn.close()

        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                "update users set is_active = 0 where id = 'user-actor'"
            )
            conn.commit()
        finally:
            conn.close()
        before = state("staff-target")
        with _as(_principal()), self.assertRaises(HTTPException) as caught:
            reinstate("staff-target", {"reason": "停用后迟到请求"})
        self.assertEqual(caught.exception.status_code, 401)
        self.assertEqual(state("staff-target"), before)

        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                "update users set is_active = 1, is_system_admin = 0 "
                "where id = 'user-actor'"
            )
            conn.commit()
        finally:
            conn.close()
        before = state("staff-target-2")
        stale_actor = _principal(
            system_admin=False,
            permissions=("staff.employment",),
        )
        with _as(stale_actor), self.assertRaises(HTTPException) as caught:
            reinstate("staff-target-2", {"reason": "撤权后迟到请求"})
        self.assertEqual(caught.exception.status_code, 403)
        self.assertEqual(state("staff-target-2"), before)

    def test_departure_rechecks_disabled_or_revoked_actor_inside_write_transaction(self):
        _add_staff(self.db_path, with_account=True)
        _add_staff(
            self.db_path,
            staff_id="staff-target-2",
            name="第二名在职人员",
            status="employed",
            with_account=True,
        )
        depart = _endpoint(
            self.router,
            "DELETE",
            "/api/staff-members/{staff_id}",
        )
        access_session_service.authenticate_and_create_session(
            self.db_path,
            username="user-staff-target",
            password="pw",
            device_name="desk",
            user_agent="test",
            ip_address="127.0.0.1",
            request_id="b" * 16,
        )

        def state(staff_id):
            conn = sqlite3.connect(self.db_path)
            try:
                return (
                    conn.execute(
                        "select employment_status, left_at, left_reason, updated_at "
                        "from staff_members where staff_id = ?",
                        (staff_id,),
                    ).fetchone(),
                    conn.execute(
                        "select is_active from users where staff_id = ?",
                        (staff_id,),
                    ).fetchone(),
                    conn.execute(
                        "select count(*) from sessions s "
                        "join users u on u.id = s.user_id where u.staff_id = ?",
                        (staff_id,),
                    ).fetchone()[0],
                    conn.execute(
                        "select count(*) from audit_logs "
                        "where action = 'staff.employment.left'"
                    ).fetchone()[0],
                )
            finally:
                conn.close()

        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                "update users set is_active = 0 where id = 'user-actor'"
            )
            conn.commit()
        finally:
            conn.close()
        before = state("staff-target")
        with _as(_principal()), self.assertRaises(HTTPException) as caught:
            depart("staff-target", {"reason": "停用后迟到请求"})
        self.assertEqual(caught.exception.status_code, 401)
        self.assertEqual(state("staff-target"), before)

        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                "update users set is_active = 1, is_system_admin = 0 "
                "where id = 'user-actor'"
            )
            conn.commit()
        finally:
            conn.close()
        before = state("staff-target-2")
        stale_actor = _principal(
            system_admin=False,
            permissions=("staff.employment",),
        )
        with _as(stale_actor), self.assertRaises(HTTPException) as caught:
            depart("staff-target-2", {"reason": "撤权后迟到请求"})
        self.assertEqual(caught.exception.status_code, 403)
        self.assertEqual(state("staff-target-2"), before)

    def test_create_rechecks_disabled_or_revoked_actor_inside_write_transaction(self):
        create = _endpoint(self.router, "POST", "/api/staff-members")

        def state():
            conn = sqlite3.connect(self.db_path)
            try:
                return (
                    conn.execute(
                        "select staff_id, name, note, employment_status, "
                        "created_at, updated_at from staff_members "
                        "order by staff_id"
                    ).fetchall(),
                    conn.execute("select count(*) from users").fetchone()[0],
                    conn.execute("select count(*) from sessions").fetchone()[0],
                    conn.execute(
                        "select count(*) from audit_logs"
                    ).fetchone()[0],
                )
            finally:
                conn.close()

        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                "update users set is_active = 0 where id = 'user-actor'"
            )
            conn.commit()
        finally:
            conn.close()
        before = state()
        with _as(_principal()), self.assertRaises(HTTPException) as caught:
            create(
                {
                    "name": "停用后迟到请求",
                    "primary_role": "doctor",
                    "role_keys": ["doctor"],
                }
            )
        self.assertEqual(caught.exception.status_code, 401)
        self.assertEqual(state(), before)

        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                "update users set is_active = 1, is_system_admin = 0 "
                "where id = 'user-actor'"
            )
            conn.commit()
        finally:
            conn.close()
        before = state()
        stale_actor = _principal(
            system_admin=False,
            permissions=("staff.edit",),
        )
        with _as(stale_actor), self.assertRaises(HTTPException) as caught:
            create(
                {
                    "name": "撤权后迟到请求",
                    "primary_role": "doctor",
                    "role_keys": ["doctor"],
                }
            )
        self.assertEqual(caught.exception.status_code, 403)
        self.assertEqual(state(), before)

    def test_update_rechecks_disabled_or_revoked_actor_inside_write_transaction(self):
        _add_staff(self.db_path)
        _add_staff(
            self.db_path,
            staff_id="staff-target-2",
            name="第二名在职人员",
            status="employed",
        )
        update = _endpoint(
            self.router,
            "PUT",
            "/api/staff-members/{staff_id}",
        )

        def state(staff_id):
            conn = sqlite3.connect(self.db_path)
            try:
                return (
                    conn.execute(
                        "select staff_id, name, note, updated_at "
                        "from staff_members where staff_id = ?",
                        (staff_id,),
                    ).fetchone(),
                    conn.execute(
                        "select role_key, is_primary from staff_role_assignments "
                        "where staff_id = ? order by role_key",
                        (staff_id,),
                    ).fetchall(),
                    conn.execute("select count(*) from users").fetchone()[0],
                    conn.execute("select count(*) from sessions").fetchone()[0],
                    conn.execute(
                        "select count(*) from audit_logs"
                    ).fetchone()[0],
                )
            finally:
                conn.close()

        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                "update users set is_active = 0 where id = 'user-actor'"
            )
            conn.commit()
        finally:
            conn.close()
        before = state("staff-target")
        with _as(_principal()), self.assertRaises(HTTPException) as caught:
            update(
                "staff-target",
                {
                    "name": "停用后迟到请求",
                    "primary_role": "doctor",
                    "role_keys": ["doctor"],
                },
            )
        self.assertEqual(caught.exception.status_code, 401)
        self.assertEqual(state("staff-target"), before)

        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                "update users set is_active = 1, is_system_admin = 0 "
                "where id = 'user-actor'"
            )
            conn.commit()
        finally:
            conn.close()
        before = state("staff-target-2")
        stale_actor = _principal(
            system_admin=False,
            permissions=("staff.edit",),
        )
        with _as(stale_actor), self.assertRaises(HTTPException) as caught:
            update(
                "staff-target-2",
                {
                    "name": "撤权后迟到请求",
                    "primary_role": "doctor",
                    "role_keys": ["doctor"],
                },
            )
        self.assertEqual(caught.exception.status_code, 403)
        self.assertEqual(state("staff-target-2"), before)

    def test_open_account_rechecks_disabled_or_revoked_actor_inside_write_transaction(self):
        _add_staff(self.db_path, staff_id="staff-open", name="待开户")
        _add_staff(
            self.db_path,
            staff_id="staff-open-2",
            name="第二待开户",
            status="employed",
        )
        open_account = _endpoint(
            self.router,
            "POST",
            "/api/staff-members/{staff_id}/account",
        )

        def state():
            conn = sqlite3.connect(self.db_path)
            try:
                return (
                    conn.execute(
                        "select id, username, display_name, staff_id, "
                        "is_active, is_system_admin, account_kind, "
                        "created_at, updated_at from users order by id"
                    ).fetchall(),
                    conn.execute("select count(*) from sessions").fetchone()[0],
                    conn.execute(
                        "select count(*) from audit_logs"
                    ).fetchone()[0],
                )
            finally:
                conn.close()

        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                "update users set is_active = 0 where id = 'user-actor'"
            )
            conn.commit()
        finally:
            conn.close()
        before = state()
        with _as(_principal()), self.assertRaises(HTTPException) as caught:
            open_account(
                "staff-open",
                {"username": "late-open", "password": "pw"},
            )
        self.assertEqual(caught.exception.status_code, 401)
        self.assertEqual(state(), before)

        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                "update users set is_active = 1, is_system_admin = 0 "
                "where id = 'user-actor'"
            )
            conn.commit()
        finally:
            conn.close()
        before = state()
        stale_actor = _principal(
            system_admin=False,
            permissions=("account.open",),
        )
        with _as(stale_actor), self.assertRaises(HTTPException) as caught:
            open_account(
                "staff-open-2",
                {"username": "late-open-2", "password": "pw"},
            )
        self.assertEqual(caught.exception.status_code, 403)
        self.assertEqual(state(), before)

    def test_reinstate_rejects_invalid_state_payload_and_permission_without_writes(self):
        _add_staff(self.db_path, status="left", with_account=True)
        _add_staff(
            self.db_path,
            staff_id="staff-employed",
            name="在职人员",
            status="employed",
        )
        _add_staff(
            self.db_path,
            staff_id="staff-archived",
            name="归档人员",
            status="archived",
        )
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                "update staff_members set left_at = '2026-07-16 09:00:00', "
                "left_reason = '历史原因' where staff_id = 'staff-target'"
            )
            conn.commit()
        finally:
            conn.close()

        def state():
            conn = sqlite3.connect(self.db_path)
            try:
                return (
                    conn.execute(
                        "select staff_id, employment_status, left_at, left_reason, "
                        "updated_at from staff_members order by staff_id"
                    ).fetchall(),
                    conn.execute(
                        "select id, is_active, updated_at from users order by id"
                    ).fetchall(),
                    conn.execute(
                        "select session_id, token_hash, user_id, device_name, "
                        "user_agent, ip_address, created_at, last_seen_at, "
                        "idle_expires_at, reauthenticated_at from sessions "
                        "order by session_id"
                    ).fetchall(),
                    conn.execute(
                        "select entity_id, action, old_json, new_json, operator, "
                        "created_at, actor_user_id, result, reason, risk_level "
                        "from audit_logs order by audit_id"
                    ).fetchall(),
                )
            finally:
                conn.close()

        reinstate = _endpoint(
            self.router,
            "POST",
            "/api/staff-members/{staff_id}/reinstate",
        )
        cases = (
            ("不存在", "staff-missing", {"reason": "返岗"}, 404),
            ("在职", "staff-employed", {"reason": "返岗"}, 409),
            ("归档", "staff-archived", {"reason": "返岗"}, 409),
            ("空载荷", "staff-target", None, 400),
            ("列表载荷", "staff-target", [], 400),
            ("缺原因", "staff-target", {}, 400),
            ("空原因", "staff-target", {"reason": ""}, 400),
            ("空白原因", "staff-target", {"reason": "   "}, 400),
            ("数字原因", "staff-target", {"reason": 1}, 400),
            ("列表原因", "staff-target", {"reason": []}, 400),
            ("孤立代理项", "staff-target", {"reason": "\ud800"}, 400),
            (
                "多余字段",
                "staff-target",
                {"reason": "返岗", "account_reenabled": True},
                400,
            ),
        )
        for label, staff_id, payload, status_code in cases:
            with self.subTest(case=label):
                before = state()
                with _as(_principal()), self.assertRaises(HTTPException) as caught:
                    reinstate(staff_id, payload)
                self.assertEqual(caught.exception.status_code, status_code)
                self.assertEqual(state(), before)

        before = state()
        ordinary_actor = _principal(system_admin=False, permissions=())
        with _as(ordinary_actor), self.assertRaises(HTTPException) as caught:
            reinstate("staff-target", {"reason": "返岗"})
        self.assertEqual(caught.exception.status_code, 403)
        self.assertEqual(state(), before)

    def test_reinstate_http_rejects_missing_null_list_and_string_bodies_without_writes(self):
        _add_staff(self.db_path, status="left", with_account=True)
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                "update staff_members set left_at = '2026-07-16 09:00:00', "
                "left_reason = '历史原因' where staff_id = 'staff-target'"
            )
            conn.commit()
        finally:
            conn.close()

        def state():
            conn = sqlite3.connect(self.db_path)
            try:
                return (
                    conn.execute(
                        "select employment_status, left_at, left_reason, updated_at "
                        "from staff_members where staff_id = 'staff-target'"
                    ).fetchone(),
                    conn.execute(
                        "select id, is_active, updated_at from users "
                        "where staff_id = 'staff-target' order by id"
                    ).fetchall(),
                    conn.execute(
                        "select session_id, token_hash, user_id, created_at "
                        "from sessions where user_id = 'user-staff-target' "
                        "order by session_id"
                    ).fetchall(),
                    conn.execute(
                        "select count(*) from audit_logs "
                        "where action = 'staff.employment.reinstate'"
                    ).fetchone()[0],
                )
            finally:
                conn.close()

        app = FastAPI()
        app.include_router(self.router)

        @app.middleware("http")
        async def inject_principal(request, call_next):
            token = authorization._current_access_principal.set(_principal())
            try:
                return await call_next(request)
            finally:
                authorization._current_access_principal.reset(token)

        client = TestClient(app)
        url = "/api/staff-members/staff-target/reinstate"
        cases = (
            ("缺失 body", lambda: client.post(url)),
            (
                "JSON null",
                lambda: client.post(
                    url,
                    content="null",
                    headers={"content-type": "application/json"},
                ),
            ),
            ("JSON 列表", lambda: client.post(url, json=[])),
            ("JSON 字符串", lambda: client.post(url, json="返岗")),
        )
        for label, request in cases:
            with self.subTest(case=label):
                before = state()
                response = request()
                self.assertEqual(state(), before)
                self.assertEqual(response.status_code, 400, response.text)

    def test_reinstate_audit_failure_rolls_back_all_state(self):
        _add_staff(self.db_path, with_account=True)
        access_session_service.authenticate_and_create_session(
            self.db_path,
            username="user-staff-target",
            password="pw",
            device_name="desk",
            user_agent="test",
            ip_address="127.0.0.1",
            request_id="d" * 16,
        )
        self.call(
            "DELETE",
            "/api/staff-members/{staff_id}",
            "staff-target",
            {"reason": "原离职原因"},
        )
        conn = sqlite3.connect(self.db_path)
        try:
            before_member = conn.execute(
                "select employment_status, left_at, left_reason, updated_at "
                "from staff_members where staff_id = 'staff-target'"
            ).fetchone()
            before_user = conn.execute(
                "select is_active, updated_at from users "
                "where id = 'user-staff-target'"
            ).fetchone()
            before_sessions = conn.execute(
                "select count(*) from sessions where user_id = 'user-staff-target'"
            ).fetchone()[0]
            conn.execute(
                "create trigger reject_reinstate_audit before insert on audit_logs "
                "when new.action = 'staff.employment.reinstate' "
                "begin select raise(abort, 'reject reinstate audit'); end"
            )
            conn.commit()
        finally:
            conn.close()

        with self.assertRaises(sqlite3.IntegrityError):
            self.call(
                "POST",
                "/api/staff-members/{staff_id}/reinstate",
                "staff-target",
                {"reason": "返岗"},
            )

        conn = sqlite3.connect(self.db_path)
        try:
            member = conn.execute(
                "select employment_status, left_at, left_reason, updated_at "
                "from staff_members where staff_id = 'staff-target'"
            ).fetchone()
            user = conn.execute(
                "select is_active, updated_at from users "
                "where id = 'user-staff-target'"
            ).fetchone()
            sessions = conn.execute(
                "select count(*) from sessions where user_id = 'user-staff-target'"
            ).fetchone()[0]
            audit_count = conn.execute(
                "select count(*) from audit_logs "
                "where action = 'staff.employment.reinstate'"
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(member, before_member)
        self.assertEqual(user, before_user)
        self.assertEqual(sessions, before_sessions)
        self.assertEqual(audit_count, 0)

    def test_departure_preserves_last_regular_admin_break_glass_not_counted(self):
        _add_staff(self.db_path, with_account=True)
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                "update users set is_system_admin = 0 where id = 'user-actor'"
            )
            # 离职接口在写锁内重读操作者有效权限：admin 角色不入 effective_permissions，
            # 普通操作者须经普通角色持 staff.employment 才能走到管理员下限判定。
            conn.execute(
                "update staff_role_assignments set role_key = 'director' "
                "where staff_id = 'staff-actor'"
            )
            conn.execute(
                "insert into role_permissions(role_key, perm_key) "
                "values ('director', 'staff.employment')"
            )
            conn.execute(
                "update users set is_system_admin = 1 "
                "where id = 'user-staff-target'"
            )
            conn.execute(
                "insert into users"
                "(id, username, display_name, password_hash, staff_id, is_active, "
                "is_system_admin, account_kind) "
                "values ('user-break-glass', 'break-glass', '应急管理员', ?, "
                "null, 1, 1, 'break_glass')",
                (hash_password("emergency"),),
            )
            conn.commit()
        finally:
            conn.close()
        raw_token, _context = access_session_service.authenticate_and_create_session(
            self.db_path,
            username="user-staff-target",
            password="pw",
            device_name="desk",
            user_agent="test",
            ip_address="127.0.0.1",
            request_id="b" * 16,
        )
        delete = _endpoint(
            self.router, "DELETE", "/api/staff-members/{staff_id}"
        )
        ordinary_actor = _principal(
            system_admin=False,
            permissions=("staff.employment",),
        )
        with _as(ordinary_actor), self.assertRaises(HTTPException) as caught:
            delete("staff-target", {"reason": "主动离职"})
        self.assertEqual(caught.exception.status_code, 409)

        conn = sqlite3.connect(self.db_path)
        try:
            status = conn.execute(
                "select employment_status from staff_members "
                "where staff_id = 'staff-target'"
            ).fetchone()[0]
            active = conn.execute(
                "select is_active from users where id = 'user-staff-target'"
            ).fetchone()[0]
            session_count = conn.execute(
                "select count(*) from sessions "
                "where user_id = 'user-staff-target'"
            ).fetchone()[0]
            audit_count = conn.execute(
                "select count(*) from audit_logs "
                "where action = 'staff.employment.left'"
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(status, "employed")
        self.assertEqual(active, 1)
        self.assertEqual(session_count, 1)
        self.assertEqual(audit_count, 0)
        self.assertIsNotNone(
            access_session_service.resolve_session(self.db_path, raw_token)
        )

    def test_departure_cannot_target_self_and_audit_failure_rolls_back(self):
        _add_staff(self.db_path, with_account=True)
        delete = _endpoint(
            self.router, "DELETE", "/api/staff-members/{staff_id}"
        )
        self_principal = _principal(
            user_id="user-staff-target",
            username="user-staff-target",
            staff_id="staff-target",
            system_admin=False,
            permissions=("staff.employment",),
        )
        with _as(self_principal), self.assertRaises(HTTPException) as caught:
            delete("staff-target", {"reason": "错误操作"})
        self.assertEqual(caught.exception.status_code, 409)

        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                "create trigger reject_departure_audit before insert on audit_logs "
                "when new.action = 'staff.employment.left' "
                "begin select raise(abort, 'reject audit'); end"
            )
            conn.commit()
        finally:
            conn.close()
        with self.assertRaises(sqlite3.IntegrityError):
            self.call(
                "DELETE",
                "/api/staff-members/{staff_id}",
                "staff-target",
                {"reason": "主动离职"},
            )

        conn = sqlite3.connect(self.db_path)
        try:
            status = conn.execute(
                "select employment_status from staff_members "
                "where staff_id = 'staff-target'"
            ).fetchone()[0]
            active = conn.execute(
                "select is_active from users where staff_id = 'staff-target'"
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(status, "employed")
        self.assertEqual(active, 1)

    def test_open_account_is_regular_and_bad_passwords_have_no_side_effect(self):
        _add_staff(self.db_path, staff_id="staff-open", name="待开户")
        for password in ("", "   ", 1, ["pw"], "\ud800"):
            with self.subTest(password=repr(password)):
                with self.assertRaises(HTTPException) as caught:
                    self.call(
                        "POST",
                        "/api/staff-members/{staff_id}/account",
                        "staff-open",
                        {
                            "username": "new-user",
                            "password": password,
                        },
                    )
                self.assertEqual(caught.exception.status_code, 400)
        for forbidden in (
            {"role": "admin"},
            {"roles": ["admin"]},
            {"primary_role": "admin"},
            {"role_keys": ["admin"]},
            {"is_system_admin": True},
            {"account_kind": "break_glass"},
        ):
            with self.subTest(forbidden=forbidden):
                with self.assertRaises(HTTPException) as caught:
                    self.call(
                        "POST",
                        "/api/staff-members/{staff_id}/account",
                        "staff-open",
                        {"username": "new-user", "password": "pw", **forbidden},
                    )
                self.assertEqual(caught.exception.status_code, 400)
        conn = sqlite3.connect(self.db_path)
        try:
            before = conn.execute(
                "select count(*) from users where staff_id = 'staff-open'"
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(before, 0)

        result = self.call(
            "POST",
            "/api/staff-members/{staff_id}/account",
            "staff-open",
            {
                "username": "new-user",
                "password": " x ",
            },
        )
        self.assertEqual(result["username"], "new-user")
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            user = conn.execute(
                "select * from users where username = 'new-user'"
            ).fetchone()
        finally:
            conn.close()
        self.assertEqual(user["is_system_admin"], 0)
        self.assertEqual(user["account_kind"], "staff")
        self.assertTrue(verify_password(" x ", user["password_hash"]))
        self.assertFalse(verify_password("x", user["password_hash"]))


if __name__ == "__main__":
    unittest.main()
