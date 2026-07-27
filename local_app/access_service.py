"""普通有效权限纯计算（人员权限中心 v3 · Task 2A）。

只读给定 connection：users.staff_id -> staff_role_assignments.role_key ->
role_permissions.perm_key 的并集，过滤到 access_policy.ALL_PERMISSIONS，
不携带任何总钥匙/通配语义。不依赖 FastAPI，不连接 DEFAULT_DB_PATH，不 commit。
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

from local_app.access_policy import (
    ALL_PERMISSIONS,
    NON_ASSIGNABLE_PERMISSIONS,
    PERMISSION_DEPENDENCIES,
)
from local_app.timeutil import BJ, bj_now

_LEGACY_ADMIN_ROLE = "admin"


def effective_permissions(conn, user_id):
    """返回某用户的普通业务有效权限并集（frozenset[str]）。"""
    user_row = conn.execute(
        "select staff_id, is_active, account_kind from users where id = ?", (user_id,)
    ).fetchone()
    if (not user_row or not user_row["is_active"] or not user_row["staff_id"]
            or user_row["account_kind"] != "staff"):
        return frozenset()

    staff_row = conn.execute(
        "select employment_status from staff_members where staff_id = ?",
        (user_row["staff_id"],),
    ).fetchone()
    if not staff_row or staff_row["employment_status"] != "employed":
        return frozenset()

    rows = conn.execute(
        "select distinct rp.perm_key "
        "from staff_role_assignments sra "
        "join role_permissions rp on rp.role_key = sra.role_key "
        "where sra.staff_id = ? and sra.role_key != ?",
        (user_row["staff_id"], _LEGACY_ADMIN_ROLE),
    ).fetchall()
    return frozenset(row["perm_key"] for row in rows if row["perm_key"] in ALL_PERMISSIONS)


# === 系统管理员授予/撤销（Task 2B.1，单事务，BEGIN IMMEDIATE 串行化）===
# 只切换 users.is_system_admin（account_kind='staff' 目标），一事务内更新+写审计，
# 失败必回滚。独立管理员/break-glass 账号不能通过本接口升降级。
# 并发撤权由 BEGIN IMMEDIATE 拿写锁天然串行化：后到的事务在先到事务提交后才
# 读到最新状态，据此稳定判定 actor_not_authorized / admin_floor_violation
# （见 Task 2B.2 test/test_system_admin.py::ConcurrentSystemAdminTests）。


class AccessServiceError(Exception):
    """稳定领域异常：str()/repr() 只暴露 .code，不带 SQL/路径/用户名/reason。"""

    def __init__(self, code):
        self.code = code
        super().__init__(code)

    def __str__(self):
        return self.code

    def __repr__(self):
        return "AccessServiceError(%r)" % (self.code,)


_REAUTH_WINDOW_SECONDS = 300


def _validate_reason(reason):
    """只认内建 str：子类可重写 strip() 抛任意异常或返回假值，一律 reason_invalid。"""
    if type(reason) is not str:
        raise AccessServiceError("reason_invalid")
    trimmed = reason.strip()
    if not trimmed:
        raise AccessServiceError("reason_required")
    return trimmed


def _to_utc(value):
    """把 aware datetime 安全归一化到 UTC；只认内建 datetime（子类可重写 astimezone
    返回自身，让后续相减/格式化抛原生异常），tzinfo 是敌意实现（utcoffset 抛任意异常
    或返回非 timedelta）时统一返回 None，不让原生异常外泄。

    offset 只问一次并冻结：astimezone() 会二次调用源 tzinfo，每次换答案的敌意
    tzinfo 能借此把过期 reauth 的墙钟洗成新鲜时刻（见 test_role_permission_service
    ::test_flipping_tzinfo_cannot_refresh_stale_reauth）。fold 语义由这一次
    utcoffset() 自带，合法 ZoneInfo 结果与 astimezone() 一致。"""
    try:
        if type(value) is not datetime:
            return None
        offset = value.utcoffset()
        if type(offset) is not timedelta:
            return None
        utc = (value.replace(tzinfo=None) - offset).replace(tzinfo=timezone.utc)
    except Exception:
        return None
    # 归一化结果必须自己就是内建 aware UTC datetime，后续相减/格式化才安全。
    if type(utc) is not datetime or utc.tzinfo is not timezone.utc:
        return None
    return utc


def _validate_now(now):
    if now is None:
        now = bj_now()
    utc_now = _to_utc(now)
    if utc_now is None:
        raise AccessServiceError("now_invalid")
    return utc_now


def _validate_reauth(reauthenticated_at, now_utc):
    reauth_utc = _to_utc(reauthenticated_at)
    if reauth_utc is None:
        raise AccessServiceError("reauth_invalid")
    # aware datetime 直接相减在 DST fold 附近会用错 utcoffset（同 tzinfo 不同 fold
    # 时墙钟差和真实 UTC 差可以差出一小时），先各自转 UTC 再相减才是真实时间差。
    delta = (now_utc - reauth_utc).total_seconds()
    if delta < 0 or delta > _REAUTH_WINDOW_SECONDS:
        raise AccessServiceError("reauth_expired")


def _format_bj(now):
    return now.astimezone(BJ).strftime("%Y-%m-%d %H:%M:%S")


def _safe_rollback(conn):
    try:
        conn.execute("rollback")
    except sqlite3.Error:
        pass


def _safe_close(conn):
    try:
        conn.close()
    except sqlite3.Error:
        pass


def _open_existing(db_path):
    """只读打开已存在的库文件，绝不隐式创建（no-create）。"""
    path_invalid = False
    exists = False
    try:
        path = Path(db_path)
        exists = path.is_file()
    except Exception:
        path_invalid = True
    if path_invalid or not exists:
        raise AccessServiceError("database_unavailable")
    uri = "file:%s?mode=rw" % quote(str(path))
    conn = sqlite3.connect(uri, uri=True)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("pragma foreign_keys = on")
        conn.execute("pragma busy_timeout = 5000")
    except sqlite3.Error:
        _safe_close(conn)
        raise
    return conn


def _is_int_one(value):
    """只认真正的整数 1：float 1.0 / bool True / 自定义等值对象一律不算。"""
    return type(value) is int and value == 1


def _load_actor(conn, actor_user_id):
    row = conn.execute(
        "select id, username, is_active, is_system_admin from users where id = ?",
        (actor_user_id,),
    ).fetchone()
    if row is None or not _is_int_one(row["is_active"]) or not _is_int_one(row["is_system_admin"]):
        raise AccessServiceError("actor_not_authorized")
    return row


def _load_target(conn, target_user_id):
    row = conn.execute(
        "select id, is_active, is_system_admin, account_kind from users where id = ?",
        (target_user_id,),
    ).fetchone()
    if row is None:
        raise AccessServiceError("target_not_found")
    if row["account_kind"] != "staff":
        raise AccessServiceError("target_not_eligible")
    return row


def _regular_admin_floor_ok(conn):
    row = conn.execute(
        "select count(*) as n from users "
        "where is_active = 1 and is_system_admin = 1 and account_kind != 'break_glass'"
    ).fetchone()
    return row["n"] >= 1


def _write_admin_change_audit(conn, *, action, actor_row, target_user_id, reason,
                               now_str, old_value, new_value):
    conn.execute(
        "insert into audit_logs(entity_type, entity_id, action, old_json, new_json, "
        "operator, actor_user_id, result, reason, risk_level, created_at) "
        "values ('user', ?, ?, ?, ?, ?, ?, 'success', ?, 'high', ?)",
        (
            target_user_id,
            action,
            json.dumps({"is_system_admin": bool(old_value)}),
            json.dumps({"is_system_admin": bool(new_value)}),
            actor_row["username"],
            actor_row["id"],
            reason,
            now_str,
        ),
    )


def _change_system_admin(db_path, *, action, actor_user_id, target_user_id, reason,
                          reauthenticated_at, now):
    now = _validate_now(now)
    reason = _validate_reason(reason)
    _validate_reauth(reauthenticated_at, now)

    conn = None
    sqlite_failed = False
    try:
        conn = _open_existing(db_path)
        conn.isolation_level = None
        conn.execute("begin immediate")
        try:
            actor_row = _load_actor(conn, actor_user_id)
            target_row = _load_target(conn, target_user_id)

            if action == "grant":
                if target_row["is_active"] != 1:
                    raise AccessServiceError("target_inactive")
                if target_row["is_system_admin"] == 1:
                    raise AccessServiceError("target_already_admin")
                new_value = 1
            else:
                if target_user_id == actor_user_id:
                    raise AccessServiceError("cannot_revoke_self")
                if target_row["is_system_admin"] != 1:
                    raise AccessServiceError("target_not_admin")
                new_value = 0

            now_str = _format_bj(now)
            conn.execute(
                "update users set is_system_admin = ?, updated_at = ? where id = ?",
                (new_value, now_str, target_user_id),
            )

            if action == "revoke" and not _regular_admin_floor_ok(conn):
                raise AccessServiceError("admin_floor_violation")

            _write_admin_change_audit(
                conn,
                action="system_admin.%s" % action,
                actor_row=actor_row,
                target_user_id=target_user_id,
                reason=reason,
                now_str=now_str,
                old_value=target_row["is_system_admin"],
                new_value=new_value,
            )
        except AccessServiceError:
            _safe_rollback(conn)
            raise
        conn.execute("commit")
    except sqlite3.Error:
        sqlite_failed = True
        if conn is not None:
            _safe_rollback(conn)
    finally:
        if conn is not None:
            _safe_close(conn)
    if sqlite_failed:
        raise AccessServiceError("transaction_failed")


def grant_system_admin(db_path, *, actor_user_id, target_user_id, reason,
                        reauthenticated_at, now=None):
    """把 account_kind='staff' 的目标账号提升为系统管理员，单事务含审计。"""
    _change_system_admin(
        db_path, action="grant", actor_user_id=actor_user_id,
        target_user_id=target_user_id, reason=reason,
        reauthenticated_at=reauthenticated_at, now=now,
    )


def revoke_system_admin(db_path, *, actor_user_id, target_user_id, reason,
                         reauthenticated_at, now=None):
    """撤销 account_kind='staff' 目标账号的系统管理员标记，单事务含审计。"""
    _change_system_admin(
        db_path, action="revoke", actor_user_id=actor_user_id,
        target_user_id=target_user_id, reason=reason,
        reauthenticated_at=reauthenticated_at, now=now,
    )


# === 角色权限原子替换（Task 2C.1，单事务，BEGIN IMMEDIATE）===
# 精确替换某角色的普通权限全集：整单成功或整单失败，不部分写入、不静默过滤。


def _validate_permissions(permissions):
    """校验并返回请求权限的 frozenset。只认内建 list/tuple + 内建 str 元素。"""
    if type(permissions) not in (list, tuple):
        raise AccessServiceError("permissions_invalid")
    for perm in permissions:
        if type(perm) is not str:
            raise AccessServiceError("permission_invalid")
    if len(set(permissions)) != len(permissions):
        raise AccessServiceError("permission_duplicate")
    for perm in permissions:
        if perm in NON_ASSIGNABLE_PERMISSIONS:
            raise AccessServiceError("permission_not_assignable")
        if perm not in ALL_PERMISSIONS:
            raise AccessServiceError("permission_unknown")
    return frozenset(permissions)


def _validate_dependencies(permissions):
    """校验最终集合自带完整传递依赖闭包，缺任何一层都整单拒绝。"""
    for perm in permissions:
        pending = list(PERMISSION_DEPENDENCIES.get(perm, ()))
        seen = set()
        while pending:
            dep = pending.pop()
            if dep in seen:
                continue
            seen.add(dep)
            if dep not in permissions:
                raise AccessServiceError("permission_dependency_missing")
            pending.extend(PERMISSION_DEPENDENCIES.get(dep, ()))


def _validate_role_key(role_key):
    """只认内建 str 且非空白的稳定角色键。str 子类可重写 __eq__ 让 `role_key ==
    'admin'` 恒假，而 SQLite 仍按底层文本绑定，据此绕过遗留 admin 角色保护；
    非法输入一律 role_invalid，不静默 trim、不回显敌意 repr。"""
    if type(role_key) is not str or not role_key.strip():
        raise AccessServiceError("role_invalid")


def _load_role_permissions(conn, role_key):
    """读取角色现有权限；发现目录外脏值直接 fail closed，不洗掉后继续。"""
    rows = conn.execute(
        "select perm_key from role_permissions where role_key = ?", (role_key,)
    ).fetchall()
    old = frozenset(row["perm_key"] for row in rows)
    if not old <= ALL_PERMISSIONS:
        raise AccessServiceError("role_permissions_corrupt")
    return old


def replace_role_permissions(db_path, *, actor_user_id, role_key, permissions,
                             expected_permissions, reason, reauthenticated_at,
                             now=None):
    """把 role_key 的普通权限全集精确替换为 permissions，单事务含高风险审计。

    expected_permissions 是调用方读到的服务器基线；在写锁内与当前集合精确比较。
    它只校验权限目录，不要求依赖闭包。

    返回 dict：role_key / old / new / added / removed / affected_staff_count / changed
    （权限列表均为稳定排序的 list，可直接 json 序列化）。
    """
    now = _validate_now(now)
    reason = _validate_reason(reason)
    _validate_reauth(reauthenticated_at, now)
    new_set = _validate_permissions(permissions)
    expected_set = _validate_permissions(expected_permissions)
    _validate_dependencies(new_set)

    conn = None
    sqlite_failed = False
    result = None
    try:
        conn = _open_existing(db_path)
        conn.isolation_level = None
        conn.execute("begin immediate")
        try:
            actor_row = _load_actor(conn, actor_user_id)
            _validate_role_key(role_key)
            if role_key == _LEGACY_ADMIN_ROLE:
                raise AccessServiceError("role_not_configurable")
            if conn.execute(
                "select 1 from roles where role_key = ?", (role_key,)
            ).fetchone() is None:
                raise AccessServiceError("role_not_found")

            old_set = _load_role_permissions(conn, role_key)
            if old_set != expected_set:
                raise AccessServiceError("role_permissions_stale")
            added = sorted(new_set - old_set)
            removed = sorted(old_set - new_set)
            changed = bool(added or removed)

            affected = conn.execute(
                "select count(distinct sra.staff_id) as n "
                "from staff_role_assignments sra "
                "join staff_members sm on sm.staff_id = sra.staff_id "
                "where sra.role_key = ? and sm.employment_status = 'employed'",
                (role_key,),
            ).fetchone()["n"]

            result = {
                "role_key": role_key,
                "old": sorted(old_set),
                "new": sorted(new_set),
                "added": added,
                "removed": removed,
                "affected_staff_count": affected,
                "changed": changed,
            }

            if changed:
                conn.execute(
                    "delete from role_permissions where role_key = ?", (role_key,)
                )
                for perm in result["new"]:
                    conn.execute(
                        "insert into role_permissions(role_key, perm_key) values (?, ?)",
                        (role_key, perm),
                    )
                conn.execute(
                    "insert into audit_logs(entity_type, entity_id, action, old_json, "
                    "new_json, operator, actor_user_id, result, reason, risk_level, "
                    "created_at) values ('role', ?, 'role_permissions.replace', ?, ?, ?, "
                    "?, 'success', ?, 'high', ?)",
                    (
                        role_key,
                        json.dumps({"permissions": result["old"]}),
                        json.dumps({
                            "permissions": result["new"],
                            "added": added,
                            "removed": removed,
                            "affected_staff_count": affected,
                        }),
                        actor_row["username"],
                        actor_row["id"],
                        reason,
                        _format_bj(now),
                    ),
                )
        except AccessServiceError:
            _safe_rollback(conn)
            raise
        conn.execute("commit")
    except sqlite3.Error:
        sqlite_failed = True
        if conn is not None:
            _safe_rollback(conn)
    finally:
        if conn is not None:
            _safe_close(conn)
    if sqlite_failed:
        raise AccessServiceError("transaction_failed")
    return result
