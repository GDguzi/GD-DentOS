"""角色权限矩阵：读取/保存「角色×权限点」勾选。

仅 role.manage 权限者(默认只有 admin，DEFAULT_ROLE_PERMS 未给任何预置角色此权限)可读写。
权限点清单是代码常量(auth.PERMISSION_DEFS)；本接口只增删 role_permissions 里的勾选关系。
admin 列代码层永远全通过(auth.role_perms 返回 ['*'])，不入表、不可改。

无缓存：权限随会话经 user_by_token 每请求从 role_permissions 现查，保存后对其他用户**下次请求即生效**。
"""
import sqlite3
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException

from local_app import auth
from local_app import access_auth, access_policy, access_service
from local_app.db import connect


_TARGET_BAD_REQUEST_CODES = frozenset({
    "reason_invalid",
    "reason_required",
    "permissions_invalid",
    "permission_invalid",
    "permission_duplicate",
    "permission_not_assignable",
    "permission_unknown",
    "permission_dependency_missing",
    "role_invalid",
    "role_not_configurable",
    "role_not_found",
})


def _target_system_admin():
    from local_app.access_authorization import (
        AccessPrincipal,
        current_access_principal,
    )

    principal = current_access_principal()
    if not isinstance(principal, AccessPrincipal):
        raise HTTPException(status_code=401, detail="authentication_required")
    if principal.is_system_admin is not True:
        raise HTTPException(status_code=403, detail="access_denied")
    return principal


def _target_write_context():
    from local_app.access_authorization import require_recent_reauth

    require_recent_reauth()
    session = access_auth.current_access_session()
    request_now = access_auth.current_access_request_time()
    if type(session) is not dict or type(request_now) is not datetime:
        raise HTTPException(status_code=500, detail="role_permissions_unavailable")
    raw_reauth = session.get("reauthenticated_at")
    if type(raw_reauth) is datetime:
        reauthenticated_at = raw_reauth
    elif type(raw_reauth) is str:
        try:
            reauthenticated_at = datetime.strptime(
                raw_reauth, "%Y-%m-%dT%H:%M:%SZ"
            ).replace(tzinfo=timezone.utc)
        except (ValueError, OverflowError):
            raise HTTPException(
                status_code=403,
                detail="recent_reauthentication_required",
            ) from None
    else:
        raise HTTPException(
            status_code=403,
            detail="recent_reauthentication_required",
        )
    return reauthenticated_at, request_now


def _raise_target_service_error(exc):
    code = exc.code
    if code in _TARGET_BAD_REQUEST_CODES:
        raise HTTPException(status_code=400, detail=code) from None
    if code == "actor_not_authorized":
        raise HTTPException(status_code=403, detail="access_denied") from None
    if code in ("reauth_invalid", "reauth_expired"):
        raise HTTPException(
            status_code=403,
            detail="recent_reauthentication_required",
        ) from None
    if code == "role_permissions_stale":
        raise HTTPException(status_code=409, detail=code) from None
    raise HTTPException(
        status_code=500,
        detail="role_permissions_unavailable",
    ) from None


def create_role_permissions_router(db_path, access_v3=False):
    router = APIRouter()

    @router.get("/api/role-permissions")
    def get_matrix():
        if access_v3:
            _target_system_admin()
            try:
                with connect(db_path) as conn:
                    assigned_staff_counts = {
                        row["role_key"]: row["assigned_staff_count"]
                        for row in conn.execute(
                            "select a.role_key, count(distinct a.staff_id) "
                            "as assigned_staff_count "
                            "from staff_role_assignments a "
                            "join staff_members s on s.staff_id = a.staff_id "
                            "where s.employment_status = 'employed' "
                            "group by a.role_key"
                        )
                    }
                    roles = [
                        {
                            "role_key": row["role_key"],
                            "name": row["name"],
                            "is_system": bool(row["is_system"]),
                            "assigned_staff_count": assigned_staff_counts.get(
                                row["role_key"], 0
                            ),
                        }
                        for row in conn.execute(
                            "select role_key, name, is_system from roles "
                            "where role_key != ? order by sort, role_key",
                            ("admin",),
                        )
                    ]
                    granted = {}
                    for row in conn.execute(
                        "select role_key, perm_key from role_permissions "
                        "where role_key != ?",
                        ("admin",),
                    ):
                        if row["perm_key"] in access_policy.ALL_PERMISSIONS:
                            granted.setdefault(row["role_key"], set()).add(
                                row["perm_key"]
                            )
            except sqlite3.Error:
                raise HTTPException(
                    status_code=500,
                    detail="role_permissions_unavailable",
                ) from None

            perms = [
                {
                    **dict(item),
                    "dependencies": list(
                        access_policy.PERMISSION_DEPENDENCIES.get(item["key"], ())
                    ),
                }
                for item in access_policy.PERMISSION_UI_DEFINITIONS
            ]
            matrix = {
                role["role_key"]: {
                    item["key"]: item["key"] in granted.get(
                        role["role_key"], set()
                    )
                    for item in perms
                }
                for role in roles
            }
            system_admin_capabilities = [
                {"key": key, "label": label, "assignable": False}
                for key, label in access_policy.SYSTEM_ADMIN_CAPABILITIES.items()
            ]
            return {
                "roles": roles,
                "perms": perms,
                "matrix": matrix,
                "system_admin_capabilities": system_admin_capabilities,
            }

        auth.require_perm("role.manage")
        with connect(db_path) as conn:
            roles = [{"role_key": r["role_key"], "name": r["name"], "is_system": bool(r["is_system"])}
                     for r in conn.execute(
                         "select role_key, name, is_system from roles order by sort, role_key")]
            granted = {}
            for r in conn.execute("select role_key, perm_key from role_permissions"):
                granted.setdefault(r["role_key"], set()).add(r["perm_key"])
        perms = [{"key": k, "module": m, "label": l} for k, m, l in auth.PERMISSION_DEFS]
        matrix = {}
        for role in roles:
            rk = role["role_key"]
            if rk == "admin":
                matrix[rk] = {p["key"]: True for p in perms}   # admin 全通过(只读展示，前端锁定)
            else:
                g = granted.get(rk, set())
                matrix[rk] = {p["key"]: (p["key"] in g) for p in perms}
        return {"roles": roles, "perms": perms, "matrix": matrix}

    @router.put("/api/role-permissions")
    def save_matrix(payload: dict):
        if access_v3:
            principal = _target_system_admin()
            reauthenticated_at, request_now = _target_write_context()
            if (
                type(payload) is not dict
                or set(payload) != {
                    "role_key", "permissions", "expected_permissions", "reason",
                }
            ):
                raise HTTPException(status_code=400, detail="invalid_request")
            try:
                result = access_service.replace_role_permissions(
                    db_path,
                    actor_user_id=principal.user_id,
                    role_key=payload["role_key"],
                    permissions=payload["permissions"],
                    expected_permissions=payload["expected_permissions"],
                    reason=payload["reason"],
                    reauthenticated_at=reauthenticated_at,
                    now=request_now,
                )
            except access_service.AccessServiceError as exc:
                _raise_target_service_error(exc)
            return {"ok": True, **result}

        auth.require_perm("role.manage")
        payload = payload or {}
        matrix = payload.get("matrix")
        if not isinstance(matrix, dict):
            raise HTTPException(status_code=400, detail="matrix 必填且需为对象")
        with connect(db_path) as conn:
            role_keys = {r["role_key"] for r in conn.execute("select role_key from roles")}
            for rk, perms in matrix.items():
                if rk not in role_keys or rk == "admin":
                    continue   # 未知角色跳过；admin 全通过靠代码，不可改
                # perms 接受 {perm_key: bool} 或 [perm_key, ...]；过滤未知权限点
                if isinstance(perms, dict):
                    keys = [k for k, v in perms.items() if v]
                else:
                    keys = list(perms or [])
                keys = [k for k in keys if k in auth.PERMISSIONS]
                conn.execute("delete from role_permissions where role_key = ?", (rk,))
                for k in keys:
                    conn.execute(
                        "insert into role_permissions(role_key, perm_key) values (?, ?)", (rk, k))
            conn.commit()
        return {"ok": True}

    return router
