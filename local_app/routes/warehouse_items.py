import math
import sqlite3
from pathlib import Path

from fastapi import APIRouter, HTTPException
from local_app.timeutil import now_str
from local_app.auth import require_perm

from local_app.db import connect, new_id


def _row_to_dict(row):
    return {key: row[key] for key in row.keys()}


def _text(payload, key, default=""):
    return str(payload.get(key, default) or "").strip()


def _to_int(value, default=0):
    if value in (None, ""):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _to_real(value):
    if value in (None, ""):
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None   # nan/inf 视为无效


def create_warehouse_items_router(db_path):
    router = APIRouter()
    db_path = Path(db_path)

    def _category_exists(conn, category_id):
        if not category_id:
            return True
        return conn.execute(
            """
            select 1
            from stock_category
            where id = ? and is_deleted = 0
            """,
            (category_id,),
        ).fetchone() is not None

    def _get_category(conn, category_id):
        row = conn.execute(
            """
            select id, name, parent_id, sort, is_deleted, created_at, updated_at
            from stock_category
            where id = ?
            """,
            (category_id,),
        ).fetchone()
        if not row or row["is_deleted"]:
            raise HTTPException(status_code=404, detail="分类不存在")
        return row

    def _get_item(conn, item_id):
        row = conn.execute(
            """
            select id, code, name, category_id, spec, unit, brand, price,
                   is_high_value, min_qty, max_qty, shelf_life_days, status,
                   is_deleted, created_at, updated_at
            from stock_item
            where id = ?
            """,
            (item_id,),
        ).fetchone()
        if not row or row["is_deleted"]:
            raise HTTPException(status_code=404, detail="物品不存在")
        return row

    @router.get("/api/stock-categories")
    def list_categories():
        require_perm("warehouse.view")   # 越权:查看库存数据
        with connect(db_path) as conn:
            rows = conn.execute(
                """
                select id, name, parent_id, sort, created_at, updated_at
                from stock_category
                where is_deleted = 0
                order by sort, name
                """
            ).fetchall()
        categories = [_row_to_dict(row) for row in rows]
        return {"categories": categories, "totalcount": len(categories)}

    @router.post("/api/stock-categories")
    def create_category(payload: dict):
        require_perm("warehouse.manage")   # 越权:改库存/采购主数据,写操作
        name = _text(payload, "name")
        if not name:
            raise HTTPException(status_code=400, detail="分类名称不能为空")
        category_id = new_id("stock-cat")
        now = now_str()
        with connect(db_path) as conn:
            conn.execute(
                """
                insert into stock_category(id, name, parent_id, sort, is_deleted, created_at, updated_at)
                values (?, ?, ?, ?, 0, ?, ?)
                """,
                (
                    category_id,
                    name,
                    _text(payload, "parent_id"),
                    _to_int(payload.get("sort"), 0),
                    now,
                    now,
                ),
            )
            conn.commit()
            row = _get_category(conn, category_id)
        return _row_to_dict(row)

    @router.put("/api/stock-categories/{category_id}")
    def update_category(category_id: str, payload: dict):
        require_perm("warehouse.manage")   # 越权:改库存/采购主数据,写操作
        with connect(db_path) as conn:
            current = _get_category(conn, category_id)
            name = _text(payload, "name", current["name"])
            if not name:
                raise HTTPException(status_code=400, detail="分类名称不能为空")
            conn.execute(
                """
                update stock_category
                set name = ?, parent_id = ?, sort = ?, updated_at = ?
                where id = ?
                """,
                (
                    name,
                    _text(payload, "parent_id", current["parent_id"]),
                    _to_int(payload.get("sort"), current["sort"]),
                    now_str(),
                    category_id,
                ),
            )
            conn.commit()
            row = _get_category(conn, category_id)
        return _row_to_dict(row)

    @router.delete("/api/stock-categories/{category_id}")
    def delete_category(category_id: str):
        require_perm("warehouse.manage")   # 越权:改库存/采购主数据,写操作
        with connect(db_path) as conn:
            _get_category(conn, category_id)
            conn.execute(
                "update stock_category set is_deleted = 1, updated_at = ? where id = ?",
                (now_str(), category_id),
            )
            conn.commit()
        return {"id": category_id, "is_deleted": 1}

    @router.get("/api/stock-items")
    def list_items(code: str = "", name: str = "", category_id: str = ""):
        require_perm("warehouse.view")   # 越权:查看库存数据
        where = ["i.is_deleted = 0"]
        params = []
        if code.strip():
            where.append("i.code = ?")
            params.append(code.strip())
        if name.strip():
            where.append("i.name like ?")
            params.append(f"%{name.strip()}%")
        if category_id.strip():
            where.append("i.category_id = ?")
            params.append(category_id.strip())
        with connect(db_path) as conn:
            rows = conn.execute(
                f"""
                select i.id, i.code, i.name, i.category_id, c.name as category_name,
                       i.spec, i.unit, i.brand, i.price, i.is_high_value,
                       i.min_qty, i.max_qty, i.shelf_life_days, i.status,
                       i.created_at, i.updated_at
                from stock_item i
                left join stock_category c on c.id = i.category_id and c.is_deleted = 0
                where {' and '.join(where)}
                order by i.code, i.name
                """,
                params,
            ).fetchall()
        items = [_row_to_dict(row) for row in rows]
        return {"items": items, "totalcount": len(items)}

    @router.post("/api/stock-items")
    def create_item(payload: dict):
        require_perm("warehouse.manage")   # 越权:改库存/采购主数据,写操作
        code = _text(payload, "code")
        name = _text(payload, "name")
        if not code or not name:
            raise HTTPException(status_code=400, detail="物品编号和名称不能为空")
        category_id = _text(payload, "category_id")
        item_id = new_id("stock-item")
        now = now_str()
        with connect(db_path) as conn:
            if not _category_exists(conn, category_id):
                raise HTTPException(status_code=400, detail="分类不存在")
            try:
                conn.execute(
                    """
                    insert into stock_item(
                        id, code, name, category_id, spec, unit, brand, price,
                        is_high_value, min_qty, max_qty, shelf_life_days, status,
                        is_deleted, created_at, updated_at
                    )
                    values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)
                    """,
                    (
                        item_id,
                        code,
                        name,
                        category_id,
                        _text(payload, "spec"),
                        _text(payload, "unit"),
                        _text(payload, "brand"),
                        _to_real(payload.get("price")),
                        1 if payload.get("is_high_value") else 0,
                        _to_real(payload.get("min_qty")),
                        _to_real(payload.get("max_qty")),
                        _to_int(payload.get("shelf_life_days"), None),
                        _text(payload, "status", "active") or "active",
                        now,
                        now,
                    ),
                )
                conn.commit()
            except sqlite3.IntegrityError as exc:
                raise HTTPException(status_code=409, detail="物品编号已存在") from exc
            row = _get_item(conn, item_id)
        return _row_to_dict(row)

    @router.put("/api/stock-items/{item_id}")
    def update_item(item_id: str, payload: dict):
        require_perm("warehouse.manage")   # 越权:改库存/采购主数据,写操作
        with connect(db_path) as conn:
            current = _get_item(conn, item_id)
            code = _text(payload, "code", current["code"])
            name = _text(payload, "name", current["name"])
            if not code or not name:
                raise HTTPException(status_code=400, detail="物品编号和名称不能为空")
            category_id = _text(payload, "category_id", current["category_id"])
            if not _category_exists(conn, category_id):
                raise HTTPException(status_code=400, detail="分类不存在")
            try:
                conn.execute(
                    """
                    update stock_item
                    set code = ?, name = ?, category_id = ?, spec = ?, unit = ?,
                        brand = ?, price = ?, is_high_value = ?, min_qty = ?,
                        max_qty = ?, shelf_life_days = ?, status = ?, updated_at = ?
                    where id = ?
                    """,
                    (
                        code,
                        name,
                        category_id,
                        _text(payload, "spec", current["spec"]),
                        _text(payload, "unit", current["unit"]),
                        _text(payload, "brand", current["brand"]),
                        _to_real(payload.get("price", current["price"])),
                        1 if payload.get("is_high_value", current["is_high_value"]) else 0,
                        _to_real(payload.get("min_qty", current["min_qty"])),
                        _to_real(payload.get("max_qty", current["max_qty"])),
                        _to_int(payload.get("shelf_life_days", current["shelf_life_days"]), None),
                        _text(payload, "status", current["status"]) or "active",
                        now_str(),
                        item_id,
                    ),
                )
                conn.commit()
            except sqlite3.IntegrityError as exc:
                raise HTTPException(status_code=409, detail="物品编号已存在") from exc
            row = _get_item(conn, item_id)
        return _row_to_dict(row)

    @router.delete("/api/stock-items/{item_id}")
    def delete_item(item_id: str):
        require_perm("warehouse.manage")   # 越权:改库存/采购主数据,写操作
        with connect(db_path) as conn:
            _get_item(conn, item_id)
            conn.execute(
                "update stock_item set is_deleted = 1, updated_at = ? where id = ?",
                (now_str(), item_id),
            )
            conn.commit()
        return {"id": item_id, "is_deleted": 1}

    return router
