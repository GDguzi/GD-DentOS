"""账号管理：管理员增/改/停用用户、重置密码；任意登录用户自助改密。"""
import json
import sqlite3
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from starlette.concurrency import run_in_threadpool

from local_app import access_auth, access_service, auth
from local_app.db import audit_write as data_audit_write
from local_app.db import begin_immediate, connect, new_id
from local_app.passwords import hash_password, password_is_nonblank, verify_password
from local_app.timeutil import now_str


def create_users_router(db_path, access_v3=False):
    if type(access_v3) is not bool:
        raise ValueError("access_v3 must be a bool")
    if access_v3:
        return _create_access_v3_users_router(db_path)
    router = APIRouter()

    def _row(u):
        return {"id": u["id"], "username": u["username"], "display_name": u["display_name"],
                "role": u["role"], "is_active": bool(u["is_active"]), "created_at": u["created_at"]}

    @router.get("/api/users")
    def list_users():
        auth.require_perm("user.manage")
        with connect(db_path) as conn:
            rows = conn.execute(
                "select id, username, display_name, role, is_active, created_at from users order by created_at"
            ).fetchall()
        return {"users": [_row(r) for r in rows], "totalcount": len(rows)}

    @router.post("/api/users")
    def create_user(payload: dict):
        actor = auth.require_perm("user.manage")
        payload = payload or {}
        username = str(payload.get("username") or "").strip()
        password = payload.get("password")   # 原值:非 str 由 password_is_nonblank 判否
        display_name = str(payload.get("display_name") or "").strip()
        role = str(payload.get("role") or "reception").strip()
        if not username:
            raise HTTPException(status_code=400, detail="用户名必填")
        if not auth.password_is_nonblank(password):
            raise HTTPException(status_code=400, detail="密码不能为空")
        # 防提权：非 admin（仅持 user.manage）不能新建 admin 账号
        if role == "admin" and actor.get("role") != "admin":
            raise HTTPException(status_code=403, detail="只有管理员能新建管理员账号")
        with connect(db_path) as conn:
            if role not in auth.valid_roles(conn):
                raise HTTPException(status_code=400, detail="角色不合法")
            if conn.execute("select 1 from users where username = ?", (username,)).fetchone():
                raise HTTPException(status_code=409, detail="用户名已存在")
            uid = auth.create_user(conn, username, display_name, password, role=role)
            conn.commit()
            row = conn.execute(
                "select id, username, display_name, role, is_active, created_at from users where id = ?", (uid,)
            ).fetchone()
        return _row(row)

    @router.put("/api/users/{user_id}")
    def update_user(user_id: str, payload: dict):
        actor = auth.require_perm("user.manage")
        payload = payload or {}
        with connect(db_path) as conn:
            row = conn.execute("select * from users where id = ?", (user_id,)).fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="用户不存在")
            display_name = str(payload.get("display_name") or row["display_name"]).strip()
            role = str(payload.get("role") or row["role"]).strip()
            if role not in auth.valid_roles(conn):
                raise HTTPException(status_code=400, detail="角色不合法")
            is_active = payload.get("is_active", bool(row["is_active"]))
            is_active = 1 if is_active else 0
            # 不许停用/改自己的角色或状态，避免自锁
            if user_id == actor["id"] and (is_active == 0 or role != row["role"]):
                raise HTTPException(status_code=400, detail="不能停用或修改当前登录用户自己的角色/状态")
            # 防提权：非 admin（仅持 user.manage）不能把账号设为/改成 admin，也不能改 admin 账号
            if actor.get("role") != "admin" and (role == "admin" or row["role"] == "admin"):
                raise HTTPException(status_code=403, detail="只有管理员能管理管理员账号")
            conn.execute(
                "update users set display_name = ?, role = ?, is_active = ?, updated_at = ? where id = ?",
                (display_name, role, is_active, now_str(), user_id),
            )
            # 停用用户时清其会话
            if is_active == 0:
                conn.execute("delete from sessions where user_id = ?", (user_id,))
            conn.commit()
            row = conn.execute(
                "select id, username, display_name, role, is_active, created_at from users where id = ?", (user_id,)
            ).fetchone()
        return _row(row)

    @router.post("/api/users/{user_id}/reset-password")
    def reset_password(user_id: str, payload: dict):
        actor = auth.require_perm("user.manage")
        password = (payload or {}).get("password")
        if not auth.password_is_nonblank(password):
            raise HTTPException(status_code=400, detail="密码不能为空")
        with connect(db_path) as conn:
            target = conn.execute("select role from users where id = ?", (user_id,)).fetchone()
            if not target:
                raise HTTPException(status_code=404, detail="用户不存在")
            # 防提权：非 admin 不能重置 admin 账号的密码
            if target["role"] == "admin" and actor.get("role") != "admin":
                raise HTTPException(status_code=403, detail="只有管理员能重置管理员账号密码")
            target_name = conn.execute("select username from users where id = ?", (user_id,)).fetchone()
            conn.execute("update users set password_hash = ?, updated_at = ? where id = ?",
                         (auth.hash_password(password), now_str(), user_id))
            conn.execute("delete from sessions where user_id = ?", (user_id,))  # 改密后强制重登
            conn.commit()
        if target_name and target_name["username"] == "admin":
            # 重置 admin 密码同样让首启明文初始密码文件失效并删除
            (Path(db_path).parent / ".admin_initial_password").unlink(missing_ok=True)
        return {"ok": True}

    @router.post("/api/auth/change-password")
    def change_password(payload: dict):
        user = auth.require_login_user()
        payload = payload or {}
        old = payload.get("old_password")   # 原值:非 str 由 verify_password 判否 → 原密码错误
        new = payload.get("new_password")
        if not auth.password_is_nonblank(new):
            raise HTTPException(status_code=400, detail="新密码不能为空")
        with connect(db_path) as conn:
            row = conn.execute("select password_hash from users where id = ?", (user["id"],)).fetchone()
            if not row or not auth.verify_password(old, row["password_hash"]):
                raise HTTPException(status_code=400, detail="原密码错误")
            conn.execute("update users set password_hash = ?, updated_at = ? where id = ?",
                         (auth.hash_password(new), now_str(), user["id"]))
            conn.execute("delete from sessions where user_id = ?", (user["id"],))  # 改密后强制各端重登(同管理员重置)
            conn.commit()
        if user.get("username") == "admin":
            # admin 改密后首启明文初始密码文件即失效,立即删除——不让凭据常驻磁盘/跟着备份外泄
            (Path(db_path).parent / ".admin_initial_password").unlink(missing_ok=True)
        return {"ok": True}

    return router


_TARGET_CREATE_FIELDS = frozenset({
    "username", "display_name", "password", "staff_id",
})
_TARGET_UPDATE_FIELDS = frozenset({
    "username", "display_name", "is_active",
})
_TARGET_PASSWORD_FIELDS = frozenset({"password"})
_TARGET_FORBIDDEN_FIELDS = frozenset({
    "role", "roles", "role_keys", "primary_role",
    "is_system_admin", "account_kind",
})


def _target_payload(payload, allowed_fields):
    if type(payload) is not dict:
        raise HTTPException(status_code=400, detail="请求格式不正确")
    if any(key in payload for key in _TARGET_FORBIDDEN_FIELDS):
        raise HTTPException(status_code=400, detail="账号权限字段不能在此修改")
    if any(key not in allowed_fields for key in payload):
        raise HTTPException(status_code=400, detail="请求字段不合法")
    return payload


def _target_text(value, *, required, detail, max_length=None):
    if type(value) is not str:
        raise HTTPException(status_code=400, detail=detail)
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise HTTPException(status_code=400, detail=detail) from None
    if any(unicodedata.category(char) in ("Cc", "Cf") for char in value):
        raise HTTPException(status_code=400, detail=detail)
    value = value.strip()
    if required and not value:
        raise HTTPException(status_code=400, detail=detail)
    if max_length is not None and len(value) > max_length:
        raise HTTPException(status_code=400, detail=detail)
    return value


def _target_actor():
    from local_app.access_authorization import (
        AccessPrincipal,
        current_access_principal,
    )

    principal = current_access_principal()
    if not isinstance(principal, AccessPrincipal):
        raise HTTPException(status_code=401, detail="需要登录")
    return principal


def _target_locked_principal(conn, actor, permission):
    from local_app.access_authorization import (
        AccessAuthorizationError,
        load_access_principal_from_connection,
    )

    try:
        current = load_access_principal_from_connection(conn, actor.user_id)
    except AccessAuthorizationError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail=exc.code,
        ) from None
    if current is None:
        raise HTTPException(
            status_code=401,
            detail="authentication_required",
        )
    if (
        current.is_system_admin is not True
        and permission not in current.permissions
    ):
        raise HTTPException(status_code=403, detail="access_denied")
    return current


def _target_system_admin_actor():
    principal = _target_actor()
    if principal.is_system_admin is not True:
        raise HTTPException(status_code=403, detail="access_denied")
    return principal


def _target_system_admin_write_context():
    from local_app.access_authorization import require_recent_reauth

    require_recent_reauth()
    session = access_auth.current_access_session()
    request_now = access_auth.current_access_request_time()
    if type(session) is not dict or type(request_now) is not datetime:
        raise HTTPException(
            status_code=500,
            detail="system_admin_change_unavailable",
        )
    raw_reauth = session.get("reauthenticated_at")
    if type(raw_reauth) is datetime:
        reauthenticated_at = raw_reauth
    elif type(raw_reauth) is str:
        try:
            reauthenticated_at = datetime.strptime(
                raw_reauth,
                "%Y-%m-%dT%H:%M:%SZ",
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


async def _target_system_admin_payload(request):
    content_type = request.headers.get("content-type") or ""
    if content_type.split(";", 1)[0].strip().lower() != "application/json":
        raise HTTPException(status_code=400, detail="invalid_request")

    duplicate_key = False

    def _object_from_pairs(pairs):
        nonlocal duplicate_key
        value = {}
        for key, item in pairs:
            if key in value:
                duplicate_key = True
            value[key] = item
        return value

    try:
        payload = json.loads(
            await request.body(),
            object_pairs_hook=_object_from_pairs,
        )
    except (json.JSONDecodeError, UnicodeDecodeError, RecursionError):
        raise HTTPException(status_code=400, detail="invalid_request") from None
    if (
        duplicate_key
        or type(payload) is not dict
        or set(payload) != {"reason"}
    ):
        raise HTTPException(status_code=400, detail="invalid_request")
    reason = payload["reason"]
    if type(reason) is str:
        try:
            reason.encode("utf-8")
        except UnicodeEncodeError:
            raise HTTPException(status_code=400, detail="reason_invalid") from None
    return payload


def _raise_target_system_admin_service_error(exc):
    code = exc.code
    if code in ("reason_invalid", "reason_required"):
        raise HTTPException(status_code=400, detail=code) from None
    if code == "target_not_found":
        raise HTTPException(status_code=404, detail=code) from None
    if code == "actor_not_authorized":
        raise HTTPException(status_code=403, detail="access_denied") from None
    if code in ("reauth_invalid", "reauth_expired"):
        raise HTTPException(
            status_code=403,
            detail="recent_reauthentication_required",
        ) from None
    if code in (
        "target_not_eligible",
        "target_inactive",
        "target_already_admin",
        "target_not_admin",
        "cannot_revoke_self",
        "admin_floor_violation",
    ):
        raise HTTPException(status_code=409, detail=code) from None
    raise HTTPException(
        status_code=500,
        detail="system_admin_change_unavailable",
    ) from None


def _target_role_context(conn, staff_id):
    if staff_id is None:
        return None, []
    rows = conn.execute(
        "select role_key, is_primary from staff_role_assignments "
        "where staff_id = ? order by is_primary desc, role_key",
        (staff_id,),
    ).fetchall()
    role_keys = [row["role_key"] for row in rows]
    primary_role = next(
        (row["role_key"] for row in rows if row["is_primary"] == 1),
        None,
    )
    return primary_role, role_keys


def _target_user_dict(conn, row):
    primary_role, role_keys = _target_role_context(conn, row["staff_id"])
    return {
        "id": row["id"],
        "username": row["username"],
        "display_name": row["display_name"],
        "staff_id": row["staff_id"],
        "is_active": row["is_active"] == 1,
        "is_system_admin": row["is_system_admin"] == 1,
        "account_kind": row["account_kind"],
        "primary_role": primary_role,
        "role_keys": role_keys,
    }


def _target_user_row(conn, user_id):
    return conn.execute(
        "select id, username, display_name, staff_id, is_active, "
        "is_system_admin, account_kind, created_at, updated_at "
        "from users where id = ?",
        (user_id,),
    ).fetchone()


def _target_audit_json(value):
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _target_audit(conn, *, actor, user_id, action, old_json=None, new_json=None):
    data_audit_write(
        conn,
        "user",
        user_id,
        action,
        old_json=old_json,
        new_json=new_json,
        operator=actor.username,
        actor_user_id=actor.user_id,
    )


def _create_access_v3_users_router(db_path):
    """Target account handlers; never inspect or write the legacy users.role."""
    router = APIRouter()

    @router.get("/api/users")
    def list_users():
        _target_actor()
        with connect(db_path) as conn:
            rows = conn.execute(
                "select id, username, display_name, staff_id, is_active, "
                "is_system_admin, account_kind, created_at, updated_at "
                "from users order by created_at, id"
            ).fetchall()
            users = [_target_user_dict(conn, row) for row in rows]
        return {"users": users, "totalcount": len(users)}

    @router.post("/api/users")
    def create_user(payload: dict):
        actor = _target_actor()
        payload = _target_payload(payload or {}, _TARGET_CREATE_FIELDS)
        username = _target_text(
            payload.get("username"),
            required=True,
            detail="用户名必填",
            max_length=128,
        )
        staff_id = _target_text(
            payload.get("staff_id"),
            required=True,
            detail="员工必填",
        )
        password = payload.get("password")
        if not password_is_nonblank(password):
            raise HTTPException(status_code=400, detail="密码不能为空")

        with connect(db_path) as conn:
            begin_immediate(conn)
            actor = _target_locked_principal(conn, actor, "account.open")
            staff = conn.execute(
                "select name, employment_status from staff_members "
                "where staff_id = ?",
                (staff_id,),
            ).fetchone()
            if staff is None:
                raise HTTPException(status_code=404, detail="员工不存在")
            if staff["employment_status"] != "employed":
                raise HTTPException(status_code=400, detail="只有在职员工可以开通账号")
            if conn.execute(
                "select 1 from users where username = ?", (username,)
            ).fetchone():
                raise HTTPException(status_code=409, detail="用户名已存在")
            if conn.execute(
                "select 1 from users where staff_id = ?", (staff_id,)
            ).fetchone():
                raise HTTPException(status_code=409, detail="该员工已有账号")

            display_name_value = payload.get("display_name", staff["name"])
            display_name = _target_text(
                display_name_value,
                required=False,
                detail="姓名不合法",
            )
            user_id = new_id("user")
            timestamp = now_str()
            try:
                conn.execute(
                    "insert into users(id, username, display_name, password_hash, "
                    "staff_id, is_active, is_system_admin, account_kind, "
                    "created_at, updated_at) values (?, ?, ?, ?, ?, 1, 0, "
                    "'staff', ?, ?)",
                    (
                        user_id,
                        username,
                        display_name,
                        hash_password(password),
                        staff_id,
                        timestamp,
                        timestamp,
                    ),
                )
            except sqlite3.IntegrityError:
                raise HTTPException(
                    status_code=409,
                    detail="用户名或员工账号已存在",
                ) from None
            conn.commit()
            row = _target_user_row(conn, user_id)
            result = _target_user_dict(conn, row)
        return result

    @router.put("/api/users/{user_id}")
    def update_user(user_id: str, payload: dict):
        actor = _target_actor()
        payload = _target_payload(payload or {}, _TARGET_UPDATE_FIELDS)
        with connect(db_path) as conn:
            begin_immediate(conn)
            actor = _target_locked_principal(conn, actor, "account.open")
            row = _target_user_row(conn, user_id)
            if row is None:
                raise HTTPException(status_code=404, detail="用户不存在")
            if row["is_system_admin"] == 1 and actor.is_system_admin is not True:
                raise HTTPException(status_code=403, detail="只有系统管理员能管理系统管理员账号")

            username = row["username"]
            if "username" in payload:
                username = _target_text(
                    payload["username"],
                    required=True,
                    detail="用户名必填",
                    max_length=128,
                )
            display_name = row["display_name"]
            if "display_name" in payload:
                display_name = _target_text(
                    payload["display_name"],
                    required=False,
                    detail="姓名不合法",
                )
            is_active = row["is_active"]
            if "is_active" in payload:
                if type(payload["is_active"]) is not bool:
                    raise HTTPException(status_code=400, detail="账号状态不合法")
                is_active = 1 if payload["is_active"] else 0
            if user_id == actor.user_id and is_active == 0:
                raise HTTPException(status_code=400, detail="不能停用当前登录账号")
            if (
                row["is_active"] == 0
                and is_active == 1
                and row["account_kind"] == "staff"
            ):
                staff = conn.execute(
                    "select employment_status from staff_members "
                    "where staff_id = ?",
                    (row["staff_id"],),
                ).fetchone()
                if staff is None or staff["employment_status"] != "employed":
                    raise HTTPException(
                        status_code=409,
                        detail="只有在职员工账号可以启用",
                    )

            try:
                conn.execute(
                    "update users set username = ?, display_name = ?, "
                    "is_active = ?, updated_at = ? where id = ?",
                    (username, display_name, is_active, now_str(), user_id),
                )
            except sqlite3.IntegrityError:
                raise HTTPException(status_code=409, detail="用户名已存在") from None

            if (
                row["is_active"] == 1
                and row["is_system_admin"] == 1
                and is_active == 0
            ):
                regular_admins = conn.execute(
                    "select count(*) from users where is_active = 1 "
                    "and is_system_admin = 1 "
                    "and account_kind != 'break_glass'"
                ).fetchone()[0]
                if regular_admins < 1:
                    raise HTTPException(
                        status_code=409,
                        detail="至少保留一个常规系统管理员",
                    )

            if is_active == 0:
                conn.execute("delete from sessions where user_id = ?", (user_id,))
            if is_active != row["is_active"]:
                action = (
                    "account.status.enable"
                    if is_active == 1
                    else "account.status.disable"
                )
                _target_audit(
                    conn,
                    actor=actor,
                    user_id=user_id,
                    action=action,
                    old_json=_target_audit_json({"is_active": row["is_active"] == 1}),
                    new_json=_target_audit_json({"is_active": is_active == 1}),
                )
            conn.commit()
            result = _target_user_dict(conn, _target_user_row(conn, user_id))
        return result

    @router.post("/api/users/{user_id}/reset-password")
    def reset_password(user_id: str, payload: dict):
        actor = _target_actor()
        payload = _target_payload(payload or {}, _TARGET_PASSWORD_FIELDS)
        password = payload.get("password")
        if not password_is_nonblank(password):
            raise HTTPException(status_code=400, detail="密码不能为空")
        with connect(db_path) as conn:
            begin_immediate(conn)
            actor = _target_locked_principal(conn, actor, "account.security")
            row = _target_user_row(conn, user_id)
            if row is None:
                raise HTTPException(status_code=404, detail="用户不存在")
            if row["is_system_admin"] == 1 and actor.is_system_admin is not True:
                raise HTTPException(status_code=403, detail="只有系统管理员能重置系统管理员密码")
            conn.execute(
                "update users set password_hash = ?, updated_at = ? where id = ?",
                (hash_password(password), now_str(), user_id),
            )
            conn.execute("delete from sessions where user_id = ?", (user_id,))
            _target_audit(
                conn,
                actor=actor,
                user_id=user_id,
                action="account.password.reset",
            )
            conn.commit()
        return {"ok": True}

    @router.post("/api/users/{user_id}/system-admin/grant")
    async def grant_system_admin(user_id: str, request: Request):
        actor = _target_system_admin_actor()
        reauthenticated_at, request_now = _target_system_admin_write_context()
        payload = await _target_system_admin_payload(request)
        try:
            await run_in_threadpool(
                access_service.grant_system_admin,
                db_path,
                actor_user_id=actor.user_id,
                target_user_id=user_id,
                reason=payload["reason"],
                reauthenticated_at=reauthenticated_at,
                now=request_now,
            )
        except access_service.AccessServiceError as exc:
            _raise_target_system_admin_service_error(exc)
        return {"ok": True, "user_id": user_id, "is_system_admin": True}

    @router.post("/api/users/{user_id}/system-admin/revoke")
    async def revoke_system_admin(user_id: str, request: Request):
        actor = _target_system_admin_actor()
        reauthenticated_at, request_now = _target_system_admin_write_context()
        payload = await _target_system_admin_payload(request)
        try:
            await run_in_threadpool(
                access_service.revoke_system_admin,
                db_path,
                actor_user_id=actor.user_id,
                target_user_id=user_id,
                reason=payload["reason"],
                reauthenticated_at=reauthenticated_at,
                now=request_now,
            )
        except access_service.AccessServiceError as exc:
            _raise_target_system_admin_service_error(exc)
        return {"ok": True, "user_id": user_id, "is_system_admin": False}

    @router.post("/api/auth/change-password")
    def change_password(payload: dict):
        actor = _target_actor()
        if type(payload) is not dict:
            raise HTTPException(status_code=400, detail="请求格式不正确")
        if any(key not in {"old_password", "new_password"} for key in payload):
            raise HTTPException(status_code=400, detail="请求字段不合法")
        old_password = payload.get("old_password")
        new_password = payload.get("new_password")
        if not password_is_nonblank(new_password):
            raise HTTPException(status_code=400, detail="新密码不能为空")
        with connect(db_path) as conn:
            begin_immediate(conn)
            row = conn.execute(
                "select password_hash from users where id = ?",
                (actor.user_id,),
            ).fetchone()
            if row is None or not verify_password(old_password, row["password_hash"]):
                raise HTTPException(status_code=400, detail="原密码错误")
            conn.execute(
                "update users set password_hash = ?, updated_at = ? where id = ?",
                (hash_password(new_password), now_str(), actor.user_id),
            )
            conn.execute(
                "delete from sessions where user_id = ?", (actor.user_id,)
            )
            _target_audit(
                conn,
                actor=actor,
                user_id=actor.user_id,
                action="account.password.change",
            )
            conn.commit()
        return {"ok": True}

    return router
