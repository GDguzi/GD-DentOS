from pathlib import Path

from fastapi import APIRouter, HTTPException

from local_app.timeutil import now_str
from local_app.auth import require_perm
from local_app.db import begin_immediate, connect, new_id
from local_app.routes.patient_common import require_patient   # 审计R2:软删患者按不存在处理(共享校验)
from local_app.validation import valid_time_field

_TEXT_FIELDS = [
    "surgery_date",
    "surgeon",
    "tooth",
    "anesthesia",
    "process",
    "postop_advice",
]




def create_surgeries_router(db_path):
    router = APIRouter()
    db_path = Path(db_path)

    @router.get("/api/patients/{patient_identity}/surgeries")
    def list_surgeries(patient_identity: str):
        require_perm("surgery.manage")   # #482：手术记录读守卫用本模块对应权限
        with connect(db_path) as conn:
            require_patient(conn, patient_identity)
            rows = conn.execute(
                """
                select surgery_id, surgery_date, surgeon, surgery_name, tooth,
                       anesthesia, process, postop_advice, created_at
                from surgeries
                where patient_identity = ?
                order by coalesce(surgery_date, '') desc, created_at desc
                """,
                (patient_identity,),
            ).fetchall()
        surgeries = [
            {
                "surgery_id": r["surgery_id"],
                "surgery_date": r["surgery_date"],
                "surgeon": r["surgeon"],
                "surgery_name": r["surgery_name"],
                "tooth": r["tooth"],
                "anesthesia": r["anesthesia"],
                "process": r["process"],
                "postop_advice": r["postop_advice"],
            }
            for r in rows
        ]
        return {"surgeries": surgeries, "totalcount": len(surgeries)}

    @router.post("/api/patients/{patient_identity}/surgeries")
    def create_surgery(patient_identity: str, payload: dict):
        require_perm("surgery.manage")
        surgery_name = str(payload.get("surgery_name") or "").strip()
        if not surgery_name:
            raise HTTPException(status_code=400, detail="手术名称不能为空")
        values = {f: str(payload.get(f) or "").strip() for f in _TEXT_FIELDS}
        values["surgery_date"] = valid_time_field(values["surgery_date"], "surgery_date")  # #15 日期校验

        now = now_str()
        surgery_id = new_id("local-surgery")
        with connect(db_path) as conn:
            begin_immediate(conn)   # 审计R2复核:抢写锁使「查患者→写入」原子,合并事务无法插进两步之间(P1竞态窗口)
            require_patient(conn, patient_identity)
            conn.execute(
                """
                insert into surgeries(
                    surgery_id, patient_identity, surgery_date, surgeon, surgery_name,
                    tooth, anesthesia, process, postop_advice, source, created_at, updated_at
                )
                values (?, ?, ?, ?, ?, ?, ?, ?, ?, 'local', ?, ?)
                """,
                (
                    surgery_id,
                    patient_identity,
                    values["surgery_date"],
                    values["surgeon"],
                    surgery_name,
                    values["tooth"],
                    values["anesthesia"],
                    values["process"],
                    values["postop_advice"],
                    now,
                    now,
                ),
            )
            conn.commit()
        return {"surgery_id": surgery_id}

    return router
