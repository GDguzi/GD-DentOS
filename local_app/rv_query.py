"""统一回访只读查询口径：列表、月历、总览与 CSV 导出共用。"""
from __future__ import annotations

import re

from fastapi import HTTPException

from local_app.timeutil import today_str
from local_app.validation import valid_date_param


DONE_STATUSES = ("done", "已回访", "完成", "4")
PENDING_STATUSES = ("待回访", "待跟进", "做计划")
LOW_SATISFACTION = frozenset(("不满意", "非常不满意", "差", "很差"))

# GD-06:回访方式唯一规范化表达式(筛选与汇总必须共用,避免列表命中但汇总记 unknown)。
# 口径:显式 channel 优先并归桶;旧数据回退中文 return_type——含"电话"→phone,含"微信"→wechat,
# 其余非空→other,空→''(unknown)。与 return_visit_transactions.normalized_rv_channel 同口径。
RV_CHANNEL_SQL = (
    "case"
    " when coalesce(r.channel, '') = 'phone' then 'phone'"
    " when coalesce(r.channel, '') = 'wechat' then 'wechat'"
    " when coalesce(r.channel, '') != '' then 'other'"
    " when coalesce(r.return_type, '') = '' then ''"
    " when r.return_type like '%电话%' then 'phone'"
    " when r.return_type like '%微信%' then 'wechat'"
    " else 'other' end"
)


def _sql_literals(values):
    return "(" + ", ".join("'%s'" % value.replace("'", "''") for value in values) + ")"


def build_done_cond(done=DONE_STATUSES, pending=PENDING_STATUSES):
    """生成唯一完成态 SQL；显式待办状态优先于旧前缀标记。"""
    return (
        f"coalesce(r.status, '') in {_sql_literals(done)} "
        f"or (coalesce(r.status, '') not in {_sql_literals(pending)} "
        "and (coalesce(r.item_name, '') like '已回访%' "
        "or coalesce(r.note, '') like '已回访%'))"
    )


DONE_COND = build_done_cond()


def _person_and_q_filters(*, q="", doctor="", visitor=""):
    parts = []
    params = []
    doctor = doctor.strip()
    visitor = visitor.strip()
    q = q.strip()
    if doctor:
        parts.append("coalesce(p.responsible_doctor, '') = ?")
        params.append(doctor)
    if visitor:
        parts.append("coalesce(r.visitor, '') = ?")
        params.append(visitor)
    if q:
        like = f"%{q}%"
        parts.append(
            "(r.return_visit_id like ? or r.patient_identity like ? "
            "or coalesce(r.item_name, '') like ? or coalesce(r.note, '') like ? "
            "or coalesce(r.return_result, '') like ? or coalesce(p.display_name, '') like ? "
            "or coalesce(p.phone, '') like ? or coalesce(p.name_pinyin, '') like ? "
            "or coalesce(p.chart_no, '') like ?)"
        )
        params.extend([like] * 9)
    return parts, params


def build_rv_filter(
    *, date="", date_from="", date_to="", status="", doctor="", visitor="",
    channel="", rtype="", att="", q="", item="", patient_q="", content_q="",
    patient="", default_today=True,
):
    """返回列表/导出的参数化 ``(where_sql, params)``。"""
    date = date.strip()
    date_from = date_from.strip()
    date_to = date_to.strip()
    status = status.strip()
    if default_today and not date and not date_from and not date_to:
        date = today_str()
    for value, field in ((date, "date"), (date_from, "date_from"), (date_to, "date_to")):
        if value:
            valid_date_param(value, field)
    if date_from and date_to and date_from > date_to:
        raise HTTPException(status_code=400, detail="date_from 不能晚于 date_to")

    parts = [
        "coalesce(r.is_deleted, 0) = 0",
        "coalesce(p.is_deleted, 0) = 0",
        "substr(coalesce(r.due_time, ''), 1, 10) != '0000-00-00'",
    ]
    params = []
    if date_from:
        parts.append("substr(coalesce(r.due_time, ''), 1, 10) >= ?")
        params.append(date_from)
    if date_to:
        parts.append("substr(coalesce(r.due_time, ''), 1, 10) <= ?")
        params.append(date_to)
    if date and not date_from and not date_to:
        parts.append("substr(coalesce(r.due_time, ''), 1, 10) <= ?")
        params.append(date)
    if status == "all":
        pass
    elif status == "已逾期":
        parts.extend((f"not ({DONE_COND})", "substr(r.due_time, 1, 10) < ?"))
        params.append(today_str())
    elif status:
        parts.append("coalesce(r.status, '') = ?")
        params.append(status)
    else:
        parts.append(f"not ({DONE_COND})")

    channel = channel.strip()
    rtype = rtype.strip()
    att = att.strip()
    if channel:
        parts.append(f"{RV_CHANNEL_SQL} = ?")
        params.append("" if channel == "unknown" else channel)
    if rtype:
        parts.append("coalesce(r.return_type, '') = ?")
        params.append(rtype)
    if att == "recording":
        parts.append(
            "exists (select 1 from calls k "
            "where k.linked_return_visit_id = r.return_visit_id "
            "and not exists (select 1 from attachment_file_ops afo "
            "where afo.parent_type='call' and afo.parent_id=k.call_id "
            "and afo.attachment_id=k.call_id and afo.status='staging_delete'))"
        )
    elif att == "screenshot":
        parts.append(
            "exists (select 1 from return_visit_images i "
            "where i.return_visit_id = r.return_visit_id "
            "and not exists (select 1 from attachment_file_ops afo "
            "where afo.parent_type='return_visit' "
            "and afo.parent_id=r.return_visit_id "
            "and afo.attachment_id=i.image_id and afo.status='staging_delete'))"
        )
    elif att == "image":
        parts.append("exists (select 1 from patient_images pi where pi.patient_identity = r.patient_identity)")
    elif att:
        raise HTTPException(status_code=400, detail="att 只能是 recording/screenshot/image 或空")

    item = item.strip()
    patient_q = patient_q.strip()
    content_q = content_q.strip()
    patient = patient.strip()
    if item:
        parts.append("coalesce(r.item_name, '') like ?")
        params.append(f"%{item}%")
    if patient_q:
        like = f"%{patient_q}%"
        parts.append(
            "(coalesce(p.display_name, '') like ? or coalesce(p.name_pinyin, '') like ? "
            "or coalesce(p.chart_no, '') like ? or coalesce(p.phone, '') like ? "
            "or r.patient_identity like ?)"
        )
        params.extend([like] * 5)
    if content_q:
        like = f"%{content_q}%"
        parts.append(
            "(coalesce(r.item_name, '') like ? or coalesce(r.note, '') like ? "
            "or coalesce(r.return_result, '') like ?)"
        )
        params.extend([like] * 3)
    if patient:
        parts.append("r.patient_identity = ?")
        params.append(patient)
    extra, extra_params = _person_and_q_filters(q=q, doctor=doctor, visitor=visitor)
    parts.extend(extra)
    params.extend(extra_params)
    return "where " + " and ".join(parts), params


def _month_cte(month, *, q="", status="", doctor="", visitor="", channel="", today=None):
    if type(month) is not str or re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month) is None:
        raise HTTPException(status_code=400, detail="month must be YYYY-MM")
    today = today or today_str()
    parts = ["coalesce(r.is_deleted, 0) = 0", "coalesce(p.is_deleted, 0) = 0"]
    params = []
    extra, extra_params = _person_and_q_filters(q=q, doctor=doctor, visitor=visitor)
    parts.extend(extra)
    params.extend(extra_params)
    channel = channel.strip()
    status = status.strip()
    if channel:
        parts.append(f"{RV_CHANNEL_SQL} = ?")
        params.append("" if channel == "unknown" else channel)
    if status == "已逾期":
        parts.extend((f"not ({DONE_COND})", "substr(coalesce(r.due_time, ''), 1, 10) < ?"))
        params.append(today)
    elif status not in ("", "all"):
        parts.append("coalesce(r.status, '') = ?")
        params.append(status)
    base_where = " and ".join(parts)
    sql = f"""
        with base as (
            select r.return_visit_id, {RV_CHANNEL_SQL} as channel, r.return_result, r.satisfaction,
                   r.due_time, r.actual_date,
                   case when ({DONE_COND}) then 1 else 0 end as is_done
            from return_visits r
            join patients p on p.patient_identity = r.patient_identity
            where {base_where}
        ), bucketed as (
            select *,
                   case when is_done = 1
                        then substr(coalesce(nullif(actual_date, ''), due_time, ''), 1, 10)
                        else substr(coalesce(due_time, ''), 1, 10) end as bucket_day,
                   case when is_done = 0 and substr(coalesce(due_time, ''), 1, 10) < ?
                        then 1 else 0 end as is_overdue
            from base
        ), month_rows as (
            select * from bucketed
            where bucket_day != '0000-00-00' and substr(bucket_day, 1, 7) = ?
        )
        select * from month_rows order by bucket_day, return_visit_id
    """
    return sql, params + [today, month]


def build_month_calendar(
    conn, month, *, q="", status="", doctor="", visitor="", channel="", today=None,
):
    """由一个共享 CTE 生成日格和不受分页影响的月级 summary。"""
    sql, params = _month_cte(
        month, q=q, status=status, doctor=doctor, visitor=visitor, channel=channel, today=today
    )
    rows = conn.execute(sql, tuple(params)).fetchall()
    days = {}
    channels = {"phone": 0, "wechat": 0, "other": 0, "unknown": 0}
    result_counts = {}
    completed = pending = overdue = low = needs = 0
    for row in rows:
        day = row["bucket_day"]
        bucket = days.setdefault(day, {"date": day, "count": 0, "done": 0, "pending": 0, "overdue": 0})
        bucket["count"] += 1
        if row["is_done"]:
            completed += 1
            bucket["done"] += 1
        else:
            pending += 1
            bucket["pending"] += 1
        if row["is_overdue"]:
            overdue += 1
            bucket["overdue"] += 1
        channel_key = (row["channel"] or "").strip()
        channel_key = channel_key if channel_key in ("phone", "wechat") else ("unknown" if not channel_key else "other")
        channels[channel_key] += 1
        satisfaction = (row["satisfaction"] or "").strip()
        is_low = satisfaction in LOW_SATISFACTION
        low += int(is_low)
        needs += int(bool(row["is_overdue"] or is_low))
        result = (row["return_result"] or "").strip()
        if result:
            result_counts[result] = result_counts.get(result, 0) + 1
    total = len(rows)
    summary = {
        "total": total,
        "completed": completed,
        "pending": pending,
        "overdue": overdue,
        "completion_rate": round(completed * 100 / total, 1) if total else 0.0,
        "channel_counts": [{"channel": key, "count": channels[key]} for key in ("phone", "wechat", "other", "unknown")],
        "problem_counts": {"needs_attention": needs, "overdue": overdue, "low_satisfaction": low},
        "top_results": [
            {"label": result, "count": count}
            for result, count in sorted(result_counts.items(), key=lambda item: (-item[1], item[0]))[:5]
        ],
        "narrative": {"status": "disabled", "text": ""},
    }
    out_days = [days[key] for key in sorted(days)]
    return {"month": month, "days": out_days, "total": total, "summary": summary}


def build_monthly_narrative(summary, executor=None):
    if executor is None:
        return {"status": "disabled", "text": ""}
    try:
        if type(summary) is not dict:
            raise ValueError("summary must be a dict")

        def number(value):
            if type(value) not in (int, float):
                raise ValueError("aggregate must be numeric")
            return value

        raw_channels = summary.get("channel_counts")
        problem_source = summary.get("problem_counts")
        if type(raw_channels) not in (list, tuple) or type(problem_source) is not dict:
            raise ValueError("nested aggregates malformed")
        channel_source = {}
        for item in raw_channels:
            if type(item) is not dict or item.get("channel") not in ("phone", "wechat", "other", "unknown"):
                continue
            channel_source[item["channel"]] = number(item.get("count"))
        payload = {
            "total": number(summary.get("total")),
            "completed": number(summary.get("completed")),
            "pending": number(summary.get("pending")),
            "overdue": number(summary.get("overdue")),
            "completion_rate": number(summary.get("completion_rate")),
            "channel_counts": [
                {"channel": channel, "count": channel_source.get(channel, 0)}
                for channel in ("phone", "wechat", "other", "unknown")
            ],
            "problem_counts": {
                key: number(problem_source.get(key))
                for key in ("needs_attention", "overdue", "low_satisfaction")
            },
        }
        text = executor(payload)
        if type(text) is not str:
            raise ValueError("narrative executor must return text")
        return {"status": "ready", "text": text}
    except Exception:
        return {"status": "failed", "text": ""}


def csv_safe(value):
    text = str(value or "")
    probe = text.lstrip(" \t\r\n")
    if probe.startswith(("=", "+", "-", "@")) or text.startswith(("\t", "\r", "\n")):
        return "'" + text
    return text
