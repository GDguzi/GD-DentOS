import math
import uuid

from fastapi import HTTPException

from local_app.db import new_id
from local_app.timeutil import bj_now, now_str


def now_text():
    return now_str()


def doc_no(prefix):
    return f"{prefix}{bj_now().strftime('%Y%m%d%H%M%S')}{uuid.uuid4().hex[:4]}"


def row_to_dict(row):
    return {key: row[key] for key in row.keys()}


def text_value(payload, key, default=""):
    return str(payload.get(key, default) or "").strip()


def to_real(value, default=None):
    if value in (None, ""):
        return default
    try:
        f = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(f):   # nan/inf 视为无效(否则绕过 require_positive_qty 污染库存)
        return default
    return f


def require_positive_qty(value):
    qty = to_real(value)
    if qty is None or qty <= 0:
        raise HTTPException(status_code=400, detail="数量必须大于 0")
    return qty


def require_stock_item(conn, stock_item_id):
    row = conn.execute(
        """
        select id
        from stock_item
        where id = ? and is_deleted = 0
        """,
        (stock_item_id,),
    ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="物品不存在")


def require_supplier(conn, supplier_id):
    if not supplier_id:
        return
    row = conn.execute(
        """
        select id
        from supplier
        where id = ? and is_deleted = 0
        """,
        (supplier_id,),
    ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="供应商不存在")


def change_balance(
    conn,
    *,
    stock_item_id,
    change_qty,
    source_type,
    source_id,
    source_item_id="",
    batch_no="",
    expire_date="",
    track_code="",
    note="",
):
    now = now_text()
    batch_no = batch_no or ""
    expire_date = expire_date or ""
    track_code = track_code or ""
    row = conn.execute(
        """
        select id, qty
        from stock_balance
        where stock_item_id = ? and batch_no = ? and expire_date = ? and track_code = ?
        """,
        (stock_item_id, batch_no, expire_date, track_code),
    ).fetchone()
    if row:
        balance_id = row["id"]
        balance_qty = row["qty"] + change_qty
        conn.execute(
            "update stock_balance set qty = ?, updated_at = ? where id = ?",
            (balance_qty, now, balance_id),
        )
    else:
        balance_id = new_id("stock-bal")
        balance_qty = change_qty
        conn.execute(
            """
            insert into stock_balance(
                id, stock_item_id, batch_no, expire_date, track_code, qty, updated_at
            )
            values (?, ?, ?, ?, ?, ?, ?)
            """,
            (balance_id, stock_item_id, batch_no, expire_date, track_code, balance_qty, now),
        )
    conn.execute(
        """
        insert into stock_ledger(
            id, stock_item_id, change_qty, balance_qty, source_type, source_id,
            source_item_id, batch_no, expire_date, track_code, note, created_at
        )
        values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            new_id("stock-ledger"),
            stock_item_id,
            change_qty,
            balance_qty,
            source_type,
            source_id,
            source_item_id,
            batch_no,
            expire_date,
            track_code,
            note,
            now,
        ),
    )
    return balance_qty


def available_qty(conn, stock_item_id):
    row = conn.execute(
        """
        select coalesce(sum(qty), 0) as qty
        from stock_balance
        where stock_item_id = ?
        """,
        (stock_item_id,),
    ).fetchone()
    return row["qty"] if row else 0


def deduct_balance(
    conn,
    *,
    stock_item_id,
    qty,
    source_type,
    source_id,
    source_item_id="",
    note="",
):
    remaining = qty
    rows = conn.execute(
        """
        select id, batch_no, expire_date, track_code, qty
        from stock_balance
        where stock_item_id = ? and qty > 0
        order by
            case when expire_date = '' then 1 else 0 end,
            expire_date,
            updated_at,
            id
        """,
        (stock_item_id,),
    ).fetchall()
    for row in rows:
        if remaining <= 0:
            break
        take_qty = min(row["qty"], remaining)
        change_balance(
            conn,
            stock_item_id=stock_item_id,
            change_qty=-take_qty,
            source_type=source_type,
            source_id=source_id,
            source_item_id=source_item_id,
            batch_no=row["batch_no"],
            expire_date=row["expire_date"],
            track_code=row["track_code"],
            note=note,
        )
        remaining -= take_qty
    if remaining > 0:
        raise HTTPException(status_code=409, detail="库存不足")
