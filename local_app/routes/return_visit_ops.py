import csv
import hashlib
import json
import tempfile
import uuid
from contextlib import closing
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Body, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from starlette.background import BackgroundTask

from local_app.auth import audit_write, require_perm
from local_app.access_authorization import access_authorizer_active, require_access
from local_app.return_visit_transactions import (
    batch_delete_return_visits,
    create_return_visit as atomic_create_return_visit,
    delete_return_visit as atomic_delete_return_visit,
    request_write_actor,
    increment_attachment_parent,
    insert_attachment_return_visit_version,
    lock_attachment_parent,
    write_attachment_audit,
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
from local_app.rv_query import build_month_calendar, build_rv_filter, csv_safe
from local_app.timeutil import now_str

IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".webp")
IMAGE_MAX_BYTES = 8 * 1024 * 1024              # 截图上限 8MB（对齐 patient_assets/communications）
CSV_BATCH_SIZE = 500
CSV_SPOOL_MAX_BYTES = 1024 * 1024
CSV_STREAM_CHUNK_CHARS = 64 * 1024
CSV_HEADERS = (
    "回访时间", "患者", "电话", "回访事项", "回访内容", "回访方式", "回访类型",
    "回访结果", "主治医生", "回访医生", "回访人", "状态", "实际回访日期", "revision",
)
CSV_FIELDS = (
    "due_time", "display_name", "phone", "item_name", "note", "channel", "return_type",
    "return_result", "responsible_doctor", "return_doctor", "visitor", "status",
    "actual_date", "revision",
)


class ClosingStreamingResponse(StreamingResponse):
    """无论正常结束、客户端断开或发送失败，都在响应外层关闭资源。"""

    def __init__(self, *args, close_resource, **kwargs):
        self._close_resource = close_resource
        super().__init__(*args, **kwargs)

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            self._close_resource()


def write_return_visit_csv(cursor, output):
    """在调用线程逐批写 CSV，不物化完整结果集。"""
    output.write("\ufeff")
    writer = csv.writer(output)
    writer.writerow(CSV_HEADERS)
    while True:
        rows = cursor.fetchmany(CSV_BATCH_SIZE)
        if not rows:
            break
        for row in rows:
            writer.writerow([csv_safe(row[field]) for field in CSV_FIELDS])


async def stream_return_visit_csv(export_file):
    """从已填充的临时文件分块响应；结束或取消时立即关闭。"""
    try:
        while True:
            chunk = export_file.read(CSV_STREAM_CHUNK_CHARS)
            if not chunk:
                break
            yield chunk
    finally:
        export_file.close()


def create_return_visit_ops_router(db_path, images_dir):
    """回访工作流：本地新建/软删/月历 + 回访详情与微信截图。现有 GET 列表与 PUT 编辑在 api.py。"""
    router = APIRouter()
    db_path = Path(db_path)
    images_dir = Path(images_dir)

    def require_rv(conn, return_visit_id):
        row = conn.execute(
            "select r.return_visit_id, r.patient_identity from return_visits r "
            "join patients p on p.patient_identity=r.patient_identity "
            "and coalesce(p.is_deleted, 0)=0 "
            "where r.return_visit_id = ? and coalesce(r.is_deleted, 0) = 0",
            (return_visit_id,),
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="回访不存在")
        return row

    def _rv_images(conn, return_visit_id):
        hidden = hidden_attachment_ids(
            conn, parent_type="return_visit", parent_id=return_visit_id
        )
        rows = conn.execute(
            "select image_id, file_name from return_visit_images "
            "where return_visit_id = ? order by created_at",
            (return_visit_id,),
        ).fetchall()
        return [
            {"image_id": r["image_id"], "file_name": r["file_name"]}
            for r in rows
            if r["image_id"] not in hidden
        ]

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
        raise HTTPException(status_code=500, detail="attachment_operation_failed") from None

    def _unlink_all(rels, return_visit_id, patient_identity):
        """删盘：返回 (成功, 失败)；失败落 delete_return_visit_image_failed 审计(带 patient_identity #719)，不静默。"""
        images_root = images_dir.resolve()
        removed = failed = 0
        for rel in rels:
            if not rel:
                continue
            fp = (images_dir / rel).resolve()
            if not (fp.is_relative_to(images_root) and fp.is_file()):
                continue
            try:
                fp.unlink()
                removed += 1
            except OSError:
                failed += 1
                with connect(db_path) as conn:
                    audit_write(
                        conn, "return_visit", return_visit_id, "delete_return_visit_image_failed",
                        old_json=json.dumps({"rel_path": rel, "patient_identity": patient_identity},
                                            ensure_ascii=False),
                    )
                    conn.commit()
        return removed, failed

    @router.post("/api/patients/{patient_identity}/return-visits")
    def create_return_visit(
        patient_identity: str, payload: Any = Body(default=None)
    ):
        require_perm("patient.edit")
        actor = request_write_actor()
        return atomic_create_return_visit(
            db_path, patient_identity, payload, actor
        )

    @router.delete("/api/return-visits/{return_visit_id}")
    def soft_delete_return_visit(
        return_visit_id: str, payload: Any = Body(default=None)
    ):
        require_perm("patient.edit")
        actor = request_write_actor()
        return atomic_delete_return_visit(
            db_path, return_visit_id, payload, actor
        )

    @router.post("/api/return-visits/batch-delete")
    def batch_delete_return_visit(payload: Any = Body(default=None)):
        require_perm("patient.edit")
        actor = request_write_actor()
        return batch_delete_return_visits(db_path, payload, actor)

    @router.get("/api/return-visits/calendar")
    def return_visit_calendar(
        month: str = "", q: str = "", status: str = "", doctor: str = "",
        visitor: str = "", channel: str = "",
    ):
        require_access("return_visit.view")
        with connect(db_path) as conn:
            return build_month_calendar(
                conn, month, q=q, status=status, doctor=doctor,
                visitor=visitor, channel=channel,
            )

    @router.get("/api/return-visits/export")
    def return_visit_export(
        date: str = "", date_from: str = "", date_to: str = "", status: str = "",
        doctor: str = "", visitor: str = "", channel: str = "", rtype: str = "",
        att: str = "", q: str = "", item: str = "", patient_q: str = "",
        content_q: str = "", patient: str = "",
    ):
        """按列表同一筛选导出全部匹配行；Access V3 路由策略负责 ALL_OF 授权。"""
        att = att.strip()
        if att == "recording":
            require_access("call_record.view")
        elif att == "image":
            require_access("patient.image.view")
        where_sql, params = build_rv_filter(
            date=date, date_from=date_from, date_to=date_to, status=status,
            doctor=doctor, visitor=visitor, channel=channel, rtype=rtype,
            att=att, q=q, item=item, patient_q=patient_q,
            content_q=content_q, patient=patient,
        )
        export_file = tempfile.SpooledTemporaryFile(
            max_size=CSV_SPOOL_MAX_BYTES, mode="w+", encoding="utf-8", newline="",
        )
        try:
            conn = connect(db_path)
            try:
                cursor = conn.execute(
                    f"""
                    select r.due_time, p.display_name, p.phone, r.item_name, r.note,
                           r.channel, r.return_type, r.return_result,
                           p.responsible_doctor, r.return_doctor, r.visitor,
                           r.status, r.actual_date, r.revision
                    from return_visits r
                    join patients p on p.patient_identity = r.patient_identity
                    {where_sql}
                    order by coalesce(r.due_time, '') desc, r.return_visit_id desc
                    """,
                    tuple(params),
                )
                write_return_visit_csv(cursor, export_file)
            finally:
                conn.close()
            export_file.flush()
            export_file.seek(0)
        except BaseException:
            export_file.close()
            raise
        return ClosingStreamingResponse(
            stream_return_visit_csv(export_file), media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": 'attachment; filename="return_visits.csv"'},
            background=BackgroundTask(export_file.close),
            close_resource=export_file.close,
        )

    @router.get("/api/return-visits/{return_visit_id}")
    def return_visit_detail(return_visit_id: str):
        """回访卡片详情：回访字段 + 微信截图列表（录音由前端按 linked_return_visit_id 从 patient calls 归组）。"""
        require_access("return_visit.view")
        with connect(db_path) as conn:
            r = conn.execute(
                "select r.return_visit_id, r.patient_identity, p.display_name, p.phone, "
                "p.responsible_doctor, "
                "r.due_time, r.item_name, r.status, r.note, r.return_doctor, r.visitor, "
                "r.channel, r.return_type, r.return_result, r.actual_date, r.intent_level, "
                "r.satisfaction, r.revision "
                "from return_visits r join patients p on p.patient_identity = r.patient_identity "
                "and coalesce(p.is_deleted, 0) = 0 "
                "where r.return_visit_id = ? and coalesce(r.is_deleted, 0) = 0",
                (return_visit_id,),
            ).fetchone()
            if not r:
                raise HTTPException(status_code=404, detail="回访不存在")
            out = {k: r[k] for k in r.keys()}
            out["images"] = _rv_images(conn, return_visit_id)
        return out

    @router.post("/api/return-visits/{return_visit_id}/images")
    async def upload_return_visit_image(
        return_visit_id: str,
        file: UploadFile = File(...),
        expected_revision: str = Form(""),
    ):
        if access_authorizer_active():
            require_access("return_visit.manage")
        else:
            require_perm("patient.edit")
        actor = request_write_actor()
        expected_revision_value = _form_revision(expected_revision)
        ext = (Path(file.filename or "").suffix or "").lower()
        if ext not in IMAGE_EXTS:
            raise HTTPException(status_code=400, detail="只支持 jpg/png/webp 截图")
        data = await file.read()
        if not data:
            raise HTTPException(status_code=400, detail="空文件")
        if len(data) > IMAGE_MAX_BYTES:
            raise HTTPException(status_code=400, detail="截图不要超过 8MB")
        if not attachment_content_matches(data, ext):
            raise HTTPException(status_code=400, detail="截图文件类型不匹配")
        now = now_str()
        image_id = "local-rvimg-" + uuid.uuid4().hex[:16]
        safe = hashlib.sha1(return_visit_id.encode("utf-8")).hexdigest()[:20]
        fname = image_id + ext
        rel = str(Path("return-visit-images") / safe / now[:10] / fname)
        paths = {
            "temp_rel_path": str(Path(".attachment-staging") / (uuid.uuid4().hex + ".tmp")),
            "final_rel_path": rel,
            "tombstone_rel_path": "",
        }
        content_hash = hashlib.sha256(data).hexdigest()
        operation_id = None
        try:
            with closing(connect(db_path)) as conn:
                begin_immediate(conn)
                _, parent = lock_attachment_parent(
                    conn, actor=actor, permissions=("return_visit.manage",),
                    parent_type="return_visit", parent_id=return_visit_id,
                    expected_revision=expected_revision_value,
                )
                operation_id = stage_publish(
                    conn, parent_type="return_visit", parent_id=return_visit_id,
                    attachment_id=image_id, expected_revision=expected_revision_value,
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
                    conn, actor=actor, permissions=("return_visit.manage",),
                    parent_type="return_visit", parent_id=return_visit_id,
                    expected_revision=expected_revision_value,
                    allow_operation_id=operation_id,
                )
                mark_publish_pending(conn, operation_id=operation_id)
                conn.commit()
            publish_staged_file(db_path, images_dir, operation_id)
            holder = {}
            with closing(connect(db_path)) as conn:
                begin_immediate(conn)
                locked_actor, parent = lock_attachment_parent(
                    conn, actor=actor, permissions=("return_visit.manage",),
                    parent_type="return_visit", parent_id=return_visit_id,
                    expected_revision=expected_revision_value,
                    allow_operation_id=operation_id,
                )

                def insert_business():
                    conn.execute(
                        "insert into return_visit_images(image_id, return_visit_id, "
                        "patient_identity, rel_path, file_name, size_bytes, content_hash, "
                        "created_at) values (?, ?, ?, ?, ?, ?, ?, ?)",
                        (image_id, return_visit_id, parent["patient_identity"], rel,
                         fname, len(data), content_hash, now),
                    )
                    holder["parent"] = increment_attachment_parent(
                        conn, parent_type="return_visit", parent_id=return_visit_id,
                        now=now,
                    )

                commit_publish(
                    conn, operation_id=operation_id,
                    insert_business_row=insert_business,
                    write_version=lambda: insert_attachment_return_visit_version(
                        conn, holder["parent"]
                    ),
                    write_audit=lambda: write_attachment_audit(
                        conn, locked_actor=locked_actor, entity_type="return_visit",
                        entity_id=return_visit_id, action="upload_return_visit_image",
                        attachment_id=image_id,
                        revision=holder["parent"]["revision"], created_at=now,
                    ),
                )
                conn.commit()
            recover_attachment_file_operation(db_path, images_dir, operation_id)
            return {"ok": True, "image_id": image_id,
                    "revision": holder["parent"]["revision"]}
        except BaseException as error:
            _recover_failure(operation_id, error)

    @router.get("/api/return-visits/{return_visit_id}/images/{image_id}/file")
    def return_visit_image_file(return_visit_id: str, image_id: str):
        require_perm("patient.view")
        with connect(db_path) as conn:
            require_rv(conn, return_visit_id)   # 回访软删/不存在 → 404,不外泄其截图(与 detail/upload 口径一致)
            if image_id in hidden_attachment_ids(
                conn,
                parent_type="return_visit",
                parent_id=return_visit_id,
            ):
                raise HTTPException(status_code=404, detail="image not found")
            row = conn.execute(
                "select rel_path from return_visit_images where image_id = ? and return_visit_id = ?",
                (image_id, return_visit_id),
            ).fetchone()
        if not row or not row["rel_path"]:
            raise HTTPException(status_code=404, detail="image not found")
        images_root = images_dir.resolve()
        fp = (images_dir / row["rel_path"]).resolve()
        if not fp.is_relative_to(images_root) or not fp.is_file():
            raise HTTPException(status_code=404, detail="image not found")
        return FileResponse(fp)

    @router.delete("/api/return-visits/{return_visit_id}/images/{image_id}")
    def delete_return_visit_image(
        return_visit_id: str,
        image_id: str,
        payload: Any = Body(default=None),
    ):
        if access_authorizer_active():
            require_access("return_visit.manage")
        else:
            require_perm("patient.edit")
        actor = request_write_actor()
        if type(payload) is not dict or set(payload) != {"expected_revision"}:
            raise HTTPException(status_code=400, detail="invalid_request_body")
        expected_revision = payload["expected_revision"]
        now = now_str()
        operation_id = None
        try:
            with closing(connect(db_path)) as conn:
                begin_immediate(conn)
                lock_attachment_parent(
                    conn, actor=actor, permissions=("return_visit.manage",),
                    parent_type="return_visit", parent_id=return_visit_id,
                    expected_revision=expected_revision,
                )
                row = conn.execute(
                    "select rel_path from return_visit_images "
                    "where image_id=? and return_visit_id=?",
                    (image_id, return_visit_id),
                ).fetchone()
                if row is None:
                    raise HTTPException(status_code=404, detail="image not found")
                paths = {
                    "temp_rel_path": "",
                    "final_rel_path": row["rel_path"],
                    "tombstone_rel_path": str(
                        Path(".attachment-tombstones")
                        / (uuid.uuid4().hex + ".deleted")
                    ),
                }
                operation_id = stage_delete(
                    conn, parent_type="return_visit", parent_id=return_visit_id,
                    attachment_id=image_id, expected_revision=expected_revision,
                    paths=paths, images_dir=images_dir,
                )
                conn.commit()
            tombstone_staged_file(db_path, images_dir, operation_id)
            holder = {}
            with closing(connect(db_path)) as conn:
                begin_immediate(conn)
                locked_actor, _ = lock_attachment_parent(
                    conn, actor=actor, permissions=("return_visit.manage",),
                    parent_type="return_visit", parent_id=return_visit_id,
                    expected_revision=expected_revision,
                    allow_operation_id=operation_id,
                )

                def delete_business():
                    conn.execute(
                        "delete from return_visit_images where image_id=? "
                        "and return_visit_id=?",
                        (image_id, return_visit_id),
                    )
                    holder["parent"] = increment_attachment_parent(
                        conn, parent_type="return_visit", parent_id=return_visit_id,
                        now=now,
                    )

                commit_delete(
                    conn, operation_id=operation_id,
                    delete_business_row=delete_business,
                    write_version=lambda: insert_attachment_return_visit_version(
                        conn, holder["parent"]
                    ),
                    write_audit=lambda: write_attachment_audit(
                        conn, locked_actor=locked_actor, entity_type="return_visit",
                        entity_id=return_visit_id, action="delete_return_visit_image",
                        attachment_id=image_id,
                        revision=holder["parent"]["revision"], created_at=now,
                    ),
                )
                conn.commit()
            recover_attachment_file_operation(db_path, images_dir, operation_id)
            return {"ok": True, "image_id": image_id,
                    "revision": holder["parent"]["revision"],
                    "file_removed": True}
        except BaseException as error:
            _recover_failure(operation_id, error)

    return router
