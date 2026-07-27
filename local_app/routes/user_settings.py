"""按登录用户的个性化设置(键值)。当前用于「今日候诊队列」列自定义。

GET  /api/my-settings/{key}  → {"value": <已存的json 或 null>}
PUT  /api/my-settings/{key}  body {"value": <任意json>} → 存当前用户
仅允许白名单 key，避免被当通用存储滥用。需登录。
"""
import json

from fastapi import APIRouter, HTTPException

from local_app import auth
from local_app.db import connect
from local_app.timeutil import now_str

_ALLOWED_KEYS = {"queue_columns"}


def create_user_settings_router(db_path):
    router = APIRouter()

    def _check_key(key):
        if key not in _ALLOWED_KEYS:
            raise HTTPException(status_code=400, detail="不支持的设置项")

    @router.get("/api/my-settings/{key}")
    def get_setting(key: str):
        user = auth.require_login_user()
        _check_key(key)
        with connect(db_path) as conn:
            row = conn.execute(
                "select value from user_settings where user_id = ? and key = ?", (user["id"], key)
            ).fetchone()
        if not row:
            return {"value": None}
        try:
            return {"value": json.loads(row["value"])}
        except (ValueError, TypeError):
            return {"value": None}

    @router.put("/api/my-settings/{key}")
    def put_setting(key: str, payload: dict):
        user = auth.require_login_user()
        _check_key(key)
        value = (payload or {}).get("value")
        with connect(db_path) as conn:
            conn.execute(
                "insert into user_settings(user_id, key, value, updated_at) values (?, ?, ?, ?) "
                "on conflict(user_id, key) do update set value = excluded.value, updated_at = excluded.updated_at",
                (user["id"], key, json.dumps(value, ensure_ascii=False), now_str()),
            )
            conn.commit()
        return {"ok": True, "value": value}

    return router
