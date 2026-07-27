from pathlib import Path

from fastapi import APIRouter, HTTPException
from local_app.auth import require_perm

from local_app.db import begin_immediate, connect
from local_app.routes.warehouse_common import (
    available_qty,
    deduct_balance,
    doc_no,
    new_id,
    now_text,
    require_positive_qty,
    require_stock_item,
    require_supplier,
    row_to_dict,
    text_value,
)

_OUT_TYPES = {
    "use": "use",
    "领用": "use",
    "return_supplier": "return_supplier",
    "return": "return_supplier",
    "退货": "return_supplier",
}


def _normalize_type(raw_type):
    stock_out_type = _OUT_TYPES.get(str(raw_type or "").strip())
    if not stock_out_type:
        raise HTTPException(status_code=400, detail="出库类型非法")
    return stock_out_type


def create_warehouse_stock_out_router(db_path):
    router = APIRouter()
    db_path = Path(db_path)

    def _get_stock_out(conn, stock_out_id):
        row = conn.execute(
            """
            select id, out_no, type, operator, handler, receiver, supplier_id,
                   status, is_deleted, confirmed_at, created_at, updated_at
            from stock_out
            where id = ?
            """,
            (stock_out_id,),
        ).fetchone()
        if not row or row["is_deleted"]:
            raise HTTPException(status_code=404, detail="出库单不存在")
        return row

    def _parse_items(conn, raw_items):
        if not isinstance(raw_items, list) or not raw_items:
            raise HTTPException(status_code=400, detail="出库单至少需要一条明细")
        items = []
        for raw in raw_items:
            if not isinstance(raw, dict):
                raise HTTPException(status_code=400, detail="出库明细必须是对象")
            stock_item_id = text_value(raw, "stock_item_id")
            require_stock_item(conn, stock_item_id)
            items.append(
                {
                    "id": new_id("stock-out-item"),
                    "stock_item_id": stock_item_id,
                    "qty": require_positive_qty(raw.get("qty")),
                    "track_code": text_value(raw, "track_code"),
                }
            )
        return items

    @router.get("/api/stock-out")
    def list_stock_out(status: str = "", type: str = ""):
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
                select s.id, s.out_no, s.type, s.operator, s.handler, s.receiver,
                       s.supplier_id, sp.name as supplier_name, s.status,
                       s.confirmed_at, s.created_at, s.updated_at,
                       count(i.id) as item_count
                from stock_out s
                left join supplier sp on sp.id = s.supplier_id and sp.is_deleted = 0
                left join stock_out_item i on i.stock_out_id = s.id
                where {' and '.join(where)}
                group by s.id
                order by s.created_at desc, s.out_no desc
                """,
                params,
            ).fetchall()
        stock_out = [row_to_dict(row) for row in rows]
        return {"stock_out": stock_out, "totalcount": len(stock_out)}

    @router.post("/api/stock-out")
    def create_stock_out(payload: dict):
        require_perm("warehouse.manage")   # 越权:改库存/采购主数据,写操作
        stock_out_type = _normalize_type(payload.get("type"))
        supplier_id = text_value(payload, "supplier_id")
        stock_out_id = new_id("stock-out")
        now = now_text()
        with connect(db_path) as conn:
            require_supplier(conn, supplier_id)
            items = _parse_items(conn, payload.get("items"))
            conn.execute(
                """
                insert into stock_out(
                    id, out_no, type, operator, handler, receiver, supplier_id,
                    status, is_deleted, created_at, updated_at
                )
                values (?, ?, ?, ?, ?, ?, ?, 'draft', 0, ?, ?)
                """,
                (
                    stock_out_id,
                    doc_no("OUT"),
                    stock_out_type,
                    text_value(payload, "operator"),
                    text_value(payload, "handler"),
                    text_value(payload, "receiver"),
                    supplier_id,
                    now,
                    now,
                ),
            )
            for item in items:
                conn.execute(
                    """
                    insert into stock_out_item(id, stock_out_id, stock_item_id, qty, track_code)
                    values (?, ?, ?, ?, ?)
                    """,
                    (
                        item["id"],
                        stock_out_id,
                        item["stock_item_id"],
                        item["qty"],
                        item["track_code"],
                    ),
                )
            conn.commit()
        return {
            "id": stock_out_id,
            "status": "draft",
            "type": stock_out_type,
            "item_count": len(items),
        }

    @router.post("/api/stock-out/{stock_out_id}/confirm")
    def confirm_stock_out(stock_out_id: str):
        require_perm("warehouse.manage")   # 越权:改库存/采购主数据,写操作
        now = now_text()
        with connect(db_path) as conn:
            begin_immediate(conn)   # 抢写锁，防双击/多设备并发确认致库存超扣
            stock_out = _get_stock_out(conn, stock_out_id)
            if stock_out["status"] != "draft":
                raise HTTPException(status_code=409, detail="出库单已确认")
            items = conn.execute(
                """
                select id, stock_item_id, qty
                from stock_out_item
                where stock_out_id = ?
                order by id
                """,
                (stock_out_id,),
            ).fetchall()
            if not items:
                raise HTTPException(status_code=400, detail="出库单没有明细")
            required = {}
            for item in items:
                required[item["stock_item_id"]] = required.get(item["stock_item_id"], 0) + item["qty"]
            for stock_item_id, qty in required.items():
                if available_qty(conn, stock_item_id) < qty:
                    raise HTTPException(status_code=409, detail="库存不足")
            for item in items:
                deduct_balance(
                    conn,
                    stock_item_id=item["stock_item_id"],
                    qty=item["qty"],
                    source_type="stock_out",
                    source_id=stock_out_id,
                    source_item_id=item["id"],
                    note=stock_out["type"],
                )
            conn.execute(
                """
                update stock_out
                set status = 'confirmed', confirmed_at = ?, updated_at = ?
                where id = ?
                """,
                (now, now, stock_out_id),
            )
            conn.commit()
        return {"id": stock_out_id, "status": "confirmed"}

    return router
