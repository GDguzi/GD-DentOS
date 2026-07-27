"""客户通今日总览（左侧「客户通」默认视图）：把某天的 回访 / 沟通记录 / 通话录音 三块聚合成只读看板。

只读业务数据、不反写、不入侵业务（删掉本模块业务照跑）。今日回访口径与今日工作台一致
（due_time 当天未回访=待回访；actual/due 当天已回访=已回访）。沟通/录音按各自表的日期当天聚合。
按权限分别门控：无权的区块不查库、不返回（防无权角色从看板推断记录）。
时区一律北京时间。
"""

from fastapi import APIRouter

from local_app.access_authorization import has_access, require_access
from local_app.db import connect
from local_app.rv_query import DONE_COND
from local_app.timeutil import today_str
from local_app.validation import valid_date_param


def create_customer_hub_router(db_path):
    router = APIRouter()

    @router.get("/api/customer-hub/today")
    def customer_hub_today(date: str = ""):
        require_access("patient.profile.view")
        day = (date or "").strip() or today_str()
        valid_date_param(day, "date")   # 坏日期(如 20260710/2026-99-99)→400,不静默伪装成"今日无记录"
        out = {"date": day}
        can_view_return_visits = has_access("return_visit.view")
        can_view_communications = has_access("communication.view")
        can_view_calls = has_access("call_record.view")
        with connect(db_path) as conn:
            if can_view_return_visits:
                pending = conn.execute(
                    f"""
                select r.return_visit_id, r.patient_identity, p.display_name, p.phone,
                       p.responsible_doctor, r.due_time, r.item_name, r.note, r.status,
                       r.return_doctor, r.visitor, r.channel, r.return_type,
                       r.return_result, r.revision
                from return_visits r
                join patients p on p.patient_identity = r.patient_identity
                  and coalesce(p.is_deleted, 0) = 0
                where coalesce(r.is_deleted, 0) = 0
                  and substr(coalesce(r.due_time, ''), 1, 10) = ?
                  and substr(coalesce(r.due_time, ''), 1, 10) != '0000-00-00'
                  and not ({DONE_COND})
                order by coalesce(r.due_time, ''), r.return_visit_id
                """,
                    (day,),
                ).fetchall()
                done = conn.execute(
                    f"""
                select r.return_visit_id, r.patient_identity, p.display_name, p.phone,
                       p.responsible_doctor, r.due_time, r.item_name, r.note, r.status,
                       r.return_doctor, r.visitor, r.channel, r.return_type,
                       r.return_result, r.revision
                from return_visits r
                join patients p on p.patient_identity = r.patient_identity
                  and coalesce(p.is_deleted, 0) = 0
                where coalesce(r.is_deleted, 0) = 0
                  and substr(coalesce(nullif(r.actual_date, ''), r.due_time, ''), 1, 10) = ?
                  and substr(coalesce(nullif(r.actual_date, ''), r.due_time, ''), 1, 10) != '0000-00-00'
                  and ({DONE_COND})
                order by coalesce(nullif(r.actual_date, ''), r.due_time, ''), r.return_visit_id
                """,
                    (day,),
                ).fetchall()
                def rv_row(row):
                    return {key: row[key] for key in row.keys()}
                out["return_visits"] = {
                    "pending_count": len(pending),
                    "done_count": len(done),
                    "pending": [rv_row(row) for row in pending],
                    "done": [rv_row(row) for row in done],
                }

            # 今日沟通记录（按 communication.view 门控）
            if can_view_communications:
                rows = conn.execute(
                    """
                    select c.communication_id, c.patient_identity, p.display_name,
                           c.channel, c.direction, c.reason, c.contacted_at,
                           c.operator, c.content, c.deal_status, c.revision
                    from communications c
                    join patients p on p.patient_identity = c.patient_identity
                      and coalesce(p.is_deleted, 0) = 0
                    where substr(coalesce(c.contacted_at, ''), 1, 10) = ?
                    order by coalesce(c.contacted_at, '') desc, c.created_at desc
                    """,
                    (day,),
                ).fetchall()
                out["communications"] = [{
                    "communication_id": r["communication_id"],
                    "patient_identity": r["patient_identity"],
                    "display_name": r["display_name"],
                    "channel": r["channel"],
                    "direction": r["direction"],
                    "reason": r["reason"],
                    "contacted_at": r["contacted_at"],
                    "operator": r["operator"],
                    "content": r["content"],
                    "deal_status": r["deal_status"],
                    "revision": r["revision"],
                } for r in rows]

            # 今日通话录音（按 call_record.view 门控）
            if can_view_calls:
                rows = conn.execute(
                    """
                    select k.call_id, k.patient_identity, p.display_name, k.direction,
                           k.peer_norm, k.started_at, k.duration_sec, k.deal_status,
                           k.revision
                    from calls k
                    join patients p on p.patient_identity = k.patient_identity
                      and coalesce(p.is_deleted, 0) = 0
                    where substr(coalesce(k.started_at, ''), 1, 10) = ?
                      and not exists (
                          select 1 from attachment_file_ops afo
                          where afo.parent_type = 'call'
                            and afo.parent_id = k.call_id
                            and afo.attachment_id = k.call_id
                            and afo.status = 'staging_delete'
                      )
                    order by coalesce(k.started_at, '') desc, k.created_at desc
                    """,
                    (day,),
                ).fetchall()
                out["calls"] = [{
                    "call_id": r["call_id"],
                    "patient_identity": r["patient_identity"],
                    "display_name": r["display_name"],
                    "direction": r["direction"],
                    "peer_norm": r["peer_norm"],
                    "started_at": r["started_at"],
                    "duration_sec": r["duration_sec"],
                    "deal_status": r["deal_status"],
                    "revision": r["revision"],
                } for r in rows]
        return out

    return router
