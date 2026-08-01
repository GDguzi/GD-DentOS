"""应用设置：预约界面设置（营业时间/最小单位/是否分医生列），驱动预约天/周视图网格。
单店一份配置，存通用 KV 表。"""
import json
import re
from pathlib import Path

from fastapi import APIRouter, HTTPException

from local_app.timeutil import now_str
from local_app.auth import audit_write, require_perm
from local_app.access_authorization import has_access
from local_app.customer_hub_security import (
    audit_actor_id,
    request_human_actor,
    require_locked_human_actor,
)
from local_app.db import (
    DEFAULT_CLINIC_NAME,
    audit_write as data_audit_write,
    begin_immediate,
    connect,
)

_HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
_SLOTS = (5, 10, 15, 20, 30, 60)
_DEFAULT_APPT = {
    "business_start": "08:00",
    "business_end": "21:30",   # 默认早8到晚9:30(含夜班)，可在「预约界面设置」改
    "min_slot_minutes": 30,
    "show_doctor_column": True,
}
_DEFAULT_CLINIC = {
    "name": DEFAULT_CLINIC_NAME,   # 就诊诊所名：首启填一次，各处(登录页/回访卡/打印头)统一显示
}
# 诊室列表：候诊队列给患者分诊室用(按诊室归属操作)。可在设置里改。
_DEFAULT_ROOMS = {"list": ["诊室1", "诊室2", "诊室3", "治疗室"]}
# 功能开关：诊所按需启停模块。membership_enabled=会员储值(默认开,不用会员可关掉)。
_DEFAULT_FEATURES = {"membership_enabled": True}
# 付款方式：收费弹窗下拉的候选名单,前台可在弹窗⚙️里自定义(默认=原写死8种,零迁移)。
_DEFAULT_PAY_METHODS = {"list": ["现金", "微信", "支付宝", "银行卡", "云闪付", "社保卡", "医保", "其他"]}

_CUSTOMER_HUB_RETURN_DICTS = (
    "return_type",
    "visit_content",
    "visit_result",
)
_CUSTOMER_HUB_COMM_DICTS = ("comm_reason",)
_CUSTOMER_HUB_DICTS = _CUSTOMER_HUB_RETURN_DICTS + _CUSTOMER_HUB_COMM_DICTS




def _as_bool(value, default):
    """稳健布尔解析：真 bool 直接用；字符串 'false'/'0'/'off'/'no'/'' → False，
    避免前端表单传字符串时 bool('false') 误判为 True。"""
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip().lower() not in ("false", "0", "off", "no", "")
    return bool(value)


def _read(conn, key, default):
    row = conn.execute("select value from app_settings where key = ?", (key,)).fetchone()
    if not row:
        return dict(default)
    try:
        stored = json.loads(row["value"] or "{}")
    except (ValueError, TypeError):
        stored = {}
    merged = dict(default)
    merged.update({k: stored[k] for k in default if k in stored})
    return merged


def _read_customer_hub_dict(conn, key):
    row = conn.execute(
        "select value from app_settings where key = ?", (key,)
    ).fetchone()
    if row is None:
        return []
    raw_value = row["value"]
    if type(raw_value) is not str:
        raise HTTPException(
            status_code=500, detail="customer_hub_dictionary_unavailable"
        )
    try:
        value = json.loads(raw_value)
    except (TypeError, ValueError):
        raise HTTPException(
            status_code=500, detail="customer_hub_dictionary_unavailable"
        ) from None
    if type(value) is not list or not all(
        _valid_customer_hub_dict_string(item) for item in value
    ):
        raise HTTPException(
            status_code=500, detail="customer_hub_dictionary_unavailable"
        )
    return value


def _valid_customer_hub_dict_string(value):
    if type(value) is not str:
        return False
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def create_app_settings_router(db_path):
    router = APIRouter()
    db_path = Path(db_path)

    @router.get("/api/settings/appointment")
    def get_appointment_settings():
        with connect(db_path) as conn:
            return _read(conn, "appointment", _DEFAULT_APPT)

    @router.get("/api/settings/clinic")
    def get_clinic_settings():
        with connect(db_path) as conn:
            stored = conn.execute("select 1 from app_settings where key = 'clinic'").fetchone()
            data = _read(conn, "clinic", _DEFAULT_CLINIC)
        data["configured"] = bool(stored)   # 首启引导用:区分"没填过"和"填成了默认同名"
        return data

    @router.get("/api/settings/rooms")
    def get_rooms_settings():
        with connect(db_path) as conn:
            return _read(conn, "rooms", _DEFAULT_ROOMS)

    @router.get("/api/settings/features")
    def get_feature_settings():
        with connect(db_path) as conn:
            return _read(conn, "features", _DEFAULT_FEATURES)

    @router.get("/api/settings/customer-hub-dicts")
    def get_customer_hub_dicts():
        request_human_actor()
        keys = ()
        if has_access("return_visit.view"):
            keys += _CUSTOMER_HUB_RETURN_DICTS
        if has_access("communication.view"):
            keys += _CUSTOMER_HUB_COMM_DICTS
        with connect(db_path) as conn:
            return {
                key: _read_customer_hub_dict(conn, key)
                for key in keys
            }

    @router.put("/api/settings/customer-hub-dicts")
    def put_customer_hub_dicts(payload: dict):
        actor = request_human_actor()
        payload = payload or {}
        submitted = tuple(key for key in _CUSTOMER_HUB_DICTS if key in payload)
        if not submitted or any(key not in _CUSTOMER_HUB_DICTS for key in payload):
            raise HTTPException(status_code=400, detail="invalid_dictionary_fields")
        if any(
            not isinstance(payload[key], list)
            or any(
                not _valid_customer_hub_dict_string(item)
                for item in payload[key]
            )
            for key in submitted
        ):
            raise HTTPException(status_code=400, detail="invalid_dictionary_value")
        required = ()
        if any(key in _CUSTOMER_HUB_RETURN_DICTS for key in submitted):
            required += ("return_visit.manage",)
        if any(key in _CUSTOMER_HUB_COMM_DICTS for key in submitted):
            required += ("communication.manage",)

        with connect(db_path) as conn:
            begin_immediate(conn)
            locked_actor = require_locked_human_actor(
                conn, actor, required
            )
            old = {
                key: _read_customer_hub_dict(conn, key)
                for key in submitted
            }
            updated_at = now_str()
            for key in submitted:
                conn.execute(
                    "insert into app_settings(key, value, updated_at) values (?, ?, ?) "
                    "on conflict(key) do update set value = excluded.value, "
                    "updated_at = excluded.updated_at",
                    (
                        key,
                        json.dumps(payload[key], ensure_ascii=False),
                        updated_at,
                    ),
                )
            new = {key: payload[key] for key in submitted}
            data_audit_write(
                conn,
                "customer_hub_dicts",
                "customer_hub_dicts",
                "update_customer_hub_dicts",
                old_json=json.dumps(old, ensure_ascii=False, sort_keys=True),
                new_json=json.dumps(new, ensure_ascii=False, sort_keys=True),
                operator=locked_actor.username,
                created_at=updated_at,
                actor_user_id=audit_actor_id(locked_actor),
            )
            conn.commit()
        return new

    @router.put("/api/settings/features")
    def put_feature_settings(payload: dict):
        require_perm("settings.manage")   # 会员开关等模块开关；全店设置权限可配(默认仅 admin)
        payload = payload or {}
        with connect(db_path) as conn:
            current = _read(conn, "features", _DEFAULT_FEATURES)
            value = {"membership_enabled": _as_bool(payload.get("membership_enabled"), current["membership_enabled"])}
            conn.execute(
                "insert into app_settings(key, value, updated_at) values ('features', ?, ?) "
                "on conflict(key) do update set value = excluded.value, updated_at = excluded.updated_at",
                (json.dumps(value, ensure_ascii=False), now_str()),
            )
            conn.commit()
        return value

    @router.put("/api/settings/rooms")
    def put_rooms_settings(payload: dict):
        require_perm("settings.manage")   # 全店配置写操作(与 put_feature_settings 口径一致)
        payload = payload or {}
        raw = payload.get("list")
        if not isinstance(raw, list):
            raise HTTPException(status_code=400, detail="诊室列表格式不对")
        rooms = [str(x).strip() for x in raw if str(x).strip()]
        value = {"list": rooms}
        with connect(db_path) as conn:
            conn.execute(
                "insert into app_settings(key, value, updated_at) values ('rooms', ?, ?) "
                "on conflict(key) do update set value = excluded.value, updated_at = excluded.updated_at",
                (json.dumps(value, ensure_ascii=False), now_str()),
            )
            conn.commit()
        return value

    @router.get("/api/settings/pay-methods")
    def get_pay_methods_settings():
        with connect(db_path) as conn:
            return _read(conn, "pay_methods", _DEFAULT_PAY_METHODS)

    @router.put("/api/settings/pay-methods")
    def put_pay_methods_settings(payload: dict):
        # 产品决策「能收费就能改」:守卫用 billing.pay 而非 settings.manage,前台/收银自己维护
        require_perm("billing.pay")
        payload = payload or {}
        raw = payload.get("list")
        if not isinstance(raw, list):
            raise HTTPException(status_code=400, detail="付款方式列表格式不对")
        methods, seen = [], set()
        for x in raw:
            name = str(x).strip()
            if not name:
                continue
            if len(name) > 20:
                raise HTTPException(status_code=400, detail=f"付款方式名太长(超20字):{name[:20]}…")
            if name in seen:
                continue   # 重名去重,保序
            seen.add(name)
            methods.append(name)
        if not methods:
            raise HTTPException(status_code=400, detail="付款方式至少留一条")
        value = {"list": methods}
        with connect(db_path) as conn:
            old = _read(conn, "pay_methods", _DEFAULT_PAY_METHODS)
            conn.execute(
                "insert into app_settings(key, value, updated_at) values ('pay_methods', ?, ?) "
                "on conflict(key) do update set value = excluded.value, updated_at = excluded.updated_at",
                (json.dumps(value, ensure_ascii=False), now_str()),
            )
            # 改钱相关配置留痕:谁把名单从什么改成什么,操作历史可查
            audit_write(
                conn, "pay_methods", "pay_methods", "update_pay_methods",
                old_json=json.dumps(old, ensure_ascii=False),
                new_json=json.dumps(value, ensure_ascii=False),
            )
            conn.commit()
        return value

    @router.put("/api/settings/clinic")
    def put_clinic_settings(payload: dict):
        require_perm("settings.manage")   # 全店配置写操作
        payload = payload or {}
        name = str(payload.get("name") or "").strip()
        if not name:
            raise HTTPException(status_code=400, detail="诊所名称不能为空")
        value = {"name": name}
        with connect(db_path) as conn:
            conn.execute(
                "insert into app_settings(key, value, updated_at) values ('clinic', ?, ?) "
                "on conflict(key) do update set value = excluded.value, updated_at = excluded.updated_at",
                (json.dumps(value, ensure_ascii=False), now_str()),
            )
            conn.commit()
        return value

    @router.put("/api/settings/appointment")
    def put_appointment_settings(payload: dict):
        require_perm("settings.manage")   # 全店配置写操作
        payload = payload or {}
        with connect(db_path) as conn:
            current = _read(conn, "appointment", _DEFAULT_APPT)
            start = str(payload.get("business_start", current["business_start"])).strip()
            end = str(payload.get("business_end", current["business_end"])).strip()
            if not _HHMM.match(start) or not _HHMM.match(end):
                raise HTTPException(status_code=400, detail="营业时间须为 HH:MM")
            if start >= end:
                raise HTTPException(status_code=400, detail="营业结束须晚于开始")
            slot = payload.get("min_slot_minutes", current["min_slot_minutes"])
            try:
                slot = int(slot)
            except (TypeError, ValueError):
                raise HTTPException(status_code=400, detail="最小单位非法")
            if slot not in _SLOTS:
                raise HTTPException(status_code=400, detail="最小单位须为 5/10/15/20/30/60 分钟")
            show = payload.get("show_doctor_column", current["show_doctor_column"])
            value = {
                "business_start": start, "business_end": end,
                "min_slot_minutes": slot, "show_doctor_column": bool(show),
            }
            conn.execute(
                "insert into app_settings(key, value, updated_at) values ('appointment', ?, ?) "
                "on conflict(key) do update set value = excluded.value, updated_at = excluded.updated_at",
                (json.dumps(value, ensure_ascii=False), now_str()),
            )
            conn.commit()
        return value

    return router
