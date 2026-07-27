"""实体版本历史查询(呈现层,只读)：患者/病历/预约/回访的版本元数据分页。"""
from pathlib import Path

from fastapi import APIRouter, HTTPException

from local_app.access_authorization import require_access
from local_app.db import connect
from local_app.snapshots import row_to_dict as _row_to_dict


VERSION_SOURCES = {
    "patient": {
        "table": "patient_versions",
        "id_column": "patient_identity",
        "permission": "patient.profile.view",
        "owner_sql": "select patient_identity from patients where patient_identity = ? and coalesce(is_deleted, 0) = 0",
    },
    "medical_record": {
        "table": "medical_record_versions",
        "id_column": "record_id",
        "permission": "medical_record.view",
        "owner_sql": "select m.patient_identity from medical_records m join patients p on p.patient_identity = m.patient_identity where m.record_id = ? and coalesce(p.is_deleted, 0) = 0",
    },
    "appointment": {
        "table": "appointment_versions",
        "id_column": "appointment_id",
        "permission": "appointment.view",
        "owner_sql": "select a.patient_identity from appointments a join patients p on p.patient_identity = a.patient_identity where a.appointment_id = ? and coalesce(p.is_deleted, 0) = 0",
    },
    "return_visit": {
        "table": "return_visit_versions",
        "id_column": "return_visit_id",
        "permission": "return_visit.view",
        "owner_sql": "select r.patient_identity from return_visits r join patients p on p.patient_identity = r.patient_identity where r.return_visit_id = ? and coalesce(r.is_deleted, 0) = 0 and coalesce(p.is_deleted, 0) = 0",
    },
}


def create_versions_router(db_path):
    router = APIRouter()
    db_path = Path(db_path)

    @router.get("/api/versions")
    def versions(entity_type: str, entity_id: str, pageno: int = 1, pagesize: int = 50):
        entity_type = entity_type.strip()
        source = VERSION_SOURCES.get(entity_type)
        if not source:
            raise HTTPException(status_code=400, detail="unsupported entity_type")
        require_access(source["permission"])
        entity_id = entity_id.strip()
        if not entity_id:
            raise HTTPException(status_code=400, detail="entity_id is required")

        pageno = max(pageno, 1)
        pagesize = min(max(pagesize, 1), 200)
        offset = (pageno - 1) * pagesize
        table = source["table"]
        id_column = source["id_column"]

        with connect(db_path) as conn:
            owner = conn.execute(source["owner_sql"], (entity_id,)).fetchone()
            if owner is None:
                raise HTTPException(status_code=404, detail="not_found")
            total = conn.execute(
                f"select count(*) from {table} where {id_column} = ?",
                (entity_id,),
            ).fetchone()[0]
            if entity_type == "return_visit":
                rows = conn.execute(
                    """
                    with numbered as (
                        select version_id, return_visit_id as entity_id,
                               version_hash, batch_id, changed_at,
                               row_number() over (order by version_id) as version_number
                        from return_visit_versions
                        where return_visit_id = ?
                    )
                    select version_id, entity_id, version_hash, batch_id,
                           changed_at, version_number
                    from numbered
                    order by version_number desc
                    limit ? offset ?
                    """,
                    (entity_id, pagesize, offset),
                ).fetchall()
            else:
                rows = conn.execute(
                    f"""
                    select version_id, {id_column} as entity_id, version_hash,
                           batch_id, changed_at
                    from {table}
                    where {id_column} = ?
                    order by changed_at desc, version_id desc
                    limit ? offset ?
                    """,
                    (entity_id, pagesize, offset),
                ).fetchall()

        return {
            "list": [
                {"entity_type": entity_type, **_row_to_dict(row)}
                for row in rows
            ],
            "totalcount": total,
            "pageno": pageno,
            "pagesize": pagesize,
        }

    return router
