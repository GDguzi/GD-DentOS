import json
from decimal import Decimal, InvalidOperation
from pathlib import Path

from fastapi import APIRouter, HTTPException

from local_app.timeutil import now_str
from local_app.auth import audit_write, require_admin, require_system_admin
from local_app.db import begin_immediate, connect, new_id


_SNAPSHOT_COLUMNS = (
    "handle_id",
    "code",
    "name",
    "unit",
    "price",
    "handle_type",
    "fee_type",
    "name_py",
    "stopped",
    "source_json",
    "last_batch_id",
    "created_at",
    "updated_at",
)


def _item_snapshot(row):
    return {column: row[column] for column in _SNAPSHOT_COLUMNS}


def _item_origin(row):
    if _has_sync_trace(row) or not str(row["handle_id"]).startswith("local-handle-"):
        return "imported"
    return "local"



def _clean_price(value, field="price"):
    """收费项目单价必填，且只接受有限、非负、最多两位小数的数值。"""
    if value is None or str(value).strip() == "":
        raise HTTPException(status_code=400, detail=f"{field} 不能为空")
    try:
        price = Decimal(str(value).strip())
    except (InvalidOperation, ValueError) as exc:
        raise HTTPException(status_code=400, detail=f"{field} 必须是数字") from exc
    if not price.is_finite() or price < 0:
        raise HTTPException(status_code=400, detail=f"{field} 必须是非负有限数")
    try:
        rounded = price.quantize(Decimal("0.01"))
    except InvalidOperation as exc:
        raise HTTPException(status_code=400, detail=f"{field} 必须是有效金额") from exc
    if price != rounded:
        raise HTTPException(status_code=400, detail=f"{field} 最多保留两位小数")
    return float(price)


def _clean_text(value, field):
    if value is None:
        return ""
    if not isinstance(value, str):
        raise HTTPException(status_code=400, detail=f"{field} 必须是字符串")
    return value.strip()


def _clean_item_payload(payload):
    payload = payload or {}
    cleaned = {
        "name": _clean_text(payload.get("name"), "name"),
        "code": _clean_text(payload.get("code"), "code"),
        "unit": _clean_text(payload.get("unit"), "unit"),
        "price": _clean_price(payload.get("price")),
        "fee_type": _clean_text(payload.get("fee_type"), "fee_type"),
    }
    if not cleaned["name"]:
        raise HTTPException(status_code=400, detail="项目名称不能为空")
    if not cleaned["fee_type"]:
        raise HTTPException(status_code=400, detail="费用分类不能为空")
    return cleaned


def _validate_restorable_item(row):
    name = _clean_text(row["name"], "name")
    if not name:
        raise HTTPException(status_code=400, detail="项目名称不能为空")
    fee_type = _clean_text(row["fee_type"], "fee_type")
    if not fee_type:
        raise HTTPException(status_code=400, detail="费用分类不能为空")
    _clean_price(row["price"])


def _restore_issue(row):
    if not row["stopped"]:
        return ""
    try:
        _validate_restorable_item(row)
    except HTTPException as exc:
        return str(exc.detail)
    return ""


def _normalize_code(value):
    return str(value or "").strip().casefold()


def _has_sync_trace(row):
    """该 handle_items 行是否带外部导入痕迹(批次号 或 非空镜像 payload)。
    source_json 按 JSON 解析判空，避免 ' { } ' 这类带空白的语义空对象被误判为痕迹。"""
    if str(row["last_batch_id"] or "").strip():
        return True
    raw = str(row["source_json"] or "").strip()
    if not raw:
        return False
    try:
        return bool(json.loads(raw))   # {} / [] / null / "" → 假(无痕迹)；非空 dict/list → 真
    except (ValueError, TypeError):
        return True                    # 解析不了，保守当作有痕迹


def create_master_data_router(db_path, *, access_v3=True):
    router = APIRouter()
    db_path = Path(db_path)

    def _require_handle_item_manager():
        if access_v3:
            return require_system_admin()
        return require_admin()

    def _require_item(conn, handle_id):
        row = conn.execute(
            "select * from handle_items where handle_id = ?", (handle_id,)
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="收费项目不存在")
        return row

    def _ensure_active_code_unique(conn, code, handle_id=None):
        normalized_code = _normalize_code(code)
        if not normalized_code:
            return
        rows = conn.execute(
            "select handle_id, code from handle_items where stopped = 0"
        ).fetchall()
        for row in rows:
            if row["handle_id"] == handle_id:
                continue
            if _normalize_code(row["code"]) == normalized_code:
                raise HTTPException(status_code=409, detail="项目编码已被在用项目占用")

    @router.get("/api/handle-items")
    def handle_items():
        """在用处置项目，按官方费用分类(fee_type)分组。
        滤掉"无分类且无价"的旧条目（多为停用耗材/未维护项），避免淹没真正可开单的处置。"""
        with connect(db_path) as conn:
            rows = conn.execute(
                """
                select handle_id, code, name, unit, price, handle_type, fee_type
                from handle_items
                where stopped = 0 and (coalesce(fee_type, '') != '' or price > 0)
                order by
                    case when coalesce(fee_type, '') = '' then 1 else 0 end,
                    fee_type, price desc, name
                """
            ).fetchall()

        groups = []
        index = {}
        for row in rows:
            cat = (row["fee_type"] or "").strip() or "其他"
            group = index.get(cat)
            if group is None:
                # category=官方费用分类；handle_type 保留同值兼容旧前端
                group = {"category": cat, "handle_type": cat, "items": []}
                index[cat] = group
                groups.append(group)
            group["items"].append(
                {
                    "handle_id": row["handle_id"],
                    "code": row["code"],
                    "name": row["name"],
                    "unit": row["unit"],
                    "price": row["price"],
                    "fee_type": row["fee_type"],
                }
            )
        return {"groups": groups}

    @router.get("/api/handle-items/manage")
    def manage_handle_items(status: str = "active"):
        _require_handle_item_manager()
        if status not in {"active", "stopped", "all"}:
            raise HTTPException(status_code=400, detail="status 必须是 active、stopped 或 all")

        clauses = {
            "active": "where stopped = 0",
            "stopped": "where stopped = 1",
            "all": "",
        }
        with connect(db_path) as conn:
            rows = conn.execute(
                f"""
                select handle_id, code, name, unit, price, fee_type, stopped,
                       source_json, last_batch_id, created_at, updated_at
                from handle_items
                {clauses[status]}
                order by stopped, name, handle_id
                """
            ).fetchall()
            counts = conn.execute(
                """
                select
                    sum(case when stopped = 0 then 1 else 0 end) as active,
                    sum(case when stopped = 1 then 1 else 0 end) as stopped
                from handle_items
                """
            ).fetchone()

        return {
            "items": [
                {
                    "handle_id": row["handle_id"],
                    "code": row["code"] or "",
                    "name": row["name"],
                    "unit": row["unit"] or "",
                    "price": row["price"],
                    "fee_type": row["fee_type"] or "",
                    "status": "stopped" if row["stopped"] else "active",
                    "restore_issue": _restore_issue(row),
                    "origin": _item_origin(row),
                    "created_at": row["created_at"],
                    "updated_at": row["updated_at"],
                }
                for row in rows
            ],
            "counts": {
                "active": int(counts["active"] or 0),
                "stopped": int(counts["stopped"] or 0),
            },
        }

    @router.get("/api/dictionaries")
    def dictionaries(type: str = ""):
        """字典枚举，可按 dict_type 筛选；不传则返回全部在用项。"""
        with connect(db_path) as conn:
            if type:
                rows = conn.execute(
                    """
                    select dict_id, dict_type, name, value, describe, display_order
                    from dictionaries
                    where stopped = 0 and dict_type = ?
                    order by display_order, name
                    """,
                    (type,),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    select dict_id, dict_type, name, value, describe, display_order
                    from dictionaries
                    where stopped = 0
                    order by dict_type, display_order, name
                    """
                ).fetchall()

        items = [
            {
                "dict_id": row["dict_id"],
                "dict_type": row["dict_type"],
                "name": row["name"],
                "value": row["value"],
                "describe": row["describe"],
                "display_order": row["display_order"],
            }
            for row in rows
        ]
        return {"items": items}

    # ---------- 收费项目管理：新建仍用 local-handle- ID，管理员可维护全部来源 ----------
    @router.post("/api/handle-items")
    def create_handle_item(payload: dict):
        _require_handle_item_manager()
        item = _clean_item_payload(payload)
        handle_id = new_id("local-handle")
        now = now_str()
        with connect(db_path) as conn:
            begin_immediate(conn)
            _ensure_active_code_unique(conn, item["code"])
            conn.execute(
                """
                insert into handle_items(
                    handle_id, code, name, unit, price, handle_type, fee_type,
                    stopped, created_at, updated_at
                )
                values (?, ?, ?, ?, ?, ?, ?, 0, ?, ?)
                """,
                (
                    handle_id,
                    item["code"],
                    item["name"],
                    item["unit"],
                    item["price"],
                    item["fee_type"],
                    item["fee_type"],
                    now,
                    now,
                ),
            )
            new_row = _require_item(conn, handle_id)
            audit_write(
                conn,
                "handle_item",
                handle_id,
                "create_handle_item",
                new_json=json.dumps(_item_snapshot(new_row), ensure_ascii=False),
                created_at=now,
            )
            conn.commit()
        return {"handle_id": handle_id}

    @router.put("/api/handle-items/{handle_id}")
    def update_handle_item(handle_id: str, payload: dict):
        _require_handle_item_manager()
        with connect(db_path) as conn:
            begin_immediate(conn)
            old_row = _require_item(conn, handle_id)
            if old_row["stopped"]:
                raise HTTPException(status_code=409, detail="已停用项目不能修改，请先恢复")
            item = _clean_item_payload(payload)
            if _normalize_code(item["code"]) != _normalize_code(old_row["code"]):
                _ensure_active_code_unique(conn, item["code"], handle_id)
            now = now_str()
            old_snapshot = _item_snapshot(old_row)
            conn.execute(
                """
                update handle_items
                set name = ?, code = ?, unit = ?, price = ?,
                    handle_type = ?, fee_type = ?, updated_at = ?
                where handle_id = ?
                """,
                (
                    item["name"],
                    item["code"],
                    item["unit"],
                    item["price"],
                    item["fee_type"],
                    item["fee_type"],
                    now,
                    handle_id,
                ),
            )
            new_snapshot = _item_snapshot(_require_item(conn, handle_id))
            audit_write(
                conn,
                "handle_item",
                handle_id,
                "update_handle_item",
                old_json=json.dumps(old_snapshot, ensure_ascii=False),
                new_json=json.dumps(new_snapshot, ensure_ascii=False),
                created_at=now,
            )
            conn.commit()
        return {"handle_id": handle_id}

    @router.delete("/api/handle-items/{handle_id}")
    def delete_handle_item(handle_id: str):
        _require_handle_item_manager()
        now = now_str()
        with connect(db_path) as conn:
            begin_immediate(conn)
            old_row = _require_item(conn, handle_id)
            if old_row["stopped"]:
                raise HTTPException(status_code=409, detail="收费项目已停用")
            old_snapshot = _item_snapshot(old_row)
            conn.execute(
                "update handle_items set stopped = 1, updated_at = ? where handle_id = ?",
                (now, handle_id),
            )
            new_snapshot = _item_snapshot(_require_item(conn, handle_id))
            audit_write(
                conn,
                "handle_item",
                handle_id,
                "stop_handle_item",
                old_json=json.dumps(old_snapshot, ensure_ascii=False),
                new_json=json.dumps(new_snapshot, ensure_ascii=False),
                created_at=now,
            )
            conn.commit()
        return {"handle_id": handle_id, "stopped": 1}

    @router.post("/api/handle-items/{handle_id}/restore")
    def restore_handle_item(handle_id: str, payload: dict = None):
        _require_handle_item_manager()
        now = now_str()
        with connect(db_path) as conn:
            begin_immediate(conn)
            old_row = _require_item(conn, handle_id)
            if not old_row["stopped"]:
                raise HTTPException(status_code=409, detail="收费项目已在用")
            old_snapshot = _item_snapshot(old_row)
            if payload is None:
                _validate_restorable_item(old_row)
                _ensure_active_code_unique(conn, old_row["code"], handle_id)
                conn.execute(
                    "update handle_items set stopped = 0, updated_at = ? where handle_id = ?",
                    (now, handle_id),
                )
            else:
                item = _clean_item_payload(payload)
                _ensure_active_code_unique(conn, item["code"], handle_id)
                conn.execute(
                    """
                    update handle_items
                    set name = ?, code = ?, unit = ?, price = ?,
                        handle_type = ?, fee_type = ?, stopped = 0, updated_at = ?
                    where handle_id = ?
                    """,
                    (
                        item["name"],
                        item["code"],
                        item["unit"],
                        item["price"],
                        item["fee_type"],
                        item["fee_type"],
                        now,
                        handle_id,
                    ),
                )
            new_snapshot = _item_snapshot(_require_item(conn, handle_id))
            audit_write(
                conn,
                "handle_item",
                handle_id,
                "restore_handle_item",
                old_json=json.dumps(old_snapshot, ensure_ascii=False),
                new_json=json.dumps(new_snapshot, ensure_ascii=False),
                created_at=now,
            )
            conn.commit()
        return {"handle_id": handle_id, "stopped": 0}

    return router
