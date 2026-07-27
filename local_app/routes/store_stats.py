"""店内统计——只读聚合，不反写业务表。

Phase4 分层试点：聚合本体已搬 services/finance_service.py（口径要点见该文件与
git 历史），本文件只剩路由层三件事——验权限、收发参数（校验/默认值/400）、拼 HTTP 响应。
"""
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response

from local_app.auth import require_perm
from local_app.access_authorization import require_access
from local_app.db import connect
from local_app.services.finance_service import (clinic_log_rows, config_role_perf,
                                                daily_settlement_rows, valid_payment_clause,
                                                visit_perf)
from local_app.timeutil import bj_now, today_str
from local_app.validation import valid_date_param


def _norm_range(start, end):
    start = (start or "").strip() or today_str()
    end = (end or "").strip() or today_str()
    valid_date_param(start, "start")
    valid_date_param(end, "end")
    if start > end:
        raise HTTPException(status_code=400, detail="start 不能晚于 end")
    return start, end


def create_store_stats_router(db_path):
    router = APIRouter()
    db_path = Path(db_path)

    @router.get("/api/store-stats/role-perf")
    def role_perf(start: str = "", end: str = "", role: str = "doctor", staff: str = ""):
        """医生/护士/助理/咨询师收费统计：初诊/复诊 × 量/成交人数/实收/应收。"""
        require_access("report.performance.view")
        if role not in ("doctor", "nurse", "consultant", "assistant"):
            raise HTTPException(status_code=400, detail="role 取值须为 doctor/nurse/consultant/assistant")
        start, end = _norm_range(start, end)
        with connect(db_path) as conn:
            if role in ("doctor", "nurse"):   # 病历为轴(nurse 取 content_json.Nurse)
                rows, unclassified = visit_perf(conn, start, end, staff.strip(), role)
            else:                              # 助理/咨询师:本地无配台人名,处置单为轴(几乎空,如实)
                rows, unclassified = config_role_perf(conn, start, end, role, staff.strip()), None

        def sum_side(side):
            return {
                "visit": sum(r[side]["visit"] for r in rows),
                "deal": sum(r[side]["deal"] for r in rows),
                "paid": round(sum(r[side]["paid"] for r in rows), 2),
                "receivable": round(sum(r[side]["receivable"] for r in rows), 2),
            }

        total = {"first": sum_side("first"), "revisit": sum_side("revisit")}
        return {
            "start": start, "end": end, "role": role,
            "rows": rows, "total": total, "unclassified": unclassified,
        }

    _DS_COLS = [
        ("visit_date", "就诊日期"), ("chart_no", "病历号"), ("name", "患者姓名"), ("sex", "性别"),
        ("age", "年龄"), ("created_at", "建档时间"), ("referral_source", "患者来源"), ("phone", "联系方式"),
        ("doctor", "主治医生"), ("visit_type", "就诊类型"), ("consultant", "咨询师"), ("items", "就诊项目"),
        ("total_fee", "总费用"), ("receivable", "应收金额"), ("paid", "实收金额"),
        ("discount", "减免金额"), ("arrears", "累计欠费"),
    ]

    @router.get("/api/store-stats/daily-settlement")
    def daily_settlement(start: str = "", end: str = "", consultant: str = ""):
        """日结报表明细：每条就诊一行 + 患者信息 + 账单财务。"""
        require_access("report.finance.view")
        start, end = _norm_range(start, end)
        with connect(db_path) as conn:
            rows = daily_settlement_rows(conn, start, end, consultant.strip())
        totals = {k: round(sum(r[k] for r in rows), 2)
                  for k in ("total_fee", "receivable", "paid", "discount")}
        return {"start": start, "end": end, "columns": [c[1] for c in _DS_COLS],
                "rows": rows, "total": totals, "count": len(rows)}

    @router.get("/api/store-stats/daily-settlement.csv")
    def daily_settlement_csv(start: str = "", end: str = "", consultant: str = ""):
        """导出日结明细 CSV(utf-8-sig BOM)。"""
        require_perm("data_export")
        import csv
        import io
        start, end = _norm_range(start, end)
        with connect(db_path) as conn:
            rows = daily_settlement_rows(conn, start, end, consultant.strip())
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow([c[1] for c in _DS_COLS])
        for r in rows:
            w.writerow([r[c[0]] for c in _DS_COLS])
        data = ("﻿" + buf.getvalue()).encode("utf-8")
        return Response(content=data, media_type="text/csv; charset=utf-8",
                        headers={"Content-Disposition": f'attachment; filename="daily-settlement-{start}_{end}.csv"'})

    _CL_COLS = [
        ("visit_time", "就诊日期"), ("chart_no", "病历号"), ("name", "患者姓名"), ("age", "患者年龄"),
        ("doctor", "医生"), ("department", "科室"), ("visit_type", "就诊类型"), ("address", "家庭住址"),
        ("diagnosis", "临床初步判断"), ("treatment", "临床诊断措施"), ("destination", "病人去向"),
    ]

    @router.get("/api/store-stats/reconcile-calendar")
    def reconcile_calendar(month: str = ""):
        """对账日历：某月每天 收(当日收款)/支(当日退费)/合(净额) + 月合计。
        产品决策：支=当日退费金额(本地无支出记账)。收退口径同收银(排作废原单)。"""
        require_access("report.operations.view")
        m = (month or "").strip() or bj_now().strftime("%Y-%m")
        if len(m) != 7 or m[4] != "-" or not (m[:4].isdigit() and m[5:].isdigit()):
            raise HTTPException(status_code=400, detail="month 需为 YYYY-MM")
        y, mo = int(m[:4]), int(m[5:])
        if not (1 <= mo <= 12):
            raise HTTPException(status_code=400, detail="月份非法")
        start, end = f"{m}-01", f"{m}-31"
        cancel = valid_payment_clause("")
        with connect(db_path) as conn:
            rows = conn.execute(
                f"""
                select substr(pay_time, 1, 10) as day,
                       round(coalesce(sum(case when amount > 0 then amount else 0 end), 0), 2) as income,
                       round(coalesce(sum(case when amount < 0 then -amount else 0 end), 0), 2) as expense
                from payments
                where substr(coalesce(pay_time, ''), 1, 10) between ? and ? and {cancel}
                group by day
                """,
                (start, end),
            ).fetchall()
        by_day = {r["day"]: {"income": r["income"], "expense": r["expense"],
                             "net": round(r["income"] - r["expense"], 2)} for r in rows if r["day"]}
        t_in = round(sum(d["income"] for d in by_day.values()), 2)
        t_out = round(sum(d["expense"] for d in by_day.values()), 2)
        return {"month": m, "year": y, "month_num": mo, "days": by_day,
                "total_income": t_in, "total_expense": t_out, "total_net": round(t_in - t_out, 2)}

    @router.get("/api/store-stats/clinic-log")
    def clinic_log(start: str = "", end: str = "", doctor: str = ""):
        """门诊日志：每条就诊一行(就诊管理性质,非财务)。"""
        require_access("report.operations.view")
        start, end = _norm_range(start, end)
        with connect(db_path) as conn:
            rows = clinic_log_rows(conn, start, end, doctor.strip())
        return {"start": start, "end": end, "columns": [c[1] for c in _CL_COLS],
                "rows": rows, "count": len(rows)}

    @router.get("/api/store-stats/clinic-log.csv")
    def clinic_log_csv(start: str = "", end: str = "", doctor: str = ""):
        """导出门诊日志 CSV(utf-8-sig BOM)。"""
        require_perm("data_export")
        import csv
        import io
        start, end = _norm_range(start, end)
        with connect(db_path) as conn:
            rows = clinic_log_rows(conn, start, end, doctor.strip())
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow([c[1] for c in _CL_COLS])
        for r in rows:
            w.writerow([r[c[0]] for c in _CL_COLS])
        data = ("﻿" + buf.getvalue()).encode("utf-8")
        return Response(content=data, media_type="text/csv; charset=utf-8",
                        headers={"Content-Disposition": f'attachment; filename="clinic-log-{start}_{end}.csv"'})

    return router
