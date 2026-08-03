"""会员储值（纯储值最小闭环）：充值 / 消费扣减(不透支) / 退储值(退现) / 余额+流水查询。
改钱模块——金额走 _clean_amount 守卫，余额随每笔事务更新，每笔写流水 + audit_logs。
本地业务表(member_*)，不回写 SaaS 镜像。"""
import json
import math
from pathlib import Path

from fastapi import APIRouter, HTTPException

from local_app.timeutil import now_str
from local_app.auth import audit_operator, audit_write, require_admin, require_perm
from local_app.db import connect, new_id
# 审计R2:软删患者按不存在处理,收敛到共享校验(原本地拷贝不查 is_deleted)
from local_app.routes.patient_common import require_patient as _require_patient
# 审计R4:充值/消费/退储值幂等,直接复用 #800 收退款的 payment_requests 范式
# (同号同载荷重放首次响应,同号异载荷 409,缺号 400)。表已在 schema.sql,scope 用
# patient_identity 落 bill_id 列,kind 前缀 member_ 与收退款隔开。
from local_app.routes.billing_ops import _idempotent_replay, _record_request, _request_key




def _membership_enabled(conn):
    """读功能开关：会员储值是否启用（app_settings 'features'，默认开）。"""
    row = conn.execute("select value from app_settings where key = 'features'").fetchone()
    if not row:
        return True
    try:
        return bool(json.loads(row["value"] or "{}").get("membership_enabled", True))
    except (ValueError, TypeError):
        return True


def _require_enabled(conn):
    if not _membership_enabled(conn):
        raise HTTPException(status_code=403, detail="会员储值未启用（可在设置里开启）")


def _clean_amount(value, field="amount"):
    """金额守卫：必须是有限正数（充值/消费/退储值都不接受 0/负数/nan/inf/脏字符串）。"""
    try:
        amount = float(value)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=f"{field} 必须是数字") from exc
    if not math.isfinite(amount) or amount <= 0:
        raise HTTPException(status_code=400, detail=f"{field} 必须是大于 0 的有限数")
    amount = round(amount, 2)
    if amount <= 0: # 复审：四舍五入到分后仍须>0(挡 0.004 这类亚分无效额)
        raise HTTPException(status_code=400, detail=f"{field} 不能小于 0.01 元")
    return amount


def create_membership_router(db_path):
    router = APIRouter()
    db_path = Path(db_path)

    def _lock(conn):
        # #165：改钱前抢写锁，把同患者并发的读-改-写串行化，杜绝丢更新/绕过不透支。
        # busy_timeout 让第二个写者排队等待而非立刻报 database is locked。
        conn.isolation_level = None
        conn.execute("pragma busy_timeout = 5000")
        conn.execute("begin immediate")

    def _balance(conn, patient_identity):
        row = conn.execute(
            "select balance from member_accounts where patient_identity = ?",
            (patient_identity,),
        ).fetchone()
        return float(row["balance"]) if row else 0.0

    def _apply(conn, patient_identity, txn_type, amount, new_balance, bill_id, note):
        now = now_str()
        # upsert 账户余额
        conn.execute(
            """
            insert into member_accounts(patient_identity, balance, created_at, updated_at)
            values (?, ?, ?, ?)
            on conflict(patient_identity) do update set balance = excluded.balance, updated_at = excluded.updated_at
            """,
            (patient_identity, new_balance, now, now),
        )
        txn_id = new_id("local-mtxn")
        conn.execute(
            """
            insert into member_transactions(
                txn_id, patient_identity, txn_type, amount, balance_after,
                note, bill_id, operator, created_at
            )
            values (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (txn_id, patient_identity, txn_type, amount, new_balance,
             note, bill_id, audit_operator(), now),
        )
        # 审查#23：审计 old/new_json 用 JSON 对象(与其它审计一致),不用裸 str(数字)
        audit_write(
            conn, "member_account", patient_identity, "member_" + txn_type,
            old_json=json.dumps({"balance": round(new_balance - _signed(txn_type, amount), 2),
                                 "amount": amount, "txn_type": txn_type}, ensure_ascii=False),
            new_json=json.dumps({"balance": new_balance}, ensure_ascii=False),
            created_at=now,
        )
        return txn_id, new_balance

    @router.get("/api/patients/{patient_identity}/member-account")
    def get_member_account(patient_identity: str):
        require_perm("billing.view")   # #482：会员储值余额属财务,按 billing.view 守卫
        with connect(db_path) as conn:
            _require_patient(conn, patient_identity)
            balance = _balance(conn, patient_identity)
            rows = conn.execute(
                """
                select txn_id, txn_type, amount, balance_after, note, bill_id, operator, created_at
                from member_transactions
                where patient_identity = ?
                order by created_at desc, rowid desc
                """,
                (patient_identity,),
            ).fetchall()
        return {
            "patient_identity": patient_identity,
            "balance": round(balance, 2),
            "transactions": [dict(r) for r in rows],
        }

    @router.post("/api/patients/{patient_identity}/member-account/topup")
    def topup(patient_identity: str, payload: dict):
        require_admin()   # 审查#15：改余额/退现仅管理员
        rid, phash = _request_key(payload, patient_identity, "member_topup")   # 审计R4:幂等键必填
        amount = _clean_amount((payload or {}).get("amount"))
        note = str((payload or {}).get("note") or "").strip()
        with connect(db_path) as conn:
            _lock(conn)   # 审计R2复核:先抢写锁再查患者——查与写之间合并事务无法插入(P1竞态窗口)
            _require_patient(conn, patient_identity)
            _require_enabled(conn)
            replay = _idempotent_replay(conn, rid, phash)   # 审计R4:同号同载荷=重放,不入第二笔
            if replay is not None:
                return replay
            new_balance = round(_balance(conn, patient_identity) + amount, 2)
            txn_id, bal = _apply(conn, patient_identity, "topup", amount, new_balance, "", note)
            result = {"txn_id": txn_id, "balance": bal}
            _record_request(conn, rid, patient_identity, "member_topup", phash, result, now_str())
            conn.commit()
        return result

    @router.post("/api/patients/{patient_identity}/member-account/consume")
    def consume(patient_identity: str, payload: dict):
        require_admin()   # 审查#15
        rid, phash = _request_key(payload, patient_identity, "member_consume")   # 审计R4:幂等键必填
        amount = _clean_amount((payload or {}).get("amount"))
        bill_id = str((payload or {}).get("bill_id") or "").strip()
        note = str((payload or {}).get("note") or "").strip()
        with connect(db_path) as conn:
            _lock(conn)   # 审计R2复核:先抢写锁再查患者——查与写之间合并事务无法插入(P1竞态窗口)
            _require_patient(conn, patient_identity)
            _require_enabled(conn)
            replay = _idempotent_replay(conn, rid, phash)   # 审计R4:同号同载荷=重放,不重复扣款
            if replay is not None:
                return replay
            balance = _balance(conn, patient_identity)
            if amount > balance + 1e-9:   # 不透支
                raise HTTPException(status_code=400, detail="储值余额不足")
            new_balance = round(balance - amount, 2)
            txn_id, bal = _apply(conn, patient_identity, "consume", amount, new_balance, bill_id, note)
            result = {"txn_id": txn_id, "balance": bal}
            _record_request(conn, rid, patient_identity, "member_consume", phash, result, now_str())
            conn.commit()
        return result

    @router.post("/api/patients/{patient_identity}/member-account/refund")
    def refund(patient_identity: str, payload: dict):
        # 退储值(退现)：把未消费余额退回现金，冲减余额
        require_admin()   # 审查#15：退现仅管理员
        rid, phash = _request_key(payload, patient_identity, "member_refund")   # 审计R4:幂等键必填
        amount = _clean_amount((payload or {}).get("amount"))
        note = str((payload or {}).get("note") or "").strip()
        with connect(db_path) as conn:
            _lock(conn)   # 审计R2复核:先抢写锁再查患者——查与写之间合并事务无法插入(P1竞态窗口)
            _require_patient(conn, patient_identity)
            _require_enabled(conn)
            replay = _idempotent_replay(conn, rid, phash)   # 审计R4:同号同载荷=重放,不重复退现
            if replay is not None:
                return replay
            balance = _balance(conn, patient_identity)
            if amount > balance + 1e-9:   # 不能退超过余额
                raise HTTPException(status_code=400, detail="退储值金额超过余额")
            new_balance = round(balance - amount, 2)
            txn_id, bal = _apply(conn, patient_identity, "refund", amount, new_balance, "", note)
            result = {"txn_id": txn_id, "balance": bal}
            _record_request(conn, rid, patient_identity, "member_refund", phash, result, now_str())
            conn.commit()
        return result

    return router


def _signed(txn_type, amount):
    """该笔对余额的有符号变化（topup 加，consume/refund 减），仅供审计快照回推旧余额。"""
    return amount if txn_type == "topup" else -amount
