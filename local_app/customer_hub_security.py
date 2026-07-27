"""Shared Access V3 guards for customer-hub transactional handlers."""
from fastapi import HTTPException

from local_app.access_authorization import (
    AccessAuthorizationError,
    AccessPrincipal,
    current_access_principal,
    load_access_principal_from_connection,
)


def request_human_actor():
    principal = current_access_principal()
    if not isinstance(principal, AccessPrincipal):
        raise HTTPException(status_code=401, detail="authentication_required")
    return principal


def require_locked_human_actor(conn, actor, permissions):
    try:
        current = load_access_principal_from_connection(conn, actor.user_id)
    except AccessAuthorizationError as exc:
        raise HTTPException(
            status_code=exc.status_code, detail=exc.code
        ) from None
    if current is None:
        raise HTTPException(status_code=403, detail="access_denied")
    required = tuple(permissions)
    if current.is_system_admin is not True and not all(
        permission in current.permissions for permission in required
    ):
        raise HTTPException(status_code=403, detail="access_denied")
    return current


def audit_actor_id(principal):
    return principal.user_id
