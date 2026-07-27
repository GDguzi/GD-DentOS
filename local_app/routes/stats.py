"""首页统计(呈现层,只读)：按权限区块查询基础、财务、报表与审计数据。"""
from pathlib import Path

from fastapi import APIRouter

from local_app.access_authorization import has_access, require_access
from local_app.db import active_bill_clause, connect


def create_stats_router(db_path):
    router = APIRouter()
    db_path = Path(db_path)

    @router.get("/api/stats")
    def stats():
        see_patient = has_access("patient.profile.view")
        see_billing = has_access("billing.view")
        see_report = has_access("report.finance.view")
        see_audit = has_access("audit.view")
        if not any((see_patient, see_billing, see_report, see_audit)):
            # 中央路由守卫会先做 any-of；此处仍 fail closed，且不触库。
            require_access("patient.profile.view")

        patient_count_tables = [
            "patients",
            "medical_records",
            "appointments",
            "return_visits",
            "local_notes",
            "patient_versions",
            "medical_record_versions",
            "appointment_versions",
            "return_visit_versions",
        ]
        result = {}
        with connect(db_path) as conn:
            counts = {}
            if see_patient:
                counts.update({
                    table: conn.execute(f"select count(*) from {table}").fetchone()[0]
                    for table in patient_count_tables
                })
            if see_billing:
                for table in ("bills", "payments", "treatment_items"):
                    counts[table] = conn.execute(f"select count(*) from {table}").fetchone()[0]
            if see_report:
                result["money"] = {
                    "bill_total_fee": conn.execute(
                        f"select coalesce(sum(total_fee), 0) from bills where {active_bill_clause()}"
                    ).fetchone()[0],
                    "bill_paid_fee": conn.execute(
                        f"select coalesce(sum(paid_fee), 0) from bills where {active_bill_clause()}"
                    ).fetchone()[0],
                    "payment_amount": conn.execute(
                        "select coalesce(sum(amount), 0) from payments"
                    ).fetchone()[0],
                }
            if see_audit:
                counts["audit_logs"] = conn.execute("select count(*) from audit_logs").fetchone()[0]
            if counts:
                result["counts"] = counts

        return result

    return router
