from pathlib import Path

from fastapi import APIRouter
from local_app.auth import require_perm

from local_app.db import connect
from local_app.routes.warehouse_common import row_to_dict


def _is_detail(value):
    return str(value or "").strip().lower() in {"1", "true", "yes", "detail"}


def create_warehouse_stock_router(db_path):
    router = APIRouter()
    db_path = Path(db_path)

    @router.get("/api/stock-balance")
    def stock_balance(item: str = "", category_id: str = "", detail: str = ""):
        require_perm("warehouse.view")   # 越权:查看库存数据
        if _is_detail(detail):
            where = ["i.is_deleted = 0"]
            params = []
            if item.strip():
                where.append("(i.code = ? or i.name like ?)")
                params.extend([item.strip(), f"%{item.strip()}%"])
            if category_id.strip():
                where.append("i.category_id = ?")
                params.append(category_id.strip())
            with connect(db_path) as conn:
                rows = conn.execute(
                    f"""
                    select b.id as balance_id, b.stock_item_id, i.code, i.name,
                           i.category_id, c.name as category_name, i.unit,
                           b.batch_no, b.expire_date, b.track_code, b.qty, b.updated_at
                    from stock_balance b
                    join stock_item i on i.id = b.stock_item_id
                    left join stock_category c on c.id = i.category_id and c.is_deleted = 0
                    where {' and '.join(where)}
                    order by i.code, b.expire_date, b.batch_no
                    """,
                    params,
                ).fetchall()
        else:
            where = ["i.is_deleted = 0"]
            params = []
            if item.strip():
                where.append("(i.code = ? or i.name like ?)")
                params.extend([item.strip(), f"%{item.strip()}%"])
            if category_id.strip():
                where.append("i.category_id = ?")
                params.append(category_id.strip())
            with connect(db_path) as conn:
                rows = conn.execute(
                    f"""
                    select i.id as stock_item_id, i.code, i.name, i.category_id,
                           c.name as category_name, i.unit, i.min_qty, i.max_qty,
                           coalesce(sum(b.qty), 0) as qty
                    from stock_item i
                    left join stock_balance b on b.stock_item_id = i.id
                    left join stock_category c on c.id = i.category_id and c.is_deleted = 0
                    where {' and '.join(where)}
                    group by i.id
                    order by i.code, i.name
                    """,
                    params,
                ).fetchall()
        items = [row_to_dict(row) for row in rows]
        return {"items": items, "totalcount": len(items)}

    @router.get("/api/stock-balance/alerts")
    def stock_balance_alerts():
        require_perm("warehouse.view")   # 越权:查看库存数据
        with connect(db_path) as conn:
            rows = conn.execute(
                """
                select i.id as stock_item_id, i.code, i.name, i.category_id,
                       c.name as category_name, i.unit, i.min_qty,
                       coalesce(sum(b.qty), 0) as current_qty
                from stock_item i
                left join stock_balance b on b.stock_item_id = i.id
                left join stock_category c on c.id = i.category_id and c.is_deleted = 0
                where i.is_deleted = 0 and i.min_qty is not null
                group by i.id
                having current_qty < i.min_qty
                order by i.code, i.name
                """
            ).fetchall()
        alerts = []
        for row in rows:
            alert = row_to_dict(row)
            alert["reason"] = "low_stock"
            alerts.append(alert)
        return {"alerts": alerts, "totalcount": len(alerts)}

    return router
