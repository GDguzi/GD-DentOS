"""消毒/灭菌追溯 · 器械送消单(第1环)：临床→消毒供应室的交接登记。
状态机 draft草稿→submitted已送审→audited已审核；audited 可撤销审核回 submitted。
送消单号 dispatch_no 是全链路串联键。删除/编辑仅限 draft。规格：docs/官方规格/消毒.md。"""
import sqlite3
import uuid
from pathlib import Path

from fastapi import APIRouter, HTTPException

from local_app.timeutil import now_str
from local_app.auth import audit_write, require_perm
from local_app.db import begin_immediate, connect, new_id




def _row_to_dict(row):
    return {key: row[key] for key in row.keys()}


def _text(payload, key, default=""):
    return str(payload.get(key, default) or "").strip()


def _to_qty(value):
    try:
        q = float(value)
    except (TypeError, ValueError):
        return None
    if q != q or q in (float("inf"), float("-inf")) or q <= 0:   # 挡 nan/inf/非正
        return None
    return q


def create_sterilize_dispatch_router(db_path):
    router = APIRouter()
    db_path = Path(db_path)

    def _get_dispatch(conn, dispatch_id):
        row = conn.execute(
            "select id, dispatch_no, department, dispatcher, status, auditor, audited_at, note, "
            "is_deleted, created_at, updated_at from sterilize_dispatch where id = ?",
            (dispatch_id,),
        ).fetchone()
        if not row or row["is_deleted"]:
            raise HTTPException(status_code=404, detail="送消单不存在")
        return row

    def _items(conn, dispatch_id):
        rows = conn.execute(
            "select di.id, di.instrument_id, di.qty, i.code as instrument_code, i.name as instrument_name "
            "from sterilize_dispatch_item di "
            "left join instrument i on i.id = di.instrument_id "
            "where di.dispatch_id = ? order by di.rowid",
            (dispatch_id,),
        ).fetchall()
        return [_row_to_dict(r) for r in rows]

    def _validate_items(conn, raw_items):
        """校验明细：器械须存在、未删、启用；qty>0。返回规整后的 [(instrument_id, qty)]。"""
        if not isinstance(raw_items, list) or not raw_items:
            raise HTTPException(status_code=400, detail="送消单至少一项器械")
        out = []
        for it in raw_items:
            it = it or {}
            instrument_id = str(it.get("instrument_id") or "").strip()
            if not instrument_id:
                raise HTTPException(status_code=400, detail="器械必填")
            qty = _to_qty(it.get("qty", 1))
            if qty is None:
                raise HTTPException(status_code=400, detail="数量须为正数")
            row = conn.execute(
                "select status, is_deleted from instrument where id = ?", (instrument_id,)
            ).fetchone()
            if not row or row["is_deleted"]:
                raise HTTPException(status_code=400, detail="器械不存在")
            if row["status"] == "disabled":
                raise HTTPException(status_code=400, detail="器械已停用，不可送消")
            out.append((instrument_id, qty))
        return out

    @router.get("/api/dispatch")
    def list_dispatch(status: str = ""):
        require_perm("sterilize.manage")   # 越权:查看消毒/器械数据
        where = ["is_deleted = 0"]
        params = []
        if status.strip():
            where.append("status = ?")
            params.append(status.strip())
        with connect(db_path) as conn:
            rows = conn.execute(
                f"select id, dispatch_no, department, dispatcher, status, auditor, audited_at, created_at "
                f"from sterilize_dispatch where {' and '.join(where)} order by created_at desc, id desc",
                params,
            ).fetchall()
        items = [_row_to_dict(r) for r in rows]
        return {"dispatches": items, "totalcount": len(items)}

    @router.get("/api/dispatch/{dispatch_id}")
    def get_dispatch(dispatch_id: str):
        require_perm("sterilize.manage")   # 越权:查看消毒/器械数据
        with connect(db_path) as conn:
            row = _get_dispatch(conn, dispatch_id)
            data = _row_to_dict(row)
            data.pop("is_deleted", None)
            data["items"] = _items(conn, dispatch_id)
        return data

    @router.post("/api/dispatch")
    def create_dispatch(payload: dict):
        require_perm("sterilize.manage")   # 越权:改器械/送消单(含审核放行),写操作
        payload = payload or {}
        department = _text(payload, "department")
        dispatcher = _text(payload, "dispatcher")
        if not department:
            raise HTTPException(status_code=400, detail="送消科室必填")
        if not dispatcher:
            raise HTTPException(status_code=400, detail="送消人必填")
        with connect(db_path) as conn:
            items = _validate_items(conn, payload.get("items"))
            dispatch_id = new_id("disp")
            dispatch_no = _text(payload, "dispatch_no") or ("SX-" + uuid.uuid4().hex[:10].upper())
            now = now_str()
            try:
                conn.execute(
                    "insert into sterilize_dispatch(id, dispatch_no, department, dispatcher, status, "
                    "auditor, audited_at, note, is_deleted, created_at, updated_at) "
                    "values (?, ?, ?, ?, 'draft', '', '', ?, 0, ?, ?)",
                    (dispatch_id, dispatch_no, _text(payload, "department"), _text(payload, "dispatcher"),
                     _text(payload, "note"), now, now),
                )
            except sqlite3.IntegrityError as exc:
                raise HTTPException(status_code=409, detail="送消单号已存在") from exc
            for instrument_id, qty in items:
                conn.execute(
                    "insert into sterilize_dispatch_item(id, dispatch_id, instrument_id, qty, created_at) "
                    "values (?, ?, ?, ?, ?)",
                    (new_id("dispi"), dispatch_id, instrument_id, qty, now),
                )
            conn.commit()
            row = _get_dispatch(conn, dispatch_id)
            data = _row_to_dict(row)
            data.pop("is_deleted", None)
            data["items"] = _items(conn, dispatch_id)
        return data

    @router.put("/api/dispatch/{dispatch_id}")
    def update_dispatch(dispatch_id: str, payload: dict):
        require_perm("sterilize.manage")   # 越权:改器械/送消单(含审核放行),写操作
        payload = payload or {}
        with connect(db_path) as conn:
            begin_immediate(conn)   # 抢写锁，防"送审后被另一请求按旧草稿状态编辑"
            current = _get_dispatch(conn, dispatch_id)
            if current["status"] != "draft":
                raise HTTPException(status_code=409, detail="仅草稿可编辑")
            now = now_str()
            # 头部字段
            conn.execute(
                "update sterilize_dispatch set department = ?, dispatcher = ?, note = ?, updated_at = ? where id = ?",
                (_text(payload, "department", current["department"]),
                 _text(payload, "dispatcher", current["dispatcher"]),
                 _text(payload, "note", current["note"]), now, dispatch_id),
            )
            # 明细：给了 items 才整体替换
            if "items" in payload:
                items = _validate_items(conn, payload.get("items"))
                conn.execute("delete from sterilize_dispatch_item where dispatch_id = ?", (dispatch_id,))
                for instrument_id, qty in items:
                    conn.execute(
                        "insert into sterilize_dispatch_item(id, dispatch_id, instrument_id, qty, created_at) "
                        "values (?, ?, ?, ?, ?)",
                        (new_id("dispi"), dispatch_id, instrument_id, qty, now),
                    )
            conn.commit()
            row = _get_dispatch(conn, dispatch_id)
            data = _row_to_dict(row)
            data.pop("is_deleted", None)
            data["items"] = _items(conn, dispatch_id)
        return data

    @router.delete("/api/dispatch/{dispatch_id}")
    def delete_dispatch(dispatch_id: str):
        require_perm("sterilize.manage")   # 越权:改器械/送消单(含审核放行),写操作
        with connect(db_path) as conn:
            begin_immediate(conn)   # 抢写锁，防"送审后被另一请求按旧草稿状态删除"
            current = _get_dispatch(conn, dispatch_id)
            if current["status"] != "draft":
                raise HTTPException(status_code=409, detail="仅草稿可删除")
            now = now_str()
            conn.execute(
                "update sterilize_dispatch set is_deleted = 1, updated_at = ? where id = ?",
                (now, dispatch_id),
            )
            # #93 删除写审计(回收站要显示谁/何时删)
            audit_write(conn, "sterilize_dispatch", dispatch_id, "soft_delete_dispatch", created_at=now)
            conn.commit()
        return {"id": dispatch_id, "is_deleted": 1}

    @router.post("/api/dispatch/{dispatch_id}/submit")
    def submit_dispatch(dispatch_id: str):
        require_perm("sterilize.manage")   # 越权:改器械/送消单(含审核放行),写操作
        with connect(db_path) as conn:
            begin_immediate(conn)   # 抢写锁，防双击/并发送审 + 与编辑/删除互相覆盖
            current = _get_dispatch(conn, dispatch_id)
            if current["status"] != "draft":
                raise HTTPException(status_code=409, detail="仅草稿可送审")
            if not str(current["department"] or "").strip() or not str(current["dispatcher"] or "").strip():
                raise HTTPException(status_code=400, detail="送消科室和送消人必填后才能送审")
            conn.execute(
                "update sterilize_dispatch set status = 'submitted', updated_at = ? where id = ?",
                (now_str(), dispatch_id),
            )
            conn.commit()
            row = _get_dispatch(conn, dispatch_id)
        data = _row_to_dict(row)
        data.pop("is_deleted", None)
        return data

    @router.post("/api/dispatch/{dispatch_id}/audit")
    def audit_dispatch(dispatch_id: str, payload: dict = None):
        require_perm("sterilize.manage")   # 越权:改器械/送消单(含审核放行),写操作
        payload = payload or {}
        auditor = _text(payload, "auditor")
        if not auditor:
            raise HTTPException(status_code=400, detail="审核人必填")
        with connect(db_path) as conn:
            begin_immediate(conn)   # 抢写锁，防双击/并发审核致重复审计
            current = _get_dispatch(conn, dispatch_id)
            if current["status"] != "submitted":
                raise HTTPException(status_code=409, detail="仅已送审可审核")
            now = now_str()
            conn.execute(
                "update sterilize_dispatch set status = 'audited', auditor = ?, audited_at = ?, updated_at = ? where id = ?",
                (auditor, now, now, dispatch_id),
            )
            conn.commit()
            row = _get_dispatch(conn, dispatch_id)
        data = _row_to_dict(row)
        data.pop("is_deleted", None)
        return data

    @router.post("/api/dispatch/{dispatch_id}/cancel-audit")
    def cancel_audit_dispatch(dispatch_id: str, payload: dict = None):
        require_perm("sterilize.manage")   # 越权:改器械/送消单(含审核放行),写操作
        payload = payload or {}
        operator = _text(payload, "operator")
        if not operator:
            raise HTTPException(status_code=400, detail="撤销操作人必填")
        with connect(db_path) as conn:
            begin_immediate(conn)   # 抢写锁，防双击/并发撤审致重复审计
            current = _get_dispatch(conn, dispatch_id)
            if current["status"] != "audited":
                raise HTTPException(status_code=409, detail="仅已审核可撤销审核")
            now = now_str()
            conn.execute(
                "update sterilize_dispatch set status = 'submitted', auditor = '', audited_at = '', updated_at = ? where id = ?",
                (now, dispatch_id),
            )
            audit_write(
                conn, "sterilize_dispatch", dispatch_id, "cancel_audit",
                new_json='{"from": "audited", "to": "submitted"}',
                operator=operator, created_at=now,
            )
            conn.commit()
            row = _get_dispatch(conn, dispatch_id)
        data = _row_to_dict(row)
        data.pop("is_deleted", None)
        return data

    return router
