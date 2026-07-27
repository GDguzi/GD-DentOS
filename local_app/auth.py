"""账号系统：用户 / 会话 / 密码 + 登录态(contextvar) + 操作归属。

- 账号登录取代原共享局域网口令做门槛；本机也需登录(否则操作记不到人)。
- 会话默认 7 天，「记住此设备」90 天；持久 cookie `dl_session`(关浏览器不失效)。
- 密码 pbkdf2_hmac(标准库，不加依赖)，存 `salt$hexdigest`。
- 当前用户存 contextvars，由「原生 ASGI 中间件」按 cookie 设置(能传到线程池里的同步端点；
  @app.middleware 那种 BaseHTTPMiddleware 会断 contextvar 传播，故不用)。
- audit_operator() 无会话时默认 'local_user'(保持既有行为/测试不破)，登录后为该用户名。
"""
import contextvars
import datetime as dt
import logging
import os
import secrets

from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

from local_app.timeutil import bj_now, now_str
from local_app.db import (DEFAULT_DB_PATH, _audit_cell, connect,
                          audit_write as _db_audit_write)
from local_app.passwords import (hash_password, password_is_nonblank,
                                 verify_password)

SESSION_TTL_DAYS = 7
SESSION_TTL_DAYS_REMEMBER = 90
COOKIE_NAME = "dl_session"
ROLES = ("admin", "reception", "doctor", "assistant")

# 当前请求登录用户(dict 或 None)，由 ASGI 中间件设置
_current_user = contextvars.ContextVar("current_user", default=None)
# 登录网关是否在本请求生效(AuthContextMiddleware 按 require_login 设置)。
# 用途：require_perm/has_perm 的"未登录放行"只对免登录模式合法；若网关开着还出现
# 未登录请求穿到权限检查(理应在中间件就被 401 拦掉)，说明网关被绕过——记告警不静默。
_login_gate = contextvars.ContextVar("login_gate", default=False)

_OPEN_PATHS = {"/__login", "/api/health"}
_OPEN_PREFIXES = ("/api/auth/login", "/api/auth/logout")


# ---------- 用户/会话 DB 操作 ----------
def create_user(conn, username, display_name, password, role="reception", staff_id=""):
    if not password_is_nonblank(password):   # fail-closed:绕过 HTTP 的内部/CLI 调用也不许建空密码账号
        raise ValueError("密码不能为空")
    if role not in valid_roles(conn):
        role = "reception"
    uid = "user-" + secrets.token_hex(8)
    now = now_str()
    conn.execute(
        "insert into users(id, username, display_name, password_hash, role, is_active, "
        "staff_id, created_at, updated_at) values (?, ?, ?, ?, ?, 1, ?, ?, ?)",
        (uid, username, display_name or username, hash_password(password), role,
         staff_id or "", now, now),
    )
    return uid


def authenticate(conn, username, password):
    row = conn.execute(
        "select id, password_hash, is_active from users where username = ?", (username,)
    ).fetchone()
    if not row or not row["is_active"]:
        return None
    if not verify_password(password, row["password_hash"]):
        return None
    return row["id"]


def create_session(conn, user_id, remember=False):
    token = secrets.token_hex(32)
    ttl_days = SESSION_TTL_DAYS_REMEMBER if remember else SESSION_TTL_DAYS
    expires = (bj_now() + dt.timedelta(days=ttl_days)).strftime("%Y-%m-%d %H:%M:%S")
    conn.execute(
        "insert into sessions(token, user_id, created_at, expires_at) values (?, ?, ?, ?)",
        (token, user_id, now_str(), expires),
    )
    return token, ttl_days


def user_by_token(conn, token):
    if not token:
        return None
    row = conn.execute(
        "select u.id, u.username, u.display_name, u.role, u.is_active, u.staff_id, s.expires_at "
        "from sessions s join users u on u.id = s.user_id where s.token = ?",
        (token,),
    ).fetchone()
    if not row or not row["is_active"]:
        return None
    if row["expires_at"] and row["expires_at"] < now_str():
        return None
    return {"id": row["id"], "username": row["username"],
            "display_name": row["display_name"], "role": row["role"],
            "staff_id": row["staff_id"], "permissions": role_perms(conn, row["role"])}


def delete_session(conn, token):
    if token:
        conn.execute("delete from sessions where token = ?", (token,))


# ---------- 登录态访问 ----------
def current_user():
    target_mode, target_user = _target_user_context()
    if target_mode:
        return target_user
    return _current_user.get()


def _target_user_context():
    """Return (is_target_request, compatibility_user_or_none)."""
    from local_app.access_authorization import (
        AccessPrincipal,
        access_authorizer_active,
        current_access_principal,
    )
    if not access_authorizer_active():
        return False, None
    principal = current_access_principal()
    if not isinstance(principal, AccessPrincipal):
        return True, None
    from local_app.access_policy import ALL_PERMISSIONS
    permissions = sorted(
        permission
        for permission in principal.permissions
        if permission in ALL_PERMISSIONS
    )
    return True, {
        "id": principal.user_id,
        "username": principal.username,
        "display_name": principal.display_name,
        "staff_id": principal.staff_id,
        "role": principal.primary_role,
        "roles": list(principal.role_keys),
        "permissions": permissions,
        "is_system_admin": principal.is_system_admin is True,
    }


def audit_operator():
    """审计 operator：登录则用户名，否则沿用 'local_user'(保持既有行为)。"""
    target_mode, target_user = _target_user_context()
    if target_mode:
        if target_user is None:
            raise HTTPException(status_code=401, detail="需要登录")
        return target_user["username"]
    u = _current_user.get()
    return u["username"] if u else "local_user"


def audit_write(conn, entity_type, entity_id, action, *,
                old_json=None, new_json=None, operator=None, created_at=None):
    """审计流水请求态入口（铁律：关键操作必须留痕）。不 commit，事务边界归调用方。

    old_json/new_json 接收调用方已序列化好的 JSON 字符串（stable_json/json.dumps
    口径由调用方定），这里不二次序列化。operator 缺省 audit_operator()；
    created_at 缺省 now_str()，事务里已取统一时间戳 now 的调用点应显式传入，
    保持同事务多行时间一致。SQL 本体在数据层 _db_audit_write(备份等独立工具直用那个,
    不背 FastAPI 依赖)；将来加 ip/request_id 等审计字段改那里。
    """
    target_mode, target_user = _target_user_context()
    if target_mode:
        if target_user is None:
            raise HTTPException(status_code=401, detail="需要登录")
        resolved_operator = (
            target_user["username"] if operator is None else operator
        )
        _db_audit_write(
            conn,
            entity_type,
            entity_id,
            action,
            old_json=old_json,
            new_json=new_json,
            operator=resolved_operator,
            created_at=created_at,
            actor_user_id=target_user["id"],
        )
    else:
        resolved_operator = audit_operator() if operator is None else operator
        _db_audit_write(
            conn,
            entity_type,
            entity_id,
            action,
            old_json=old_json,
            new_json=new_json,
            operator=resolved_operator,
            created_at=created_at,
        )


def require_admin():
    """管理员守卫：非 admin 抛 403。

    v3 目标模式下同样硬闸：路由策略层是第一道门,这里是第二道——患者合并/储值退现等
    "仅管理员"操作,普通角色即便勾了对应权限点也 403。若产品层面要放开某个入口给非管理员,
    须显式把该调用点改成 require_perm 并拍板,不得靠守卫静默失效。

    v3 只认 is_system_admin：角色名"admin"不再顶替管理员身份。空库 v3 会种出一个零权限的
    遗留 admin 角色,若继续认它,任何拿到 staff.edit 的账号都能给自己勾上该角色越过本守卫
    (提权路径已实测复现)。与 access_service 既有口径一致——那边早已拒绝 role_keys=('admin',)。"""
    target_mode, target_user = _target_user_context()
    if target_mode:
        if target_user is None:
            raise HTTPException(status_code=401, detail="需要登录")
        if target_user.get("is_system_admin") is not True:
            raise HTTPException(status_code=403, detail="需要管理员权限")
        return target_user
    u = _current_user.get()
    if not u or u.get("role") != "admin":
        raise HTTPException(status_code=403, detail="需要管理员权限")
    return u


def require_login_user():
    """登录守卫：未登录抛 401。返回当前用户。"""
    u = current_user()
    if not u:
        raise HTTPException(status_code=401, detail="需要登录")
    return u


def require_system_admin():
    """独立系统管理员守卫（人员权限中心 v3）：只认严格的内建 bool True 或 int 1，
    role='admin'/permissions=['*']/显示名"管理员"/字符串'1'/浮点 1.0/自定义等值对象均不能顶替。"""
    u = require_login_user()
    flag = u.get("is_system_admin")
    if flag is not True and (type(flag) is not int or flag != 1):
        raise HTTPException(status_code=403, detail="需要系统管理员权限")
    return u


# ---------- 角色权限矩阵 (RBAC) ----------
# 权限点清单是代码常量(唯一真相源，开发随功能迭代定义)；role_permissions 表只存"角色勾了哪些 key"。
# admin 永远全通过(不入表)。前端权限矩阵按 module 分组渲染这份清单。
# (perm_key, module, label)
PERMISSION_DEFS = [
    ("patient.view",        "患者", "查看患者档案/列表"),
    ("patient.create",      "患者", "新建患者"),
    ("patient.edit",        "患者", "编辑患者档案"),
    ("patient.delete",      "患者", "删除/合并患者"),
    ("patient.export",      "患者", "导出患者(CSV)"),
    ("medical_record.view", "病历", "查看病历"),
    ("medical_record.edit", "病历", "写/改病历"),
    ("treatment.manage",    "病历", "处置开单/划价/作废"),
    ("lab_order.manage",    "病历", "技工单"),
    ("surgery.manage",      "病历", "手术记录"),
    ("oral_exam.manage",    "病历", "口腔检查"),
    ("consult.manage",      "病历", "咨询沟通"),
    ("consent.manage",      "病历", "知情同意书"),
    ("call.view",           "病历", "查看/回放通话录音"),
    ("call.manage",         "病历", "拨号/录音入库/标记成交"),
    ("communication.view",  "病历", "查看沟通记录(电话/微信)"),
    ("communication.manage", "病历", "新增/编辑沟通记录、传微信截图"),
    ("billing.view",        "财务", "查看账单/收费"),
    ("billing.pay",         "财务", "收款确认"),
    ("billing.refund",      "财务", "退费"),
    ("membership.manage",   "财务", "会员储值(充值/消费/退款)"),
    ("report.view",         "报表", "查看经营/财务报表"),
    ("data.export",         "报表", "全量数据导出(CSV)"),
    ("warehouse.view",      "库房", "查看库存"),
    ("warehouse.manage",    "库房", "出入库/盘点/采购"),
    ("sterilize.manage",    "消毒", "器械/送消单"),
    ("master_data.manage",  "配置", "处置项目库/字典/来源"),
    ("staff.manage",        "配置", "员工与账号管理"),
    ("settings.manage",     "配置", "全店设置"),
    ("user.manage",         "系统", "账号增删/重置密码"),
    ("role.manage",         "系统", "角色权限矩阵编辑"),
    ("audit.view",          "系统", "审计日志/版本"),
    ("backup.manage",       "系统", "数据备份"),
    ("sync.manage",         "系统", "同步操作"),
    ("recycle.manage",      "系统", "回收站入口(仅控前端子tab可见性,后端仍按 patient.view+本人/admin 还原)"),
]
PERMISSIONS = frozenset(k for k, _, _ in PERMISSION_DEFS)

# 旧权限键 → 新键别名(零回归：现网 routes 仍调旧键，归一到新键判定)
_PERM_ALIASES = {
    "billing_refund": "billing.refund",
    "data_export":    "data.export",
}

# 目标 v3 请求中，旧软分区 helper 只接受这组明确、安全的新权限映射；
# 未列出的旧键不猜测、不按同名自动放行。
_TARGET_HAS_PERM_ALIASES = {
    "patient.view": "patient.profile.view",
    "billing.view": "billing.view",
    "audit.view": "audit.view",
    "report.view": "report.finance.view",
    "communication.view": "communication.view",
    "call.view": "call_record.view",
}

# 预置角色(is_system=1，不可删，权限可在矩阵里改)。(role_key, name, sort)
# 按权限树蓝本补齐角色——新增 主任/收银员/客服/助理2 独立角色(不再把主任并到 admin)。
# 产品决策去掉「技师」独立角色,技工功能并入 助理/前台;旧库 technician 账号/技师员工
# 由 migrate_technician_role 一次性迁到 assistant/助理。升级用 ensure_preset_roles_present 幂等补缺。
PRESET_ROLES = [
    ("admin",      "管理员", 0),
    ("director",   "主任",   1),
    ("doctor",     "医生",   2),
    ("nurse",      "护士",   3),
    ("consultant", "咨询师", 4),
    ("reception",  "前台",   5),
    ("cashier",    "收银员", 6),
    ("assistant",  "助理",   8),
    ("assistant2", "助理2",  9),
    ("support",    "客服",   10),
]

# 各预置角色默认勾选(admin 不列，代码层全通过)。管理员可在权限矩阵页随时调整。
DEFAULT_ROLE_PERMS = {
    "doctor": {
        "patient.view", "patient.create", "patient.edit",
        "medical_record.view", "medical_record.edit",
        "treatment.manage", "lab_order.manage", "surgery.manage", "oral_exam.manage",
        "consult.manage", "consent.manage", "billing.view",
        "call.view", "call.manage",
        "communication.view", "communication.manage",
    },
    "nurse": {
        "patient.view", "medical_record.view", "treatment.manage",
        "sterilize.manage", "warehouse.view",
    },
    "reception": {  # 技工功能并入前台,加 lab_order.manage
        "patient.view", "patient.create", "patient.edit",
        "billing.view", "billing.pay", "membership.manage", "lab_order.manage",
        "call.view", "call.manage",   # 前台=电话沟通主力(录入人:前台)
        "communication.view", "communication.manage",
    },
    "consultant": {  # report.view 收归管理员(业绩/门诊报表只有院长可查)
        "patient.view", "patient.edit", "consult.manage", "billing.view",
        "call.view", "call.manage",
        "communication.view", "communication.manage",
    },
    "assistant": {  # 技工功能并入助理(lab_order/warehouse);尾巴拍板再补患者建档/编辑
        "patient.view", "patient.create", "patient.edit",
        "medical_record.view", "treatment.manage", "sterilize.manage",
        "lab_order.manage", "warehouse.view",
    },
    # 新增 5 类(可在权限矩阵随时调)
    "director": {   # 主任:全科临床 + 财务可见,不含系统/账号管理;报表收归 admin
        "patient.view", "patient.create", "patient.edit",
        "medical_record.view", "medical_record.edit",
        "treatment.manage", "lab_order.manage", "surgery.manage", "oral_exam.manage",
        "consult.manage", "consent.manage",
        "billing.view", "billing.pay", "billing.refund", "membership.manage",
        "warehouse.view", "sterilize.manage",
        "call.view", "call.manage",
        "communication.view", "communication.manage",
    },
    "cashier": {    # 收银员:收费/退费/储值;报表收归 admin
        "patient.view", "billing.view", "billing.pay", "billing.refund",
        "membership.manage",
    },
    "assistant2": {  # 助理2:同助理
        "patient.view", "patient.create", "patient.edit",
        "medical_record.view", "treatment.manage", "sterilize.manage",
        "lab_order.manage", "warehouse.view",
    },
    "support": {     # 客服:档案 + 咨询沟通;报表收归 admin
        "patient.view", "patient.create", "patient.edit", "consult.manage",
        "call.view", "call.manage",
        "communication.view", "communication.manage",
    },
}


def valid_roles(conn):
    """合法角色 = roles 表 role_key 集合；未种子化时回退预置角色，避免建账号被卡。"""
    rows = conn.execute("select role_key from roles").fetchall()
    return {r[0] for r in rows} or {r[0] for r in PRESET_ROLES}


def role_perms(conn, role):
    """该角色的权限 key 列表；admin 返回哨兵 ['*'](代码层全通过)。"""
    if role == "admin":
        return ["*"]
    rows = conn.execute("select perm_key from role_permissions where role_key = ?", (role,)).fetchall()
    return [r[0] for r in rows]


def require_perm(action):
    """细粒度权限守卫：在登录用户之间按角色判权。

    未登录(current_user 为 None)时放行——是否需要登录由登录网关(setup_auth 的 require_login)
    统一把关：生产 require_login=True，未登录请求在中间件就被 401 拦掉，根本到不了这里；
    本机免登录模式(require_login=False)视作可信全权，与 audit_operator 返回 'local_user' 一致。
    登录后则严格判权：admin(perms=['*']) 全通过，其余按角色权限点，缺权抛 403。

    v3 目标模式下：动作能映射到 v3 权限的(别名表优先,其次动作名本身就是合法
    v3 权限键)必须真查权限——这是回收站 patient.view 这类"二次门禁"的守卫;无 v3 对应物的
    动作放行,由路由策略层把关(每路由必有一条策略,有守卫测试锁定,放行不属兜底)。"""
    target_mode, target_user = _target_user_context()
    if target_mode:
        if target_user is None:
            raise HTTPException(status_code=401, detail="需要登录")
        mapped = _TARGET_HAS_PERM_ALIASES.get(action)
        if mapped is None:
            from local_app.access_policy import ALL_PERMISSIONS
            mapped = action if action in ALL_PERMISSIONS else None
        if mapped is not None and not (
            target_user.get("is_system_admin") is True
            or mapped in (target_user.get("permissions") or ())
        ):
            raise HTTPException(status_code=403, detail="当前角色无权执行此操作")
        return target_user
    u = _current_user.get()
    if u is None:
        if _login_gate.get():   # 网关开着还漏进未登录请求=网关被绕过,告警不静默(仍放行,保持既有契约)
            logging.getLogger("local_app").warning(
                "require_perm(%s) 在登录网关开启时收到未登录请求——登录网关疑似被绕过", action)
        return None
    action = _PERM_ALIASES.get(action, action)
    perms = u.get("permissions") or []
    if "*" in perms or action in perms:
        return u
    raise HTTPException(status_code=403, detail="当前角色无权执行此操作")


def has_perm(action):
    """非抛版权限判断,返回 True/False。口径与 require_perm 一致：未登录(本机免登录)视作可信全权→True；
    登录后 admin('*') 或含该权限点→True,否则 False。用于"有权才带某段数据、无权则省略"的软控制(如今日工作台审计摘要),
    区别于 require_perm 的"无权直接 403"。"""
    target_mode, target_user = _target_user_context()
    if target_mode:
        if target_user is None:
            return False
        target_permission = _TARGET_HAS_PERM_ALIASES.get(action)
        if target_permission is None:
            return False
        return (
            target_user["is_system_admin"] is True
            or target_permission in target_user["permissions"]
        )
    u = _current_user.get()
    if u is None:
        if _login_gate.get():   # 同 require_perm：网关开着不该有未登录请求到这里
            logging.getLogger("local_app").warning(
                "has_perm(%s) 在登录网关开启时收到未登录请求——登录网关疑似被绕过", action)
        return True
    action = _PERM_ALIASES.get(action, action)
    perms = u.get("permissions") or []
    return "*" in perms or action in perms


# ---------- 迁移函数转发壳 ----------
# Phase3 迁移机制归一:7 个 ensure_*/migrate_* 一次性升级的本体已搬到 local_app/migrations.py
# (schema_migrations 有序记账,run_local 启动统一 apply_migrations)。这里留一行转发壳,
# 因几十个测试/旧调用点按 auth.ensure_xxx 引用;函数级 import 避免 auth⇄migrations 循环。
# ensure_seed_admin 不属迁移(首启账号供给,含初始凭据落盘),本体仍在下方。

def ensure_seed_roles(db_path=DEFAULT_DB_PATH):
    from local_app.migrations import ensure_seed_roles as _f
    return _f(db_path)


def ensure_preset_roles_present(db_path=DEFAULT_DB_PATH):
    from local_app.migrations import ensure_preset_roles_present as _f
    return _f(db_path)


def migrate_technician_role(db_path=DEFAULT_DB_PATH):
    from local_app.migrations import migrate_technician_role as _f
    return _f(db_path)


def ensure_absorbed_lab_perms(db_path=DEFAULT_DB_PATH):
    from local_app.migrations import ensure_absorbed_lab_perms as _f
    return _f(db_path)


def ensure_assistant_patient_perms(db_path=DEFAULT_DB_PATH):
    from local_app.migrations import ensure_assistant_patient_perms as _f
    return _f(db_path)


def ensure_comm_call_perms(db_path=DEFAULT_DB_PATH):
    from local_app.migrations import ensure_comm_call_perms as _f
    return _f(db_path)


def ensure_report_perm_admin_only(db_path=DEFAULT_DB_PATH):
    from local_app.migrations import ensure_report_perm_admin_only as _f
    return _f(db_path)


def ensure_seed_admin(db_path=DEFAULT_DB_PATH):
    """首启无任何用户则建 admin，初始密码写 data/.admin_initial_password 并返回 (username, pw)；否则 None。"""
    from pathlib import Path
    with connect(db_path) as conn:
        if conn.execute("select count(*) from users").fetchone()[0]:
            return None
        # DENTAL_ADMIN_PASSWORD 可显式预设首启密码(演示与运维自动化);
        # 未设则生成 128bit 随机口令,打印到终端并存 .admin_initial_password。
        pw = os.environ.get("DENTAL_ADMIN_PASSWORD", "").strip() or secrets.token_hex(16)
        user_cols = {row[1] for row in conn.execute("pragma table_info(users)")}
        if "is_system_admin" in user_cols:
            # v3 混合形态(空库首启经 v3_bootstrap 建成/生产已迁移库):
            # 建独立系统管理员(破局账号)——legacy create_user 的 staff_id='' 写法
            # 违反 v3 users 的检查约束(trim(staff_id)<>'' 或 null),不能复用。
            uid = "user-" + secrets.token_hex(8)
            now = now_str()
            conn.execute(
                "insert into users(id, username, display_name, password_hash, role, "
                "is_active, staff_id, is_system_admin, account_kind, created_at, updated_at) "
                "values (?, 'admin', '管理员', ?, 'admin', 1, null, 1, 'independent_admin', ?, ?)",
                (uid, hash_password(pw), now, now),
            )
        else:
            create_user(conn, "admin", "管理员", pw, role="admin")
        conn.commit()
    p = Path(db_path).parent / ".admin_initial_password"
    p.write_text(f"admin / {pw}\n", encoding="utf-8")
    return ("admin", pw)


# ---------- ASGI 中间件(设当前用户 + 可选门禁) ----------
def _cookie_from_scope(scope, name):
    for k, v in scope.get("headers", []):
        if k == b"cookie":
            for part in v.decode("latin-1").split(";"):
                kk, _, vv = part.strip().partition("=")
                if kk == name:
                    return vv
    return None


class AuthContextMiddleware:
    """原生 ASGI 中间件：按 cookie 解析当前用户写 contextvar(传播到端点)；require_login 时拦截未登录。"""

    def __init__(self, app, db_path, require_login=False):
        self.app = app
        self.db_path = db_path
        self.require_login = require_login

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            return await self.app(scope, receive, send)
        token = _cookie_from_scope(scope, COOKIE_NAME)
        user = None
        if token:
            try:
                with connect(self.db_path) as conn:
                    user = user_by_token(conn, token)
            except Exception:
                user = None
        reset = _current_user.set(user)
        reset_gate = _login_gate.set(self.require_login)
        try:
            if self.require_login and user is None:
                path = scope.get("path", "")
                if not (path in _OPEN_PATHS or path.startswith(_OPEN_PREFIXES) or _is_login_asset(path)):
                    if path.startswith("/api/"):
                        resp = JSONResponse({"detail": "需要登录"}, status_code=401)
                    else:
                        # no-store:登录页与骨架页都禁缓存,防登出后浏览器回放缓存页
                        resp = HTMLResponse(render_login_html(self.db_path),
                                            headers={"Cache-Control": "no-store"})
                    return await resp(scope, receive, send)
            return await self.app(scope, receive, send)
        finally:
            _current_user.reset(reset)
            _login_gate.reset(reset_gate)


# ---------- 统一操作审计(中间件) ----------
# 凡是改数据的 API 调用(POST/PUT/DELETE/PATCH)成功后,自动补一条操作审计,保证"所有操作都留痕"
# (含库存/消毒等本来没写审计的,以及以后新增的端点)。隐私铁律:只记 动作/对象/操作人/时间,
# **绝不记录请求体**(患者明文不进 audit)。
_AUDIT_MUTATING = {"POST", "PUT", "DELETE", "PATCH"}
# 这些不审计:认证(无业务)、同步触发(另有 sync_batches 批次记录)。
_AUDIT_SKIP = ("/api/auth",)


def _audit_entity_from_path(path):
    """/api/instruments/123/disable → (entity='instruments', entity_id='123', tail='disable')。"""
    parts = [p for p in path.split("/") if p][1:]   # 去掉 'api'
    entity = parts[0] if parts else ""
    entity_id = parts[1] if len(parts) >= 2 else ""
    tail = parts[2] if len(parts) >= 3 else ""
    return entity, entity_id, tail


def should_audit_operation(method, path, status):
    """这次 API 调用是否"候选"补通用操作审计(改数据 + 成功 + 非认证/同步)。纯函数。
    不再用资源前缀白名单(太粗会把"患者子资源 手术/面诊"也跳过→漏审计)。是否真补由中间件
    按本请求 cell(,db._audit_trace 写回)精确判:路由自审计(audited)→不重复;库改了但没审计
    (changed)→兜底补;库没改(no-op 保存)→不记。每请求各自一个 cell=并发安全。"""
    return (method in _AUDIT_MUTATING and path.startswith("/api/") and 200 <= int(status) < 400
            and not path.startswith(_AUDIT_SKIP))


def audit_action_name(method, path):
    """通用操作的动作码:method + 资源 (+ 末段动作)。如 post_instruments / put_instruments_disable。"""
    entity, _eid, tail = _audit_entity_from_path(path)
    name = f"{method.lower()}_{entity or 'api'}"
    if tail and not tail.isdigit():
        name += f"_{tail}"
    return name


class OperationAuditMiddleware:
    """所有改数据的 API 调用成功后补一条操作审计(自包含解析操作人,不依赖 contextvar 时序)。"""

    def __init__(self, app, db_path):
        self.app = app
        self.db_path = db_path

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            return await self.app(scope, receive, send)
        method = scope.get("method", "")
        path = scope.get("path", "")
        candidate = (method in _AUDIT_MUTATING and path.startswith("/api/")
                     and not path.startswith(_AUDIT_SKIP))
        # 改并发安全:给本请求挂一个独立 cell,任何连接的 insert/update/delete 经 db._audit_trace
        # 写回它(changed=库真改了, audited=路由已自写 audit_logs)。每请求各自一个 cell,
        # 不再读全局 max(audit_id)/data_version(并发下会被别的请求的 audit 写污染→漏审计)。
        reset = None
        cell = None
        if candidate:
            cell = {"changed": False, "audited": False}
            reset = _audit_cell.set(cell)
        status_holder = {"code": 0}

        async def _send(message):
            if message.get("type") == "http.response.start":
                status_holder["code"] = message.get("status", 0)
                if cell is not None:
                    target_mode, target_user = _target_user_context()
                    if target_mode:
                        cell["target_mode"] = True
                        if target_user is not None:
                            cell["actor_user_id"] = target_user["id"]
                            cell["operator"] = target_user["username"]
            await send(message)

        try:
            await self.app(scope, receive, _send)
        finally:
            if reset is not None:
                _audit_cell.reset(reset)
            try:
                # 路由没自审计 + 库确实改了(非 no-op) + 成功 → 兜底补一条操作审计
                if (cell is not None and not cell["audited"] and cell["changed"]
                        and should_audit_operation(method, path, status_holder["code"])):
                    entity, entity_id, _tail = _audit_entity_from_path(path)
                    if cell.get("target_mode"):
                        actor_user_id = cell.get("actor_user_id")
                        operator = cell.get("operator")
                        if not actor_user_id or not operator:
                            logging.getLogger("local_app").error(
                                "目标授权请求缺少审计操作者 %s %s", method, path
                            )
                        else:
                            with connect(self.db_path) as conn:
                                _db_audit_write(
                                    conn,
                                    entity,
                                    entity_id,
                                    audit_action_name(method, path),
                                    operator=operator,
                                    actor_user_id=actor_user_id,
                                )
                                conn.commit()
                    else:
                        token = _cookie_from_scope(scope, COOKIE_NAME)
                        with connect(self.db_path) as conn:
                            operator = "local_user"
                            u = user_by_token(conn, token) if token else None
                            if u:
                                operator = u["username"]
                            _db_audit_write(
                                conn,
                                entity,
                                entity_id,
                                audit_action_name(method, path),
                                operator=operator,
                            )
                            conn.commit()
            except Exception:
                # 审计失败绝不影响业务响应，但必须留痕(架构铁律#禁止兜底:不许无声吞掉)
                logging.getLogger("local_app").exception("审计日志写入失败 %s %s", method, path)


def _is_login_asset(path):
    # 登录页本身的静态资源(若有)放行；当前登录页内联，无需额外资源
    return path == "/login.html"


# ---------- 登录页(内联) ----------
# 标题/抬头显示配置的诊所名(占位符渲染时替换;未配置=中性默认)
LOGIN_HTML_TMPL = """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>登录 · __CLINIC_NAME__本地系统</title>
<style>body{font-family:-apple-system,"PingFang SC",sans-serif;display:flex;align-items:center;
justify-content:center;height:100vh;margin:0;background:#f1f4f7}.box{background:#fff;padding:30px 26px;
border-radius:14px;box-shadow:0 6px 24px rgba(0,0,0,.08);width:min(340px,88vw)}
h2{margin:0 0 4px;font-size:19px;text-align:center}p{color:#888;font-size:13px;margin:0 0 18px;text-align:center}
input[type=text],input[type=password]{width:100%;box-sizing:border-box;padding:11px 12px;font-size:16px;
border:1px solid #d6dde3;border-radius:8px;margin-bottom:12px}
.rm{display:flex;align-items:center;gap:6px;font-size:13px;color:#555;margin:2px 0 16px}
button{width:100%;padding:11px;font-size:16px;border:0;border-radius:8px;background:#2c8c6f;color:#fff;cursor:pointer}
#msg{color:#c0392b;font-size:13px;margin-top:10px;min-height:18px;text-align:center}</style></head><body>
<div class="box"><h2>__CLINIC_NAME__ · 本地系统</h2><p>请登录</p>
<input id="u" type="text" placeholder="用户名" autofocus autocomplete="username">
<input id="p" type="password" placeholder="密码" autocomplete="current-password">
<label class="rm"><input id="rm" type="checkbox"> 记住此设备（长期免登）</label>
<button onclick="go()">登 录</button><div id="msg"></div></div>
<script>
async function go(){var u=document.getElementById('u').value,p=document.getElementById('p').value,
rm=document.getElementById('rm').checked;
var r=await fetch('/api/auth/login',{method:'POST',headers:{'Content-Type':'application/json'},
body:JSON.stringify({username:u,password:p,remember:rm})});
if(r.ok){location.href='/';}else{var m=await r.json().catch(function(){return{}});
document.getElementById('msg').textContent=(m.detail||'登录失败');}}
document.getElementById('p').addEventListener('keydown',function(e){if(e.key==='Enter')go();});
</script></body></html>"""


def render_login_html(db_path):
    """登录页按配置诊所名渲染。名字是管理员配置值,仍防御性转义。"""
    import html as _html
    from local_app.db import clinic_display_name
    return LOGIN_HTML_TMPL.replace("__CLINIC_NAME__", _html.escape(clinic_display_name(db_path)))


# ---------- 挂载到 app ----------
def setup_auth(app, db_path, require_login=False):
    app.add_middleware(AuthContextMiddleware, db_path=db_path, require_login=require_login)
    # 原子切换 v3 前的显式兼容桥。只有调用旧 setup_auth 的 app 才会安装；
    # 目标模式移除 setup_auth 后，无新 principal/policy 的 require_access 必然 fail-closed。
    from local_app.access_authorization import LegacyAccessBridgeMiddleware
    app.add_middleware(
        LegacyAccessBridgeMiddleware,
        legacy_require_perm=require_perm,
        legacy_has_perm=has_perm,
    )
    # 统一操作审计:补齐所有改数据 API 的操作留痕(后加=更外层,自包含解析操作人,不依赖上面的 contextvar)
    app.add_middleware(OperationAuditMiddleware, db_path=db_path)

    @app.post("/api/auth/login")
    def login(payload: dict):
        payload = payload or {}
        username = str(payload.get("username") or "").strip()
        password = payload.get("password")   # 原值直传:非 str 由 verify_password 判否 → 通用 401
        remember = bool(payload.get("remember"))
        with connect(db_path) as conn:
            uid = authenticate(conn, username, password)
            if not uid:
                raise HTTPException(status_code=401, detail="用户名或密码错误")
            token, ttl_days = create_session(conn, uid, remember=remember)
            conn.commit()
            user = user_by_token(conn, token)
        resp = JSONResponse({"ok": True, "user": user})
        resp.set_cookie(COOKIE_NAME, token, httponly=True, samesite="lax",
                        max_age=ttl_days * 86400)
        return resp

    @app.post("/api/auth/logout")
    def logout(request: Request):
        token = request.cookies.get(COOKIE_NAME)
        with connect(db_path) as conn:
            delete_session(conn, token)
            conn.commit()
        resp = JSONResponse({"ok": True})
        resp.delete_cookie(COOKIE_NAME)
        return resp

    @app.get("/api/auth/me")
    def me():
        u = current_user()
        return {"user": u}
