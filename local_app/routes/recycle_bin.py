"""回收站：聚合软删/作废记录(谁、何时删) + 还原。永久清除暂不做(YAGNI)。

支持类型：回访(is_deleted)、同意书作废(status=voided)、送消单(is_deleted)、器械(is_deleted)。
账单作废与处置单纠缠，不在本期还原范围。还原 = 清标记 + 写 restore 审计(记当前用户)。
"""
import sqlite3

from fastapi import APIRouter, HTTPException

from local_app import auth
from local_app.auth import audit_write
from local_app.db import connect


def create_recycle_bin_router(db_path, access_v3=False):
    router = APIRouter()

    def _deleter(conn, entity_type, entity_id):
        row = conn.execute(
            "select operator, created_at from audit_logs where entity_type = ? and entity_id = ? "
            "and (action like '%delete%' or action like '%void%') order by audit_id desc limit 1",
            (entity_type, entity_id),
        ).fetchone()
        return (row["operator"], row["created_at"]) if row else (None, None)

    @router.get("/api/recycle-bin")
    def list_recycle():
        auth.require_login_user()   # 回收站敏感:即便本机免登录也要求登录(保持原契约)
        auth.require_perm("patient.view")   # #494：含已删回访/同意书/账单(带患者名+金额),加 patient.view 守卫
        items = []
        with connect(db_path) as conn:
            for r in conn.execute(
                "select r.return_visit_id id, r.item_name, r.due_time, r.updated_at, p.display_name "
                "from return_visits r left join patients p on p.patient_identity = r.patient_identity "
                "where coalesce(r.is_deleted, 0) = 1 order by r.updated_at desc limit 200"
            ):
                op, at = _deleter(conn, "return_visit", r["id"])
                items.append({"entity_type": "return_visit", "entity_id": r["id"], "kind": "回访",
                              "title": f'{r["display_name"] or ""} · {r["item_name"] or "回访"}'.strip(" ·"),
                              "subtitle": r["due_time"] or "", "deleted_by": op, "deleted_at": at or r["updated_at"]})
            for r in conn.execute(
                "select c.document_id id, c.template_name, c.updated_at, p.display_name "
                "from consent_documents c left join patients p on p.patient_identity = c.patient_identity "
                "where c.status = 'voided' order by c.updated_at desc limit 200"
            ):
                op, at = _deleter(conn, "consent_document", r["id"])
                items.append({"entity_type": "consent_document", "entity_id": r["id"], "kind": "同意书(作废)",
                              "title": f'{r["display_name"] or ""} · {r["template_name"] or "同意书"}'.strip(" ·"),
                              "subtitle": "", "deleted_by": op, "deleted_at": at or r["updated_at"]})
            for r in conn.execute(
                "select id, dispatch_no, updated_at from sterilize_dispatch where coalesce(is_deleted, 0) = 1 "
                "order by updated_at desc limit 200"
            ):
                op, at = _deleter(conn, "sterilize_dispatch", r["id"])
                items.append({"entity_type": "sterilize_dispatch", "entity_id": r["id"], "kind": "送消单",
                              "title": r["dispatch_no"] or "送消单", "subtitle": "",
                              "deleted_by": op, "deleted_at": at or r["updated_at"]})
            for r in conn.execute(
                "select id, code, name, updated_at from instrument where coalesce(is_deleted, 0) = 1 "
                "order by updated_at desc limit 200"
            ):
                op, at = _deleter(conn, "instrument", r["id"])
                items.append({"entity_type": "instrument", "entity_id": r["id"], "kind": "器械",
                              "title": f'{r["name"] or ""} {r["code"] or ""}'.strip(),
                              "subtitle": "", "deleted_by": op, "deleted_at": at or r["updated_at"]})
            for r in conn.execute(  # #95 纳入作废账单
                "select b.bill_id id, b.bill_no, b.total_fee, b.updated_at, p.display_name "
                "from bills b left join patients p on p.patient_identity = b.patient_identity "
                "where b.state = 'voided' order by b.updated_at desc limit 200"
            ):
                op, at = _deleter(conn, "bill", r["id"])
                items.append({"entity_type": "bill", "entity_id": r["id"], "kind": "账单(作废)",
                              "title": f'{r["display_name"] or ""} · {r["bill_no"] or "账单"}'.strip(" ·"),
                              "subtitle": f'¥{r["total_fee"]}', "deleted_by": op, "deleted_at": at or r["updated_at"]})
        items.sort(key=lambda x: x["deleted_at"] or "", reverse=True)
        return {"items": items, "totalcount": len(items)}

    _RESTORE = {
        "return_visit": ("update return_visits set is_deleted = 0 where return_visit_id = ? and is_deleted = 1",
                         "return_visit", "restore_return_visit"),
        "consent_document": ("update consent_documents set status = 'signed' where document_id = ? and status = 'voided'",
                             "consent_document", "restore_consent"),
        "sterilize_dispatch": ("update sterilize_dispatch set is_deleted = 0 where id = ? and is_deleted = 1",
                               "sterilize_dispatch", "restore_dispatch"),
        "instrument": ("update instrument set is_deleted = 0 where id = ? and is_deleted = 1",
                       "instrument", "restore_instrument"),
        # 审查#8：'open' 不是合法账单状态(合法:pending/paid/refunded/voided)→ 改 pending(待收费)
        "bill": ("update bills set state = 'pending' where bill_id = ? and state = 'voided'",
                 "bill", "restore_bill"),
    }

    @router.post("/api/recycle-bin/restore")
    def restore(payload: dict):
        user = auth.require_login_user()
        payload = payload or {}
        et = payload.get("entity_type")
        eid = str(payload.get("entity_id") or "")
        if et not in _RESTORE:
            raise HTTPException(status_code=400, detail="不支持的还原类型")
        sql, atype, action = _RESTORE[et]
        with connect(db_path) as conn:
            # #94 仅本人(删除者)或管理员可还原
            is_admin = (
                user.get("is_system_admin") is True
                if access_v3
                else user.get("role") == "admin"
            )
            if not is_admin:
                op, _ = _deleter(conn, atype, eid)
                if not op or op != user.get("username"):
                    raise HTTPException(status_code=403, detail="只能还原自己删除的记录（或由管理员还原）")
            try:
                cur = conn.execute(sql, (eid,))
            except sqlite3.IntegrityError:
                raise HTTPException(status_code=409, detail="编号/单号已被重新占用，无法还原")
            if cur.rowcount == 0:
                raise HTTPException(status_code=404, detail="记录不存在或未处于已删除状态")
            # 审查#8：还原作废账单要级联还原它撤销时一起作废的处置单+明细,否则账单回来了但没明细(财务不一致)
            if et == "bill":
                order = conn.execute(
                    "select order_id from treatment_orders where bill_id = ? and status = 'voided'", (eid,)
                ).fetchone()
                if order:
                    conn.execute("update treatment_orders set status = 'priced' where order_id = ?", (order["order_id"],))
                    conn.execute("update treatment_items set is_deleted = 0 where order_id = ?", (order["order_id"],))
            audit_write(conn, atype, eid, action)
            conn.commit()
        return {"ok": True}

    return router
