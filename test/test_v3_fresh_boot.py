"""Access V3 空库首启引导（v3_bootstrap）合同测试。

背景：v3 切换靠一次性 personnel_access_migration 换库，
「空库首启」路径从未更新——init_db 只建 schema.sql 旧形状(users 缺 is_system_admin)，
首启管理员按旧形状写入,登录必 500。本测试锁定首启引导的三条合同：

1. 空库 → 重建为 v3 目标结构 + init_db 列差分补齐 legacy 扩展列(=生产库混合形态)，
   首启管理员落 v3 形状(independent_admin/is_system_admin)，v3 会话认证直接可登录；
2. 已是 v3 的库 → 幂等不动(生产每次启动都经过这里)；
3. 有数据却缺 v3 列的旧库 → 响亮报错指向 personnel_access_migration，
   绝不静默重建(数据损毁红线)。
"""
from __future__ import annotations

import secrets
import sqlite3
import tempfile
import unittest
from pathlib import Path

from local_app.auth import create_user, ensure_seed_admin
from local_app.db import init_db
from local_app.migrations import apply_migrations
from local_app.v3_bootstrap import (
    LegacyDatabaseNotMigratedError,
    ensure_v3_personnel_schema,
)


def _columns(db_path, table):
    conn = sqlite3.connect(str(db_path))
    try:
        return {row[1] for row in conn.execute(f"pragma table_info({table})")}
    finally:
        conn.close()


def _row_count(db_path, table):
    conn = sqlite3.connect(str(db_path))
    try:
        return conn.execute(f"select count(*) from {table}").fetchone()[0]
    finally:
        conn.close()


class FreshEmptyDatabaseBootTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "clinic.sqlite3"

    def tearDown(self):
        self.tmp.cleanup()

    def test_fresh_empty_db_converges_to_v3_hybrid_and_admin_can_login(self):
        init_db(self.db_path)
        ensure_v3_personnel_schema(str(self.db_path))
        apply_migrations(self.db_path)
        seeded = ensure_seed_admin(str(self.db_path))
        self.assertIsNotNone(seeded)

        # 四表 = v3 基座 + legacy 扩展列(与生产库一次性迁移后的混合形态一致)
        self.assertTrue(
            {"is_system_admin", "account_kind", "role"}
            <= _columns(self.db_path, "users")
        )
        self.assertTrue(
            {"employment_status", "role", "active", "roles"}
            <= _columns(self.db_path, "staff_members")
        )
        self.assertTrue(
            {"session_id", "token_hash", "token"}
            <= _columns(self.db_path, "sessions")
        )
        for extra_table in ("staff_role_assignments", "role_data_scopes"):
            self.assertIn(extra_table, "staff_role_assignments role_data_scopes")

        # 首启管理员 = v3 独立系统管理员(破局账号),不是旧形状 staff 账号
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute(
                "select username, role, staff_id, is_system_admin, account_kind "
                "from users where username = 'admin'"
            ).fetchone()
        finally:
            conn.close()
        self.assertEqual(row["account_kind"], "independent_admin")
        self.assertEqual(row["is_system_admin"], 1)
        self.assertIsNone(row["staff_id"])

        # v3 会话认证:用首启密码直接登录成功(此前踩过的 500 不允许再现)
        from local_app import access_session_service as session_service

        token, context = session_service.authenticate_and_create_session(
            str(self.db_path),
            username=seeded[0],
            password=seeded[1],
            device_name="smoke",
            user_agent="smoke",
            ip_address="127.0.0.1",
            request_id=secrets.token_hex(8),
        )
        self.assertTrue(token)
        self.assertEqual(context["user"]["username"], "admin")

        # 幂等:第二次启动(模拟重启)不动结构不动数据
        ensure_v3_personnel_schema(str(self.db_path))
        self.assertEqual(_row_count(self.db_path, "users"), 1)


class SeedAdminPasswordEnvTest(unittest.TestCase):
    def test_env_preset_initial_password_is_used(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "clinic.sqlite3"
            init_db(db_path)
            ensure_v3_personnel_schema(str(db_path))
            import os
            from unittest import mock

            with mock.patch.dict(os.environ, {"DENTAL_ADMIN_PASSWORD": "123456"}):
                seeded = ensure_seed_admin(str(db_path))
            self.assertEqual(seeded, ("admin", "123456"))

            # 预设密码直接可通过 v3 会话认证
            from local_app import access_session_service as session_service

            token, context = session_service.authenticate_and_create_session(
                str(db_path),
                username="admin",
                password="123456",
                device_name="t",
                user_agent="t",
                ip_address="127.0.0.1",
                request_id=secrets.token_hex(8),
            )
            self.assertTrue(token)
            self.assertEqual(context["user"]["username"], "admin")


class AlreadyV3DatabaseTest(unittest.TestCase):
    def test_noop_when_users_already_has_v3_columns(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "clinic.sqlite3"
            init_db(db_path)
            ensure_v3_personnel_schema(str(db_path))
            ensure_seed_admin(str(db_path))
            # 模拟生产库:已有 v3 结构且有数据 → 再跑引导必须是纯 no-op
            ensure_v3_personnel_schema(str(db_path))
            self.assertEqual(_row_count(db_path, "users"), 1)


class LegacyPopulatedDatabaseTest(unittest.TestCase):
    def test_populated_legacy_db_fails_loud_and_untouched(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "clinic.sqlite3"
            init_db(db_path)
            conn = sqlite3.connect(str(db_path))
            conn.row_factory = sqlite3.Row
            try:
                conn.execute(
                    "insert into staff_members(staff_id, name, role, active) "
                    "values ('legacy-staff-1', '合成旧员工', '医生', 1)"
                )
                create_user(conn, "legacy-admin", "旧管理员", "pw-123456")
                conn.commit()
            finally:
                conn.close()

            with self.assertRaises(LegacyDatabaseNotMigratedError) as raised:
                ensure_v3_personnel_schema(str(db_path))
            self.assertIn("personnel_access_migration", str(raised.exception))

            # 数据一行未动
            self.assertEqual(_row_count(db_path, "users"), 1)
            self.assertEqual(_row_count(db_path, "staff_members"), 1)


if __name__ == "__main__":
    unittest.main()
