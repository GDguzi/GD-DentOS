"""消毒/灭菌追溯 · 器械库(首期)：器械分类 + 器械主数据 CRUD + 停用。
器械库是整条追溯链(送消→消毒→发放)的主数据源，先做单条增删改查，Excel导入二期。"""
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


def create_sterilization_router(db_path):
    router = APIRouter()
    db_path = Path(db_path)

    def _category_exists(conn, category_id):
        if not category_id:
            return True
        return conn.execute(
            "select 1 from instrument_category where id = ? and is_deleted = 0",
            (category_id,),
        ).fetchone() is not None

    def _get_category(conn, category_id):
        row = conn.execute(
            "select id, name, parent_id, sort, is_deleted, created_at, updated_at "
            "from instrument_category where id = ?",
            (category_id,),
        ).fetchone()
        if not row or row["is_deleted"]:
            raise HTTPException(status_code=404, detail="器械分类不存在")
        return row

    def _get_instrument(conn, instrument_id):
        row = conn.execute(
            "select id, code, name, category_id, status, spec, note, is_deleted, created_at, updated_at "
            "from instrument where id = ?",
            (instrument_id,),
        ).fetchone()
        if not row or row["is_deleted"]:
            raise HTTPException(status_code=404, detail="器械不存在")
        return row

    # ---------- 器械分类 ----------
    @router.get("/api/instrument-categories")
    def list_categories():
        require_perm("sterilize.manage")   # 越权:查看消毒/器械数据
        with connect(db_path) as conn:
            rows = conn.execute(
                "select id, name, parent_id, sort, created_at, updated_at "
                "from instrument_category where is_deleted = 0 order by sort, name"
            ).fetchall()
        categories = [_row_to_dict(row) for row in rows]
        return {"categories": categories, "totalcount": len(categories)}

    @router.post("/api/instrument-categories")
    def create_category(payload: dict):
        require_perm("sterilize.manage")   # 越权:改器械/送消单(含审核放行),写操作
        name = _text(payload, "name")
        if not name:
            raise HTTPException(status_code=400, detail="分类名称不能为空")
        category_id = new_id("instr-cat")
        now = now_str()
        with connect(db_path) as conn:
            conn.execute(
                "insert into instrument_category(id, name, parent_id, sort, is_deleted, created_at, updated_at) "
                "values (?, ?, ?, ?, 0, ?, ?)",
                (category_id, name, _text(payload, "parent_id"), _to_int(payload.get("sort"), 0), now, now),
            )
            conn.commit()
            row = _get_category(conn, category_id)
        return _row_to_dict(row)

    @router.put("/api/instrument-categories/{category_id}")
    def update_category(category_id: str, payload: dict):
        require_perm("sterilize.manage")   # 越权:改器械/送消单(含审核放行),写操作
        with connect(db_path) as conn:
            current = _get_category(conn, category_id)
            name = _text(payload, "name", current["name"])
            if not name:
                raise HTTPException(status_code=400, detail="分类名称不能为空")
            conn.execute(
                "update instrument_category set name = ?, parent_id = ?, sort = ?, updated_at = ? where id = ?",
                (name, _text(payload, "parent_id", current["parent_id"]),
                 _to_int(payload.get("sort"), current["sort"]), now_str(), category_id),
            )
            conn.commit()
            row = _get_category(conn, category_id)
        return _row_to_dict(row)

    @router.delete("/api/instrument-categories/{category_id}")
    def delete_category(category_id: str):
        require_perm("sterilize.manage")   # 越权:改器械/送消单(含审核放行),写操作
        with connect(db_path) as conn:
            _get_category(conn, category_id)
            # 后端校验：分类下还有器械则不允许删除(避免孤儿器械,与"分类必填"一致)
            n = conn.execute(
                "select count(*) from instrument where category_id = ? and coalesce(is_deleted, 0) = 0",
                (category_id,),
            ).fetchone()[0]
            if n:
                raise HTTPException(status_code=409, detail=f"该分类下还有 {n} 件器械，请先移走再删除")
            conn.execute(
                "update instrument_category set is_deleted = 1, updated_at = ? where id = ?",
                (now_str(), category_id),
            )
            conn.commit()
        return {"id": category_id, "is_deleted": 1}

    # ---------- 器械主数据 ----------
    @router.get("/api/instruments")
    def list_instruments(code: str = "", name: str = "", category_id: str = "", status: str = ""):
        require_perm("sterilize.manage")   # 越权:查看消毒/器械数据
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
        if status.strip():
            where.append("i.status = ?")
            params.append(status.strip())
        with connect(db_path) as conn:
            rows = conn.execute(
                f"select i.id, i.code, i.name, i.category_id, c.name as category_name, "
                f"i.status, i.spec, i.note, i.created_at, i.updated_at "
                f"from instrument i "
                f"left join instrument_category c on c.id = i.category_id and c.is_deleted = 0 "
                f"where {' and '.join(where)} order by i.code, i.name",
                params,
            ).fetchall()
        items = [_row_to_dict(row) for row in rows]
        return {"instruments": items, "totalcount": len(items)}

    @router.post("/api/instruments")
    def create_instrument(payload: dict):
        require_perm("sterilize.manage")   # 越权:改器械/送消单(含审核放行),写操作
        code = _text(payload, "code")
        name = _text(payload, "name")
        if not code or not name:
            raise HTTPException(status_code=400, detail="器械编号和名称不能为空")
        category_id = _text(payload, "category_id")
        if not category_id:
            raise HTTPException(status_code=400, detail="器械分类必填")
        status = _text(payload, "status", "active") or "active"
        instrument_id = new_id("instr")
        now = now_str()
        with connect(db_path) as conn:
            if not _category_exists(conn, category_id):
                raise HTTPException(status_code=400, detail="器械分类不存在")
            try:
                conn.execute(
                    "insert into instrument(id, code, name, category_id, status, spec, note, "
                    "is_deleted, created_at, updated_at) values (?, ?, ?, ?, ?, ?, ?, 0, ?, ?)",
                    (instrument_id, code, name, category_id, status,
                     _text(payload, "spec"), _text(payload, "note"), now, now),
                )
                conn.commit()
            except sqlite3.IntegrityError as exc:
                raise HTTPException(status_code=409, detail="器械编号已存在") from exc
            row = _get_instrument(conn, instrument_id)
        return _row_to_dict(row)

    @router.put("/api/instruments/{instrument_id}")
    def update_instrument(instrument_id: str, payload: dict):
        require_perm("sterilize.manage")   # 越权:改器械/送消单(含审核放行),写操作
        with connect(db_path) as conn:
            current = _get_instrument(conn, instrument_id)
            code = _text(payload, "code", current["code"])
            name = _text(payload, "name", current["name"])
            if not code or not name:
                raise HTTPException(status_code=400, detail="器械编号和名称不能为空")
            category_id = _text(payload, "category_id", current["category_id"])
            if not category_id:   # 编辑也不允许清空分类
                raise HTTPException(status_code=400, detail="器械分类必填")
            if not _category_exists(conn, category_id):
                raise HTTPException(status_code=400, detail="器械分类不存在")
            try:
                conn.execute(
                    "update instrument set code = ?, name = ?, category_id = ?, status = ?, "
                    "spec = ?, note = ?, updated_at = ? where id = ?",
                    (code, name, category_id, _text(payload, "status", current["status"]) or "active",
                     _text(payload, "spec", current["spec"]), _text(payload, "note", current["note"]),
                     now_str(), instrument_id),
                )
                conn.commit()
            except sqlite3.IntegrityError as exc:
                raise HTTPException(status_code=409, detail="器械编号已存在") from exc
            row = _get_instrument(conn, instrument_id)
        return _row_to_dict(row)

    @router.put("/api/instruments/{instrument_id}/disable")
    def disable_instrument(instrument_id: str):
        """停用：不删但不可再被新单据选用（保留历史追溯）。"""
        require_perm("sterilize.manage")   # 越权:改器械/送消单(含审核放行),写操作
        with connect(db_path) as conn:
            _get_instrument(conn, instrument_id)
            conn.execute(
                "update instrument set status = 'disabled', updated_at = ? where id = ?",
                (now_str(), instrument_id),
            )
            conn.commit()
            row = _get_instrument(conn, instrument_id)
        return _row_to_dict(row)

    @router.delete("/api/instruments/{instrument_id}")
    def delete_instrument(instrument_id: str):
        require_perm("sterilize.manage")   # 越权:改器械/送消单(含审核放行),写操作
        with connect(db_path) as conn:
            _get_instrument(conn, instrument_id)
            conn.execute(
                "update instrument set is_deleted = 1, updated_at = ? where id = ?",
                (now_str(), instrument_id),
            )
            conn.commit()
        return {"id": instrument_id, "is_deleted": 1}

    return router
