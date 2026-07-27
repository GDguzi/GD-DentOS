from pathlib import Path

from fastapi import APIRouter, HTTPException
from local_app.timeutil import now_str
from local_app.auth import require_perm

from local_app.db import connect, new_id




def _new_id():
    return new_id("supplier")


def _row_to_dict(row):
    return {key: row[key] for key in row.keys()}


def _text(payload, key, default=""):
    return str(payload.get(key, default) or "").strip()


def create_warehouse_suppliers_router(db_path):
    router = APIRouter()
    db_path = Path(db_path)

    def _get_supplier(conn, supplier_id):
        row = conn.execute(
            """
            select id, name, contact, phone, note, is_deleted, created_at, updated_at
            from supplier
            where id = ?
            """,
            (supplier_id,),
        ).fetchone()
        if not row or row["is_deleted"]:
            raise HTTPException(status_code=404, detail="供应商不存在")
        return row

    @router.get("/api/suppliers")
    def list_suppliers(name: str = ""):
        require_perm("warehouse.view")   # 越权:查看库存数据
        where = ["is_deleted = 0"]
        params = []
        if name.strip():
            where.append("name like ?")
            params.append(f"%{name.strip()}%")
        with connect(db_path) as conn:
            rows = conn.execute(
                f"""
                select id, name, contact, phone, note, created_at, updated_at
                from supplier
                where {' and '.join(where)}
                order by name
                """,
                params,
            ).fetchall()
        suppliers = [_row_to_dict(row) for row in rows]
        return {"suppliers": suppliers, "totalcount": len(suppliers)}

    @router.post("/api/suppliers")
    def create_supplier(payload: dict):
        require_perm("warehouse.manage")   # 越权:改库存/采购主数据,写操作
        name = _text(payload, "name")
        if not name:
            raise HTTPException(status_code=400, detail="供应商名称不能为空")
        supplier_id = _new_id()
        now = now_str()
        with connect(db_path) as conn:
            conn.execute(
                """
                insert into supplier(
                    id, name, contact, phone, note, is_deleted, created_at, updated_at
                )
                values (?, ?, ?, ?, ?, 0, ?, ?)
                """,
                (
                    supplier_id,
                    name,
                    _text(payload, "contact"),
                    _text(payload, "phone"),
                    _text(payload, "note"),
                    now,
                    now,
                ),
            )
            conn.commit()
            row = _get_supplier(conn, supplier_id)
        return _row_to_dict(row)

    @router.put("/api/suppliers/{supplier_id}")
    def update_supplier(supplier_id: str, payload: dict):
        require_perm("warehouse.manage")   # 越权:改库存/采购主数据,写操作
        with connect(db_path) as conn:
            current = _get_supplier(conn, supplier_id)
            name = _text(payload, "name", current["name"])
            if not name:
                raise HTTPException(status_code=400, detail="供应商名称不能为空")
            conn.execute(
                """
                update supplier
                set name = ?, contact = ?, phone = ?, note = ?, updated_at = ?
                where id = ?
                """,
                (
                    name,
                    _text(payload, "contact", current["contact"]),
                    _text(payload, "phone", current["phone"]),
                    _text(payload, "note", current["note"]),
                    now_str(),
                    supplier_id,
                ),
            )
            conn.commit()
            row = _get_supplier(conn, supplier_id)
        return _row_to_dict(row)

    @router.delete("/api/suppliers/{supplier_id}")
    def delete_supplier(supplier_id: str):
        require_perm("warehouse.manage")   # 越权:改库存/采购主数据,写操作
        with connect(db_path) as conn:
            _get_supplier(conn, supplier_id)
            conn.execute(
                "update supplier set is_deleted = 1, updated_at = ? where id = ?",
                (now_str(), supplier_id),
            )
            conn.commit()
        return {"id": supplier_id, "is_deleted": 1}

    return router
