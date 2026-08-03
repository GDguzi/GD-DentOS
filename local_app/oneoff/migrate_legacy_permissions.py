"""旧 35 权限键 → 新 99 权限键一次性迁移 CLI（映射真相源 = docs/权限映射_旧到新_2026-07-18.md）。

用法（仓库根执行）：
  python3 -m local_app.oneoff.migrate_legacy_permissions report --db PATH
  python3 -m local_app.oneoff.migrate_legacy_permissions apply --db PATH --yes

退出码语义：0=成功，1=数据问题（缺表/库打不开/含目录外脏键），2=用法错误。
apply 在单事务 BEGIN IMMEDIATE 内逐角色精确替换 role_permissions 并逐角色写审计；
发现目录外脏键整单拒绝、零写入；对已迁移库再跑幂等（每角色 changed=False、不写审计）。

隐私铁律：只输出权限键/角色键/计数，绝不输出员工姓名、用户名或任何患者数据。
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

from local_app.oneoff.legacy_permission_mapping import (
    LegacyPermissionMappingError,
    compute_new_permissions,
    find_unknown_keys,
)
from local_app.timeutil import now_str

MIGRATION_AUDIT_ACTION = "permission.migration.legacy_to_v3"
MIGRATION_AUDIT_OPERATOR = "legacy_permission_migration"
_LEGACY_ADMIN_ROLE = "admin"

_REQUIRED_TABLES = ("roles", "role_permissions", "staff_role_assignments", "audit_logs")

EXIT_CODE_LEGEND = "退出码语义: 0=成功 1=数据问题(缺表/库打不开/含目录外脏键) 2=用法错误"


class _DataProblem(Exception):
    """库打不开、缺必需表、含目录外脏键等数据问题；退出码 1。"""


def _open_db(db_path, mode):
    """以 URI mode=ro/rw 打开已存在的库；文件不存在或打不开一律数据问题，绝不静默建库。"""
    path = Path(db_path)
    if not path.is_file():
        raise _DataProblem(f"数据库文件不存在: {db_path}")
    uri = f"{path.resolve().as_uri()}?mode={mode}"
    try:
        conn = sqlite3.connect(uri, uri=True)
        conn.row_factory = sqlite3.Row
        conn.execute("select count(*) from sqlite_master").fetchone()
    except sqlite3.Error:
        raise _DataProblem(f"无法打开数据库: {db_path}") from None
    return conn


def _table_columns(conn, table):
    return {row["name"] for row in conn.execute(f"pragma table_info({table})")}


def _require_tables(conn):
    existing = {
        row["name"]
        for row in conn.execute("select name from sqlite_master where type='table'")
    }
    missing = [table for table in _REQUIRED_TABLES if table not in existing]
    if missing:
        raise _DataProblem(f"数据库缺少必需表: {', '.join(missing)}")


def _load_role_keys(conn):
    return [
        row["role_key"]
        for row in conn.execute("select role_key from roles order by role_key")
    ]


def _load_all_role_permissions(conn):
    perms = {}
    for row in conn.execute("select role_key, perm_key from role_permissions"):
        perms.setdefault(row["role_key"], set()).add(row["perm_key"])
    return perms


def _affected_staff_counts(conn):
    """逐角色受影响员工数：staff_role_assignments 去重计数，不读任何姓名。"""
    return {
        row["role_key"]: row["n"]
        for row in conn.execute(
            "select role_key, count(distinct staff_id) as n "
            "from staff_role_assignments group by role_key"
        )
    }


def _cmd_report(db_path):
    conn = _open_db(db_path, "ro")
    try:
        _require_tables(conn)
        role_keys = _load_role_keys(conn)
        all_perms = _load_all_role_permissions(conn)
        affected = _affected_staff_counts(conn)
    finally:
        conn.close()

    unknown = find_unknown_keys(all_perms)

    print("旧 35 权限键 → 新 99 权限键迁移报告（只读，不写库）")
    for role_key in role_keys:
        if role_key == _LEGACY_ADMIN_ROLE:
            print(f"角色 {role_key}: 跳过（admin 不入表，代码层全通过）")
            continue
        old = frozenset(all_perms.get(role_key, ()))
        try:
            mapped = compute_new_permissions(role_key, old, with_closure=False)
            new = compute_new_permissions(role_key, old)
        except LegacyPermissionMappingError as exc:
            print(f"角色 {role_key}: 无法计算迁移结果（{exc}）")
            continue
        added = sorted(new - old)
        removed = sorted(old - new)
        kept = sorted(old & new)
        dep_added = sorted(new - mapped)
        changed = bool(added or removed)
        print(
            f"角色 {role_key}: changed={changed} "
            f"受影响员工数={affected.get(role_key, 0)}"
        )
        print(f"  旧键({len(old)}): {', '.join(sorted(old)) or '(空)'}")
        print(f"  新增({len(added)}): {', '.join(added) or '(无)'}")
        print(f"  移除({len(removed)}): {', '.join(removed) or '(无)'}")
        print(f"  保留({len(kept)}): {', '.join(kept) or '(无)'}")
        print(f"  依赖补入({len(dep_added)}): {', '.join(dep_added) or '(无)'}")
    if unknown:
        print("⚠ 未知脏键（既不在旧 35 键也不在新 99 目录，fail-closed 不静默丢弃）:")
        for role_key in sorted(unknown):
            print(f"  {role_key}: {', '.join(sorted(unknown[role_key]))}")
        print("存在脏键：apply 将整单拒绝、零写入。")
    print(EXIT_CODE_LEGEND)
    return 1 if unknown else 0


def _write_migration_audit(conn, role_key, old_sorted, new_sorted):
    """按现有 audit_logs 表结构对齐列写一行迁移审计（只写存在的列）。"""
    record = {
        "entity_type": "role",
        "entity_id": role_key,
        "action": MIGRATION_AUDIT_ACTION,
        "old_json": json.dumps(old_sorted),
        "new_json": json.dumps(new_sorted),
        "operator": MIGRATION_AUDIT_OPERATOR,
        "created_at": now_str(),
        # 扩展列（personnel_access_schema.sql 目标结构）存在才写。
        "result": "success",
        "risk_level": "high",
        "reason": "旧 35 权限键 → 新 99 权限键一次性迁移",
    }
    present = [col for col in record if col in _table_columns(conn, "audit_logs")]
    conn.execute(
        "insert into audit_logs({}) values ({})".format(
            ", ".join(present), ", ".join("?" for _ in present)
        ),
        [record[col] for col in present],
    )


def _cmd_apply(db_path):
    conn = _open_db(db_path, "rw")
    results = []
    try:
        _require_tables(conn)
        conn.isolation_level = None
        conn.execute("begin immediate")
        try:
            role_keys = _load_role_keys(conn)
            all_perms = _load_all_role_permissions(conn)
            unknown = find_unknown_keys(all_perms)
            if unknown:
                detail = "; ".join(
                    f"{role_key}({', '.join(sorted(keys))})"
                    for role_key, keys in sorted(unknown.items())
                )
                raise _DataProblem(f"存在目录外脏键，整单拒绝、零写入: {detail}")
            for role_key in role_keys:
                if role_key == _LEGACY_ADMIN_ROLE:
                    results.append((role_key, None))
                    continue
                old = frozenset(all_perms.get(role_key, ()))
                mapped = compute_new_permissions(role_key, old, with_closure=False)
                new = compute_new_permissions(role_key, old)
                dep_added = sorted(new - mapped)
                if old != new:
                    conn.execute(
                        "delete from role_permissions where role_key = ?", (role_key,)
                    )
                    for perm in sorted(new):
                        conn.execute(
                            "insert into role_permissions(role_key, perm_key) values (?, ?)",
                            (role_key, perm),
                        )
                    _write_migration_audit(conn, role_key, sorted(old), sorted(new))
                results.append((role_key, (old, new, dep_added)))
            conn.execute("commit")
        except Exception:
            if conn.in_transaction:
                conn.execute("rollback")
            raise
    finally:
        conn.close()

    print("旧 35 权限键 → 新 99 权限键迁移执行（单事务，已提交）")
    changed_count = 0
    migrated_roles = 0
    for role_key, payload in results:
        if payload is None:
            print(f"角色 {role_key}: 跳过（admin 不入表，代码层全通过）")
            continue
        old, new, dep_added = payload
        changed = old != new
        changed_count += changed
        migrated_roles += 1
        suffix = (
            f" 新增{len(new - old)} 移除{len(old - new)}" if changed else ""
        )
        if dep_added:
            suffix += f" 依赖补入{len(dep_added)}({', '.join(dep_added)})"
        print(f"角色 {role_key}: changed={changed} 旧{len(old)}键 → 新{len(new)}键{suffix}")
    print(f"汇总: 迁移角色 {migrated_roles} 个，写入 {changed_count} 个，审计 {changed_count} 行")
    print(EXIT_CODE_LEGEND)
    return 0


def _build_parser():
    parser = argparse.ArgumentParser(
        prog="python3 -m local_app.oneoff.migrate_legacy_permissions",
        description="旧 35 权限键 → 新 99 权限键一次性迁移"
        "（映射: docs/权限映射_旧到新_2026-07-18.md，D1-D9 拍板）",
        epilog=EXIT_CODE_LEGEND,
    )
    sub = parser.add_subparsers(dest="command", required=True)
    p_report = sub.add_parser(
        "report", help="只读报告：逐角色增删明细 + 脏键告警 + 受影响员工数"
    )
    p_report.add_argument("--db", required=True, help="数据库路径（URI mode=ro 只读打开）")
    p_apply = sub.add_parser(
        "apply", help="执行迁移：单事务精确替换 role_permissions，逐角色写审计"
    )
    p_apply.add_argument("--db", required=True, help="数据库路径（URI mode=rw 打开）")
    p_apply.add_argument("--yes", action="store_true", help="确认执行（缺省拒绝运行）")
    return parser


def main(argv=None):
    args = _build_parser().parse_args(argv)
    if args.command == "apply" and not args.yes:
        print(
            "用法: python3 -m local_app.oneoff.migrate_legacy_permissions "
            "apply --db PATH --yes",
            file=sys.stderr,
        )
        print("apply 会改写 role_permissions 并写审计，必须显式 --yes 确认。", file=sys.stderr)
        print(EXIT_CODE_LEGEND, file=sys.stderr)
        return 2
    handler = _cmd_report if args.command == "report" else _cmd_apply
    try:
        return handler(args.db)
    except _DataProblem as exc:
        print(f"数据问题: {exc}", file=sys.stderr)
        print(EXIT_CODE_LEGEND, file=sys.stderr)
        return 1
    except (sqlite3.Error, LegacyPermissionMappingError) as exc:
        print(f"数据问题: 迁移事务失败，已回滚零写入（{type(exc).__name__}）", file=sys.stderr)
        print(EXIT_CODE_LEGEND, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
