"""本店财务报表(只读聚合)。Phase4 分层试点:聚合本体在 services/finance_service.py,
本文件只剩路由层三件事——验权限、收发参数(校验/默认/400)、拼 HTTP 响应(JSON/CSV)。"""
import io
import json
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response

from local_app.timeutil import bj_now, today_str
from local_app.auth import require_perm
from local_app.access_authorization import require_access
from local_app.db import connect, VOIDED_BILL_STATES, bill_net_arrears_sql, bill_discount_sql, active_bill_clause
from local_app.services.finance_service import (CASHIER_METHODS, build_cashier_rows,
                                                cashier_totals, handle_category_agg,
                                                income_payload, method_parts)
from local_app.validation import valid_date_param


def _valid_date(value, field):
    valid_date_param(value, field)


def create_reports_router(db_path):
    """本店财务报表（只读聚合）。口径：以 payments 实收为准。"""
    router = APIRouter()
    db_path = Path(db_path)

    def _income_norm(date_from, date_to):
        today = today_str()
        date_from = date_from.strip() or today
        date_to = date_to.strip() or today
        _valid_date(date_from, "date_from")
        _valid_date(date_to, "date_to")
        if date_from > date_to:
            raise HTTPException(status_code=400, detail="date_from 不能晚于 date_to")
        return date_from, date_to

    @router.get("/api/reports/income")
    def income_by_day(date_from: str = "", date_to: str = ""):
        """收入统计：按天汇总实收 + 日×付款方式透视 + 当日应收 + 欠款流水。"""
        require_access("report.finance.view")
        date_from, date_to = _income_norm(date_from, date_to)
        with connect(db_path) as conn:
            return income_payload(conn, date_from, date_to)

    @router.get("/api/reports/income.csv")
    def income_csv(date_from: str = "", date_to: str = ""):
        """导出收入统计 CSV(utf-8-sig BOM)：日期×付款方式 + 应收款项/实际收款/新增欠款/补交欠款 + 总合计。"""
        require_perm("data_export")
        import csv
        date_from, date_to = _income_norm(date_from, date_to)
        with connect(db_path) as conn:
            d = income_payload(conn, date_from, date_to)
        methods = d["methods"]
        head = ["日期"] + methods + ["应收款项", "实际收款", "新增欠款", "补交欠款"]
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow([f"期初欠款:{d['opening_arrears']}", f"期末欠款:{d['closing_arrears']}"])
        w.writerow(head)
        for row in d["days"]:
            w.writerow([row["date"]] + [row["methods"].get(m, "") for m in methods]
                       + [row.get("receivable", ""), row.get("amount", ""),
                          row.get("new_debt", ""), row.get("repay_debt", "")])
        w.writerow(["总合计"] + ["" for _ in methods]
                   + [d["total_receivable"], d["total_amount"], d["total_new_debt"], d["total_repay_debt"]])
        data = ("﻿" + buf.getvalue()).encode("utf-8")
        return Response(content=data, media_type="text/csv; charset=utf-8",
                        headers={"Content-Disposition": f'attachment; filename="income-{date_from}_{date_to}.csv"'})

    @router.get("/api/reports/arrears")
    def arrears_list(date_from: str = "", date_to: str = "", min_arrears: float = 0, q: str = ""):
        require_access("report.arrears.view")
        """欠费清单(全量未结单，非当日限制)：arrears=total_fee-paid_fee>0，排除 voided/refunded/已取消。
        可按 bill_time 区间、最低欠费额、患者姓名/电话/病历号筛选；按欠费额倒序。"""
        # 审查#9：用 round 比较,否则 total=100.00/paid=99.9999 浮点残差会冒"幻影欠费"
        # 排除作废：本地 voided/refunded/已取消/3 + SaaS 撤单码(900/500/400/200)，否则撤单单冒幽灵欠费
        # 欠费=总价-优惠(DiscountFee)-已收：SaaS 打折单不扣优惠会冒 ~60万幽灵欠费
        _void = ", ".join("'%s'" % s for s in (("refunded", "已取消", "3") + VOIDED_BILL_STATES))
        _net = bill_net_arrears_sql("b.")
        where = ["%s > 0" % _net,
                 "coalesce(b.state, '') not in (%s)" % _void]
        params = []
        df, dt_ = date_from.strip(), date_to.strip()
        if df:
            _valid_date(df, "date_from")
            where.append("substr(coalesce(b.bill_time, ''), 1, 10) >= ?"); params.append(df)
        if dt_:
            _valid_date(dt_, "date_to")
            where.append("substr(coalesce(b.bill_time, ''), 1, 10) <= ?"); params.append(dt_)
        kw = q.strip()
        if kw:
            where.append("(coalesce(p.display_name,'') like ? or coalesce(p.phone,'') like ? "
                         "or b.patient_identity like ?)")
            params += [f"%{kw}%", f"%{kw}%", f"%{kw}%"]
        if min_arrears and min_arrears > 0:
            where.append("%s >= ?" % _net); params.append(min_arrears)
        sql = (
            "select b.bill_id, b.bill_no, b.bill_time, b.total_fee, b.paid_fee, "
            "%s as arrears, b.patient_identity, " % _net +
            "coalesce(p.display_name, '') as display_name, coalesce(p.phone, '') as phone "
            "from bills b left join patients p on p.patient_identity = b.patient_identity "
            "where " + " and ".join(where) + " "
            "order by arrears desc, b.bill_time desc"
        )
        with connect(db_path) as conn:
            rows = [dict(r) for r in conn.execute(sql, params).fetchall()]
        return {"rows": rows, "count": len(rows),
                "total_arrears": round(sum(r["arrears"] for r in rows), 2),
                "date_from": df, "date_to": dt_}

    @router.get("/api/reports/income-monthly")
    def income_monthly(date_from: str = "", date_to: str = ""):
        require_access("report.finance.view")
        """#657 月度汇总：按月净实收(口径同 income：排作废原单、保留退费负数冲减)+应收(开单净额)。
        默认近 12 个月。"""
        today = bj_now().date()
        date_to = date_to.strip() or today.isoformat()
        if not date_from.strip():
            y, m = today.year, today.month - 11
            if m <= 0:
                y, m = y - 1, m + 12
            date_from = f"{y:04d}-{m:02d}-01"
        _valid_date(date_from, "date_from")
        _valid_date(date_to, "date_to")
        if date_from > date_to:
            raise HTTPException(status_code=400, detail="date_from 不能晚于 date_to")
        with connect(db_path) as conn:
            prows = conn.execute(
                """
                select substr(pay_time, 1, 7) as month, count(*) as cnt,
                       round(sum(coalesce(amount, 0)), 2) as amount
                from payments
                where substr(coalesce(pay_time, ''), 1, 10) between ? and ?
                  and (coalesce(json_extract(iif(json_valid(source_json), source_json, '{}'), '$.CancelMark'), '') = ''
                       or coalesce(amount, 0) < 0)
                group by month order by month
                """,
                (date_from, date_to),
            ).fetchall()
            brows = conn.execute(
                f"""
                select substr(bill_time, 1, 7) as month,
                       round(coalesce(sum(total_fee - {bill_discount_sql()}), 0), 2) as receivable
                from bills
                where substr(coalesce(bill_time, ''), 1, 10) between ? and ? and {active_bill_clause()}
                group by month
                """,
                (date_from, date_to),
            ).fetchall()
        mmap = {}
        for r in prows:
            if r["month"]:
                mmap[r["month"]] = {"month": r["month"], "count": r["cnt"],
                                    "amount": r["amount"], "receivable": 0.0}
        for r in brows:
            if not r["month"]:
                continue
            m = mmap.setdefault(r["month"], {"month": r["month"], "count": 0, "amount": 0.0, "receivable": 0.0})
            m["receivable"] = r["receivable"]
        months = [mmap[k] for k in sorted(mmap)]
        return {"date_from": date_from, "date_to": date_to, "months": months,
                "total_amount": round(sum(m["amount"] for m in months), 2),
                "total_count": sum(m["count"] for m in months),
                "total_receivable": round(sum(m["receivable"] for m in months), 2)}

    @router.get("/api/reports/today-collection")
    def today_collection(date: str = ""):
        require_access("report.cashier.view")
        """今日营收日报：当天实收(按收费方式分组)+ 待收(pending/partial 收费单未收合计)。
        收费方式优先取 payments.pay_type 列(本地+同步),回退 source_json.pay_method/refund_method。"""
        import json as _json
        day = date.strip() or today_str()
        _valid_date(day, "date")
        with connect(db_path) as conn:
            # #38/#1：作废原单(CancelMark非空且金额≥0)不计实收，保留退费负数作冲减，口径与 income 一致
            pay_rows = conn.execute(
                "select amount, pay_type, source_json from payments "
                "where substr(coalesce(pay_time,''),1,10) = ? "
                "and (coalesce(json_extract(iif(json_valid(source_json), source_json, '{}'),'$.CancelMark'),'') = '' or coalesce(amount,0) < 0)",
                (day,),
            ).fetchall()
            # #39/#574：待收按当天账单(bill_time)过滤;口径与工作台/收费管理未结列表统一用
            # 净欠费>0+排除作废(SaaS 同步单 state 是数字码,靠 state in ('pending','partial') 会漏)
            pend = conn.execute(
                f"select count(*) c, coalesce(sum({bill_net_arrears_sql()}),0) s "
                f"from bills where {active_bill_clause()} and {bill_net_arrears_sql()} > 0 "
                "and substr(coalesce(bill_time,''),1,10) = ?",
                (day,),
            ).fetchone()
        total = 0.0
        count = 0
        by_method = {}
        for r in pay_rows:
            amt = r["amount"] or 0
            total += amt
            count += 1
            # #559：多方式收款按 PaidDetail 拆到真实方式(和工作台/收入统计一致)
            try:
                _detail = (_json.loads(r["source_json"] or "{}") or {}).get("PaidDetail") or ""
            except (ValueError, TypeError):
                _detail = ""
            parts = method_parts(amt, _detail)
            if parts:
                for m, a in parts:
                    by_method[m] = round(by_method.get(m, 0) + a, 2)
                continue
            # #211：优先 pay_type 列(本地+同步收款的独立付款方式),回退 source_json.pay_method/refund_method
            method = (r["pay_type"] or "").strip()
            if not method:
                try:
                    j = _json.loads(r["source_json"] or "{}") or {}
                    method = j.get("pay_method") or j.get("refund_method") or ""   # 审查#11/#27:兼容历史退费 refund_method 键
                except (ValueError, TypeError):
                    method = ""
            method = method or "未填"
            by_method[method] = round(by_method.get(method, 0) + amt, 2)
        methods = [{"method": k, "amount": v} for k, v in sorted(by_method.items(), key=lambda x: -x[1])]
        return {
            "date": day,
            "collected": round(total, 2),
            "count": count,
            "by_method": methods,
            "pending_count": pend["c"],
            "pending_total": round(pend["s"], 2),
        }

    @router.get("/api/reports/cashier")
    def cashier_query(date_from: str = "", date_to: str = ""):
        require_access("report.cashier.view")
        """收银查询：起止日期内的收款流水明细（含患者名）+ 按患者聚合 + 实收合计。
        复审#4：支持日期区间 + 按患者聚合。
        收银员筛选待 t_pay 操作员字段同步后再加（本地无该列）。"""
        today = today_str()
        date_from = date_from.strip() or today
        date_to = date_to.strip() or today
        _valid_date(date_from, "date_from")
        _valid_date(date_to, "date_to")
        if date_from > date_to:
            raise HTTPException(status_code=400, detail="date_from 不能晚于 date_to")

        with connect(db_path) as conn:
            rows = conn.execute(
                """
                select pay.payment_id, pay.patient_identity, pay.pay_time, pay.amount,
                       pay.state, pay.bill_id, p.display_name,
                       json_extract(iif(json_valid(pay.source_json), pay.source_json, '{}'), '$.CancelMark') as cancel_mark
                from payments pay
                left join patients p on p.patient_identity = pay.patient_identity
                where substr(coalesce(pay.pay_time, ''), 1, 10) between ? and ?
                order by pay.pay_time, pay.payment_id
                """,
                (date_from, date_to),
            ).fetchall()
        records = [
            {
                "payment_id": r["payment_id"],
                "patient_identity": r["patient_identity"],
                "pay_time": r["pay_time"],
                "amount": r["amount"],
                "state": r["state"],
                "bill_id": r["bill_id"],
                "display_name": r["display_name"],
                "cancel_mark": r["cancel_mark"] or "",
            }
            for r in rows
        ]
        # #1：按患者聚合与合计剔除「作废原单」(CancelMark非空且金额≥0)，退费负数保留作冲减；明细全保留可见
        def _is_void_original(r):
            return str(r.get("cancel_mark") or "") != "" and (r["amount"] or 0) >= 0
        valid = [r for r in records if not _is_void_original(r)]
        by_patient = {}
        for r in valid:
            key = r["patient_identity"]
            agg = by_patient.get(key)
            if agg is None:
                agg = {
                    "patient_identity": key,
                    "display_name": r["display_name"],
                    "amount": 0,
                    "count": 0,
                }
                by_patient[key] = agg
            agg["amount"] += r["amount"] or 0
            agg["count"] += 1
        return {
            "date_from": date_from,
            "date_to": date_to,
            "records": records,
            # 审查#28：合计/分组金额 round,避免浮点残差泄漏到接口
            "by_patient": sorted(
                ({**a, "amount": round(a["amount"], 2)} for a in by_patient.values()),
                key=lambda a: a["amount"], reverse=True
            ),
            "total_amount": round(sum((r["amount"] or 0) for r in valid), 2),
            "count": len(records),
            "voided_count": len(records) - len(valid),
        }

    def _cashier_query_window(date_from, date_to):
        today = today_str()
        df = date_from.strip() or today
        dt_ = date_to.strip() or today
        _valid_date(df, "date_from")
        _valid_date(dt_, "date_to")
        if df > dt_:
            raise HTTPException(status_code=400, detail="date_from 不能晚于 date_to")
        return df, dt_

    @router.get("/api/reports/cashier-query")
    def cashier_query_detail(date_from: str = "", date_to: str = "", doctor: str = "", source: str = "", method: str = "", operator: str = ""):
        require_access("report.cashier.view")
        """收银查询：逐笔收款 + 付款方式 + 处置类别分摊，按日期/医生/来源/方式/收银员筛。"""
        df, dt_ = _cashier_query_window(date_from, date_to)
        with connect(db_path) as conn:
            rows = build_cashier_rows(conn, df, dt_, doctor.strip(), source.strip(), method.strip(), operator.strip())
        by_method, by_cat = cashier_totals(rows)
        return {
            "date_from": df, "date_to": dt_, "rows": rows,
            "total_amount": round(sum(r["amount"] for r in rows), 2), "count": len(rows),
            "by_method": by_method, "by_category": by_cat,
            # 筛选下拉=固定列 + 数据里真出现过的方式：诊所自定义的方式(第三方支付/会员卡等)
            # 不在固定列里，只给固定列会导致「报表里看得到这笔、却没法按它筛」(同 CSV 列的口径)
            "methods": CASHIER_METHODS + [m for m in by_method if m not in CASHIER_METHODS],
            "categories": sorted(by_cat.keys()),
        }

    @router.get("/api/reports/cashier-query.csv")
    def cashier_query_csv(date_from: str = "", date_to: str = "", doctor: str = "", source: str = "", method: str = "", operator: str = ""):
        """导出收银查询为 CSV(utf-8-sig BOM,Excel 直接打开,无第三方依赖)。"""
        require_perm("data_export")   # P1-13：全量导出仅 admin
        import csv
        df, dt_ = _cashier_query_window(date_from, date_to)
        with connect(db_path) as conn:
            rows = build_cashier_rows(conn, df, dt_, doctor.strip(), source.strip(), method.strip(), operator.strip())
        by_method, by_cat = cashier_totals(rows)
        cat_cols = sorted(by_cat.keys())
        # 付款方式列=固定列 + 结果里出现但不在固定列的方式(会员卡/储值卡等),否则总收入≠方式列合计
        method_cols = CASHIER_METHODS + [m for m in by_method if m not in CASHIER_METHODS]
        head = (["收款时间", "病历号", "患者姓名", "联系电话", "患者来源", "二级来源", "三级来源", "医生", "收款员", "总收入"]
                + method_cols + cat_cols + ["发票号", "备注"])
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(head)
        for r in rows:
            w.writerow([r["pay_time"], r["chart_no"], r["display_name"], r["phone"], r["referral_source"],
                        r["referral_source2"], r["referral_source3"], r["doctor"], r["operator"], r["amount"]]
                       + [r["amount"] if r["pay_type"] == m else "" for m in method_cols]
                       + [r["categories"].get(c, "") for c in cat_cols]
                       + [r["invoice_no"], r["remark"]])
        w.writerow(["总合计", "", "", "", "", "", "", "", "", round(sum(r["amount"] for r in rows), 2)]
                   + [by_method.get(m, "") for m in method_cols]
                   + [by_cat.get(c, "") for c in cat_cols] + ["", ""])
        data = ("﻿" + buf.getvalue()).encode("utf-8")
        return Response(content=data, media_type="text/csv; charset=utf-8",
                        headers={"Content-Disposition": f'attachment; filename="cashier-query-{df}_{dt_}.csv"'})

    @router.get("/api/reports/handle-category")
    def handle_category(date_from: str = "", date_to: str = "", doctor: str = "", source: str = ""):
        require_access("report.operations.view")
        """处置大类/大类汇总：收银查询同一批数据按 fee_type(处置类别)分组,每类 实收(分摊)+笔数。
        与收银查询的处置类别透视同源,合计一致。"""
        df, dt_ = _cashier_query_window(date_from, date_to)
        with connect(db_path) as conn:
            items, total, pay_count = handle_category_agg(conn, df, dt_, doctor.strip(), source.strip())
        return {"date_from": df, "date_to": dt_, "categories": items,
                "total_amount": total, "pay_count": pay_count}

    @router.get("/api/reports/handle-category.csv")
    def handle_category_csv(date_from: str = "", date_to: str = "", doctor: str = "", source: str = ""):
        require_perm("data_export")   # P1-13：全量导出仅 admin
        import csv
        df, dt_ = _cashier_query_window(date_from, date_to)
        with connect(db_path) as conn:
            items, total, pay_count = handle_category_agg(conn, df, dt_, doctor.strip(), source.strip())
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["处置类别", "实收金额", "笔数"])
        for c in items:
            w.writerow([c["fee_type"], c["amount"], c["count"]])
        w.writerow(["总合计", total, pay_count])   # 总笔数=实际收款笔数(非各类别计数和,避免多类别重复计)
        data = ("﻿" + buf.getvalue()).encode("utf-8")
        return Response(content=data, media_type="text/csv; charset=utf-8",
                        headers={"Content-Disposition": f'attachment; filename="handle-category-{df}_{dt_}.csv"'})

    @router.get("/api/reports/reconcile")
    def reconcile_report(date_from: str = "", date_to: str = ""):
        require_access("report.operations.view")
        """双跑九维对账：读 reconcile_log,逐天本地vs SaaS(预约/患者/账单/欠费/收款/退费/治疗项目/病历/影像)
        差异 + 差异 id 明细,差异≠0 标红(ok 由 diff_json._flags 决定,与 run_reconcile 同口径)。"""
        df, dt_ = _cashier_query_window(date_from, date_to)
        with connect(db_path) as conn:
            rows = conn.execute(
                "select * from reconcile_log where recon_date between ? and ? order by recon_date desc",
                (df, dt_)).fetchall()

        def _count(r, k):
            return {"local": r[f"local_{k}"], "saas": r[f"saas_{k}"], "diff": r[f"local_{k}"] - r[f"saas_{k}"]}

        def _amt(r, k):
            return {"local": round(r[f"local_{k}"], 2), "saas": round(r[f"saas_{k}"], 2),
                    "diff": round(r[f"local_{k}"] - r[f"saas_{k}"], 2)}

        out = []
        for r in rows:
            keys = r.keys()
            try:
                details = json.loads(r["diff_json"] or "{}") if "diff_json" in keys else {}
            except (ValueError, TypeError):
                details = {}
            flags = details.get("_flags", [])
            ok = not flags
            out.append({
                "date": r["recon_date"], "run_at": r["run_at"], "ok": ok, "flags": flags,
                "appts": _count(r, "appts"),
                "patients": _count(r, "patients"),
                "income": _amt(r, "income"),
                "bills": _count(r, "bills") if "local_bills" in keys else None,
                "arrears": _amt(r, "arrears") if "local_arrears" in keys else None,
                "refunds": _amt(r, "refunds") if "local_refunds" in keys else None,
                "items": _count(r, "items") if "local_items" in keys else None,
                "records": _count(r, "records") if "local_records" in keys else None,
                "images": ({"local": r["local_images"], "done": r["local_images_done"],
                            "saas": r["saas_images"]} if "local_images" in keys else None),
                "details": {k: v for k, v in details.items() if k != "_flags"},
            })
        return {"date_from": df, "date_to": dt_, "rows": out, "diff_days": sum(1 for x in out if not x["ok"])}

    @router.get("/api/reports/staff-workload")
    def staff_workload(date_from: str = "", date_to: str = ""):
        require_access("report.performance.view")
        """#6 配台统计：起止日期内按配台人员(医生/护士/咨询师/助理)汇总
        配台处置单数、处置项数、金额，用于工作量/绩效/提成。
        口径(#56)：只统计**已划价**(status='priced')的处置单——未划价单 total_fee 仅为录入原价、
        会高估绩效金额，故排除；已撤销(voided)同样排除。金额取处置明细 total_fee 之和(划价后最终值)。"""
        today = today_str()
        date_from = date_from.strip() or today
        date_to = date_to.strip() or today
        _valid_date(date_from, "date_from")
        _valid_date(date_to, "date_to")
        if date_from > date_to:
            raise HTTPException(status_code=400, detail="date_from 不能晚于 date_to")

        with connect(db_path) as conn:
            rows = conn.execute(
                """
                select o.order_id, o.doctor_name, o.nurse_name, o.consultant_name, o.assistant_name,
                       count(ti.treatment_item_id) as item_count,
                       coalesce(sum(ti.total_fee), 0) as amount
                from treatment_orders o
                left join treatment_items ti
                       on ti.order_id = o.order_id and coalesce(ti.is_deleted, 0) = 0
                where o.status = 'priced'  -- #56：只算已划价单，未划价(recorded)金额未定不计入绩效
                  and substr(coalesce(o.order_date, o.created_at, ''), 1, 10) between ? and ?
                group by o.order_id
                """,
                (date_from, date_to),
            ).fetchall()

        roles = [("doctor_name", "医生"), ("nurse_name", "护士"),
                 ("consultant_name", "咨询师"), ("assistant_name", "助理")]
        agg = {}
        for r in rows:
            for col, role in roles:
                name = str(r[col] or "").strip()
                if not name:
                    continue
                key = (role, name)
                a = agg.get(key)
                if a is None:
                    a = {"role": role, "name": name, "order_count": 0, "item_count": 0, "amount": 0.0}
                    agg[key] = a
                a["order_count"] += 1
                a["item_count"] += r["item_count"] or 0
                a["amount"] += r["amount"] or 0
        staff = sorted(agg.values(), key=lambda x: (-x["amount"], x["role"], x["name"]))
        for a in staff:
            a["amount"] = round(a["amount"], 2)
        return {"date_from": date_from, "date_to": date_to, "staff": staff}

    return router
