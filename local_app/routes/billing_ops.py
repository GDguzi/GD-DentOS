"""前台收款确认：给待收费单(bill pending/partial)登记收款 → 写 payment + 更新 bill.paid_fee/state。
完成 处置开单→待收费→收款 闭环。只对本地待收费单(state pending/partial)操作。"""
import hashlib
import json
import math
from pathlib import Path

from fastapi import APIRouter, HTTPException

from local_app.timeutil import now_str
from local_app.auth import audit_write, require_perm
from local_app.db import begin_immediate, bill_discount_sql, connect, new_id

_PAYABLE_STATES = {"pending", "partial"}


def _request_key(payload, bill_id, kind):
    """#800:幂等键。request_id 必填(改钱路径,禁止兜底)。
    payload_hash 把目标账单(bill_id,URL 参数不在 body 里)和动作(pay/refund)一并纳入——
    同号复用到别的账单/动作时 hash 不同→409,不会把 A 单的响应重放给 B 单。"""
    rid = str((payload or {}).get("request_id") or "").strip()
    if not rid:
        raise HTTPException(status_code=400, detail="缺少请求号 request_id")
    body = {k: v for k, v in (payload or {}).items() if k != "request_id"}
    keyed = {"bill_id": bill_id, "kind": kind, "body": body}
    phash = hashlib.sha256(
        json.dumps(keyed, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()
    return rid, phash


def _idempotent_replay(conn, rid, phash):
    """事务内查幂等登记：同号同载荷 → 返回首次响应(重放)；同号异载荷 → 409；未登记 → None。"""
    row = conn.execute(
        "select payload_hash, response_json from payment_requests where request_id = ?", (rid,)
    ).fetchone()
    if not row:
        return None
    if row["payload_hash"] != phash:
        raise HTTPException(status_code=409, detail="同一请求号提交了不同内容，请刷新后重试")
    return json.loads(row["response_json"])


def _record_request(conn, rid, bill_id, kind, phash, result, now):
    """成功路径与业务写同事务登记(原子)：崩溃则一起回滚,重试可重新执行。"""
    conn.execute(
        "insert into payment_requests(request_id, bill_id, kind, payload_hash, response_json, created_at) "
        "values (?, ?, ?, ?, ?, ?)",
        (rid, bill_id, kind, phash, json.dumps(result, ensure_ascii=False), now),
    )




def create_billing_ops_router(db_path):
    router = APIRouter()
    db_path = Path(db_path)

    @router.get("/api/bills/{bill_id}")
    def get_bill(bill_id: str):
        """#8 收费弹框：账单 + 逐项明细(单价/数量/总价/减免/费用分类)，供收款弹框展示。"""
        require_perm("billing.view")   # #482：账单明细含金额,按 billing.view 守卫
        with connect(db_path) as conn:
            bill = conn.execute(
                "select bill_id, bill_no, bill_time, total_fee, paid_fee, state, source_json "
                "from bills where bill_id = ?", (bill_id,),
            ).fetchone()
            if not bill:
                raise HTTPException(status_code=404, detail="收费单不存在")
            irows = conn.execute(
                "select treatment_item_id, item_name, unit_price, quantity, total_fee, fee_type, "
                "is_refund, refunded_fee "
                "from treatment_items where bill_id = ? and is_deleted = 0 "
                "order by treatment_item_id", (bill_id,),
            ).fetchall()
            # #584:SaaS 同步单折扣在 source_json.DiscountFee(字符串带千分位逗号),
            # 复用欠费清单同款 SQL 口径解析;本地自建单该值恒 0(折扣已并入 total_fee)。
            saas_discount = float(conn.execute(
                f"select {bill_discount_sql()} d from bills where bill_id = ?", (bill_id,)
            ).fetchone()["d"] or 0)
            refunded = round(-(conn.execute(
                "select coalesce(sum(amount), 0) from payments "
                "where bill_id = ? and payment_record_type = 'refund'", (bill_id,)
            ).fetchone()[0] or 0), 2)
        discount = 0.0
        try:
            discount = float((json.loads(bill["source_json"] or "{}") or {}).get("discount") or 0)
        except (ValueError, TypeError):
            discount = 0.0
        items = []
        for it in irows:
            up = it["unit_price"] or 0
            qty = it["quantity"] or 0
            line = it["total_fee"] or 0
            gross = up * qty
            items.append({
                "treatment_item_id": it["treatment_item_id"],
                "item_name": it["item_name"], "unit_price": up, "quantity": qty,
                "gross": round(gross, 2), "line_fee": round(line, 2),
                "reduce": round(gross - line, 2), "fee_type": it["fee_type"] or "",
                "is_refund": it["is_refund"] or 0,
                "refunded_fee": round(it["refunded_fee"] or 0, 2),   # #802:退费弹窗已退/可退列
            })
        total = bill["total_fee"] or 0
        paid = bill["paid_fee"] or 0
        return {
            "bill_id": bill_id, "bill_no": bill["bill_no"] or "", "bill_time": bill["bill_time"] or "",
            "state": bill["state"], "total_fee": round(total, 2), "paid_fee": round(paid, 2),
            # #584:remaining 与欠费清单 bill_net_arrears_sql 同式(总-SaaS折扣-已收),打折同步单不再显示虚欠
            "remaining": round(total - saas_discount - paid, 2),
            # #813①:退款不再冲减 total/paid,已退与可退从退款流水派生,供退费弹窗封顶
            "refunded_fee": refunded, "refundable": round(paid - refunded, 2),
            "discount": round(discount or saas_discount, 2), "items": items,
        }

    def _parse_amount(raw):
        """金额校验：有限数 → 先 round2 → 再判 >0(否则 0.004 之类四舍五入后落库 0 元,改钱漏洞)。"""
        try:
            amt = float(raw)
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="收款金额无效")
        if not math.isfinite(amt):   # #23 挡 nan/inf
            raise HTTPException(status_code=400, detail="收款金额无效")
        amt = round(amt, 2)          # 先取整到分,再校验,避免 0.004→0.0 落库
        if amt <= 0:
            raise HTTPException(status_code=400, detail="收款金额必须大于 0")
        return amt

    @router.post("/api/bills/{bill_id}/pay")
    def confirm_payment(bill_id: str, payload: dict):
        require_perm("billing.pay")
        payload = payload or {}
        rid, phash = _request_key(payload, bill_id, "pay")   # #800:幂等键必填(含 bill_id/动作)
        cashier = str(payload.get("cashier") or "").strip()  # #8 收银员(本地无登录，手填/记住上次)
        invoice_no = str(payload.get("invoice_no") or "").strip()
        invoice_remark = str(payload.get("invoice_remark") or "").strip()
        remark = str(payload.get("remark") or "").strip()

        # 唯一契约 methods=[{method,amount}]，单方式=一行(旧amount+pay_method双轨已删)。每方式一条 payment 行
        raw_methods = payload.get("methods")
        if not isinstance(raw_methods, list) or not raw_methods:
            raise HTTPException(status_code=400, detail="缺少付款方式明细 methods")
        lines = []   # [(method, amount)]
        for m in raw_methods:
            if not isinstance(m, dict):
                raise HTTPException(status_code=400, detail="付款方式明细格式错误")
            method = str(m.get("method") or "").strip()
            if not method:   # #801:空方式=账目对不上现金/微信桶,任何一行为空整单拒(改钱路径禁止兜底)
                raise HTTPException(status_code=400, detail="付款方式必填")
            lines.append((method, _parse_amount(m.get("amount"))))
        total_amount = round(sum(a for _, a in lines), 2)

        now = now_str()
        with connect(db_path) as conn:
            begin_immediate(conn)   # 抢写锁，防双击/多设备并发收款致重复记账
            replay = _idempotent_replay(conn, rid, phash)   # #800:同号同载荷=重放首次结果,不落第二笔
            if replay is not None:
                return replay
            bill = conn.execute(
                "select bill_id, patient_identity, total_fee, paid_fee, state from bills where bill_id = ?",
                (bill_id,),
            ).fetchone()
            if not bill:
                raise HTTPException(status_code=404, detail="收费单不存在")
            if bill["state"] not in _PAYABLE_STATES:
                raise HTTPException(status_code=409, detail="该收费单不可收款（已结清或非本地待收费单）")
            total = bill["total_fee"] or 0
            paid = bill["paid_fee"] or 0
            remaining = round(total - paid, 2)
            if total_amount > remaining + 1e-6:
                raise HTTPException(status_code=400, detail=f"收款超过待收金额（剩余 {remaining}）")

            new_paid = round(paid + total_amount, 2)
            new_state = "paid" if new_paid + 1e-6 >= total else "partial"
            payment_ids = []
            for method, amt in lines:
                pid = new_id("local-pay")
                payment_ids.append(pid)
                conn.execute(
                    """
                    insert into payments(payment_id, patient_identity, bill_id, pay_time,
                        amount, state, pay_type, payment_record_type,
                        operator, invoice_no, invoice_remark, remark, source_json, updated_at)
                    values (?, ?, ?, ?, ?, ?, ?, 'charge', ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        pid, bill["patient_identity"], bill_id, now, amt, new_state, method,
                        cashier, invoice_no, invoice_remark, remark,
                        json.dumps({"origin": "local_payment", "pay_method": method, "cashier": cashier},
                                   ensure_ascii=False),
                        now,
                    ),
                )
            conn.execute(
                "update bills set paid_fee = ?, state = ?, updated_at = ? where bill_id = ?",
                (new_paid, new_state, now, bill_id),
            )
            audit_write(
                conn, "bill", bill_id, "confirm_payment",
                old_json=json.dumps({"paid_fee": paid, "state": bill["state"]}, ensure_ascii=False),
                new_json=json.dumps({"paid_fee": new_paid, "state": new_state, "amount": total_amount,
                                     "methods": [{"method": mth, "amount": a} for mth, a in lines]},
                                    ensure_ascii=False),
                created_at=now,
            )
            result = {"bill_id": bill_id, "paid_fee": new_paid, "state": new_state,
                      "remaining": round(total - new_paid, 2),
                      "payment_id": payment_ids[0], "payment_ids": payment_ids}
            _record_request(conn, rid, bill_id, "pay", phash, result, now)
            conn.commit()
        return result

    @router.post("/api/bills/{bill_id}/refund")
    def refund_bill(bill_id: str, payload: dict):
        """P0-1 整单退费：仅对本地已结清单(local-bill- 前缀, state=paid)退费。
        #813①:退款只追加负数 payment 流水(payment_record_type=refund),落退款日冲减实收报表；
        bills.total_fee/paid_fee 永不冲减(应收/已收史实不可变,历史报表冻结),退满仅推进 state=refunded。
        可退额度 = paid_fee − Σ已退流水,事务内派生。
        红线：绝不退 SaaS 同步单(非 local-bill- 前缀)。"""
        require_perm("billing_refund")   # P1-13：退费仅 admin
        payload = payload or {}
        rid, phash = _request_key(payload, bill_id, "refund")   # #800:幂等键必填(含 bill_id/动作)
        reason = str(payload.get("refund_reason") or "").strip()
        if not reason:
            raise HTTPException(status_code=400, detail="退费原因必填")
        # 退款方式(现金/微信/...)；退费医生(可选)
        refund_method = str(payload.get("refund_method") or "").strip()
        doctor = str(payload.get("doctor") or "").strip()
        now = now_str()
        with connect(db_path) as conn:
            begin_immediate(conn)   # 抢写锁，防双击/多设备并发退费致重复冲减
            replay = _idempotent_replay(conn, rid, phash)   # #800:同号同载荷=重放,不再退第二笔
            if replay is not None:
                return replay
            bill = conn.execute(
                "select bill_id, patient_identity, total_fee, paid_fee, state from bills where bill_id = ?",
                (bill_id,),
            ).fetchone()
            if not bill:
                raise HTTPException(status_code=404, detail="收费单不存在")
            if not bill_id.startswith("local-bill-"):
                raise HTTPException(status_code=409, detail="仅本地账单可退费，不可退 SaaS 同步单")
            if bill["state"] != "paid":
                raise HTTPException(status_code=409, detail="仅已结清的本地单可退费")
            paid = round(bill["paid_fee"] or 0, 2)
            if paid <= 0:
                raise HTTPException(status_code=409, detail="该单无已收金额可退")
            # #813①:可退额度从退款流水派生(paid_fee 不再被退款冲减)。refund 流水仅本地退款写入。
            refunded_sum = round(-(conn.execute(
                "select coalesce(sum(amount), 0) from payments "
                "where bill_id = ? and payment_record_type = 'refund'",
                (bill_id,)).fetchone()[0] or 0), 2)
            refundable = round(paid - refunded_sum, 2)
            if refundable <= 1e-6:
                raise HTTPException(status_code=409, detail="该单已退完，无可退金额")

            # 三种来源算退款金额：①按处置 items[](逐项)②按整单 refund_amount(部分)③不传=整单全退
            items_in = payload.get("items")
            refund_item_ids = None   # 按处置时记要标 is_refund 的项目
            per_iid = {}             # #802:按处置的精确归属;整单路径为空,走比例分摊
            if isinstance(items_in, list) and items_in:
                # CRITICAL修：①只取未退项(is_refund=0),已退项不能再退;②带出每项金额做单项封顶
                # (旧逻辑只按整单 paid 封顶,可对同一项重复退/跨项超额退,真实资金损失)
                # #573:可退额度=总价-累计已退(部分退过的项目剩余额度仍可继续按项退,退满才出局)
                valid_amt = {r["treatment_item_id"]: round((r["total_fee"] or 0) - (r["refunded_fee"] or 0), 2)
                             for r in conn.execute(
                    "select treatment_item_id, total_fee, refunded_fee from treatment_items "
                    "where bill_id = ? and is_deleted = 0 and coalesce(is_refund, 0) = 0",
                    (bill_id,)).fetchall()}
                refund_item_ids = []
                # 看板#183:per_iid 按 iid 累计,挡"同请求同item拆多行合计超额"
                for it in items_in:
                    iid = str((it or {}).get("treatment_item_id") or "")
                    # #144：每个提交项先严格校验属本单且未退(传错项目/已退项目都挡)
                    if iid not in valid_amt:
                        raise HTTPException(status_code=400, detail="退费项目不属于该账单或已退")
                    raw = (it or {}).get("amount")
                    if raw in (None, ""):
                        continue   # 未填金额=不退该项
                    try:
                        a = round(float(raw), 2)
                    except (TypeError, ValueError):
                        raise HTTPException(status_code=400, detail="退款金额无效")
                    if not math.isfinite(a) or a < 0:
                        raise HTTPException(status_code=400, detail="退款金额无效")
                    if a == 0:
                        continue
                    per_iid[iid] = round(per_iid.get(iid, 0) + a, 2)   # 同 iid 多行累计
                    if per_iid[iid] > valid_amt[iid] + 1e-6:   # 单项封顶(按累计):不能退超过该项目金额
                        raise HTTPException(status_code=400,
                                            detail=f"退款金额超过该项目金额（{valid_amt[iid]}）")
                    if iid not in refund_item_ids:
                        refund_item_ids.append(iid)   # 去重,避免重复标 is_refund
                if not refund_item_ids:
                    raise HTTPException(status_code=400, detail="请至少选择一项并填写退款金额")
                amount = round(sum(per_iid.values()), 2)
                if amount > refundable + 1e-6:
                    raise HTTPException(status_code=400, detail=f"退款金额超过可退金额（可退 {refundable}）")
            elif payload.get("refund_amount") in (None, ""):
                amount = refundable
            else:
                try:
                    amount = round(float(payload.get("refund_amount")), 2)
                except (TypeError, ValueError):
                    raise HTTPException(status_code=400, detail="退款金额无效")
                if not math.isfinite(amount) or amount <= 0:
                    raise HTTPException(status_code=400, detail="退款金额必须大于 0")
                if amount > refundable + 1e-6:
                    raise HTTPException(status_code=400, detail=f"退款金额超过可退金额（可退 {refundable}）")

            # #802:三种退款路径统一在落库时确定项目归属 refund_items——按处置=用户逐项指定(精确);
            # 整单(全退/部分退)=按各项剩余额(总价−已退)比例分摊、余数归最后一项(同 build_cashier_rows
            # 余数手法)。分摊在退款那一刻固化成事实,分类报表不再事后按比例猜。
            item_rows = conn.execute(
                "select treatment_item_id, fee_type, round(coalesce(total_fee,0),2) as line_fee, "
                "round(coalesce(refunded_fee,0),2) as done from treatment_items "
                "where bill_id = ? and is_deleted = 0 order by treatment_item_id",
                (bill_id,)).fetchall()
            if per_iid:
                ftypes = {x["treatment_item_id"]: (x["fee_type"] or "").strip() or "未分类" for x in item_rows}
                refund_items = [{"treatment_item_id": iid, "fee_type": ftypes.get(iid, "未分类"),
                                 "amount": per_iid[iid]} for iid in refund_item_ids]
            else:
                rems = [(x["treatment_item_id"], (x["fee_type"] or "").strip() or "未分类",
                         round(x["line_fee"] - x["done"], 2)) for x in item_rows]
                rems = [x for x in rems if x[2] > 0]
                refund_items = []
                # #802:整数分分摊——逐项向下取整,余数一分一分补给"占比小数部分最大"的项,
                # 且不超各项剩余额。保证 Σ分摊 == 退款额、每项 ≤ 剩余额(逐项独立四舍五入会超退)。
                # 剩余额全 0 的老数据(历史整单退未回填归属)分不了,报表回落比例口径。
                rem_c = [round(x[2] * 100) for x in rems]
                total_c = sum(rem_c)
                if total_c > 0:
                    amt_c = round(amount * 100)
                    base = [amt_c * rc // total_c for rc in rem_c]   # 向下取整
                    order = sorted(range(len(rems)),
                                   key=lambda i: (amt_c * rem_c[i]) % total_c, reverse=True)
                    leftover = amt_c - sum(base)
                    for i in order:
                        if leftover <= 0:
                            break
                        if base[i] < rem_c[i]:   # 不超该项剩余额
                            base[i] += 1
                            leftover -= 1
                    for (iid, ft, _), c in zip(rems, base):
                        if c > 0:
                            refund_items.append({"treatment_item_id": iid, "fee_type": ft,
                                                 "amount": round(c / 100, 2)})

            # #813①(替代旧#143)：total_fee/paid_fee 一律不动——欠款式 total−优惠−paid 对已结清单
            # 恒为 0,退费不冒假欠费；历史应收/实收报表不被未来退款改写。退满推进 refunded 挡再收/再退。
            remaining_refundable = round(refundable - amount, 2)
            new_state = "refunded" if remaining_refundable <= 1e-6 else "paid"
            neg = round(-amount, 2)
            payment_id = new_id("local-refund")
            conn.execute(
                """
                insert into payments(payment_id, patient_identity, bill_id, pay_time,
                    amount, state, pay_type, payment_record_type, source_json, updated_at)
                values (?, ?, ?, ?, ?, 'refunded', ?, 'refund', ?, ?)
                """,
                (
                    payment_id, bill["patient_identity"], bill_id, now, neg, refund_method,
                    # 审查#11/#27：下游(today-collection by_method、患者缴费明细)读 $.pay_method,
                    # 退费此前只写 refund_method→退费金额取不到方式归入'未填',无法冲减真实收款方式。补 pay_method 键。
                    # #802:refund_items=本笔退款的项目归属(流水自带,分类报表精确记类别)
                    json.dumps({"origin": "local_refund", "refund_reason": reason,
                                "pay_method": refund_method, "refund_method": refund_method,
                                "doctor": doctor, "refund_amount": amount,
                                "refund_items": refund_items},
                               ensure_ascii=False),
                    now,
                ),
            )
            conn.execute(
                "update bills set state = ?, updated_at = ? where bill_id = ?",
                (new_state, now, bill_id),
            )
            if refund_items:   # #802:三路径统一按归属累计 refunded_fee,退满(#573 容差)才标已退
                conn.executemany(
                    "update treatment_items set refunded_fee = round(coalesce(refunded_fee,0) + ?, 2), "
                    "is_refund = case when round(coalesce(refunded_fee,0) + ?, 2) >= round(coalesce(total_fee,0), 2) - 0.005 "
                    "then 1 else coalesce(is_refund,0) end, updated_at = ? where treatment_item_id = ?",
                    [(ri["amount"], ri["amount"], now, ri["treatment_item_id"]) for ri in refund_items],
                )
            if new_state == "refunded":
                # 终态不变式:整单退清 ⟺ 全部项目 is_refund=1。补齐分摊没覆盖到的项
                # (浮点残项/整单折扣单——item.total_fee 是折前行价,退满也到不了 total,threshold 不触发)
                # 和老数据。只标 is_refund,不改 refunded_fee(折扣单实退<行价,顶到 total 会虚增退款额)。
                conn.execute(
                    "update treatment_items set is_refund = 1, updated_at = ? "
                    "where bill_id = ? and is_deleted = 0 and coalesce(is_refund,0) = 0",
                    (now, bill_id),
                )
            audit_write(
                conn, "bill", bill_id, "refund",
                old_json=json.dumps({"refunded_fee": refunded_sum, "state": bill["state"]},
                                    ensure_ascii=False),
                new_json=json.dumps({"refunded_fee": round(refunded_sum + amount, 2), "state": new_state,
                                     "refund_amount": amount, "refund_method": refund_method,
                                     "refund_reason": reason},
                                    ensure_ascii=False),
                created_at=now,
            )
            result = {"bill_id": bill_id, "state": new_state, "refund_amount": amount,
                      "refunded_fee": round(refunded_sum + amount, 2),
                      "refundable": remaining_refundable, "payment_id": payment_id}
            _record_request(conn, rid, bill_id, "refund", phash, result, now)
            conn.commit()
        return result

    return router
