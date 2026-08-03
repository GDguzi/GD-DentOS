import json


def row_to_dict(row):
    return {key: row[key] for key in row.keys()}


def rows(conn, sql, params=()):
    return [row_to_dict(row) for row in conn.execute(sql, params).fetchall()]


def patient_profile_snapshot(row):
    # 不含 source_json：镜像层原始 payload 留在 patients.source_json 本体，
    # 快照若嵌入它，写回/审计会逐层转义膨胀且污染镜像语义。
    data = row_to_dict(row)
    return {
        "patient_identity": data.get("patient_identity"),
        "source_customer_id": data.get("source_customer_id"),
        "display_name": data.get("display_name"),
        "phone": data.get("phone"),
        "sex": data.get("sex"),
        "birthday": data.get("birthday"),
        "address": data.get("address"),
        "allergy_history": data.get("allergy_history"),
        "medication_history": data.get("medication_history"),
        "id_card": data.get("id_card"),
        "wechat": data.get("wechat"),
        "email": data.get("email"),
        "occupation": data.get("occupation"),
        "work_unit": data.get("work_unit"),
        "patient_type": data.get("patient_type"),
        "responsible_doctor": data.get("responsible_doctor"),
        "consultant": data.get("consultant"),
        "referral_source": data.get("referral_source"),
        "referral_source2": data.get("referral_source2"),
        "referral_source3": data.get("referral_source3"),
        "referral_source4": data.get("referral_source4"),
        "introducer_type": data.get("introducer_type"),
        "introducer_name": data.get("introducer_name"),
        "phone_vestee": data.get("phone_vestee"),
        "remark": data.get("remark"),
        "chart_no": data.get("chart_no"),
        "updated_at": data.get("updated_at"),
    }


def medical_record_snapshot(row):
    data = row_to_dict(row)
    return {
        "record_id": data.get("record_id"),
        "patient_identity": data.get("patient_identity"),
        "study_identity": data.get("study_identity"),
        "record_type": data.get("record_type"),
        "visit_time": data.get("visit_time"),
        "doctor_name": data.get("doctor_name"),
        "tooth_json": data.get("tooth_json"),
        "content_json": data.get("content_json"),
        "updated_at": data.get("updated_at"),
    }


def appointment_snapshot(row):
    data = row_to_dict(row)
    return {
        "appointment_id": data.get("appointment_id"),
        "patient_identity": data.get("patient_identity"),
        "study_identity": data.get("study_identity"),
        "start_time": data.get("start_time"),
        "end_time": data.get("end_time"),
        "doctor_name": data.get("doctor_name"),
        "item_name": data.get("item_name"),
        "status": data.get("status"),
        "cancel_reason": data.get("cancel_reason"),
        # 审查#7：到达/离店/诊室/就诊类型是状态机最关键字段,必须进快照(否则审计漏改动)
        "room": data.get("room"),
        "arrived_at": data.get("arrived_at"),
        "finished_at": data.get("finished_at"),
        "visit_type": data.get("visit_type"),
        "register_type": data.get("register_type"),
        "suspect_cancelled": data.get("suspect_cancelled"),
        "suspect_reason": data.get("suspect_reason"),
        "source_json": data.get("source_json"),
        "updated_at": data.get("updated_at"),
    }


def return_visit_snapshot(row):
    data = row_to_dict(row)
    return {
        "return_visit_id": data.get("return_visit_id"),
        "patient_identity": data.get("patient_identity"),
        "due_time": data.get("due_time"),
        "item_name": data.get("item_name"),
        "status": data.get("status"),
        "note": data.get("note"),
        "visitor": data.get("visitor"),
        "return_doctor": data.get("return_doctor"),
        "return_type": data.get("return_type"),
        "channel": data.get("channel"),
        "return_result": data.get("return_result"),
        "actual_date": data.get("actual_date"),
        "intent_level": data.get("intent_level"),
        "satisfaction": data.get("satisfaction"),
        "revision": data.get("revision"),
        "is_deleted": data.get("is_deleted"),
        "source_json": data.get("source_json"),
        "updated_at": data.get("updated_at"),
    }


def json_payload_value(value):
    if isinstance(value, str):
        return value
    return json.dumps(value if value is not None else {}, ensure_ascii=False, sort_keys=True)


def safe_json_object(value):
    if not value:
        return {}
    try:
        parsed = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}
