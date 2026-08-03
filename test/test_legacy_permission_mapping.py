"""旧 35 权限键 → 新 99 权限键迁移工具测试：全程 TemporaryDirectory 合成库，不接触真实数据。

映射唯一真相源 = docs/权限映射_旧到新_2026-07-18.md 第 3 节（D1-D9 已拍板）。
"""

import io
import json
import sqlite3
import subprocess
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory

from local_app import auth
from local_app.access_policy import ALL_PERMISSIONS
from local_app.oneoff import legacy_permission_mapping as mapping
from local_app.oneoff import migrate_legacy_permissions as cli

REPO_ROOT = Path(__file__).resolve().parent.parent

# D2 拍板：删除类 6 键不随旧键派发，仅主任（director）授予。
D2_DIRECTOR_DELETE_KEYS = {
    "patient.image.delete",
    "appointment.delete",
    "call_record.delete",
    "communication.delete",
    "lab_order.delete",
    "return_visit.delete",
}
# D4 拍板：审核/停用/删除 4 键仅主任授予。
D4_DIRECTOR_AUDIT_KEYS = {
    "instrument.disable",
    "instrument.delete",
    "sterilization_order.audit",
    "sterilization_order.cancel_audit",
}
# D9 拍板：仅医生、主任授予。
D9_AI_REVIEW_KEY = "medical_record.ai.review"

# 最小合成库 DDL：roles/role_permissions/staff_members/staff_role_assignments/users/audit_logs。
# audit_logs 只建 8 核心列（无扩展列），验证 CLI 按现有表结构对齐写入列。
_SCHEMA_SQL = """
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
create table staff_members (
    staff_id text primary key,
    name text not null,
    employment_status text not null default 'employed'
);
create table staff_role_assignments (
    staff_id text not null,
    role_key text not null,
    is_primary integer not null default 0,
    created_at text,
    primary key (staff_id, role_key)
);
create table users (
    id text primary key,
    username text not null,
    display_name text not null default '',
    password_hash text not null,
    staff_id text,
    is_active integer not null default 1,
    is_system_admin integer not null default 0,
    account_kind text not null default 'staff'
);
create table audit_logs (
    audit_id integer primary key autoincrement,
    entity_type text not null,
    entity_id text not null,
    action text not null,
    old_json text,
    new_json text,
    operator text,
    created_at text not null default current_timestamp
);
"""

# 合成员工/账号：名字与用户名为显眼的合成记号，供"report 不得泄露姓名/用户名"断言。
_SYNTHETIC_STAFF = [
    ("staff-syn-01", "合成姓名张神医", "employed"),
    ("staff-syn-02", "合成姓名李仙姑", "employed"),
    ("staff-syn-03", "合成姓名王隐士", "employed"),
]
_SYNTHETIC_USERS = [
    ("user-syn-01", "syn_user_zshen", "合成显示张", "synthetic-hash-1", "staff-syn-01"),
    ("user-syn-02", "syn_user_lxian", "合成显示李", "synthetic-hash-2", "staff-syn-02"),
]
_SYNTHETIC_ASSIGNMENTS = [
    ("staff-syn-01", "doctor", 1),
    ("staff-syn-02", "doctor", 1),
    ("staff-syn-03", "director", 1),
]
_PRIVACY_TOKENS = (
    "合成姓名张神医",
    "合成姓名李仙姑",
    "合成姓名王隐士",
    "syn_user_zshen",
    "syn_user_lxian",
    "合成显示张",
    "合成显示李",
)


def _fixture_role_perms():
    """合成混合键集：director 含报表门禁双键 + 待删 role.manage；doctor 混一个新键；nurse 持 report.view。"""
    return {
        "admin": {"audit.view"},
        "cashier": set(auth.DEFAULT_ROLE_PERMS["cashier"]),
        "director": set(auth.DEFAULT_ROLE_PERMS["director"]) | {
            "report.view",
            "data.export",
            "role.manage",
            "audit.view",
        },
        "doctor": set(auth.DEFAULT_ROLE_PERMS["doctor"]) | {"patient.profile.export"},
        "nurse": set(auth.DEFAULT_ROLE_PERMS["nurse"]) | {"report.view"},
    }


def _build_db(root, role_perms=None, dirty_key=None):
    db_path = Path(root) / "synthetic-clinic.sqlite3"
    conn = sqlite3.connect(db_path)
    conn.executescript(_SCHEMA_SQL)
    perms = role_perms if role_perms is not None else _fixture_role_perms()
    for sort, (role_key, keys) in enumerate(sorted(perms.items())):
        conn.execute(
            "insert into roles(role_key, name, is_system, sort) values (?, ?, 1, ?)",
            (role_key, f"合成角色{role_key}", sort),
        )
        for perm in sorted(keys):
            conn.execute(
                "insert into role_permissions(role_key, perm_key) values (?, ?)",
                (role_key, perm),
            )
    if dirty_key:
        conn.execute(
            "insert into role_permissions(role_key, perm_key) values ('nurse', ?)",
            (dirty_key,),
        )
    conn.executemany(
        "insert into staff_members(staff_id, name, employment_status) values (?, ?, ?)",
        _SYNTHETIC_STAFF,
    )
    conn.executemany(
        "insert into users(id, username, display_name, password_hash, staff_id) "
        "values (?, ?, ?, ?, ?)",
        _SYNTHETIC_USERS,
    )
    conn.executemany(
        "insert into staff_role_assignments(staff_id, role_key, is_primary) values (?, ?, ?)",
        _SYNTHETIC_ASSIGNMENTS,
    )
    conn.commit()
    conn.close()
    return db_path


def _read_role_permissions(db_path):
    conn = sqlite3.connect(db_path)
    result = {}
    for role_key, perm_key in conn.execute(
        "select role_key, perm_key from role_permissions"
    ):
        result.setdefault(role_key, set()).add(perm_key)
    conn.close()
    return result


def _read_audit_rows(db_path):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = [dict(row) for row in conn.execute("select * from audit_logs order by audit_id")]
    conn.close()
    return rows


class MappingCompletenessTest(unittest.TestCase):
    """映射完备性：旧 35 键每键被显式覆盖（同名/展开/删除三选一），目标全部 ∈ 99 目录。"""

    def test_legacy_catalog_matches_auth_permission_defs(self):
        self.assertEqual(mapping.LEGACY_PERMISSIONS, auth.PERMISSIONS)
        self.assertEqual(len(mapping.LEGACY_PERMISSIONS), 35)

    def test_every_legacy_key_covered_exactly_once(self):
        same = set(mapping.SAME_NAME_KEEP)
        expanded = set(mapping.EXPANSION_MAP)
        gated = set(mapping.REPORT_GATED_LEGACY_KEYS)
        removed = set(mapping.REMOVED_LEGACY_KEYS)
        self.assertFalse(same & expanded)
        self.assertFalse(same & gated)
        self.assertFalse(same & removed)
        self.assertFalse(expanded & gated)
        self.assertFalse(expanded & removed)
        self.assertFalse(gated & removed)
        self.assertEqual(
            same | expanded | gated | removed, set(mapping.LEGACY_PERMISSIONS)
        )
        self.assertEqual(len(same), 11)
        self.assertEqual(len(expanded), 19)

    def test_removed_keys_are_exactly_the_d6_three(self):
        self.assertEqual(
            set(mapping.REMOVED_LEGACY_KEYS),
            {"role.manage", "backup.manage", "sync.manage"},
        )

    def test_all_mapping_targets_are_in_v3_catalog(self):
        targets = set(mapping.SAME_NAME_KEEP)
        for values in mapping.EXPANSION_MAP.values():
            targets |= set(values)
        for values in mapping.ROLE_BLANKET_GRANTS.values():
            targets |= set(values)
        for values in mapping.DERIVED_BY_LEGACY_KEY.values():
            targets |= set(values)
        for values in mapping.RESULT_DERIVED_GRANTS.values():
            targets |= set(values)
        self.assertTrue(targets <= ALL_PERMISSIONS)
        # D6 移除的三键本身不在 99 目录（不可分配给角色）。
        self.assertFalse(set(mapping.REMOVED_LEGACY_KEYS) & ALL_PERMISSIONS)


class MappingDecisionBehaviorTest(unittest.TestCase):
    """D1-D9 逐条行为测试。"""

    def test_same_name_keys_pass_through(self):
        result = mapping.compute_new_permissions(
            "custom", {"audit.view", "billing.view", "oral_exam.manage"}
        )
        self.assertIsInstance(result, frozenset)
        self.assertEqual(result, frozenset({"audit.view", "billing.view", "oral_exam.manage"}))

    def test_patient_view_expands_and_derives_group_and_master_data(self):
        # patient.view 展开 4 键 + D7/D8 派生 patient.group.manage/master_data.view。
        result = mapping.compute_new_permissions("custom", {"patient.view"})
        self.assertEqual(
            result,
            frozenset({
                "patient.profile.view",
                "patient.image.view",
                "appointment.view",
                "return_visit.view",
                "patient.group.manage",
                "master_data.view",
            }),
        )

    def test_master_data_manage_alone_closure_adds_view(self):
        # D8 授予规则不随 manage 派 view；#859 依赖闭包要求必须补齐 master_data.view。
        result = mapping.compute_new_permissions("custom", {"master_data.manage"})
        self.assertEqual(
            result, frozenset({"master_data.manage", "master_data.view"})
        )

    def test_patient_edit_expansion_excludes_delete_keys(self):
        # D2：删除类键不随 patient.edit 派发；#859：依赖闭包补齐各项 view。
        result = mapping.compute_new_permissions("custom", {"patient.edit"})
        self.assertEqual(
            result,
            frozenset({
                "patient.profile.view",
                "patient.profile.edit",
                "patient.tag.manage",
                "patient.image.view",
                "patient.image.manage",
                "appointment.view",
                "appointment.create",
                "appointment.edit",
                "appointment.status",
                "appointment.cancel",
                "return_visit.view",
                "return_visit.manage",
            }),
        )
        self.assertNotIn("patient.image.delete", result)
        self.assertNotIn("appointment.delete", result)

    def test_d2_d4_blanket_grants_apply_to_director_only(self):
        result = mapping.compute_new_permissions("director", frozenset())
        # 主任 blanket 另含 D11 报表包（见 test_d11_*），此处锁定 D2/D4/D9 部分。
        self.assertTrue(
            frozenset(D2_DIRECTOR_DELETE_KEYS | D4_DIRECTOR_AUDIT_KEYS | {D9_AI_REVIEW_KEY})
            <= result
        )
        for role_key in ("doctor", "nurse", "cashier", "reception", "custom"):
            result = mapping.compute_new_permissions(role_key, frozenset())
            self.assertFalse(
                set(D2_DIRECTOR_DELETE_KEYS | D4_DIRECTOR_AUDIT_KEYS) & result, role_key
            )

    def test_d11_director_report_blanket_without_legacy_trigger(self):
        # D11：主任即使未持有 report.view/data.export 也直接获财务/绩效/运营报表包。
        for old_keys in (frozenset(), {"report.view", "data.export"}):
            result = mapping.compute_new_permissions("director", old_keys)
            self.assertTrue(
                {
                    "report.finance.view",
                    "report.finance.export",
                    "report.performance.view",
                    "report.operations.view",
                    "report.operations.export",
                }
                <= result,
                old_keys,
            )
            self.assertFalse(
                {"report.arrears.view", "report.cashier.view", "report.cashier.export"}
                & result
            )

    def test_d11_cashier_and_reception_report_blanket_without_legacy_trigger(self):
        # D11：收银员/前台即使未持有旧报表键也直接获欠款/收银报表包。
        for role_key in ("cashier", "reception"):
            for old_keys in (frozenset(), {"report.view", "data.export"}):
                result = mapping.compute_new_permissions(role_key, old_keys)
                self.assertEqual(
                    result,
                    frozenset({
                        "report.arrears.view",
                        "report.cashier.view",
                        "report.cashier.export",
                    }),
                    (role_key, old_keys),
                )

    def test_d3_other_roles_get_no_report_keys(self):
        for role_key in ("nurse", "doctor", "consultant", "custom"):
            result = mapping.compute_new_permissions(role_key, {"report.view", "data.export"})
            self.assertFalse({k for k in result if k.startswith("report.")}, role_key)
        self.assertEqual(mapping.compute_new_permissions("nurse", {"report.view"}), frozenset())

    def test_d12_no_management_keys_via_blanket(self):
        # D12：staff/account/settings/audit/库房 manage/回收站等管理键不做 blanket 授予，
        # 迁移后仅系统管理员持有（旧键持有者仍按映射表正常展开，不受本条影响）。
        management_keys = {
            "staff.view", "staff.edit", "staff.employment",
            "account.view", "account.open", "account.security",
            "clinic_settings.manage", "audit.view",
            "recycle.view", "recycle.restore",
            "patient.profile.merge", "patient.profile.export",
            "master_data.manage",
        }
        management_keys |= {
            k for k in ALL_PERMISSIONS
            if k.startswith("warehouse.") and not k.endswith(".view")
        }
        for role_key in (
            "director", "doctor", "nurse", "cashier", "reception",
            "consultant", "assistant", "assistant2", "support", "custom",
        ):
            result = mapping.compute_new_permissions(role_key, frozenset())
            self.assertFalse(management_keys & result, role_key)

    def test_d9_ai_review_blanket_for_doctor_and_director_only(self):
        self.assertEqual(
            mapping.compute_new_permissions("doctor", frozenset()),
            frozenset({D9_AI_REVIEW_KEY, "medical_record.view"}),  # #859 闭包补 view
        )
        self.assertIn(
            D9_AI_REVIEW_KEY, mapping.compute_new_permissions("director", frozenset())
        )
        for role_key in ("nurse", "cashier", "reception", "custom"):
            self.assertNotIn(
                D9_AI_REVIEW_KEY,
                mapping.compute_new_permissions(role_key, frozenset()),
                role_key,
            )

    def test_medical_record_edit_derives_template_manage(self):
        result = mapping.compute_new_permissions("custom", {"medical_record.edit"})
        self.assertEqual(
            result,
            frozenset({
                "medical_record.edit",
                "medical_record.template.manage",
                "medical_record.view",  # #859 闭包补 edit 的依赖
            }),
        )

    def test_medical_record_view_derives_treatment_plan_view(self):
        result = mapping.compute_new_permissions("custom", {"medical_record.view"})
        self.assertEqual(
            result, frozenset({"medical_record.view", "treatment_plan.view"})
        )

    def test_lab_order_manage_derives_view_for_dependency_closure(self):
        result = mapping.compute_new_permissions("custom", {"lab_order.manage"})
        self.assertEqual(
            result, frozenset({"lab_order.manage", "lab_order.view"})
        )

    def test_write_only_legacy_roles_pass_v3_dependency_validation(self):
        # #859：只持写/管理类旧键的角色，迁移结果也必须通过新 GUI 保存校验。
        from local_app import access_service

        for role_key, old_keys in (
            ("custom", {"patient.edit"}),
            ("custom", {"billing.pay", "billing.refund"}),
            ("custom", {"warehouse.manage"}),
            ("custom", {"medical_record.edit"}),
            ("custom", {"treatment.manage"}),
            ("doctor", {"medical_record.edit"}),
        ):
            with self.subTest(role_key=role_key, old_keys=old_keys):
                result = mapping.compute_new_permissions(role_key, old_keys)
                access_service._validate_dependencies(result)  # 不抛即通过
                self.assertTrue(result <= ALL_PERMISSIONS)

    def test_removed_keys_are_dropped_without_derivation(self):
        result = mapping.compute_new_permissions(
            "custom", {"role.manage", "backup.manage", "sync.manage"}
        )
        self.assertEqual(result, frozenset())

    def test_v3_keys_pass_through_unchanged(self):
        # 新旧混合键集：已是 99 目录内的键原样保留。
        result = mapping.compute_new_permissions(
            "custom", {"patient.profile.create", "audit.view"}
        )
        self.assertEqual(result, frozenset({"patient.profile.create", "audit.view"}))

    def test_unknown_keys_fail_closed(self):
        with self.assertRaises(mapping.LegacyPermissionMappingError):
            mapping.compute_new_permissions("custom", {"patient.view", "bogus.key"})

    def test_find_unknown_keys_groups_by_role(self):
        found = mapping.find_unknown_keys({
            "doctor": {"patient.view", "bogus.a"},
            "nurse": {"patient.profile.view"},
            "admin": {"role.manage", "bogus.b", "system_admin.grant"},
        })
        self.assertEqual(
            found, {"doctor": {"bogus.a"}, "admin": {"bogus.b", "system_admin.grant"}}
        )

    def test_preset_role_results_stay_inside_v3_catalog(self):
        # 预置角色默认键集 + D3 双触发变体：迁移结果必须全部落在 99 目录内。
        variants = {role: set(keys) for role, keys in auth.DEFAULT_ROLE_PERMS.items()}
        for role_key in ("director", "cashier", "reception"):
            variants[role_key] |= {"report.view", "data.export"}
        for role_key, old_keys in sorted(variants.items()):
            result = mapping.compute_new_permissions(role_key, old_keys)
            self.assertIsInstance(result, frozenset, role_key)
            self.assertTrue(result <= ALL_PERMISSIONS, role_key)

    def test_all_preset_roles_derive_group_manage_and_master_data_view(self):
        # D7/D8：全部 9 个预置角色默认键集都含 patient.view → 都补这两键。
        self.assertEqual(len(auth.DEFAULT_ROLE_PERMS), 9)
        for role_key, old_keys in sorted(auth.DEFAULT_ROLE_PERMS.items()):
            self.assertIn("patient.view", old_keys, role_key)
            result = mapping.compute_new_permissions(role_key, old_keys)
            self.assertIn("patient.group.manage", result, role_key)
            self.assertIn("master_data.view", result, role_key)


class MigrateApplyEndToEndTest(unittest.TestCase):
    """apply 端到端：合成库 → 精确替换 → 逐角色审计 → 二次幂等 → 脏键整单拒绝。"""

    def test_apply_replaces_role_permissions_and_audits_each_role_then_idempotent(self):
        with TemporaryDirectory() as root:
            db_path = _build_db(root)
            fixture = _fixture_role_perms()
            expected = {
                role_key: mapping.compute_new_permissions(role_key, keys)
                for role_key, keys in fixture.items()
                if role_key != "admin"
            }

            out = io.StringIO()
            with redirect_stdout(out):
                rc = cli.main(["apply", "--db", str(db_path), "--yes"])
            self.assertEqual(rc, 0)
            self.assertIn("退出码语义", out.getvalue())

            actual = _read_role_permissions(db_path)
            self.assertEqual(set(actual), set(fixture))
            for role_key, want in expected.items():
                self.assertEqual(actual[role_key], set(want), role_key)
            # admin 角色跳过：键集原样保留。
            self.assertEqual(actual["admin"], {"audit.view"})

            # 每角色一行审计：action/operator/排序后键集 JSON。
            rows = _read_audit_rows(db_path)
            self.assertEqual(len(rows), len(expected))
            by_role = {row["entity_id"]: row for row in rows}
            self.assertEqual(set(by_role), set(expected))
            for role_key, row in by_role.items():
                self.assertEqual(row["entity_type"], "role")
                self.assertEqual(row["action"], "permission.migration.legacy_to_v3")
                self.assertEqual(row["operator"], "legacy_permission_migration")
                self.assertEqual(json.loads(row["old_json"]), sorted(fixture[role_key]))
                self.assertEqual(json.loads(row["new_json"]), sorted(expected[role_key]))

            # 幂等：迁移后的库再跑一次 = 每角色 changed=False、不写审计、不改键集。
            out = io.StringIO()
            with redirect_stdout(out):
                rc2 = cli.main(["apply", "--db", str(db_path), "--yes"])
            self.assertEqual(rc2, 0)
            self.assertNotIn("changed=True", out.getvalue())
            for role_key in expected:
                self.assertIn(f"角色 {role_key}: changed=False", out.getvalue())
            self.assertEqual(_read_audit_rows(db_path), rows)
            self.assertEqual(_read_role_permissions(db_path), actual)

    def test_apply_with_unknown_key_rejects_all_and_writes_nothing(self):
        with TemporaryDirectory() as root:
            db_path = _build_db(root, dirty_key="bogus.perm.xyz")
            before = _read_role_permissions(db_path)
            err = io.StringIO()
            with redirect_stderr(err):
                rc = cli.main(["apply", "--db", str(db_path), "--yes"])
            self.assertEqual(rc, 1)
            self.assertIn("bogus.perm.xyz", err.getvalue())
            # 整单拒绝、零写入：role_permissions 与 audit_logs 原样。
            self.assertEqual(_read_role_permissions(db_path), before)
            self.assertEqual(_read_audit_rows(db_path), [])


class MigrateReportTest(unittest.TestCase):
    """report：只读、逐角色明细 + 脏键告警 + 受影响员工数；绝不输出姓名/用户名。"""

    def test_report_outputs_mapping_details_without_any_names(self):
        with TemporaryDirectory() as root:
            db_path = _build_db(root)
            out = io.StringIO()
            with redirect_stdout(out):
                rc = cli.main(["report", "--db", str(db_path)])
            text = out.getvalue()
            self.assertEqual(rc, 0)
            for token in _PRIVACY_TOKENS:
                self.assertNotIn(token, text)
            self.assertIn("角色 director", text)
            self.assertIn("角色 doctor", text)
            self.assertIn("受影响员工数", text)
            self.assertIn("退出码语义", text)

    def test_report_flags_unknown_keys_and_exits_1(self):
        with TemporaryDirectory() as root:
            db_path = _build_db(root, dirty_key="bogus.perm.xyz")
            out = io.StringIO()
            with redirect_stdout(out):
                rc = cli.main(["report", "--db", str(db_path)])
            text = out.getvalue()
            self.assertEqual(rc, 1)
            self.assertIn("bogus.perm.xyz", text)
            self.assertIn("nurse", text)
            for token in _PRIVACY_TOKENS:
                self.assertNotIn(token, text)

    def test_report_missing_database_is_data_problem(self):
        with TemporaryDirectory() as root:
            missing = Path(root) / "no-such.sqlite3"
            err = io.StringIO()
            with redirect_stderr(err):
                rc = cli.main(["report", "--db", str(missing)])
            self.assertEqual(rc, 1)


class MigrateUsageTest(unittest.TestCase):
    """用法错误 = 退出码 2。"""

    def test_apply_without_yes_is_usage_error(self):
        with TemporaryDirectory() as root:
            db_path = _build_db(root)
            err = io.StringIO()
            with redirect_stderr(err):
                rc = cli.main(["apply", "--db", str(db_path)])
            self.assertEqual(rc, 2)
            self.assertIn("--yes", err.getvalue())
            self.assertEqual(_read_audit_rows(db_path), [])

    def test_module_without_arguments_exits_2(self):
        proc = subprocess.run(
            [sys.executable, "-m", "local_app.oneoff.migrate_legacy_permissions"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(proc.returncode, 2)


if __name__ == "__main__":
    unittest.main()
