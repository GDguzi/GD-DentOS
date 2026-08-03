from pathlib import Path

from fastapi import APIRouter, HTTPException

from local_app.timeutil import now_str
from local_app.auth import require_perm
from local_app.db import begin_immediate, connect, new_id
from local_app.routes.patient_common import require_patient   # 审计R2:软删患者按不存在处理(共享校验)

_HEALTH_LEVELS = {"", "健康", "良好", "一般", "不佳"}
_SEVERITIES = {"", "较轻", "中等", "严重"}




def create_oral_exams_router(db_path):
    router = APIRouter()
    db_path = Path(db_path)

    @router.get("/api/patients/{patient_identity}/oral-exams")
    def list_oral_exams(patient_identity: str):
        require_perm("oral_exam.manage")   # #482：口腔检查记录读守卫用本模块对应权限
        with connect(db_path) as conn:
            require_patient(conn, patient_identity)
            exam_rows = conn.execute(
                """
                select exam_id, exam_date, exam_doctor, health_level,
                       next_followup, note, created_at
                from oral_exams
                where patient_identity = ?
                order by coalesce(exam_date, '') desc, created_at desc
                """,
                (patient_identity,),
            ).fetchall()
            finding_rows = conn.execute(
                """
                select f.finding_id, f.exam_id, f.tooth, f.symptom, f.severity,
                       f.suggest_time, f.note
                from oral_exam_findings f
                join oral_exams e on e.exam_id = f.exam_id
                where e.patient_identity = ?
                order by f.exam_id, f.tooth, f.finding_id
                """,
                (patient_identity,),
            ).fetchall()

        findings_by_exam = {}
        for r in finding_rows:
            findings_by_exam.setdefault(r["exam_id"], []).append(
                {
                    "finding_id": r["finding_id"],
                    "tooth": r["tooth"],
                    "symptom": r["symptom"],
                    "severity": r["severity"],
                    "suggest_time": r["suggest_time"],
                    "note": r["note"],
                }
            )
        exams = [
            {
                "exam_id": r["exam_id"],
                "exam_date": r["exam_date"],
                "exam_doctor": r["exam_doctor"],
                "health_level": r["health_level"],
                "next_followup": r["next_followup"],
                "note": r["note"],
                "findings": findings_by_exam.get(r["exam_id"], []),
            }
            for r in exam_rows
        ]
        return {"exams": exams, "totalcount": len(exams)}

    @router.post("/api/patients/{patient_identity}/oral-exams")
    def create_oral_exam(patient_identity: str, payload: dict):
        require_perm("oral_exam.manage")
        health_level = str(payload.get("health_level") or "").strip()
        if health_level not in _HEALTH_LEVELS:
            raise HTTPException(status_code=400, detail="口腔健康程度取值非法")
        raw_findings = payload.get("findings") or []
        if not isinstance(raw_findings, list):
            raise HTTPException(status_code=400, detail="findings 必须是列表")

        findings = []
        for raw in raw_findings:
            if raw is not None and not isinstance(raw, dict):
                raise HTTPException(status_code=400, detail="findings 元素必须是对象")
            symptom = str((raw or {}).get("symptom") or "").strip()
            if not symptom:
                continue
            severity = str(raw.get("severity") or "").strip()
            if severity not in _SEVERITIES:
                raise HTTPException(status_code=400, detail="严重程度取值非法")
            findings.append(
                {
                    "finding_id": new_id("local-finding"),
                    "tooth": str(raw.get("tooth") or "").strip(),
                    "symptom": symptom,
                    "severity": severity,
                    "suggest_time": str(raw.get("suggest_time") or "").strip(),
                    "note": str(raw.get("note") or "").strip(),
                }
            )

        now = now_str()
        exam_id = new_id("local-exam")
        with connect(db_path) as conn:
            begin_immediate(conn)   # 审计R2复核:抢写锁使「查患者→写入」原子,合并事务无法插进两步之间(P1竞态窗口)
            require_patient(conn, patient_identity)
            conn.execute(
                """
                insert into oral_exams(
                    exam_id, patient_identity, exam_date, exam_doctor, health_level,
                    next_followup, source, note, created_at, updated_at
                )
                values (?, ?, ?, ?, ?, ?, 'local', ?, ?, ?)
                """,
                (
                    exam_id,
                    patient_identity,
                    str(payload.get("exam_date") or "").strip(),
                    str(payload.get("exam_doctor") or "").strip(),
                    health_level,
                    str(payload.get("next_followup") or "").strip(),
                    str(payload.get("note") or "").strip(),
                    now,
                    now,
                ),
            )
            for f in findings:
                conn.execute(
                    """
                    insert into oral_exam_findings(
                        finding_id, exam_id, tooth, symptom, severity, suggest_time, note
                    )
                    values (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        f["finding_id"], exam_id, f["tooth"], f["symptom"],
                        f["severity"], f["suggest_time"], f["note"],
                    ),
                )
            conn.commit()

        return {"exam_id": exam_id, "finding_count": len(findings)}

    return router
