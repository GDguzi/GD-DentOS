"""沟通记录模块（客户沟通→沟通咨询）：电话咨询 / 微信沟通的联系记录 + 微信截图。

与面诊(consults)各是各的：面诊=看牙病历记录；沟通记录=客户联系。独立表 communications，
微信截图落 communication_images（仿 calls 落盘范式，表里只存相对路径 + 元数据，模块自持不混入影像库）。
电话咨询录音走 calls 表的 linked_communication_id 弱关联（前端按患者列 calls 后归组）。

时区铁律：一律北京时间（ZoneInfo("Asia/Shanghai")），不用裸 datetime.now()。
落盘/删除遵 入库失败清盘防孤儿，删盘失败可追踪不静默。
"""

import hashlib
import json
import uuid
from contextlib import closing
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Body, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse

from local_app.auth import audit_write, require_perm
from local_app.access_authorization import access_authorizer_active, require_access
from local_app.communication_transactions import (
    create_communication as atomic_create_communication,
    delete_communication as atomic_delete_communication,
    request_write_actor,
    increment_attachment_parent,
    lock_attachment_parent,
    write_attachment_audit,
    update_communication as atomic_update_communication,
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
from local_app.validation import valid_date_param

CHANNELS = ("phone", "wechat")                 # 电话 / 微信
IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".webp")
IMAGE_MAX_BYTES = 8 * 1024 * 1024              # 截图上限 8MB（对齐 patient_assets）


def create_communications_router(db_path, images_dir):
    router = APIRouter()
    db_path = Path(db_path)
    images_dir = Path(images_dir)

    def require_comm(conn, communication_id):
        row = conn.execute(
            "select communication_id, patient_identity from communications where communication_id = ?",
            (communication_id,),
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="communication not found")
        require_patient(conn, row["patient_identity"])
        return row

    def _images_of(conn, communication_id):
        hidden = hidden_attachment_ids(
            conn, parent_type="communication", parent_id=communication_id
        )
        rows = conn.execute(
            "select image_id, file_name from communication_images "
            "where communication_id = ? order by created_at",
            (communication_id,),
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

    @router.get("/api/patients/{patient_identity}/communications")
    def list_patient_communications(patient_identity: str):
        require_access("communication.view")
        with connect(db_path) as conn:
            require_patient(conn, patient_identity)
            rows = conn.execute(
                "select communication_id, channel, direction, reason, contacted_at, operator, content, "
                "deal_status, note, revision from communications where patient_identity = ? "
                "order by coalesce(contacted_at, '') desc, coalesce(created_at, '') desc, communication_id desc",
                (patient_identity,),
            ).fetchall()
            items = []
            for r in rows:
                items.append({
                    "communication_id": r["communication_id"],
                    "channel": r["channel"],
                    "direction": r["direction"],
                    "reason": r["reason"],
                    "contacted_at": r["contacted_at"],
                    "operator": r["operator"],
                    "content": r["content"],
                    "deal_status": r["deal_status"],
                    "note": r["note"],
                    "revision": r["revision"],
                    "images": _images_of(conn, r["communication_id"]),
                })
        return {"communications": items, "totalcount": len(items)}

    @router.get("/api/communications")
    def list_communications_ledger(
        date_from: str = "", date_to: str = "", q: str = "", channel: str = "",
        direction: str = "", reason: str = "", operator: str = "",
        pageno: int = 1, pagesize: int = 50,
    ):
        require_access("patient.profile.view")
        require_access("communication.view")
        date_from = date_from.strip()
        date_to = date_to.strip()
        for value, field in ((date_from, "date_from"), (date_to, "date_to")):
            if value:
                valid_date_param(value, field)
        if date_from and date_to and date_from > date_to:
            raise HTTPException(status_code=400, detail="date_from 不能晚于 date_to")
        parts = ["coalesce(p.is_deleted, 0) = 0"]
        params = []
        if date_from:
            parts.append("substr(coalesce(c.contacted_at, ''), 1, 10) >= ?")
            params.append(date_from)
        if date_to:
            parts.append("substr(coalesce(c.contacted_at, ''), 1, 10) <= ?")
            params.append(date_to)
        for value, column in (
            (channel.strip(), "channel"), (direction.strip(), "direction"),
            (reason.strip(), "reason"), (operator.strip(), "operator"),
        ):
            if value:
                parts.append(f"coalesce(c.{column}, '') = ?")
                params.append(value)
        q = q.strip()
        if q:
            like = f"%{q}%"
            parts.append(
                "(c.communication_id like ? or c.patient_identity like ? "
                "or coalesce(c.content, '') like ? or coalesce(c.reason, '') like ? "
                "or coalesce(c.operator, '') like ? or coalesce(p.display_name, '') like ?)"
            )
            params.extend([like] * 6)
        where_sql = "where " + " and ".join(parts)
        pageno = max(pageno, 1)
        pagesize = min(max(pagesize, 1), 200)
        with connect(db_path) as conn:
            total = conn.execute(
                f"select count(*) from communications c "
                f"join patients p on p.patient_identity = c.patient_identity {where_sql}",
                tuple(params),
            ).fetchone()[0]
            rows = conn.execute(
                f"""
                select c.communication_id, c.patient_identity, p.display_name,
                       c.channel, c.direction, c.reason, c.contacted_at, c.operator,
                       c.content, c.deal_status, c.note, c.revision
                from communications c
                join patients p on p.patient_identity = c.patient_identity
                {where_sql}
                order by coalesce(c.contacted_at, '') desc, c.communication_id desc
                limit ? offset ?
                """,
                tuple(params + [pagesize, (pageno - 1) * pagesize]),
            ).fetchall()
        return {
            "list": [{key: row[key] for key in row.keys()} for row in rows],
            "totalcount": total, "pageno": pageno, "pagesize": pagesize,
        }

    @router.post("/api/patients/{patient_identity}/communications")
    def create_communication(
        patient_identity: str, payload: Any = Body(default=None)
    ):
        require_access("communication.manage")
        actor = request_write_actor()
        return atomic_create_communication(
            db_path, patient_identity, payload, actor
        )

    @router.patch("/api/communications/{communication_id}")
    def update_communication(
        communication_id: str, payload: Any = Body(default=None)
    ):
        require_access("communication.manage")
        actor = request_write_actor()
        return atomic_update_communication(
            db_path, communication_id, payload, actor
        )

    @router.delete("/api/communications/{communication_id}")
    def delete_communication(
        communication_id: str, payload: Any = Body(default=None)
    ):
        require_access("communication.delete")
        actor = request_write_actor()
        result = atomic_delete_communication(
            db_path, communication_id, payload, actor
        )
        rels = result.pop("_rel_paths")
        pid = result.pop("_patient_identity")
        removed, failed = _unlink_all(rels, communication_id, pid)
        # 删盘失败明确返回 files_failed,前端据此告警(录音/截图属隐私,不静默当成功)
        return {
            **result,
            "files_removed": removed,
            "files_failed": failed,
        }

    @router.post("/api/communications/{communication_id}/images")
    async def upload_communication_image(
        communication_id: str,
        file: UploadFile = File(...),
        expected_revision: str = Form(""),
    ):
        if access_authorizer_active():
            require_access("communication.image.manage")
        else:
            require_perm("patient.view"); require_perm("communication.manage")
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
        image_id = "local-commimg-" + uuid.uuid4().hex[:16]
        safe = hashlib.sha1(communication_id.encode("utf-8")).hexdigest()[:20]
        sub = Path("communication-images") / safe / now[:10]
        fname = image_id + ext
        rel = str(sub / fname)
        temp_rel = str(Path(".attachment-staging") / (uuid.uuid4().hex + ".tmp"))
        paths = {
            "temp_rel_path": temp_rel,
            "final_rel_path": rel,
            "tombstone_rel_path": "",
        }
        content_hash = hashlib.sha256(data).hexdigest()
        operation_id = None
        try:
            with closing(connect(db_path)) as conn:
                begin_immediate(conn)
                _, parent = lock_attachment_parent(
                    conn,
                    actor=actor,
                    permissions=("communication.image.manage",),
                    parent_type="communication",
                    parent_id=communication_id,
                    expected_revision=expected_revision_value,
                )
                patient_identity = parent["patient_identity"]
                operation_id = stage_publish(
                    conn,
                    parent_type="communication",
                    parent_id=communication_id,
                    attachment_id=image_id,
                    expected_revision=expected_revision_value,
                    paths=paths,
                    images_dir=images_dir,
                )
                conn.commit()
            write_staged_bytes(
                db_path,
                images_dir,
                operation_id,
                data,
                expected_size=len(data),
                expected_hash=content_hash,
            )
            with closing(connect(db_path)) as conn:
                begin_immediate(conn)
                lock_attachment_parent(
                    conn,
                    actor=actor,
                    permissions=("communication.image.manage",),
                    parent_type="communication",
                    parent_id=communication_id,
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
                    conn,
                    actor=actor,
                    permissions=("communication.image.manage",),
                    parent_type="communication",
                    parent_id=communication_id,
                    expected_revision=expected_revision_value,
                    allow_operation_id=operation_id,
                )

                def insert_business():
                    conn.execute(
                        "insert into communication_images(image_id, communication_id, "
                        "patient_identity, rel_path, file_name, size_bytes, content_hash, "
                        "created_at) values (?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            image_id,
                            communication_id,
                            parent["patient_identity"],
                            rel,
                            fname,
                            len(data),
                            content_hash,
                            now,
                        ),
                    )
                    holder["parent"] = increment_attachment_parent(
                        conn,
                        parent_type="communication",
                        parent_id=communication_id,
                        now=now,
                    )

                commit_publish(
                    conn,
                    operation_id=operation_id,
                    insert_business_row=insert_business,
                    write_version=lambda: None,
                    write_audit=lambda: write_attachment_audit(
                        conn,
                        locked_actor=locked_actor,
                        entity_type="communication",
                        entity_id=communication_id,
                        action="upload_communication_image",
                        attachment_id=image_id,
                        revision=holder["parent"]["revision"],
                        created_at=now,
                    ),
                )
                conn.commit()
            recover_attachment_file_operation(db_path, images_dir, operation_id)
            return {
                "ok": True,
                "image_id": image_id,
                "revision": holder["parent"]["revision"],
            }
        except BaseException as error:
            _recover_failure(operation_id, error)

    @router.get("/api/communications/{communication_id}/images/{image_id}/file")
    def communication_image_file(communication_id: str, image_id: str):
        require_perm("patient.view"); require_perm("communication.view")
        with connect(db_path) as conn:
            require_comm(conn, communication_id)
            if image_id in hidden_attachment_ids(
                conn,
                parent_type="communication",
                parent_id=communication_id,
            ):
                raise HTTPException(status_code=404, detail="image not found")
            row = conn.execute(
                "select rel_path from communication_images where image_id = ? and communication_id = ?",
                (image_id, communication_id),
            ).fetchone()
        if not row or not row["rel_path"]:
            raise HTTPException(status_code=404, detail="image not found")
        images_root = images_dir.resolve()
        fp = (images_dir / row["rel_path"]).resolve()
        if not fp.is_relative_to(images_root) or not fp.is_file():
            raise HTTPException(status_code=404, detail="image not found")
        return FileResponse(fp)

    @router.delete("/api/communications/{communication_id}/images/{image_id}")
    def delete_communication_image(
        communication_id: str,
        image_id: str,
        payload: Any = Body(default=None),
    ):
        if access_authorizer_active():
            require_access("communication.image.manage")
        else:
            require_perm("patient.view"); require_perm("communication.manage")
        actor = request_write_actor()
        if type(payload) is not dict or set(payload) != {"expected_revision"}:
            raise HTTPException(status_code=400, detail="invalid_request_body")
        expected_revision = payload["expected_revision"]
        now = now_str()
        operation_id = None
        try:
            with closing(connect(db_path)) as conn:
                begin_immediate(conn)
                _, parent = lock_attachment_parent(
                    conn,
                    actor=actor,
                    permissions=("communication.image.manage",),
                    parent_type="communication",
                    parent_id=communication_id,
                    expected_revision=expected_revision,
                )
                row = conn.execute(
                    "select rel_path from communication_images "
                    "where image_id=? and communication_id=?",
                    (image_id, communication_id),
                ).fetchone()
                if row is None:
                    raise HTTPException(status_code=404, detail="image not found")
                rel = row["rel_path"]
                paths = {
                    "temp_rel_path": "",
                    "final_rel_path": rel,
                    "tombstone_rel_path": str(
                        Path(".attachment-tombstones")
                        / (uuid.uuid4().hex + ".deleted")
                    ),
                }
                operation_id = stage_delete(
                    conn,
                    parent_type="communication",
                    parent_id=communication_id,
                    attachment_id=image_id,
                    expected_revision=expected_revision,
                    paths=paths,
                    images_dir=images_dir,
                )
                conn.commit()
            tombstone_staged_file(db_path, images_dir, operation_id)
            holder = {}
            with closing(connect(db_path)) as conn:
                begin_immediate(conn)
                locked_actor, _ = lock_attachment_parent(
                    conn,
                    actor=actor,
                    permissions=("communication.image.manage",),
                    parent_type="communication",
                    parent_id=communication_id,
                    expected_revision=expected_revision,
                    allow_operation_id=operation_id,
                )

                def delete_business():
                    conn.execute(
                        "delete from communication_images where image_id=? "
                        "and communication_id=?",
                        (image_id, communication_id),
                    )
                    holder["parent"] = increment_attachment_parent(
                        conn,
                        parent_type="communication",
                        parent_id=communication_id,
                        now=now,
                    )

                commit_delete(
                    conn,
                    operation_id=operation_id,
                    delete_business_row=delete_business,
                    write_version=lambda: None,
                    write_audit=lambda: write_attachment_audit(
                        conn,
                        locked_actor=locked_actor,
                        entity_type="communication",
                        entity_id=communication_id,
                        action="delete_communication_image",
                        attachment_id=image_id,
                        revision=holder["parent"]["revision"],
                        created_at=now,
                    ),
                )
                conn.commit()
            recover_attachment_file_operation(db_path, images_dir, operation_id)
            return {
                "ok": True,
                "image_id": image_id,
                "revision": holder["parent"]["revision"],
                "file_removed": True,
            }
        except BaseException as error:
            _recover_failure(operation_id, error)

    def _unlink_all(rels, communication_id, patient_identity):
        """删盘：返回 (成功删除数, 失败数)；失败的落 delete_communication_file_failed 审计
        （带 patient_identity 供患者审计追溯 ，可重试清理），不静默。"""
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
                        conn, "communication", communication_id, "delete_communication_file_failed",
                        old_json=json.dumps({"rel_path": rel, "patient_identity": patient_identity},
                                            ensure_ascii=False),
                    )
                    conn.commit()
        return removed, failed

    return router
