"""付款↔账单自动关联（严格唯一匹配）。

外部导入的付款明细常常不带账单关联（bill_id 空），
患者页堆积「未关联付款」。本模块按四个条件回填，缺一不可：
  1. 同一患者；
  2. 付款金额与账单已收(paid_fee)分毫不差；
  3. 账单开单日 <= 付款日；
  4. 该账单名下没有任何已关联付款，且全患者范围内只有这一张候选账单；
     一张账单只允许被一条孤儿付款认领(两条都匹配同一张 → 都不动)。
0 元流水、作废付款、撤单/作废账单一律不碰。只写 bill_id 一列，不碰金额。
"""
from collections import Counter

from local_app.timeutil import now_str
from local_app.db import VOIDED_BILL_STATES


def compute_links(conn):
    """返回可安全回填的 [(payment_id, bill_id)]。只读。"""
    voided_marks = ",".join("?" for _ in VOIDED_BILL_STATES)
    rows = conn.execute(
        f"""
        select o.payment_id, b.bill_id
        from payments o
        join bills b
          on b.patient_identity = o.patient_identity
         and abs(b.paid_fee - o.amount) < 0.01
         and date(substr(b.bill_time, 1, 10)) <= date(substr(o.pay_time, 1, 10))
         and b.state not in ({voided_marks})
         and not exists (select 1 from payments p2 where p2.bill_id = b.bill_id)
        where (o.bill_id is null or o.bill_id = '')
          and o.amount != 0
          and o.state != '作废'
        """,
        VOIDED_BILL_STATES,
    ).fetchall()

    pay_hits = Counter(r[0] for r in rows)
    bill_hits = Counter(r[1] for r in rows)
    return [(p, b) for p, b in rows if pay_hits[p] == 1 and bill_hits[b] == 1]


def apply_links(conn):
    """回填并提交，返回条数。幂等：挂上的下轮不再是孤儿。"""
    links = compute_links(conn)
    if links:
        now = now_str()
        conn.executemany(
            "update payments set bill_id = ?, updated_at = ? "
            "where payment_id = ? and (bill_id is null or bill_id = '')",
            [(b, now, p) for p, b in links],
        )
        conn.commit()
    return len(links)
