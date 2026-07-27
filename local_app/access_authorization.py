"""Central route authorization for the personnel-access v3 runtime."""
from __future__ import annotations

import contextvars
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

from fastapi import HTTPException, Request

from local_app import access_auth
from local_app import access_service
from local_app import access_session_service as session_service
from local_app.access_policy import (
    ALL_PERMISSIONS,
    POLICY_TYPES,
    RECENT_REAUTH_ROUTE_KEYS,
    ROUTE_POLICY_BY_KEY,
    RoutePolicy,
)


@dataclass(frozen=True)
class AccessPrincipal:
    user_id: str
    username: str
    display_name: str
    staff_id: str | None
    is_system_admin: bool
    primary_role: str | None
    role_keys: tuple[str, ...]
    permissions: frozenset[str]
    role_permissions: frozenset[str]


class AccessAuthorizationError(Exception):
    """Sanitized authorization failure used by pure policy checks."""

    def __init__(self, status_code, code):
        self.status_code = status_code
        self.code = code
        super().__init__(code)

    def __str__(self):
        return self.code


_current_access_principal = contextvars.ContextVar(
    "current_access_principal", default=None
)
_current_authorized_policy = contextvars.ContextVar(
    "current_authorized_policy", default=None
)
_legacy_access_bridge = contextvars.ContextVar(
    "legacy_access_bridge", default=None
)
_access_authorizer_active = contextvars.ContextVar(
    "access_authorizer_active", default=False
)

_LEGACY_PERMISSION_ALIASES = {
    "patient.profile.view": "patient.view",
    "return_visit.view": "patient.view",
    "communication.view": "communication.view",
    "communication.manage": "communication.manage",
    "communication.delete": "communication.manage",
    "call_record.view": "call.view",
    "call_record.manage": "call.manage",
    "call_record.delete": "call.manage",
    "billing.view": "billing.view",
    "report.finance.view": "report.view",
    "report.arrears.view": "report.view",
    "report.cashier.view": "report.view",
    "report.performance.view": "report.view",
    "report.operations.view": "report.view",
    "audit.view": "audit.view",
    "appointment.edit": "patient.edit",
    "appointment.status": "patient.edit",
    "appointment.cancel": "patient.edit",
}


def current_access_principal():
    return _current_access_principal.get()


def current_authorized_policy():
    return _current_authorized_policy.get()


def access_authorizer_active():
    """Read-only request flag used by narrow legacy compatibility helpers."""
    return _access_authorizer_active.get() is True


class LegacyAccessBridgeMiddleware:
    """Request-scoped bridge installed only by the pre-v3 ``setup_auth`` path."""

    def __init__(self, app, *, legacy_require_perm, legacy_has_perm):
        self.app = app
        self.legacy_require_perm = legacy_require_perm
        self.legacy_has_perm = legacy_has_perm

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            return await self.app(scope, receive, send)
        token = _legacy_access_bridge.set(
            (self.legacy_require_perm, self.legacy_has_perm)
        )
        try:
            return await self.app(scope, receive, send)
        finally:
            _legacy_access_bridge.reset(token)


def _failure(status_code, code):
    raise AccessAuthorizationError(status_code, code)


def _http_failure(status_code, code):
    raise HTTPException(status_code=status_code, detail=code)


def _is_int_one(value):
    return type(value) is int and value == 1


def _open_readonly(db_path):
    try:
        path = Path(db_path)
        if not path.is_file():
            _failure(500, "authorization_unavailable")
        uri = "file:%s?mode=ro" % quote(str(path))
        conn = sqlite3.connect(uri, uri=True)
        conn.row_factory = sqlite3.Row
        conn.execute("pragma query_only = on")
        return conn
    except AccessAuthorizationError:
        raise
    except Exception:
        _failure(500, "authorization_unavailable")


def _load_role_context(conn, staff_id):
    rows = conn.execute(
        "select role_key, is_primary from staff_role_assignments "
        "where staff_id = ?",
        (staff_id,),
    ).fetchall()
    role_keys = []
    primary_role = None
    seen = set()
    for role in rows:
        role_key = role["role_key"]
        is_primary = role["is_primary"]
        try:
            role_key_valid = (
                type(role_key) is str
                and bool(role_key)
                and role_key == role_key.strip()
                and bool(role_key.encode("utf-8"))
            )
        except (UnicodeEncodeError, ValueError):
            role_key_valid = False
        if (
            not role_key_valid
            or role_key in seen
            or type(is_primary) is not int
            or is_primary not in (0, 1)
        ):
            _failure(500, "authorization_unavailable")
        seen.add(role_key)
        role_keys.append(role_key)
        if is_primary == 1:
            if primary_role is not None:
                _failure(500, "authorization_unavailable")
            primary_role = role_key

    secondary_roles = sorted(
        role_key for role_key in role_keys if role_key != primary_role
    )
    if primary_role is None:
        return None, tuple(secondary_roles)
    return primary_role, (primary_role,) + tuple(secondary_roles)


def load_access_principal_from_connection(conn, user_id):
    """Load one principal from the caller's current database snapshot."""
    if type(user_id) is not str or not user_id.strip():
        return None
    try:
        row = conn.execute(
            "select u.id, u.username, u.display_name, u.staff_id, u.is_active, "
            "u.is_system_admin, u.account_kind, sm.employment_status "
            "from users u left join staff_members sm on sm.staff_id = u.staff_id "
            "where u.id = ?",
            (user_id,),
        ).fetchone()
        if row is None or not _is_int_one(row["is_active"]):
            return None

        is_system_admin = _is_int_one(row["is_system_admin"])
        staff_id = row["staff_id"]
        if staff_id is None:
            if not is_system_admin or row["account_kind"] not in (
                "independent_admin",
                "break_glass",
            ):
                return None
            primary_role = None
            role_keys = ()
        elif (
            row["account_kind"] != "staff"
            or row["employment_status"] != "employed"
        ):
            return None
        else:
            primary_role, role_keys = _load_role_context(conn, staff_id)

        role_permissions = frozenset(
            access_service.effective_permissions(conn, row["id"])
        )
        permissions = ALL_PERMISSIONS if is_system_admin else role_permissions
        return AccessPrincipal(
            user_id=row["id"],
            username=row["username"],
            display_name=row["display_name"],
            staff_id=staff_id,
            is_system_admin=is_system_admin,
            primary_role=primary_role,
            role_keys=role_keys,
            permissions=frozenset(permissions),
            role_permissions=role_permissions,
        )
    except AccessAuthorizationError:
        raise
    except Exception:
        _failure(500, "authorization_unavailable")


def load_access_principal(db_path, user_id):
    """Load one current principal without trusting the session's public user fields."""
    if type(user_id) is not str or not user_id.strip():
        return None

    conn = _open_readonly(db_path)
    try:
        conn.execute("begin")
        return load_access_principal_from_connection(conn, user_id)
    except AccessAuthorizationError:
        raise
    except Exception:
        _failure(500, "authorization_unavailable")
    finally:
        try:
            conn.close()
        except sqlite3.Error:
            pass


_ANONYMOUS_POLICY_TYPES = frozenset({"PUBLIC", "PUBLIC_AUTH"})
_LOGIN_ONLY_POLICY_TYPES = frozenset(
    {"SELF_AUTH", "AUTHENTICATED_COMMON", "AUTHENTICATED_DIRECTORY"}
)
_KEYED_POLICY_TYPES = frozenset(
    {"PERMISSION", "ALL_OF", "SECTIONED", "CONDITIONAL"}
)


def _validated_policy(policy):
    if not isinstance(policy, RoutePolicy):
        _failure(500, "access_policy_invalid")
    if (
        type(policy.method) is not str
        or not policy.method
        or type(policy.path) is not str
        or not policy.path.startswith("/")
        or policy.policy_type not in POLICY_TYPES
        or type(policy.permissions) is not tuple
    ):
        _failure(500, "access_policy_invalid")

    permissions = policy.permissions
    if (
        len(permissions) != len(set(permissions))
        or any(
            type(permission) is not str or permission not in ALL_PERMISSIONS
            for permission in permissions
        )
    ):
        _failure(500, "access_policy_invalid")

    if policy.policy_type == "PERMISSION":
        valid_shape = len(permissions) == 1
    elif policy.policy_type in ("ALL_OF", "SECTIONED", "CONDITIONAL"):
        valid_shape = bool(permissions)
    else:
        valid_shape = not permissions
    if not valid_shape:
        _failure(500, "access_policy_invalid")
    return policy.policy_type, permissions


def authorize_policy(policy, principal):
    """Pure fail-closed policy decision; returns None only when authorized."""
    policy_type, required = _validated_policy(policy)
    if policy_type in _ANONYMOUS_POLICY_TYPES:
        return None
    if principal is None:
        _failure(401, "authentication_required")
    if not isinstance(principal, AccessPrincipal):
        _failure(500, "access_principal_invalid")

    if policy_type == "SYSTEM_ADMIN":
        if principal.is_system_admin is True:
            return None
        _failure(403, "access_denied")

    if principal.is_system_admin is True:
        return None
    if policy_type in _LOGIN_ONLY_POLICY_TYPES:
        return None
    if policy_type == "PERMISSION":
        allowed = required[0] in principal.permissions
    elif policy_type == "ALL_OF":
        allowed = all(item in principal.permissions for item in required)
    elif policy_type == "SECTIONED":
        if policy.method == "GET" and policy.path == "/api/stats":
            allowed = any(item in principal.permissions for item in required)
        else:
            allowed = required[0] in principal.permissions
    elif policy_type == "CONDITIONAL":
        allowed = any(item in principal.permissions for item in required)
    else:
        _failure(500, "access_policy_invalid")
    if not allowed:
        _failure(403, "access_denied")
    return None


def _route_policy(request):
    route = request.scope.get("route")
    path = getattr(route, "path_format", None)
    if type(path) is not str:
        path = getattr(route, "path", None)
    method = request.method
    if type(path) is not str or type(method) is not str:
        _failure(500, "access_policy_missing")
    if path != "/api" and not path.startswith("/api/"):
        return None
    target = ROUTE_POLICY_BY_KEY.get((method.upper(), path))
    if target is None:
        _failure(500, "access_policy_missing")
    return target


def build_access_authorizer(db_path):
    """Build the global FastAPI yield dependency for one explicit database."""

    async def authorize_request(request: Request):
        active_token = _access_authorizer_active.set(True)
        try:
            try:
                target_policy = _route_policy(request)
                principal = None
                if (
                    target_policy is not None
                    and target_policy.policy_type not in _ANONYMOUS_POLICY_TYPES
                ):
                    public_user = access_auth.current_access_user()
                    user_id = (
                        public_user.get("id")
                        if type(public_user) is dict
                        else None
                    )
                    principal = load_access_principal(db_path, user_id)
                if target_policy is not None:
                    authorize_policy(target_policy, principal)
            except AccessAuthorizationError as exc:
                raise HTTPException(
                    status_code=exc.status_code, detail=exc.code
                ) from None

            principal_token = _current_access_principal.set(principal)
            policy_token = _current_authorized_policy.set(target_policy)
            try:
                if (
                    target_policy is not None
                    and (target_policy.method, target_policy.path)
                    in RECENT_REAUTH_ROUTE_KEYS
                ):
                    require_recent_reauth()
                yield principal
            finally:
                _current_authorized_policy.reset(policy_token)
                _current_access_principal.reset(principal_token)
        finally:
            _access_authorizer_active.reset(active_token)

    return authorize_request


def has_access(permission):
    if type(permission) is not str or permission not in ALL_PERMISSIONS:
        return False
    principal = current_access_principal()
    if (
        _access_authorizer_active.get()
        or current_authorized_policy() is not None
        or principal is not None
    ):
        if not isinstance(principal, AccessPrincipal):
            return False
        return (
            principal.is_system_admin is True
            or permission in principal.permissions
        )

    bridge = _legacy_access_bridge.get()
    legacy_permission = _LEGACY_PERMISSION_ALIASES.get(permission)
    if bridge is None or legacy_permission is None:
        return False
    try:
        return bridge[1](legacy_permission) is True
    except Exception:
        return False


def require_access(permission):
    if type(permission) is not str or permission not in ALL_PERMISSIONS:
        _http_failure(500, "access_permission_invalid")
    principal = current_access_principal()
    if (
        _access_authorizer_active.get()
        or current_authorized_policy() is not None
        or principal is not None
    ):
        if not isinstance(principal, AccessPrincipal):
            _http_failure(401, "authentication_required")
        if (
            principal.is_system_admin is not True
            and permission not in principal.permissions
        ):
            _http_failure(403, "access_denied")
        return principal

    bridge = _legacy_access_bridge.get()
    if bridge is None:
        _http_failure(401, "authentication_required")
    legacy_permission = _LEGACY_PERMISSION_ALIASES.get(permission)
    if legacy_permission is None:
        _http_failure(403, "access_denied")
    return bridge[0](legacy_permission)


def require_recent_reauth():
    principal = current_access_principal()
    if not isinstance(principal, AccessPrincipal):
        _http_failure(401, "authentication_required")
    session = access_auth.current_access_session()
    if type(session) is not dict or not session:
        _http_failure(401, "authentication_required")
    request_now = access_auth.current_access_request_time()
    if request_now is None:
        _http_failure(500, "authorization_unavailable")
    if not session_service.is_reauth_fresh(
        session.get("reauthenticated_at"),
        created_at=session.get("created_at"),
        now=request_now,
    ):
        _http_failure(403, "recent_reauthentication_required")
    return principal
