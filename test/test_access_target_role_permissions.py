"""Access V3 角色权限路由：仅合成目标库，不接触真实数据。"""

import sqlite3
import unittest
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from fastapi import HTTPException

from local_app import access_auth, access_authorization, access_policy
from local_app import personnel_access_migration as pam
from local_app.routes.role_permissions import create_role_permissions_router


NOW = datetime(2026, 7, 16, 12, 0, 0, tzinfo=timezone.utc)


def _build_database(root):
    db_path = Path(root) / "target-role-permissions.sqlite3"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        create table roles (
            role_key text primary key,
            name text not null,
            is_system integer not null default 0,
            sort integer not null default 0,
            created_at text,
            updated_at text
        );
        create table role_permissions (
            role_key text not null,
            perm_key text not null,
            primary key (role_key, perm_key)
        );
        """
    )
    conn.executescript(pam.load_personnel_access_schema_sql())
    conn.executemany(
        "insert into roles(role_key, name, sort) values (?, ?, ?)",
        [
            ("admin", "旧管理员", 0),
            ("doctor", "医生", 10),
            ("nurse", "护士", 20),
        ],
    )
    conn.execute(
        "insert into staff_members(staff_id, name, employment_status) "
        "values ('staff-actor', '系统管理员', 'employed')"
    )
    conn.execute(
        "insert into users(id, username, password_hash, staff_id, is_active, "
        "is_system_admin, account_kind) "
        "values ('user-actor', 'root', 'synthetic-hash', 'staff-actor', 1, 1, 'staff')"
    )
    conn.executemany(
        "insert into staff_members(staff_id, name, employment_status) "
        "values (?, ?, ?)",
        [
            ("staff-doctor-nurse", "合成兼任医护", "employed"),
            ("staff-doctor-no-account", "合成无账号医生", "employed"),
            ("staff-doctor-left", "合成离职医生", "left"),
        ],
    )
    conn.execute(
        "insert into users(id, username, password_hash, staff_id, is_active, "
        "is_system_admin, account_kind) "
        "values ('user-doctor-nurse', 'doctor-nurse', 'synthetic-hash', "
        "'staff-doctor-nurse', 0, 0, 'staff')"
    )
    conn.executemany(
        "insert into staff_role_assignments(staff_id, role_key, is_primary) "
        "values (?, ?, ?)",
        [
            ("staff-doctor-nurse", "doctor", 1),
            ("staff-doctor-nurse", "nurse", 0),
            ("staff-doctor-no-account", "doctor", 1),
            ("staff-doctor-left", "doctor", 1),
        ],
    )
    conn.execute(
        "insert into role_permissions(role_key, perm_key) "
        "values ('doctor', 'appointment.view')"
    )
    conn.commit()
    conn.close()
    return db_path


def _endpoint(router, method):
    return next(
        route.endpoint
        for route in router.routes
        if route.path == "/api/role-permissions" and method in route.methods
    )


def _principal(*, system_admin=True, role_keys=("doctor",), user_id="user-actor"):
    return access_authorization.AccessPrincipal(
        user_id=user_id,
        username=user_id,
        display_name=user_id,
        staff_id="staff-actor",
        is_system_admin=system_admin,
        primary_role=role_keys[0] if role_keys else None,
        role_keys=role_keys,
        permissions=frozenset(access_policy.ALL_PERMISSIONS if system_admin else ()),
        role_permissions=frozenset(),
    )


def _session(*, reauthenticated_at="2026-07-16T11:59:00Z"):
    return {
        "session_id": "s1",
        "user_id": "user-actor",
        "created_at": "2026-07-16T11:00:00Z",
        "reauthenticated_at": reauthenticated_at,
    }


@contextmanager
def _request_context(principal, *, session=None, now=NOW):
    principal_token = access_authorization._current_access_principal.set(principal)
    session_token = access_auth._current_access_session.set(
        _session() if session is None else session
    )
    time_token = access_auth._current_access_request_time.set(now)
    try:
        yield
    finally:
        access_auth._current_access_request_time.reset(time_token)
        access_auth._current_access_session.reset(session_token)
        access_authorization._current_access_principal.reset(principal_token)


def _permissions(db_path, role_key):
    conn = sqlite3.connect(db_path)
    try:
        return [
            row[0]
            for row in conn.execute(
                "select perm_key from role_permissions where role_key = ? order by perm_key",
                (role_key,),
            )
        ]
    finally:
        conn.close()


def _role_audits(db_path):
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute(
            "select action, entity_id, reason from audit_logs "
            "where entity_type = 'role' order by audit_id"
        ).fetchall()
    finally:
        conn.close()


class AccessTargetRolePermissionsTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.db_path = _build_database(self.temp_dir.name)
        self.router = create_role_permissions_router(self.db_path, access_v3=True)
        self.get_matrix = _endpoint(self.router, "GET")
        self.save_role = _endpoint(self.router, "PUT")

    def _call_get(self, principal=None):
        with _request_context(principal or _principal()):
            return self.get_matrix()

    def _call_put(self, payload, *, principal=None, session=None, now=NOW):
        with _request_context(
            principal or _principal(), session=session, now=now
        ):
            return self.save_role(payload)

    def _assert_http(self, status, detail, callback):
        with self.assertRaises(HTTPException) as caught:
            callback()
        self.assertEqual(caught.exception.status_code, status)
        self.assertEqual(caught.exception.detail, detail)

    def test_get_requires_true_system_admin_and_returns_target_catalog(self):
        self._assert_http(
            403,
            "access_denied",
            lambda: self._call_get(
                _principal(system_admin=False, role_keys=("admin",))
            ),
        )

        data = self._call_get()
        self.assertEqual(
            [role["role_key"] for role in data["roles"]],
            ["doctor", "nurse"],
        )
        expected = [
            (module, key)
            for module, keys in access_policy.PERMISSION_CATALOG.items()
            for key in keys
        ]
        self.assertEqual(
            [(item["module"], item["key"]) for item in data["perms"]],
            expected,
        )
        self.assertEqual(len(data["perms"]), 99)
        self.assertEqual(set(data["matrix"]), {"doctor", "nurse"})
        self.assertTrue(data["matrix"]["doctor"]["appointment.view"])
        self.assertFalse(data["matrix"]["doctor"]["patient.profile.view"])

    def test_get_returns_ui_metadata_dependencies_and_active_staff_counts(self):
        data = self._call_get()

        roles = {role["role_key"]: role for role in data["roles"]}
        self.assertEqual(roles["doctor"]["assigned_staff_count"], 2)
        self.assertEqual(roles["nurse"]["assigned_staff_count"], 1)
        for role in data["roles"]:
            self.assertIsInstance(role["assigned_staff_count"], int)
            self.assertGreaterEqual(role["assigned_staff_count"], 0)

        expected_perms = access_policy.PERMISSION_UI_DEFINITIONS
        self.assertEqual(
            [item["key"] for item in data["perms"]],
            [item["key"] for item in expected_perms],
        )
        self.assertEqual(len(data["perms"]), 99)
        for item, expected in zip(data["perms"], expected_perms):
            self.assertEqual(
                set(item),
                {
                    "key",
                    "module",
                    "object",
                    "label",
                    "description",
                    "risk",
                    "dependencies",
                },
            )
            self.assertEqual(
                item["dependencies"],
                list(access_policy.PERMISSION_DEPENDENCIES.get(item["key"], ())),
            )
            self.assertEqual(
                {key: item[key] for key in expected},
                dict(expected),
            )

        patient_profile_edit = next(
            item for item in data["perms"] if item["key"] == "patient.profile.edit"
        )
        self.assertEqual(patient_profile_edit["module"], "患者档案与回收")
        self.assertEqual(patient_profile_edit["object"], "患者资料")
        self.assertEqual(patient_profile_edit["label"], "编辑")
        self.assertNotEqual(patient_profile_edit["label"], patient_profile_edit["key"])
        self.assertTrue(patient_profile_edit["description"])
        self.assertTrue(any("\u4e00" <= char <= "\u9fff" for char in patient_profile_edit["description"]))
        self.assertIn(patient_profile_edit["risk"], {"normal", "sensitive", "high"})
        self.assertEqual(patient_profile_edit["dependencies"], ["patient.profile.view"])

        self.assertEqual(set(data), {"roles", "perms", "matrix", "system_admin_capabilities"})
        self.assertEqual(
            data["system_admin_capabilities"],
            [
                {"key": key, "label": label, "assignable": False}
                for key, label in access_policy.SYSTEM_ADMIN_CAPABILITIES.items()
            ],
        )
        system_admin_keys = set(access_policy.SYSTEM_ADMIN_CAPABILITIES)
        self.assertTrue(system_admin_keys.isdisjoint({item["key"] for item in data["perms"]}))
        self.assertTrue(
            all(system_admin_keys.isdisjoint(row) for row in data["matrix"].values())
        )

    def test_put_rejects_legacy_matrix_and_exactly_replaces_one_role_with_audit(self):
        self._assert_http(
            400,
            "invalid_request",
            lambda: self._call_put(
                {"matrix": {"doctor": {"patient.profile.view": True}}}
            ),
        )
        self.assertEqual(_permissions(self.db_path, "doctor"), ["appointment.view"])
        self.assertEqual(_role_audits(self.db_path), [])

        result = self._call_put(
            {
                "role_key": "doctor",
                "permissions": ["patient.profile.view", "patient.profile.edit"],
                "expected_permissions": ["appointment.view"],
                "reason": "调整医生权限",
            }
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["role_key"], "doctor")
        self.assertEqual(
            _permissions(self.db_path, "doctor"),
            ["patient.profile.edit", "patient.profile.view"],
        )
        self.assertEqual(
            _role_audits(self.db_path),
            [("role_permissions.replace", "doctor", "调整医生权限")],
        )

    def test_put_requires_exact_expected_permissions_request_contract(self):
        valid = {
            "role_key": "doctor",
            "permissions": ["patient.profile.view"],
            "expected_permissions": ["appointment.view"],
            "reason": "严格字段合同",
        }
        invalid_payloads = (
            {key: value for key, value in valid.items()
             if key != "expected_permissions"},
            {**valid, "unexpected": True},
        )

        for payload in invalid_payloads:
            with self.subTest(fields=sorted(payload)):
                self._assert_http(
                    400,
                    "invalid_request",
                    lambda payload=payload: self._call_put(payload),
                )
                self.assertEqual(
                    _permissions(self.db_path, "doctor"), ["appointment.view"]
                )
                self.assertEqual(_role_audits(self.db_path), [])

    def test_expired_reauth_is_rejected_without_changes(self):
        stale = (NOW - timedelta(seconds=301)).strftime("%Y-%m-%dT%H:%M:%SZ")
        self._assert_http(
            403,
            "recent_reauthentication_required",
            lambda: self._call_put(
                {
                    "role_key": "doctor",
                    "permissions": ["patient.profile.view"],
                    "expected_permissions": ["appointment.view"],
                    "reason": "过期确认",
                },
                session=_session(reauthenticated_at=stale),
            ),
        )
        self.assertEqual(_permissions(self.db_path, "doctor"), ["appointment.view"])
        self.assertEqual(_role_audits(self.db_path), [])

    def test_put_rejects_stale_expected_permissions_with_409_and_no_side_effects(self):
        first = self._call_put(
            {
                "role_key": "doctor",
                "permissions": ["patient.profile.view"],
                "expected_permissions": ["appointment.view"],
                "reason": "第一位管理员保存",
            }
        )
        self.assertTrue(first["ok"])

        self._assert_http(
            409,
            "role_permissions_stale",
            lambda: self._call_put(
                {
                    "role_key": "doctor",
                    "permissions": ["recycle.view"],
                    "expected_permissions": ["appointment.view"],
                    "reason": "第二位管理员保存旧基线",
                }
            ),
        )
        self.assertEqual(_permissions(self.db_path, "doctor"), ["patient.profile.view"])
        self.assertEqual(
            _role_audits(self.db_path),
            [("role_permissions.replace", "doctor", "第一位管理员保存")],
        )

    def test_invalid_permission_sets_and_legacy_admin_are_whole_request_rejections(self):
        cases = [
            (
                ["patient.profile.edit"],
                "doctor",
                "permission_dependency_missing",
            ),
            (["unknown.permission"], "doctor", "permission_unknown"),
            (
                ["system_admin.role_permissions.manage"],
                "doctor",
                "permission_not_assignable",
            ),
            (["patient.profile.view"], "admin", "role_not_configurable"),
        ]
        for permissions, role_key, detail in cases:
            with self.subTest(detail=detail):
                self._assert_http(
                    400,
                    detail,
                    lambda permissions=permissions, role_key=role_key: self._call_put(
                        {
                            "role_key": role_key,
                            "permissions": permissions,
                            "expected_permissions": ["appointment.view"],
                            "reason": "应整单拒绝",
                        }
                    ),
                )
                self.assertEqual(
                    _permissions(self.db_path, "doctor"), ["appointment.view"]
                )
                self.assertEqual(_role_audits(self.db_path), [])

    def test_audit_failure_rolls_back_role_change_and_hides_sql_error(self):
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "create trigger fail_role_audit before insert on audit_logs "
            "when new.entity_type = 'role' "
            "begin select raise(abort, 'SECRET SQL audit failure'); end"
        )
        conn.commit()
        conn.close()

        self._assert_http(
            500,
            "role_permissions_unavailable",
            lambda: self._call_put(
                {
                    "role_key": "doctor",
                    "permissions": ["patient.profile.view"],
                    "expected_permissions": ["appointment.view"],
                    "reason": "触发回滚",
                }
            ),
        )
        self.assertEqual(_permissions(self.db_path, "doctor"), ["appointment.view"])
        self.assertEqual(_role_audits(self.db_path), [])


if __name__ == "__main__":
    unittest.main()
