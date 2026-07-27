import json
from pathlib import Path

from fastapi import APIRouter, HTTPException

from local_app.timeutil import now_str
from local_app.auth import audit_write, require_perm
from local_app.db import begin_immediate, connect, new_id

_ITEM_TYPES = {"exam", "diagnosis", "treatment", "plan"}




def _item_dict(row):
    return {
        "item_id": row["item_id"],
        "record_id": row["record_id"],
        "tooth": row["tooth"],
        "item_type": row["item_type"],
        "content": row["content"],
        "sort_order": row["sort_order"],
        "created_at": row["created_at"],
    }


def _record_summary_dict(row):
    return {
        "record_id": row["record_id"],
        "record_type": row["record_type"],
        "visit_time": row["visit_time"],
        "doctor_name": row["doctor_name"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def create_record_items_router(db_path):
    router = APIRouter()
    db_path = Path(db_path)

    def require_record(conn, record_id):
        record = conn.execute(
            "select record_id, patient_identity from medical_records where record_id = ?",
            (record_id,),
        ).fetchone()
        if not record:
            raise HTTPException(status_code=404, detail="medical record not found")
        return record

    @router.get("/api/medical-records/{record_id}/items")
    def list_record_items(record_id: str):
        require_perm("medical_record.view")   # 病历牙位条目属病历,按 medical_record.view 守卫
        with connect(db_path) as conn:
            require_record(conn, record_id)
            rows = conn.execute(
                """
                select item_id, record_id, tooth, item_type, content, sort_order, created_at
                from medical_record_items
                where record_id = ?
                order by sort_order, item_id
                """,
                (record_id,),
            ).fetchall()
        items = [_item_dict(r) for r in rows]
        return {"items": items, "totalcount": len(items)}

    @router.put("/api/medical-records/{record_id}/items")
    def replace_record_items(record_id: str, payload: dict):
        require_perm("medical_record.edit")
        raw_items = payload.get("items")
        if not isinstance(raw_items, list):
            raise HTTPException(status_code=400, detail="items 必须是列表")

        items = []
        for raw in raw_items:
            content = str((raw or {}).get("content") or "").strip()
            if not content:
                continue
            item_type = str(raw.get("item_type") or "").strip()
            if item_type not in _ITEM_TYPES:
                raise HTTPException(
                    status_code=400,
                    detail="item_type 必须是 exam/diagnosis/treatment/plan",
                )
            items.append(
                {
                    "item_id": new_id("local-item"),
                    "tooth": str(raw.get("tooth") or "").strip(),
                    "item_type": item_type,
                    "content": content,
                }
            )

        now = now_str()
        with connect(db_path) as conn:
            begin_immediate(conn)   # 抢写锁，防并发双保存交错致审计链断档+丢更新
            record = require_record(conn, record_id)
            old_rows = conn.execute(
                """
                select tooth, item_type, content from medical_record_items
                where record_id = ? order by sort_order, item_id
                """,
                (record_id,),
            ).fetchall()
            old_items = [
                {"tooth": r["tooth"], "item_type": r["item_type"], "content": r["content"]}
                for r in old_rows
            ]
            conn.execute(
                "delete from medical_record_items where record_id = ?", (record_id,)
            )
            for order, item in enumerate(items):
                conn.execute(
                    """
                    insert into medical_record_items(
                        item_id, record_id, patient_identity, tooth, item_type,
                        content, sort_order, created_at, updated_at
                    )
                    values (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        item["item_id"],
                        record_id,
                        record["patient_identity"],
                        item["tooth"],
                        item["item_type"],
                        item["content"],
                        order,
                        now,
                        now,
                    ),
                )
            new_items = [
                {"tooth": i["tooth"], "item_type": i["item_type"], "content": i["content"]}
                for i in items
            ]
            audit_write(
                conn,
                "medical_record",
                record_id,
                "update_record_items",
                old_json=json.dumps(old_items, ensure_ascii=False),
                new_json=json.dumps(new_items, ensure_ascii=False),
                created_at=now,
            )
            conn.commit()

        return {"record_id": record_id, "item_count": len(items)}

    @router.get("/api/patients/{patient_identity}/record-items")
    def patient_record_items(patient_identity: str):
        """患者正式病历摘要及牙位条目（病历列表展示卡一次取齐）。"""
        require_perm("medical_record.view")   # 
        with connect(db_path) as conn:
            if not conn.execute(
                "select 1 from patients "
                "where patient_identity = ? and coalesce(is_deleted, 0) = 0",
                (patient_identity,),
            ).fetchone():
                raise HTTPException(status_code=404, detail="patient not found")
            timeline_rows = conn.execute(
                """
                select r.record_id, r.record_type, r.visit_time, r.doctor_name,
                       r.created_at, r.updated_at,
                       i.item_id, i.tooth, i.item_type, i.content,
                       i.sort_order, i.created_at as item_created_at
                from medical_records r
                left join medical_record_items i on i.record_id = r.record_id
                where r.patient_identity = ?
                  and coalesce(r.draft_status, '') = ''
                order by coalesce(
                    nullif(r.visit_time, ''), nullif(r.updated_at, ''),
                    nullif(r.created_at, ''), ''
                ) desc, r.record_id, i.sort_order, i.item_id
                """,
                (patient_identity,),
            ).fetchall()
        medical_records = []
        by_record = {}
        totalcount = 0
        for row in timeline_rows:
            record_id = row["record_id"]
            if record_id not in by_record:
                medical_records.append(_record_summary_dict(row))
                by_record[record_id] = []
            if row["item_id"] is not None:
                by_record[record_id].append({
                    "item_id": row["item_id"],
                    "record_id": record_id,
                    "tooth": row["tooth"],
                    "item_type": row["item_type"],
                    "content": row["content"],
                    "sort_order": row["sort_order"],
                    "created_at": row["item_created_at"],
                })
                totalcount += 1
        return {
            "medical_records": medical_records,
            "records": by_record,
            "totalcount": totalcount,
        }

    @router.get("/api/patients/{patient_identity}/teeth/summary")
    def teeth_summary(patient_identity: str):
        require_perm("medical_record.view")   # 
        with connect(db_path) as conn:
            if not conn.execute(
                "select 1 from patients where patient_identity = ?",
                (patient_identity,),
            ).fetchone():
                raise HTTPException(status_code=404, detail="patient not found")
            rows = conn.execute(
                """
                select i.tooth, i.item_type, count(*) as cnt,
                       max(coalesce(r.visit_time, '')) as last_visit_time
                from medical_record_items i
                join medical_records r on r.record_id = i.record_id
                where i.patient_identity = ? and i.tooth != ''
                group by i.tooth, i.item_type
                """,
                (patient_identity,),
            ).fetchall()

        by_tooth = {}
        for r in rows:
            entry = by_tooth.setdefault(
                r["tooth"], {"tooth": r["tooth"], "counts": {}, "last_visit_time": ""}
            )
            entry["counts"][r["item_type"]] = r["cnt"]
            if r["last_visit_time"] > entry["last_visit_time"]:
                entry["last_visit_time"] = r["last_visit_time"]
        teeth = sorted(by_tooth.values(), key=lambda t: t["tooth"])
        return {"teeth": teeth, "totalcount": len(teeth)}

    @router.get("/api/patients/{patient_identity}/teeth/{tooth}/history")
    def tooth_history(patient_identity: str, tooth: str, item_type: str = ""):
        require_perm("medical_record.view")   # 
        item_type = (item_type or "").strip()
        if item_type and item_type not in _ITEM_TYPES:
            raise HTTPException(
                status_code=400,
                detail="item_type 必须是 exam/diagnosis/treatment/plan",
            )
        with connect(db_path) as conn:
            if not conn.execute(
                "select 1 from patients where patient_identity = ?",
                (patient_identity,),
            ).fetchone():
                raise HTTPException(status_code=404, detail="patient not found")
            sql = """
                select i.item_id, i.record_id, i.tooth, i.item_type, i.content,
                       i.sort_order, i.created_at,
                       r.visit_time, r.doctor_name, r.record_type
                from medical_record_items i
                join medical_records r on r.record_id = i.record_id
                where i.patient_identity = ? and i.tooth = ?
            """
            params = [patient_identity, tooth]
            if item_type:
                sql += " and i.item_type = ?"
                params.append(item_type)
            sql += """
                order by coalesce(r.visit_time, '') desc, r.record_id, i.sort_order
            """
            rows = conn.execute(sql, params).fetchall()

        visits = []
        by_record = {}
        for r in rows:
            visit = by_record.get(r["record_id"])
            if visit is None:
                visit = {
                    "record_id": r["record_id"],
                    "visit_time": r["visit_time"],
                    "doctor_name": r["doctor_name"],
                    "record_type": r["record_type"],
                    "items": [],
                }
                by_record[r["record_id"]] = visit
                visits.append(visit)
            visit["items"].append(_item_dict(r))
        return {"tooth": tooth, "visits": visits, "totalcount": len(visits)}

    return router
