"""财务聚合服务层(Phase4 分层试点样板)。

分层铁律(新代码照此办理):
1. 纯函数:只接「数据库连接 conn + 普通参数」,返回普通 dict/list——不 import FastAPI、
   不查权限、不摸请求、不抛 HTTPException(参数校验归路由层)。
2. 零副作用:只读聚合,绝不写库。
3. 口径复用:金额口径一律用 db.py 三件套(bill_net_arrears_sql/bill_discount_sql/
   active_bill_clause)与本文件的 valid_payment_clause/net_paid_in_range,不许再抄。
路由层只剩三件事:验权限(require_perm)、收发参数(校验/默认值/HTTPException)、拼 HTTP 响应(JSON/CSV)。

本体自 routes/reports.py 与 routes/store_stats.py 原样搬入(Phase4 行为零改动;
两文件各自的 valid_payment_clause 为逐字双胞胎,合一为 valid_payment_clause)。
已知口径债(搬家时如实保留,统一属行为变更另立项):daily_settlement_rows 的累计欠费为
手写内联(与 bill_net_arrears_sql 同构),arrears_flows 内联同构 bill_net_arrears_sql。
"""
import json
import re
from datetime import datetime

from local_app.db import active_bill_clause, bill_discount_sql
from local_app.paid_detail import split_paid_detail
from local_app.timeutil import bj_now


def method_parts(amount, detail):
    """把一笔收款按 PaidDetail 拆成 [(方式,金额)],与工作台 today_pay_methods 同口径——
    多方式且拆分和≈整笔金额才拆,否则返回 None(调用方按 pay_type 整笔计)。"""
    parts = split_paid_detail(detail or "")
    amt = amount or 0
    if len(parts) > 1 and abs(sum(a for _, a in parts) - amt) < 0.01:
        return parts
    return None

# 收银查询付款方式列(对齐通用口径 实际用的)；处置类别动态来自 fee_type
CASHIER_METHODS = ["现金", "微信", "支付宝", "银行卡", "云闪付", "社保卡", "医保", "其他"]


def valid_payment_clause(prefix=""):
    """收款有效性过滤 SQL(同收银/收入)：排作废原单(CancelMark 非空且金额≥0)，保留退费负数冲减。"""
    p = prefix
    return (f"(coalesce(json_extract(iif(json_valid({p}source_json), {p}source_json, '{{}}'), '$.CancelMark'), '') = '' "
            f"or coalesce({p}amount, 0) < 0)")


def chunks(seq, n=500):
    seq = list(seq)
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def net_paid_in_range(conn, start, end):
    """区间内每账单净实收(按 pay_time)：剔除作废原单、退费负数冲减。
    返回 {bill_id: net}；无账单收款归到 key ''（进未归类）。"""
    net = {}
    for r in conn.execute(
        """
        select coalesce(bill_id, '') as bill_id, amount,
               json_extract(iif(json_valid(source_json), source_json, '{}'), '$.CancelMark') as cancel_mark
        from payments
        where substr(coalesce(pay_time, ''), 1, 10) between ? and ?
        """,
        (start, end),
    ).fetchall():
        cancel = str(r["cancel_mark"] or "")
        amt = r["amount"] or 0
        if cancel != "" and amt >= 0:   # 作废原单排除；退费负数保留冲减
            continue
        net[r["bill_id"]] = round(net.get(r["bill_id"], 0) + amt, 2)
    return net


def build_cashier_rows(conn, date_from, date_to, doctor="", source="", method="", operator=""):
    """收银查询逐笔：每笔收款 + 付款方式 + 处置类别分摊(按账单 items 各 fee_type 占比)。
    口径：剔除作废原单(CancelMark非空且金额≥0)，退费负数保留作冲减(同 income)。"""
    pays = conn.execute(
        """
        select pay.payment_id, pay.patient_identity, pay.pay_time, pay.amount, pay.pay_type,
               pay.operator, pay.bill_id, pay.invoice_no, pay.remark,
               pay.payment_record_type, pay.source_json as pay_source_json,
               p.display_name, p.phone, p.referral_source, p.referral_source2, p.referral_source3,
               coalesce(nullif(p.chart_no, ''),
                        json_extract(iif(json_valid(p.source_json), p.source_json, '{}'), '$.patientid')) as chart_no,
               json_extract(iif(json_valid(pay.source_json), pay.source_json, '{}'), '$.CancelMark') as cancel_mark
        from payments pay
        left join patients p on p.patient_identity = pay.patient_identity
        where substr(coalesce(pay.pay_time, ''), 1, 10) between ? and ?
        order by pay.pay_time, pay.payment_id
        """,
        (date_from, date_to),
    ).fetchall()
    pays = [r for r in pays if not (str(r["cancel_mark"] or "") != "" and (r["amount"] or 0) >= 0)]

    # 账单 → {fee_type: 小计}、主医生、账单总额(给处置类别分摊用)
    bill_ids = {r["bill_id"] for r in pays if r["bill_id"]}
    bill_info = {}
    for bid in bill_ids:
        cats, doc_tally = {}, {}
        for it in conn.execute(
            "select fee_type, doctor_name, total_fee from treatment_items where bill_id = ? and is_deleted = 0",
            (bid,),
        ).fetchall():
            ft = (it["fee_type"] or "").strip() or "未分类"
            cats[ft] = round(cats.get(ft, 0) + (it["total_fee"] or 0), 2)
            dn = (it["doctor_name"] or "").strip()
            if dn:
                doc_tally[dn] = doc_tally.get(dn, 0) + (it["total_fee"] or 0)
        total = round(sum(cats.values()), 2)
        main_doc = max(doc_tally, key=doc_tally.get) if doc_tally else ""
        bill_info[bid] = {"cats": cats, "total": total, "doctor": main_doc}

    rows = []
    for r in pays:
        amount = round(r["amount"] or 0, 2)
        info = bill_info.get(r["bill_id"])
        # 本地退款流水自带项目归属(source_json.refund_items,退款那一刻固化)——
        # 分类按归属精确记负数(只退检查=检查-300),不再按整单比例猜。老退款/导入的负数行
        # 没有 refund_items(归属信息不存在),维持下面的比例口径。
        refund_alloc = None
        if (r["payment_record_type"] or "") == "refund":
            try:
                ritems = (json.loads(r["pay_source_json"] or "{}") or {}).get("refund_items") or []
            except (ValueError, TypeError):
                ritems = []
            if ritems:
                refund_alloc = {}
                for ri in ritems:
                    ft = str((ri or {}).get("fee_type") or "").strip() or "未分类"
                    refund_alloc[ft] = round(refund_alloc.get(ft, 0) - float((ri or {}).get("amount") or 0), 2)
        # 处置类别分摊：按各 fee_type 占账单比例，余数归最后一类(避免分摊后合计≠总收入)
        alloc = {}
        if refund_alloc:
            alloc = refund_alloc
            row_doctor = info["doctor"] if info else ""
        elif info and info["total"] > 0:
            items = list(info["cats"].items())
            running = 0.0
            for i, (ft, t) in enumerate(items):
                a = round(amount - running, 2) if i == len(items) - 1 else round(amount * t / info["total"], 2)
                running = round(running + a, 2)
                alloc[ft] = a
            row_doctor = info["doctor"]
        else:
            alloc = {"未分类": amount}
            row_doctor = ""
        rows.append({
            "pay_time": r["pay_time"] or "", "chart_no": r["chart_no"] or "",
            "display_name": r["display_name"] or "", "phone": r["phone"] or "",
            "doctor": row_doctor, "operator": r["operator"] or "",
            "referral_source": r["referral_source"] or "", "referral_source2": r["referral_source2"] or "",
            "referral_source3": r["referral_source3"] or "",
            "amount": amount, "pay_type": (r["pay_type"] or "").strip() or "其他",
            "categories": alloc, "invoice_no": r["invoice_no"] or "", "remark": r["remark"] or "",
        })

    # 筛选(医生/来源/付款方式)
    if doctor:
        rows = [r for r in rows if r["doctor"] == doctor]
    if source:
        rows = [r for r in rows if source in (r["referral_source"], r["referral_source2"], r["referral_source3"])]
    if method:
        rows = [r for r in rows if r["pay_type"] == method]
    if operator:
        rows = [r for r in rows if r["operator"] == operator]
    return rows


def arrears_flows(conn, date_from, date_to):
    """欠款流水(对标行业通用口径 收入统计的期初/期末欠款 + 每日新增/补交欠款)。

    欠款绝对值以 **bills.paid_fee 口径** 锚定(= 现有 bill_net_arrears_sql，与全项目一致、外部导入维护)——
    不用 payments 表逐笔重建(实测本地约 195 万收款无有效账单可挂[空 bill_id 的预交/储值等]，
    重建会把欠款虚高一个数量级)。口径：
    - 净应收 = total_fee − 优惠(DiscountFee)，仅有效账单(排作废)；当前欠款 = Σ(净应收 − paid_fee)。
    - 期末欠款(date_to) = 当前欠款 + 期后收款(pay_time>date_to) − 期后新开账单净应收(bill_time>date_to)
      （从「现在」按期后活动回推到 date_to 末）。
    - 每日 新增欠款(d) = 当日新开账单净应收 − 当日对当日账单的收款；补交欠款(d) = 当日对往期账单的收款。
    - 期初欠款 = 期末 − Σ(新增 − 补交)（由恒等式导出，保证 期末 = 期初 + Σ新增 − Σ补交）。
    """
    disc = bill_discount_sql()          # bills.source_json 优惠
    active = active_bill_clause()       # bills.state 有效
    active_b = active_bill_clause("b.state")
    cancel_p = valid_payment_clause("p.")
    # ①:本地退款(payment_record_type='refund')不再冲减 bills.paid_fee → 也不得进欠款流水,
    # 否则回推/补交把退款算成欠款变动。导入的负数行 record_type='' 不受影响(其 paid_fee 由导入源维护)。
    no_refund = "coalesce(p.payment_record_type, '') <> 'refund'"

    # 当前欠款(paid_fee 口径，与 bill_net_arrears_sql 一致)
    current = conn.execute(
        f"select coalesce(sum(total_fee - {disc} - paid_fee), 0) from bills where {active}"
    ).fetchone()[0] or 0
    # 期后收款 − 期后新开账单净应收 → 回推到 date_to 末。
    # pay_after 必须与 paid_fee 锚同一全集——只算「挂在有效账单上」的收款，
    # 排除空 bill_id 预交/储值(它们不改任何账单 paid_fee，计入会把历史期末欠款虚高)。
    pay_after = conn.execute(
        f"""select coalesce(sum(p.amount), 0) from payments p join bills b on b.bill_id = p.bill_id
            where substr(coalesce(p.pay_time, ''), 1, 10) > ? and {active_b} and {cancel_p} and {no_refund}""",
        (date_to,)).fetchone()[0] or 0
    recv_after = conn.execute(
        f"""select coalesce(sum(total_fee - {disc}), 0) from bills
            where substr(coalesce(bill_time, ''), 1, 10) > ? and {active}""",
        (date_to,)).fetchone()[0] or 0
    closing = round(current + pay_after - recv_after, 2)

    # 当日新开账单净应收
    new_recv = {r["day"]: r["v"] for r in conn.execute(
        f"""select substr(bill_time, 1, 10) as day, round(coalesce(sum(total_fee - {disc}), 0), 2) as v
            from bills where substr(coalesce(bill_time, ''), 1, 10) between ? and ? and {active} group by day""",
        (date_from, date_to)).fetchall()}
    # 当日对「当日新开账单」的收款
    paid_new = {r["day"]: r["v"] for r in conn.execute(
        f"""select substr(p.pay_time, 1, 10) as day, round(coalesce(sum(p.amount), 0), 2) as v
            from payments p join bills b on b.bill_id = p.bill_id
            where substr(coalesce(p.pay_time, ''), 1, 10) between ? and ?
              and substr(coalesce(p.pay_time, ''), 1, 10) = substr(coalesce(b.bill_time, ''), 1, 10)
              and {active_b} and {cancel_p} and {no_refund} group by day""",
        (date_from, date_to)).fetchall()}
    # 当日对往期账单的收款(补交)
    paid_old = {r["day"]: r["v"] for r in conn.execute(
        f"""select substr(p.pay_time, 1, 10) as day, round(coalesce(sum(p.amount), 0), 2) as v
            from payments p join bills b on b.bill_id = p.bill_id
            where substr(coalesce(p.pay_time, ''), 1, 10) between ? and ?
              and substr(coalesce(p.pay_time, ''), 1, 10) > substr(coalesce(b.bill_time, ''), 1, 10)
              and {active_b} and {cancel_p} and {no_refund} group by day""",
        (date_from, date_to)).fetchall()}

    days = sorted(set(new_recv) | set(paid_new) | set(paid_old))
    per_day = {}
    total_new = total_repay = 0.0
    for d in days:
        new_debt = round(new_recv.get(d, 0) - paid_new.get(d, 0), 2)
        repay = round(paid_old.get(d, 0), 2)
        per_day[d] = {"new_debt": new_debt, "repay_debt": repay}
        total_new += new_debt
        total_repay += repay
    opening = round(closing - total_new + total_repay, 2)   # 由恒等式导出
    return {"opening": opening, "closing": closing, "per_day": per_day,
            "total_new_debt": round(total_new, 2), "total_repay_debt": round(total_repay, 2)}


def income_payload(conn, date_from, date_to):
    """收入统计数据(JSON/CSV 共用)：按天汇总实收 + 日×付款方式透视 + 应收 + 欠款流水。"""
    # 逐笔取(含 PaidDetail),在 Python 里按方式拆分,与工作台 today_pay_methods 同口径
    prows = conn.execute(
        """
        select substr(pay_time, 1, 10) as day,
               coalesce(nullif(trim(pay_type), ''), '其他') as method,
               coalesce(amount, 0) as amt,
               json_extract(iif(json_valid(source_json), source_json, '{}'), '$.PaidDetail') as detail
        from payments
        where substr(coalesce(pay_time, ''), 1, 10) between ? and ?
          -- 作废口径：排「作废原单」(CancelMark 非空且金额≥0),保留退费负数冲减
          and (coalesce(json_extract(iif(json_valid(source_json), source_json, '{}'), '$.CancelMark'), '') = ''
               or coalesce(amount, 0) < 0)
        order by day
        """,
        (date_from, date_to),
    ).fetchall()
    # 当日应收(按开单日,净额=总价-优惠,排作废账单)
    brows = conn.execute(
        f"""
        select substr(bill_time, 1, 10) as day,
               round(coalesce(sum(total_fee - {bill_discount_sql()}), 0), 2) as receivable
        from bills
        where substr(coalesce(bill_time, ''), 1, 10) between ? and ? and {active_bill_clause()}
        group by day
        """,
        (date_from, date_to),
    ).fetchall()

    day_map, seen = {}, []
    for r in prows:
        if not r["day"]:
            continue
        d = day_map.setdefault(r["day"], {"date": r["day"], "count": 0, "amount": 0.0, "methods": {}, "receivable": 0.0})
        d["count"] += 1                       # 每笔算 1(拆方式不虚增笔数)
        d["amount"] = round(d["amount"] + r["amt"], 2)
        # 多方式收款按 PaidDetail 拆到真实方式(和工作台一致),拆不出按 pay_type 整笔计
        parts = method_parts(r["amt"], r["detail"]) or [(r["method"], r["amt"])]
        for m, a in parts:
            d["methods"][m] = round(d["methods"].get(m, 0) + a, 2)
            if m not in seen:
                seen.append(m)
    for r in brows:
        if not r["day"]:
            continue
        d = day_map.setdefault(r["day"], {"date": r["day"], "count": 0, "amount": 0.0, "methods": {}, "receivable": 0.0})
        d["receivable"] = r["receivable"]

    # 欠款流水(对标行业通用口径 收入统计：期初/期末欠款 + 每日新增/补交欠款)
    flows = arrears_flows(conn, date_from, date_to)
    for day_key, f in flows["per_day"].items():
        d = day_map.setdefault(day_key, {"date": day_key, "count": 0, "amount": 0.0, "methods": {}, "receivable": 0.0})
        d["new_debt"] = f["new_debt"]
        d["repay_debt"] = f["repay_debt"]
    for d in day_map.values():
        d.setdefault("new_debt", 0.0)
        d.setdefault("repay_debt", 0.0)

    days = [day_map[k] for k in sorted(day_map)]
    # 方式列顺序：固定列在前 + 额外方式
    from_fixed = [m for m in CASHIER_METHODS if m in seen]
    methods = from_fixed + [m for m in seen if m not in CASHIER_METHODS]
    return {
        "date_from": date_from, "date_to": date_to, "days": days, "methods": methods,
        "total_amount": round(sum(d["amount"] for d in days), 2),   # 
        "total_count": sum(d["count"] for d in days),
        "total_receivable": round(sum(d["receivable"] for d in days), 2),
        "opening_arrears": flows["opening"], "closing_arrears": flows["closing"],
        "total_new_debt": flows["total_new_debt"], "total_repay_debt": flows["total_repay_debt"],
    }


def cashier_totals(rows):
    by_method, by_cat = {}, {}
    for r in rows:
        by_method[r["pay_type"]] = round(by_method.get(r["pay_type"], 0) + r["amount"], 2)
        for ft, a in r["categories"].items():
            by_cat[ft] = round(by_cat.get(ft, 0) + a, 2)
    return by_method, by_cat


def handle_category_agg(conn, date_from, date_to, doctor="", source=""):
    """处置大类聚合(JSON/CSV 共用,避免口径漂移)。返回 (items, total_amount, pay_count)。
    item={fee_type,amount(分摊实收),count(命中该类别的收款笔数)}；笔数按类别计,总笔数另给 pay_count。"""
    rows = build_cashier_rows(conn, date_from, date_to, doctor, source, "")
    cat = {}
    for r in rows:
        for ft, a in r["categories"].items():
            c = cat.setdefault(ft, {"fee_type": ft, "amount": 0.0, "count": 0})
            c["amount"] = round(c["amount"] + a, 2)
            c["count"] += 1
    items = sorted(cat.values(), key=lambda x: x["amount"], reverse=True)
    return items, round(sum(c["amount"] for c in items), 2), len(rows)


def blank_cell():
    return {"visit": 0, "deal": set(), "paid": 0.0, "receivable": 0.0}


def pack_row(name, first, revisit):
    def pack(c):
        return {"visit": c["visit"], "deal": len(c["deal"]),
                "paid": round(c["paid"], 2), "receivable": round(c["receivable"], 2)}
    return {"name": name, "first": pack(first), "revisit": pack(revisit)}


# 病历为轴的角色取名 SQL：医生=doctor_name 列；护士=病历 content_json.Nurse。
_VISIT_ROLE_SQL = {
    "doctor": "trim(coalesce(doctor_name, ''))",
    "nurse": "trim(coalesce(json_extract(iif(json_valid(content_json), content_json, '{}'), '$.Nurse'), ''))",
}


def split_persons(s):
    """病历里角色名可能多人(如护士 "曹佳,陈婷")——拆成去重人名列表。空→空列表。"""
    out, seen = [], set()
    for p in re.split(r"[,，、;；/]", s or ""):
        p = p.strip()
        if p and p not in seen:
            seen.add(p)
            out.append(p)
    return out


def visit_perf(conn, start, end, staff, role):
    """医生/护士统计（病历为轴）。role=doctor 取 doctor_name；nurse 取 content_json.Nurse。
    一次就诊可配多人(护士常逗号多人)：量按人各计一次；实收/应收在同就诊多人间平摊(合计不虚增)。
    返回 (rows, unclassified_row)。"""
    person_sql = _VISIT_ROLE_SQL[role]
    raw = conn.execute(
        f"""
        select coalesce(nullif(trim(study_identity), ''), '') as sid,
               patient_identity as pid, {person_sql} as person,
               trim(record_type) as vt, substr(visit_time, 1, 10) as vd
        from medical_records
        where trim(record_type) in ('初诊', '复诊')
          and substr(coalesce(visit_time, ''), 1, 10) between ? and ?
          and {person_sql} <> ''
        order by coalesce(visit_time, ''), record_id
        """,
        (start, end),
    ).fetchall()
    visits = []
    for r in raw:
        persons = split_persons(r["person"])
        if persons:
            visits.append({"sid": r["sid"], "pid": r["pid"], "vt": r["vt"], "vd": r["vd"], "persons": persons})

    # 就诊索引：study → 就诊；(患者,日期) → 当日最早就诊(查询已按时间升序，setdefault 取首个=最早)
    study2visit, pd2visit = {}, {}
    for v in visits:
        if v["sid"]:
            study2visit.setdefault(v["sid"], v)
        pd2visit.setdefault((v["pid"], v["vd"]), v)

    cells = {}   # person -> {"first":cell, "revisit":cell}

    def cell_for(person, vt):
        d = cells.setdefault(person, {"first": blank_cell(), "revisit": blank_cell()})
        return d["first"] if vt == "初诊" else d["revisit"]

    def resolve(sid, pid, bd):
        v = study2visit.get(sid) if sid else None
        if v is None:
            v = pd2visit.get((pid, bd))
        return v

    # 量：同就诊每个配台人各计一次
    for v in visits:
        for person in v["persons"]:
            cell_for(person, v["vt"])["visit"] += 1

    # 应收：仅有效账单(排作废)，净应收=total−优惠；就诊在区间才计，多人平摊
    pids = {v["pid"] for v in visits}
    disc = bill_discount_sql("")
    seen_recv = set()

    def _recv_bills(sql, params):
        for b in conn.execute(sql, params).fetchall():
            if b["bill_id"] in seen_recv:
                continue
            seen_recv.add(b["bill_id"])
            v = resolve(str(b["sid"] or ""), b["pid"], b["bd"])
            if v is not None:
                share = ((b["total_fee"] or 0) - (b["net_disc"] or 0)) / len(v["persons"])
                for person in v["persons"]:
                    cell_for(person, v["vt"])["receivable"] += share

    recv_cols = ("bill_id, patient_identity as pid, substr(bill_time, 1, 10) as bd, total_fee, "
                 f"cast({disc} as real) as net_disc, "
                 "json_extract(iif(json_valid(source_json), source_json, '{}'), '$.BillStudyIdentity') as sid")
    active = active_bill_clause("state")
    for chunk in chunks(pids):
        ph = ",".join("?" * len(chunk))
        _recv_bills(f"select {recv_cols} from bills where {active} and patient_identity in ({ph}) "
                    f"and substr(coalesce(bill_time, ''), 1, 10) between ? and ?",
                    tuple(chunk) + (start, end))
    for chunk in chunks(study2visit.keys()):
        ph = ",".join("?" * len(chunk))
        _recv_bills(f"select {recv_cols} from bills where {active} and "
                    f"json_extract(iif(json_valid(source_json), source_json, '{{}}'), '$.BillStudyIdentity') in ({ph})",
                    tuple(chunk))

    # 实收：区间内全部收款(按收款时间)为全集，关联到就诊则按配台人平摊，否则未归类(与收银闭合)
    net_by_bill = net_paid_in_range(conn, start, end)
    bill_meta = {}   # bill_id -> (sid, pid, bd)
    real_ids = [bid for bid in net_by_bill if bid]
    for chunk in chunks(real_ids):
        ph = ",".join("?" * len(chunk))
        for b in conn.execute(
            f"""select bill_id, patient_identity as pid, substr(bill_time, 1, 10) as bd,
                   json_extract(iif(json_valid(source_json), source_json, '{{}}'), '$.BillStudyIdentity') as sid
                from bills where bill_id in ({ph})""",
            tuple(chunk),
        ).fetchall():
            bill_meta[b["bill_id"]] = (str(b["sid"] or ""), b["pid"], b["bd"])

    unclassified = blank_cell()
    for bid, net in net_by_bill.items():
        meta = bill_meta.get(bid)
        v = resolve(*meta) if meta else None
        if v is None:
            # 关联不上就诊(含无病历患者)的收款进未归类,使总实收+未归类=收银净实收。
            unclassified["paid"] += net
            continue
        share = net / len(v["persons"])
        for person in v["persons"]:
            c = cell_for(person, v["vt"])
            c["paid"] += share
            if net > 0:
                c["deal"].add(meta[1])

    if staff:   # 单人筛选:只留该人的行,不显示别人的钱/未归类(防串账)
        cells = {k: val for k, val in cells.items() if k == staff}
        unclassified = blank_cell()

    rows = [pack_row(person, d["first"], d["revisit"]) for person, d in cells.items()]
    rows.sort(key=lambda r: -(r["first"]["paid"] + r["revisit"]["paid"]))
    uncl = pack_row("(未归类)", blank_cell(), blank_cell())
    uncl["first"]["paid"] = round(unclassified["paid"], 2)
    return rows, uncl


_CONFIG_ROLE_COL = {"nurse": "nurse_name", "consultant": "consultant_name", "assistant": "assistant_name"}


def config_role_perf(conn, start, end, role, staff):
    """护士/助理/咨询师统计（处置单为轴）。本地配台数据几乎为空时如实返回空。
    应收排作废账单、扣优惠；实收按收款时间落区间。"""
    col = _CONFIG_ROLE_COL[role]
    disc = bill_discount_sql("b.")
    active = active_bill_clause("b.state")
    orders = conn.execute(
        f"""
        select o.patient_identity as pid, trim(o.{col}) as name, trim(o.visit_type) as vt,
               o.bill_id, b.total_fee, cast({disc} as real) as net_disc
        from treatment_orders o
        left join bills b on b.bill_id = o.bill_id
        where o.status != 'voided'
          and trim(coalesce(o.{col}, '')) <> ''
          and trim(o.visit_type) in ('初诊', '复诊')
          and substr(coalesce(o.order_date, o.created_at, ''), 1, 10) between ? and ?
          and (o.bill_id = '' or o.bill_id is null or {active})
        """,
        (start, end),
    ).fetchall()
    if staff:
        orders = [o for o in orders if o["name"] == staff]
    net = net_paid_in_range(conn, start, end)
    cells = {}
    for o in orders:
        d = cells.setdefault(o["name"], {"first": blank_cell(), "revisit": blank_cell()})
        c = d["first"] if o["vt"] == "初诊" else d["revisit"]
        c["visit"] += 1
        paid = net.get(o["bill_id"] or "", 0.0) if o["bill_id"] else 0.0
        c["paid"] += paid
        c["receivable"] += (o["total_fee"] or 0) - (o["net_disc"] or 0)
        if paid > 0:
            c["deal"].add(o["pid"])
    rows = [pack_row(n, d["first"], d["revisit"]) for n, d in cells.items()]
    rows.sort(key=lambda r: -(r["first"]["paid"] + r["revisit"]["paid"]))
    return rows


def age_from_birthday(birthday):
    """由出生日期算周岁(北京今天)。空/非法(含非法月日如 13 月/2-30)返回空串。"""
    b = (birthday or "").strip()[:10]
    if len(b) != 10 or b[4] != "-" or b[7] != "-":
        return ""
    try:
        bdate = datetime.strptime(b, "%Y-%m-%d").date()   # 严格校验月/日/闰年，非法即抛
    except ValueError:
        return ""
    t = bj_now()
    age = t.year - bdate.year - ((t.month, t.day) < (bdate.month, bdate.day))
    return str(age) if 0 <= age < 130 else ""


def daily_settlement_rows(conn, start, end, consultant):
    """日结报表明细(对标行业通用口径)：每条就诊(病历)一行 + 患者信息 + 该就诊账单财务。
    就诊→账单同 _doctor_perf(study 精确→同患者同日退化)。会员/储值等本地无数据的列留空(禁止兜底)。"""
    disc = bill_discount_sql("")
    active = active_bill_clause("state")
    visits = conn.execute(
        """
        select m.record_id, m.patient_identity as pid,
               coalesce(nullif(trim(m.study_identity), ''), '') as sid,
               trim(m.record_type) as vt, substr(m.visit_time, 1, 10) as vd,
               trim(m.doctor_name) as doctor,
               p.chart_no, p.display_name, p.sex, p.birthday, p.phone, p.created_at,
               p.referral_source, p.referral_source2, p.referral_source3, p.referral_source4,
               trim(coalesce(p.consultant, '')) as consultant
        from medical_records m
        join patients p on p.patient_identity = m.patient_identity
        where substr(coalesce(m.visit_time, ''), 1, 10) between ? and ?
          -- ②:不排除软删患者——财务史实(钱收了就是收了)与收入报表口径一致,软删只影响患者列表展示
          and trim(m.record_type) in ('初诊', '复诊')   -- 只算有类型的就诊,排除空类型草稿病历(否则同患者同日重复行)
        order by m.visit_time, m.record_id
        """,
        (start, end),
    ).fetchall()
    if consultant:
        visits = [v for v in visits if v["consultant"] == consultant]
    if not visits:
        return []

    # 就诊索引：study → 就诊；(患者,日期) → 当日最早就诊(visits 已按时间升序)
    study2visit, pd2visit = {}, {}
    pids = set()
    for v in visits:
        pids.add(v["pid"])
        if v["sid"]:
            study2visit.setdefault(v["sid"], v)
        pd2visit.setdefault((v["pid"], v["vd"]), v)

    # 候选账单(有效)。每张账单**唯一归属一个就诊、只用一次**：study 精确优先，
    # 否则同患者同日退化到当日最早就诊；都归不上则不计入(该就诊无对应账单财务留 0)。
    fin = {v["record_id"]: {"total": 0.0, "disc": 0.0, "paid": 0.0, "bills": []} for v in visits}
    for chunk in chunks(pids):
        ph = ",".join("?" * len(chunk))
        for b in conn.execute(
            f"""select bill_id, patient_identity as pid, substr(bill_time, 1, 10) as bd,
                   total_fee, paid_fee, cast({disc} as real) as net_disc,
                   json_extract(iif(json_valid(source_json), source_json, '{{}}'), '$.BillStudyIdentity') as sid
                from bills where {active} and patient_identity in ({ph})""",
            tuple(chunk),
        ).fetchall():
            sid = str(b["sid"] or "")
            v = study2visit.get(sid) if sid else None
            if v is None:
                v = pd2visit.get((b["pid"], b["bd"]))
            if v is None:
                continue                            # 归不上任何就诊 → 不计(避免错配)
            acc = fin[v["record_id"]]               # 同一就诊多张账单在此聚合
            acc["total"] += b["total_fee"] or 0
            acc["disc"] += b["net_disc"] or 0
            acc["paid"] += b["paid_fee"] or 0
            acc["bills"].append(b["bill_id"])

    # 账单就诊项目(treatment_items 名称拼接)
    bill_items = {}
    all_bids = {bid for acc in fin.values() for bid in acc["bills"]}
    for chunk in chunks(all_bids):
        ph = ",".join("?" * len(chunk))
        for r in conn.execute(
            f"""select bill_id, group_concat(item_name, ',') as items from treatment_items
                where bill_id in ({ph}) and coalesce(is_deleted, 0) = 0 group by bill_id""",
            tuple(chunk),
        ).fetchall():
            bill_items[r["bill_id"]] = r["items"] or ""

    # 患者累计欠费(有效账单净欠)
    arrears = {}
    for chunk in chunks(pids):
        ph = ",".join("?" * len(chunk))
        for r in conn.execute(
            f"""select patient_identity as pid,
                   round(coalesce(sum(total_fee - cast({disc} as real) - paid_fee), 0), 2) as arr
                from bills where {active} and patient_identity in ({ph}) group by patient_identity""",
            tuple(chunk),
        ).fetchall():
            arrears[r["pid"]] = r["arr"]

    rows = []
    for v in visits:
        acc = fin[v["record_id"]]
        items = "、".join(bill_items.get(bid, "") for bid in acc["bills"] if bill_items.get(bid))
        src = "/".join(x for x in [v["referral_source"], v["referral_source2"],
                                   v["referral_source3"], v["referral_source4"]] if (x or "").strip())
        rows.append({
            "visit_date": v["vd"], "chart_no": v["chart_no"] or "", "name": v["display_name"] or "",
            "sex": v["sex"] or "", "age": age_from_birthday(v["birthday"]),
            "created_at": v["created_at"] or "", "referral_source": src, "phone": v["phone"] or "",
            "doctor": v["doctor"] or "", "visit_type": v["vt"], "consultant": v["consultant"] or "",
            "items": items,
            "total_fee": round(acc["total"], 2), "receivable": round(acc["total"] - acc["disc"], 2),
            "paid": round(acc["paid"], 2), "discount": round(acc["disc"], 2),
            "arrears": arrears.get(v["pid"], 0),
        })
    return rows


def emr_leaves(v):
    """递归把 EMR 字段的叶子文本抽出来(dict 取值、list 展开、标量转字符串)，过滤空值。
    避免对 dict/list 直接 str() 把 Python 字面量({'name':..}/['a','b'])暴露到报表。"""
    if v is None:
        return []
    if isinstance(v, str):
        s = v.strip()
        return [s] if s else []
    if isinstance(v, bool):
        return []
    if isinstance(v, (int, float)):
        return [str(v)]
    if isinstance(v, list):
        out = []
        for x in v:
            out.extend(emr_leaves(x))
        return out
    if isinstance(v, dict):
        out = []
        for x in v.values():
            out.extend(emr_leaves(x))
        return out
    return []


def emr_text(content_json, key):
    """从病历 content_json 取某字段的可读文本：递归提取叶子文本，以「；」拼接，空则空串。"""
    import json as _json
    try:
        obj = _json.loads(content_json) if content_json else {}
    except (ValueError, TypeError):
        return ""
    if not isinstance(obj, dict):
        return ""
    return "；".join(emr_leaves(obj.get(key)))


def clinic_log_rows(conn, start, end, doctor):
    """门诊日志(对标行业通用口径)：每条就诊一行。就诊日期/病历号/姓名/年龄/医生/科室/就诊类型/家庭住址/
    临床初步判断(病历 DG 诊断)/临床诊断措施(病历 TR 治疗)/病人去向。科室、病人去向本地无字段→留空(禁止兜底)。"""
    rows = conn.execute(
        """
        select substr(m.visit_time, 1, 16) as visit_time, trim(m.doctor_name) as doctor,
               trim(m.record_type) as vt, m.content_json,
               p.chart_no, p.display_name, p.birthday, p.address
        from medical_records m
        join patients p on p.patient_identity = m.patient_identity
        where substr(coalesce(m.visit_time, ''), 1, 10) between ? and ?
          and coalesce(p.is_deleted, 0) = 0
        order by m.visit_time desc, m.record_id
        """,
        (start, end),
    ).fetchall()
    out = []
    for r in rows:
        if doctor and r["doctor"] != doctor:
            continue
        out.append({
            "visit_time": r["visit_time"] or "", "chart_no": r["chart_no"] or "",
            "name": r["display_name"] or "", "age": age_from_birthday(r["birthday"]),
            "doctor": r["doctor"] or "", "department": "", "visit_type": r["vt"],
            "address": r["address"] or "",
            "diagnosis": emr_text(r["content_json"], "DG"),
            "treatment": emr_text(r["content_json"], "TR"),
            "destination": "",
        })
    return out
