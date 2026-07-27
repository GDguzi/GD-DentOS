"""配台人员管理：医生/护士/咨询师/助理，本地录入，供处置配台下拉 + 绩效统计。
本地新建，软删(active=0)，不与原系统同步表重叠。"""
import json
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Body, HTTPException

from local_app.timeutil import now_str
from local_app.auth import (create_user, password_is_nonblank, require_perm,
                            valid_roles)
from local_app.db import begin_immediate, connect, new_id

# 诊所岗位集：配台4 + 前台/收银员/主任/管理员/客服/助理2。
# staff_members 升级为统一员工表；配台下拉仍按医生/护士/咨询师/助理筛(前端 STAFF_ROLES)。
# 按权限树蓝本补齐岗位(增 客服/助理2)。去「技师」岗位,技工职能并入助理/前台。
ROLES = ("医生", "护士", "咨询师", "助理", "前台", "收银员", "主任", "管理员", "客服", "助理2")




def create_staff_router(db_path, access_v3=False):
    router = APIRouter()
    db_path = Path(db_path)

    # P2-31 员工档案补全字段(随 name/role/note 一起读写)
    profile_fields = ("job_no", "phone", "sex", "id_card", "title", "license_no", "department")
    # 公开选人接口只回轻量字段(隐私铁律身份证/手机/执业证等敏感档案不进无守卫的下拉接口)
    light_cols = "staff_id, name, role, roles"
    # 受 staff.manage 守的明细接口才回全档案
    full_cols = "staff_id, name, role, note, roles, " + ", ".join(profile_fields)

    def _wrap_roles(payload, role):
        """payload.roles(list 或逗号串) → 逗号包裹 ',医生,助理,'；缺省回退到主岗位 [role]。"""
        raw = payload.get("roles")
        if isinstance(raw, list):
            items = [str(x).strip() for x in raw if str(x).strip()]
        elif isinstance(raw, str) and raw.strip():
            items = [x.strip() for x in raw.replace("，", ",").split(",") if x.strip()]
        else:
            items = [role] if role else []
        return ("," + ",".join(items) + ",") if items else ""

    def _roles_list(r):
        return [x for x in (r["roles"] or "").strip(",").split(",") if x]

    def _row_light(r):
        """选人下拉用：只 staff_id/name/role/roles，无任何隐私档案。"""
        return {"staff_id": r["staff_id"], "name": r["name"], "role": r["role"], "roles": _roles_list(r)}

    def _row_to_member(r):
        """员工管理明细：含档案字段(仅 staff.manage 接口返回)。"""
        m = {"staff_id": r["staff_id"], "name": r["name"], "role": r["role"], "note": r["note"],
             "roles": _roles_list(r)}
        for f in profile_fields:
            m[f] = r[f]
        return m

    def _target_principal(permission=None):
        from local_app.access_authorization import (
            AccessPrincipal,
            current_access_principal,
            require_access,
        )

        if permission is not None:
            return require_access(permission)
        principal = current_access_principal()
        if not isinstance(principal, AccessPrincipal):
            raise HTTPException(status_code=401, detail="authentication_required")
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

    def _target_text(payload, field, *, required=False):
        value = payload.get(field, "")
        if type(value) is not str:
            raise HTTPException(status_code=400, detail=f"{field}格式不合法")
        try:
            value.encode("utf-8")
        except UnicodeEncodeError:
            raise HTTPException(status_code=400, detail=f"{field}格式不合法")
        value = value.strip()
        if required and not value:
            raise HTTPException(status_code=400, detail=f"{field}不能为空")
        return value

    def _target_role_catalog(conn):
        rows = conn.execute(
            "select role_key, name from roles order by role_key"
        ).fetchall()
        return {row["role_key"]: row["name"] for row in rows}

    def _target_resolve_role(token, catalog):
        if type(token) is not str:
            raise HTTPException(status_code=400, detail="岗位格式不合法")
        try:
            token.encode("utf-8")
        except UnicodeEncodeError:
            raise HTTPException(status_code=400, detail="岗位格式不合法")
        matches = {key for key, name in catalog.items() if token in (key, name)}
        if len(matches) != 1:
            raise HTTPException(status_code=400, detail="岗位不存在")
        return matches.pop()

    def _target_role_plan(conn, payload):
        catalog = _target_role_catalog(conn)
        explicit = "primary_role" in payload or "role_keys" in payload
        if explicit:
            primary = payload.get("primary_role")
            raw_roles = payload.get("role_keys")
            if type(primary) is not str or type(raw_roles) is not list:
                raise HTTPException(status_code=400, detail="岗位参数不完整")
            if any(type(role_key) is not str for role_key in raw_roles):
                raise HTTPException(status_code=400, detail="岗位格式不合法")
            try:
                primary.encode("utf-8")
                for role_key in raw_roles:
                    role_key.encode("utf-8")
            except UnicodeEncodeError:
                raise HTTPException(status_code=400, detail="岗位格式不合法")
            if (
                not primary
                or not raw_roles
                or primary not in catalog
                or any(role_key not in catalog for role_key in raw_roles)
                or len(set(raw_roles)) != len(raw_roles)
                or primary not in raw_roles
            ):
                raise HTTPException(status_code=400, detail="岗位不存在或重复")
            role_keys = raw_roles
        else:
            primary = _target_resolve_role(payload.get("role"), catalog)
            raw_roles = payload.get("roles")
            if isinstance(raw_roles, str):
                raw_roles = [
                    item.strip()
                    for item in raw_roles.replace("，", ",").split(",")
                    if item.strip()
                ]
            elif raw_roles is None:
                raw_roles = [payload.get("role")]
            if type(raw_roles) is not list:
                raise HTTPException(status_code=400, detail="岗位格式不合法")
            resolved = [_target_resolve_role(item, catalog) for item in raw_roles]
            role_keys = list(dict.fromkeys(resolved))
            if primary not in role_keys:
                role_keys.append(primary)
        ordered = [primary] + sorted(key for key in role_keys if key != primary)
        return primary, ordered, catalog

    def _target_roles_for_staff(conn, staff_id, catalog=None):
        catalog = catalog or _target_role_catalog(conn)
        rows = conn.execute(
            "select role_key, is_primary from staff_role_assignments "
            "where staff_id = ? order by is_primary desc, role_key",
            (staff_id,),
        ).fetchall()
        role_keys = [row["role_key"] for row in rows]
        primary = next(
            (row["role_key"] for row in rows if row["is_primary"] == 1),
            None,
        )
        return {
            "primary_role": primary,
            "role_keys": role_keys,
            "role": catalog.get(primary) if primary else None,
            "roles": [catalog.get(key, key) for key in role_keys],
        }

    def _target_profile(payload):
        return {
            "name": _target_text(payload, "name", required=True),
            "note": _target_text(payload, "note"),
            **{field: _target_text(payload, field) for field in profile_fields},
        }

    def _target_member(conn, row, catalog=None):
        member = {
            "staff_id": row["staff_id"],
            "name": row["name"],
            "note": row["note"],
            **{field: row[field] for field in profile_fields},
            "employment_status": row["employment_status"],
            "left_at": row["left_at"],
            "left_reason": row["left_reason"],
        }
        member.update(_target_roles_for_staff(conn, row["staff_id"], catalog))
        return member

    def _target_write_assignments(conn, staff_id, primary, role_keys, now):
        for role_key in role_keys:
            conn.execute(
                "insert into staff_role_assignments"
                "(staff_id, role_key, is_primary, created_at) values (?, ?, ?, ?)",
                (staff_id, role_key, int(role_key == primary), now),
            )

    @router.get("/api/staff-members")
    def list_staff(role: str = ""):
        """选人下拉数据源(全站可读)：只回 staff_id/name/role/roles。
        可按角色筛(主岗位 role 命中 或 多岗位 roles 含该角色)。**不回身份证/手机/证号等隐私档案**。"""
        if access_v3:
            _target_principal()
            requested_role = (role or "").strip()
            with connect(db_path) as conn:
                catalog = _target_role_catalog(conn)
                if requested_role:
                    try:
                        role_key = _target_resolve_role(requested_role, catalog)
                    except HTTPException:
                        return {"members": [], "totalcount": 0}
                    rows = conn.execute(
                        "select s.staff_id, s.name from staff_members s "
                        "where s.employment_status = 'employed' and exists ("
                        "select 1 from staff_role_assignments a "
                        "where a.staff_id = s.staff_id and a.role_key = ?) "
                        "order by s.name, s.staff_id",
                        (role_key,),
                    ).fetchall()
                else:
                    rows = conn.execute(
                        "select staff_id, name from staff_members "
                        "where employment_status = 'employed' "
                        "order by name, staff_id"
                    ).fetchall()
                members = []
                for row in rows:
                    member = {"staff_id": row["staff_id"], "name": row["name"]}
                    member.update(
                        _target_roles_for_staff(conn, row["staff_id"], catalog)
                    )
                    members.append(member)
            return {"members": members, "totalcount": len(members)}
        role = (role or "").strip()
        with connect(db_path) as conn:
            if role:
                rows = conn.execute(
                    f"select {light_cols} from staff_members "
                    "where active = 1 and (role = ? or roles like ?) order by name",
                    (role, f"%,{role},%"),
                ).fetchall()
            else:
                rows = conn.execute(
                    f"select {light_cols} from staff_members "
                    "where active = 1 order by role, name"
                ).fetchall()
        members = [_row_light(r) for r in rows]
        return {"members": members, "totalcount": len(members)}

    @router.post("/api/staff-members")
    def create_staff(payload: dict):
        if access_v3:
            actor = _target_principal("staff.edit")
            payload = payload or {}
            profile = _target_profile(payload)
            now = now_str()
            staff_id = new_id("staff")
            with connect(db_path) as conn:
                begin_immediate(conn)
                actor = _target_locked_principal(
                    conn,
                    actor,
                    "staff.edit",
                )
                primary, role_keys, catalog = _target_role_plan(conn, payload)
                fields = ("name", "note") + profile_fields
                conn.execute(
                    "insert into staff_members(staff_id, "
                    + ", ".join(fields)
                    + ", employment_status, created_at, updated_at) values ("
                    + ", ".join(["?"] * (len(fields) + 4))
                    + ")",
                    (
                        staff_id,
                        *[profile[field] for field in fields],
                        "employed",
                        now,
                        now,
                    ),
                )
                _target_write_assignments(
                    conn, staff_id, primary, role_keys, now
                )
                row = conn.execute(
                    "select * from staff_members where staff_id = ?", (staff_id,)
                ).fetchone()
                member = _target_member(conn, row, catalog)
                conn.commit()
            return member
        require_perm("staff.manage")
        payload = payload or {}
        name = str(payload.get("name") or "").strip()
        role = str(payload.get("role") or "").strip()
        note = str(payload.get("note") or "").strip()
        if not name:
            raise HTTPException(status_code=400, detail="姓名不能为空")
        if role not in ROLES:
            raise HTTPException(status_code=400, detail="角色须为：" + "/".join(ROLES))
        prof = {f: str(payload.get(f) or "").strip() for f in profile_fields}
        roles = _wrap_roles(payload, role)
        now = now_str()
        staff_id = new_id("local-staff")
        cols = "staff_id, name, role, note, roles, " + ", ".join(profile_fields) + ", active, created_at, updated_at"
        placeholders = ", ".join(["?"] * (5 + len(profile_fields))) + ", 1, ?, ?"
        with connect(db_path) as conn:
            conn.execute(
                f"insert into staff_members({cols}) values ({placeholders})",
                (staff_id, name, role, note, roles, *[prof[f] for f in profile_fields], now, now),
            )
            conn.commit()
        return {"staff_id": staff_id, "name": name, "role": role, "note": note,
                "roles": [x for x in roles.strip(",").split(",") if x], **prof}

    @router.put("/api/staff-members/{staff_id}")
    def update_staff(staff_id: str, payload: dict):
        if access_v3:
            actor = _target_principal("staff.edit")
            payload = payload or {}
            profile = _target_profile(payload)
            now = now_str()
            with connect(db_path) as conn:
                begin_immediate(conn)
                actor = _target_locked_principal(
                    conn,
                    actor,
                    "staff.edit",
                )
                if not conn.execute(
                    "select 1 from staff_members where staff_id = ? "
                    "and employment_status = 'employed'",
                    (staff_id,),
                ).fetchone():
                    raise HTTPException(status_code=404, detail="人员不存在")
                primary, role_keys, catalog = _target_role_plan(conn, payload)
                fields = ("name", "note") + profile_fields
                conn.execute(
                    "update staff_members set "
                    + ", ".join(f"{field} = ?" for field in fields)
                    + ", updated_at = ? where staff_id = ?",
                    (*[profile[field] for field in fields], now, staff_id),
                )
                conn.execute(
                    "delete from staff_role_assignments where staff_id = ?",
                    (staff_id,),
                )
                _target_write_assignments(
                    conn, staff_id, primary, role_keys, now
                )
                row = conn.execute(
                    "select * from staff_members where staff_id = ?", (staff_id,)
                ).fetchone()
                member = _target_member(conn, row, catalog)
                conn.commit()
            return member
        require_perm("staff.manage")
        payload = payload or {}
        name = str(payload.get("name") or "").strip()
        role = str(payload.get("role") or "").strip()
        note = str(payload.get("note") or "").strip()
        if not name:
            raise HTTPException(status_code=400, detail="姓名不能为空")
        if role not in ROLES:
            raise HTTPException(status_code=400, detail="角色须为：" + "/".join(ROLES))
        prof = {f: str(payload.get(f) or "").strip() for f in profile_fields}
        roles = _wrap_roles(payload, role)
        now = now_str()
        set_cols = ("name = ?, role = ?, note = ?, roles = ?, "
                    + ", ".join(f"{f} = ?" for f in profile_fields) + ", updated_at = ?")
        with connect(db_path) as conn:
            # 只改在职人员；软删(active=0)行 PUT 返回 404，防 stale 编辑把新人信息写进已删行
            if not conn.execute("select 1 from staff_members where staff_id = ? and active = 1", (staff_id,)).fetchone():
                raise HTTPException(status_code=404, detail="人员不存在")
            conn.execute(
                f"update staff_members set {set_cols} where staff_id = ?",
                (name, role, note, roles, *[prof[f] for f in profile_fields], now, staff_id),
            )
            conn.commit()
        return {"staff_id": staff_id, "name": name, "role": role, "note": note,
                "roles": [x for x in roles.strip(",").split(",") if x], **prof}

    @router.delete("/api/staff-members/{staff_id}")
    def delete_staff(staff_id: str, payload: dict = None):
        if access_v3:
            actor = _target_principal("staff.employment")
            if actor.staff_id == staff_id:
                raise HTTPException(status_code=409, detail="不能将自己设为离职")
            payload = payload or {}
            reason = _target_text(payload, "reason") or "人员离职"
            now = now_str()
            with connect(db_path) as conn:
                begin_immediate(conn)
                actor = _target_locked_principal(
                    conn,
                    actor,
                    "staff.employment",
                )
                member = conn.execute(
                    "select employment_status, left_at, left_reason "
                    "from staff_members where staff_id = ?",
                    (staff_id,),
                ).fetchone()
                if not member or member["employment_status"] != "employed":
                    raise HTTPException(status_code=404, detail="人员不存在")
                old_json = json.dumps(
                    {
                        "employment_status": member["employment_status"],
                        "left_at": member["left_at"],
                        "left_reason": member["left_reason"],
                    },
                    ensure_ascii=False,
                )
                conn.execute(
                    "update staff_members set employment_status = 'left', "
                    "left_at = ?, left_reason = ?, updated_at = ? where staff_id = ?",
                    (now, reason, now, staff_id),
                )
                users = conn.execute(
                    "select id from users where staff_id = ?", (staff_id,)
                ).fetchall()
                for user in users:
                    conn.execute(
                        "update users set is_active = 0, updated_at = ? where id = ?",
                        (now, user["id"]),
                    )
                    conn.execute(
                        "delete from sessions where user_id = ?", (user["id"],)
                    )
                regular_admins = conn.execute(
                    "select count(*) as n from users "
                    "where is_active = 1 and is_system_admin = 1 "
                    "and account_kind != 'break_glass'"
                ).fetchone()["n"]
                if regular_admins < 1:
                    raise HTTPException(
                        status_code=409,
                        detail="必须保留至少一名常规系统管理员",
                    )
                new_json = json.dumps(
                    {
                        "employment_status": "left",
                        "left_at": now,
                        "left_reason": reason,
                    },
                    ensure_ascii=False,
                )
                conn.execute(
                    "insert into audit_logs(entity_type, entity_id, action, "
                    "old_json, new_json, operator, created_at, actor_user_id, "
                    "result, reason, risk_level) "
                    "values ('staff', ?, 'staff.employment.left', ?, ?, ?, ?, ?, "
                    "'success', ?, 'high')",
                    (
                        staff_id,
                        old_json,
                        new_json,
                        actor.username,
                        now,
                        actor.user_id,
                        reason,
                    ),
                )
                conn.commit()
            return {
                "staff_id": staff_id,
                "deleted": True,
                "departed": True,
                "account_disabled": bool(users),
            }
        require_perm("staff.manage")
        """软删(active=0)，并级联停用其登录账号(取消员工=即时收回登录)。历史处置配台姓名是快照不受影响。"""
        now = now_str()
        account_disabled = False
        with connect(db_path) as conn:
            if not conn.execute("select 1 from staff_members where staff_id = ?", (staff_id,)).fetchone():
                raise HTTPException(status_code=404, detail="人员不存在")
            conn.execute(
                "update staff_members set active = 0, updated_at = ? where staff_id = ?",
                (now, staff_id),
            )
            # 级联：停用关联登录账号 + 清会话(取消员工后立刻无法再登录)
            u = conn.execute(
                "select id from users where coalesce(staff_id, '') = ? and is_active = 1", (staff_id,)
            ).fetchone()
            if u:
                conn.execute("update users set is_active = 0, updated_at = ? where id = ?", (now, u["id"]))
                conn.execute("delete from sessions where user_id = ?", (u["id"],))
                account_disabled = True
            conn.commit()
        return {"staff_id": staff_id, "deleted": True, "account_disabled": account_disabled}

    @router.post("/api/staff-members/{staff_id}/reinstate")
    def reinstate_staff(staff_id: str, payload: Any = Body(default=None)):
        actor = _target_principal("staff.employment")
        if type(payload) is not dict or set(payload) != {"reason"}:
            raise HTTPException(status_code=400, detail="invalid_request")
        reason = _target_text(payload, "reason", required=True)
        now = now_str()
        with connect(db_path) as conn:
            begin_immediate(conn)
            actor = _target_locked_principal(
                conn,
                actor,
                "staff.employment",
            )
            member = conn.execute(
                "select employment_status, left_at, left_reason "
                "from staff_members where staff_id = ?",
                (staff_id,),
            ).fetchone()
            if member is None:
                raise HTTPException(status_code=404, detail="人员不存在")
            if member["employment_status"] != "left":
                raise HTTPException(status_code=409, detail="人员当前不是离职状态")
            old_json = json.dumps(
                {
                    "employment_status": member["employment_status"],
                    "left_at": member["left_at"],
                    "left_reason": member["left_reason"],
                },
                ensure_ascii=False,
            )
            conn.execute(
                "update staff_members set employment_status = 'employed', "
                "left_at = NULL, left_reason = '', updated_at = ? "
                "where staff_id = ?",
                (now, staff_id),
            )
            new_json = json.dumps(
                {
                    "employment_status": "employed",
                    "left_at": None,
                    "left_reason": "",
                },
                ensure_ascii=False,
            )
            conn.execute(
                "insert into audit_logs(entity_type, entity_id, action, "
                "old_json, new_json, operator, created_at, actor_user_id, "
                "result, reason, risk_level) "
                "values ('staff', ?, 'staff.employment.reinstate', ?, ?, ?, ?, "
                "?, 'success', ?, 'high')",
                (
                    staff_id,
                    old_json,
                    new_json,
                    actor.username,
                    now,
                    actor.user_id,
                    reason,
                ),
            )
            conn.commit()
        return {
            "ok": True,
            "staff_id": staff_id,
            "employment_status": "employed",
            "account_reenabled": False,
        }

    @router.get("/api/staff-admin/list")
    def staff_admin_list():
        """员工 + 登录账号聚合(统一员工管理页用)：每个在职员工 + 其账号摘要(未开通=None)。"""
        if access_v3:
            _target_principal("staff.view")
            with connect(db_path) as conn:
                catalog = _target_role_catalog(conn)
                rows = conn.execute(
                    "select * from staff_members order by name, staff_id"
                ).fetchall()
                accounts = {
                    row["staff_id"]: {
                        "user_id": row["id"],
                        "username": row["username"],
                        "is_active": bool(row["is_active"]),
                        "is_system_admin": bool(row["is_system_admin"]),
                        "account_kind": row["account_kind"],
                    }
                    for row in conn.execute(
                        "select id, username, staff_id, is_active, "
                        "is_system_admin, account_kind from users "
                        "where staff_id is not null"
                    )
                }
                members = []
                for row in rows:
                    member = _target_member(conn, row, catalog)
                    member["account"] = accounts.get(row["staff_id"])
                    members.append(member)
            return {"members": members, "totalcount": len(members)}
        require_perm("staff.manage")
        with connect(db_path) as conn:
            rows = conn.execute(
                f"select {full_cols} from staff_members where active = 1 order by role, name"
            ).fetchall()
            accounts = {}
            for u in conn.execute(
                "select id, username, role, is_active, staff_id from users "
                "where coalesce(staff_id, '') != ''"
            ):
                accounts[u["staff_id"]] = {
                    "user_id": u["id"], "username": u["username"],
                    "account_role": u["role"], "is_active": bool(u["is_active"])}
        members = []
        for r in rows:
            m = _row_to_member(r)
            m["account"] = accounts.get(r["staff_id"])   # None = 未开通登录
            members.append(m)
        return {"members": members, "totalcount": len(members)}

    @router.post("/api/staff-members/{staff_id}/account")
    def open_staff_account(staff_id: str, payload: dict):
        """给配台人员开通登录账号(写 users.staff_id 关联)。一个员工只开一个账号。"""
        if access_v3:
            actor = _target_principal("account.open")
            from local_app.passwords import (
                hash_password as target_hash_password,
                password_is_nonblank as target_password_is_nonblank,
            )

            payload = payload or {}
            forbidden_account_fields = {
                "role",
                "roles",
                "primary_role",
                "role_keys",
                "is_system_admin",
                "account_kind",
            }
            if any(field in payload for field in forbidden_account_fields):
                raise HTTPException(status_code=400, detail="不允许指定账号权限")
            username = _target_text(payload, "username", required=True)
            if (
                len(username) > 128
                or any(ord(char) < 32 or ord(char) == 127 for char in username)
            ):
                raise HTTPException(status_code=400, detail="用户名格式不合法")
            password = payload.get("password")
            if not target_password_is_nonblank(password):
                raise HTTPException(status_code=400, detail="密码不能为空")
            password_hash = target_hash_password(password)
            now = now_str()
            user_id = new_id("user")
            with connect(db_path) as conn:
                begin_immediate(conn)
                actor = _target_locked_principal(
                    conn,
                    actor,
                    "account.open",
                )
                member = conn.execute(
                    "select name from staff_members where staff_id = ? "
                    "and employment_status = 'employed'",
                    (staff_id,),
                ).fetchone()
                if not member:
                    raise HTTPException(status_code=404, detail="人员不存在")
                if conn.execute(
                    "select 1 from users where staff_id = ?", (staff_id,)
                ).fetchone():
                    raise HTTPException(status_code=409, detail="该员工已开通账号")
                if conn.execute(
                    "select 1 from users where username = ?", (username,)
                ).fetchone():
                    raise HTTPException(status_code=409, detail="用户名已存在")
                conn.execute(
                    "insert into users(id, username, display_name, password_hash, "
                    "staff_id, is_active, is_system_admin, account_kind, "
                    "created_at, updated_at) "
                    "values (?, ?, ?, ?, ?, 1, 0, 'staff', ?, ?)",
                    (
                        user_id,
                        username,
                        member["name"],
                        password_hash,
                        staff_id,
                        now,
                        now,
                    ),
                )
                conn.commit()
            return {
                "ok": True,
                "user_id": user_id,
                "username": username,
                "staff_id": staff_id,
                "account_kind": "staff",
                "is_system_admin": False,
            }
        actor = require_perm("staff.manage")
        payload = payload or {}
        username = str(payload.get("username") or "").strip()
        password = payload.get("password")   # 原值:非 str 由 password_is_nonblank 判否
        role = str(payload.get("role") or "reception").strip()
        if not username:
            raise HTTPException(status_code=400, detail="用户名必填")
        if not password_is_nonblank(password):
            raise HTTPException(status_code=400, detail="密码不能为空")
        # 防提权：非 admin（仅持 staff.manage）不能借开通员工账号建 admin 账号（与 users.py 同口径）
        if role == "admin" and actor.get("role") != "admin":
            raise HTTPException(status_code=403, detail="只有管理员能新建管理员账号")
        with connect(db_path) as conn:
            staff = conn.execute(
                "select name from staff_members where staff_id = ? and active = 1", (staff_id,)
            ).fetchone()
            if not staff:
                raise HTTPException(status_code=404, detail="人员不存在")
            if role not in valid_roles(conn):
                raise HTTPException(status_code=400, detail="角色不合法")
            if conn.execute(
                "select 1 from users where coalesce(staff_id, '') = ?", (staff_id,)
            ).fetchone():
                raise HTTPException(status_code=409, detail="该员工已开通账号")
            if conn.execute("select 1 from users where username = ?", (username,)).fetchone():
                raise HTTPException(status_code=409, detail="用户名已存在")
            uid = create_user(conn, username, staff["name"], password, role=role, staff_id=staff_id)
            conn.commit()
        return {"ok": True, "user_id": uid, "username": username,
                "account_role": role, "staff_id": staff_id}

    return router
