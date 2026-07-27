from pathlib import Path

from fastapi import APIRouter, HTTPException
from local_app.auth import require_perm

from local_app.db import begin_immediate, connect
from local_app.routes.warehouse_common import (
    change_balance,
    doc_no,
    new_id,
    now_text,
    require_positive_qty,
    require_stock_item,
    require_supplier,
    row_to_dict,
    text_value,
    to_real,
)

_IN_TYPES = {
    "purchase": "purchase",
    "进货": "purchase",
    "return": "return",
    "退领": "return",
}


def _normalize_type(raw_type):
    stock_in_type = _IN_TYPES.get(str(raw_type or "").strip())
    if not stock_in_type:
        raise HTTPException(status_code=400, detail="入库类型非法")
    return stock_in_type


def create_warehouse_stock_in_router(db_path):
    router = APIRouter()
    db_path = Path(db_path)

    def _get_stock_in(conn, stock_in_id):
        row = conn.execute(
            """
            select id, in_no, type, supplier_id, operator, handler, status,
                   is_deleted, confirmed_at, created_at, updated_at
            from stock_in
            where id = ?
            """,
            (stock_in_id,),
        ).fetchone()
        if not row or row["is_deleted"]:
            raise HTTPException(status_code=404, detail="入库单不存在")
        return row

    def _parse_items(conn, raw_items):
        if not isinstance(raw_items, list) or not raw_items:
            raise HTTPException(status_code=400, detail="入库单至少需要一条明细")
        items = []
        for raw in raw_items:
            if not isinstance(raw, dict):
                raise HTTPException(status_code=400, detail="入库明细必须是对象")
            stock_item_id = text_value(raw, "stock_item_id")
            require_stock_item(conn, stock_item_id)
            items.append(
                {
                    "id": new_id("stock-in-item"),
                    "stock_item_id": stock_item_id,
                    "qty": require_positive_qty(raw.get("qty")),
                    "unit_price": to_real(raw.get("unit_price")),
                    "batch_no": text_value(raw, "batch_no"),
                    "expire_date": text_value(raw, "expire_date"),
                    "track_code": text_value(raw, "track_code"),
                }
            )
        return items

    @router.get("/api/stock-in")
    def list_stock_in(status: str = "", type: str = ""):
        require_perm("warehouse.view")   # 越权:查看库存数据
        where = ["s.is_deleted = 0"]
        params = []
        if status.strip():
            where.append("s.status = ?")
            params.append(status.strip())
        if type.strip():
            where.append("s.type = ?")
            params.append(_normalize_type(type))
        with connect(db_path) as conn:
            rows = conn.execute(
                f"""
                select s.id, s.in_no, s.type, s.supplier_id, sp.name as supplier_name,
                       s.operator, s.handler, s.status, s.confirmed_at,
                       s.created_at, s.updated_at,
                       count(i.id) as item_count
                from stock_in s
                left join supplier sp on sp.id = s.supplier_id and sp.is_deleted = 0
                left join stock_in_item i on i.stock_in_id = s.id
                where {' and '.join(where)}
                group by s.id
                order by s.created_at desc, s.in_no desc
                """,
                params,
            ).fetchall()
        stock_in = [_row for _row in (row_to_dict(row) for row in rows)]
        return {"stock_in": stock_in, "totalcount": len(stock_in)}

    @router.post("/api/stock-in")
    def create_stock_in(payload: dict):
        require_perm("warehouse.manage")   # 越权:改库存/采购主数据,写操作
        stock_in_type = _normalize_type(payload.get("type"))
        supplier_id = text_value(payload, "supplier_id")
        now = now_text()
        stock_in_id = new_id("stock-in")
        with connect(db_path) as conn:
            require_supplier(conn, supplier_id)
            items = _parse_items(conn, payload.get("items"))
            conn.execute(
                """
                insert into stock_in(
                    id, in_no, type, supplier_id, operator, handler, status,
                    is_deleted, created_at, updated_at
                )
                values (?, ?, ?, ?, ?, ?, 'draft', 0, ?, ?)
                """,
                (
                    stock_in_id,
                    doc_no("IN"),
                    stock_in_type,
                    supplier_id,
                    text_value(payload, "operator"),
                    text_value(payload, "handler"),
                    now,
                    now,
                ),
            )
            for item in items:
                conn.execute(
                    """
                    insert into stock_in_item(
                        id, stock_in_id, stock_item_id, qty, unit_price,
                        batch_no, expire_date, track_code
                    )
                    values (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        item["id"],
                        stock_in_id,
                        item["stock_item_id"],
                        item["qty"],
                        item["unit_price"],
                        item["batch_no"],
                        item["expire_date"],
                        item["track_code"],
                    ),
                )
            conn.commit()
        return {
            "id": stock_in_id,
            "status": "draft",
            "type": stock_in_type,
            "item_count": len(items),
        }

    @router.post("/api/stock-in/{stock_in_id}/confirm")
    def confirm_stock_in(stock_in_id: str):
        require_perm("warehouse.manage")   # 越权:改库存/采购主数据,写操作
        now = now_text()
        with connect(db_path) as conn:
            begin_immediate(conn)   # 抢写锁，防双击/多设备并发确认致库存翻倍
            stock_in = _get_stock_in(conn, stock_in_id)
            if stock_in["status"] != "draft":
                raise HTTPException(status_code=409, detail="入库单已确认")
            items = conn.execute(
                """
                select id, stock_item_id, qty, batch_no, expire_date, track_code
                from stock_in_item
                where stock_in_id = ?
                order by id
                """,
                (stock_in_id,),
            ).fetchall()
            if not items:
                raise HTTPException(status_code=400, detail="入库单没有明细")
            for item in items:
                change_balance(
                    conn,
                    stock_item_id=item["stock_item_id"],
                    change_qty=item["qty"],
                    source_type="stock_in",
                    source_id=stock_in_id,
                    source_item_id=item["id"],
                    batch_no=item["batch_no"],
                    expire_date=item["expire_date"],
                    track_code=item["track_code"],
                    note=stock_in["type"],
                )
            conn.execute(
                """
                update stock_in
                set status = 'confirmed', confirmed_at = ?, updated_at = ?
                where id = ?
                """,
                (now, now, stock_in_id),
            )
            conn.commit()
        return {"id": stock_in_id, "status": "confirmed"}

    return router
