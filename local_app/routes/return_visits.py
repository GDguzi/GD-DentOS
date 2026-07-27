"""回访列表/更新(业务层)：分页列表+字段更新+版本快照+审计。工作流操作在 return_visit_ops。"""
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Body, HTTPException

from local_app.auth import require_perm
from local_app.access_authorization import has_access, require_access
from local_app.return_visit_transactions import (
    request_write_actor,
    update_return_visit as atomic_update_return_visit,
)
from local_app.db import connect
from local_app.rv_query import build_rv_filter
from local_app.snapshots import row_to_dict as _row_to_dict


def create_return_visits_router(db_path):
    router = APIRouter()
    db_path = Path(db_path)

    @router.get("/api/return-visits")
    def return_visit_list(
        date: str = "",
        date_from: str = "",
        date_to: str = "",
        status: str = "",
        doctor: str = "",
        visitor: str = "",
        channel: str = "",
        rtype: str = "",
        att: str = "",
        q: str = "",
        item: str = "",
        patient_q: str = "",
        content_q: str = "",
        patient: str = "",
        pageno: int = 1,
        pagesize: int = 50,
    ):
        require_access("return_visit.view")
        if att.strip() == "recording":
            require_access("call_record.view")
        elif att.strip() == "image":
            require_access("patient.image.view")
        where_sql, params = build_rv_filter(
            date=date, date_from=date_from, date_to=date_to, status=status,
            doctor=doctor, visitor=visitor, channel=channel, rtype=rtype,
            att=att, q=q, item=item, patient_q=patient_q,
            content_q=content_q, patient=patient,
        )
        pageno = max(pageno, 1)
        pagesize = min(max(pagesize, 1), 200)
        offset = (pageno - 1) * pagesize

        can_recording = has_access("call_record.view")
        recording_sql = (
            "exists (select 1 from calls k "
            "where k.linked_return_visit_id = r.return_visit_id "
            "and not exists (select 1 from attachment_file_ops afo "
            "where afo.parent_type='call' and afo.parent_id=k.call_id "
            "and afo.attachment_id=k.call_id and afo.status='staging_delete'))"
            if can_recording else "0"
        )
        image_sql = (
            "exists (select 1 from patient_images pi where pi.patient_identity = r.patient_identity)"
            if has_access("patient.image.view") else "0"
        )

        with connect(db_path) as conn:
            total = conn.execute(
                f"""
                select count(*)
                from return_visits r
                join patients p on p.patient_identity = r.patient_identity
                {where_sql}
                """,
                tuple(params),
            ).fetchone()[0]
            rows = conn.execute(
                f"""
                select
                    r.return_visit_id, r.patient_identity, p.display_name, p.phone,
                    p.responsible_doctor,
                    r.due_time, r.item_name, r.status, r.note,
                    r.return_doctor, r.visitor, r.channel, r.return_type,
                    r.return_result, r.actual_date, r.revision, r.updated_at,
                    exists (select 1 from return_visit_images i
                            where i.return_visit_id = r.return_visit_id
                              and not exists (
                                  select 1 from attachment_file_ops afo
                                  where afo.parent_type = 'return_visit'
                                    and afo.parent_id = r.return_visit_id
                                    and afo.attachment_id = i.image_id
                                    and afo.status = 'staging_delete'
                              )) as has_screenshot,
                    {recording_sql} as has_recording,
                    {image_sql} as has_image
                from return_visits r
                join patients p on p.patient_identity = r.patient_identity
                {where_sql}
                order by coalesce(r.due_time, '') desc, r.return_visit_id desc
                limit ? offset ?
                """,
                tuple(params + [pagesize, offset]),
            ).fetchall()

        return {
            "list": [_row_to_dict(row) for row in rows],
            "totalcount": total,
            "pageno": pageno,
            "pagesize": pagesize,
        }

    @router.put("/api/return-visits/{return_visit_id}")
    def update_return_visit(
        return_visit_id: str, payload: Any = Body(default=None)
    ):
        require_perm("patient.edit")
        actor = request_write_actor()
        return atomic_update_return_visit(
            db_path, return_visit_id, payload, actor
        )

    return router
