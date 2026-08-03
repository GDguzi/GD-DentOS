import json
import math
from pathlib import Path

from fastapi import APIRouter, HTTPException

from local_app.timeutil import now_str
from local_app.auth import audit_write, require_perm
from local_app.db import begin_immediate, connect, init_db, new_id
from local_app.routes.patient_common import require_patient   # 审计R2:软删患者按不存在处理(共享校验)
from local_app.validation import valid_int_qty   # #557:数量非整数拒绝报错(与处置单同口径)

# 复审#9：官方四段流转——确认/完成 + 撤回确认(confirmed→draft)/撤回完成(done→confirmed)。
_ALLOWED_TRANSITIONS = {
    "draft": {"confirmed"},
    "confirmed": {"done", "draft"},   # done=完成；draft=撤回确认
    "done": {"confirmed"},            # confirmed=撤回完成
}
_TARGET_STATES = {"confirmed", "done", "draft"}




def _to_int(v, default=0):
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _to_real(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _parse_plan_item(raw, sort_order):
    """单个处置项 → 规范化 dict；item_name 为空返回 None（跳过）。"""
    if raw is not None and not isinstance(raw, dict):
        raise HTTPException(status_code=400, detail="items 元素必须是对象")
    raw = raw or {}
    name = str(raw.get("item_name") or "").strip()
    if not name:
        return None
    qty = valid_int_qty(raw.get("quantity"), 1)   # #557:非整数数量拒绝报错
    if qty <= 0:                     # #110 负/零数量回退为 1
        qty = 1
    unit_price = _to_real(raw.get("unit_price"))
    if unit_price is not None and (not math.isfinite(unit_price) or unit_price < 0):
        unit_price = None            # #110 负/nan 单价丢弃,留待划价时再填,不存进库污染后续
    line_total = _to_real(raw.get("total_price"))
    if line_total is not None and (not math.isfinite(line_total) or line_total < 0):
        line_total = None
    if line_total is None and unit_price is not None:
        line_total = unit_price * qty
    return {
        "item_id": new_id("local-pitem"),
        "group_id": "",
        "handle_id": str(raw.get("handle_id") or "").strip(),
        "item_name": name,
        "tooth": str(raw.get("tooth") or "").strip(),
        "quantity": qty,
        "unit_price": unit_price,
        "total_price": line_total,
        "sort_order": sort_order,
        "note": str(raw.get("note") or "").strip(),
    }


def _plan_total(items, group_type, group_selected):
    """总价口径：都做组/未分组全计；二选一组只计选中项。
    group_type: {group_id: 'all'|'alt'}；group_selected: {group_id: selected_item_id}。"""
    total = 0.0
    for it in items:
        line = it["total_price"]
        if line is None:
            continue
        gid = it["group_id"]
        if gid and group_type.get(gid) == "alt":
            if group_selected.get(gid) == it["item_id"]:
                total += line
        else:
            total += line
    return total


def _recompute_plan_total(conn, plan_id):
    """从库里读该计划的处置项与分组，按总价口径重算（都做全计 / 二选一只计选中）。"""
    item_rows = conn.execute(
        "select item_id, group_id, total_price from treatment_plan_items where plan_id = ?",
        (plan_id,),
    ).fetchall()
    group_rows = conn.execute(
        "select group_id, group_type, selected_item_id from treatment_plan_groups where plan_id = ?",
        (plan_id,),
    ).fetchall()
    items = [
        {"item_id": r["item_id"], "group_id": r["group_id"], "total_price": r["total_price"]}
        for r in item_rows
    ]
    group_type = {r["group_id"]: r["group_type"] for r in group_rows}
    group_selected = {r["group_id"]: r["selected_item_id"] for r in group_rows}
    return _plan_total(items, group_type, group_selected)


def create_treatment_plans_router(db_path):
    router = APIRouter()
    db_path = Path(db_path)

    @router.get("/api/patients/{patient_identity}/treatment-plans")
    def list_treatment_plans(patient_identity: str):
        require_perm("medical_record.view")   # 越权:计划含诊断/治疗目标/价格,无权不可读
        with connect(db_path) as conn:
            require_patient(conn, patient_identity)
            plan_rows = conn.execute(
                """
                select plan_id, plan_name, plan_date, doctor_name, status,
                       total_price, note, category, label, diagnosis,
                       treatment_goals, precautions, created_at, updated_at
                from treatment_plans
                where patient_identity = ? and coalesce(is_deleted, 0) = 0
                order by coalesce(plan_date, '') desc, created_at desc
                """,
                (patient_identity,),
            ).fetchall()
            item_rows = conn.execute(
                """
                select i.item_id, i.plan_id, i.group_id, i.handle_id, i.item_name,
                       i.tooth, i.quantity, i.unit_price, i.total_price, i.sort_order, i.note
                from treatment_plan_items i
                join treatment_plans p on p.plan_id = i.plan_id
                where p.patient_identity = ?
                order by i.plan_id, i.sort_order, i.item_id
                """,
                (patient_identity,),
            ).fetchall()
            group_rows = conn.execute(
                """
                select g.group_id, g.plan_id, g.group_name, g.group_type,
                       g.selected_item_id, g.sort_order
                from treatment_plan_groups g
                join treatment_plans p on p.plan_id = g.plan_id
                where p.patient_identity = ?
                order by g.plan_id, g.sort_order, g.group_id
                """,
                (patient_identity,),
            ).fetchall()

        items_by_plan = {}
        items_by_group = {}
        for r in item_rows:
            item = {
                "item_id": r["item_id"],
                "group_id": r["group_id"],
                "handle_id": r["handle_id"],
                "item_name": r["item_name"],
                "tooth": r["tooth"],
                "quantity": r["quantity"],
                "unit_price": r["unit_price"],
                "total_price": r["total_price"],
                "note": r["note"],
            }
            # items 顶层只放未分组项（兼容老接口）；分组项归到各自 group 下
            if r["group_id"]:
                items_by_group.setdefault(r["group_id"], []).append(item)
            else:
                items_by_plan.setdefault(r["plan_id"], []).append(item)
        groups_by_plan = {}
        for r in group_rows:
            groups_by_plan.setdefault(r["plan_id"], []).append(
                {
                    "group_id": r["group_id"],
                    "group_name": r["group_name"],
                    "group_type": r["group_type"],
                    "selected_item_id": r["selected_item_id"],
                    "items": items_by_group.get(r["group_id"], []),
                }
            )
        plans = [
            {
                "plan_id": r["plan_id"],
                "plan_name": r["plan_name"],
                "plan_date": r["plan_date"],
                "doctor_name": r["doctor_name"],
                "status": r["status"],
                "total_price": r["total_price"],
                "note": r["note"],
                "category": r["category"],
                "label": r["label"],
                "diagnosis": r["diagnosis"],
                "treatment_goals": r["treatment_goals"],
                "precautions": r["precautions"],
                "items": items_by_plan.get(r["plan_id"], []),
                "groups": groups_by_plan.get(r["plan_id"], []),
            }
            for r in plan_rows
        ]
        return {"plans": plans, "totalcount": len(plans)}

    @router.post("/api/patients/{patient_identity}/treatment-plans")
    def create_treatment_plan(patient_identity: str, payload: dict):
        require_perm("treatment.manage")   # 越权:为患者写治疗计划=开单类写操作
        plan_name = str(payload.get("plan_name") or "").strip()
        if not plan_name:
            raise HTTPException(status_code=400, detail="计划名称不能为空")

        now = now_str()
        plan_id = new_id("local-plan")
        items = []
        groups = []
        # 唯一契约:分组结构 groups=[{group_type,group_name,items}](旧顶层items扁平双轨已删,前端只发groups)
        raw_groups = payload.get("groups")
        if not isinstance(raw_groups, list) or not raw_groups:
            raise HTTPException(status_code=400, detail="缺少方案分组 groups")
        # 方案分组（都做/二选一）→ 各组挂自己的处置
        sort = 0
        for gidx, rg in enumerate(raw_groups):
            if not isinstance(rg, dict):
                raise HTTPException(status_code=400, detail="groups 元素必须是对象")
            gtype = str(rg.get("group_type") or "all").strip()
            if gtype not in ("all", "alt"):
                raise HTTPException(status_code=400, detail="group_type 只能是 all 或 alt")
            group_id = new_id("local-pgrp")
            g_count = 0
            for raw in (rg.get("items") or []):
                it = _parse_plan_item(raw, sort)
                if it is None:
                    continue
                it["group_id"] = group_id
                items.append(it)
                sort += 1
                g_count += 1
            if not g_count:
                continue  # 空组不落库
            groups.append({
                "group_id": group_id,
                "group_name": str(rg.get("group_name") or "").strip(),
                "group_type": gtype,
                "selected_item_id": "",
                "sort_order": gidx,
            })
        if not items:
            raise HTTPException(status_code=400, detail="计划处置项均为空")

        group_type = {g["group_id"]: g["group_type"] for g in groups}
        group_selected = {g["group_id"]: g["selected_item_id"] for g in groups}
        total = _plan_total(items, group_type, group_selected)

        with connect(db_path) as conn:
            begin_immediate(conn)   # 审计R2复核:抢写锁使「查患者→写入」原子,合并事务无法插进两步之间(P1竞态窗口)
            require_patient(conn, patient_identity)
            conn.execute(
                """
                insert into treatment_plans(
                    plan_id, patient_identity, plan_name, plan_date, doctor_name,
                    status, total_price, source, note,
                    category, label, diagnosis, treatment_goals, precautions,
                    created_at, updated_at
                )
                values (?, ?, ?, ?, ?, 'draft', ?, 'local', ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    plan_id,
                    patient_identity,
                    plan_name,
                    str(payload.get("plan_date") or "").strip(),
                    str(payload.get("doctor_name") or "").strip(),
                    total,
                    str(payload.get("note") or "").strip(),
                    str(payload.get("category") or "").strip(),          # P2-28
                    str(payload.get("label") or "").strip(),             # P2-28
                    str(payload.get("diagnosis") or "").strip(),         # P2-28
                    str(payload.get("treatment_goals") or "").strip(),   # P2-28
                    str(payload.get("precautions") or "").strip(),       # P2-28
                    now,
                    now,
                ),
            )
            for g in groups:
                conn.execute(
                    """
                    insert into treatment_plan_groups(
                        group_id, plan_id, group_name, group_type,
                        selected_item_id, sort_order, created_at, updated_at
                    )
                    values (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        g["group_id"], plan_id, g["group_name"], g["group_type"],
                        g["selected_item_id"], g["sort_order"], now, now,
                    ),
                )
            for it in items:
                conn.execute(
                    """
                    insert into treatment_plan_items(
                        item_id, plan_id, group_id, handle_id, item_name, tooth,
                        quantity, unit_price, total_price, sort_order, note
                    )
                    values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        it["item_id"], plan_id, it["group_id"], it["handle_id"],
                        it["item_name"], it["tooth"], it["quantity"], it["unit_price"],
                        it["total_price"], it["sort_order"], it["note"],
                    ),
                )
            audit_write(
                conn, "treatment_plan", plan_id, "create_treatment_plan",
                new_json=json.dumps(
                    {"plan_name": plan_name, "total_price": total,
                     "item_count": len(items)},
                    ensure_ascii=False,
                ),
                created_at=now,
            )
            conn.commit()

        return {"plan_id": plan_id, "status": "draft", "total_price": total,
                "item_count": len(items)}

    @router.post("/api/treatment-plans/{plan_id}/status")
    def change_plan_status(plan_id: str, payload: dict):
        require_perm("treatment.manage")   # 越权:改状态会连带删未收费账单,高风险写
        new_status = str(payload.get("status") or "").strip()
        if new_status not in _TARGET_STATES:
            raise HTTPException(status_code=400, detail="目标状态非法")
        with connect(db_path) as conn:
            begin_immediate(conn)   # 抢写锁，防并发状态流转绕过状态机+矛盾审计
            row = conn.execute(
                "select status, bill_id from treatment_plans where plan_id = ? and coalesce(is_deleted, 0) = 0",
                (plan_id,),
            ).fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="计划不存在")
            current = row["status"]
            if new_status not in _ALLOWED_TRANSITIONS.get(current, set()):
                raise HTTPException(
                    status_code=409,
                    detail=f"不能从「{current}」变为「{new_status}」",
                )
            now = now_str()
            # 审计#19：撤回确认(confirmed→draft)若计划已转划价(挂着 bill_id),必须连带撤销账单,
            # 否则计划回 draft 而账单仍在=脱节孤儿。语义=撤销这次转划价(账单是转划价刚生成、本未付):
            # 已收费则拦截(先去收费处退费);未收费则硬删账单+明细+清 bill_id,彻底回到划价前(计划可重新划价)。
            # 不用 state='voided'——那是"作废一张真账单",会进回收站,从回收站还原会再造 pending 孤儿(明细仍软删)。
            if current == "confirmed" and new_status == "draft" and row["bill_id"]:
                bill = conn.execute(
                    "select paid_fee, state from bills where bill_id = ?", (row["bill_id"],)).fetchone()
                if bill and ((bill["paid_fee"] or 0) > 0 or (bill["state"] or "") in ("paid", "refunded")):
                    raise HTTPException(status_code=409,
                                        detail="该计划已收费，不能撤回确认；如需撤销请到收费处退费/撤单")
                # 显式查 payments(不只靠 paid_fee 不变量),挡手工脏数据/历史异常导入的悬空收款流水
                if conn.execute("select 1 from payments where bill_id = ?", (row["bill_id"],)).fetchone():
                    raise HTTPException(status_code=409,
                                        detail="该计划账单已有收款流水，不能撤回确认；请到收费处退费/撤单")
                # 账单曾绑任何知情同意书(含已作废——回收站可还原成 signed 再悬空):硬删会留悬空引用,一律拦截
                if conn.execute("select 1 from consent_documents where bill_id = ?",
                                (row["bill_id"],)).fetchone():
                    raise HTTPException(status_code=409,
                                        detail="该计划账单已绑定知情同意书，不能撤回确认；请改用收费处撤单流程")
                # 账单已有会员储值消费流水(consume 不回写 paid_fee,'已付'守卫盖不住):硬删会留悬空储值流水
                if conn.execute("select 1 from member_transactions where bill_id = ?",
                                (row["bill_id"],)).fetchone():
                    raise HTTPException(status_code=409,
                                        detail="该计划账单已有会员储值消费记录，不能撤回确认；请先处理储值消费")
                conn.execute("delete from treatment_items where bill_id = ?", (row["bill_id"],))
                conn.execute("delete from bills where bill_id = ?", (row["bill_id"],))
                conn.execute("update treatment_plans set bill_id = '' where plan_id = ?", (plan_id,))
            conn.execute(
                "update treatment_plans set status = ?, updated_at = ? where plan_id = ?",
                (new_status, now, plan_id),
            )
            # 确认/完成/撤回是关键医疗+收费决策，必须留痕（项目不变量：版本+审计记录变更）
            audit_write(
                conn, "treatment_plan", plan_id, "change_plan_status",
                old_json=json.dumps({"status": current}, ensure_ascii=False),
                new_json=json.dumps({"status": new_status}, ensure_ascii=False),
                created_at=now,
            )
            conn.commit()
        return {"plan_id": plan_id, "status": new_status}

    @router.post("/api/treatment-plan-groups/{group_id}/select")
    def select_group_option(group_id: str, payload: dict):
        """二选一方案分组：病人选定其中一个处置（或传空 item_id 取消选择）。重算计划总价。"""
        require_perm("treatment.manage")   # 越权:改方案选择会重算计划总价(改钱)
        item_id = str((payload or {}).get("item_id") or "").strip()
        with connect(db_path) as conn:
            begin_immediate(conn)   # 抢写锁，防与转划价跨端点竞态致总价与已出账单脱节
            grow = conn.execute(
                "select plan_id, group_type from treatment_plan_groups where group_id = ?",
                (group_id,),
            ).fetchone()
            if not grow:
                raise HTTPException(status_code=404, detail="方案分组不存在")
            prow = conn.execute(
                "select status, bill_id from treatment_plans where plan_id = ? and coalesce(is_deleted, 0) = 0",
                (grow["plan_id"],),
            ).fetchone()
            if not prow:   # 软删的计划不能再改选方案(否则改了被删计划的总价)
                raise HTTPException(status_code=404, detail="计划不存在")
            # 审查#14：已确认/已划价的计划不能再改二选一(否则 total_price 变了与已生成账单脱节)
            if prow["status"] != "draft" or str(prow["bill_id"] or "").strip():
                raise HTTPException(status_code=409, detail="计划已确认或已划价，不能再改方案选项")
            if grow["group_type"] != "alt":
                raise HTTPException(status_code=400, detail="只有二选一方案分组才能选择")
            if item_id and not conn.execute(
                "select 1 from treatment_plan_items where item_id = ? and group_id = ?",
                (item_id, group_id),
            ).fetchone():
                raise HTTPException(status_code=400, detail="选中项不属于该方案分组")
            now = now_str()
            conn.execute(
                "update treatment_plan_groups set selected_item_id = ?, updated_at = ? where group_id = ?",
                (item_id, now, group_id),
            )
            total = _recompute_plan_total(conn, grow["plan_id"])
            conn.execute(
                "update treatment_plans set total_price = ?, updated_at = ? where plan_id = ?",
                (total, now, grow["plan_id"]),
            )
            conn.commit()
        return {"group_id": group_id, "selected_item_id": item_id, "total_price": total}

    @router.post("/api/treatment-plans/{plan_id}/bill")
    def generate_plan_bill(plan_id: str, payload: dict):
        """转划价：把计划的处置生成一张『待收费』单(bill, state=pending)+划价明细(treatment_items)。
        都做组/未分组全部划入；二选一组只划选中项。医生可整单优惠(discount)、逐项改价(adjustments)。
        不直接收钱——前台另走收款确认。"""
        require_perm("treatment.manage")   # 越权:转划价生成账单+可改价,核心改钱写路径
        payload = payload or {}
        # 架构铁律#禁止兜底：整单优惠非法(负/非数/inf)400报错，不静默当0(对齐处置单划价)
        if payload.get("discount") in (None, ""):
            discount = 0.0
        else:
            discount = _to_real(payload.get("discount"))
            if discount is None or not math.isfinite(discount) or discount < 0:
                raise HTTPException(status_code=400, detail="整单优惠必须是非负数字")
        adjustments = payload.get("adjustments")
        if not isinstance(adjustments, dict):
            adjustments = {}
        with connect(db_path) as conn:
            begin_immediate(conn)   # 抢写锁，防双击/多设备并发转划价致重复生成收费单(重复计费)
            plan = conn.execute(
                "select plan_id, patient_identity, doctor_name, status, bill_id "
                "from treatment_plans where plan_id = ? and coalesce(is_deleted, 0) = 0",
                (plan_id,),
            ).fetchone()
            if not plan:
                raise HTTPException(status_code=404, detail="治疗计划不存在")
            # #16：必须先确认方案才能转划价（草稿不能直接进收费，遵守状态机）
            if plan["status"] != "confirmed":
                raise HTTPException(status_code=409, detail="计划未确认，不能转划价（请先确认方案）")
            # #17：防重复划价——已生成过收费单的计划不再重复生成
            if plan["bill_id"]:
                raise HTTPException(status_code=409, detail="该计划已转划价，不能重复生成收费单")
            item_rows = conn.execute(
                """
                select item_id, group_id, handle_id, item_name, tooth,
                       quantity, unit_price, total_price
                from treatment_plan_items where plan_id = ?
                order by sort_order, item_id
                """,
                (plan_id,),
            ).fetchall()
            group_rows = conn.execute(
                "select group_id, group_type, selected_item_id from treatment_plan_groups where plan_id = ?",
                (plan_id,),
            ).fetchall()
            gtype = {r["group_id"]: r["group_type"] for r in group_rows}
            gsel = {r["group_id"]: r["selected_item_id"] for r in group_rows}
            billable = [
                r for r in item_rows
                if not (r["group_id"] and gtype.get(r["group_id"]) == "alt"
                        and gsel.get(r["group_id"]) != r["item_id"])
            ]
            if not billable:
                raise HTTPException(status_code=400, detail="没有可划价的处置（二选一方案未选择）")

            now = now_str()
            doctor = str(payload.get("doctor_name") or plan["doctor_name"] or "").strip()
            bill_id = new_id("local-bill")
            lines = []
            subtotal = 0.0
            for r in billable:
                adj = adjustments.get(r["item_id"]) or {}
                if not isinstance(adj, dict):   # 对齐处置单 isinstance 口径:非 dict 改价条目视为未改价,防 AttributeError 500
                    adj = {}
                if adj.get("unit_price") is not None:
                    unit_price = _to_real(adj.get("unit_price"))
                    # 架构铁律#禁止兜底：非法单价400报错，不静默回退原单价(对齐处置单划价)
                    if unit_price is None or not math.isfinite(unit_price) or unit_price < 0:
                        raise HTTPException(status_code=400, detail="单价必须是非负数字")
                else:
                    stored = r["unit_price"]   # #110 未改价回退库内值时钳正(老数据可能存了负价)
                    unit_price = stored if (stored is not None and math.isfinite(stored) and stored >= 0) else 0.0
                if adj.get("quantity") in (None, ""):
                    qty = r["quantity"] or 1
                    if qty <= 0:   # 老数据可能存了负/零数量
                        qty = 1
                else:
                    qty = valid_int_qty(adj.get("quantity"))   # #557:非整数数量拒绝报错
                    # 架构铁律#禁止兜底：零/负数量400报错，不静默退回原数量(对齐处置单划价)
                    if qty <= 0:
                        raise HTTPException(status_code=400, detail="数量必须大于 0")
                if adj.get("total_fee") is not None:
                    line_fee = _to_real(adj.get("total_fee"))
                    # 架构铁律#禁止兜底：非法金额(负/nan/非数)400报错，不静默退回按单价计
                    if line_fee is None or not math.isfinite(line_fee) or line_fee < 0:
                        raise HTTPException(status_code=400, detail="本项金额必须是非负数字")
                else:
                    line_fee = unit_price * qty
                line_fee = round(line_fee, 2)   # #585：同 #550,逐项取整到分,避免带厘账单收不齐
                subtotal += line_fee
                lines.append({
                    "treatment_item_id": new_id("local-ti"),
                    "item_name": r["item_name"],
                    "item_code": r["handle_id"],
                    "unit_price": unit_price,
                    "quantity": qty,
                    "total_fee": line_fee,
                })
            total_fee = round(max(subtotal - discount, 0.0), 2)   # #585：账单总额取整到分
            # #3：全免/抵到0元 → 直接结清(state=paid)，否则0元单卡在pending无完成路径(同处置单#27)
            bill_state = "paid" if total_fee <= 1e-6 else "pending"
            # 审查#13：账单编号 YYMMDD+当日4位流水(处置单路径已修,计划路径此前写空串→收费单"没有账单编号")
            yymmdd = now[2:4] + now[5:7] + now[8:10]
            mx = conn.execute(
                "select max(cast(substr(bill_no, 7) as integer)) from bills where bill_no like ? and length(bill_no) = 10",
                (yymmdd + "%",),
            ).fetchone()[0]
            bill_no = f"{yymmdd}{(int(mx or 0) + 1):04d}"

            conn.execute(
                """
                insert into bills(
                    bill_id, patient_identity, bill_no, bill_time,
                    total_fee, paid_fee, state, source_json, updated_at
                )
                values (?, ?, ?, ?, ?, 0, ?, ?, ?)
                """,
                (
                    bill_id, plan["patient_identity"], bill_no, now, total_fee, bill_state,
                    json.dumps({"origin": "local_plan", "plan_id": plan_id,
                                "discount": discount, "subtotal": subtotal},
                               ensure_ascii=False),
                    now,
                ),
            )
            for ln in lines:
                conn.execute(
                    """
                    insert into treatment_items(
                        treatment_item_id, patient_identity, bill_id, item_name,
                        item_code, doctor_name, unit_price, quantity, total_fee,
                        source_json, updated_at
                    )
                    values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        ln["treatment_item_id"], plan["patient_identity"], bill_id,
                        ln["item_name"], ln["item_code"], doctor,
                        ln["unit_price"], ln["quantity"], ln["total_fee"],
                        json.dumps({"origin": "local_plan", "plan_id": plan_id},
                                   ensure_ascii=False),
                        now,
                    ),
                )
            conn.execute(
                "update treatment_plans set bill_id = ?, updated_at = ? where plan_id = ?",
                (bill_id, now, plan_id),
            )
            # 计划实体本身被改(挂上 bill_id)→也记一条 plan 审计,供同步冲突守卫(#207)识别本地变更
            audit_write(
                conn, "treatment_plan", plan_id, "generate_plan_bill",
                new_json=json.dumps({"bill_id": bill_id}, ensure_ascii=False),
                created_at=now,
            )
            audit_write(
                conn, "bill", bill_id, "generate_plan_bill",
                new_json=json.dumps(
                    {"plan_id": plan_id, "total_fee": total_fee,
                     "discount": discount, "item_count": len(lines)},
                    ensure_ascii=False,
                ),
                created_at=now,
            )
            conn.commit()
        return {"bill_id": bill_id, "total_fee": total_fee, "discount": discount,
                "item_count": len(lines), "state": bill_state}

    @router.delete("/api/treatment-plans/{plan_id}")
    def delete_treatment_plan(plan_id: str):
        """软删诊疗计划：从列表隐藏，保留行可追溯。已转划价生成的账单不受影响(独立财务记录)。"""
        require_perm("treatment.manage")   # 越权:软删他人计划是写操作
        with connect(db_path) as conn:
            begin_immediate(conn)   # 抢写锁，防 delete 与 转划价/status 并发(删了却仍出账单)
            row = conn.execute(
                "select status, is_deleted from treatment_plans where plan_id = ?", (plan_id,)
            ).fetchone()
            if not row or row["is_deleted"]:
                raise HTTPException(status_code=404, detail="计划不存在")
            now = now_str()
            conn.execute(
                "update treatment_plans set is_deleted = 1, updated_at = ? where plan_id = ?",
                (now, plan_id),
            )
            audit_write(
                conn, "treatment_plan", plan_id, "delete_treatment_plan",
                old_json=json.dumps({"status": row["status"]}, ensure_ascii=False),
                created_at=now,
            )
            conn.commit()
        return {"plan_id": plan_id, "is_deleted": 1}

    return router
