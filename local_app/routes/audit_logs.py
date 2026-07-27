"""审计日志查询(呈现层,只读)：全量审计(audit.view) + 患者关联审计(patient.view)。"""
import json
from pathlib import Path

from fastapi import APIRouter, HTTPException

from local_app.auth import require_perm
from local_app.db import connect
from local_app.snapshots import row_to_dict as _row_to_dict
from local_app.validation import valid_date_param


_HANDLE_ITEM_AUDIT_FIELDS = (
    "handle_id",
    "code",
    "name",
    "fee_type",
    "unit",
    "price",
    "stopped",
)


def _project_handle_item_snapshot(raw):
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(value, dict):
        return None
    return {field: value.get(field) for field in _HANDLE_ITEM_AUDIT_FIELDS}


def _audit_list_item(row):
    item = _row_to_dict(row)
    if item.get("entity_type") == "handle_item":
        item["change"] = {
            "before": _project_handle_item_snapshot(row["old_json"]),
            "after": _project_handle_item_snapshot(row["new_json"]),
        }
    item.pop("old_json", None)
    item.pop("new_json", None)
    return item


def create_audit_logs_router(db_path):
    router = APIRouter()
    db_path = Path(db_path)

    @router.get("/api/audit-logs/operators")
    def audit_operators():
        require_perm("audit.view")   # 与操作历史列表同守卫
        with connect(db_path) as conn:
            rows = conn.execute(
                "select distinct operator from audit_logs "
                "where coalesce(operator, '') <> '' order by operator"
            ).fetchall()
        return {"operators": [r["operator"] for r in rows]}

    @router.get("/api/audit-logs")
    def audit_logs(
        entity_type: str = "",
        entity_id: str = "",
        operator: str = "",
        date_from: str = "",
        date_to: str = "",
        q: str = "",
        pageno: int = 1,
        pagesize: int = 50,
    ):
        require_perm("audit.view")   # 全量审计含跨患者业务记录号,隐藏菜单挡不住直接API调用,后端硬守卫
        pageno = max(pageno, 1)
        pagesize = min(max(pagesize, 1), 200)
        offset = (pageno - 1) * pagesize
        where_parts = []
        params = []
        if entity_type.strip():
            where_parts.append("entity_type = ?")
            params.append(entity_type.strip())
        if entity_id.strip():
            where_parts.append("entity_id = ?")
            params.append(entity_id.strip())
        if operator.strip():                       # 操作历史页:按操作人筛
            where_parts.append("operator = ?")
            params.append(operator.strip())
        if date_from.strip():                       # 按日期范围筛(含头含尾,按 created_at 日期部分)
            valid_date_param(date_from.strip(), "date_from")
            where_parts.append("substr(coalesce(created_at,''),1,10) >= ?")
            params.append(date_from.strip())
        if date_to.strip():
            valid_date_param(date_to.strip(), "date_to")
            where_parts.append("substr(coalesce(created_at,''),1,10) <= ?")
            params.append(date_to.strip())
        if q.strip():                               # 关键词:动作/实体类型/实体ID 模糊
            kw = f"%{q.strip()}%"
            where_parts.append("(action like ? or entity_type like ? or entity_id like ?)")
            params.extend([kw, kw, kw])
        where_sql = "where " + " and ".join(where_parts) if where_parts else ""

        with connect(db_path) as conn:
            total = conn.execute(
                f"select count(*) from audit_logs {where_sql}",
                tuple(params),
            ).fetchone()[0]
            rows = conn.execute(
                f"""
                select audit_id, entity_type, entity_id, action, operator, created_at,
                       old_json, new_json
                from audit_logs
                {where_sql}
                order by created_at desc, audit_id desc
                limit ? offset ?
                """,
                tuple(params + [pagesize, offset]),
            ).fetchall()

        return {
            "list": [_audit_list_item(row) for row in rows],
            "totalcount": total,
            "pageno": pageno,
            "pagesize": pagesize,
        }

    @router.get("/api/patients/{patient_identity}/audit-logs")
    def patient_audit_logs(patient_identity: str, pageno: int = 1, pagesize: int = 50):
        require_perm("patient.view")   # 患者本人操作历史(患者详情内嵌),按 patient.view 守卫(全量审计另按 audit.view,见 )
        patient_identity = patient_identity.strip()
        if not patient_identity:
            raise HTTPException(status_code=400, detail="patient_identity is required")
        pageno = max(pageno, 1)
        pagesize = min(max(pagesize, 1), 200)
        offset = (pageno - 1) * pagesize
        related_where = """
            (
                (entity_type = 'patient' and entity_id = ?)
                or (entity_type = 'medical_record' and entity_id in (
                    select record_id from medical_records where patient_identity = ?
                ))
                or (entity_type = 'appointment' and entity_id in (
                    select appointment_id from appointments where patient_identity = ?
                ))
                or (entity_type = 'return_visit' and entity_id in (
                    select return_visit_id from return_visits where patient_identity = ?
                ))
                or (entity_type = 'member_account' and entity_id = ?)
                or (entity_type = 'call' and (
                    entity_id in (select call_id from calls where patient_identity = ?)
                    or json_extract(new_json, '$.patient_identity') = ?
                    or json_extract(old_json, '$.patient_identity') = ?
                ))
                or (entity_type = 'communication' and (
                    entity_id in (select communication_id from communications where patient_identity = ?)
                    or json_extract(new_json, '$.patient_identity') = ?
                    or json_extract(old_json, '$.patient_identity') = ?
                ))
            )
        """
        # 会员储值流水(member_account,entity_id=患者)也要进患者审计视图
        # 通话录音(call)、沟通记录(communication)按表 patient_identity 反查(记录仍在)，
        # 并叠加按审计 json 的 patient_identity 匹配(删除/删盘失败审计——业务行已删，反查会落空)
        params = (patient_identity,) * 11

        with connect(db_path) as conn:
            exists = conn.execute(
                "select 1 from patients where patient_identity = ?",
                (patient_identity,),
            ).fetchone()
            if not exists:
                raise HTTPException(status_code=404, detail="patient not found")
            total = conn.execute(
                f"select count(*) from audit_logs where {related_where}",
                params,
            ).fetchone()[0]
            rows = conn.execute(
                f"""
                select audit_id, entity_type, entity_id, action, operator, created_at
                from audit_logs
                where {related_where}
                order by created_at desc, audit_id desc
                limit ? offset ?
                """,
                params + (pagesize, offset),
            ).fetchall()

        return {
            "list": [_row_to_dict(row) for row in rows],
            "totalcount": total,
            "pageno": pageno,
            "pagesize": pagesize,
        }

    return router
