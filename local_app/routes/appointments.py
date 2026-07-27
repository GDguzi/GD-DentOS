"""预约CRUD(业务层)：列表/汇总/新建/更新/查询/删除+版本快照+审计。"""
import json
from pathlib import Path

from fastapi import APIRouter, HTTPException

from local_app.access_authorization import require_access
from local_app.timeutil import now_str, today_str
from local_app.auth import audit_write, require_perm
from local_app.db import begin_immediate, connect, new_id
from local_app.models import appt_stage
from local_app.routes.patient_common import require_patient   # 审计R2:软删患者按不存在处理(共享校验)
from local_app.snapshots import appointment_snapshot, row_to_dict as _row_to_dict
from local_app.validation import valid_date_param, valid_time_field
from local_app.versioning import stable_hash, stable_json


def create_appointments_router(db_path):
    router = APIRouter()
    db_path = Path(db_path)

    @router.get("/api/appointments")
    def appointment_list(
        date: str = "",
        date_from: str = "",
        date_to: str = "",
        doctor_name: str = "",
        status: str = "",
        q: str = "",
        pageno: int = 1,
        pagesize: int = 50,
    ):
        require_perm("patient.view")   # 预约列表含患者姓名/电话,按 patient.view 守卫(无 appointment.view 权限点)
        date = date.strip()
        if date:
            valid_date_param(date, "date")
        # 区间查询（天/周/月四视图统一取数）：date_from/date_to 任一可单独给
        date_from = date_from.strip()
        date_to = date_to.strip()
        if date_from:
            valid_date_param(date_from, "date_from")
        if date_to:
            valid_date_param(date_to, "date_to")
        doctor_name = doctor_name.strip()
        status = status.strip()
        q = q.strip()
        pageno = max(pageno, 1)
        pagesize = min(max(pagesize, 1), 200)
        offset = (pageno - 1) * pagesize

        where_parts = []
        params = []
        if date:
            where_parts.append("substr(coalesce(a.start_time, ''), 1, 10) = ?")
            params.append(date)
        if date_from:
            where_parts.append("substr(coalesce(a.start_time, ''), 1, 10) >= ?")
            params.append(date_from)
        if date_to:
            where_parts.append("substr(coalesce(a.start_time, ''), 1, 10) <= ?")
            params.append(date_to)
        if doctor_name:
            where_parts.append("a.doctor_name = ?")
            params.append(doctor_name)
        if status:
            where_parts.append("a.status = ?")
            params.append(status)
        if q:
            like = f"%{q}%"
            where_parts.append(
                """
                (
                    a.appointment_id like ?
                    or a.patient_identity like ?
                    or a.doctor_name like ?
                    or a.item_name like ?
                    or p.display_name like ?
                    or p.phone like ?
                )
                """
            )
            params.extend([like, like, like, like, like, like])
        where_sql = "where " + " and ".join(where_parts) if where_parts else ""

        with connect(db_path) as conn:
            total = conn.execute(
                f"""
                select count(*)
                from appointments a
                left join patients p on p.patient_identity = a.patient_identity
                {where_sql}
                """,
                tuple(params),
            ).fetchone()[0]
            rows = conn.execute(
                f"""
                select
                    a.appointment_id, a.patient_identity, p.display_name, p.phone,
                    a.study_identity, a.start_time, a.end_time,
                    a.doctor_name, a.item_name, a.status, a.cancel_reason, a.updated_at
                from appointments a
                left join patients p on p.patient_identity = a.patient_identity
                {where_sql}
                order by coalesce(a.start_time, ''), a.appointment_id
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

    @router.get("/api/appointments/summary")
    def appointment_summary(date: str = "", date_from: str = "", date_to: str = ""):
        """预约统计：按状态/类型/医生计数，供视图顶部可点 chip。
        给 date（单日）或 date_from/date_to（区间）。都不给默认今天。"""
        require_perm("patient.view")   # 预约统计口径同列表,按 patient.view 守卫
        date = date.strip()
        date_from = date_from.strip()
        date_to = date_to.strip()
        if date:
            valid_date_param(date, "date")
            date_from = date_to = date
        if date_from:
            valid_date_param(date_from, "date_from")
        if date_to:
            valid_date_param(date_to, "date_to")
        if not date_from and not date_to:
            date_from = date_to = today_str()
        where_parts, params = [], []
        if date_from:
            where_parts.append("substr(coalesce(start_time, ''), 1, 10) >= ?")
            params.append(date_from)
        if date_to:
            where_parts.append("substr(coalesce(start_time, ''), 1, 10) <= ?")
            params.append(date_to)
        where_sql = "where " + " and ".join(where_parts) if where_parts else ""

        def _counts(group_col):
            with connect(db_path) as conn:
                rows = conn.execute(
                    f"select coalesce({group_col}, '') as k, count(*) as n "
                    f"from appointments {where_sql} group by k order by n desc",
                    tuple(params),
                ).fetchall()
            return [{"key": r["k"], "count": r["n"]} for r in rows]

        return {
            "date_from": date_from, "date_to": date_to,
            "by_status": _counts("status"),
            "by_type": _counts("item_name"),
            "by_doctor": _counts("doctor_name"),
        }

    @router.post("/api/appointments")
    def create_appointment(payload: dict):
        """新建预约。患者必填且须存在；start_time 必填且合法；其余可选。"""
        require_perm("patient.edit")   # 预约写操作按患者编辑权守卫(前台/医生有,库房/消毒无)
        payload = payload or {}
        patient_identity = str(payload.get("patient_identity") or "").strip()
        if not patient_identity:
            raise HTTPException(status_code=400, detail="患者必填")
        start_time = valid_time_field(str(payload.get("start_time") or "").strip(), "start_time")
        if not start_time:
            raise HTTPException(status_code=400, detail="预约时间必填")
        end_time = valid_time_field(str(payload.get("end_time") or "").strip(), "end_time")
        doctor_name = str(payload.get("doctor_name") or "").strip()
        if not doctor_name:
            raise HTTPException(status_code=400, detail="主治医生必填")
        item_name = str(payload.get("item_name") or "").strip()
        if not item_name:
            raise HTTPException(status_code=400, detail="预约项目必填")
        status = str(payload.get("status") or "已预约").strip()
        visit_type = str(payload.get("visit_type") or "").strip()
        register_type = str(payload.get("register_type") or "预约").strip() or "预约"  # 登记方式: 预约/到店
        room = str(payload.get("room") or "").strip()   # P1-3/诊室(椅位)：落库 + 参与冲突检测
        # 待确定预约/咨询师/患者来源/预约人/备注 无独立列，落 source_json(不动 schema)
        src = {"origin": "local"}
        for key in ("appointment_type", "consultant", "patient_source", "booked_by", "remark"):
            val = str(payload.get(key) or "").strip()
            if val:
                src[key] = val
        now = now_str()
        # 到店登记=walk-in,人已在场→直接落「已到诊」+到达时刻,进候诊队列「到达」阶段,
        # 前台不用再手点到达。仅当还没过到达阶段(预约/确认)时升,不覆盖更后的阶段。
        arrived_at = ""
        if register_type == "到店" and status in ("", "已预约", "已确认"):
            status = "已到诊"
            arrived_at = now
        appointment_id = new_id("local-appt")
        with connect(db_path) as conn:
            begin_immediate(conn)   # 审计R2复核:抢写锁使「查患者→写入」原子,合并事务无法插进两步之间(P1竞态窗口)
            require_patient(conn, patient_identity)   # 审计R2:软删/已合并患者不得新建预约
            # P1-3 时段冲突提示(不硬阻断)：同医生 或 同诊室(非空) 时段重叠且未 force → 返回 conflict 二次确认
            # 半开区间：相接(09:30 接 09:30)不算冲突；真重叠或完全同起点才算。
            if not payload.get("force"):
                new_end = end_time or start_time
                day = start_time[:10]
                hit = conn.execute(
                    "select start_time, doctor_name, room from appointments "
                    "where substr(coalesce(start_time,''),1,10) = ? "
                    "and coalesce(status,'') not in ('已取消','3','已爽约','爽约') "
                    "and (doctor_name = ? or (? <> '' and room = ?)) "
                    "and ((? < coalesce(nullif(end_time,''), start_time) and start_time < ?) or start_time = ?) "
                    "limit 1",
                    (day, doctor_name, room, room, start_time, new_end, start_time),
                ).fetchone()
                if hit:
                    who = "诊室" if (room and hit["room"] == room and hit["doctor_name"] != doctor_name) else "医生"
                    return {"conflict": True,
                            "warning": f"该{who}在 {day} 已有时段重叠的预约（{hit['start_time']}），确认仍要新增？"}
            conn.execute(
                "insert into appointments(appointment_id, patient_identity, start_time, end_time, "
                "doctor_name, item_name, status, visit_type, register_type, room, arrived_at, source_json, "
                "created_at, updated_at) "
                "values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (appointment_id, patient_identity, start_time, end_time,
                 doctor_name, item_name, status, visit_type, register_type, room, arrived_at,
                 json.dumps(src, ensure_ascii=False), now, now),   # created_at=now:本地新增(今日工作台「预约」高亮据此)
            )
            audit_write(
                conn, "appointment", appointment_id, "create_appointment",
                new_json=json.dumps({"patient_identity": patient_identity, "start_time": start_time,
                                     "doctor_name": doctor_name, "item_name": item_name},
                                    ensure_ascii=False),
                created_at=now,
            )
            conn.commit()
            created = conn.execute(
                "select a.appointment_id, a.patient_identity, p.display_name, p.phone, a.study_identity, "
                "a.start_time, a.end_time, a.doctor_name, a.item_name, a.status, a.cancel_reason, a.updated_at "
                "from appointments a left join patients p on p.patient_identity = a.patient_identity "
                "where a.appointment_id = ?",
                (appointment_id,),
            ).fetchone()
        return {"appointment": _row_to_dict(created)}

    @router.put("/api/appointments/{appointment_id}")
    def update_appointment(appointment_id: str, payload: dict):
        editable_fields = ["start_time", "end_time", "doctor_name", "item_name", "status", "cancel_reason", "room", "visit_type", "register_type"]
        updates = {
            field: str(payload.get(field) or "").strip()
            for field in editable_fields
            if field in payload
        }
        for field in ("start_time", "end_time"):
            if field in updates:
                updates[field] = valid_time_field(updates[field], field)
        # source_json 内嵌字段(预约类型/咨询师/患者来源/预约人/备注)：双击编辑时一并改,
        # 合并回 source_json(对齐 create_appointment 口径,不动 schema)；实际合并在取到旧行后做。
        src_keys = ("appointment_type", "consultant", "patient_source", "booked_by", "remark")
        src_in = {k: str(payload.get(k) or "").strip() for k in src_keys if k in payload}
        if not updates and not src_in:
            raise HTTPException(status_code=400, detail="no editable fields")

        required_permissions = []
        edit_fields = {
            "start_time", "end_time", "doctor_name", "item_name", "room",
            "visit_type", "register_type", *src_keys,
        }
        if any(field in payload for field in edit_fields):
            required_permissions.append("appointment.edit")
        if "status" in payload:
            submitted_status = str(payload.get("status") or "").strip()
            if submitted_status in {"3", "9", "已取消", "已爽约", "爽约"}:
                required_permissions.append("appointment.cancel")
            else:
                required_permissions.append("appointment.status")
        if "cancel_reason" in payload and "appointment.cancel" not in required_permissions:
            required_permissions.append("appointment.cancel")
        for permission in required_permissions:
            require_access(permission)

        now = now_str()

        with connect(db_path) as conn:
            begin_immediate(conn)   # 抢写锁，防双击/多端并发改预约致丢更新+审计重复
            appointment = conn.execute(
                "select * from appointments where appointment_id = ?",
                (appointment_id,),
            ).fetchone()
            if not appointment:
                raise HTTPException(status_code=404, detail="appointment not found")

            # source_json 内嵌字段合并：取旧 source_json,叠加本次提交的键(空串=清除该键)。
            if src_in:
                try:
                    src_obj = json.loads(appointment["source_json"] or "{}")
                    if not isinstance(src_obj, dict):
                        src_obj = {}
                except (ValueError, TypeError):
                    src_obj = {}
                for k, v in src_in.items():
                    if v:
                        src_obj[k] = v
                    else:
                        src_obj.pop(k, None)
                updates["source_json"] = json.dumps(src_obj, ensure_ascii=False)

            # 取消预约必须留原因（本次提交里给了，或记录上已有）——审计可追溯
            target_status = updates.get("status", appointment["status"])
            if target_status in ("已取消", "3"):   # 数字3也是已取消,一并要求填原因
                target_reason = updates.get("cancel_reason", appointment["cancel_reason"] or "")
                if not str(target_reason).strip():
                    raise HTTPException(status_code=400, detail="取消预约必须填写取消原因")
            # 就诊状态流打时间戳(支持回退清戳)：到达(stage≥2)记 arrived_at、完成治疗(stage≥4)记 finished_at；
            # 回退到更早阶段则清掉对应时间戳，保持口径一致。
            if "status" in updates:
                stage = appt_stage(target_status)
                if stage >= 2:
                    if not (appointment["arrived_at"] or ""):
                        updates["arrived_at"] = now
                elif stage >= 0:        # 回退到到达之前(非取消)→清到达/完成戳
                    updates["arrived_at"] = ""
                if stage >= 4:
                    if not (appointment["finished_at"] or ""):
                        updates["finished_at"] = now
                elif stage >= 0:        # 回退到完成之前 → 清完成戳
                    updates["finished_at"] = ""
                if stage < 0:   # +取消/爽约都未真正完成就诊，清到达/完成戳避免矛盾记录(原只清爽约,漏了取消)
                    updates["arrived_at"] = ""
                    updates["finished_at"] = ""

            # no-op 守卫——只比可编辑字段实际值,无变更不灌版本/不审计(对齐 patient/return_visit;
            # 旧逻辑靠 new_hash!=old_hash,但 new_snapshot 含新 updated_at/edit_source,恒不等→每次都灌)
            changed = {f: v for f, v in updates.items()
                       if str(v if v is not None else "") != str(appointment[f] if appointment[f] is not None else "")}
            if not changed:
                return {"appointment": _row_to_dict(appointment)}

            old_snapshot = appointment_snapshot(appointment)
            old_json = stable_json(old_snapshot)
            old_hash = stable_hash(old_snapshot)
            new_snapshot = dict(old_snapshot)
            new_snapshot.update(updates)
            new_snapshot["updated_at"] = now
            new_snapshot["edit_source"] = "local_appointment_edit"
            new_hash = stable_hash(new_snapshot)

            if new_hash != old_hash:
                conn.execute(
                    """
                    insert into appointment_versions(
                        appointment_id, version_hash, snapshot_json, batch_id
                    )
                    values (?, ?, ?, ?)
                    """,
                    (appointment_id, old_hash, old_json, None),
                )

            conn.execute(
                """
                update appointments
                set start_time = ?,
                    end_time = ?,
                    doctor_name = ?,
                    item_name = ?,
                    status = ?,
                    cancel_reason = ?,
                    room = ?,
                    arrived_at = ?,
                    finished_at = ?,
                    visit_type = ?,
                    register_type = ?,
                    source_json = ?,
                    updated_at = ?
                where appointment_id = ?
                """,
                (
                    updates.get("start_time", appointment["start_time"]),
                    updates.get("end_time", appointment["end_time"]),
                    updates.get("doctor_name", appointment["doctor_name"]),
                    updates.get("item_name", appointment["item_name"]),
                    updates.get("status", appointment["status"]),
                    updates.get("cancel_reason", appointment["cancel_reason"]),
                    updates.get("room", appointment["room"]),
                    updates.get("arrived_at", appointment["arrived_at"]),
                    updates.get("finished_at", appointment["finished_at"]),
                    updates.get("visit_type", appointment["visit_type"]),
                    updates.get("register_type", appointment["register_type"]),
                    updates.get("source_json", appointment["source_json"]),
                    now,
                    appointment_id,
                ),
            )
            audit_write(
                conn, "appointment", appointment_id, "update_appointment",
                old_json=old_json,
                new_json=stable_json(new_snapshot),
                created_at=now,
            )
            conn.commit()
            updated = conn.execute(
                "select * from appointments where appointment_id = ?",
                (appointment_id,),
            ).fetchone()

        return {"appointment": _row_to_dict(updated)}

    @router.get("/api/appointments/{appointment_id}")
    def get_appointment(appointment_id: str):
        """单条预约完整详情(供双击编辑回填)：含 room/visit_type/register_type 及 source_json
        内嵌字段(预约类型/咨询师/患者来源/预约人/备注)、病历号。"""
        require_perm("patient.view")   # 预约详情含患者姓名/电话/病历号,按 patient.view 守卫
        src = "iif(json_valid(a.source_json), a.source_json, '{}')"
        with connect(db_path) as conn:
            row = conn.execute(
                f"""
                select
                    a.appointment_id, a.patient_identity, p.display_name, p.phone,
                    a.study_identity, a.start_time, a.end_time,
                    a.doctor_name, a.item_name, a.status, a.cancel_reason,
                    a.room, a.visit_type, a.register_type, a.updated_at,
                    coalesce(nullif(p.chart_no, ''), json_extract({src}, '$.patientid')) as chart_no,
                    json_extract({src}, '$.appointment_type') as appointment_type,
                    json_extract({src}, '$.consultant')       as consultant,
                    json_extract({src}, '$.patient_source')   as patient_source,
                    json_extract({src}, '$.booked_by')        as booked_by,
                    json_extract({src}, '$.remark')           as remark
                from appointments a
                left join patients p on p.patient_identity = a.patient_identity
                where a.appointment_id = ?
                """,
                (appointment_id,),
            ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="appointment not found")
        return {"appointment": _row_to_dict(row)}

    @router.delete("/api/appointments/{appointment_id}")
    def delete_appointment(appointment_id: str):
        """删除预约(队列误建/作废)：审计留底 old 快照后硬删，并清其版本行。"""
        require_perm("patient.edit")   # 预约写操作按患者编辑权守卫
        now = now_str()
        with connect(db_path) as conn:
            row = conn.execute(
                "select * from appointments where appointment_id = ?", (appointment_id,)
            ).fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="appointment not found")
            # 守卫:该预约患者当天已有处置/收费 → 不可删(删了财务/记录对不上号,对齐原系统"有处置不可删")
            pid = row["patient_identity"]
            day = str(row["start_time"] or "")[:10]
            if pid and day:
                treated = conn.execute(
                    "select 1 from treatment_orders where patient_identity = ? "
                    "and substr(coalesce(order_date,''),1,10) = ? and coalesce(status,'') != 'voided' limit 1",
                    (pid, day),
                ).fetchone()
                paid = conn.execute(
                    "select 1 from payments where patient_identity = ? "
                    "and substr(coalesce(pay_time,''),1,10) = ? and coalesce(amount,0) > 0 "
                    "and coalesce(state,'') != '1' limit 1",
                    (pid, day),
                ).fetchone()
                if treated or paid:
                    raise HTTPException(status_code=409,
                                        detail="该患者当天已有处置或收费记录，不能删除预约（请先撤销相关记录）")
            audit_write(
                conn, "appointment", appointment_id, "delete_appointment",
                old_json=json.dumps(_row_to_dict(row), ensure_ascii=False, default=str),
                created_at=now,
            )
            conn.execute("delete from appointment_versions where appointment_id = ?", (appointment_id,))
            conn.execute("delete from appointments where appointment_id = ?", (appointment_id,))
            conn.commit()
        return {"ok": True}

    return router
