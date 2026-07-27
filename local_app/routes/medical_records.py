"""病历新建/更新(业务层)：写medical_records+版本快照+审计。"""
import json
from pathlib import Path

from fastapi import APIRouter, HTTPException

from local_app.timeutil import now_str
from local_app.auth import audit_write, require_perm
from local_app.db import begin_immediate, connect, new_id
from local_app.snapshots import json_payload_value, medical_record_snapshot, row_to_dict as _row_to_dict
from local_app.validation import valid_time_field
from local_app.versioning import stable_hash, stable_json


def create_medical_records_router(db_path):
    router = APIRouter()
    db_path = Path(db_path)

    @router.post("/api/medical-records")
    def create_medical_record(payload: dict):
        require_perm("medical_record.edit")
        patient_identity = str(payload.get("patient_identity", "")).strip()
        if not patient_identity:
            raise HTTPException(status_code=400, detail="patient_identity is required")
        record_type = str(payload.get("record_type") or "").strip()
        visit_time = valid_time_field(payload.get("visit_time"), "visit_time")
        doctor_name = str(payload.get("doctor_name") or "").strip()
        content_json = json_payload_value(payload.get("content_json"))
        tooth_json = json_payload_value(payload.get("tooth_json"))

        now = now_str()
        record_id = new_id("local")

        with connect(db_path) as conn:
            exists = conn.execute(
                "select 1 from patients where patient_identity = ?",
                (patient_identity,),
            ).fetchone()
            if not exists:
                raise HTTPException(status_code=404, detail="patient not found")

            snapshot = medical_record_snapshot(
                {
                    "record_id": record_id,
                    "patient_identity": patient_identity,
                    "study_identity": None,
                    "record_type": record_type,
                    "visit_time": visit_time,
                    "doctor_name": doctor_name,
                    "tooth_json": tooth_json,
                    "content_json": content_json,
                    "updated_at": now,
                }
            )
            snapshot["edit_source"] = "local_medical_record_create"
            new_hash = stable_hash(snapshot)

            conn.execute(
                """
                insert into medical_records(
                    record_id, patient_identity, record_type, visit_time, doctor_name,
                    tooth_json, content_json, draft_status, origin, ai_meta_json,
                    created_at, updated_at, current_hash
                )
                values (?, ?, ?, ?, ?, ?, ?, '', 'manual', '{}', ?, ?, ?)
                """,
                (
                    record_id,
                    patient_identity,
                    record_type,
                    visit_time,
                    doctor_name,
                    tooth_json,
                    content_json,
                    now,
                    now,
                    new_hash,
                ),
            )
            conn.execute(
                """
                insert into medical_record_versions(
                    record_id, version_hash, snapshot_json, batch_id
                )
                values (?, ?, ?, ?)
                """,
                (record_id, new_hash, stable_json(snapshot), None),
            )
            audit_write(
                conn,
                "medical_record",
                record_id,
                "create_medical_record",
                new_json=stable_json(snapshot),
                created_at=now,
            )
            conn.commit()
            created = conn.execute(
                "select * from medical_records where record_id = ?",
                (record_id,),
            ).fetchone()

        return {"medical_record": _row_to_dict(created)}

    @router.put("/api/medical-records/{record_id}")
    def update_medical_record(record_id: str, payload: dict):
        require_perm("medical_record.edit")
        editable_text_fields = ["record_type", "visit_time", "doctor_name"]
        updates = {
            field: str(payload.get(field) or "").strip()
            for field in editable_text_fields
            if field in payload
        }
        if "visit_time" in updates:
            updates["visit_time"] = valid_time_field(updates["visit_time"], "visit_time")
        # JSON null 按"未提供"处理：否则 null 会被规整成 "{}" 把整篇病历静默清空
        if payload.get("tooth_json") is not None:
            updates["tooth_json"] = json_payload_value(payload.get("tooth_json"))
        if payload.get("content_json") is not None:
            updates["content_json"] = json_payload_value(payload.get("content_json"))
        if not updates:
            raise HTTPException(status_code=400, detail="no editable fields")

        now = now_str()

        with connect(db_path) as conn:
            begin_immediate(conn)   # 抢写锁，防 iPad+PC 同改/双击保存病历致整篇丢更新+版本/审计重复
            record = conn.execute(
                "select * from medical_records where record_id = ?",
                (record_id,),
            ).fetchone()
            if not record:
                raise HTTPException(status_code=404, detail="medical record not found")

            old_snapshot = medical_record_snapshot(record)
            old_json = stable_json(old_snapshot)
            old_hash = record["current_hash"] or stable_hash(old_snapshot)
            new_snapshot = dict(old_snapshot)
            new_snapshot.update(updates)
            new_snapshot["updated_at"] = now
            new_snapshot["edit_source"] = "local_medical_record_edit"
            new_hash = stable_hash(new_snapshot)

            if new_hash != old_hash:
                conn.execute(
                    """
                    insert into medical_record_versions(
                        record_id, version_hash, snapshot_json, batch_id
                    )
                    values (?, ?, ?, ?)
                    """,
                    (record_id, old_hash, old_json, None),
                )

            conn.execute(
                """
                update medical_records
                set record_type = ?,
                    visit_time = ?,
                    doctor_name = ?,
                    tooth_json = ?,
                    content_json = ?,
                    updated_at = ?,
                    current_hash = ?
                where record_id = ?
                """,
                (
                    updates.get("record_type", record["record_type"]),
                    updates.get("visit_time", record["visit_time"]),
                    updates.get("doctor_name", record["doctor_name"]),
                    updates.get("tooth_json", record["tooth_json"]),
                    updates.get("content_json", record["content_json"]),
                    now,
                    new_hash,
                    record_id,
                ),
            )
            audit_write(
                conn,
                "medical_record",
                record_id,
                "update_medical_record",
                old_json=old_json,
                new_json=stable_json(new_snapshot),
                created_at=now,
            )
            conn.commit()
            updated = conn.execute(
                "select * from medical_records where record_id = ?",
                (record_id,),
            ).fetchone()

        return {"medical_record": _row_to_dict(updated)}

    return router
