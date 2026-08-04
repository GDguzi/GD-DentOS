"""今日全量工作检查器(呈现层,只读)——逐条列出今日所有预约/处置/账单/收款/回访,
自动比对交叉校验：有预约没到诊、有处置没病历、有账单没收款、金额不一致等。
每条标 ✅/⚠️/❌，供前台逐条点击核验。"""
from pathlib import Path

from fastapi import APIRouter

from local_app.access_authorization import has_access, require_access
from local_app.timeutil import today_str
from local_app.db import (active_bill_clause, bill_discount_sql,
                          bill_net_arrears_sql, connect)
from local_app.rv_query import DONE_COND
from local_app.snapshots import row_to_dict, rows as _rows
from local_app.validation import valid_date_param


def create_today_inspection_router(db_path):
    router = APIRouter()
    db_path = Path(db_path)

    @router.get("/api/today-inspection")
    def today_inspection(date: str = ""):
        """全量检查今日（或指定日期）所有工作项，逐条标核验状态。"""
        require_access("patient.profile.view")
        see_billing = has_access("billing.view")
        see_audit = has_access("audit.view")
        work_date = date.strip() or today_str()
        valid_date_param(work_date, "date")

        with connect(db_path) as conn:
            not_cancelled = (
                "coalesce(status, '') not in ('3', '已取消', '已爽约', '爽约') "
                "and coalesce(suspect_cancelled, 0) = 0"
            )

            # ─── 1) 今日预约列表(含检查状态) ───
            appointments = _rows(conn, f"""
                select
                    a.appointment_id, a.patient_identity,
                    p.display_name, p.phone, p.sex,
                    a.start_time, a.doctor_name, a.item_name,
                    a.status, a.room, a.visit_type
                from appointments a
                left join patients p on p.patient_identity = a.patient_identity
                where substr(coalesce(a.start_time, ''), 1, 10) = ?
                  and {not_cancelled}
                order by coalesce(a.start_time, ''), a.appointment_id
                limit 300
            """, (work_date,))

            # ─── 2) 今日处置单 ───
            billing_order_columns = ", o.status, o.bill_id" if see_billing else ""
            item_summary_expr = (
                "ti.item_name || '(' || ti.total_fee || '元)'"
                if see_billing else "ti.item_name"
            )
            orders = _rows(conn, f"""
                select
                    o.order_id, o.order_no, o.patient_identity,
                    p.display_name, p.phone,
                    o.order_date, o.doctor_name, o.diagnosis{billing_order_columns},
                    coalesce((select group_concat({item_summary_expr})
                     from treatment_items ti
                     where ti.order_id = o.order_id
                       and coalesce(ti.is_deleted,0)=0
                       and coalesce(ti.is_refund,0)=0), '') as item_summary
                from treatment_orders o
                left join patients p on p.patient_identity = o.patient_identity
                where substr(coalesce(o.order_date, ''), 1, 10) = ?
                  and coalesce(o.status, '') != 'voided'
                order by coalesce(o.order_date, ''), o.order_id
                limit 300
            """, (work_date,))

            order_items = {}
            if see_billing:
                # 处置项目金额：财务展示与金额一致性检查共用原口径。
                for o in orders:
                    oid = o["order_id"]
                    items = conn.execute("""
                        select ti.item_name, ti.total_fee
                        from treatment_items ti
                        where ti.order_id = ?
                          and coalesce(ti.is_deleted,0)=0
                          and coalesce(ti.is_refund,0)=0
                    """, (oid,)).fetchall()
                    order_items[oid] = [
                        {"item_name": r["item_name"], "total_fee": r["total_fee"]}
                        for r in items
                    ]

            # ─── 3) 今日账单 ───
            bills = []
            if see_billing:
                bills = _rows(conn, f"""
                    select
                        b.bill_id, b.patient_identity,
                        p.display_name, p.phone,
                        b.bill_no, b.bill_time, b.total_fee, b.paid_fee,
                        round(b.total_fee - {bill_discount_sql('b.')}, 2) as net_receivable,
                        {bill_net_arrears_sql('b.')} as unpaid_fee,
                        b.state,
                        (select group_concat(distinct ti.item_name)
                         from treatment_items ti
                         where ti.bill_id = b.bill_id
                           and coalesce(ti.is_deleted,0)=0
                           and coalesce(ti.is_refund,0)=0) as items
                    from bills b
                    left join patients p on p.patient_identity = b.patient_identity
                    where substr(coalesce(b.bill_time, ''), 1, 10) = ?
                      and {active_bill_clause('b.state')}
                    order by coalesce(b.bill_time, ''), b.bill_id
                    limit 300
                """, (work_date,))

            # ─── 4) 今日收款 ───
            payments = []
            if see_billing:
                pay_active = (
                    "(coalesce(json_extract(iif(json_valid(p.source_json), p.source_json, '{}'), "
                    "'$.CancelMark'), '') = '' or coalesce(p.amount, 0) < 0)"
                )
                payments = _rows(conn, f"""
                    select
                        p.payment_id, p.patient_identity, p.bill_id,
                        p.pay_time, p.amount, p.pay_type, p.payment_record_type
                    from payments p
                    where substr(coalesce(p.pay_time, ''), 1, 10) = ?
                      and {pay_active}
                    order by coalesce(p.pay_time, ''), p.payment_id
                    limit 300
                """, (work_date,))

            # ─── 5) 今日病历 ───
            records = _rows(conn, """
                select
                    mr.record_id, mr.patient_identity,
                    p.display_name,
                    mr.visit_time, mr.doctor_name, mr.record_type,
                    mr.draft_status
                from medical_records mr
                left join patients p on p.patient_identity = mr.patient_identity
                where substr(coalesce(mr.visit_time, ''), 1, 10) = ?
                order by coalesce(mr.visit_time, ''), mr.record_id
                limit 300
            """, (work_date,))

            # ─── 6) 今日回访(到期) ───
            # 完成态复用全站唯一口径 DONE_COND(含旧导入数据以 item_name/note "已回访"前缀表完成),
            # 只看显式状态会把这些老记录误标 warn 拉低健康度
            return_visits_due = _rows(conn, f"""
                select
                    r.return_visit_id, r.patient_identity,
                    p.display_name, p.phone,
                    r.due_time, r.item_name, r.status, r.note,
                    r.actual_date,
                    case when ({DONE_COND}) then 1 else 0 end as is_done
                from return_visits r
                left join patients p on p.patient_identity = r.patient_identity
                where coalesce(r.is_deleted, 0) = 0
                  and substr(coalesce(r.due_time, ''), 1, 10) = ?
                  and substr(coalesce(r.due_time, ''), 1, 10) != '0000-00-00'
                order by coalesce(r.due_time, ''), r.return_visit_id
                limit 100
            """, (work_date,))

            # ─── 无预约到店（有处置/病历，或有财务权限时有账单） ───
            treated_pids = {o["patient_identity"] for o in orders}
            recorded_pids = {r["patient_identity"] for r in records}
            billed_pids = {b["patient_identity"] for b in bills}
            appt_pids = {
                a["patient_identity"] for a in appointments
                if a.get("patient_identity")
            }
            walkin_pids = treated_pids | recorded_pids
            if see_billing:
                walkin_pids |= billed_pids
            unlinked_pids = walkin_pids - appt_pids
            walkins = []
            if unlinked_pids:
                walkins = _rows(conn, f"""
                    select patient_identity, display_name, phone
                    from patients
                    where patient_identity in ({','.join('?' * len(unlinked_pids))})
                """, tuple(unlinked_pids))

        # ─── 构建交叉检查数据 ───
        # 快速索引
        treated_pids = set()
        billed_pids = set()
        paid_pids = set()
        recorded_pids = set()
        order_by_pid = {}
        bill_by_pid = {}
        payment_by_bill = {}
        bill_amount = {}
        paid_amount_by_bill = {}

        for o in orders:
            pid = o["patient_identity"]
            treated_pids.add(pid)
            if pid not in order_by_pid:
                order_by_pid[pid] = []
            order_by_pid[pid].append(o)

        bill_unpaid = {}
        for b in bills:
            pid = b["patient_identity"]
            billed_pids.add(pid)
            if pid not in bill_by_pid:
                bill_by_pid[pid] = []
            bill_by_pid[pid].append(b)
            # #688:金额一致性用净应收(总费用−优惠 DiscountFee),否则打折已结清单被误报金额异常/待收款
            bill_amount[b["bill_id"]] = b["net_receivable"] or 0
            bill_unpaid[b["bill_id"]] = b["unpaid_fee"] or 0

        for pay in payments:
            pid = pay["patient_identity"]
            paid_pids.add(pid)
            bid = pay["bill_id"]
            if bid:
                if bid not in payment_by_bill:
                    payment_by_bill[bid] = []
                payment_by_bill[bid].append(pay)
                if bid not in paid_amount_by_bill:
                    paid_amount_by_bill[bid] = 0
                paid_amount_by_bill[bid] += pay["amount"] or 0

        for r in records:
            recorded_pids.add(r["patient_identity"])

        # ─── 逐条预约检查 ───
        appointment_items = []
        for a in appointments:
            pid = a["patient_identity"] or ""
            item = {
                "type": "appointment",
                "appointment_id": a["appointment_id"],
                "patient_identity": pid,
                "display_name": a["display_name"],
                "phone": a["phone"],
                "start_time": a["start_time"],
                "doctor_name": a["doctor_name"],
                "item_name": a["item_name"],
                "status": a["status"],
                "room": a["room"],
                "visit_type": a["visit_type"],
            }
            if see_audit:
                stage = str(a.get("status", "") or "")
                is_arrived = stage in (
                    "1", "已到诊", "已到达", "预约到达",
                    "2", "完成", "已完成", "完成治疗",
                    "已分诊", "已离开", "患者离开",
                )
                checks = [{
                    "check": "到诊",
                    "status": "ok" if is_arrived else "warn",
                    "detail": "已到诊" if is_arrived else "未到诊",
                }]
                if is_arrived:
                    if pid in treated_pids:
                        checks.append({"check": "处置", "status": "ok", "detail": "有处置单"})
                    else:
                        checks.append({"check": "处置", "status": "missing", "detail": "无处置单"})
                    if pid in recorded_pids:
                        checks.append({"check": "病历", "status": "ok", "detail": "有病历"})
                    else:
                        checks.append({"check": "病历", "status": "warn", "detail": "无病历"})

                    if see_billing:
                        if pid in billed_pids:
                            checks.append({"check": "账单", "status": "ok", "detail": "有账单"})
                        else:
                            checks.append({"check": "账单", "status": "warn", "detail": "无账单"})
                        if pid in paid_pids:
                            checks.append({"check": "收款", "status": "ok", "detail": "已收款"})
                        else:
                            checks.append({"check": "收款", "status": "warn", "detail": "待收款"})

                        amount_issues = []
                        for order in order_by_pid.get(pid, []):
                            bid = order.get("bill_id") or ""
                            if bid and bid in bill_amount:
                                item_total = sum(
                                    ti["total_fee"] or 0
                                    for ti in order_items.get(order["order_id"], [])
                                )
                                bill_total = bill_amount[bid]
                                if abs(item_total - bill_total) > 0.02:
                                    amount_issues.append(
                                        f"处置{order['order_no']}项目合计{item_total}≠账单{bid[-6:]}金额{bill_total}"
                                    )
                            if bid and bid in paid_amount_by_bill:
                                paid_sum = paid_amount_by_bill[bid]
                                bill_val = bill_amount.get(bid, 0)
                                if abs(paid_sum - bill_val) > 0.02:
                                    amount_issues.append(
                                        f"账单{bid[-6:]}应收{bill_val}≠实收{paid_sum}"
                                    )
                        if amount_issues:
                            checks.append({"check": "金额", "status": "error",
                                           "detail": "; ".join(amount_issues)})
                        else:
                            checks.append({"check": "金额", "status": "ok", "detail": "一致"})

                statuses = {check["status"] for check in checks}
                if "error" in statuses:
                    overall = "error"
                elif "missing" in statuses:
                    overall = "missing"
                elif "warn" in statuses:
                    overall = "warn"
                else:
                    overall = "ok"
                item.update({"overall": overall, "checks": checks})
            appointment_items.append(item)

        # ─── 逐条处置单检查 ───
        order_items_list = []
        for o in orders:
            pid = o["patient_identity"] or ""
            item = {
                "type": "order",
                "order_id": o["order_id"],
                "order_no": o["order_no"],
                "patient_identity": pid,
                "display_name": o["display_name"],
                "phone": o["phone"],
                "order_date": o["order_date"],
                "doctor_name": o["doctor_name"],
                "diagnosis": o["diagnosis"],
                "item_summary": o["item_summary"] or "",
            }
            bid = ""
            item_total = 0
            if see_billing:
                bid = o.get("bill_id") or ""
                items_data = order_items.get(o["order_id"], [])
                item_total = round(
                    sum(ti["total_fee"] or 0 for ti in items_data), 2
                )
                # GD-05:收费展示用账单+实收派生,不信可能滞后的处置状态列。
                # 无账单→待划价/待收费;有账单未结清→待收费;已结清→已收费;已撤销照旧。
                status = str(o["status"] or "")
                if status == "voided":
                    pay_state = "voided"
                elif not bid:
                    pay_state = "recorded" if status == "recorded" else "priced"
                elif bid in bill_unpaid:
                    pay_state = "paid" if bill_unpaid[bid] <= 0.005 else "priced"
                else:
                    pay_state = status   # 账单不在今日列表(跨日结算等),回退原状态
                item.update({
                    "status": o["status"],
                    "bill_id": bid,
                    "item_total": item_total,
                    "pay_state": pay_state,
                })

            if see_audit:
                checks = [{
                    "check": "病历",
                    "status": "ok" if pid in recorded_pids else "warn",
                }]
                if see_billing:
                    if bid:
                        checks.append({"check": "账单", "status": "ok"})
                        if bid in bill_amount:
                            if abs(item_total - bill_amount[bid]) < 0.02:
                                checks.append({"check": "金额", "status": "ok"})
                            else:
                                checks.append({"check": "金额", "status": "error",
                                               "detail": f"项目{item_total}≠账单{bill_amount[bid]}"})
                        if bid in paid_amount_by_bill:
                            paid = paid_amount_by_bill[bid]
                            if abs(paid - bill_amount.get(bid, 0)) < 0.02:
                                checks.append({"check": "收款", "status": "ok"})
                            else:
                                checks.append({"check": "收款", "status": "warn",
                                               "detail": f"应收{bill_amount[bid]}实收{paid}"})
                        else:
                            checks.append({"check": "收款", "status": "missing"})
                    else:
                        checks.append({"check": "账单", "status": "missing"})

                statuses = {check["status"] for check in checks}
                if "error" in statuses:
                    overall = "error"
                elif "missing" in statuses:
                    overall = "missing"
                elif "warn" in statuses:
                    overall = "warn"
                else:
                    overall = "ok"
                item.update({"overall": overall, "checks": checks})
            order_items_list.append(item)

        # ─── 逐条账单检查 ───
        bill_items_list = []
        for b in bills:
            bid = b["bill_id"]
            pid = b["patient_identity"] or ""
            unpaid = b["unpaid_fee"] or 0
            item = {
                "type": "bill",
                "bill_id": bid,
                "patient_identity": pid,
                "display_name": b["display_name"],
                "phone": b["phone"],
                "bill_no": b["bill_no"],
                "bill_time": b["bill_time"],
                "total_fee": b["total_fee"],
                "paid_fee": b["paid_fee"],
                "unpaid_fee": unpaid,
                "state": b["state"],
                "items": b["items"] or "",
            }
            if see_audit:
                checks = []
                if unpaid > 0.005:
                    checks.append({"check": "收款", "status": "warn",
                                   "detail": f"欠费{unpaid}元"})
                else:
                    checks.append({"check": "收款", "status": "ok"})
                item.update({
                    "overall": "warn" if unpaid > 0.005 else "ok",
                    "checks": checks,
                })
            bill_items_list.append(item)

        # ─── 逐条回访检查 ───
        rv_items = []
        for rv in return_visits_due:
            item = {
                "type": "return_visit",
                "return_visit_id": rv["return_visit_id"],
                "patient_identity": rv["patient_identity"],
                "display_name": rv["display_name"],
                "phone": rv["phone"],
                "due_time": rv["due_time"],
                "item_name": rv["item_name"],
                "status": rv["status"],
                "note": rv["note"],
                "actual_date": rv.get("actual_date", ""),
            }
            if see_audit:
                item["overall"] = "ok" if rv.get("is_done") else "warn"
            rv_items.append(item)

        response = {
            "date": work_date,
            "appointments": appointment_items,
            "orders": order_items_list,
            "return_visits": rv_items,
            "walkins": walkins,
        }
        if see_billing:
            response.update({
                "bills": bill_items_list,
                "payments": [row_to_dict(p) for p in payments],
            })
        if see_audit:
            # GD-05:无预约到店=需要核对的 warn,进状态汇总;分母覆盖全部可见条目
            for w in walkins:
                w["overall"] = "warn"
            all_items = appointment_items + order_items_list + rv_items + walkins
            if see_billing:
                all_items += bill_items_list
            summary = {
                "appointments": len(appointment_items),
                "orders": len(order_items_list),
                "return_visits_due": len(rv_items),
                "walkins": len(walkins),
                "total": len(all_items),
                "ok": sum(1 for item in all_items if item["overall"] == "ok"),
                "warn": sum(1 for item in all_items if item["overall"] == "warn"),
                "missing": sum(
                    1 for item in all_items if item["overall"] == "missing"
                ),
                "error": sum(
                    1 for item in all_items if item["overall"] == "error"
                ),
            }
            if see_billing:
                summary["bills"] = len(bill_items_list)
            response["summary"] = summary
        return response

    return router
