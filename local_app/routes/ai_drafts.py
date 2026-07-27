import difflib
import json
from pathlib import Path

from fastapi import APIRouter, HTTPException

from local_app.timeutil import now_str
from local_app.auth import audit_write, require_perm
from local_app.db import begin_immediate, connect, new_id
from local_app.snapshots import (
    json_payload_value,
    medical_record_snapshot,
    safe_json_object,
)
from local_app.versioning import stable_hash, stable_json




def _edit_tier(original_json, new_json):
    # "none" 必须是真正未改动（字节相等）。原先用 ratio>=0.98 当"无改"，
    # 但对长病历，医生加十几个字的真实修改相似度仍 >=0.98 被误判为"无改"，
    # 会高估「无改率」——而这正是"AI何时替代原系统"的核心决策指标。
    # baseline 与确认内容都用 json.dumps(sort_keys=True) 规范化，未改即字节相等。
    if original_json == new_json:
        return "none"
    ratio = difflib.SequenceMatcher(None, original_json, new_json).ratio()
    if ratio >= 0.85:
        return "minor"
    return "major"


def create_ai_drafts_router(db_path):
    router = APIRouter()
    db_path = Path(db_path)

    @router.get("/api/ai-drafts")
    def list_ai_drafts():
        require_perm("medical_record.view")   # 越权:草稿含病历正文+患者姓名,无权不可读
        with connect(db_path) as conn:
            draft_rows = conn.execute(
                """
                select
                    mr.record_id, mr.patient_identity, p.display_name,
                    mr.visit_time, mr.doctor_name, mr.ai_meta_json,
                    mr.content_json, mr.tooth_json, mr.record_type, mr.created_at
                from medical_records mr
                left join patients p on p.patient_identity = mr.patient_identity
                where mr.draft_status = 'ai_draft'
                order by mr.created_at desc
                """
            ).fetchall()
            unclaimed_rows = conn.execute(
                """
                select
                    unclaimed_id, code_name, visit_time, room, doctor_name,
                    content_json
                from ai_unclaimed_drafts
                where status = 'pending'
                order by created_at desc
                """
            ).fetchall()
            stats_row = conn.execute(
                """
                select
                    count(*) as generated,
                    sum(case when draft_status = '' then 1 else 0 end) as confirmed,
                    sum(case when json_extract(ai_meta_json, '$.edit_tier') = 'none'
                             then 1 else 0 end) as no_edit,
                    sum(case when draft_status = 'discarded' then 1 else 0 end) as discarded,
                    sum(case when draft_status = 'ai_draft' then 1 else 0 end) as pending
                from medical_records
                where origin = 'ai_voice'
                  and created_at >= date('now', 'localtime', '-7 days')
                """
            ).fetchone()

        drafts = [
            {
                "record_id": row["record_id"],
                "patient_identity": row["patient_identity"],
                "display_name": row["display_name"],
                "visit_time": row["visit_time"],
                "doctor_name": row["doctor_name"],
                "room": safe_json_object(row["ai_meta_json"]).get("room", ""),
                "content_json": safe_json_object(row["content_json"]),
                "tooth_json": row["tooth_json"] or "{}",
                "record_type": row["record_type"] or "",
                "created_at": row["created_at"],
            }
            for row in draft_rows
        ]
        unclaimed = [
            {
                "unclaimed_id": row["unclaimed_id"],
                "code_name": row["code_name"],
                "visit_time": row["visit_time"],
                "room": row["room"],
                "doctor_name": row["doctor_name"],
                "content_json": safe_json_object(row["content_json"]),
            }
            for row in unclaimed_rows
        ]
        stats = {
            "generated": stats_row["generated"] or 0,
            "confirmed": stats_row["confirmed"] or 0,
            "no_edit": stats_row["no_edit"] or 0,
            "discarded": stats_row["discarded"] or 0,
            "pending": stats_row["pending"] or 0,
        }
        return {"drafts": drafts, "unclaimed": unclaimed, "stats": stats}

    @router.post("/api/ai-drafts/{record_id}/confirm")
    def confirm_ai_draft(record_id: str, payload: dict):
        require_perm("medical_record.edit")   # 越权:确认会覆盖病历正文,写操作须 medical_record.edit
        editable_text_fields = ["record_type", "visit_time", "doctor_name"]
        updates = {
            field: str(payload.get(field) or "").strip()
            for field in editable_text_fields
            if field in payload
        }
        if "tooth_json" in payload:
            updates["tooth_json"] = json_payload_value(payload.get("tooth_json"))
        if "content_json" in payload:
            updates["content_json"] = json_payload_value(payload.get("content_json"))

        now = now_str()

        with connect(db_path) as conn:
            begin_immediate(conn)   # 抢写锁，防双击/并发确认致丢更新覆盖病历+版本/审计重复
            record = conn.execute(
                "select * from medical_records where record_id = ?",
                (record_id,),
            ).fetchone()
            if not record or record["draft_status"] != "ai_draft":
                raise HTTPException(status_code=404, detail="ai draft not found")

            ai_meta = safe_json_object(record["ai_meta_json"])
            # 摄入器当时未存 original_content_json，则以当前库里的 content 作基准
            baseline = ai_meta.get("original_content_json") or record["content_json"]
            new_content = updates.get("content_json", record["content_json"])
            edit_tier = _edit_tier(baseline, new_content)
            # 无改率是核心决策指标："无改"必须是全字段无改。
            # 内容字节相等但牙位/类型/时间/医生有真实变更的，至少计 minor。
            if edit_tier == "none":
                others_changed = any(
                    updates[field] != str(record[field] or "")
                    for field in ("record_type", "visit_time", "doctor_name")
                    if field in updates
                )
                if not others_changed and "tooth_json" in updates:
                    # 牙位按解析后的对象比较，避免键序/空白差异误判
                    others_changed = (
                        safe_json_object(updates["tooth_json"])
                        != safe_json_object(record["tooth_json"])
                    )
                if others_changed:
                    edit_tier = "minor"

            old_snapshot = medical_record_snapshot(record)
            old_json = stable_json(old_snapshot)
            old_hash = record["current_hash"] or stable_hash(old_snapshot)
            new_snapshot = dict(old_snapshot)
            new_snapshot.update(updates)
            new_snapshot["updated_at"] = now
            new_snapshot["edit_source"] = "ai_draft_confirm"
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

            ai_meta.update({"edit_tier": edit_tier, "confirmed_at": now})
            conn.execute(
                """
                update medical_records
                set record_type = ?,
                    visit_time = ?,
                    doctor_name = ?,
                    tooth_json = ?,
                    content_json = ?,
                    draft_status = '',
                    ai_meta_json = ?,
                    updated_at = ?,
                    current_hash = ?
                where record_id = ?
                """,
                (
                    updates.get("record_type", record["record_type"]),
                    updates.get("visit_time", record["visit_time"]),
                    updates.get("doctor_name", record["doctor_name"]),
                    updates.get("tooth_json", record["tooth_json"]),
                    new_content,
                    json.dumps(ai_meta, ensure_ascii=False, sort_keys=True),
                    now,
                    new_hash,
                    record_id,
                ),
            )
            audit_write(
                conn,
                "medical_record",
                record_id,
                "confirm_ai_draft",
                old_json=old_json,
                new_json=stable_json(new_snapshot),
                created_at=now,
            )
            conn.commit()

        return {"record_id": record_id, "edit_tier": edit_tier}

    @router.post("/api/ai-drafts/{record_id}/discard")
    def discard_ai_draft(record_id: str):
        require_perm("medical_record.edit")   # 越权:废弃他人病历草稿是写操作
        now = now_str()
        with connect(db_path) as conn:
            begin_immediate(conn)   # 抢写锁，防 confirm 与 discard 并发互相覆盖(确认成功又被废弃)
            record = conn.execute(
                "select draft_status from medical_records where record_id = ?",
                (record_id,),
            ).fetchone()
            if not record or record["draft_status"] != "ai_draft":
                raise HTTPException(status_code=404, detail="ai draft not found")
            conn.execute(
                "update medical_records set draft_status = 'discarded', updated_at = ? where record_id = ?",
                (now, record_id),
            )
            audit_write(
                conn,
                "medical_record",
                record_id,
                "discard_ai_draft",
                created_at=now,
            )
            conn.commit()
        return {"record_id": record_id, "draft_status": "discarded"}

    @router.post("/api/ai-unclaimed/{unclaimed_id}/claim")
    def claim_ai_unclaimed(unclaimed_id: str, payload: dict):
        require_perm("medical_record.edit")   # 越权:把无主草稿挂到患者=写病历
        patient_identity = str(payload.get("patient_identity", "")).strip()
        now = now_str()
        with connect(db_path) as conn:
            patient = conn.execute(
                "select 1 from patients where patient_identity = ?",
                (patient_identity,),
            ).fetchone()
            if not patient:
                raise HTTPException(status_code=404, detail="patient not found")

            unclaimed = conn.execute(
                "select * from ai_unclaimed_drafts where unclaimed_id = ?",
                (unclaimed_id,),
            ).fetchone()
            if not unclaimed or unclaimed["status"] != "pending":
                raise HTTPException(status_code=404, detail="unclaimed draft not found")

            # 原子抢占：先带 status='pending' 条件置 claimed，抢不到说明
            # 已被并发请求/双击认领，避免同一草稿生成两条病历挂到不同患者
            cur = conn.execute(
                "update ai_unclaimed_drafts set status = 'claimed' "
                "where unclaimed_id = ? and status = 'pending'",
                (unclaimed_id,),
            )
            if cur.rowcount == 0:
                raise HTTPException(status_code=404, detail="unclaimed draft not found")

            record_id = new_id("local-ai")
            # 继承摄入时的元数据（含收费明细 extras），与直配路径契约一致
            ai_meta = safe_json_object(unclaimed["ai_meta_json"])
            ai_meta.update({
                "code_name": unclaimed["code_name"],
                "room": unclaimed["room"],
                "source_file": unclaimed["source_file"],
                "claimed_from": unclaimed_id,
            })
            current_hash = stable_hash(
                {
                    "record_id": record_id,
                    "patient_identity": patient_identity,
                    "visit_time": unclaimed["visit_time"],
                    "doctor_name": unclaimed["doctor_name"],
                    "content_json": unclaimed["content_json"],
                    "tooth_json": unclaimed["tooth_json"],
                    "source_file": unclaimed["source_file"],
                }
            )
            conn.execute(
                """
                insert into medical_records(
                    record_id, patient_identity, record_type, visit_time, doctor_name,
                    tooth_json, content_json, draft_status, origin, ai_meta_json,
                    created_at, updated_at, current_hash
                )
                values (?, ?, '', ?, ?, ?, ?, 'ai_draft', 'ai_voice', ?, ?, ?, ?)
                """,
                (
                    record_id,
                    patient_identity,
                    unclaimed["visit_time"],
                    unclaimed["doctor_name"],
                    unclaimed["tooth_json"],
                    unclaimed["content_json"],
                    json.dumps(ai_meta, ensure_ascii=False, sort_keys=True),
                    now,
                    now,
                    current_hash,
                ),
            )
            audit_write(
                conn,
                "ai_unclaimed_draft",
                unclaimed_id,
                "claim_ai_unclaimed",
                new_json=stable_json({"record_id": record_id, "patient_identity": patient_identity}),
                created_at=now,
            )
            conn.commit()
        return {"record_id": record_id, "patient_identity": patient_identity}

    @router.post("/api/ai-unclaimed/{unclaimed_id}/discard")
    def discard_ai_unclaimed(unclaimed_id: str):
        require_perm("medical_record.edit")   # 越权:废弃待认领草稿是写操作
        now = now_str()
        with connect(db_path) as conn:
            unclaimed = conn.execute(
                "select status from ai_unclaimed_drafts where unclaimed_id = ?",
                (unclaimed_id,),
            ).fetchone()
            if not unclaimed:
                raise HTTPException(status_code=404, detail="unclaimed draft not found")
            # 与 claim 对齐：pending 是唯一可废弃的状态。已认领的草稿已生成病历，
            # 改写成 discarded 会丢失"已生成病历"的状态事实。
            if unclaimed["status"] != "pending":
                raise HTTPException(status_code=409, detail="该草稿已被认领或已废弃")
            conn.execute(
                "update ai_unclaimed_drafts set status = 'discarded' where unclaimed_id = ?",
                (unclaimed_id,),
            )
            audit_write(
                conn,
                "ai_unclaimed_draft",
                unclaimed_id,
                "discard_ai_unclaimed",
                created_at=now,
            )
            conn.commit()
        return {"unclaimed_id": unclaimed_id, "status": "discarded"}

    return router
