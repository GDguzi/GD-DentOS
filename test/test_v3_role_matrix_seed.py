"""v3 空库角色权限矩阵种子合同。

背景：v3 矩阵接口(/api/role-permissions)只收 99 新键并强校验依赖闭包；
而角色种子(0001)写的是旧 35 键默认集——旧键里 lab_order.manage 没有配套
lab_order.view，新依赖图里 manage 依赖 view → 前端 normalize 整页抛错。
生产库因一次性 personnel_access_migration 显式赋新键而免疫，空库全中招。

合同：
1. 空库走完整启动序列后，每个预置角色的权限集 = 旧→新映射(含依赖闭包)的结果，
   且全部落在 99 新键目录内（页面加载不再崩）。
2. 已被自定义过的角色（含生产库已迁新键的）一律不动，幂等。
3. 非 v3 库（旧栈）不动。
"""
from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from local_app.access_policy import ALL_PERMISSIONS
from local_app.auth import DEFAULT_ROLE_PERMS
from local_app.db import init_db
from local_app.migrations import apply_migrations, ensure_v3_role_permission_matrix
from local_app.oneoff.legacy_permission_mapping import (
    compute_new_permissions,
    dependency_closure,
)
from local_app.v3_bootstrap import ensure_v3_personnel_schema


def _role_perms(db_path, role):
    conn = sqlite3.connect(str(db_path))
    try:
        return {
            row[0]
            for row in conn.execute(
                "select perm_key from role_permissions where role_key = ?", (role,)
            )
        }
    finally:
        conn.close()


class FreshV3RoleMatrixSeedTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "clinic.sqlite3"
        init_db(self.db_path)
        ensure_v3_personnel_schema(str(self.db_path))
        apply_migrations(self.db_path)

    def tearDown(self):
        self.tmp.cleanup()

    def test_preset_roles_seeded_with_new_keys_and_dependency_closure(self):
        for role, legacy_defaults in DEFAULT_ROLE_PERMS.items():
            with self.subTest(role=role):
                got = _role_perms(self.db_path, role)
                self.assertTrue(got, f"{role} 权限集不应为空")
                self.assertLessEqual(got, ALL_PERMISSIONS, f"{role} 含目录外旧键")
                self.assertEqual(
                    got,
                    dependency_closure(got),
                    f"{role} 依赖闭包不自洽(页面会崩): "
                    f"缺 {sorted(dependency_closure(got) - got)}",
                )
                self.assertEqual(
                    got,
                    set(
                        compute_new_permissions(role, frozenset(legacy_defaults))
                    ),
                    f"{role} 应等于旧→新映射结果",
                )

    def test_customized_roles_are_never_touched_and_idempotent(self):
        # 模拟用户在矩阵里改过 doctor(从映射结果里删一个键)——不再等于旧默认集
        converted = sorted(
            compute_new_permissions("doctor", frozenset(DEFAULT_ROLE_PERMS["doctor"]))
        )
        victim = converted[0]
        conn = sqlite3.connect(str(self.db_path))
        try:
            cur = conn.execute(
                "delete from role_permissions where role_key='doctor' and perm_key=?",
                (victim,),
            )
            assert cur.rowcount == 1, f"{victim} 应在 doctor 映射结果里"
            conn.commit()
        finally:
            conn.close()
        before = _role_perms(self.db_path, "doctor")
        changed = ensure_v3_role_permission_matrix(str(self.db_path))
        self.assertEqual(changed, 0)
        self.assertEqual(_role_perms(self.db_path, "doctor"), before)
        # 其余角色已在新键态,再跑也一律不动
        self.assertEqual(changed, 0)


class LegacyStackRoleMatrixTest(unittest.TestCase):
    def test_non_v3_database_untouched(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "clinic.sqlite3"
            init_db(db_path)
            apply_migrations(db_path)
            before = _role_perms(db_path, "doctor")
            changed = ensure_v3_role_permission_matrix(str(db_path))
            self.assertEqual(changed, 0)
            self.assertEqual(_role_perms(db_path, "doctor"), before)


if __name__ == "__main__":
    unittest.main()
