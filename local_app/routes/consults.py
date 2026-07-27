from pathlib import Path

from fastapi import APIRouter, HTTPException

from local_app.timeutil import now_str
from local_app.auth import require_perm
from local_app.db import begin_immediate, connect, new_id
from local_app.routes.patient_common import require_patient   # 审计R2:软删患者按不存在处理(共享校验)
from local_app.validation import valid_time_field

_TEXT_FIELDS = [
    "consult_date",
    "consult_doctor",
    "consult_type",
    "chief_complaint",
    "plan",
    "communication",
    "potential",
    "advice",
]




def create_consults_router(db_path):
    router = APIRouter()
    db_path = Path(db_path)

    @router.get("/api/patients/{patient_identity}/consults")
    def list_consults(patient_identity: str):
        require_perm("consult.manage")   # 咨询沟通记录读守卫用本模块对应权限(consultant/support 有 manage 能读自己的)
        with connect(db_path) as conn:
            require_patient(conn, patient_identity)
            rows = conn.execute(
                """
                select consult_id, consult_date, consult_doctor, consult_type,
                       chief_complaint, plan, communication, potential, advice,
                       created_at
                from consults
                where patient_identity = ?
                order by coalesce(consult_date, '') desc, created_at desc
                """,
                (patient_identity,),
            ).fetchall()
        consults = [
            {
                "consult_id": r["consult_id"],
                "consult_date": r["consult_date"],
                "consult_doctor": r["consult_doctor"],
                "consult_type": r["consult_type"],
                "chief_complaint": r["chief_complaint"],
                "plan": r["plan"],
                "communication": r["communication"],
                "potential": r["potential"],
                "advice": r["advice"],
            }
            for r in rows
        ]
        return {"consults": consults, "totalcount": len(consults)}

    @router.post("/api/patients/{patient_identity}/consults")
    def create_consult(patient_identity: str, payload: dict):
        require_perm("consult.manage")
        values = {f: str(payload.get(f) or "").strip() for f in _TEXT_FIELDS}
        values["consult_date"] = valid_time_field(values["consult_date"], "consult_date")  # 日期校验
        # 至少要有沟通内容之一，避免建空记录
        if not any(
            values[f] for f in ("chief_complaint", "plan", "communication", "advice")
        ):
            raise HTTPException(status_code=400, detail="面诊内容不能全为空")

        now = now_str()
        consult_id = new_id("local-consult")
        with connect(db_path) as conn:
            begin_immediate(conn)   # 审计R2复核:抢写锁使「查患者→写入」原子,合并事务无法插进两步之间(P1竞态窗口)
            require_patient(conn, patient_identity)
            conn.execute(
                """
                insert into consults(
                    consult_id, patient_identity, consult_date, consult_doctor,
                    consult_type, chief_complaint, plan, communication, potential,
                    advice, source, created_at, updated_at
                )
                values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'local', ?, ?)
                """,
                (
                    consult_id,
                    patient_identity,
                    values["consult_date"],
                    values["consult_doctor"],
                    values["consult_type"],
                    values["chief_complaint"],
                    values["plan"],
                    values["communication"],
                    values["potential"],
                    values["advice"],
                    now,
                    now,
                ),
            )
            conn.commit()
        return {"consult_id": consult_id}

    return router
