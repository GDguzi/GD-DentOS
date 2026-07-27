"""患者专属标签(精品路线·大客户经营)：给患者打 VIP/怕疼/偏好/忌讳等自由标签。
打标签是常规前台/医生操作(非全店配置)，登录即可，但写审计可追溯。"""
import json

from fastapi import APIRouter, HTTPException

from local_app.timeutil import now_str
from local_app.auth import audit_operator, audit_write, require_perm
from local_app.db import connect, new_id

# 预设颜色，前端给快捷色板；name 自由填
TAG_COLORS = ("gray", "red", "orange", "green", "blue", "purple")


def _audit(conn, pid, action, detail):
    audit_write(conn, "patient_tag", pid, action,
                new_json=json.dumps(detail, ensure_ascii=False))


def create_patient_tags_router(db_path):
    router = APIRouter()

    @router.get("/api/patients/{pid}/tags")
    def list_tags(pid: str):
        require_perm("patient.view")   # 越权:读患者/病历数据
        with connect(db_path) as conn:
            rows = conn.execute(
                "select tag_id, name, color, created_at from patient_tags "
                "where patient_identity = ? order by created_at", (pid,),
            ).fetchall()
            return {"list": [dict(r) for r in rows]}

    @router.post("/api/patients/{pid}/tags")
    def add_tag(pid: str, payload: dict):
        require_perm("patient.edit")   # 越权:写患者/病历数据
        payload = payload or {}
        name = str(payload.get("name") or "").strip()
        if not name:
            raise HTTPException(status_code=400, detail="标签名不能为空")
        if len(name) > 12:
            raise HTTPException(status_code=400, detail="标签名最多12字")
        color = str(payload.get("color") or "gray").strip()
        if color not in TAG_COLORS:
            color = "gray"
        with connect(db_path) as conn:
            if not conn.execute("select 1 from patients where patient_identity = ?", (pid,)).fetchone():
                raise HTTPException(status_code=404, detail="patient not found")
            # 同名标签去重（同一患者不重复打同一标签）
            if conn.execute("select 1 from patient_tags where patient_identity = ? and name = ?", (pid, name)).fetchone():
                raise HTTPException(status_code=409, detail="该标签已存在")
            tag_id = new_id("tag")
            conn.execute(
                "insert into patient_tags(tag_id, patient_identity, name, color, created_at, operator) "
                "values (?, ?, ?, ?, ?, ?)",
                (tag_id, pid, name, color, now_str(), audit_operator()),
            )
            _audit(conn, pid, "add_tag", {"name": name, "color": color})
            conn.commit()
            return {"tag_id": tag_id, "name": name, "color": color}

    @router.delete("/api/patient-tags/{tag_id}")
    def delete_tag(tag_id: str):
        require_perm("patient.edit")   # 越权:写患者/病历数据
        with connect(db_path) as conn:
            row = conn.execute("select patient_identity, name from patient_tags where tag_id = ?", (tag_id,)).fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="标签不存在")
            conn.execute("delete from patient_tags where tag_id = ?", (tag_id,))
            _audit(conn, row["patient_identity"], "remove_tag", {"name": row["name"]})
            conn.commit()
            return {"ok": True}

    return router
