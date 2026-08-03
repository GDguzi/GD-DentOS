"""今日工作台(呈现层,只读聚合)：预约/回访/收费/审计等当日概览,按数据域分权。"""
import datetime as dt
from pathlib import Path

from fastapi import APIRouter

from local_app.access_authorization import has_access, require_access
from local_app.timeutil import today_str
from local_app.db import (active_bill_clause, bill_discount_sql,
                          bill_net_arrears_sql, connect)
from local_app.models import appt_stage
from local_app.paid_detail import split_paid_detail  # #525 支付方式拆分(核心真相源,同步层同用)
from local_app.rv_query import DONE_COND
from local_app.snapshots import row_to_dict as _row_to_dict, rows as _rows
from local_app.validation import valid_date_param


def create_today_work_router(db_path):
    router = APIRouter()
    db_path = Path(db_path)

    @router.get("/api/today-work")
    def today_work(date: str = ""):
        require_access("patient.profile.view")
        see_billing = has_access("billing.view")
        see_audit = has_access("audit.view")
        work_date = date.strip() or today_str()
        parsed_work_date = valid_date_param(work_date, "date")
        tomorrow = (parsed_work_date + dt.timedelta(days=1)).isoformat()

        with connect(db_path) as conn:
            # #418:今日工作台统一隐藏取消/疑似取消的预约(KPI/候诊队列/漏斗/就诊类型桶/明日预约都排除)。
            # 数据本身保留,预约管理与历史查询仍可追溯。suspect_cancelled=同步对账标记的疑似取消。
            not_cancelled = ("coalesce(status, '') not in ('3', '已取消', '已爽约', '爽约') "
                             "and coalesce(suspect_cancelled, 0) = 0")
            not_cancelled_a = ("coalesce(a.status, '') not in ('3', '已取消', '已爽约', '爽约') "
                               "and coalesce(a.suspect_cancelled, 0) = 0")
            # 审查#21：今日就诊 KPI 排除已取消/已爽约,与初/复/新诊明细口径一致
            appointments_count = conn.execute(
                f"""
                select count(*)
                from appointments
                where substr(coalesce(start_time, ''), 1, 10) = ?
                  and {not_cancelled}
                """,
                (work_date,),
            ).fetchone()[0]
            tomorrow_appointments_count = conn.execute(
                f"""
                select count(*)
                from appointments
                where substr(coalesce(start_time, ''), 1, 10) = ?
                  and {not_cancelled}
                """,
                (tomorrow,),
            ).fetchone()[0]
            birthdays_today_count = conn.execute(
                """
                select count(*)
                from patients
                where birthday is not null
                  and birthday != ''
                  and substr(birthday, 1, 10) != '0000-00-00'
                  and substr(birthday, 6, 5) = substr(?, 6, 5)
                """,
                (work_date,),
            ).fetchone()[0]
            appointments = _rows(
                conn,
                f"""
                select
                    a.appointment_id, a.patient_identity, p.display_name, p.phone, p.sex, p.birthday,
                    a.start_time, a.end_time, a.doctor_name, a.item_name, a.status, a.room,
                    a.arrived_at, a.finished_at, a.visit_type, a.register_type
                from appointments a
                left join patients p on p.patient_identity = a.patient_identity
                where substr(coalesce(a.start_time, ''), 1, 10) = ?
                  and {not_cancelled_a}
                order by coalesce(a.start_time, ''), a.appointment_id
                limit 300
                """,
                (work_date,),
            )
            # 候诊队列实时状态：每个患者今天处置了没/病历写了没/欠多少钱
            # (跨诊室共享同一个库，别的诊室做了处置/收费，这里刷新就体现)
            for appt in appointments:
                pid = appt.get("patient_identity")
                if not pid:
                    appt["treated_today"] = False
                    appt["record_today"] = False
                    if see_billing:
                        appt["unpaid_amount"] = 0
                    appt["today_items"] = ""
                    appt["paid_today"] = False
                    appt["return_visit_today"] = False
                    appt["rebooked_today"] = False
                    continue
                # 今日实际项目：当天处置单下的项目名(区别于 item_name=预约项目)
                appt["today_items"] = conn.execute(
                    "select group_concat(distinct ti.item_name) from treatment_items ti "
                    "join treatment_orders o on o.order_id = ti.order_id "
                    "where ti.patient_identity = ? and substr(coalesce(o.order_date,''),1,10) = ? "
                    "and coalesce(o.status,'') != 'voided' "
                    "and coalesce(ti.is_deleted,0)=0 and coalesce(ti.is_refund,0)=0",
                    (pid, work_date),
                ).fetchone()[0] or ""
                appt["treated_today"] = conn.execute(
                    "select 1 from treatment_orders where patient_identity = ? "
                    "and substr(coalesce(order_date, ''), 1, 10) = ? "
                    "and coalesce(status, '') != 'voided' limit 1",   # #97 排除已撤销处置
                    (pid, work_date),
                ).fetchone() is not None
                appt["record_today"] = conn.execute(
                    "select 1 from medical_records where patient_identity = ? "
                    "and substr(coalesce(visit_time, ''), 1, 10) = ? limit 1",
                    (pid, work_date),
                ).fetchone() is not None
                if see_billing:
                    appt["unpaid_amount"] = conn.execute(
                        f"select round(coalesce(sum({bill_net_arrears_sql()}), 0), 2) from bills "
                        f"where patient_identity = ? and {bill_net_arrears_sql()} > 0.005 "
                        f"and {active_bill_clause()}",
                        (pid,),
                    ).fetchone()[0]
                # 今日是否已收款(队列"收费"按钮已收高亮用)：当天有正向收款记录
                appt["paid_today"] = conn.execute(
                    "select 1 from payments where patient_identity = ? "
                    "and substr(coalesce(pay_time, ''), 1, 10) = ? "
                    "and coalesce(amount, 0) > 0 and coalesce(state, '') != '1' limit 1",
                    (pid, work_date),
                ).fetchone() is not None
                # 卡片状态标签(对标官方今日卡)：影像/未来预约/回访/分组
                appt["has_image"] = conn.execute(
                    "select 1 from patient_images where patient_identity = ? limit 1", (pid,)
                ).fetchone() is not None
                # #423:约下次高亮的"未来预约"也要排取消/疑似取消/爽约,口径同 #418(否则取消的未来约也点亮)
                appt["has_future_appt"] = conn.execute(
                    f"select 1 from appointments where patient_identity = ? "
                    f"and {not_cancelled} "
                    f"and substr(coalesce(start_time,''),1,10) > date('now','localtime') limit 1", (pid,)
                ).fetchone() is not None
                appt["has_return_visit"] = conn.execute(
                    "select 1 from return_visits where patient_identity = ? and coalesce(is_deleted,0)=0 limit 1", (pid,)
                ).fetchone() is not None
                # 队列「预约」按钮高亮:用户口径=今天约了"下次就诊"才亮。即 created_at=今天(今天新建)
                # 且 start_time 在未来(约的是下次,不是当前这条到诊预约)、且未取消/疑似取消(#433)。
                appt["rebooked_today"] = conn.execute(
                    f"select 1 from appointments where patient_identity = ? "
                    f"and substr(coalesce(created_at,''),1,10) = ? "
                    f"and substr(coalesce(start_time,''),1,10) > ? and {not_cancelled} limit 1",
                    (pid, work_date, work_date),
                ).fetchone() is not None
                # 队列「回访」按钮高亮:用户口径=今天本地新增了回访才亮(同步碰 updated_at 不算)。
                # 按 created_at=今天判(本地新增 return_visit 才填 created_at)。
                appt["return_visit_today"] = conn.execute(
                    "select 1 from return_visits where patient_identity = ? and coalesce(is_deleted,0)=0 "
                    "and substr(coalesce(created_at,''),1,10) = ? limit 1",
                    (pid, work_date),
                ).fetchone() is not None
                appt["groupname"] = conn.execute(
                    "select patient_group from patients where patient_identity = ?", (pid,)
                ).fetchone()[0] or ""
            # 工作台给前台用：只看「当天到期」的回访(逾期积压历史在回访管理模块查)
            return_visit_where = f"""
                coalesce(r.is_deleted, 0) = 0
                and coalesce(p.is_deleted, 0) = 0
                and substr(coalesce(r.due_time, ''), 1, 10) = ?
                and substr(coalesce(r.due_time, ''), 1, 10) != '0000-00-00'
                and not ({DONE_COND})
            """
            return_visits_count = conn.execute(
                f"""
                select count(*)
                from return_visits r
                join patients p on p.patient_identity = r.patient_identity
                where {return_visit_where}
                """,
                (work_date,),
            ).fetchone()[0]
            return_visits = _rows(
                conn,
                f"""
                select
                    r.return_visit_id, r.patient_identity, p.display_name, p.phone,
                    r.due_time, r.item_name, r.status, r.note
                from return_visits r
                join patients p on p.patient_identity = r.patient_identity
                where {return_visit_where}
                order by coalesce(r.due_time, ''), r.return_visit_id
                limit 20
                """,
                (work_date,),
            )
            completed_return_visits_count = conn.execute(
                f"""
                select count(*)
                from return_visits r
                join patients p on p.patient_identity = r.patient_identity
                where coalesce(r.is_deleted, 0) = 0
                  and coalesce(p.is_deleted, 0) = 0
                  -- #54：已回访按实际回访日期(actual_date)统计；缺失则回退应回访日期(due_time)
                  and substr(coalesce(nullif(r.actual_date, ''), r.due_time, ''), 1, 10) = ?
                  and ({DONE_COND})
                """,
                (work_date,),
            ).fetchone()[0]
            if see_billing:
                unpaid_bills_count = conn.execute(
                    """
                    select count(*)
                    from bills
                    where {net} > 0.005 and {active}
                      and substr(coalesce(bill_time, ''), 1, 10) = ?
                    """.format(net=bill_net_arrears_sql(), active=active_bill_clause()),
                    (work_date,),
                ).fetchone()[0]
                unpaid_bills = _rows(
                    conn,
                    """
                    select
                        b.bill_id, b.patient_identity, p.display_name, p.phone,
                        b.bill_no, b.bill_time, b.total_fee, b.paid_fee,
                        {net} as unpaid_fee,
                        b.state
                    from bills b
                    left join patients p on p.patient_identity = b.patient_identity
                    where {net} > 0.005 and {active}
                      and substr(coalesce(b.bill_time, ''), 1, 10) = ?
                    order by unpaid_fee desc, coalesce(b.bill_time, '') desc
                    limit 20
                    """.format(
                        net=bill_net_arrears_sql("b."),
                        active=active_bill_clause("b.state"),
                    ),
                    (work_date,),
                )
                today_billing = conn.execute(
                    """
                    select
                        round(coalesce(sum(total_fee - {disc}), 0), 2) as receivable,
                        round(coalesce(sum(paid_fee), 0), 2) as paid
                    from bills
                    where substr(coalesce(bill_time, ''), 1, 10) = ?
                      and {active}
                    """.format(disc=bill_discount_sql(), active=active_bill_clause()),
                    (work_date,),
                ).fetchone()
                pay_active = """(coalesce(json_extract(iif(json_valid(p.source_json), p.source_json, '{}'), '$.CancelMark'), '') = ''
                           or coalesce(p.amount, 0) < 0)"""
                today_cash_in = conn.execute(
                    f"""
                    select round(coalesce(sum(p.amount), 0), 2)
                    from payments p
                    where substr(coalesce(p.pay_time, ''), 1, 10) = ?
                      and {pay_active}
                    """,
                    (work_date,),
                ).fetchone()[0]
                today_settlements_count = conn.execute(
                    f"""
                    select count(*)
                    from payments p
                    where substr(coalesce(p.pay_time, ''), 1, 10) = ?
                      and {pay_active}
                    """,
                    (work_date,),
                ).fetchone()[0]
                today_unpaid_amount = round(
                    float(today_billing["receivable"] or 0)
                    - float(today_billing["paid"] or 0),
                    2,
                )
            # 今日就诊流程漏斗：按预约状态分桶(0/待到诊·已预约=候诊, 1/已到诊, 2/已完成)
            appt_status_rows = conn.execute(
                f"""
                select coalesce(status, '') as status, count(*) as n
                from appointments
                where substr(coalesce(start_time, ''), 1, 10) = ?
                  and {not_cancelled}
                group by coalesce(status, '')
                """,
                (work_date,),
            ).fetchall()
            appts_waiting = appts_arrived = appts_done = 0
            for r in appt_status_rows:
                stage = appt_stage(r["status"])
                if stage < 0:
                    continue  # 取消不计入漏斗
                if stage <= 1:        # 待确认/已确认 = 候诊
                    appts_waiting += r["n"]
                elif stage <= 3:      # 已到达/已分诊 = 已到诊(在诊)
                    appts_arrived += r["n"]
                else:                 # 完成治疗/已离开 = 已完成
                    appts_done += r["n"]
            # 就诊类型分桶(初/复/新诊)：由分诊填的 visit_type 统计 → 驱动顶部 KPI
            first_visits = revisits = new_visits = 0
            for r in conn.execute(
                f"select coalesce(visit_type, '') vt, count(*) n from appointments "
                f"where substr(coalesce(start_time, ''), 1, 10) = ? "
                f"and {not_cancelled} "   # 审查#21 + #418：与今日预约总数口径一致(排取消/疑似取消)
                f"group by coalesce(visit_type, '')",
                (work_date,),
            ).fetchall():
                vt = str(r["vt"]).strip()
                if vt == "初诊":
                    first_visits += r["n"]
                elif vt == "复诊":
                    revisits += r["n"]
                elif vt in ("新诊", "点诊"):   # #452：SaaS 的「点诊」= 本地「新诊」,同步来的点诊并入新诊桶,否则漏统计
                    new_visits += r["n"]
            if see_audit:
                recent_audits_count = conn.execute(
                    "select count(*) from audit_logs"
                ).fetchone()[0]
                recent_audits = _rows(
                    conn,
                    """
                    select audit_id, entity_type, entity_id, action, operator, created_at
                    from audit_logs
                    order by created_at desc, audit_id desc
                    limit 10
                    """,
                )
            # #321/#362 工作台左入口右侧出简表用：明日预约 / 今日结算 / 今日生日
            # 三个轻量列表(只取展示必需列,limit 收口),与上面对应 count 同口径。
            tomorrow_appointments = _rows(
                conn,
                f"""
                select
                    a.appointment_id, a.patient_identity, p.display_name, p.phone,
                    a.start_time, a.doctor_name, a.item_name, a.status, a.room
                from appointments a
                left join patients p on p.patient_identity = a.patient_identity
                where substr(coalesce(a.start_time, ''), 1, 10) = ?
                  and {not_cancelled_a}
                order by coalesce(a.start_time, ''), a.appointment_id
                limit 30
                """,
                (tomorrow,),
            )
            if see_billing:
                today_settlements = _rows(
                    conn,
                    f"""
                    select
                        p.payment_id, p.patient_identity, pt.display_name, pt.phone,
                        b.bill_no, b.bill_id, p.pay_time, p.amount, p.pay_type,
                        coalesce(nullif(json_extract(iif(json_valid(p.source_json), p.source_json, '{{}}'), '$.Doctor'), ''),
                                 nullif(b.bill_doctor, ''), '') as doctor
                    from payments p
                    left join bills b on b.bill_id = p.bill_id
                    left join patients pt on pt.patient_identity = p.patient_identity
                    where substr(coalesce(p.pay_time, ''), 1, 10) = ?
                      and {pay_active}
                    order by coalesce(p.pay_time, ''), p.payment_id
                    limit 30
                    """,
                    (work_date,),
                )
                _method_rows = conn.execute(
                    f"""
                    select coalesce(nullif(p.pay_type, ''), '其他'),
                           coalesce(p.amount, 0),
                           json_extract(iif(json_valid(p.source_json), p.source_json, '{{}}'), '$.PaidDetail')
                    from payments p
                    where substr(coalesce(p.pay_time, ''), 1, 10) = ?
                      and {pay_active}
                    """,
                    (work_date,),
                ).fetchall()
                _method_sum = {}
                for _ptype, _amt, _detail in _method_rows:
                    _parts = split_paid_detail(_detail)
                    if len(_parts) > 1 and abs(sum(a for _, a in _parts) - _amt) < 0.01:
                        for _m, _a in _parts:
                            _method_sum[_m] = _method_sum.get(_m, 0) + _a
                    else:
                        _method_sum[_ptype] = _method_sum.get(_ptype, 0) + _amt
                today_pay_methods = [
                    {"pay_type": m, "amount": round(v, 2)}
                    for m, v in sorted(_method_sum.items(), key=lambda kv: -kv[1])
                ]
            birthdays = _rows(
                conn,
                """
                select patient_identity, display_name, phone, birthday, sex
                from patients
                where birthday is not null and birthday != ''
                  and substr(birthday, 1, 10) != '0000-00-00'
                  and substr(birthday, 6, 5) = substr(?, 6, 5)
                order by coalesce(display_name, '')
                limit 50
                """,
                (work_date,),
            )

            # 预约表按"约的号"算,过去的日子号可能已改期/取消,但人已经来看过病/交过钱——
            # 那天真到店按病历/账单算才准。只对过去的日子补这份"预约表没体现的到店"清单，
            # 避免动到今天队列按钮依赖的预约行为。
            unlinked_visits = []
            if work_date < today_str():
                covered_pids = {a["patient_identity"] for a in appointments if a.get("patient_identity")}
                visit_sources = """
                    select patient_identity from medical_records
                    where substr(coalesce(visit_time,''), 1, 10) = ?
                """
                visit_params = [work_date]
                if see_billing:
                    visit_sources += """
                        union
                        select patient_identity from bills
                        where substr(coalesce(bill_time,''), 1, 10) = ?
                    """
                    visit_params.append(work_date)
                visit_rows = conn.execute(
                    f"""
                    select distinct p.patient_identity, p.display_name
                    from patients p
                    where p.patient_identity in (
                        {visit_sources}
                    )
                    """,
                    tuple(visit_params),
                ).fetchall()
                unlinked_visits = [
                    _row_to_dict(r) for r in visit_rows if r["patient_identity"] not in covered_pids
                ]

        summary = {
            "appointments_today": appointments_count,
            "tomorrow_appointments": tomorrow_appointments_count,
            "birthdays_today": birthdays_today_count,
            "pending_return_visits": return_visits_count,
            "completed_return_visits": completed_return_visits_count,
            "visits_today": appointments_count,
            "first_visits_today": first_visits,
            "revisits_today": revisits,
            "new_visits_today": new_visits,
            "appts_waiting": appts_waiting,
            "appts_arrived": appts_arrived,
            "appts_done": appts_done,
        }
        if see_billing:
            summary.update({
                "today_receivable": today_billing["receivable"],
                "today_paid": today_billing["paid"],
                "today_cash_in": today_cash_in,
                "today_settlements": today_settlements_count,
                "unpaid_bills": unpaid_bills_count,
                "today_unpaid_amount": today_unpaid_amount,
            })
        if see_audit:
            summary["recent_audits"] = recent_audits_count

        result = {
            "date": work_date,
            "summary": summary,
            "appointments": appointments,
            "return_visits": return_visits,
            "tomorrow_appointments": tomorrow_appointments,
            "birthdays": birthdays,
            "unlinked_visits": unlinked_visits,
        }
        if see_billing:
            result.update({
                "unpaid_bills": unpaid_bills,
                "today_settlements": today_settlements,
                "today_pay_methods": today_pay_methods,
            })
        if see_audit:
            result["recent_audits"] = recent_audits
        return result

    return router
