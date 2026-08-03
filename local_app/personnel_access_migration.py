"""Read-only, anonymized preflight report for legacy personnel/access tables.

Never opens the real default database; always requires an explicit
source_db_path pointing at a synthetic/backup copy.
"""

import os
import sqlite3
import tempfile
from pathlib import Path

from local_app.db import DEFAULT_DB_PATH

_GENERIC_ERROR = "无法以只读方式打开该数据库文件"

PERSONNEL_ACCESS_SCHEMA_SQL_PATH = Path(__file__).parent / "personnel_access_schema.sql"


def load_personnel_access_schema_sql():
    """Read-only load of the frozen personnel-access target schema DDL."""
    return PERSONNEL_ACCESS_SCHEMA_SQL_PATH.read_text(encoding="utf-8")

REQUIRED_COLUMNS = {
    "staff_members": ["staff_id", "name", "role", "roles", "active"],
    "users": ["id", "username", "password_hash", "role", "is_active", "staff_id"],
    "sessions": ["token", "user_id", "created_at", "expires_at"],
    "roles": ["role_key", "name"],
    "role_permissions": ["role_key", "perm_key"],
    "audit_logs": [
        "audit_id",
        "entity_type",
        "entity_id",
        "action",
        "old_json",
        "new_json",
        "operator",
        "created_at",
    ],
}

AUDIT_EXTENDED_FIELDS = [
    "actor_user_id",
    "result",
    "reason",
    "risk_level",
    "request_id",
    "ip_address",
    "user_agent",
]

_FIXED_STAFF_ROLE_TOKENS = {
    "管理员",
    "主任",
    "医生",
    "护士",
    "咨询师",
    "前台",
    "收银员",
    "助理",
    "助理2",
    "客服",
    "admin",
    "director",
    "doctor",
    "nurse",
    "consultant",
    "reception",
    "cashier",
    "assistant",
    "assistant2",
    "support",
}

_ALLOWED_READ_ACTIONS = {
    sqlite3.SQLITE_SELECT,
    sqlite3.SQLITE_READ,
    sqlite3.SQLITE_FUNCTION,
}

_ALLOWED_PRAGMAS = {"table_info"}

_LINKED_STAFF_ID = "TRIM(COALESCE(staff_id, '')) <> ''"
_UNLINKED_STAFF_ID = "TRIM(COALESCE(staff_id, '')) = ''"


class PreflightError(Exception):
    pass


_DECISION_ERROR_MESSAGES = {
    "decisions_shape": "迁移决策信封结构不合法",
    "admin_ids": "管理员用户 ID 列表不合法",
    "break_glass": "应急账号信息不合法",
    "staff_role_map": "员工角色映射不合法",
    "acknowledged_user_roles": "已确认的历史用户角色 ID 列表不合法",
    "status_resolutions": "在职状态冲突处理方案不合法",
    "source_path": "无法以只读方式打开迁移源数据库",
    "source_schema": "迁移源数据库结构不符合要求",
    "source_already_target": "迁移源数据库已是目标结构",
    "source_rows": "迁移源数据行不符合基础校验要求",
    "duplicate_username": "迁移源存在重复用户名",
    "duplicate_staff_account": "迁移源存在同一员工关联多个账号",
    "orphan_staff_link": "迁移源存在关联到不存在员工的账号",
    "unlinked_account": "存在未关联员工且未获授权保留的旧账号",
    "no_usable_admin": "迁移后不存在可用的系统管理员账号",
    "target_path": "无法使用该迁移目标数据库路径",
    "target_exists": "迁移目标数据库路径已存在",
    "source_target_same": "迁移目标不能与迁移源相同",
    "backup_failed": "创建迁移临时备份失败",
    "transform_failed": "迁移临时副本转换失败",
    "publish_failed": "发布迁移目标数据库失败",
    "cleanup_failed": "迁移发布前清理临时文件失败",
}

_DECISION_ENVELOPE_KEYS = {
    "regular_system_admin_user_ids",
    "break_glass_account",
    "staff_role_map",
    "acknowledged_legacy_user_role_user_ids",
    "status_conflict_resolutions",
}

_BREAK_GLASS_ACCOUNT_KEYS = {"id", "username", "display_name", "password_hash"}

_STATUS_CONFLICT_RESOLUTIONS = {"disable_user", "keep_disabled"}


class MigrationDecisionError(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(_DECISION_ERROR_MESSAGES.get(code, "迁移决策信封校验失败"))

    def __repr__(self):
        return f"MigrationDecisionError(code={self.code!r})"


def _is_valid_id_value(value):
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return True
    if isinstance(value, str):
        return value.strip() != ""
    return False


def _is_nonblank_str(value):
    return isinstance(value, str) and value.strip() != ""


def _validate_id_list(value, code):
    if not isinstance(value, list):
        raise MigrationDecisionError(code)
    seen = set()
    result = []
    for item in value:
        if not _is_valid_id_value(item):
            raise MigrationDecisionError(code)
        if item in seen:
            raise MigrationDecisionError(code)
        seen.add(item)
        result.append(item)
    return result


def _validate_decision_envelope(decisions):
    if not isinstance(decisions, dict) or set(decisions.keys()) != _DECISION_ENVELOPE_KEYS:
        raise MigrationDecisionError("decisions_shape")

    admin_ids = _validate_id_list(
        decisions["regular_system_admin_user_ids"], "admin_ids"
    )
    if len(admin_ids) < 1:
        raise MigrationDecisionError("admin_ids")

    break_glass = decisions["break_glass_account"]
    if not isinstance(break_glass, dict) or set(break_glass.keys()) != _BREAK_GLASS_ACCOUNT_KEYS:
        raise MigrationDecisionError("break_glass")
    normalized_break_glass = {}
    for key in _BREAK_GLASS_ACCOUNT_KEYS:
        val = break_glass[key]
        if not _is_nonblank_str(val):
            raise MigrationDecisionError("break_glass")
        normalized_break_glass[key] = val

    staff_role_map = decisions["staff_role_map"]
    if not isinstance(staff_role_map, dict):
        raise MigrationDecisionError("staff_role_map")
    normalized_staff_role_map = {}
    for key, val in staff_role_map.items():
        if not _is_nonblank_str(key) or not _is_nonblank_str(val):
            raise MigrationDecisionError("staff_role_map")
        normalized_staff_role_map[key] = val

    acknowledged_ids = _validate_id_list(
        decisions["acknowledged_legacy_user_role_user_ids"], "acknowledged_user_roles"
    )

    status_resolutions = decisions["status_conflict_resolutions"]
    if not isinstance(status_resolutions, dict):
        raise MigrationDecisionError("status_resolutions")
    normalized_status_resolutions = {}
    for key, val in status_resolutions.items():
        if not _is_valid_id_value(key) or val not in _STATUS_CONFLICT_RESOLUTIONS:
            raise MigrationDecisionError("status_resolutions")
        normalized_status_resolutions[key] = val

    return {
        "regular_system_admin_user_ids": admin_ids,
        "break_glass_account": normalized_break_glass,
        "staff_role_map": normalized_staff_role_map,
        "acknowledged_legacy_user_role_user_ids": acknowledged_ids,
        "status_conflict_resolutions": normalized_status_resolutions,
    }


def _staff_source_tokens(record):
    role = record["role"]
    if not _is_nonblank_str(role):
        raise MigrationDecisionError("staff_role_map")
    primary_token = role.strip()

    roles_field = record["roles"]
    if roles_field is not None and not isinstance(roles_field, str):
        raise MigrationDecisionError("staff_role_map")
    extra_tokens = [
        t.strip() for t in (roles_field or "").split(",") if t.strip()
    ]

    tokens = [primary_token] + [t for t in extra_tokens if t != primary_token]
    return primary_token, tokens


def _prepare_staff_role_assignments(normalized_source, normalized_decisions):
    staff_role_map = normalized_decisions["staff_role_map"]
    known_role_keys = normalized_source["roles"]

    staff_tokens_by_id = {}
    all_tokens = set()
    for staff_id, record in normalized_source["staff"].items():
        primary_token, tokens = _staff_source_tokens(record)
        staff_tokens_by_id[staff_id] = (primary_token, tokens)
        all_tokens.update(tokens)

    if set(staff_role_map.keys()) != all_tokens:
        raise MigrationDecisionError("staff_role_map")

    for target in staff_role_map.values():
        if target == "admin" or target not in known_role_keys:
            raise MigrationDecisionError("staff_role_map")

    assignments = []
    staff_role_keys = {}
    for staff_id in sorted(staff_tokens_by_id.keys()):
        primary_token, tokens = staff_tokens_by_id[staff_id]
        primary_role_key = staff_role_map[primary_token]

        target_role_keys = [primary_role_key]
        for token in tokens:
            role_key = staff_role_map[token]
            if role_key not in target_role_keys:
                target_role_keys.append(role_key)

        for role_key in target_role_keys:
            is_primary = 1 if role_key == primary_role_key else 0
            assignments.append((staff_id, role_key, is_primary, None))
        staff_role_keys[staff_id] = frozenset(target_role_keys)

    return {
        "assignments": assignments,
        "staff_role_keys": staff_role_keys,
        "source_token_map": dict(staff_role_map),
    }


def _prepare_account_decisions(normalized_source, normalized_decisions):
    users = normalized_source["users"]

    raw_admin_ids = normalized_decisions["regular_system_admin_user_ids"]
    admin_ids = []
    seen_admin_ids = set()
    for raw_id in raw_admin_ids:
        normalized_id = _normalize_id_value(raw_id)
        if normalized_id in seen_admin_ids:
            raise MigrationDecisionError("admin_ids")
        seen_admin_ids.add(normalized_id)
        if normalized_id not in users:
            raise MigrationDecisionError("admin_ids")
        admin_ids.append(normalized_id)
    regular_admin_ids = frozenset(admin_ids)

    account_kinds = {}
    for user_id, record in users.items():
        if record["staff_id"] is not None:
            account_kinds[user_id] = "staff"
        elif user_id in regular_admin_ids:
            account_kinds[user_id] = "independent_admin"
        else:
            raise MigrationDecisionError("unlinked_account")

    break_glass = normalized_decisions["break_glass_account"]
    break_glass_id = _normalize_id_value(break_glass["id"])
    if break_glass_id in users:
        raise MigrationDecisionError("break_glass")
    existing_usernames = {record["username"] for record in users.values()}
    if break_glass["username"] in existing_usernames:
        raise MigrationDecisionError("break_glass")
    normalized_break_glass = dict(break_glass)
    normalized_break_glass["id"] = break_glass_id

    status_resolutions = normalized_decisions["status_conflict_resolutions"]
    staff_by_id = normalized_source["staff"]
    normalized_status_resolutions = {}
    for raw_key, value in status_resolutions.items():
        normalized_key = _normalize_id_value(raw_key)
        if normalized_key in normalized_status_resolutions:
            raise MigrationDecisionError("status_resolutions")
        normalized_status_resolutions[normalized_key] = value

    expected_conflicts = {}
    for user_id, record in users.items():
        staff_id = record["staff_id"]
        if staff_id is None or staff_id not in staff_by_id:
            continue
        staff_active = staff_by_id[staff_id]["active"]
        if record["is_active"] == 1 and staff_active == 0:
            expected_conflicts[user_id] = "disable_user"
        elif record["is_active"] == 0 and staff_active == 1:
            expected_conflicts[user_id] = "keep_disabled"

    if normalized_status_resolutions != expected_conflicts:
        raise MigrationDecisionError("status_resolutions")

    final_user_active = {}
    for user_id, record in users.items():
        resolution = normalized_status_resolutions.get(user_id)
        if resolution == "disable_user":
            final_user_active[user_id] = 0
        elif resolution == "keep_disabled":
            final_user_active[user_id] = 0
        else:
            final_user_active[user_id] = record["is_active"]

    usable_admin_count = sum(
        1 for admin_id in regular_admin_ids if final_user_active[admin_id] == 1
    )
    if usable_admin_count < 1:
        raise MigrationDecisionError("no_usable_admin")

    users_to_disable = sum(
        1 for resolution in normalized_status_resolutions.values()
        if resolution == "disable_user"
    )

    return {
        "regular_admin_ids": regular_admin_ids,
        "account_kinds": account_kinds,
        "final_user_active": final_user_active,
        "break_glass_account": normalized_break_glass,
        "users_to_disable": users_to_disable,
    }


def _prepare_legacy_user_role_acknowledgments(
    normalized_source, normalized_decisions, role_plan, account_plan
):
    known_role_keys = normalized_source["roles"]
    staff_role_keys = role_plan["staff_role_keys"]

    required_ids = set()
    for user_id, record in normalized_source["users"].items():
        staff_id = record["staff_id"]
        if staff_id is None:
            if account_plan.get("account_kinds", {}).get(user_id) != "independent_admin":
                raise MigrationDecisionError("acknowledged_user_roles")
            continue
        legacy_role = record["role"]
        needs_ack = True
        if isinstance(legacy_role, str):
            trimmed = legacy_role.strip()
            if (
                trimmed != ""
                and trimmed != "admin"
                and trimmed in known_role_keys
                and trimmed in staff_role_keys.get(staff_id, frozenset())
            ):
                needs_ack = False
        if needs_ack:
            required_ids.add(user_id)

    raw_ids = normalized_decisions["acknowledged_legacy_user_role_user_ids"]
    normalized_ids = set()
    for raw_id in raw_ids:
        normalized_id = _normalize_id_value(raw_id)
        if normalized_id in normalized_ids:
            raise MigrationDecisionError("acknowledged_user_roles")
        normalized_ids.add(normalized_id)

    if normalized_ids != required_ids:
        raise MigrationDecisionError("acknowledged_user_roles")

    return {
        "acknowledged_user_ids": frozenset(normalized_ids),
        "legacy_user_roles_acknowledged": len(required_ids),
    }


def _readonly_authorizer(action, arg1, arg2, arg3, arg4):
    if action in _ALLOWED_READ_ACTIONS:
        return sqlite3.SQLITE_OK
    if action == sqlite3.SQLITE_PRAGMA and arg1 in _ALLOWED_PRAGMAS:
        return sqlite3.SQLITE_OK
    return sqlite3.SQLITE_DENY


def _resolve_and_guard_path(source_db_path):
    try:
        raw_path = Path(source_db_path)
        resolved = raw_path.resolve(strict=True)
    except (OSError, TypeError, ValueError):
        raise PreflightError(_GENERIC_ERROR) from None

    if resolved == DEFAULT_DB_PATH.resolve():
        raise PreflightError(_GENERIC_ERROR)

    if not resolved.is_file():
        raise PreflightError(_GENERIC_ERROR)

    return resolved


def _safe_close(con):
    """Best-effort close; returns True if it succeeded, False if it raised."""
    try:
        con.close()
        return True
    except Exception:
        return False


def _open_readonly(resolved_path):
    uri = f"{resolved_path.as_uri()}?mode=ro"
    con = None
    try:
        con = sqlite3.connect(uri, uri=True)
        con.execute("PRAGMA query_only = ON")
        con.set_authorizer(_readonly_authorizer)
        con.execute("SELECT count(*) FROM sqlite_master").fetchone()
    except sqlite3.Error:
        if con is not None:
            _safe_close(con)
        raise PreflightError(_GENERIC_ERROR) from None
    return con


def _guard_copy_paths(source_db_path, target_db_path):
    try:
        resolved_source = _resolve_and_guard_path(source_db_path)
    except PreflightError:
        raise MigrationDecisionError("source_path") from None

    try:
        raw_target = Path(target_db_path)
        target_exists = os.path.lexists(raw_target)
        resolved_target = raw_target.resolve(strict=False)
    except (OSError, TypeError, ValueError):
        raise MigrationDecisionError("target_path") from None

    if resolved_target == resolved_source:
        raise MigrationDecisionError("source_target_same")

    if resolved_target == DEFAULT_DB_PATH.resolve():
        raise MigrationDecisionError("target_path")

    if target_exists:
        raise MigrationDecisionError("target_exists")

    if not resolved_target.parent.is_dir():
        raise MigrationDecisionError("target_path")

    return resolved_source, resolved_target


def _remove_sqlite_artifacts(path):
    """Best-effort cleanup: never raises, may leave a file if the OS refuses."""
    path = Path(path)
    for candidate in (path, *(Path(str(path) + suffix) for suffix in ("-wal", "-shm", "-journal"))):
        _unlink_with_retries(candidate)


_SIDECAR_CLEANUP_ATTEMPTS = 3
_MAIN_FILE_CLEANUP_ATTEMPTS = 3


def _unlink_with_retries(path, attempts=_MAIN_FILE_CLEANUP_ATTEMPTS):
    """Best-effort unlink with limited retries; returns True once gone."""
    path = Path(path)
    for _ in range(attempts):
        try:
            path.unlink()
            return True
        except FileNotFoundError:
            return True
        except OSError:
            continue
    return False


def _remove_temp_sidecars_before_publish(path):
    """Clear -wal/-shm/-journal sidecars before publish; retries transient errors.

    Must run before the temp copy is published so a failure here never leaves
    a published target behind. Raises MigrationDecisionError('cleanup_failed')
    if an OSError still remains after all retries.
    """
    path = Path(path)
    for suffix in ("-wal", "-shm", "-journal"):
        candidate = Path(str(path) + suffix)
        last_error = None
        for _ in range(_SIDECAR_CLEANUP_ATTEMPTS):
            try:
                candidate.unlink()
                last_error = None
                break
            except FileNotFoundError:
                last_error = None
                break
            except OSError as exc:
                last_error = exc
        if last_error is not None:
            raise MigrationDecisionError("cleanup_failed") from None


def _backup_source_to_temp(resolved_source, resolved_target):
    fd = None
    temp_path = None
    source_con = None
    dest_con = None
    failed = False
    try:
        fd, temp_name = tempfile.mkstemp(
            prefix=f".{resolved_target.name}.migrating-",
            dir=resolved_target.parent,
        )
        temp_path = Path(temp_name)
        os.close(fd)
        fd = None
        os.chmod(temp_path, 0o600)
        source_con = _open_readonly(resolved_source)
        dest_con = sqlite3.connect(str(temp_path))
        source_con.backup(dest_con)
    except Exception:
        failed = True
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except Exception:
                pass
        if dest_con is not None and not _safe_close(dest_con):
            failed = True
        if source_con is not None and not _safe_close(source_con):
            failed = True

    if failed:
        if temp_path is not None:
            _remove_sqlite_artifacts(temp_path)
        raise MigrationDecisionError("backup_failed") from None

    return temp_path


def _table_columns(con, table):
    rows = con.execute(f"PRAGMA table_info({table})").fetchall()
    return {row[1] for row in rows}


def _tables_present_and_columns(con):
    existing_tables = {
        row[0]
        for row in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }

    tables_present = {}
    missing_columns = {}
    columns_by_table = {}

    for table, required_cols in REQUIRED_COLUMNS.items():
        present = table in existing_tables
        tables_present[table] = present
        if present:
            cols = _table_columns(con, table)
            columns_by_table[table] = cols
            missing_columns[table] = [c for c in required_cols if c not in cols]
        else:
            columns_by_table[table] = set()
            missing_columns[table] = list(required_cols)

    return tables_present, missing_columns, columns_by_table


def _has_cols(columns_by_table, table, cols):
    return all(c in columns_by_table.get(table, set()) for c in cols)


def _known_role_keys(con, tables_present, columns_by_table):
    if not tables_present["roles"] or not _has_cols(columns_by_table, "roles", ["role_key"]):
        return set()
    rows = con.execute("SELECT role_key FROM roles").fetchall()
    return {r[0] for r in rows if r[0] is not None}


def _known_staff_tokens(con, tables_present, columns_by_table):
    tokens = set(_FIXED_STAFF_ROLE_TOKENS)
    if tables_present["roles"]:
        if _has_cols(columns_by_table, "roles", ["role_key"]):
            tokens.update(
                r[0] for r in con.execute("SELECT role_key FROM roles").fetchall() if r[0]
            )
        if _has_cols(columns_by_table, "roles", ["name"]):
            tokens.update(
                r[0] for r in con.execute("SELECT name FROM roles").fetchall() if r[0]
            )
    return tokens


def _compute_counts_and_conflicts(con, tables_present, columns_by_table):
    counts = {
        "staff_total": 0,
        "staff_active": 0,
        "staff_inactive": 0,
        "users_total": 0,
        "users_active": 0,
        "users_inactive": 0,
        "users_linked": 0,
        "users_independent": 0,
        "legacy_admin_role_users": 0,
        "sessions_total": 0,
        "staff_multi_role_rows": 0,
    }
    conflicts = {
        "duplicate_username_groups": 0,
        "duplicate_username_rows": 0,
        "duplicate_staff_link_groups": 0,
        "duplicate_staff_link_rows": 0,
        "orphan_staff_links": 0,
        "active_user_inactive_staff": 0,
        "inactive_user_active_staff": 0,
        "unknown_user_roles": 0,
        "unknown_staff_role_tokens": 0,
    }

    staff_active_map = {}
    if tables_present["staff_members"]:
        has_staff_id = _has_cols(columns_by_table, "staff_members", ["staff_id"])
        has_active = _has_cols(columns_by_table, "staff_members", ["active"])

        if has_staff_id:
            counts["staff_total"] = con.execute(
                "SELECT COUNT(*) FROM staff_members"
            ).fetchone()[0]

        if has_active:
            counts["staff_active"] = con.execute(
                "SELECT COUNT(*) FROM staff_members WHERE active = 1"
            ).fetchone()[0]
            counts["staff_inactive"] = con.execute(
                "SELECT COUNT(*) FROM staff_members WHERE active != 1 OR active IS NULL"
            ).fetchone()[0]

        if has_staff_id and has_active:
            for staff_id, active in con.execute(
                "SELECT staff_id, active FROM staff_members"
            ).fetchall():
                staff_active_map[staff_id] = active == 1

        known_staff_tokens = _known_staff_tokens(con, tables_present, columns_by_table)
        has_role = _has_cols(columns_by_table, "staff_members", ["role"])
        has_roles = _has_cols(columns_by_table, "staff_members", ["roles"])

        if has_role or has_roles:
            select_cols = [c for c in ("role", "roles") if {"role": has_role, "roles": has_roles}[c]]
            unknown_tokens = 0
            multi_role_rows = 0
            for row in con.execute(
                "SELECT {} FROM staff_members".format(", ".join(select_cols))
            ).fetchall():
                values = dict(zip(select_cols, row))
                roles_field_tokens = [
                    t.strip()
                    for t in (values.get("roles") or "").split(",")
                    if t.strip()
                ] if has_roles else []
                if len(roles_field_tokens) > 1:
                    multi_role_rows += 1

                per_staff_tokens = set(roles_field_tokens)
                if has_role:
                    role_token = (values.get("role") or "").strip()
                    if role_token:
                        per_staff_tokens.add(role_token)

                for t in per_staff_tokens:
                    if t not in known_staff_tokens:
                        unknown_tokens += 1
            conflicts["unknown_staff_role_tokens"] += unknown_tokens
            counts["staff_multi_role_rows"] = multi_role_rows

    users_active_map = {}
    users_staff_link = []
    if tables_present["users"]:
        has_id = _has_cols(columns_by_table, "users", ["id"])
        has_is_active = _has_cols(columns_by_table, "users", ["is_active"])
        has_staff_id = _has_cols(columns_by_table, "users", ["staff_id"])
        has_username = _has_cols(columns_by_table, "users", ["username"])
        has_role = _has_cols(columns_by_table, "users", ["role"])

        if has_id:
            counts["users_total"] = con.execute("SELECT COUNT(*) FROM users").fetchone()[0]

        if has_is_active:
            counts["users_active"] = con.execute(
                "SELECT COUNT(*) FROM users WHERE is_active = 1"
            ).fetchone()[0]
            counts["users_inactive"] = con.execute(
                "SELECT COUNT(*) FROM users WHERE is_active != 1 OR is_active IS NULL"
            ).fetchone()[0]

        if has_staff_id:
            counts["users_linked"] = con.execute(
                f"SELECT COUNT(*) FROM users WHERE {_LINKED_STAFF_ID}"
            ).fetchone()[0]
            counts["users_independent"] = con.execute(
                f"SELECT COUNT(*) FROM users WHERE {_UNLINKED_STAFF_ID}"
            ).fetchone()[0]

        if has_role:
            counts["legacy_admin_role_users"] = con.execute(
                "SELECT COUNT(*) FROM users WHERE role = 'admin'"
            ).fetchone()[0]

            known_role_keys = _known_role_keys(con, tables_present, columns_by_table)
            unknown = con.execute(
                "SELECT COUNT(*) FROM users WHERE role IS NOT NULL AND role != ''"
            ).fetchone()[0]
            if known_role_keys:
                known_count = con.execute(
                    "SELECT COUNT(*) FROM users WHERE role IN ({})".format(
                        ",".join("?" for _ in known_role_keys)
                    ),
                    list(known_role_keys),
                ).fetchone()[0]
                conflicts["unknown_user_roles"] = unknown - known_count
            else:
                conflicts["unknown_user_roles"] = unknown

        if has_username:
            dup_rows = con.execute(
                """
                SELECT COUNT(*) FROM (
                    SELECT username FROM users
                    WHERE username IS NOT NULL
                    GROUP BY username HAVING COUNT(*) > 1
                )
                """
            ).fetchone()[0]
            conflicts["duplicate_username_groups"] = dup_rows
            dup_row_total = con.execute(
                """
                SELECT COALESCE(SUM(cnt), 0) FROM (
                    SELECT COUNT(*) as cnt FROM users
                    WHERE username IS NOT NULL
                    GROUP BY username HAVING COUNT(*) > 1
                )
                """
            ).fetchone()[0]
            conflicts["duplicate_username_rows"] = dup_row_total

        if has_staff_id:
            conflicts["duplicate_staff_link_groups"] = con.execute(
                f"""
                SELECT COUNT(*) FROM (
                    SELECT staff_id FROM users
                    WHERE {_LINKED_STAFF_ID}
                    GROUP BY staff_id HAVING COUNT(*) > 1
                )
                """
            ).fetchone()[0]
            conflicts["duplicate_staff_link_rows"] = con.execute(
                f"""
                SELECT COALESCE(SUM(cnt), 0) FROM (
                    SELECT COUNT(*) as cnt FROM users
                    WHERE {_LINKED_STAFF_ID}
                    GROUP BY staff_id HAVING COUNT(*) > 1
                )
                """
            ).fetchone()[0]

        if has_staff_id and tables_present["staff_members"] and has_id:
            if _has_cols(columns_by_table, "staff_members", ["staff_id"]):
                orphan = con.execute(
                    f"""
                    SELECT COUNT(*) FROM users u
                    WHERE {_LINKED_STAFF_ID.replace('staff_id', 'u.staff_id')}
                      AND NOT EXISTS (
                          SELECT 1 FROM staff_members s WHERE s.staff_id = u.staff_id
                      )
                    """
                ).fetchone()[0]
                conflicts["orphan_staff_links"] = orphan

        if has_staff_id and has_is_active and staff_active_map:
            active_inactive = 0
            inactive_active = 0
            for user_id, is_active, staff_id in con.execute(
                f"SELECT id, is_active, staff_id FROM users WHERE {_LINKED_STAFF_ID}"
            ).fetchall():
                if staff_id not in staff_active_map:
                    continue
                staff_is_active = staff_active_map[staff_id]
                user_is_active = is_active == 1
                if user_is_active and not staff_is_active:
                    active_inactive += 1
                if not user_is_active and staff_is_active:
                    inactive_active += 1
            conflicts["active_user_inactive_staff"] = active_inactive
            conflicts["inactive_user_active_staff"] = inactive_active

    if tables_present["sessions"]:
        counts["sessions_total"] = con.execute(
            "SELECT COUNT(*) FROM sessions"
        ).fetchone()[0]

    return counts, conflicts


def _compute_security(tables_present, columns_by_table):
    security = {
        "users_has_is_system_admin": False,
        "users_has_account_kind": False,
        "sessions_has_plaintext_token": False,
        "sessions_has_token_hash": False,
        "audit_has_extended_fields": False,
    }

    if tables_present["users"]:
        user_cols = columns_by_table.get("users", set())
        security["users_has_is_system_admin"] = "is_system_admin" in user_cols
        security["users_has_account_kind"] = "account_kind" in user_cols

    if tables_present["sessions"]:
        session_cols = columns_by_table.get("sessions", set())
        security["sessions_has_plaintext_token"] = "token" in session_cols
        security["sessions_has_token_hash"] = "token_hash" in session_cols

    if tables_present["audit_logs"]:
        audit_cols = columns_by_table.get("audit_logs", set())
        security["audit_has_extended_fields"] = all(
            f in audit_cols for f in AUDIT_EXTENDED_FIELDS
        )

    return security


_MIGRATION_REQUIRED_COLUMNS = {
    "staff_members": [
        "staff_id", "name", "note", "job_no", "phone", "sex", "id_card",
        "title", "license_no", "department", "role", "roles", "active",
        "created_at", "updated_at",
    ],
    "users": [
        "id", "username", "display_name", "password_hash", "role",
        "is_active", "staff_id", "created_at", "updated_at",
    ],
    "sessions": ["token", "user_id", "created_at", "expires_at"],
    "roles": ["role_key", "name", "is_system", "sort", "created_at", "updated_at"],
    "role_permissions": ["role_key", "perm_key"],
    "audit_logs": [
        "audit_id", "entity_type", "entity_id", "action", "old_json",
        "new_json", "operator", "created_at",
    ],
}

_TARGET_STRUCTURE_TABLES = {
    "staff_role_assignments",
    "role_data_scopes",
}

_TARGET_STRUCTURE_COLUMNS = {
    "users": {"is_system_admin", "account_kind"},
    "sessions": {"token_hash", "session_id"},
    "staff_members": {"employment_status"},
}


def _load_legacy_source_snapshot(source_db_path):
    try:
        resolved = _resolve_and_guard_path(source_db_path)
        con = _open_readonly(resolved)
    except PreflightError:
        raise MigrationDecisionError("source_path") from None

    try:
        existing_tables = {
            row[0]
            for row in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }

        if existing_tables & _TARGET_STRUCTURE_TABLES:
            raise MigrationDecisionError("source_already_target")

        for table, required_cols in _MIGRATION_REQUIRED_COLUMNS.items():
            if table not in existing_tables:
                raise MigrationDecisionError("source_schema")
            cols = _table_columns(con, table)
            if any(c not in cols for c in required_cols):
                raise MigrationDecisionError("source_schema")
            marker_cols = _TARGET_STRUCTURE_COLUMNS.get(table)
            if marker_cols and (cols & marker_cols):
                raise MigrationDecisionError("source_already_target")

        staff_rows = con.execute(
            "SELECT {} FROM staff_members".format(
                ", ".join(_MIGRATION_REQUIRED_COLUMNS["staff_members"])
            )
        ).fetchall()
        user_rows = con.execute(
            "SELECT {} FROM users".format(
                ", ".join(_MIGRATION_REQUIRED_COLUMNS["users"])
            )
        ).fetchall()
        roles = con.execute("SELECT role_key, name FROM roles").fetchall()
        sessions_to_invalidate = con.execute(
            "SELECT COUNT(*) FROM sessions"
        ).fetchone()[0]
        audit_rows_preserved = con.execute(
            "SELECT COUNT(*) FROM audit_logs"
        ).fetchone()[0]
    except MigrationDecisionError:
        raise
    except sqlite3.Error:
        raise MigrationDecisionError("source_schema") from None
    finally:
        close_ok = _safe_close(con)

    if not close_ok:
        raise MigrationDecisionError("source_schema")

    return {
        "staff_rows": staff_rows,
        "user_rows": user_rows,
        "roles": roles,
        "sessions_to_invalidate": sessions_to_invalidate,
        "audit_rows_preserved": audit_rows_preserved,
    }


def _normalize_id_value(value):
    if not _is_valid_id_value(value):
        raise MigrationDecisionError("source_rows")
    if isinstance(value, int):
        return str(value)
    return value


def _normalize_optional_staff_id(value):
    if value is None:
        return None
    if isinstance(value, str) and value.strip() == "":
        return None
    return _normalize_id_value(value)


def _normalize_legacy_source_rows(snapshot):
    staff_cols = _MIGRATION_REQUIRED_COLUMNS["staff_members"]
    user_cols = _MIGRATION_REQUIRED_COLUMNS["users"]

    staff = {}
    for row in snapshot["staff_rows"]:
        record = dict(zip(staff_cols, row))
        staff_id = _normalize_id_value(record["staff_id"])
        if staff_id in staff:
            raise MigrationDecisionError("source_rows")
        active = record["active"]
        if type(active) is not int or active not in (0, 1):
            raise MigrationDecisionError("source_rows")
        record["staff_id"] = staff_id
        staff[staff_id] = record

    users = {}
    seen_usernames = set()
    staff_account_map = {}
    for row in snapshot["user_rows"]:
        record = dict(zip(user_cols, row))
        user_id = _normalize_id_value(record["id"])
        if user_id in users:
            raise MigrationDecisionError("source_rows")

        if not _is_nonblank_str(record["username"]) or not _is_nonblank_str(
            record["password_hash"]
        ):
            raise MigrationDecisionError("source_rows")
        if record["username"] in seen_usernames:
            raise MigrationDecisionError("duplicate_username")
        seen_usernames.add(record["username"])

        is_active = record["is_active"]
        if type(is_active) is not int or is_active not in (0, 1):
            raise MigrationDecisionError("source_rows")

        staff_id = _normalize_optional_staff_id(record["staff_id"])
        if staff_id is not None:
            if staff_id not in staff:
                raise MigrationDecisionError("orphan_staff_link")
            if staff_id in staff_account_map:
                raise MigrationDecisionError("duplicate_staff_account")
            staff_account_map[staff_id] = user_id

        record["id"] = user_id
        record["staff_id"] = staff_id
        users[user_id] = record

    roles = {}
    for role_key, name in snapshot["roles"]:
        normalized_key = _normalize_id_value(role_key)
        if normalized_key in roles:
            raise MigrationDecisionError("source_rows")
        roles[normalized_key] = name

    sessions_to_invalidate = snapshot["sessions_to_invalidate"]
    audit_rows_preserved = snapshot["audit_rows_preserved"]
    if type(sessions_to_invalidate) is not int or sessions_to_invalidate < 0:
        raise MigrationDecisionError("source_rows")
    if type(audit_rows_preserved) is not int or audit_rows_preserved < 0:
        raise MigrationDecisionError("source_rows")

    return {
        "staff": staff,
        "users": users,
        "roles": roles,
        "sessions_to_invalidate": sessions_to_invalidate,
        "audit_rows_preserved": audit_rows_preserved,
    }


def preflight(source_db_path):
    resolved = _resolve_and_guard_path(source_db_path)
    con = _open_readonly(resolved)
    try:
        tables_present, missing_columns, columns_by_table = _tables_present_and_columns(con)
        counts, conflicts = _compute_counts_and_conflicts(con, tables_present, columns_by_table)
        security = _compute_security(tables_present, columns_by_table)
    finally:
        close_ok = _safe_close(con)

    if not close_ok:
        raise PreflightError(_GENERIC_ERROR)

    return {
        "version": 1,
        "schema": {
            "tables_present": tables_present,
            "missing_columns": missing_columns,
        },
        "counts": counts,
        "conflicts": conflicts,
        "security": security,
    }


def _prepare_migration_plan(source_db_path, decisions):
    normalized_decisions = _validate_decision_envelope(decisions)
    snapshot = _load_legacy_source_snapshot(source_db_path)
    source = _normalize_legacy_source_rows(snapshot)
    role_plan = _prepare_staff_role_assignments(source, normalized_decisions)
    account_plan = _prepare_account_decisions(source, normalized_decisions)
    role_ack_plan = _prepare_legacy_user_role_acknowledgments(
        source, normalized_decisions, role_plan, account_plan
    )

    summary = {
        "version": 1,
        "staff_migrated": len(source["staff"]),
        "role_assignments_created": len(role_plan["assignments"]),
        "users_migrated": len(source["users"]) + 1,
        "regular_system_admins": len(account_plan["regular_admin_ids"]),
        "break_glass_accounts": 1,
        "sessions_to_invalidate": source["sessions_to_invalidate"],
        "audit_rows_preserved": source["audit_rows_preserved"],
        "users_to_disable": account_plan["users_to_disable"],
        "legacy_user_roles_acknowledged": role_ack_plan["legacy_user_roles_acknowledged"],
    }

    return {
        "summary": summary,
        "source": source,
        "decisions": normalized_decisions,
        "role_plan": role_plan,
        "account_plan": account_plan,
        "role_ack_plan": role_ack_plan,
    }


def plan_migration(source_db_path, decisions):
    plan = _prepare_migration_plan(source_db_path, decisions)
    return dict(plan["summary"])


_LEGACY_AUDIT_LOG_COLUMNS = [
    "audit_id", "entity_type", "entity_id", "action", "old_json",
    "new_json", "operator", "created_at",
]

_TARGET_TABLES = {
    "staff_members", "staff_role_assignments", "users",
    "sessions", "role_data_scopes", "audit_logs",
}

# 2026-07-15 用户决定：局域网精简模式取消登录限速。这两张表不属于目标模型，
# 但可能作为废表残留在旧副本里（SQLite backup 会原样带过来），必须显式清掉。
_OBSOLETE_TABLES = ("login_throttle_accounts", "login_throttle_sources")


def _execute_schema_ddl(con, sql_text):
    """Execute frozen target DDL statement-by-statement (no implicit commit)."""
    buf = ""
    for line in sql_text.splitlines(keepends=True):
        buf += line
        if sqlite3.complete_statement(buf):
            stmt = buf.strip()
            if stmt:
                con.execute(stmt)
            buf = ""
    if buf.strip():
        raise MigrationDecisionError("transform_failed")


def _none_to_blank(value):
    return "" if value is None else value


def _staff_target_row(record):
    active = record["active"]
    return (
        record["staff_id"],
        record["name"],
        _none_to_blank(record["note"]),
        _none_to_blank(record["job_no"]),
        _none_to_blank(record["phone"]),
        _none_to_blank(record["sex"]),
        _none_to_blank(record["id_card"]),
        _none_to_blank(record["title"]),
        _none_to_blank(record["license_no"]),
        _none_to_blank(record["department"]),
        "employed" if active == 1 else "left",
        None,
        "",
        record["created_at"],
        record["updated_at"],
    )


def _transform_temporary_copy(temp_path, prepared_plan):
    """Transform an already-isolated temporary copy in-place under one transaction."""
    source = prepared_plan["source"]
    role_plan = prepared_plan["role_plan"]
    account_plan = prepared_plan["account_plan"]

    con = None
    close_ok = True
    try:
        con = sqlite3.connect(str(temp_path), isolation_level=None)

        audit_rows = con.execute(
            "SELECT {} FROM audit_logs".format(", ".join(_LEGACY_AUDIT_LOG_COLUMNS))
        ).fetchall()

        con.execute("PRAGMA foreign_keys=OFF")
        con.execute("BEGIN IMMEDIATE")

        con.execute("DROP TABLE sessions")
        con.execute("DROP TABLE users")
        con.execute("DROP TABLE staff_members")
        con.execute("DROP TABLE audit_logs")

        for table in _OBSOLETE_TABLES:
            con.execute(f"DROP TABLE IF EXISTS {table}")

        _execute_schema_ddl(con, load_personnel_access_schema_sql())

        for staff_id, record in source["staff"].items():
            con.execute(
                """
                INSERT INTO staff_members(
                    staff_id, name, note, job_no, phone, sex, id_card,
                    title, license_no, department, employment_status,
                    left_at, left_reason, created_at, updated_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                _staff_target_row(record),
            )

        for staff_id, role_key, is_primary, created_at in role_plan["assignments"]:
            con.execute(
                "INSERT INTO staff_role_assignments(staff_id, role_key, is_primary, created_at) "
                "VALUES (?,?,?,?)",
                (staff_id, role_key, is_primary, created_at),
            )

        regular_admin_ids = account_plan["regular_admin_ids"]
        final_user_active = account_plan["final_user_active"]
        account_kinds = account_plan["account_kinds"]
        for user_id, record in source["users"].items():
            is_system_admin = 1 if user_id in regular_admin_ids else 0
            account_kind = account_kinds[user_id]
            con.execute(
                """
                INSERT INTO users(
                    id, username, display_name, password_hash, staff_id,
                    is_active, is_system_admin, account_kind,
                    created_at, updated_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    record["id"],
                    record["username"],
                    _none_to_blank(record["display_name"]),
                    record["password_hash"],
                    record["staff_id"],
                    final_user_active[user_id],
                    is_system_admin,
                    account_kind,
                    record["created_at"],
                    record["updated_at"],
                ),
            )

        break_glass = account_plan["break_glass_account"]
        con.execute(
            """
            INSERT INTO users(
                id, username, display_name, password_hash, staff_id,
                is_active, is_system_admin, account_kind
            ) VALUES (?,?,?,?,NULL,1,1,'break_glass')
            """,
            (
                break_glass["id"],
                break_glass["username"],
                break_glass["display_name"],
                break_glass["password_hash"],
            ),
        )

        for row in audit_rows:
            con.execute(
                "INSERT INTO audit_logs({}) VALUES (?,?,?,?,?,?,?,?)".format(
                    ", ".join(_LEGACY_AUDIT_LOG_COLUMNS)
                ),
                row,
            )

        con.execute("DELETE FROM role_permissions WHERE role_key='admin'")
        con.execute("DELETE FROM roles WHERE role_key='admin'")

        _validate_transformed_copy(
            con, source, role_plan, account_plan, audit_rows
        )

        con.execute("COMMIT")

        con.execute("PRAGMA foreign_keys=ON")
        fk_value = con.execute("PRAGMA foreign_keys").fetchone()
        if fk_value is None or fk_value[0] != 1:
            raise MigrationDecisionError("transform_failed")

        journal_mode = con.execute("PRAGMA journal_mode=DELETE").fetchone()
        if journal_mode is None or str(journal_mode[0]).lower() != "delete":
            raise MigrationDecisionError("transform_failed")

        con.execute("VACUUM")

        result = {
            "version": 1,
            "staff_migrated": len(source["staff"]),
            "role_assignments_created": len(role_plan["assignments"]),
            "users_migrated": len(source["users"]) + 1,
            "regular_system_admins": len(account_plan["regular_admin_ids"]),
            "break_glass_accounts": 1,
            "sessions_invalidated": source["sessions_to_invalidate"],
            "audit_rows_preserved": len(audit_rows),
        }
    except Exception:
        if con is not None and con.in_transaction:
            try:
                con.execute("ROLLBACK")
            except Exception:
                pass
        raise MigrationDecisionError("transform_failed") from None
    finally:
        if con is not None:
            close_ok = _safe_close(con)

    if not close_ok:
        raise MigrationDecisionError("transform_failed")

    return result


def _validate_transformed_copy(con, source, role_plan, account_plan, audit_rows):
    existing_tables = {
        row[0]
        for row in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    if not _TARGET_TABLES.issubset(existing_tables):
        raise MigrationDecisionError("transform_failed")
    if existing_tables.intersection(_OBSOLETE_TABLES):
        raise MigrationDecisionError("transform_failed")

    def _count(table):
        return con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]

    if _count("staff_members") != len(source["staff"]):
        raise MigrationDecisionError("transform_failed")
    if _count("users") != len(source["users"]) + 1:
        raise MigrationDecisionError("transform_failed")
    if _count("staff_role_assignments") != len(role_plan["assignments"]):
        raise MigrationDecisionError("transform_failed")
    if _count("audit_logs") != len(audit_rows):
        raise MigrationDecisionError("transform_failed")
    if _count("sessions") != 0:
        raise MigrationDecisionError("transform_failed")

    active_admin_non_bg = con.execute(
        "SELECT COUNT(*) FROM users WHERE is_active=1 AND is_system_admin=1 "
        "AND account_kind != 'break_glass'"
    ).fetchone()[0]
    if active_admin_non_bg < 1:
        raise MigrationDecisionError("transform_failed")

    break_glass_count = con.execute(
        "SELECT COUNT(*) FROM users WHERE is_active=1 AND is_system_admin=1 "
        "AND account_kind='break_glass'"
    ).fetchone()[0]
    if break_glass_count != 1:
        raise MigrationDecisionError("transform_failed")

    leftover_admin_role = con.execute(
        "SELECT COUNT(*) FROM roles WHERE role_key='admin'"
    ).fetchone()[0]
    leftover_admin_perms = con.execute(
        "SELECT COUNT(*) FROM role_permissions WHERE role_key='admin'"
    ).fetchone()[0]
    if leftover_admin_role != 0 or leftover_admin_perms != 0:
        raise MigrationDecisionError("transform_failed")

    staff_cols = _table_columns(con, "staff_members")
    if staff_cols & {"role", "roles", "active"}:
        raise MigrationDecisionError("transform_failed")
    users_cols = _table_columns(con, "users")
    if "role" in users_cols:
        raise MigrationDecisionError("transform_failed")
    sessions_cols = _table_columns(con, "sessions")
    if sessions_cols != {
        "session_id", "token_hash", "user_id", "device_name", "user_agent",
        "ip_address", "created_at", "last_seen_at", "idle_expires_at",
        "reauthenticated_at",
    }:
        raise MigrationDecisionError("transform_failed")
    audit_cols = _table_columns(con, "audit_logs")
    if not all(f in audit_cols for f in AUDIT_EXTENDED_FIELDS):
        raise MigrationDecisionError("transform_failed")

    fk_violations = con.execute("PRAGMA foreign_key_check").fetchall()
    if fk_violations:
        raise MigrationDecisionError("transform_failed")


def build_migrated_copy(source_db_path, target_db_path, decisions):
    """Build the personnel-access target DB as a new file, atomically.

    Validates the decision envelope before touching the filesystem, backs the
    source up into a temporary copy, transforms that copy under one
    transaction, then publishes it to target_db_path only once every step
    has succeeded. Never mutates the source and never leaves a partial
    target behind.
    """
    normalized_decisions = _validate_decision_envelope(decisions)
    resolved_source, resolved_target = _guard_copy_paths(source_db_path, target_db_path)
    temp_path = _backup_source_to_temp(resolved_source, resolved_target)
    try:
        prepared_plan = _prepare_migration_plan(str(temp_path), normalized_decisions)
        result = _transform_temporary_copy(temp_path, prepared_plan)

        _remove_temp_sidecars_before_publish(temp_path)

        try:
            os.link(temp_path, resolved_target)
        except FileExistsError:
            raise MigrationDecisionError("target_exists") from None
        except OSError:
            raise MigrationDecisionError("publish_failed") from None

        if not _unlink_with_retries(temp_path):
            try:
                same_inode = os.path.samefile(temp_path, resolved_target)
            except OSError:
                same_inode = False
            if same_inode:
                _unlink_with_retries(resolved_target)
            raise MigrationDecisionError("cleanup_failed") from None

        return result
    finally:
        _remove_sqlite_artifacts(temp_path)
