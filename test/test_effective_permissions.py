"""普通有效权限纯计算 access_service.effective_permissions() 的正反例（Task 2A）。

只用 :memory: 合成库，不接触真实库/患者/员工数据。
"""
import sqlite3
import unittest

from local_app import access_service
from local_app import personnel_access_migration as pam

_LEGACY_ADMIN_ROLE = "admin"


def _build_db():
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    con.executescript(
        """
        CREATE TABLE roles (
            role_key text primary key,
            name text not null,
            is_system integer not null default 0,
            sort integer not null default 0,
            created_at text,
            updated_at text
        );
        CREATE TABLE role_permissions (
            role_key text not null,
            perm_key text not null,
            primary key (role_key, perm_key)
        );
        """
    )
    for role_key, name in (
        ("doctor", "医生"), ("nurse", "护士"), (_LEGACY_ADMIN_ROLE, "管理员"),
    ):
        con.execute("insert into roles(role_key, name) values (?, ?)", (role_key, name))
    con.executescript(pam.load_personnel_access_schema_sql())
    return con


def _insert_staff(con, staff_id, employment_status="employed"):
    con.execute(
        "insert into staff_members(staff_id, name, employment_status) values (?, ?, ?)",
        (staff_id, staff_id, employment_status),
    )


def _insert_user(con, user_id, *, staff_id=None, is_active=1, is_system_admin=0,
                  account_kind="staff"):
    con.execute(
        "insert into users(id, username, password_hash, staff_id, is_active, "
        "is_system_admin, account_kind) values (?, ?, 'x$y', ?, ?, ?, ?)",
        (user_id, user_id, staff_id, is_active, is_system_admin, account_kind),
    )


def _assign_role(con, staff_id, role_key):
    con.execute(
        "insert into staff_role_assignments(staff_id, role_key) values (?, ?)",
        (staff_id, role_key),
    )


def _grant(con, role_key, *perm_keys):
    for perm_key in perm_keys:
        con.execute(
            "insert into role_permissions(role_key, perm_key) values (?, ?)",
            (role_key, perm_key),
        )


class EffectivePermissionsTests(unittest.TestCase):
    def setUp(self):
        self.con = _build_db()
        self.addCleanup(self.con.close)

    def test_unknown_user_returns_empty(self):
        result = access_service.effective_permissions(self.con, "missing")
        self.assertEqual(result, frozenset())

    def test_inactive_account_returns_empty(self):
        _insert_staff(self.con, "s1")
        _assign_role(self.con, "s1", "doctor")
        _grant(self.con, "doctor", "medical_record.view")
        _insert_user(self.con, "u1", staff_id="s1", is_active=0)
        result = access_service.effective_permissions(self.con, "u1")
        self.assertEqual(result, frozenset())

    def test_staff_left_returns_empty(self):
        _insert_staff(self.con, "s1", employment_status="left")
        _assign_role(self.con, "s1", "doctor")
        _grant(self.con, "doctor", "medical_record.view")
        _insert_user(self.con, "u1", staff_id="s1")
        result = access_service.effective_permissions(self.con, "u1")
        self.assertEqual(result, frozenset())

    def test_staff_archived_returns_empty(self):
        _insert_staff(self.con, "s1", employment_status="archived")
        _assign_role(self.con, "s1", "doctor")
        _grant(self.con, "doctor", "medical_record.view")
        _insert_user(self.con, "u1", staff_id="s1")
        result = access_service.effective_permissions(self.con, "u1")
        self.assertEqual(result, frozenset())

    def test_independent_admin_has_no_business_permissions(self):
        _insert_user(self.con, "u1", staff_id=None, is_system_admin=1,
                      account_kind="independent_admin")
        result = access_service.effective_permissions(self.con, "u1")
        self.assertEqual(result, frozenset())

    def test_break_glass_has_no_business_permissions(self):
        _insert_user(self.con, "u1", staff_id=None, is_system_admin=1,
                      account_kind="break_glass")
        result = access_service.effective_permissions(self.con, "u1")
        self.assertEqual(result, frozenset())

    def test_independent_admin_with_corrupt_staff_link_still_empty(self):
        _insert_staff(self.con, "s1")
        _assign_role(self.con, "s1", "doctor")
        _grant(self.con, "doctor", "medical_record.view")
        self.con.execute("PRAGMA ignore_check_constraints = ON")
        _insert_user(self.con, "u1", staff_id="s1", is_system_admin=1,
                      account_kind="independent_admin")
        result = access_service.effective_permissions(self.con, "u1")
        self.assertEqual(result, frozenset())

    def test_break_glass_with_corrupt_staff_link_still_empty(self):
        _insert_staff(self.con, "s1")
        _assign_role(self.con, "s1", "doctor")
        _grant(self.con, "doctor", "medical_record.view")
        self.con.execute("PRAGMA ignore_check_constraints = ON")
        _insert_user(self.con, "u1", staff_id="s1", is_system_admin=1,
                      account_kind="break_glass")
        result = access_service.effective_permissions(self.con, "u1")
        self.assertEqual(result, frozenset())

    def test_multi_role_union_deduped(self):
        _insert_staff(self.con, "s1")
        _assign_role(self.con, "s1", "doctor")
        _assign_role(self.con, "s1", "nurse")
        _grant(self.con, "doctor", "medical_record.view", "medical_record.edit")
        _grant(self.con, "nurse", "medical_record.view", "sterilization_order.view")
        _insert_user(self.con, "u1", staff_id="s1")
        result = access_service.effective_permissions(self.con, "u1")
        self.assertEqual(
            result,
            frozenset({
                "medical_record.view", "medical_record.edit",
                "sterilization_order.view",
            }),
        )

    def test_unknown_permission_key_filtered_out(self):
        _insert_staff(self.con, "s1")
        _assign_role(self.con, "s1", "doctor")
        _grant(self.con, "doctor", "medical_record.view", "not.a.real.permission")
        _insert_user(self.con, "u1", staff_id="s1")
        result = access_service.effective_permissions(self.con, "u1")
        self.assertEqual(result, frozenset({"medical_record.view"}))

    def test_legacy_admin_role_excluded_even_if_assigned(self):
        _insert_staff(self.con, "s1")
        _assign_role(self.con, "s1", _LEGACY_ADMIN_ROLE)
        _grant(self.con, _LEGACY_ADMIN_ROLE, "medical_record.view", "staff.view")
        _insert_user(self.con, "u1", staff_id="s1")
        result = access_service.effective_permissions(self.con, "u1")
        self.assertEqual(result, frozenset())

    def test_system_admin_staff_only_gets_role_permissions(self):
        _insert_staff(self.con, "s1")
        _assign_role(self.con, "s1", "doctor")
        _grant(self.con, "doctor", "medical_record.view")
        _insert_user(self.con, "u1", staff_id="s1", is_system_admin=1)
        result = access_service.effective_permissions(self.con, "u1")
        self.assertEqual(result, frozenset({"medical_record.view"}))

    def test_staff_id_not_found_returns_empty(self):
        self.con.execute("PRAGMA foreign_keys = OFF")
        _insert_user(self.con, "u1", staff_id="ghost", account_kind="staff")
        result = access_service.effective_permissions(self.con, "u1")
        self.assertEqual(result, frozenset())


if __name__ == "__main__":
    unittest.main()
