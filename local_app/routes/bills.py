"""账单列表查询(呈现层,只读)：日期/关键词/欠费/结算过滤+分页。收款/退款操作在 billing_ops。"""
from pathlib import Path

from fastapi import APIRouter

from local_app.auth import require_perm
from local_app.db import active_bill_clause, bill_net_arrears_sql, connect
from local_app.snapshots import row_to_dict as _row_to_dict
from local_app.validation import valid_date_param


def create_bills_router(db_path):
    router = APIRouter()
    db_path = Path(db_path)

    @router.get("/api/bills")
    def bill_list(
        date: str = "",
        q: str = "",
        unpaid_only: bool = False,
        settlement_only: bool = False,
        pageno: int = 1,
        pagesize: int = 50,
    ):
        require_perm("billing.view")   # #482：账单列表含金额/欠款,按 billing.view 守卫(护士/技师/助理等无权)
        date = date.strip()
        if date:
            valid_date_param(date, "date")
        q = q.strip()
        pageno = max(pageno, 1)
        pagesize = min(max(pagesize, 1), 200)
        offset = (pageno - 1) * pagesize

        where_parts = []
        params = []
        if date:
            where_parts.append("substr(coalesce(b.bill_time, ''), 1, 10) = ?")
            params.append(date)
        if unpaid_only:
            where_parts.append(bill_net_arrears_sql("b.") + " > 0.005")   # 扣优惠后仍欠
            where_parts.append(active_bill_clause("b.state"))   # 撤单单不算欠费
        if settlement_only:
            where_parts.append("b.total_fee > 0")
        if q:
            like = f"%{q}%"
            where_parts.append(
                """
                (
                    b.bill_id like ?
                    or b.bill_no like ?
                    or b.patient_identity like ?
                    or b.state like ?
                    or p.display_name like ?
                    or p.phone like ?
                )
                """
            )
            params.extend([like, like, like, like, like, like])
        where_sql = "where " + " and ".join(where_parts) if where_parts else ""
        order_sql = (
            "unpaid_fee desc, coalesce(b.bill_time, '') desc, b.bill_id"
            if unpaid_only and not settlement_only
            else "coalesce(b.bill_time, '') desc, b.bill_id"
        )

        with connect(db_path) as conn:
            total = conn.execute(
                f"""
                select count(*)
                from bills b
                left join patients p on p.patient_identity = b.patient_identity
                {where_sql}
                """,
                tuple(params),
            ).fetchone()[0]
            rows = conn.execute(
                f"""
                select
                    b.bill_id, b.patient_identity, p.display_name, p.phone,
                    b.bill_no, b.bill_time, b.total_fee, b.paid_fee,
                    {bill_net_arrears_sql("b.")} as unpaid_fee,
                    b.state, b.updated_at
                from bills b
                left join patients p on p.patient_identity = b.patient_identity
                {where_sql}
                order by {order_sql}
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

    return router
