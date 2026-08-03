import hashlib
import json

from local_app.models import PatientSnapshot
from local_app.pinyin_util import name_pinyin


def stable_json(data):
    return json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def stable_hash(data):
    return hashlib.sha256(stable_json(data).encode("utf-8")).hexdigest()


def framed_utf8_sha256(*values):
    """Hash stripped text parts using unambiguous UTF-8 byte-length frames."""
    framed = []
    for value in values:
        text = "" if value is None else str(value).strip()
        encoded = text.encode("utf-8")
        framed.append(str(len(encoded)).encode("ascii") + b":" + encoded)
    return hashlib.sha256(b"\x1f".join(framed)).hexdigest()


_PROFILE_FIELDS = ("display_name", "phone", "sex", "birthday", "address")


def locally_edited_since_last_import(conn, entity_type, entity_id, actions, last_batch_id):
    """该实体在最近一次导入之后是否有本地编辑（看审计表）。"""
    placeholders = ",".join("?" for _ in actions)
    row = conn.execute(
        f"""
        select 1 from audit_logs
        where entity_type = ? and entity_id = ? and action in ({placeholders})
          and created_at >= coalesce(
              (select started_at from sync_batches where batch_id = ?), '')
        limit 1
        """,
        (entity_type, entity_id, *actions, last_batch_id),
    ).fetchone()
    return row is not None


def record_import_conflict(conn, entity_type, entity_id, field_name,
                           local_value, incoming_value, batch_id):
    # 同一实体同一字段已有 open 冲突就不再重复记（重跑导入不灌水冲突表）
    exists = conn.execute(
        """
        select 1 from import_conflicts
        where entity_type = ? and entity_id = ? and field_name = ? and status = 'open'
        limit 1
        """,
        (entity_type, entity_id, field_name),
    ).fetchone()
    if exists:
        return
    conn.execute(
        """
        insert into import_conflicts(
            entity_type, entity_id, field_name, local_value, incoming_value, batch_id
        )
        values (?, ?, ?, ?, ?, ?)
        """,
        (entity_type, entity_id, field_name, local_value, incoming_value, batch_id),
    )


def upsert_patient_with_version(conn, patient: PatientSnapshot, batch_id):
    """导入患者。返回 'imported'（新建/覆盖）或 'conflict'（本地编辑过，挂起不覆盖）。"""
    source_json = stable_json(patient.source)
    version_hash = stable_hash(patient.source)
    existing = conn.execute(
        """
        select current_hash, source_json, last_batch_id,
               display_name, phone, sex, birthday, address
        from patients where patient_identity = ?
        """,
        (patient.patient_identity,),
    ).fetchone()

    if existing and existing[0] != version_hash:
        # 兑现「冲突报告：不自动覆盖本地编辑」：上次导入后被本地编辑过、
        # 且档案字段确实有差异的行 → 逐字段记冲突、跳过覆盖，由人工裁决。
        diffs = [
            (field, existing[3 + idx], getattr(patient, field))
            for idx, field in enumerate(_PROFILE_FIELDS)
            if str(existing[3 + idx] or "").strip() != str(getattr(patient, field) or "").strip()
        ]
        if diffs and locally_edited_since_last_import(
            conn, "patient", patient.patient_identity,
            # #553：患者合并补全(merge_patient_master)也算本地编辑，否则合并回填的
            # phone/sex/birthday/address 会被重跑导入静默回退
            ("update_patient_profile", "merge_patient_master"), existing[2],
        ):
            for field, local_value, incoming_value in diffs:
                record_import_conflict(
                    conn, "patient", patient.patient_identity, field,
                    local_value, incoming_value, batch_id,
                )
            return "conflict"
        # #552：归档的 version_hash 必须与被归档的 source_json 自洽（系统不变量
        # current_hash==stable_hash(source_json)）。直接抄 existing.current_hash 会在
        # 该行被本地编辑/合并偏离后写出 hash≠hash(source_json) 的坏版本行。
        try:
            archived_hash = stable_hash(json.loads(existing[1])) if existing[1] else existing[0]
        except (ValueError, TypeError):
            archived_hash = existing[0]
        conn.execute(
            """
            insert into patient_versions(
                patient_identity, version_hash, source_json, batch_id
            )
            values (?, ?, ?, ?)
            """,
            (patient.patient_identity, archived_hash, existing[1], batch_id),
        )

    conn.execute(
        """
        insert into patients(
            patient_identity,
            source_customer_id,
            display_name,
            phone,
            sex,
            birthday,
            address,
            name_pinyin,
            updated_at,
            current_hash,
            source_json,
            last_batch_id
        )
        values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        on conflict(patient_identity) do update set
            -- 审计#27:incoming 为空时保留存量绑定,别用空/NULL 静默清空已绑定客户号
            source_customer_id = coalesce(nullif(excluded.source_customer_id, ''), patients.source_customer_id),
            display_name = excluded.display_name,
            phone = excluded.phone,
            sex = excluded.sex,
            birthday = excluded.birthday,
            address = excluded.address,
            name_pinyin = excluded.name_pinyin,
            updated_at = excluded.updated_at,
            current_hash = excluded.current_hash,
            source_json = excluded.source_json,
            local_updated_at = current_timestamp,
            last_batch_id = excluded.last_batch_id
        """,
        (
            patient.patient_identity,
            patient.source_customer_id,
            patient.display_name,
            patient.phone,
            patient.sex,
            patient.birthday,
            patient.address,
            name_pinyin(patient.display_name),
            patient.updated_at,
            version_hash,
            source_json,
            batch_id,
        ),
    )
    return "imported"
