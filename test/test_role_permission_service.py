"""角色权限原子替换 access_service.replace_role_permissions() 的正反例（Task 2C.1）。

只用 TemporaryDirectory 合成 SQLite 文件库，不接触真实库/患者/员工数据。
"""
import contextlib
import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone, tzinfo
from pathlib import Path
from unittest import mock

from local_app import access_policy, access_service
from local_app import personnel_access_migration as pam
from local_app.timeutil import BJ

NOW = datetime(2026, 7, 15, 10, 0, 0, tzinfo=BJ)
VALID_REAUTH = NOW - timedelta(seconds=10)

_RBAC_DDL = """
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


def _build_db(db_path):
    conn = sqlite3.connect(str(db_path))
    conn.executescript(_RBAC_DDL)
    conn.executescript(pam.load_personnel_access_schema_sql())
    for role_key, name in (("admin", "管理员"), ("doctor", "医生"), ("nurse", "护士")):
        conn.execute("insert into roles(role_key, name) values (?, ?)", (role_key, name))
    conn.commit()
    conn.close()


def _insert_staff(conn, staff_id, employment_status="employed"):
    conn.execute(
        "insert into staff_members(staff_id, name, employment_status) values (?, ?, ?)",
        (staff_id, staff_id, employment_status),
    )


def _insert_user(conn, user_id, *, staff_id=None, is_active=1, is_system_admin=0,
                 account_kind="staff"):
    conn.execute(
        "insert into users(id, username, password_hash, staff_id, is_active, "
        "is_system_admin, account_kind) values (?, ?, 'x$y', ?, ?, ?, ?)",
        (user_id, user_id, staff_id, is_active, is_system_admin, account_kind),
    )


def _assign_role(conn, staff_id, role_key):
    conn.execute(
        "insert into staff_role_assignments(staff_id, role_key) values (?, ?)",
        (staff_id, role_key),
    )


def _set_perms(conn, role_key, perms):
    conn.executemany(
        "insert into role_permissions(role_key, perm_key) values (?, ?)",
        [(role_key, p) for p in perms],
    )


def _perms(conn, role_key):
    return sorted(
        row[0] for row in conn.execute(
            "select perm_key from role_permissions where role_key = ?", (role_key,)
        )
    )


def _audit_rows(conn):
    conn.row_factory = sqlite3.Row
    return conn.execute(
        "select * from audit_logs where entity_type = 'role' order by audit_id"
    ).fetchall()


class _HostileRoleKey(str):
    """内容是合法角色键、但 __eq__ 恒假的 str 子类：`role_key == 'admin'` 被绕过，
    SQLite 仍按底层文本 'admin' 绑定。"""

    def __eq__(self, other):
        return False

    def __ne__(self, other):
        return True

    def __hash__(self):
        return hash(str(self))

    def __repr__(self):
        return "<HOSTILE-ROLE-KEY-SECRET>"


class _HostileReason(str):
    """strip() 抛任意异常的 str 子类。"""

    def strip(self, *args, **kwargs):
        raise RuntimeError("SECRET_REASON_LEAK")

    def __repr__(self):
        return "<HOSTILE-REASON-SECRET>"


class _HostileDatetime(datetime):
    """astimezone() 返回自身、后续相减/格式化抛任意异常的 datetime 子类。"""

    def astimezone(self, tz=None):
        return self

    def __sub__(self, other):
        raise RuntimeError("SECRET_TZ_LEAK")

    def __rsub__(self, other):
        raise RuntimeError("SECRET_TZ_LEAK")

    def strftime(self, fmt):
        raise RuntimeError("SECRET_TZ_LEAK")

    def __repr__(self):
        return "<HOSTILE-DATETIME-SECRET>"


class _FlippingTZ(tzinfo):
    """每次被问 utcoffset 都换一个答案的敌意 tzinfo：第一次报 UTC+0（让 stale 的
    墙钟看着像 stale），第二次报 UTC-1（把同一墙钟往后挪一小时冒充 now）。
    只要归一化读两次 offset，就能把过期 reauth 洗成新鲜的。"""

    def __init__(self, offsets):
        self.offsets = list(offsets)
        self.calls = 0

    def utcoffset(self, dt):
        self.calls += 1
        return self.offsets[min(self.calls - 1, len(self.offsets) - 1)]

    def dst(self, dt):
        return timedelta(0)

    def tzname(self, dt):
        return "FLIP"


@contextlib.contextmanager
def _connect_factory(connection_cls):
    """把 access_service 的 sqlite3.connect 换成注入 factory 的版本，退出即恢复。"""
    real_connect = sqlite3.connect

    def fake_connect(*args, **kwargs):
        kwargs["factory"] = connection_cls
        return real_connect(*args, **kwargs)

    with mock.patch("local_app.access_service.sqlite3.connect", fake_connect):
        yield


@contextlib.contextmanager
def _refuse_connect():
    """任何数据库打开都算失败，用来证明校验期不碰库。"""
    def boom(*args, **kwargs):
        raise AssertionError("校验期不应打开数据库")

    with mock.patch("local_app.access_service.sqlite3.connect", boom):
        yield


class PermissionDependencyCatalogTests(unittest.TestCase):
    def test_dependencies_only_reference_catalog_keys(self):
        for key, deps in access_policy.PERMISSION_DEPENDENCIES.items():
            self.assertIn(key, access_policy.ALL_PERMISSIONS)
            for dep in deps:
                self.assertIn(dep, access_policy.ALL_PERMISSIONS)

    def test_explicit_relations_present(self):
        deps = access_policy.PERMISSION_DEPENDENCIES
        self.assertIn("patient.profile.view", deps["patient.profile.edit"])
        self.assertIn("patient.image.view", deps["patient.image.delete"])
        self.assertIn("recycle.view", deps["recycle.restore"])
        self.assertIn("billing.view", deps["payment.refund"])
        self.assertIn("consent.manage", deps["consent.void"])
        self.assertIn("account.view", deps["account.security"])

    def test_system_admin_capabilities_are_exactly_the_five_reserved_keys(self):
        self.assertEqual(
            dict(access_policy.SYSTEM_ADMIN_CAPABILITIES),
            {
                "system_admin.role_permissions.manage": "修改角色权限",
                "system_admin.grant": "授予/撤销系统管理员",
                "system_admin.backup.download": "下载数据库备份",
                "system_admin.sync.run": "执行全量同步",
                "system_admin.migration.run": "执行数据迁移",
            },
        )
        for key in (
            "system_admin.role_permissions.manage", "system_admin.grant",
            "system_admin.backup.download", "system_admin.sync.run",
            "system_admin.migration.run",
        ):
            with self.subTest(key=key):
                self.assertNotIn(key, access_policy.ALL_PERMISSIONS)
                self.assertIn(key, access_policy.NON_ASSIGNABLE_PERMISSIONS)

    def test_system_admin_capabilities_are_not_ordinary_permissions(self):
        self.assertEqual(len(access_policy.SYSTEM_ADMIN_CAPABILITIES), 5)
        for key in access_policy.SYSTEM_ADMIN_CAPABILITIES:
            self.assertNotIn(key, access_policy.ALL_PERMISSIONS)
        self.assertNotIn("role.manage", access_policy.ALL_PERMISSIONS)
        self.assertIn("role.manage", access_policy.NON_ASSIGNABLE_PERMISSIONS)
        self.assertTrue(
            set(access_policy.SYSTEM_ADMIN_CAPABILITIES)
            <= set(access_policy.NON_ASSIGNABLE_PERMISSIONS)
        )


class ReplaceRolePermissionsTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db_path = Path(self._tmp.name) / "synthetic.db"
        _build_db(self.db_path)
        conn = self._conn()
        _insert_staff(conn, "sa")
        _insert_user(conn, "actor", staff_id="sa", is_system_admin=1)
        conn.commit()
        conn.close()

    def _conn(self, db_path=None):
        conn = sqlite3.connect(str(db_path or self.db_path))
        conn.row_factory = sqlite3.Row
        return conn

    def _replace(self, permissions, *, role_key="doctor", actor_user_id="actor",
                 expected_permissions=(), reason="test",
                 reauthenticated_at=VALID_REAUTH, now=NOW, db_path=None):
        return access_service.replace_role_permissions(
            db_path or self.db_path, actor_user_id=actor_user_id, role_key=role_key,
            permissions=permissions, expected_permissions=expected_permissions,
            reason=reason,
            reauthenticated_at=reauthenticated_at, now=now,
        )

    def _expect(self, code, permissions, **kwargs):
        with self.assertRaises(access_service.AccessServiceError) as ctx:
            self._replace(permissions, **kwargs)
        self.assertEqual(ctx.exception.code, code)

    def _assert_untouched(self, role_key="doctor", expected=()):
        conn = self._conn()
        self.assertEqual(_perms(conn, role_key), sorted(expected))
        self.assertEqual(len(_audit_rows(conn)), 0)
        conn.close()

    # --- 成功路径 ---

    def test_success_exact_replace_and_audit(self):
        conn = self._conn()
        _set_perms(conn, "doctor", ["patient.profile.view", "recycle.view", "recycle.restore"])
        conn.commit()
        conn.close()

        result = self._replace([
            "patient.profile.view", "patient.profile.edit", "appointment.view",
        ], expected_permissions=[
            "patient.profile.view", "recycle.view", "recycle.restore",
        ])

        self.assertEqual(result["role_key"], "doctor")
        self.assertTrue(result["changed"])
        self.assertEqual(
            sorted(result["new"]),
            ["appointment.view", "patient.profile.edit", "patient.profile.view"],
        )
        self.assertEqual(
            sorted(result["old"]),
            ["patient.profile.view", "recycle.restore", "recycle.view"],
        )
        self.assertEqual(sorted(result["added"]), ["appointment.view", "patient.profile.edit"])
        self.assertEqual(sorted(result["removed"]), ["recycle.restore", "recycle.view"])

        conn = self._conn()
        self.assertEqual(
            _perms(conn, "doctor"),
            ["appointment.view", "patient.profile.edit", "patient.profile.view"],
        )
        rows = _audit_rows(conn)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["entity_id"], "doctor")
        self.assertEqual(row["action"], "role_permissions.replace")
        self.assertEqual(row["actor_user_id"], "actor")
        self.assertEqual(row["operator"], "actor")
        self.assertEqual(row["result"], "success")
        self.assertEqual(row["reason"], "test")
        self.assertEqual(row["risk_level"], "high")
        self.assertEqual(row["created_at"], "2026-07-15 10:00:00")
        old_json = json.loads(row["old_json"])
        new_json = json.loads(row["new_json"])
        self.assertEqual(
            old_json["permissions"],
            ["patient.profile.view", "recycle.restore", "recycle.view"],
        )
        self.assertEqual(
            new_json["permissions"],
            ["appointment.view", "patient.profile.edit", "patient.profile.view"],
        )
        self.assertEqual(new_json["added"], ["appointment.view", "patient.profile.edit"])
        self.assertEqual(new_json["removed"], ["recycle.restore", "recycle.view"])
        self.assertEqual(new_json["affected_staff_count"], result["affected_staff_count"])
        conn.close()

    def test_result_is_json_serializable(self):
        result = self._replace(["patient.profile.view"])
        json.dumps(result)

    def test_affected_staff_count_counts_only_employed_distinct(self):
        conn = self._conn()
        for staff_id, status in (
            ("s1", "employed"), ("s2", "employed"), ("s3", "left"), ("s4", "archived"),
        ):
            _insert_staff(conn, staff_id, employment_status=status)
            _assign_role(conn, staff_id, "doctor")
        # 同一个在职人员多岗位，不得重复计数
        _assign_role(conn, "s1", "nurse")
        # 别的角色的人不算
        _insert_staff(conn, "s5")
        _assign_role(conn, "s5", "nurse")
        conn.commit()
        conn.close()

        result = self._replace(["patient.profile.view"])
        self.assertEqual(result["affected_staff_count"], 2)

        conn = self._conn()
        self.assertEqual(json.loads(_audit_rows(conn)[0]["new_json"])["affected_staff_count"], 2)
        conn.close()

    def test_affected_staff_count_dedupes_duplicate_assignment_rows(self):
        # 合成坏结构（仅测试用）：去掉 (staff_id, role_key) 主键，让同一人同一角色能
        # 出现重复行。count(*) 会数成 2，只有 count(distinct) 才是 1。
        conn = self._conn()
        conn.executescript(
            "drop table staff_role_assignments;"
            "create table staff_role_assignments ("
            "staff_id text not null, role_key text not null);"
        )
        _insert_staff(conn, "s1")
        _assign_role(conn, "s1", "doctor")
        _assign_role(conn, "s1", "doctor")
        conn.commit()
        self.assertEqual(
            conn.execute(
                "select count(*) from staff_role_assignments where role_key = 'doctor'"
            ).fetchone()[0],
            2,
        )
        conn.close()

        result = self._replace(["patient.profile.view"])
        self.assertEqual(result["affected_staff_count"], 1)

    def test_empty_list_clears_all_permissions(self):
        conn = self._conn()
        _set_perms(conn, "doctor", ["patient.profile.view"])
        conn.commit()
        conn.close()

        result = self._replace([], expected_permissions=["patient.profile.view"])
        self.assertTrue(result["changed"])
        self.assertEqual(list(result["new"]), [])
        self.assertEqual(sorted(result["removed"]), ["patient.profile.view"])
        conn = self._conn()
        self.assertEqual(_perms(conn, "doctor"), [])
        conn.close()

    def test_other_roles_untouched(self):
        conn = self._conn()
        _set_perms(conn, "nurse", ["appointment.view"])
        conn.commit()
        conn.close()

        self._replace(["patient.profile.view"])
        conn = self._conn()
        self.assertEqual(_perms(conn, "nurse"), ["appointment.view"])
        conn.close()

    def test_no_change_skips_write_and_audit(self):
        conn = self._conn()
        _set_perms(conn, "doctor", ["patient.profile.view", "appointment.view"])
        conn.commit()
        conn.close()

        result = self._replace(
            ["appointment.view", "patient.profile.view"],
            expected_permissions=["patient.profile.view", "appointment.view"],
        )
        self.assertFalse(result["changed"])
        self.assertEqual(list(result["added"]), [])
        self.assertEqual(list(result["removed"]), [])
        conn = self._conn()
        self.assertEqual(_perms(conn, "doctor"), ["appointment.view", "patient.profile.view"])
        self.assertEqual(len(_audit_rows(conn)), 0)
        conn.close()

    def test_no_change_still_validates(self):
        # 依赖闭包完整的真实 no-op：doctor 已持有的权限集合与请求集合完全相同
        # （对照 test_complete_transitive_closure_accepted 的同一闭包），
        # 用来证明校验不会因为"最终集合不变"而被短路跳过。
        same_permissions = [
            "patient.image.delete", "patient.image.view", "patient.profile.view",
        ]
        conn = self._conn()
        _set_perms(conn, "doctor", same_permissions)
        conn.commit()
        conn.close()

        self._expect("reason_required", same_permissions, reason="  ")
        self._expect("reauth_expired", same_permissions,
                     reauthenticated_at=NOW - timedelta(seconds=301))
        self._expect("actor_not_authorized", same_permissions, actor_user_id="nobody")
        self._expect("role_not_found", same_permissions, role_key="ghost")

        conn = self._conn()
        self.assertEqual(_perms(conn, "doctor"), sorted(same_permissions))
        self.assertEqual(len(_audit_rows(conn)), 0)
        conn.close()

    def test_stale_expected_permissions_rejects_second_admin_without_side_effects(self):
        conn = self._conn()
        _insert_staff(conn, "sb")
        _insert_user(conn, "actor-b", staff_id="sb", is_system_admin=1)
        _set_perms(conn, "doctor", ["appointment.view"])
        conn.commit()
        conn.close()

        first = access_service.replace_role_permissions(
            self.db_path,
            actor_user_id="actor",
            role_key="doctor",
            permissions=["patient.profile.view"],
            expected_permissions=["appointment.view"],
            reason="第一位管理员保存",
            reauthenticated_at=VALID_REAUTH,
            now=NOW,
        )
        self.assertTrue(first["changed"])

        with self.assertRaises(access_service.AccessServiceError) as caught:
            access_service.replace_role_permissions(
                self.db_path,
                actor_user_id="actor-b",
                role_key="doctor",
                permissions=["recycle.view"],
                expected_permissions=["appointment.view"],
                reason="第二位管理员基于旧基线保存",
                reauthenticated_at=VALID_REAUTH,
                now=NOW,
            )
        self.assertEqual(caught.exception.code, "role_permissions_stale")

        conn = self._conn()
        self.assertEqual(_perms(conn, "doctor"), ["patient.profile.view"])
        rows = _audit_rows(conn)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["actor_user_id"], "actor")
        conn.close()

    def test_empty_expected_permissions_detects_first_admin_addition(self):
        conn = self._conn()
        _insert_staff(conn, "sb")
        _insert_user(conn, "actor-b", staff_id="sb", is_system_admin=1)
        conn.commit()
        conn.close()

        first = access_service.replace_role_permissions(
            self.db_path,
            actor_user_id="actor",
            role_key="doctor",
            permissions=["appointment.view"],
            expected_permissions=[],
            reason="第一位管理员从空基线新增",
            reauthenticated_at=VALID_REAUTH,
            now=NOW,
        )
        self.assertTrue(first["changed"])

        with self.assertRaises(access_service.AccessServiceError) as caught:
            access_service.replace_role_permissions(
                self.db_path,
                actor_user_id="actor-b",
                role_key="doctor",
                permissions=["patient.profile.view"],
                expected_permissions=[],
                reason="第二位管理员仍按空基线保存",
                reauthenticated_at=VALID_REAUTH,
                now=NOW,
            )
        self.assertEqual(caught.exception.code, "role_permissions_stale")

        conn = self._conn()
        self.assertEqual(_perms(conn, "doctor"), ["appointment.view"])
        rows = _audit_rows(conn)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["actor_user_id"], "actor")
        conn.close()

    def test_expected_permissions_validation_is_strict_but_needs_no_dependency_closure(self):
        conn = self._conn()
        _set_perms(conn, "doctor", ["patient.profile.edit"])
        conn.commit()
        conn.close()

        result = access_service.replace_role_permissions(
            self.db_path,
            actor_user_id="actor",
            role_key="doctor",
            permissions=[],
            expected_permissions=["patient.profile.edit"],
            reason="旧基线不要求依赖闭包",
            reauthenticated_at=VALID_REAUTH,
            now=NOW,
        )
        self.assertTrue(result["changed"])

        class _StrLike(str):
            pass

        invalid_cases = (
            (None, "permissions_invalid"),
            ("patient.profile.view", "permissions_invalid"),
            ({"patient.profile.view"}, "permissions_invalid"),
            ((None,), "permission_invalid"),
            ((_StrLike("patient.profile.view"),), "permission_invalid"),
            (("patient.profile.view", "patient.profile.view"), "permission_duplicate"),
            (("unknown.permission",), "permission_unknown"),
        )
        for expected_permissions, code in invalid_cases:
            with self.subTest(expected_permissions=expected_permissions):
                with self.assertRaises(access_service.AccessServiceError) as caught:
                    access_service.replace_role_permissions(
                        self.db_path,
                        actor_user_id="actor",
                        role_key="nurse",
                        permissions=[],
                        expected_permissions=expected_permissions,
                        reason="校验旧基线",
                        reauthenticated_at=VALID_REAUTH,
                        now=NOW,
                    )
                self.assertEqual(caught.exception.code, code)

        conn = self._conn()
        self.assertEqual(_perms(conn, "nurse"), [])
        self.assertEqual(len(_audit_rows(conn)), 1)
        conn.close()

    # --- 权限目录校验 ---

    def test_unknown_permission_rejected_whole_request(self):
        self._expect("permission_unknown", ["patient.profile.view", "bogus.key"])
        self._expect("permission_unknown", ["*"])
        self._expect("permission_unknown", ["patient.profile.view", ""])
        self._assert_untouched()

    def test_system_admin_capabilities_rejected(self):
        for cap in access_policy.SYSTEM_ADMIN_CAPABILITIES:
            with self.subTest(cap=cap):
                self._expect("permission_not_assignable", ["patient.profile.view", cap])
        self._assert_untouched()

    def test_legacy_role_manage_rejected(self):
        self._expect("permission_not_assignable", ["role.manage"])
        self._assert_untouched()

    def test_invalid_container_rejected(self):
        for bad in (None, "patient.profile.view", {"patient.profile.view"},
                    {"patient.profile.view": True}, iter(["patient.profile.view"]), 5):
            with self.subTest(bad=type(bad).__name__):
                self._expect("permissions_invalid", bad)
        self._assert_untouched()

    def test_invalid_element_rejected(self):
        class _StrLike(str):
            pass

        for bad in ([None], [1], [["patient.profile.view"]], [_StrLike("patient.profile.view")]):
            with self.subTest(bad=bad):
                self._expect("permission_invalid", bad)
        self._assert_untouched()

    def test_duplicate_permission_rejected(self):
        self._expect("permission_duplicate", ["patient.profile.view", "patient.profile.view"])
        self._assert_untouched()

    # --- 依赖校验 ---

    def test_missing_direct_dependency_rejected(self):
        self._expect("permission_dependency_missing", ["patient.profile.edit"])
        self._expect("permission_dependency_missing", ["recycle.restore"])
        self._expect("permission_dependency_missing", ["payment.refund"])
        self._assert_untouched()

    def test_missing_transitive_dependency_rejected(self):
        # image.delete -> image.view -> profile.view，缺最底层一样整单拒绝
        self._expect("permission_dependency_missing",
                     ["patient.image.delete", "patient.image.view"])
        self._assert_untouched()

    def test_complete_transitive_closure_accepted(self):
        result = self._replace([
            "patient.image.delete", "patient.image.view", "patient.profile.view",
        ])
        self.assertTrue(result["changed"])

    # --- 旧脏数据 fail closed ---

    def test_corrupt_existing_permissions_fail_closed(self):
        conn = self._conn()
        _set_perms(conn, "doctor", ["patient.profile.view", "legacy.ghost.key"])
        conn.commit()
        conn.close()

        self._expect("role_permissions_corrupt", ["patient.profile.view"])
        conn = self._conn()
        self.assertEqual(_perms(conn, "doctor"), ["legacy.ghost.key", "patient.profile.view"])
        self.assertEqual(len(_audit_rows(conn)), 0)
        conn.close()

    def test_corrupt_legacy_role_manage_in_existing_set_fail_closed(self):
        conn = self._conn()
        _set_perms(conn, "doctor", ["role.manage"])
        conn.commit()
        conn.close()
        self._expect("role_permissions_corrupt", ["patient.profile.view"])

    # --- 角色资格 ---

    def test_legacy_admin_role_never_configurable(self):
        self._expect("role_not_configurable", ["patient.profile.view"], role_key="admin")
        self._assert_untouched(role_key="admin")

    def test_missing_role_rejected(self):
        self._expect("role_not_found", ["patient.profile.view"], role_key="ghost")
        conn = self._conn()
        self.assertEqual(_perms(conn, "ghost"), [])
        self.assertEqual(len(_audit_rows(conn)), 0)
        conn.close()

    # --- actor 资格 ---

    def test_plain_user_actor_rejected(self):
        conn = self._conn()
        _insert_staff(conn, "s9")
        _insert_user(conn, "plain", staff_id="s9", is_system_admin=0)
        conn.commit()
        conn.close()
        self._expect("actor_not_authorized", ["patient.profile.view"], actor_user_id="plain")
        self._assert_untouched()

    def test_legacy_admin_role_assignment_actor_rejected(self):
        conn = self._conn()
        _insert_staff(conn, "s10")
        _insert_user(conn, "legacy", staff_id="s10", is_system_admin=0)
        _assign_role(conn, "s10", "admin")
        _set_perms(conn, "admin", ["patient.profile.view"])
        conn.commit()
        conn.close()
        self._expect("actor_not_authorized", ["patient.profile.view"], actor_user_id="legacy")
        self._assert_untouched()

    def test_inactive_admin_actor_rejected(self):
        conn = self._conn()
        _insert_staff(conn, "s11")
        _insert_user(conn, "gone", staff_id="s11", is_system_admin=1, is_active=0)
        conn.commit()
        conn.close()
        self._expect("actor_not_authorized", ["patient.profile.view"], actor_user_id="gone")
        self._assert_untouched()

    def test_unknown_actor_rejected_before_role_existence_is_revealed(self):
        self._expect("actor_not_authorized", ["patient.profile.view"],
                     actor_user_id="nobody", role_key="ghost")

    # --- 敌意 role_key ---

    def test_hostile_role_key_subclass_cannot_write_legacy_admin(self):
        # 内容为 'admin' 但 __eq__ 恒假：`role_key == _LEGACY_ADMIN_ROLE` 判不出来，
        # 而 SQLite 仍按底层文本 'admin' 绑定，旧代码会真的给 admin 写权限。
        hostile = _HostileRoleKey("admin")
        self.assertFalse(hostile == "admin")
        self.assertEqual(str(hostile), "admin")

        with self.assertRaises(access_service.AccessServiceError) as ctx:
            self._replace(["patient.profile.view"], role_key=hostile)
        self.assertEqual(ctx.exception.code, "role_invalid")
        self.assertNotIn("HOSTILE-ROLE-KEY-SECRET", str(ctx.exception))
        self.assertNotIn("HOSTILE-ROLE-KEY-SECRET", repr(ctx.exception))
        self._assert_untouched(role_key="admin")

    def test_hostile_role_key_subclass_cannot_write_ordinary_role(self):
        hostile = _HostileRoleKey("doctor")
        self._expect("role_invalid", ["patient.profile.view"], role_key=hostile)
        self._assert_untouched(role_key="doctor")

    def test_role_key_type_and_blank_validation(self):
        for bad in (None, b"doctor", bytearray(b"doctor"), 5, ("doctor",),
                    ["doctor"], "", "   ", "\t\n"):
            with self.subTest(bad=repr(bad)):
                self._expect("role_invalid", ["patient.profile.view"], role_key=bad)
        self._assert_untouched()

    def test_whitespace_padded_role_key_is_not_silently_trimmed(self):
        # ' doctor ' 不是稳定角色键：既不静默 trim 去命中 doctor，也不许落库。
        self._expect("role_not_found", ["patient.profile.view"], role_key=" doctor ")
        self._assert_untouched(role_key="doctor")

    def test_unauthorized_actor_precedes_hostile_role_key(self):
        # 非法 role_key 不得把未授权 actor 的 actor_not_authorized 顶掉。
        for bad in (_HostileRoleKey("admin"), None, b"doctor", "  "):
            with self.subTest(bad=repr(bad)):
                self._expect("actor_not_authorized", ["patient.profile.view"],
                             actor_user_id="nobody", role_key=bad)
        self._assert_untouched(role_key="admin")

    # --- 敌意 reason / datetime ---

    def test_hostile_reason_subclass_rejected_without_leak(self):
        hostile = _HostileReason("正当理由")
        with self.assertRaises(access_service.AccessServiceError) as ctx:
            self._replace(["patient.profile.view"], reason=hostile)
        self.assertEqual(ctx.exception.code, "reason_invalid")
        for text in (str(ctx.exception), repr(ctx.exception)):
            self.assertNotIn("SECRET_REASON_LEAK", text)
            self.assertNotIn("HOSTILE-REASON-SECRET", text)
        self.assertIsNone(ctx.exception.__cause__)
        self.assertIsNone(ctx.exception.__context__)
        self._assert_untouched()

    def test_benign_reason_subclass_rejected(self):
        class _StrLike(str):
            pass

        self._expect("reason_invalid", ["patient.profile.view"], reason=_StrLike("ok"))
        self._assert_untouched()

    def test_hostile_datetime_now_rejected_without_touching_db(self):
        hostile = _HostileDatetime(2026, 7, 15, 10, 0, 0, tzinfo=BJ)
        with _refuse_connect():
            with self.assertRaises(access_service.AccessServiceError) as ctx:
                self._replace(["patient.profile.view"], now=hostile)
        self.assertEqual(ctx.exception.code, "now_invalid")
        for text in (str(ctx.exception), repr(ctx.exception)):
            self.assertNotIn("SECRET_TZ_LEAK", text)
            self.assertNotIn("HOSTILE-DATETIME-SECRET", text)
        self._assert_untouched()

    def test_hostile_datetime_reauth_rejected_without_touching_db(self):
        hostile = _HostileDatetime(2026, 7, 15, 9, 59, 50, tzinfo=BJ)
        with _refuse_connect():
            with self.assertRaises(access_service.AccessServiceError) as ctx:
                self._replace(["patient.profile.view"], reauthenticated_at=hostile)
        self.assertEqual(ctx.exception.code, "reauth_invalid")
        for text in (str(ctx.exception), repr(ctx.exception)):
            self.assertNotIn("SECRET_TZ_LEAK", text)
            self.assertNotIn("HOSTILE-DATETIME-SECRET", text)
        self._assert_untouched()

    def test_naive_hostile_datetime_subclass_rejected(self):
        naive = _HostileDatetime(2026, 7, 15, 10, 0, 0)
        with _refuse_connect():
            self._expect("now_invalid", ["patient.profile.view"], now=naive)
            self._expect("reauth_invalid", ["patient.profile.view"],
                         reauthenticated_at=naive)
        self._assert_untouched()

    def test_flipping_tzinfo_cannot_refresh_stale_reauth(self):
        """内建 datetime + 每次换答案的 tzinfo：09:00 真实是一小时前（第一次报的
        UTC+0），第二次报 UTC-1 就能把它冒充成 10:00 UTC = now。归一化只准读一次
        offset，冻结第一个答案，stale 必须挡在 reauth_expired。"""
        flip = _FlippingTZ([timedelta(0), timedelta(hours=-1)])
        stale = datetime(2026, 7, 15, 9, 0, 0, tzinfo=flip)
        now_utc = datetime(2026, 7, 15, 10, 0, 0, tzinfo=timezone.utc)

        with self.assertRaises(access_service.AccessServiceError) as ctx:
            self._replace(["patient.profile.view"], reauthenticated_at=stale, now=now_utc)
        self.assertIn(ctx.exception.code, ("reauth_expired", "reauth_invalid"))
        self.assertEqual(flip.calls, 1)
        self._assert_untouched()

    def test_flipping_tzinfo_on_now_read_once(self):
        flip = _FlippingTZ([timedelta(0), timedelta(hours=-1)])
        now = datetime(2026, 7, 15, 10, 0, 0, tzinfo=flip)
        reauth = datetime(2026, 7, 15, 9, 0, 0, tzinfo=timezone.utc)

        self._expect("reauth_expired", ["patient.profile.view"],
                     reauthenticated_at=reauth, now=now)
        self.assertEqual(flip.calls, 1)
        self._assert_untouched()

    # --- reason / reauth / now ---

    def test_reason_validation(self):
        self._expect("reason_required", ["patient.profile.view"], reason="")
        self._expect("reason_invalid", ["patient.profile.view"], reason=None)
        self._assert_untouched()

    def test_reauth_window_boundaries(self):
        self._replace(["patient.profile.view"],
                      reauthenticated_at=NOW - timedelta(seconds=299))
        conn = self._conn()
        self.assertEqual(_perms(conn, "doctor"), ["patient.profile.view"])
        conn.close()

        self._replace(["patient.profile.view", "appointment.view"],
                      expected_permissions=["patient.profile.view"],
                      reauthenticated_at=NOW - timedelta(seconds=300))
        conn = self._conn()
        self.assertEqual(_perms(conn, "doctor"), ["appointment.view", "patient.profile.view"])
        conn.close()

        self._expect("reauth_expired", ["recycle.view"],
                     reauthenticated_at=NOW - timedelta(seconds=301))
        self._expect("reauth_expired", ["recycle.view"],
                     reauthenticated_at=NOW + timedelta(seconds=5))
        self._expect("reauth_invalid", ["recycle.view"],
                     reauthenticated_at=datetime(2026, 7, 15, 9, 59, 50))
        self._expect("now_invalid", ["recycle.view"], now=datetime(2026, 7, 15, 10, 0, 0))

    def test_error_does_not_leak_reason_or_path(self):
        secret = "机密原因-不该外泄"
        try:
            self._replace(["bogus.key"], reason=secret)
        except access_service.AccessServiceError as err:
            self.assertNotIn(secret, str(err))
            self.assertNotIn(secret, repr(err))
            self.assertNotIn(str(self.db_path), str(err))
            self.assertNotIn(str(self.db_path), repr(err))

    # --- 库文件安全 ---

    def test_missing_db_rejected_without_creating_file(self):
        missing = Path(self._tmp.name) / "missing.db"
        with self.assertRaises(access_service.AccessServiceError) as ctx:
            self._replace(["patient.profile.view"], db_path=missing)
        self.assertEqual(ctx.exception.code, "database_unavailable")
        self.assertFalse(missing.exists())

    def test_no_files_outside_synthetic_db(self):
        self._replace(["patient.profile.view"])
        names = {p.name for p in Path(self._tmp.name).iterdir()}
        self.assertTrue(names.issubset({"synthetic.db", "synthetic.db-journal"}))


# --- 事务失败必回滚 ---


class ReplaceRolePermissionsTransactionTests(unittest.TestCase):
    setUp = ReplaceRolePermissionsTests.setUp
    _conn = ReplaceRolePermissionsTests._conn
    _replace = ReplaceRolePermissionsTests._replace

    def test_audit_trigger_failure_rolls_back(self):
        conn = self._conn()
        _set_perms(conn, "doctor", ["appointment.view"])
        conn.execute(
            "create trigger boom before insert on audit_logs "
            "begin select raise(abort, 'audit boom'); end"
        )
        conn.commit()
        conn.close()

        with self.assertRaises(access_service.AccessServiceError) as ctx:
            self._replace(
                ["patient.profile.view"],
                expected_permissions=["appointment.view"],
            )
        self.assertEqual(ctx.exception.code, "transaction_failed")
        conn = self._conn()
        self.assertEqual(_perms(conn, "doctor"), ["appointment.view"])
        self.assertEqual(len(_audit_rows(conn)), 0)
        conn.close()

    def test_key_sql_failures_report_transaction_failed(self):
        for match in ("delete from role_permissions", "insert into role_permissions",
                      "count(distinct", "commit"):
            with self.subTest(match=match):
                with tempfile.TemporaryDirectory() as tmp_dir:
                    db_path = Path(tmp_dir) / "synthetic.db"
                    _build_db(db_path)
                    conn = sqlite3.connect(str(db_path))
                    _insert_staff(conn, "sa")
                    _insert_user(conn, "actor", staff_id="sa", is_system_admin=1)
                    _set_perms(conn, "doctor", ["appointment.view"])
                    conn.commit()
                    conn.close()

                    needle = match
                    # 每个子例声明自己的 Connection，并在退出子例时恢复；
                    # used 证明本次真的走了本子例的 fake，而不是上一个子例
                    # 叠加残留的 patch（那样 needle 会是别人的）。
                    used = []

                    class _FailConnection(sqlite3.Connection):
                        def execute(self, sql, *args, **kwargs):
                            used.append(needle)
                            if needle in sql.lower():
                                raise sqlite3.OperationalError("injected failure")
                            return super().execute(sql, *args, **kwargs)

                    with _connect_factory(_FailConnection):
                        with self.assertRaises(access_service.AccessServiceError) as ctx:
                            self._replace(
                                ["patient.profile.view"],
                                expected_permissions=["appointment.view"],
                                db_path=db_path,
                            )
                    self.assertEqual(ctx.exception.code, "transaction_failed")
                    self.assertEqual(set(used), {match})

                    conn = self._conn(db_path=db_path)
                    self.assertEqual(_perms(conn, "doctor"), ["appointment.view"])
                    self.assertEqual(len(_audit_rows(conn)), 0)
                    conn.close()

    def test_no_change_executes_no_write_statement(self):
        conn = self._conn()
        _set_perms(conn, "doctor", ["patient.profile.view", "appointment.view"])
        conn.commit()
        conn.close()

        attempted = []

        class _RefuseWriteConnection(sqlite3.Connection):
            def execute(self, sql, *args, **kwargs):
                flat = " ".join(sql.lower().split())
                if flat.startswith(("delete from role_permissions",
                                    "insert into role_permissions",
                                    "insert into audit_logs")):
                    attempted.append(flat)
                    raise AssertionError("no-op 路径执行了写语句: %s" % flat)
                return super().execute(sql, *args, **kwargs)

        with _connect_factory(_RefuseWriteConnection):
            result = self._replace(
                ["appointment.view", "patient.profile.view"],
                expected_permissions=["appointment.view", "patient.profile.view"],
            )

        self.assertEqual(attempted, [])
        self.assertFalse(result["changed"])

    def test_actor_flags_must_be_real_integer_one(self):
        class _FakeCursor:
            def __init__(self, row):
                self._row = row

            def fetchone(self):
                return self._row

        class _EqualsOne:
            """等值伪装成 1 的敌意对象（== 1 为真，但不是 int）。"""

            def __eq__(self, other):
                return other == 1

            def __ne__(self, other):
                return not self.__eq__(other)

            def __hash__(self):
                return hash(1)

            def __repr__(self):
                return "<EQUALS-ONE-SENTINEL>"

        def _run_with_actor_flags(active_flag, admin_flag):
            row = {"id": "actor", "username": "actor",
                   "is_active": active_flag, "is_system_admin": admin_flag}

            class _ActorRowConnection(sqlite3.Connection):
                def execute(self, sql, *args, **kwargs):
                    cursor = super().execute(sql, *args, **kwargs)
                    if "is_system_admin from users where id" in sql.lower():
                        return _FakeCursor(row)
                    return cursor

            real_connect = sqlite3.connect

            def fake_connect(*args, **kwargs):
                kwargs["factory"] = _ActorRowConnection
                return real_connect(*args, **kwargs)

            with mock.patch("local_app.access_service.sqlite3.connect", fake_connect):
                return self._replace(["patient.profile.view"])

        def _assert_rejected(active_flag, admin_flag):
            with self.assertRaises(access_service.AccessServiceError) as ctx:
                _run_with_actor_flags(active_flag, admin_flag)
            self.assertEqual(ctx.exception.code, "actor_not_authorized")
            self.assertNotIn("EQUALS-ONE-SENTINEL", str(ctx.exception))
            self.assertNotIn("EQUALS-ONE-SENTINEL", repr(ctx.exception))
            conn = self._conn()
            self.assertEqual(_perms(conn, "doctor"), [])
            self.assertEqual(len(_audit_rows(conn)), 0)
            conn.close()

        # is_active 与 is_system_admin 必须分别独立判定：单独把其中一个
        # 伪装成 1 都不能借另一个真 1 蒙混过关（is_active 先短路会掩盖
        # is_system_admin 校验被放宽的回归）。
        for flag_value in (1.0, True, _EqualsOne()):
            with self.subTest(bad_flag="is_system_admin", flag=repr(flag_value)):
                _assert_rejected(1, flag_value)
            with self.subTest(bad_flag="is_active", flag=repr(flag_value)):
                _assert_rejected(flag_value, 1)

        # 正常整数 (1, 1) 走同一条注入路径仍必须放行
        result = _run_with_actor_flags(1, 1)
        self.assertTrue(result["changed"])
        conn = self._conn()
        self.assertEqual(_perms(conn, "doctor"), ["patient.profile.view"])
        conn.close()

    def test_begin_immediate_precedes_any_write(self):
        seen = []

        class _RecordingConnection(sqlite3.Connection):
            def execute(self, sql, *args, **kwargs):
                seen.append(sql.strip().lower())
                return super().execute(sql, *args, **kwargs)

        with _connect_factory(_RecordingConnection):
            self._replace(["patient.profile.view"])

        self.assertIn("begin immediate", seen)
        begin_at = seen.index("begin immediate")
        permission_read_at = next(
            i for i, sql in enumerate(seen)
            if sql.startswith("select perm_key from role_permissions")
        )
        self.assertGreater(permission_read_at, begin_at)
        writes = [i for i, sql in enumerate(seen)
                  if sql.startswith(("delete", "insert", "update"))]
        self.assertTrue(writes)
        self.assertTrue(all(i > begin_at for i in writes))
        self.assertEqual(seen[-1], "commit")
