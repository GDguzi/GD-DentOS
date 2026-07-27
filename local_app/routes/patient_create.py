import datetime as dt
import sqlite3
from pathlib import Path

from fastapi import APIRouter, HTTPException

from local_app.timeutil import now_str
from local_app.auth import audit_write, require_perm
from local_app.db import connect, new_id
from local_app.pinyin_util import name_pinyin
from local_app.versioning import stable_hash, stable_json

_FIELDS = ["display_name", "phone", "sex", "birthday", "address",
           "allergy_history", "medication_history",
           "id_card", "wechat", "email", "occupation", "work_unit", "patient_type",
           "responsible_doctor", "consultant",
           "referral_source", "referral_source2", "referral_source3", "referral_source4",
           "introducer_type", "introducer_name", "phone_vestee", "remark"]




def _gen_chart_no(conn, now, bump=0):
    """本地自建病历号：YYMMDD + 当日流水(≥3位)。
    取当日最大流水+1，**同时看本地 chart_no 和已同步患者的 source_json.patientid**(都是 YYMMDD+流水)，
    本地号续在两者当日最大值之后，避开 外部系统 已有病历号。bump 给唯一约束冲突时重试递增用。"""
    prefix = dt.datetime.strptime(now, "%Y-%m-%d %H:%M:%S").strftime("%y%m%d")
    row = conn.execute(
        """
        select max(seq) from (
            select cast(substr(chart_no, 7) as integer) seq from patients
              where substr(coalesce(chart_no, ''), 1, 6) = ? and length(chart_no) >= 9
            union all
            select cast(substr(json_extract(source_json, '$.patientid'), 7) as integer) seq from patients
              where json_valid(source_json)
                and substr(coalesce(json_extract(source_json, '$.patientid'), ''), 1, 6) = ?
                and length(coalesce(json_extract(source_json, '$.patientid'), '')) >= 9
        )
        """,
        (prefix, prefix),
    ).fetchone()
    seq = (row[0] or 0) + 1 + bump
    return f"{prefix}{seq:03d}"   # %03d：≥3 位；单店日建档量远小于 999，不会溢出


def create_patient_create_router(db_path):
    """患者建档：本地新建患者（POST /api/patients）。列表 GET / 编辑 PUT 在 api.py。"""
    router = APIRouter()
    db_path = Path(db_path)

    @router.post("/api/patients")
    def create_patient(payload: dict):
        require_perm("patient.create")
        values = {f: str(payload.get(f) or "").strip() for f in _FIELDS}
        if not values["display_name"]:
            raise HTTPException(status_code=400, detail="患者姓名不能为空")
        if not values["phone"]:
            # 对齐官方新增患者(姓名+电话必填)，避免落"有名无电话"的患者
            raise HTTPException(status_code=400, detail="患者电话不能为空")

        patient_identity = new_id("local-pat")
        now = now_str()

        with connect(db_path) as conn:
            snapshot = None
            # 唯一索引 ux_patients_chart_no 挡重号；并发/回填占号时撞了就递增流水重试
            for _bump in range(8):
                chart_no = _gen_chart_no(conn, now, bump=_bump)
                snapshot = {"patient_identity": patient_identity, "chart_no": chart_no, **values}
                current_hash = stable_hash(snapshot)
                try:
                    conn.execute(
                        """
                        insert into patients(
                            patient_identity, display_name, phone, sex, birthday, address,
                            allergy_history, medication_history,
                            id_card, wechat, email, occupation, work_unit, patient_type,
                            responsible_doctor, consultant,
                            referral_source, referral_source2, referral_source3, referral_source4,
                            introducer_type, introducer_name, phone_vestee, remark, chart_no,
                            name_pinyin, current_hash, created_at, updated_at, local_updated_at
                        )
                        values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            patient_identity,
                            values["display_name"],
                            values["phone"],
                            values["sex"],
                            values["birthday"],
                            values["address"],
                            values["allergy_history"],
                            values["medication_history"],
                            values["id_card"],
                            values["wechat"],
                            values["email"],
                            values["occupation"],
                            values["work_unit"],
                            values["patient_type"],
                            values["responsible_doctor"],
                            values["consultant"],
                            values["referral_source"],
                            values["referral_source2"],
                            values["referral_source3"],
                            values["referral_source4"],
                            values["introducer_type"],
                            values["introducer_name"],
                            values["phone_vestee"],
                            values["remark"],
                            chart_no,
                            name_pinyin(values["display_name"]),
                            current_hash,
                            now,
                            now,
                            now,
                        ),
                    )
                    break
                except sqlite3.IntegrityError:
                    if _bump >= 7:
                        raise HTTPException(status_code=409, detail="病历号生成冲突，请重试")
                    continue
            audit_write(conn, "patient", patient_identity, "create_patient",
                        new_json=stable_json(snapshot), created_at=now)
            conn.commit()

        return {"patient_identity": patient_identity}

    return router
