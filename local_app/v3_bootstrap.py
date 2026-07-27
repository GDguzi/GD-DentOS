"""Access V3 空库首启引导。

v3 人事权限（2026-07-17 开闸）靠一次性 `personnel_access_migration` 把生产库换库迁移；
「空库首启」没有旧数据可迁，正确形态是把人事权限四表直接建成 v3 目标结构，
再靠 `init_db` 列差分补齐 legacy 扩展列——与生产库经迁移收敛出的混合形态逐列一致。

判定与红线：
- `users` 已有 `is_system_admin` → 已是 v3：纯 no-op（生产每次启动都经过这里）；
- 四张人事表（users/staff_members/sessions/audit_logs）全空且缺 v3 列 → 首启空库，重建；
- 有数据却缺 v3 列 → 未迁移旧库：抛 `LegacyDatabaseNotMigratedError` 指向
  `personnel_access_migration`，绝不静默重建（数据损毁红线 / 禁止兜底）。

调用方：`run_local` 启动序列（init_db 之后、apply_migrations 之前）、`demo/seed_demo`。
"""
from __future__ import annotations

import sqlite3

from local_app.db import init_db
from local_app.personnel_access_migration import load_personnel_access_schema_sql

_PERSONNEL_TABLES = ("users", "staff_members", "sessions", "audit_logs")
_V3_MARKER_COLUMN = "is_system_admin"
# 四表各自的 v3 特征列：只看 users 会误判——若重建在建完 users 后中断,
# 下次启动 init_db 会把 sessions/audit_logs 补成 legacy 形态,而 users 的标记
# 让引导直接 return False,认证就跑在半截结构上。
_V3_SHAPE_MARKERS = {
    "users": "is_system_admin",
    "staff_members": "employment_status",
    "sessions": "idle_expires_at",
    "audit_logs": "actor_user_id",
}
# 目标 DDL 实际拥有的全部表：比四张人事表多出 staff_role_assignments / role_data_scopes,
# 且它们的 create 不带 if not exists。旧版两段 executescript 若在建出这两张后中断,
# 只 drop 四张就会让重建撞 "table already exists" 而永久起不来。
_V3_OWNED_TABLES = _PERSONNEL_TABLES + ("staff_role_assignments", "role_data_scopes")


class LegacyDatabaseNotMigratedError(RuntimeError):
    """旧库有数据却缺 v3 人事结构：必须先跑 personnel_access_migration 换库迁移。"""


def _table_columns(conn, table):
    return {row[1] for row in conn.execute(f"pragma table_info({table})")}


def ensure_v3_personnel_schema(db_path):
    """空库把人事权限四表建成 v3 混合形态；已是 v3 的库纯 no-op。返回是否执行了重建。"""
    conn = sqlite3.connect(str(db_path))
    try:
        if all(marker in _table_columns(conn, table)
               for table, marker in _V3_SHAPE_MARKERS.items()):
            return False
        populated = [
            table
            for table in _V3_OWNED_TABLES
            if _table_columns(conn, table)
            and conn.execute(f"select count(*) from {table}").fetchone()[0] > 0
        ]
        if populated:
            raise LegacyDatabaseNotMigratedError(
                "数据库是 v3 之前的旧结构且已有数据"
                f"（{', '.join(populated)}），不能自动重建："
                "请先用 local_app/personnel_access_migration.py 完成换库迁移，"
                "再用当前版本启动。"
            )
        conn.execute("pragma foreign_keys = off")
        # 单事务重建:executescript 会先隐式提交,drop 与 create 分两批跑时
        # 中途失败会把「已 drop 未重建」永久留在库里。显式 begin/commit 包成一笔,
        # 失败时 close() 回滚,库退回重建前形态。
        conn.executescript(
            "begin;"
            + "".join(f"drop table if exists {table};" for table in _V3_OWNED_TABLES)
            + load_personnel_access_schema_sql()
            + ";commit;"
        )
    finally:
        conn.close()
    # v3 基座就位后，用 init_db 列差分把 schema.sql 的 legacy 扩展列
    # （users.role、staff_members.role/active/roles、sessions.token 等）补齐，
    # 结果与生产库经一次性迁移 + 启动差分收敛出的混合形态逐列一致。
    init_db(db_path)
    return True
