"""患者来源三级树 CRUD(本地可配置)。对齐官方"患者来源设置"页：
一级/二级/三级 增删改 + 上移下移 + 隐藏。"""
import json
import uuid
from pathlib import Path

from fastapi import APIRouter, HTTPException

from local_app.auth import audit_write, require_admin
from local_app.db import connect


def _audit(conn, sid, action, detail):
    """来源树变更写审计(谁/何时/改了什么)。"""
    audit_write(conn, "referral_source", sid, action,
                new_json=json.dumps(detail, ensure_ascii=False))


def _descendants(conn, sid):
    """sid 自身 + 所有子孙 source_id(删除级联用)。"""
    out, frontier = [sid], [sid]
    while frontier:
        kids = []
        for f in frontier:
            for r in conn.execute(
                "select source_id from referral_sources where parent_id = ?", (f,)
            ).fetchall():
                kids.append(r["source_id"])
        out += kids
        frontier = kids
    return out


def create_referral_sources_router(db_path):
    router = APIRouter()
    db_path = Path(db_path)

    @router.get("/api/referral-sources")
    def list_sources(include_hidden: int = 0):
        where = "" if include_hidden else "where hidden = 0 "
        with connect(db_path) as conn:
            rows = conn.execute(
                "select source_id, parent_id, level, name, display_order, hidden "
                "from referral_sources " + where +
                "order by level, display_order, name"
            ).fetchall()
        return {"list": [dict(r) for r in rows]}

    @router.post("/api/referral-sources")
    def add_source(payload: dict):
        require_admin()   # 全店配置写操作仅管理员(与 settings/rooms 等口径一致)
        name = str((payload or {}).get("name") or "").strip()
        if not name:
            raise HTTPException(status_code=400, detail="名称不能为空")
        parent_id = str(payload.get("parent_id") or "").strip()
        with connect(db_path) as conn:
            level = 1
            if parent_id:
                prow = conn.execute(
                    "select level from referral_sources where source_id = ?", (parent_id,)
                ).fetchone()
                if not prow:
                    raise HTTPException(status_code=404, detail="父级不存在")
                level = prow["level"] + 1
                if level > 3:
                    raise HTTPException(status_code=400, detail="患者来源最多三级")
            order = conn.execute(
                "select coalesce(max(display_order), -1) + 1 from referral_sources where parent_id = ?",
                (parent_id,),
            ).fetchone()[0]
            sid = "rs-" + uuid.uuid4().hex[:10]
            conn.execute(
                "insert into referral_sources(source_id, parent_id, level, name, display_order) "
                "values (?, ?, ?, ?, ?)",
                (sid, parent_id, level, name, order),
            )
            _audit(conn, sid, "add_referral_source", {"name": name, "level": level, "parent_id": parent_id})
            conn.commit()
        return {"source_id": sid, "level": level}

    @router.put("/api/referral-sources/{sid}")
    def update_source(sid: str, payload: dict):
        require_admin()
        payload = payload or {}
        with connect(db_path) as conn:
            if not conn.execute(
                "select 1 from referral_sources where source_id = ?", (sid,)
            ).fetchone():
                raise HTTPException(status_code=404, detail="来源项不存在")
            sets, vals = [], []
            if "name" in payload:
                nm = str(payload.get("name") or "").strip()
                if not nm:
                    raise HTTPException(status_code=400, detail="名称不能为空")
                sets.append("name = ?"); vals.append(nm)
            if "hidden" in payload:
                sets.append("hidden = ?"); vals.append(1 if payload.get("hidden") else 0)
            if sets:
                vals.append(sid)
                conn.execute(
                    "update referral_sources set " + ", ".join(sets) + " where source_id = ?", vals
                )
                _audit(conn, sid, "update_referral_source", payload)
                conn.commit()
        return {"ok": True}

    @router.delete("/api/referral-sources/{sid}")
    def delete_source(sid: str):
        require_admin()
        with connect(db_path) as conn:
            if not conn.execute(
                "select 1 from referral_sources where source_id = ?", (sid,)
            ).fetchone():
                raise HTTPException(status_code=404, detail="来源项不存在")
            ids = _descendants(conn, sid)
            conn.executemany(
                "delete from referral_sources where source_id = ?", [(i,) for i in ids]
            )
            _audit(conn, sid, "delete_referral_source", {"deleted_ids": ids})
            conn.commit()
        return {"ok": True, "deleted": len(ids)}

    @router.post("/api/referral-sources/{sid}/move")
    def move_source(sid: str, payload: dict):
        require_admin()
        direction = str((payload or {}).get("dir") or "").strip()
        if direction not in ("up", "down"):
            raise HTTPException(status_code=400, detail="dir 须为 up/down")
        with connect(db_path) as conn:
            row = conn.execute(
                "select parent_id, display_order from referral_sources where source_id = ?", (sid,)
            ).fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="来源项不存在")
            op, order_dir = ("<", "desc") if direction == "up" else (">", "asc")
            sib = conn.execute(
                f"select source_id, display_order from referral_sources "
                f"where parent_id = ? and display_order {op} ? order by display_order {order_dir} limit 1",
                (row["parent_id"], row["display_order"]),
            ).fetchone()
            if sib:
                conn.execute(
                    "update referral_sources set display_order = ? where source_id = ?",
                    (sib["display_order"], sid),
                )
                conn.execute(
                    "update referral_sources set display_order = ? where source_id = ?",
                    (row["display_order"], sib["source_id"]),
                )
                _audit(conn, sid, "move_referral_source", {"dir": direction, "swap_with": sib["source_id"]})
                conn.commit()
        return {"ok": True}

    return router
