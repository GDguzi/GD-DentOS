"""患者核心(业务层)：列表/搜索/CSV导出/详情/更新/备注+版本快照+审计。建档在 patient_create。"""
import json
import uuid
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response

from local_app.access_authorization import has_access, require_access
from local_app.timeutil import now_str
from local_app.auth import audit_write, require_perm
from local_app.db import (VOIDED_BILL_STATES, active_bill_clause, begin_immediate,
                          bill_discount_sql, bill_net_arrears_sql, connect)
from local_app.snapshots import patient_profile_snapshot, row_to_dict as _row_to_dict, rows as _rows
from local_app.validation import valid_date_param
from local_app.versioning import stable_hash, stable_json


def build_patient_filter(q="", birthday_on="", group="", scope="", doctor="", first_doctor=""):
    """患者筛选 WHERE 构建，列表与导出共用。返回 (where_sql, params)，where_sql 以 'where ' 开头，
    恒含「排除已合并/删除」。铁律：两端筛选口径必须一致——历史上各抄一份漂移出过
    bug(按病历号筛选后导出空CSV)，收敛成单一构建器。"""
    q = (q or "").strip()
    birthday_on = (birthday_on or "").strip()
    group = (group or "").strip()
    scope = (scope or "").strip()
    doctor = (doctor or "").strip()
    first_doctor = (first_doctor or "").strip()
    # 排除已合并/删除的患者(合并去重后次患者不再出现)
    where_parts = ["coalesce(is_deleted, 0) = 0"]
    params = []
    if q:
        like = f"%{q}%"
        # q 口径含病历号 chart_no + 回退 source_json.patientid
        where_parts.append(
            "(patient_identity like ? or source_customer_id like ? or display_name like ? "
            "or phone like ? or address like ? or name_pinyin like ? "
            "or chart_no like ? "
            "or json_extract(iif(json_valid(source_json), source_json, '{}'), '$.patientid') like ?)")
        params.extend([like] * 8)
    if birthday_on:
        valid_date_param(birthday_on, "birthday_on")
        where_parts.append(
            "birthday is not null and birthday != '' "
            "and substr(birthday, 1, 10) != '0000-00-00' "
            "and substr(birthday, 6, 5) = substr(?, 6, 5)")
        params.append(birthday_on)
    # patient_group 是逗号拼接的多分组（"最近患者,种植牙" 或带空格 "最近患者, 种植牙"）。
    # 匹配时去掉所有空格再前后补逗号做精确成员匹配，与 /api/patient-groups 计数(按逗号拆strip)
    # 同源；转义 LIKE 通配符 %/_ 避免组名含这些字符误命中。
    # 真相源=本地列 patient_group(同步建档/箱子回填已落列),不再读 source_json。
    groupname_norm = "(',' || replace(coalesce(patient_group, ''), ' ', '') || ',')"
    if group:
        g = group.replace(" ", "").replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        where_parts.append(f"{groupname_norm} like ? escape '\\'")
        params.append(f"%,{g},%")
    if scope == "today":
        where_parts.append(
            "exists(select 1 from appointments a where a.patient_identity = patients.patient_identity "
            "and coalesce(a.status, '') not in ('3', '已取消') "
            "and substr(coalesce(a.start_time, ''), 1, 10) = date('now', 'localtime'))")
    elif scope == "recent":
        # 「最近」分组与 /api/patient-groups 的 recent 计数同源
        where_parts.append(f"{groupname_norm} like '%,最近患者,%'")
    if doctor:   # 主治医生：有该医生的(未取消)预约
        where_parts.append(
            "exists(select 1 from appointments a where a.patient_identity = patients.patient_identity "
            "and a.doctor_name = ? and coalesce(a.status,'') not in ('3','已取消'))")
        params.append(doctor)
    if first_doctor:   # 初诊医生：最早一次(未取消)预约的医生
        where_parts.append(
            "(select a.doctor_name from appointments a where a.patient_identity = patients.patient_identity "
            "and coalesce(a.status,'') not in ('3','已取消') and coalesce(a.start_time,'')!='' "
            "order by a.start_time asc limit 1) = ?")
        params.append(first_doctor)
    return "where " + " and ".join(where_parts), params


def create_patients_router(db_path):
    router = APIRouter()
    db_path = Path(db_path)

    @router.get("/api/patients")
    def patients(q: str = "", birthday_on: str = "", group: str = "", scope: str = "",
                 doctor: str = "", first_doctor: str = "", pageno: int = 1, pagesize: int = 50):
        require_access("patient.profile.view")
        q = q.strip()
        scope = scope.strip()
        pageno = max(pageno, 1)
        pagesize = min(max(pagesize, 1), 200)
        offset = (pageno - 1) * pagesize
        where, params = build_patient_filter(q=q, birthday_on=birthday_on, group=group,
                                             scope=scope, doctor=doctor, first_doctor=first_doctor)

        with connect(db_path) as conn:
            total = conn.execute(
                f"select count(*) from patients {where}",
                tuple(params),
            ).fetchone()[0]
            # 搜索时按「最近就诊」排序(就近原则,近期来过的排前)；无搜索词的全量列表保持
            # 按更新时间排(避免对全表 11k 行做相关子查询排序拖慢)。
            order_clause = (
                "order by last_visit desc, coalesce(updated_at, local_updated_at, created_at) desc"
                if (q or scope in ("recent", "today")) else
                "order by coalesce(updated_at, local_updated_at, created_at) desc"
            )
            rows = conn.execute(
                f"""
                select
                    patient_identity,
                    source_customer_id,
                    display_name,
                    phone,
                    sex,
                    birthday,
                    address,
                    updated_at,
                    local_updated_at,
                    avatar_path,
                    remark,
                    coalesce(nullif(chart_no, ''), json_extract(source_json, '$.patientid')) as chart_no,
                    (select max(a.start_time) from appointments a
                       where a.patient_identity = patients.patient_identity
                         and coalesce(a.status, '') not in ('3', '已取消')
                         and substr(coalesce(a.start_time, ''), 1, 10) <= date('now', 'localtime')) as last_visit,
                    -- 卡片状态标签数据（仅对本页 ≤200 行计算，不影响全表）
                    (select a.visit_type from appointments a
                       where a.patient_identity = patients.patient_identity
                         and coalesce(a.status, '') not in ('3', '已取消')
                         and substr(coalesce(a.start_time, ''), 1, 10) <= date('now', 'localtime')
                       order by a.start_time desc limit 1) as last_visit_type,
                    (select a.doctor_name from appointments a
                       where a.patient_identity = patients.patient_identity
                         and coalesce(a.status, '') not in ('3', '已取消')
                         and substr(coalesce(a.start_time, ''), 1, 10) <= date('now', 'localtime')
                       order by a.start_time desc limit 1) as last_doctor,
                    (select a.doctor_name from appointments a
                       where a.patient_identity = patients.patient_identity
                         and coalesce(a.status, '') not in ('3', '已取消')
                         and coalesce(a.start_time, '') != ''
                       order by a.start_time asc limit 1) as first_doctor,
                    (select exists(select 1 from appointments a
                       where a.patient_identity = patients.patient_identity
                         and coalesce(a.status, '') not in ('3', '已取消')
                         and substr(coalesce(a.start_time, ''), 1, 10) > date('now', 'localtime'))) as has_future_appt,
                    (select exists(select 1 from return_visits r
                       where r.patient_identity = patients.patient_identity
                         and coalesce(r.is_deleted, 0) = 0)) as has_return_visit,
                    (select exists(select 1 from medical_records m
                       where m.patient_identity = patients.patient_identity
                         and coalesce(m.draft_status, '') = '')) as has_record,
                    (select exists(select 1 from patient_images i
                       where i.patient_identity = patients.patient_identity)) as has_image,
                    patient_group as groupname
                from patients
                {where}
                {order_clause}
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

    @router.get("/api/patients/export")
    def patients_export(q: str = "", group: str = "", scope: str = "",
                        doctor: str = "", first_doctor: str = "", birthday_on: str = ""):
        """按当前筛选导出患者为 CSV(UTF-8 BOM,Excel 可直接打开)。用户本机下载。"""
        require_perm("data_export")   # 导出含患者姓名/手机/病历号等隐私，仅 admin
        import csv
        import io as _io
        # 导出必须复用列表的当前筛选，否则在筛选结果里点导出会导出全量患者。
        # 与列表共用 build_patient_filter(口径一致铁律)；顺带补齐历史缺口——
        # 旧导出不认 scope=today,在「今日」筛选下点导出会静默导出全量。
        where, params = build_patient_filter(q=q, birthday_on=birthday_on, group=group,
                                             scope=scope, doctor=doctor, first_doctor=first_doctor)
        with connect(db_path) as conn:
            rows = conn.execute(
                f"""
                select display_name,
                       coalesce(nullif(chart_no, ''), json_extract(source_json,'$.patientid')) as chart_no,
                       phone, sex, birthday,
                       patient_group as groupname,
                       (select a.doctor_name from appointments a where a.patient_identity=patients.patient_identity
                          and coalesce(a.status,'') not in ('3','已取消')
                          order by a.start_time desc limit 1) as last_doctor,
                       (select a.doctor_name from appointments a where a.patient_identity=patients.patient_identity
                          and coalesce(a.status,'') not in ('3','已取消') and coalesce(a.start_time,'')!=''
                          order by a.start_time asc limit 1) as first_doctor,
                       (select max(a.start_time) from appointments a where a.patient_identity=patients.patient_identity
                          and coalesce(a.status,'') not in ('3','已取消')
                          and substr(coalesce(a.start_time,''),1,10) <= date('now','localtime')) as last_visit
                from patients {where}
                order by coalesce(updated_at, local_updated_at, created_at) desc
                """,
                tuple(params),
            ).fetchall()
        buf = _io.StringIO()
        buf.write("﻿")   # BOM,Excel 识别 UTF-8 中文
        w = csv.writer(buf)
        w.writerow(["姓名", "病历号", "手机号", "性别", "出生日期", "分组", "主治医生", "初诊医生", "最近就诊"])
        for r in rows:
            w.writerow([r["display_name"] or "", r["chart_no"] or "", r["phone"] or "", r["sex"] or "",
                        (r["birthday"] or "")[:10], r["groupname"] or "", r["last_doctor"] or "",
                        r["first_doctor"] or "", (r["last_visit"] or "")[:10]])
        return Response(content=buf.getvalue(), media_type="text/csv; charset=utf-8",
                        headers={"Content-Disposition": 'attachment; filename="patients.csv"'})

    @router.get("/api/patients/{patient_identity}")
    def patient_detail(patient_identity: str):
        require_access("patient.profile.view")
        can_view_billing = has_access("billing.view")
        with connect(db_path) as conn:
            patient = conn.execute(
                "select * from patients where patient_identity = ?",
                (patient_identity,),
            ).fetchone()
            if not patient:
                raise HTTPException(status_code=404, detail="patient not found")

            sections = {
                "medical_records": _rows(
                    conn,
                    """
                    select * from medical_records
                    where patient_identity = ?
                      and coalesce(draft_status, '') = ''
                    order by coalesce(visit_time, updated_at, created_at) desc
                    """,
                    (patient_identity,),
                ),
                "appointments": _rows(
                    conn,
                    """
                    select *,
                      case when json_valid(source_json)
                           then json_extract(source_json, '$.ScheduleStartTime') else null end
                        as original_start_time
                    from appointments
                    where patient_identity = ?
                    order by coalesce(start_time, updated_at) desc
                    """,
                    (patient_identity,),
                ),
                "return_visits": _rows(
                    conn,
                    """
                    select * from return_visits
                    where patient_identity = ?
                      and coalesce(is_deleted, 0) = 0
                    order by coalesce(due_time, updated_at) desc
                    """,
                    (patient_identity,),
                ),
                "local_notes": _rows(
                    conn,
                    """
                    select * from local_notes
                    where patient_identity = ?
                    order by updated_at desc
                    """,
                    (patient_identity,),
                ),
            }

            if can_view_billing:
                sections.update({
                    "bills": _rows(
                        conn,
                        """
                        select *,
                          json_extract(source_json, '$.BillDoct')     as bill_doctor,
                          coalesce(replace(json_extract(source_json, '$.DiscountFee'), ',', ''),
                                   replace(json_extract(source_json, '$.discount'), ',', '')) as discount_fee,
                          json_extract(source_json, '$.DiscountMemo') as discount_memo,
                          json_extract(source_json, '$.BillRemark')   as bill_remark,
                          case when coalesce(state, '') in ({void}) then 1 else 0 end as is_void,
                          json_extract(source_json, '$.CancelMark')   as cancel_mark,
                          round(total_fee - {disc}, 2)                as net_receivable
                        from bills
                        where patient_identity = ?
                        order by coalesce(bill_time, updated_at) desc
                        """.format(void=", ".join("'%s'" % s for s in VOIDED_BILL_STATES),
                                   disc=bill_discount_sql()),
                        (patient_identity,),
                    ),
                    # 收费单明细(按 bill_id 关联)：收费单卡展示本单有哪些诊疗项目
                    "treatment_items": _rows(
                        conn,
                        """
                        select item_name, quantity, unit_price, total_fee, fee_type, doctor_name, bill_id
                        from treatment_items
                        where patient_identity = ? and coalesce(is_deleted, 0) = 0
                          and coalesce(bill_id, '') != ''
                        order by bill_id
                        """,
                        (patient_identity,),
                    ),
                    "payments": _rows(
                        conn,
                        """
                        select *, json_extract(source_json, '$.pay_method') as pay_method
                        from payments
                        where patient_identity = ?
                        order by coalesce(pay_time, updated_at) desc
                        """,
                        (patient_identity,),
                    ),
                })

        return {"patient": _row_to_dict(patient), **sections}

    @router.put("/api/patients/{patient_identity}")
    def update_patient(patient_identity: str, payload: dict):
        require_perm("patient.edit")
        allowed_fields = ["display_name", "phone", "sex", "birthday", "address",
                          "allergy_history", "medication_history",
                          "id_card", "wechat", "email", "occupation", "work_unit", "patient_type",
                          "responsible_doctor", "consultant",
                          "referral_source", "referral_source2", "referral_source3", "referral_source4",
                          "introducer_type", "introducer_name", "phone_vestee", "remark"]
        # payload.get(field) or ""：JSON null 不得变成字面量 "None" 入库
        updates = {
            field: str(payload.get(field) or "").strip()
            for field in allowed_fields
            if field in payload
        }
        if not updates:
            raise HTTPException(status_code=400, detail="no editable fields")

        now = now_str()

        with connect(db_path) as conn:
            begin_immediate(conn)   # 抢写锁，防双击/多端并发改患者档案致丢更新+版本/审计重复
            patient = conn.execute(
                "select * from patients where patient_identity = ?",
                (patient_identity,),
            ).fetchone()
            if not patient:
                raise HTTPException(status_code=404, detail="patient not found")

            # 与建档校验同口径：已有姓名/电话不允许清空。
            # 历史同步行本来为空的不卡（否则整表单提交会被 400 卡死）。
            for required in ("display_name", "phone"):
                if (
                    required in updates
                    and not updates[required]
                    and str(patient[required] or "").strip()
                ):
                    raise HTTPException(status_code=400, detail="姓名/电话不能清空")

            # 无实际变更：不写版本/审计，原样返回（避免 no-op 保存灌水版本表）
            changed = {
                field: value
                for field, value in updates.items()
                if value != str(patient[field] or "").strip()
            }
            if not changed:
                return {"patient": _row_to_dict(patient)}

            old_snapshot = patient_profile_snapshot(patient)
            old_json = stable_json(old_snapshot)
            new_snapshot = dict(old_snapshot)
            new_snapshot.update(updates)
            new_snapshot["updated_at"] = now
            new_snapshot["edit_source"] = "local_profile_edit"
            new_hash = stable_hash(new_snapshot)

            # 版本行哈希由其存储的快照本身算出，保证哈希链可校验
            conn.execute(
                """
                insert into patient_versions(
                    patient_identity, version_hash, source_json, batch_id
                )
                values (?, ?, ?, ?)
                """,
                (patient_identity, stable_hash(old_snapshot), old_json, None),
            )

            # 镜像层红线：本地编辑绝不改写 patients.source_json（导入时的原始 payload）
            conn.execute(
                """
                update patients
                set display_name = ?,
                    phone = ?,
                    sex = ?,
                    birthday = ?,
                    address = ?,
                    allergy_history = ?,
                    medication_history = ?,
                    id_card = ?,
                    wechat = ?,
                    email = ?,
                    occupation = ?,
                    work_unit = ?,
                    patient_type = ?,
                    responsible_doctor = ?,
                    consultant = ?,
                    referral_source = ?,
                    referral_source2 = ?,
                    referral_source3 = ?,
                    referral_source4 = ?,
                    introducer_type = ?,
                    introducer_name = ?,
                    phone_vestee = ?,
                    remark = ?,
                    updated_at = ?,
                    current_hash = ?,
                    local_updated_at = current_timestamp
                where patient_identity = ?
                """,
                (
                    updates.get("display_name", patient["display_name"]),
                    updates.get("phone", patient["phone"]),
                    updates.get("sex", patient["sex"]),
                    updates.get("birthday", patient["birthday"]),
                    updates.get("address", patient["address"]),
                    updates.get("allergy_history", patient["allergy_history"]),
                    updates.get("medication_history", patient["medication_history"]),
                    updates.get("id_card", patient["id_card"]),
                    updates.get("wechat", patient["wechat"]),
                    updates.get("email", patient["email"]),
                    updates.get("occupation", patient["occupation"]),
                    updates.get("work_unit", patient["work_unit"]),
                    updates.get("patient_type", patient["patient_type"]),
                    updates.get("responsible_doctor", patient["responsible_doctor"]),
                    updates.get("consultant", patient["consultant"]),
                    updates.get("referral_source", patient["referral_source"]),
                    updates.get("referral_source2", patient["referral_source2"]),
                    updates.get("referral_source3", patient["referral_source3"]),
                    updates.get("referral_source4", patient["referral_source4"]),
                    updates.get("introducer_type", patient["introducer_type"]),
                    updates.get("introducer_name", patient["introducer_name"]),
                    updates.get("phone_vestee", patient["phone_vestee"]),
                    updates.get("remark", patient["remark"]),
                    now,
                    new_hash,
                    patient_identity,
                ),
            )
            audit_write(
                conn,
                "patient",
                patient_identity,
                "update_patient_profile",
                old_json=old_json,
                new_json=stable_json(new_snapshot),
                created_at=now,
            )
            conn.commit()
            updated = conn.execute(
                "select * from patients where patient_identity = ?",
                (patient_identity,),
            ).fetchone()

        return {"patient": _row_to_dict(updated)}

    @router.post("/api/patients/{patient_identity}/notes")
    def create_note(patient_identity: str, payload: dict):
        require_perm("patient.edit")
        note_text = str(payload.get("note_text", "")).strip()
        if not note_text:
            raise HTTPException(status_code=400, detail="note_text is required")

        now = now_str()
        note_id = uuid.uuid4().hex
        new_json = stable_json({"note_text": note_text})

        with connect(db_path) as conn:
            exists = conn.execute(
                "select 1 from patients where patient_identity = ?",
                (patient_identity,),
            ).fetchone()
            if not exists:
                raise HTTPException(status_code=404, detail="patient not found")

            conn.execute(
                """
                insert into local_notes(note_id, patient_identity, note_text, created_at, updated_at)
                values (?, ?, ?, ?, ?)
                """,
                (note_id, patient_identity, note_text, now, now),
            )
            audit_write(
                conn,
                "patient",
                patient_identity,
                "create_local_note",
                new_json=new_json,
                created_at=now,
            )
            conn.commit()

        return {"note_id": note_id, "patient_identity": patient_identity, "note_text": note_text}

    return router
