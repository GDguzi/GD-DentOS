"""通话录音模块（Phase 1：纯本地录音上传 / 回放 / 挂病历）。

独立表 calls，录音文件复用 patient_assets 的落盘范式（表里只存相对路径 + 元数据）。
Phase 1 只做「已知患者的手动上传/回访手机录音 + 回放」；按号码自动匹配患者、点击拨号、
总机对接属 Phase 2/3（telephony 层），本文件不含。

时区铁律：一律北京时间（ZoneInfo("Asia/Shanghai")），不用裸 datetime.now()。
"""

import hashlib
import re
import uuid
from contextlib import closing
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Body, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse

from local_app.auth import require_perm
from local_app.access_authorization import require_access
from local_app.call_transactions import (
    delete_call as atomic_delete_call,
    increment_attachment_parent,
    insert_attachment_return_visit_version,
    insert_uploaded_call,
    lock_attachment_parent,
    request_write_actor,
    update_call as atomic_update_call,
    write_attachment_audit,
    write_attachment_delete_audit,
)
from local_app.attachment_file_ops import (
    AttachmentConsistencyError,
    attachment_content_matches,
    commit_delete,
    commit_publish,
    hidden_attachment_ids,
    mark_publish_pending,
    publish_staged_file,
    recover_attachment_file_operation,
    stage_delete,
    stage_publish,
    tombstone_staged_file,
    write_staged_bytes,
)
from local_app.db import begin_immediate, connect
from local_app.routes.patient_common import require_patient   # 审计R2:软删患者按不存在处理(共享校验)
from local_app.timeutil import now_str

AUDIO_EXTS = (".wav", ".mp3", ".ogg", ".m4a", ".webm", ".amr")
AUDIO_MAX_BYTES = 100 * 1024 * 1024          # 录音上限 100MB
DIRECTIONS = ("inbound", "outbound")          # 呼入 / 呼出
UPLOAD_SOURCES = ("manual_import", "return_visit_mobile")   # Phase1 允许的来源


def normalize_phone(raw):
    """号码归一：只留数字，去 +86/86 国码与前导 0，用于匹配/展示。空进空出。"""
    digits = re.sub(r"\D", "", str(raw or ""))
    if digits.startswith("86") and len(digits) > 11:
        digits = digits[2:]
    digits = digits.lstrip("0")
    return digits


def create_calls_router(db_path, images_dir):
    router = APIRouter()
    db_path = Path(db_path)
    images_dir = Path(images_dir)

    def _form_revision(value):
        if type(value) is not str or not value.isdigit() or int(value) < 1:
            raise HTTPException(status_code=400, detail="invalid_expected_revision")
        return int(value)

    def _recover_failure(operation_id, error):
        if operation_id:
            try:
                recover_attachment_file_operation(db_path, images_dir, operation_id)
            except Exception:
                raise HTTPException(
                    status_code=500, detail="attachment_consistency_error"
                ) from None
        if isinstance(error, HTTPException):
            raise error
        if isinstance(error, FileExistsError):
            raise HTTPException(
                status_code=409, detail="attachment_target_conflict"
            ) from None
        raise HTTPException(status_code=500, detail="call_write_failed") from None

    def _call_row(r):
        return {
            "call_id": r["call_id"],
            "direction": r["direction"],
            "source": r["source"],
            "peer_norm": r["peer_norm"],
            "region": r["region"],
            "match_status": r["match_status"],
            "operator": r["operator"],
            "started_at": r["started_at"],
            "duration_sec": r["duration_sec"],
            "deal_status": r["deal_status"],
            "file_name": r["file_name"],
            "size_bytes": r["size_bytes"],
            "linked_communication_id": r["linked_communication_id"],
            "linked_return_visit_id": r["linked_return_visit_id"],   # 回访卡片按此归组电话回访录音
            "linked_consult_id": r["linked_consult_id"],
            "note": r["note"],
            "revision": r["revision"],
        }

    @router.get("/api/patients/{patient_identity}/calls")
    def list_patient_calls(patient_identity: str):
        require_perm("patient.view"); require_perm("call.view")
        with connect(db_path) as conn:
            require_patient(conn, patient_identity)
            rows = conn.execute(
                """
                select call_id, direction, source, peer_norm, region, match_status,
                       operator, started_at, duration_sec, deal_status,
                       file_name, size_bytes, linked_communication_id,
                       linked_return_visit_id, linked_consult_id, note, revision
                from calls
                where patient_identity = ?
                order by coalesce(started_at, '') desc, created_at desc
                """,
                (patient_identity,),
            ).fetchall()
            calls = [
                _call_row(row)
                for row in rows
                if row["call_id"] not in hidden_attachment_ids(
                    conn, parent_type="call", parent_id=row["call_id"]
                )
            ]
        return {"calls": calls, "totalcount": len(calls)}

    @router.post("/api/patients/{patient_identity}/calls")
    async def upload_patient_call(
        patient_identity: str,
        file: UploadFile = File(...),
        direction: str = Form("outbound"),
        peer_number: str = Form(""),
        started_at: str = Form(""),
        duration_sec: int = Form(0),
        deal_status: str = Form(""),
        source: str = Form("manual_import"),
        linked_consult_id: str = Form(""),
        linked_return_visit_id: str = Form(""),
        linked_communication_id: str = Form(""),
        note: str = Form(""),
        expected_revision: str = Form(""),
    ):
        require_access("call_record.manage")
        actor = request_write_actor()
        expected_revision_value = _form_revision(expected_revision)
        if direction not in DIRECTIONS:
            raise HTTPException(status_code=400, detail="direction 只能是 inbound/outbound")
        if source not in UPLOAD_SOURCES:
            raise HTTPException(status_code=400, detail="非法来源")
        ext = (Path(file.filename or "").suffix or "").lower()
        if ext not in AUDIO_EXTS:
            raise HTTPException(status_code=400, detail="只支持 wav/mp3/ogg/m4a/webm/amr 录音")
        data = await file.read()
        if not data:
            raise HTTPException(status_code=400, detail="空文件")
        if len(data) > AUDIO_MAX_BYTES:
            raise HTTPException(status_code=400, detail="录音不要超过 100MB")
        if not attachment_content_matches(data, ext):
            raise HTTPException(status_code=400, detail="录音文件类型不匹配")
        linked_consult_id = linked_consult_id.strip()
        linked_return_visit_id = linked_return_visit_id.strip()
        linked_communication_id = linked_communication_id.strip()
        if linked_consult_id or bool(linked_return_visit_id) == bool(linked_communication_id):
            raise HTTPException(status_code=400, detail="call_context_required")
        context_type = (
            "communication" if linked_communication_id else "return_visit"
        )
        context_id = linked_communication_id or linked_return_visit_id

        peer_norm = normalize_phone(peer_number)
        caller_raw = str(peer_number or "").strip() if direction == "inbound" else ""
        callee_raw = str(peer_number or "").strip() if direction == "outbound" else ""
        started = started_at.strip() or now_str()
        try:
            dur = max(0, int(duration_sec))
        except (TypeError, ValueError):
            dur = 0

        now = now_str()
        call_id = "local-call-" + uuid.uuid4().hex[:16]
        # 落盘路径按「对方号(无号则患者)」分桶,与 patient_assets 同护栏
        bucket_key = peer_norm or patient_identity
        safe = hashlib.sha1(bucket_key.encode("utf-8")).hexdigest()[:20]
        fname = call_id + ext
        rel = str(Path("call-recordings") / safe / now[:10] / fname)
        paths = {
            "temp_rel_path": str(Path(".attachment-staging") / (uuid.uuid4().hex + ".tmp")),
            "final_rel_path": rel,
            "tombstone_rel_path": "",
        }
        content_hash = hashlib.sha256(data).hexdigest()
        values = {
            "call_id": call_id,
            "direction": direction,
            "source": source,
            "caller_raw": caller_raw,
            "callee_raw": callee_raw,
            "peer_norm": peer_norm,
            "patient_identity": patient_identity,
            "started_at": started,
            "duration_sec": dur,
            "deal_status": deal_status.strip(),
            "rel_path": rel,
            "file_name": fname,
            "size_bytes": len(data),
            "content_hash": content_hash,
            "linked_return_visit_id": linked_return_visit_id,
            "linked_communication_id": linked_communication_id,
            "note": note.strip(),
            "created_at": now,
        }
        operation_id = None
        try:
            with closing(connect(db_path)) as conn:
                begin_immediate(conn)
                lock_attachment_parent(
                    conn, actor=actor, permissions=("call_record.manage",),
                    parent_type=context_type, parent_id=context_id,
                    expected_revision=expected_revision_value,
                    patient_identity=patient_identity,
                    direction=direction if context_type == "communication" else None,
                )
                operation_id = stage_publish(
                    conn, parent_type="call_create", parent_id=context_id,
                    attachment_id=call_id, expected_revision=expected_revision_value,
                    paths=paths, images_dir=images_dir,
                )
                conn.commit()
            write_staged_bytes(
                db_path, images_dir, operation_id, data,
                expected_size=len(data), expected_hash=content_hash,
            )
            with closing(connect(db_path)) as conn:
                begin_immediate(conn)
                lock_attachment_parent(
                    conn, actor=actor, permissions=("call_record.manage",),
                    parent_type=context_type, parent_id=context_id,
                    expected_revision=expected_revision_value,
                    patient_identity=patient_identity,
                    direction=direction if context_type == "communication" else None,
                    allow_operation_id=operation_id,
                )
                mark_publish_pending(conn, operation_id=operation_id)
                conn.commit()
            publish_staged_file(db_path, images_dir, operation_id)
            holder = {}
            with closing(connect(db_path)) as conn:
                begin_immediate(conn)
                locked_actor, _ = lock_attachment_parent(
                    conn, actor=actor, permissions=("call_record.manage",),
                    parent_type=context_type, parent_id=context_id,
                    expected_revision=expected_revision_value,
                    patient_identity=patient_identity,
                    direction=direction if context_type == "communication" else None,
                    allow_operation_id=operation_id,
                )

                def insert_business():
                    holder["call"] = insert_uploaded_call(
                        conn, values=values, locked_actor=locked_actor
                    )
                    holder["context"] = increment_attachment_parent(
                        conn, parent_type=context_type, parent_id=context_id, now=now
                    )

                commit_publish(
                    conn, operation_id=operation_id,
                    insert_business_row=insert_business,
                    write_version=(
                        (lambda: insert_attachment_return_visit_version(
                            conn, holder["context"]
                        ))
                        if context_type == "return_visit"
                        else (lambda: None)
                    ),
                    write_audit=lambda: write_attachment_audit(
                        conn, locked_actor=locked_actor, entity_type="call",
                        entity_id=call_id, action="upload_patient_call",
                        attachment_id=call_id,
                        revision=holder["call"]["revision"], created_at=now,
                    ),
                )
                conn.commit()
            recover_attachment_file_operation(db_path, images_dir, operation_id)
            return {
                "ok": True,
                "call_id": call_id,
                "revision": holder["call"]["revision"],
                "context_revision": holder["context"]["revision"],
            }
        except BaseException as error:
            _recover_failure(operation_id, error)

    @router.get("/api/calls/{call_id}/recording")
    def call_recording(call_id: str):
        require_perm("patient.view"); require_perm("call.view")
        with connect(db_path) as conn:
            row = conn.execute(
                "select c.rel_path from calls c join patients p "
                "on p.patient_identity=c.patient_identity "
                "and coalesce(p.is_deleted, 0)=0 where c.call_id = ?",
                (call_id,),
            ).fetchone()
            if call_id in hidden_attachment_ids(
                conn, parent_type="call", parent_id=call_id
            ):
                row = None
        if not row or not row["rel_path"]:
            raise HTTPException(status_code=404, detail="recording not found")
        images_root = images_dir.resolve()
        file_path = (images_dir / row["rel_path"]).resolve()
        if not file_path.is_relative_to(images_root) or not file_path.is_file():
            raise HTTPException(status_code=404, detail="recording not found")
        return FileResponse(file_path)   # FileResponse 原生支持 HTTP Range,<audio> 可拖动进度

    @router.patch("/api/calls/{call_id}")
    def update_call(call_id: str, payload: Any = Body(default=None)):
        require_access("call_record.manage")
        actor = request_write_actor()
        return atomic_update_call(db_path, call_id, payload, actor)

    @router.delete("/api/calls/{call_id}")
    def delete_call(call_id: str, payload: Any = Body(default=None)):
        require_access("call_record.delete")
        actor = request_write_actor()
        with closing(connect(db_path)) as conn:
            existing = conn.execute(
                "select rel_path from calls where call_id=?", (call_id,)
            ).fetchone()
        if existing is not None and not existing["rel_path"]:
            return atomic_delete_call(db_path, call_id, payload, actor)
        expected_revision = (
            payload.get("expected_revision") if type(payload) is dict else None
        )
        now = now_str()
        operation_id = None
        try:
            with closing(connect(db_path)) as conn:
                begin_immediate(conn)
                _, current = lock_attachment_parent(
                    conn, actor=actor, permissions=("call_record.delete",),
                    parent_type="call", parent_id=call_id,
                    expected_revision=expected_revision,
                )
                if type(payload) is not dict or set(payload) != {"expected_revision"}:
                    raise HTTPException(status_code=400, detail="invalid_request_body")
                if not current["rel_path"]:
                    raise HTTPException(status_code=409, detail="recording_missing")
                paths = {
                    "temp_rel_path": "",
                    "final_rel_path": current["rel_path"],
                    "tombstone_rel_path": str(
                        Path(".attachment-tombstones")
                        / (uuid.uuid4().hex + ".deleted")
                    ),
                }
                operation_id = stage_delete(
                    conn, parent_type="call", parent_id=call_id,
                    attachment_id=call_id, expected_revision=expected_revision,
                    paths=paths, images_dir=images_dir,
                )
                conn.commit()
            tombstone_staged_file(db_path, images_dir, operation_id)
            holder = {}
            with closing(connect(db_path)) as conn:
                begin_immediate(conn)
                locked_actor, current = lock_attachment_parent(
                    conn, actor=actor, permissions=("call_record.delete",),
                    parent_type="call", parent_id=call_id,
                    expected_revision=expected_revision,
                    allow_operation_id=operation_id,
                )

                def delete_business():
                    changed = increment_attachment_parent(
                        conn, parent_type="call", parent_id=call_id, now=now
                    )
                    holder["revision"] = changed["revision"]
                    conn.execute("delete from calls where call_id=?", (call_id,))

                commit_delete(
                    conn, operation_id=operation_id,
                    delete_business_row=delete_business,
                    write_version=lambda: None,
                    write_audit=lambda: write_attachment_delete_audit(
                        conn,
                        locked_actor=locked_actor,
                        entity_type="call",
                        entity_id=call_id,
                        id_field="call_id",
                        old_row=current,
                        revision=holder["revision"],
                        created_at=now,
                    ),
                )
                conn.commit()
            recover_attachment_file_operation(db_path, images_dir, operation_id)
            return {"ok": True, "call_id": call_id,
                    "revision": holder["revision"], "file_removed": True}
        except BaseException as error:
            _recover_failure(operation_id, error)

    return router
