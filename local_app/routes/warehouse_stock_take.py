from pathlib import Path

from fastapi import APIRouter, HTTPException
from local_app.auth import require_perm

from local_app.db import begin_immediate, connect
from local_app.routes.warehouse_common import (
    available_qty,
    change_balance,
    deduct_balance,
    doc_no,
    new_id,
    now_text,
    require_stock_item,
    row_to_dict,
    text_value,
    to_real,
)


def create_warehouse_stock_take_router(db_path):
    router = APIRouter()
    db_path = Path(db_path)

    def _get_stock_take(conn, stock_take_id):
        row = conn.execute(
            """
            select id, take_no, operator, status, is_deleted,
                   confirmed_at, created_at, updated_at
            from stock_take
            where id = ?
            """,
            (stock_take_id,),
        ).fetchone()
        if not row or row["is_deleted"]:
            raise HTTPException(status_code=404, detail="盘点单不存在")
        return row

    def _parse_items(conn, raw_items):
        if not isinstance(raw_items, list) or not raw_items:
            raise HTTPException(status_code=400, detail="盘点单至少需要一条明细")
        items = []
        for raw in raw_items:
            if not isinstance(raw, dict):
                raise HTTPException(status_code=400, detail="盘点明细必须是对象")
            stock_item_id = text_value(raw, "stock_item_id")
            require_stock_item(conn, stock_item_id)
            actual_qty = to_real(raw.get("actual_qty"))
            if actual_qty is None or actual_qty < 0:
                raise HTTPException(status_code=400, detail="实盘数不能小于 0")
            book_qty = available_qty(conn, stock_item_id)
            items.append(
                {
                    "id": new_id("stock-take-item"),
                    "stock_item_id": stock_item_id,
                    "book_qty": book_qty,
                    "actual_qty": actual_qty,
                    "diff": actual_qty - book_qty,
                }
            )
        return items

    @router.get("/api/stock-take")
    def list_stock_take(status: str = ""):
        require_perm("warehouse.view")   # 越权:查看库存数据
        where = ["t.is_deleted = 0"]
        params = []
        if status.strip():
            where.append("t.status = ?")
            params.append(status.strip())
        with connect(db_path) as conn:
            rows = conn.execute(
                f"""
                select t.id, t.take_no, t.operator, t.status, t.confirmed_at,
                       t.created_at, t.updated_at, count(i.id) as item_count
                from stock_take t
                left join stock_take_item i on i.stock_take_id = t.id
                where {' and '.join(where)}
                group by t.id
                order by t.created_at desc, t.take_no desc
                """,
                params,
            ).fetchall()
        stock_take = [row_to_dict(row) for row in rows]
        return {"stock_take": stock_take, "totalcount": len(stock_take)}

    @router.post("/api/stock-take")
    def create_stock_take(payload: dict):
        require_perm("warehouse.manage")   # 越权:改库存/采购主数据,写操作
        stock_take_id = new_id("stock-take")
        now = now_text()
        with connect(db_path) as conn:
            items = _parse_items(conn, payload.get("items"))
            conn.execute(
                """
                insert into stock_take(
                    id, take_no, operator, status, is_deleted, created_at, updated_at
                )
                values (?, ?, ?, 'draft', 0, ?, ?)
                """,
                (stock_take_id, doc_no("TAKE"), text_value(payload, "operator"), now, now),
            )
            for item in items:
                conn.execute(
                    """
                    insert into stock_take_item(
                        id, stock_take_id, stock_item_id, book_qty, actual_qty, diff
                    )
                    values (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        item["id"],
                        stock_take_id,
                        item["stock_item_id"],
                        item["book_qty"],
                        item["actual_qty"],
                        item["diff"],
                    ),
                )
            conn.commit()
        return {
            "id": stock_take_id,
            "status": "draft",
            "item_count": len(items),
            "items": [
                {
                    "stock_item_id": item["stock_item_id"],
                    "book_qty": item["book_qty"],
                    "actual_qty": item["actual_qty"],
                    "diff": item["diff"],
                }
                for item in items
            ],
        }

    @router.post("/api/stock-take/{stock_take_id}/confirm")
    def confirm_stock_take(stock_take_id: str):
        require_perm("warehouse.manage")   # 越权:改库存/采购主数据,写操作
        now = now_text()
        with connect(db_path) as conn:
            begin_immediate(conn)   # 抢写锁，防双击/多设备并发确认致盘点差异双应用
            stock_take = _get_stock_take(conn, stock_take_id)
            if stock_take["status"] != "draft":
                raise HTTPException(status_code=409, detail="盘点单已确认")
            items = conn.execute(
                """
                select id, stock_item_id, diff
                from stock_take_item
                where stock_take_id = ?
                order by id
                """,
                (stock_take_id,),
            ).fetchall()
            if not items:
                raise HTTPException(status_code=400, detail="盘点单没有明细")
            for item in items:
                diff = item["diff"]
                if diff > 0:
                    change_balance(
                        conn,
                        stock_item_id=item["stock_item_id"],
                        change_qty=diff,
                        source_type="stock_take",
                        source_id=stock_take_id,
                        source_item_id=item["id"],
                        note="gain",
                    )
                elif diff < 0:
                    if available_qty(conn, item["stock_item_id"]) < abs(diff):
                        raise HTTPException(status_code=409, detail="库存不足，无法盘亏")
                    deduct_balance(
                        conn,
                        stock_item_id=item["stock_item_id"],
                        qty=abs(diff),
                        source_type="stock_take",
                        source_id=stock_take_id,
                        source_item_id=item["id"],
                        note="loss",
                    )
            conn.execute(
                """
                update stock_take
                set status = 'confirmed', confirmed_at = ?, updated_at = ?
                where id = ?
                """,
                (now, now, stock_take_id),
            )
            conn.commit()
        return {"id": stock_take_id, "status": "confirmed"}

    return router
