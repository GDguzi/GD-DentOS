"""处置单两步工作流：
  新增处置(recorded 待划价) → 划价(priced 待收费, 生成bill) → 收费(已收费,派生自bill) / 撤销(voided)。
划价支持逐项 收费/免费/打折 + 整单优惠。状态"已收费"在读取时按 bill.paid_fee 派生，不耦合收款端点。"""
import json
import math
import re
from pathlib import Path

from fastapi import APIRouter, HTTPException

from local_app.timeutil import now_str
from local_app.validation import valid_int_qty
from local_app.auth import audit_write, require_perm
from local_app.db import begin_immediate, connect, new_id
from local_app.routes.patient_common import require_patient   # 审计R2:软删患者按不存在处理(共享校验)




def _to_int(v, default=1):
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _to_real(v, default=None):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _audit(conn, entity_id, action, new, now):
    audit_write(
        conn,
        "treatment_order",
        entity_id,
        action,
        new_json=json.dumps(new, ensure_ascii=False),
        created_at=now,
    )


def _apply_pricing(conn, order_id, patient_identity, discount, adj, now):
    """划价核心：逐项计费(免单>直填金额>原价)+整单优惠 → 生成bill、
    回填treatment_items、把处置单置priced。被 price_order 与 create_order(price_now) 共用。
    返回 (bill_id, total_fee)。调用方负责事务提交与前置校验(404/状态)。"""
    items = conn.execute(
        "select treatment_item_id, unit_price, quantity from treatment_items where order_id = ?",
        (order_id,),
    ).fetchall()
    subtotal = 0.0
    updates = []
    for it in items:
        a = adj.get(it["treatment_item_id"]) or {}
        if a.get("unit_price") is not None:
            unit_price = _to_real(a.get("unit_price"))
            # 架构铁律#禁止兜底：非法单价400报错，不静默保留原单价
            if unit_price is None or not math.isfinite(unit_price) or unit_price < 0:
                raise HTTPException(status_code=400, detail="单价必须是非负数字")
        else:
            unit_price = it["unit_price"] or 0.0
        qty = it["quantity"] or 1
        gross = unit_price * qty
        # #1：优先级 免单勾选 > 直填金额 > 不调整按原价(旧charge/discount_rate双轨已删,前端只发free/amount)
        if a.get("free"):
            line = 0.0
        elif a.get("amount") is not None:
            amount = _to_real(a.get("amount"))
            # 架构铁律#禁止兜底：非法金额(负/nan/非数)400报错，不静默退回原价
            if amount is None or not math.isfinite(amount) or amount < 0:
                raise HTTPException(status_code=400, detail="本项金额必须是非负数字")
            line = amount
        else:
            line = gross
        line = round(line, 2)   # #550：逐项取整到分，避免带小数厘的行金额累积成收不齐的账单
        subtotal += line
        updates.append((it["treatment_item_id"], unit_price, line))
    total_fee = round(max(subtotal - discount, 0.0), 2)
    # #27：全免费/抵到0元 → 直接结清(state=paid)，不需走收款；否则待收费 pending
    bill_state = "paid" if total_fee <= 1e-6 else "pending"
    bill_id = new_id("local-bill")
    # 账单编号：年月日(YYMMDD)+当日4位流水，对齐 SaaS 习惯；此前写空串导致收费单"没有账单编号"
    yymmdd = now[2:4] + now[5:7] + now[8:10]
    # 取当日最大流水+1(非 count+1)：避免编号非连续/撤单留洞时撞号
    mx = conn.execute(
        "select max(cast(substr(bill_no, 7) as integer)) from bills where bill_no like ? and length(bill_no) = 10",
        (yymmdd + "%",),
    ).fetchone()[0]
    bill_no = f"{yymmdd}{(int(mx or 0) + 1):04d}"
    conn.execute(
        "insert into bills(bill_id, patient_identity, bill_no, bill_time, total_fee, paid_fee, state, source_json, updated_at) "
        "values (?, ?, ?, ?, ?, 0, ?, ?, ?)",
        (bill_id, patient_identity, bill_no, now, total_fee, bill_state,
         json.dumps({"origin": "local_order", "order_id": order_id, "discount": discount,
                     "subtotal": subtotal}, ensure_ascii=False), now),
    )
    for item_id, unit_price, line in updates:
        conn.execute(
            "update treatment_items set bill_id = ?, unit_price = ?, total_fee = ?, updated_at = ? "
            "where treatment_item_id = ?",
            (bill_id, unit_price, line, now, item_id),
        )
    conn.execute(
        "update treatment_orders set status = 'priced', bill_id = ?, discount = ?, updated_at = ? where order_id = ?",
        (bill_id, discount, now, order_id),
    )
    _audit(conn, order_id, "price_treatment_order",
           {"bill_id": bill_id, "total_fee": total_fee, "discount": discount}, now)
    return bill_id, total_fee


def _find_today_appointment(conn, patient_identity, today):
    """该患者今天的「挂号」=今日未取消/未爽约的预约，取最新一条(优先已到达的)。
    用于：新增处置默认带挂号医生 + today-visit 门槛提示。返回 sqlite Row 或 None。"""
    return conn.execute(
        """
        select appointment_id, doctor_name, status, room, visit_type, start_time, arrived_at, finished_at
        from appointments
        where patient_identity = ?
          and substr(coalesce(start_time, ''), 1, 10) = ?
          and coalesce(status, '') not in ('3', '已取消', '已爽约', '爽约')
        order by (case when coalesce(arrived_at, '') <> '' then 0 else 1 end),
                 coalesce(arrived_at, '') desc, coalesce(start_time, '') desc
        limit 1
        """,
        (patient_identity, today),
    ).fetchone()


def _gen_order_no(conn, now):
    """本地处置单号 CZ+YYMMDD+当日4位流水(避开 SaaS 的 B 号,不与同步撞号)。须在写锁内调用防重号。"""
    yymmdd = now[2:4] + now[5:7] + now[8:10]
    mx = conn.execute(
        "select max(cast(substr(order_no, 9) as integer)) from treatment_orders "
        "where order_no like ? and length(order_no) = 12",
        ("CZ" + yymmdd + "%",),
    ).fetchone()[0]
    return f"CZ{yymmdd}{(int(mx or 0) + 1):04d}"


def _parse_order_lines(payload):
    """解析处置项 → lines(新建/编辑共用)。空名项跳过;全空或数量/单价非法抛 400。"""
    raw_items = (payload or {}).get("items")
    if not isinstance(raw_items, list) or not raw_items:
        raise HTTPException(status_code=400, detail="处置单至少需要一个处置项")
    lines = []
    for raw in raw_items:
        if raw is not None and not isinstance(raw, dict):
            raise HTTPException(status_code=400, detail="items 元素必须是对象")
        raw = raw or {}
        name = str(raw.get("item_name") or "").strip()
        if not name:
            continue
        qty = valid_int_qty(raw.get("quantity"), 1)   # #557:非整数数量拒绝报错,不静默按1/截断
        unit_price = _to_real(raw.get("unit_price"), 0.0) or 0.0
        if qty <= 0:
            raise HTTPException(status_code=400, detail="数量必须大于 0")
        if not math.isfinite(unit_price) or unit_price < 0:
            raise HTTPException(status_code=400, detail="单价无效")
        lines.append({
            "treatment_item_id": new_id("local-ti"),
            "item_name": name,
            "item_code": str(raw.get("item_code") or raw.get("handle_id") or "").strip(),
            "tooth": str(raw.get("tooth") or "").strip(),
            "unit_price": unit_price,
            "quantity": qty,
            "fee_type": str(raw.get("fee_type") or "").strip(),  # #7 费用分类
        })
    if not lines:
        raise HTTPException(status_code=400, detail="处置项均为空")
    return lines


def create_treatment_orders_router(db_path):
    router = APIRouter()
    db_path = Path(db_path)

    @router.get("/api/patients/{patient_identity}/today-visit")
    def today_visit(patient_identity: str):
        """该患者今天是否已挂号(今日未取消预约)，给新增处置做"先挂号"门槛/默认医生。"""
        require_perm("treatment.manage")   # #482：处置相关读守卫用本模块对应权限
        today = now_str()[:10]
        with connect(db_path) as conn:
            require_patient(conn, patient_identity)   # 审计R2:软删/已合并患者按不存在处理
            row = _find_today_appointment(conn, patient_identity, today)
        if not row:
            return {"has_today": False}
        return {
            "has_today": True,
            "appointment_id": row["appointment_id"],
            "doctor_name": row["doctor_name"] or "",
            "status": row["status"] or "",
            "room": row["room"] or "",
            "visit_type": row["visit_type"] or "",
            # 已到达(arrived_at 有值)=可开处置；前端据此自动摆一张空壳待录处置单(到达才出)
            "arrived": bool((row["arrived_at"] or "").strip()),
        }

    # ---------- 新增处置（待划价，不出收费单） ----------
    @router.post("/api/patients/{patient_identity}/treatment-orders")
    def create_order(patient_identity: str, payload: dict):
        require_perm("treatment.manage")
        payload = payload or {}
        lines = _parse_order_lines(payload)

        now = now_str()
        order_id = new_id("local-ord")
        doctor = str(payload.get("doctor_name") or "").strip()
        # #4 配台人员（护士/咨询师/助理）
        nurse = str(payload.get("nurse_name") or "").strip()
        consultant = str(payload.get("consultant_name") or "").strip()
        assistant = str(payload.get("assistant_name") or "").strip()
        with connect(db_path) as conn:
            begin_immediate(conn)   # 抢写锁，防 price_now 并发开单致当日 bill_no 重号(与 price_order 一致)
            require_patient(conn, patient_identity)   # 审计R2:软删/已合并患者按不存在处理
            # 处置医生默认 = 今日挂号医生(没手选时兜底取，对齐"先挂号后处置")
            if not doctor:
                reg = _find_today_appointment(conn, patient_identity, now[:10])
                if reg and str(reg["doctor_name"] or "").strip():
                    doctor = str(reg["doctor_name"]).strip()
            order_no = _gen_order_no(conn, now)   # 本地处置单号(锁内取号防重号)
            conn.execute(
                "insert into treatment_orders(order_id, order_no, patient_identity, order_date, doctor_name, "
                "nurse_name, consultant_name, assistant_name, "
                "diagnosis, status, bill_id, discount, created_at, updated_at) "
                "values (?, ?, ?, ?, ?, ?, ?, ?, ?, 'recorded', '', 0, ?, ?)",
                (order_id, order_no, patient_identity, now, doctor, nurse, consultant, assistant,
                 str(payload.get("diagnosis") or "").strip(), now, now),
            )
            for ln in lines:
                conn.execute(
                    "insert into treatment_items(treatment_item_id, patient_identity, order_id, bill_id, "
                    "item_name, item_code, doctor_name, unit_price, quantity, total_fee, fee_type, "
                    "tooth_codes, source_json, updated_at) "
                    "values (?, ?, ?, '', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (ln["treatment_item_id"], patient_identity, order_id, ln["item_name"], ln["item_code"],
                     doctor, ln["unit_price"], ln["quantity"], ln["unit_price"] * ln["quantity"],
                     ln["fee_type"], ln["tooth"],   # 扫荡#390:牙位写进 tooth_codes 规范列(不只 source_json)
                     json.dumps({"origin": "local_order", "tooth": ln["tooth"]}, ensure_ascii=False), now),
                )
            _audit(conn, order_id, "create_treatment_order",
                   {"patient": patient_identity, "item_count": len(lines)}, now)
            # #3：开单并划价 → 建单后直接按全价生成待收费单，一步到位
            if payload.get("price_now"):
                bill_id, total_fee = _apply_pricing(conn, order_id, patient_identity, 0.0, {}, now)
                conn.commit()
                return {"order_id": order_id, "order_no": order_no, "status": "priced", "bill_id": bill_id,
                        "total_fee": total_fee, "item_count": len(lines)}
            conn.commit()
        return {"order_id": order_id, "order_no": order_no, "status": "recorded", "item_count": len(lines)}

    # ---------- 到达自动建空号单（替代纯界面空壳）：到店即生成一张带号的空待划价单 ----------
    @router.post("/api/patients/{patient_identity}/treatment-orders/ensure-arrival")
    def ensure_arrival_order(patient_identity: str):
        require_perm("treatment.manage")
        now = now_str()
        today = now[:10]
        with connect(db_path) as conn:
            begin_immediate(conn)   # 抢写锁:并发/重复请求只建一张,防重号防重建
            require_patient(conn, patient_identity)   # 审计R2:软删/已合并患者按不存在处理
            appt = _find_today_appointment(conn, patient_identity, today)
            if not appt or not str(appt["arrived_at"] or "").strip():
                return {"created": False, "reason": "未到达"}   # 没到达不生成
            # 仅"已离开"(患者真离店)不再自动建空单；"已完成"(治疗做完但还没离开)仍要有空处置单录治疗。
            # (用户:挂号到达后就该有处置单可选,哪怕没选离开也有一张空单;只有离店才收。)
            if str(appt["status"] or "").strip() in ("已离开", "患者离开"):
                return {"created": False, "reason": "已离开"}
            # 今日已有任何未撤销单(待划价/待收费/已收费)→ 不再自动建空单。
            # (修bug:旧逻辑只看"待划价",划价后单子变"待收费"就误判为没单,又冒一张空单。)
            # 优先返回"待划价"单(供前端复用编辑);只有已划价/已收费单则 created=False 不建;全撤销或无单才建。
            cur = conn.execute(
                "select order_id, order_no from treatment_orders "
                "where patient_identity = ? and substr(coalesce(order_date,''),1,10) = ? and status != 'voided' "
                "order by case when status = 'recorded' then 0 else 1 end, created_at desc limit 1",
                (patient_identity, today)).fetchone()
            if cur:
                return {"created": False, "order_id": cur["order_id"], "order_no": cur["order_no"]}
            # 建空号单(无明细)
            order_id = new_id("local-ord")
            order_no = _gen_order_no(conn, now)
            doctor = str(appt["doctor_name"] or "").strip()
            conn.execute(
                "insert into treatment_orders(order_id, order_no, patient_identity, order_date, doctor_name, "
                "nurse_name, consultant_name, assistant_name, diagnosis, status, bill_id, discount, created_at, updated_at) "
                "values (?, ?, ?, ?, ?, '', '', '', '', 'recorded', '', 0, ?, ?)",
                (order_id, order_no, patient_identity, now, doctor, now, now))
            _audit(conn, order_id, "create_arrival_order", {"order_no": order_no, "empty": True}, now)
            conn.commit()
        return {"created": True, "order_id": order_id, "order_no": order_no}

    # ---------- 编辑待划价处置单（原地改项目,不必撤销重建,避免堆重复单） ----------
    @router.put("/api/treatment-orders/{order_id}")
    def edit_order(order_id: str, payload: dict):
        require_perm("treatment.manage")
        payload = payload or {}
        lines = _parse_order_lines(payload)
        now = now_str()
        with connect(db_path) as conn:
            begin_immediate(conn)   # 抢写锁，防编辑与划价/撤销并发
            order = conn.execute(
                "select order_id, order_date, patient_identity, status, doctor_name, "
                "nurse_name, consultant_name, assistant_name, diagnosis "
                "from treatment_orders where order_id = ?", (order_id,)
            ).fetchone()
            if not order:
                raise HTTPException(status_code=404, detail="处置单不存在")
            if order["status"] != "recorded":
                raise HTTPException(status_code=409, detail="仅待划价处置单可编辑（已划价/已撤销不可改）")
            patient_identity = order["patient_identity"]
            # 离开锁定:今日单且患者"已离开"(真离店)→不可再编辑(划价/收费仍可)。
            # 与 ensure-arrival 同口径按 status 判,而非 finished_at(已完成 stage≥4 即置,会误锁未离开的已完成单)。
            if (order["order_date"] or "")[:10] == now[:10]:
                _appt = _find_today_appointment(conn, patient_identity, now[:10])
                if _appt and str(_appt["status"] or "").strip() in ("已离开", "患者离开"):
                    raise HTTPException(status_code=409, detail="患者已离开，处置单已关闭，不能再编辑（如需修改请先回退就诊状态）")
            # #231/#251:已绑未作废知情同意书的处置单禁止直接改项目——签署正文会与实际处置错配。
            # 文书"签了不能改,改只能作废重签",所以要先作废对应同意书才能改项目。
            if conn.execute(
                "select 1 from consent_documents where order_id = ? and status != 'voided' limit 1",
                (order_id,)
            ).fetchone():
                raise HTTPException(status_code=409, detail="该处置单已签知情同意书，不能直接改项目（请先作废对应同意书再改，避免同意书与实际处置错配）")

            def _keep(key, existing):   # payload 未给该字段则保留原值,不被空串清掉
                v = payload.get(key)
                return str(v).strip() if v is not None else str(existing or "").strip()
            doctor = _keep("doctor_name", order["doctor_name"])
            # 替换明细：待划价单的项尚未绑账单(bill_id=''),整批换掉
            conn.execute("delete from treatment_items where order_id = ? and coalesce(bill_id, '') = ''",
                         (order_id,))
            for ln in lines:
                conn.execute(
                    "insert into treatment_items(treatment_item_id, patient_identity, order_id, bill_id, "
                    "item_name, item_code, doctor_name, unit_price, quantity, total_fee, fee_type, "
                    "tooth_codes, source_json, updated_at) "
                    "values (?, ?, ?, '', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (ln["treatment_item_id"], patient_identity, order_id, ln["item_name"], ln["item_code"],
                     doctor, ln["unit_price"], ln["quantity"], ln["unit_price"] * ln["quantity"],
                     ln["fee_type"], ln["tooth"],   # 扫荡#390:牙位写进 tooth_codes 规范列(不只 source_json)
                     json.dumps({"origin": "local_order", "tooth": ln["tooth"]}, ensure_ascii=False), now),
                )
            conn.execute(
                "update treatment_orders set doctor_name = ?, nurse_name = ?, consultant_name = ?, "
                "assistant_name = ?, diagnosis = ?, updated_at = ? where order_id = ?",
                (doctor, _keep("nurse_name", order["nurse_name"]),
                 _keep("consultant_name", order["consultant_name"]),
                 _keep("assistant_name", order["assistant_name"]),
                 _keep("diagnosis", order["diagnosis"]), now, order_id),
            )
            _audit(conn, order_id, "edit_treatment_order", {"item_count": len(lines)}, now)
            conn.commit()
        return {"order_id": order_id, "status": "recorded", "item_count": len(lines)}

    # ---------- 处置单列表（含派生状态） ----------
    @router.get("/api/patients/{patient_identity}/treatment-orders")
    def list_orders(patient_identity: str):
        require_perm("treatment.manage")   # #482：处置单读守卫用本模块对应权限
        with connect(db_path) as conn:
            require_patient(conn, patient_identity)   # 审计R2:软删/已合并患者按不存在处理
            # 患者今日就诊是否"已离开"(真离店)→今日待划价单锁定不可编辑(回退状态则自动解锁)。
            # 按 status 判而非 finished_at:已完成(未离开)仍要能编辑录治疗(#266,与 ensure-arrival/edit 同口径)。
            today = now_str()[:10]
            today_appt = _find_today_appointment(conn, patient_identity, today)
            visit_finished = bool(today_appt and str(today_appt["status"] or "").strip() in ("已离开", "患者离开"))
            orows = conn.execute(
                "select order_id, order_no, order_date, doctor_name, nurse_name, consultant_name, assistant_name, "
                "diagnosis, status, bill_id, discount "
                "from treatment_orders where patient_identity = ? order by created_at desc",
                (patient_identity,),
            ).fetchall()
            irows = conn.execute(
                "select i.order_id, i.treatment_item_id, i.item_name, i.item_code, "
                "i.unit_price, i.quantity, i.total_fee, i.fee_type, i.source_json "
                "from treatment_items i "
                "join treatment_orders o on o.order_id = i.order_id "
                "where o.patient_identity = ? and i.order_id != '' and i.is_deleted = 0 "
                "order by i.order_id, i.treatment_item_id",
                (patient_identity,),
            ).fetchall()
            bill_states = {}
            for b in conn.execute(
                "select b.bill_id, b.total_fee, b.paid_fee, b.state from bills b "
                "join treatment_orders o on o.bill_id = b.bill_id where o.patient_identity = ?",
                (patient_identity,),
            ).fetchall():
                bill_states[b["bill_id"]] = b
        items_by_order = {}
        for r in irows:
            try:
                tooth = (json.loads(r["source_json"] or "{}") or {}).get("tooth", "")
            except (ValueError, TypeError):
                tooth = ""
            items_by_order.setdefault(r["order_id"], []).append({
                "item_id": r["treatment_item_id"], "item_name": r["item_name"],
                "item_code": r["item_code"], "tooth": tooth, "fee_type": r["fee_type"] or "",
                "unit_price": r["unit_price"], "quantity": r["quantity"], "total_fee": r["total_fee"],
            })
        orders = []
        for r in orows:
            eff = r["status"]
            if r["status"] == "priced" and r["bill_id"] in bill_states:
                b = bill_states[r["bill_id"]]
                # #379:账单已退费 → 处置单 effective_status=refunded(否则停在"待收费"还显示撤销)
                if (b["state"] or "") == "refunded":
                    eff = "refunded"
                else:
                    fully_paid = (b["paid_fee"] or 0) + 1e-6 >= (b["total_fee"] or 0) and (b["total_fee"] or 0) > 0
                    if b["state"] == "paid" or fully_paid:
                        eff = "paid"
            b = bill_states.get(r["bill_id"])
            bill_total = (b["total_fee"] or 0) if b else 0
            bill_paid = (b["paid_fee"] or 0) if b else 0
            orders.append({
                "order_id": r["order_id"], "order_no": r["order_no"] or "", "order_date": r["order_date"], "doctor_name": r["doctor_name"],
                "nurse_name": r["nurse_name"] or "", "consultant_name": r["consultant_name"] or "",
                "assistant_name": r["assistant_name"] or "",
                "diagnosis": r["diagnosis"], "status": r["status"], "effective_status": eff,
                "bill_id": r["bill_id"], "discount": r["discount"],
                "bill_total": bill_total, "bill_paid": bill_paid,
                "bill_remaining": round(bill_total - bill_paid, 2),
                # 离开锁定:今日的待划价单,患者已离开(就诊结束)则不可再编辑
                "closed": bool(r["status"] == "recorded" and (r["order_date"] or "")[:10] == today and visit_finished),
                "items": items_by_order.get(r["order_id"], []),
            })
        return {"orders": orders, "totalcount": len(orders)}

    # ---------- 划价（待收费）：逐项收费/免费/打折 + 整单优惠 → 生成 bill ----------
    @router.post("/api/treatment-orders/{order_id}/price")
    def price_order(order_id: str, payload: dict):
        require_perm("treatment.manage")
        payload = payload or {}
        # 架构铁律#禁止兜底：整单优惠非法(负/非数/inf)400报错，不静默当0
        if payload.get("discount") in (None, ""):
            discount = 0.0
        else:
            discount = _to_real(payload.get("discount"))
            if discount is None or not math.isfinite(discount) or discount < 0:
                raise HTTPException(status_code=400, detail="整单优惠必须是非负数字")
        adj = {}
        for a in (payload.get("items") or []):
            if isinstance(a, dict) and a.get("item_id"):
                adj[a["item_id"]] = a
        now = now_str()
        with connect(db_path) as conn:
            begin_immediate(conn)   # 抢写锁，防双击/多设备并发划价致重复生成收费单(重复计费)
            order = conn.execute(
                "select order_id, patient_identity, status, bill_id from treatment_orders where order_id = ?",
                (order_id,),
            ).fetchone()
            if not order:
                raise HTTPException(status_code=404, detail="处置单不存在")
            if order["status"] != "recorded":
                raise HTTPException(status_code=409, detail="该处置单已划价或已撤销")
            # 空单(无明细)不能划价,否则生成0元空账单;先录处置项目
            if not conn.execute(
                "select 1 from treatment_items where order_id = ? and coalesce(is_deleted, 0) = 0 limit 1",
                (order_id,)).fetchone():
                raise HTTPException(status_code=409, detail="空处置单不能划价，请先录入处置项目")
            bill_id, total_fee = _apply_pricing(
                conn, order_id, order["patient_identity"], discount, adj, now)
            conn.commit()
        return {"order_id": order_id, "status": "priced", "bill_id": bill_id,
                "total_fee": total_fee, "discount": discount}

    def _void_order_tx(conn, order, reason, now):
        """撤销处置单核心(被按order_id/按bill_id两个端点共用)：已撤销/已收费抛异常；
        否则作废关联收费单、置voided、软删明细、审计留撤销理由。"""
        if order["status"] == "voided":
            raise HTTPException(status_code=409, detail="处置单已撤销")
        if order["bill_id"]:
            bill = conn.execute("select paid_fee, state from bills where bill_id = ?", (order["bill_id"],)).fetchone()
            # 审查修#2/#3/#12：已收费(paid_fee>0)、已退费(state=refunded)、零元已结清(state=paid)都不能撤销。
            # 否则退费终态被覆盖成 voided、孤儿负流水;或绕过"已收费不能撤销"红线。待收款(pending)单仍可撤。
            if bill and ((bill["paid_fee"] or 0) > 0 or (bill["state"] or "") in ("paid", "refunded")):
                raise HTTPException(status_code=409, detail="已收费/已退费的处置单不能撤销")
            conn.execute("update bills set state = 'voided', updated_at = ? where bill_id = ?", (now, order["bill_id"]))
        conn.execute("update treatment_orders set status = 'voided', updated_at = ? where order_id = ?", (now, order["order_id"]))
        conn.execute("update treatment_items set is_deleted = 1, updated_at = ? where order_id = ?", (now, order["order_id"]))
        _audit(conn, order["order_id"], "void_treatment_order",
               {"voided": True, "reason": reason}, now)

    # ---------- 撤销（待划价/待收费未付可撤；已收费拒绝） ----------
    @router.post("/api/treatment-orders/{order_id}/void")
    def void_order(order_id: str, payload: dict = None):
        require_perm("treatment.manage")
        now = now_str()
        reason = str((payload or {}).get("reason") or "").strip()
        with connect(db_path) as conn:
            begin_immediate(conn)   # 抢写锁，防双击/多设备并发撤销致重复作废/孤儿流水
            order = conn.execute(
                "select order_id, status, bill_id from treatment_orders where order_id = ?", (order_id,)
            ).fetchone()
            if not order:
                raise HTTPException(status_code=404, detail="处置单不存在")
            if not reason:  # #48：撤销必须填理由，API 也不能绕过
                raise HTTPException(status_code=400, detail="撤销必须填写理由")
            _void_order_tx(conn, order, reason, now)
            conn.commit()
        return {"order_id": order_id, "status": "voided"}

    # ---------- 撤销（收费处入口：按账单找处置单撤销） ----------
    @router.post("/api/bills/{bill_id}/void-order")
    def void_order_by_bill(bill_id: str, payload: dict = None):
        require_perm("treatment.manage")
        now = now_str()
        reason = str((payload or {}).get("reason") or "").strip()
        with connect(db_path) as conn:
            begin_immediate(conn)   # 抢写锁，防双击/多设备并发撤销致重复作废/孤儿流水
            order = conn.execute(
                "select order_id, status, bill_id from treatment_orders where bill_id = ?", (bill_id,)
            ).fetchone()
            if not order:
                raise HTTPException(status_code=404, detail="该账单无可撤销的本地处置单")
            if not reason:  # #48：撤销必须填理由
                raise HTTPException(status_code=400, detail="撤销必须填写理由")
            _void_order_tx(conn, order, reason, now)
            conn.commit()
        return {"order_id": order["order_id"], "bill_id": bill_id, "status": "voided"}

    return router
