"""Slim access-session core for the local clinic application."""
import hashlib
import json
import os
import re
import secrets
import sqlite3
import unicodedata
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

from local_app.passwords import verify_password


IDLE_TIMEOUT_SECONDS = 6 * 60 * 60
ACTIVITY_REFRESH_SECONDS = 5 * 60
REAUTH_FRESH_SECONDS = 5 * 60
TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"

_DUMMY_PASSWORD_HASH = (
    "f4bfaa87cd6f471fa65ec1a404fe63e4$"
    "2816b5dddb5fc6e66973e04de7e13e6217968d4fc7fc21524791b0f9d2b56b24"
)
_TOKEN_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
_SESSION_ID_PATTERN = re.compile(r"[0-9a-f]{32}\Z")
_REQUEST_ID_PATTERN = re.compile(r"[0-9a-f]{16}\Z")
_LOGIN_DENIED = object()


class AccessSessionError(Exception):
    """Stable public error code without credential or database details."""

    def __init__(self, code):
        self.code = code
        super().__init__(code)

    def __str__(self):
        return self.code

    def __repr__(self):
        return "AccessSessionError({!r})".format(self.code)


class _AuditFailure(Exception):
    pass


def normalize_server_now(value):
    if type(value) is not datetime or value.tzinfo is None:
        raise AccessSessionError("now_invalid")
    try:
        offset = value.utcoffset()
    except BaseException:
        raise AccessSessionError("now_invalid") from None
    if type(offset) is not timedelta:
        raise AccessSessionError("now_invalid")
    one_day = timedelta(days=1)
    if not -one_day < offset < one_day:
        raise AccessSessionError("now_invalid")
    try:
        naive_utc = value.replace(tzinfo=None) - offset
        return datetime(
            naive_utc.year,
            naive_utc.month,
            naive_utc.day,
            naive_utc.hour,
            naive_utc.minute,
            naive_utc.second,
            tzinfo=timezone.utc,
        )
    except BaseException:
        raise AccessSessionError("now_invalid") from None


def _server_now(now):
    value = datetime.now(timezone.utc) if now is None else now
    return normalize_server_now(value)


def _format_time(value):
    return value.strftime(TIMESTAMP_FORMAT)


def _parse_time(value):
    if type(value) is not str:
        return None
    try:
        value.encode("utf-8")
        parsed = datetime.strptime(value, TIMESTAMP_FORMAT)
    except (UnicodeEncodeError, ValueError, OverflowError):
        return None
    if _format_time(parsed) != value:
        return None
    return parsed.replace(tzinfo=timezone.utc)


def _coerce_time(value):
    if type(value) is str:
        return _parse_time(value)
    if type(value) is datetime:
        try:
            return normalize_server_now(value)
        except AccessSessionError:
            return None
    return None


def is_reauth_fresh(reauthenticated_at, *, created_at, now=None):
    try:
        created_utc = _coerce_time(created_at)
        reauth_utc = _coerce_time(reauthenticated_at)
        now_utc = _server_now(now)
        if created_utc is None or reauth_utc is None:
            return False
        if not created_utc <= reauth_utc <= now_utc:
            return False
        return now_utc - reauth_utc <= timedelta(seconds=REAUTH_FRESH_SECONDS)
    except BaseException:
        return False


def hash_token(raw_token):
    if type(raw_token) is not str or _TOKEN_PATTERN.fullmatch(raw_token) is None:
        raise AccessSessionError("token_invalid")
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


def _has_forbidden_controls(value):
    return any(unicodedata.category(char) in ("Cc", "Cf") for char in value)


def _clean_metadata(value, limit, code):
    if type(value) is not str:
        raise AccessSessionError(code)
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise AccessSessionError(code) from None
    if _has_forbidden_controls(value):
        raise AccessSessionError(code)
    return value.strip()[:limit]


def _clean_username(value):
    if type(value) is not str:
        raise AccessSessionError("username_invalid")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise AccessSessionError("username_invalid") from None
    if _has_forbidden_controls(value):
        raise AccessSessionError("username_invalid")
    value = value.strip()[:128]
    if not value:
        raise AccessSessionError("username_invalid")
    return value


def _request_metadata(request_id, ip_address, user_agent):
    if type(request_id) is not str or _REQUEST_ID_PATTERN.fullmatch(request_id) is None:
        raise AccessSessionError("request_id_invalid")
    return (
        request_id,
        _clean_metadata(ip_address, 64, "ip_address_invalid"),
        _clean_metadata(user_agent, 256, "user_agent_invalid"),
    )


def _database_uri(db_path):
    try:
        path = os.fspath(db_path)
        if type(path) is not str or not path:
            raise ValueError
        absolute = os.path.abspath(path)
    except (OSError, TypeError, ValueError):
        raise AccessSessionError("database_unavailable") from None
    return "file:" + quote(absolute, safe="/") + "?mode=rw"


def _open_existing(db_path):
    conn = None
    try:
        conn = sqlite3.connect(
            _database_uri(db_path),
            uri=True,
            isolation_level=None,
            timeout=5.0,
        )
        conn.row_factory = sqlite3.Row
        conn.execute("pragma foreign_keys = on")
        conn.execute("pragma busy_timeout = 5000")
        return conn
    except AccessSessionError:
        if conn is not None:
            conn.close()
        raise
    except (sqlite3.Error, OSError, TypeError, ValueError):
        if conn is not None:
            try:
                conn.close()
            except sqlite3.Error:
                pass
        raise AccessSessionError("database_unavailable") from None


def _rollback(conn):
    if conn is None or not conn.in_transaction:
        return
    try:
        conn.rollback()
    except sqlite3.Error:
        pass


def _run_immediate(db_path, operation):
    conn = None
    try:
        conn = _open_existing(db_path)
        conn.execute("begin immediate")
        result = operation(conn)
        conn.commit()
        return result
    except AccessSessionError:
        _rollback(conn)
        raise
    except _AuditFailure:
        _rollback(conn)
        raise AccessSessionError("audit_failed") from None
    except BaseException:
        _rollback(conn)
        raise AccessSessionError("transaction_failed") from None
    finally:
        if conn is not None:
            try:
                conn.close()
            except sqlite3.Error:
                pass


def _load_user_by_username(conn, username):
    return conn.execute(
        "select u.id as user_id, u.username, u.display_name, u.password_hash, "
        "u.staff_id, u.is_active, u.is_system_admin, u.account_kind, "
        "sm.employment_status "
        "from users u left join staff_members sm on sm.staff_id = u.staff_id "
        "where u.username = ?",
        (username,),
    ).fetchone()


def _load_session_by_token(conn, token_hash_value):
    return conn.execute(
        "select s.session_id, s.token_hash, s.user_id, s.device_name, "
        "s.user_agent, s.ip_address, s.created_at, s.last_seen_at, "
        "s.idle_expires_at, s.reauthenticated_at, u.username, "
        "u.display_name, u.password_hash, u.staff_id, u.is_active, "
        "u.is_system_admin, u.account_kind, sm.employment_status "
        "from sessions s join users u on u.id = s.user_id "
        "left join staff_members sm on sm.staff_id = u.staff_id "
        "where s.token_hash = ?",
        (token_hash_value,),
    ).fetchone()


def _load_session_by_id(conn, session_id):
    return conn.execute(
        "select s.session_id, s.token_hash, s.user_id, s.device_name, "
        "s.user_agent, s.ip_address, s.created_at, s.last_seen_at, "
        "s.idle_expires_at, s.reauthenticated_at, u.username, "
        "u.display_name, u.password_hash, u.staff_id, u.is_active, "
        "u.is_system_admin, u.account_kind, sm.employment_status "
        "from sessions s join users u on u.id = s.user_id "
        "left join staff_members sm on sm.staff_id = u.staff_id "
        "where s.session_id = ?",
        (session_id,),
    ).fetchone()


def _eligible_account(row):
    if row is None or type(row["is_active"]) is not int or row["is_active"] != 1:
        return False
    if row["account_kind"] == "staff":
        return (
            type(row["staff_id"]) is str
            and bool(row["staff_id"].strip())
            and row["employment_status"] == "employed"
        )
    if row["account_kind"] in ("independent_admin", "break_glass"):
        return (
            row["staff_id"] is None
            and type(row["is_system_admin"]) is int
            and row["is_system_admin"] == 1
        )
    return False


def _session_state(row, now):
    if row is None:
        return "missing", None
    created = _parse_time(row["created_at"])
    last_seen = _parse_time(row["last_seen_at"])
    idle_expires = _parse_time(row["idle_expires_at"])
    if (
        created is None
        or last_seen is None
        or idle_expires is None
        or created > last_seen
        or idle_expires <= last_seen
    ):
        return "invalid", None
    detail = {
        "created": created,
        "last_seen": last_seen,
        "idle_expires": idle_expires,
    }
    if now >= idle_expires:
        return "expired", detail
    if not _eligible_account(row):
        return "inactive", detail
    return "valid", detail


def _public_context(row, now):
    reauthenticated = _parse_time(row["reauthenticated_at"])
    created = _parse_time(row["created_at"])
    reauth_text = None
    if (
        reauthenticated is not None
        and created is not None
        and created <= reauthenticated <= now
    ):
        reauth_text = row["reauthenticated_at"]
    return {
        "user": {
            "id": row["user_id"],
            "username": row["username"],
            "display_name": row["display_name"],
            "staff_id": row["staff_id"],
            "account_kind": row["account_kind"],
            "is_system_admin": row["is_system_admin"] == 1,
        },
        "session": {
            "session_id": row["session_id"],
            "user_id": row["user_id"],
            "device_name": row["device_name"],
            "user_agent": row["user_agent"],
            "ip_address": row["ip_address"],
            "created_at": row["created_at"],
            "last_seen_at": row["last_seen_at"],
            "idle_expires_at": row["idle_expires_at"],
            "reauthenticated_at": reauth_text,
        },
    }


def _compact_json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _write_audit(
    conn,
    *,
    entity_id,
    action,
    operator,
    actor_user_id,
    result,
    reason,
    risk_level,
    request_id,
    ip_address,
    user_agent,
    now,
    old_json=None,
    new_json=None
):
    try:
        conn.execute(
            "insert into audit_logs(entity_type, entity_id, action, old_json, "
            "new_json, operator, created_at, actor_user_id, result, reason, "
            "risk_level, request_id, ip_address, user_agent) "
            "values ('session', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                entity_id,
                action,
                old_json,
                new_json,
                operator,
                _format_time(now),
                actor_user_id,
                result,
                reason,
                risk_level,
                request_id,
                ip_address,
                user_agent,
            ),
        )
    except sqlite3.Error:
        raise _AuditFailure from None


def _safe_session_snapshot(row):
    return {
        "session_id": row["session_id"],
        "device_name": row["device_name"],
        "user_agent": row["user_agent"],
        "ip_address": row["ip_address"],
        "created_at": row["created_at"],
        "last_seen_at": row["last_seen_at"],
        "idle_expires_at": row["idle_expires_at"],
    }


def _maybe_renew(conn, row, detail, now):
    if now - detail["last_seen"] < timedelta(seconds=ACTIVITY_REFRESH_SECONDS):
        return row
    conn.execute(
        "update sessions set last_seen_at = ?, idle_expires_at = ? "
        "where session_id = ?",
        (
            _format_time(now),
            _format_time(now + timedelta(seconds=IDLE_TIMEOUT_SECONDS)),
            row["session_id"],
        ),
    )
    return _load_session_by_token(conn, row["token_hash"])


def authenticate_and_create_session(
    db_path,
    *,
    username,
    password,
    device_name,
    user_agent,
    ip_address,
    request_id,
    now=None
):
    username = _clean_username(username)
    device_name = _clean_metadata(device_name, 64, "device_name_invalid")
    request_id, ip_address, user_agent = _request_metadata(
        request_id, ip_address, user_agent
    )
    now_utc = _server_now(now)

    def operation(conn):
        user = _load_user_by_username(conn, username)
        stored_hash = _DUMMY_PASSWORD_HASH if user is None else user["password_hash"]
        password_ok = verify_password(password, stored_hash)
        if not password_ok or not _eligible_account(user):
            _write_audit(
                conn,
                entity_id="login",
                action="auth.login",
                operator="anonymous",
                actor_user_id=None,
                result="denied",
                reason=None,
                risk_level="medium",
                request_id=request_id,
                ip_address=ip_address,
                user_agent=user_agent,
                now=now_utc,
            )
            return _LOGIN_DENIED

        session_id = secrets.token_hex(16)
        raw_token = secrets.token_hex(32)
        if _SESSION_ID_PATTERN.fullmatch(session_id) is None:
            raise RuntimeError
        token_hash_value = hash_token(raw_token)
        now_text = _format_time(now_utc)
        conn.execute(
            "insert into sessions(session_id, token_hash, user_id, device_name, "
            "user_agent, ip_address, created_at, last_seen_at, idle_expires_at, "
            "reauthenticated_at) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                session_id,
                token_hash_value,
                user["user_id"],
                device_name,
                user_agent,
                ip_address,
                now_text,
                now_text,
                _format_time(now_utc + timedelta(seconds=IDLE_TIMEOUT_SECONDS)),
                now_text,
            ),
        )
        row = _load_session_by_token(conn, token_hash_value)
        _write_audit(
            conn,
            entity_id=session_id,
            action="auth.login",
            operator=row["username"],
            actor_user_id=row["user_id"],
            result="success",
            reason=None,
            risk_level="low",
            request_id=request_id,
            ip_address=ip_address,
            user_agent=user_agent,
            now=now_utc,
            new_json=_compact_json({
                "session_id": session_id,
                "user_id": row["user_id"],
                "device_name": device_name,
            }),
        )
        return raw_token, _public_context(row, now_utc)

    result = _run_immediate(db_path, operation)
    if result is _LOGIN_DENIED:
        raise AccessSessionError("invalid_credentials")
    return result


def resolve_session(db_path, raw_token, *, now=None, renew_activity=True):
    if type(renew_activity) is not bool:
        raise AccessSessionError("transaction_failed")
    try:
        token_hash_value = hash_token(raw_token)
    except AccessSessionError as exc:
        if exc.code == "token_invalid":
            return None
        raise
    now_utc = _server_now(now)

    def operation(conn):
        row = _load_session_by_token(conn, token_hash_value)
        state, detail = _session_state(row, now_utc)
        if state == "missing":
            return None
        if state != "valid":
            conn.execute("delete from sessions where session_id = ?", (row["session_id"],))
            return None
        if renew_activity:
            row = _maybe_renew(conn, row, detail, now_utc)
        return _public_context(row, now_utc)

    return _run_immediate(db_path, operation)


def logout_session(
    db_path,
    *,
    raw_token,
    request_id,
    ip_address,
    user_agent,
    now=None
):
    request_id, ip_address, user_agent = _request_metadata(
        request_id, ip_address, user_agent
    )
    now_utc = _server_now(now)
    try:
        token_hash_value = hash_token(raw_token)
    except AccessSessionError as exc:
        if exc.code == "token_invalid":
            return False
        raise

    def operation(conn):
        row = _load_session_by_token(conn, token_hash_value)
        if row is None:
            return False
        old_json = _compact_json(_safe_session_snapshot(row))
        conn.execute("delete from sessions where session_id = ?", (row["session_id"],))
        _write_audit(
            conn,
            entity_id=row["session_id"],
            action="auth.logout",
            operator=row["username"],
            actor_user_id=row["user_id"],
            result="success",
            reason="self_logout",
            risk_level="low",
            request_id=request_id,
            ip_address=ip_address,
            user_agent=user_agent,
            now=now_utc,
            old_json=old_json,
        )
        return True

    return _run_immediate(db_path, operation)


def reauthenticate_session(
    db_path,
    *,
    session_id,
    password,
    request_id,
    ip_address,
    user_agent,
    now=None
):
    if type(session_id) is not str or _SESSION_ID_PATTERN.fullmatch(session_id) is None:
        raise AccessSessionError("session_id_invalid")
    request_id, ip_address, user_agent = _request_metadata(
        request_id, ip_address, user_agent
    )
    now_utc = _server_now(now)

    def operation(conn):
        row = _load_session_by_id(conn, session_id)
        state, _detail = _session_state(row, now_utc)
        if state == "missing":
            return "authentication_required"
        if state != "valid":
            conn.execute("delete from sessions where session_id = ?", (row["session_id"],))
            return "authentication_required"
        if not verify_password(password, row["password_hash"]):
            _write_audit(
                conn,
                entity_id=row["session_id"],
                action="auth.reauth",
                operator=row["username"],
                actor_user_id=row["user_id"],
                result="denied",
                reason=None,
                risk_level="medium",
                request_id=request_id,
                ip_address=ip_address,
                user_agent=user_agent,
                now=now_utc,
            )
            return False
        conn.execute(
            "update sessions set reauthenticated_at = ? where session_id = ?",
            (_format_time(now_utc), row["session_id"]),
        )
        _write_audit(
            conn,
            entity_id=row["session_id"],
            action="auth.reauth",
            operator=row["username"],
            actor_user_id=row["user_id"],
            result="success",
            reason=None,
            risk_level="medium",
            request_id=request_id,
            ip_address=ip_address,
            user_agent=user_agent,
            now=now_utc,
        )
        return True

    result = _run_immediate(db_path, operation)
    if result == "authentication_required":
        raise AccessSessionError("authentication_required")
    return result
