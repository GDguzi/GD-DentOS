"""患者合并：把重复建档的「次患者」的所有数据重指到「主患者」，标记次患者已合并。

同一个人重复建档(姓名+电话相同,不同 patient_identity/病历号)时用。合并不可逆——
全程写审计(含次患者完整快照可追溯),但不进回收站(级联还原过于复杂)。
核心逻辑在 merge_patients()(抽出):路由和运维脚本共用同一真相源。
"""
import json
from pathlib import Path

from fastapi import APIRouter, HTTPException

from local_app.timeutil import now_str
from local_app.auth import audit_operator, audit_write, require_admin
from local_app.db import begin_immediate, connect, new_id
from local_app.snapshots import patient_profile_snapshot
from local_app.versioning import stable_hash, stable_json

# 所有挂 patient_identity 的业务表(合并时把次患者的行重指到主患者)
# member_transactions 的 patient_identity 非主键,直接重指安全; member_accounts 主键是
# patient_identity,不能盲改(撞主键),在循环外按余额合并(见下)。
_MERGE_TABLES = [
    "appointments", "medical_records", "medical_record_items", "treatment_orders",
    "treatment_items", "treatment_plans", "bills", "payments", "return_visits",
    "consults", "surgeries", "oral_exams", "patient_images", "image_download_tasks",
    "lab_orders", "consent_documents", "local_notes", "patient_versions",
    "member_transactions",
    # 客户沟通：通话录音 / 沟通记录 / 微信截图（都带 patient_identity）也随合并改指主患者
    "calls", "communications", "communication_images",
    # 回访截图（新表漏登记的第二课）。此清单由 test_patient_merge 守卫测试对着
    # schema.sql 全量核对：新表带 patient_identity 却没进清单/豁免名单,测试直接红。
    "return_visit_images",
    # CT/CBCT 云端检查档案(study_key 为主键,重指 patient_identity 安全)
    "patient_ct_studies",
]
# 主患者该字段为空时,用次患者的值补全
_FILL_FIELDS = ["phone", "sex", "birthday", "address"]


def merge_patients(db_path, primary, secondary, reason, operator=None):
    """合并核心(路由/脚本共用)。校验失败抛 HTTPException;成功返回 {moved, filled}。
    operator 不传时取请求上下文操作人(脚本调用请显式传,如 'merge-script')。"""
    op = operator or audit_operator()
    now = now_str()
    with connect(db_path) as conn:
        begin_immediate(conn)   # 抢写锁，防双击/并发合并致版本链幻象合并+审计双写
        pm = conn.execute("select * from patients where patient_identity=?", (primary,)).fetchone()
        se = conn.execute("select * from patients where patient_identity=?", (secondary,)).fetchone()
        if not pm:
            raise HTTPException(status_code=404, detail="主患者不存在")
        if not se:
            raise HTTPException(status_code=404, detail="次患者不存在")
        keys = pm.keys()
        if "is_deleted" in keys and (pm["is_deleted"] or 0):
            raise HTTPException(status_code=400, detail="主患者已被合并/删除,不能作为合并目标")
        if ("merged_into" in keys and (se["merged_into"] or "")) or ("is_deleted" in keys and (se["is_deleted"] or 0)):
            raise HTTPException(status_code=409, detail="次患者已被合并过")
        se_snapshot = {k: se[k] for k in se.keys()}
        # 1) 把 _MERGE_TABLES 登记的业务表里次患者的行重指到主患者
        moved = {}
        for t in _MERGE_TABLES:
            cur = conn.execute(f"update {t} set patient_identity=? where patient_identity=?", (primary, secondary))
            if cur.rowcount:
                moved[t] = cur.rowcount
        # 1b) 会员储值账户：patient_identity 是主键不能盲改(撞主键),按余额合并。
        # 否则次患者预存余额随次账户被软删而成孤儿、主患者读不到 → 静默丢钱(对账缺口)。
        sec_acct = conn.execute(
            "select balance from member_accounts where patient_identity=?", (secondary,)).fetchone()
        if sec_acct:
            sec_bal = round(sec_acct["balance"] or 0, 2)
            pri_acct = conn.execute(
                "select balance from member_accounts where patient_identity=?", (primary,)).fetchone()
            if pri_acct:
                # 主已有账户：次余额累加到主，删次账户(其流水上面已重指到主)
                new_bal = round((pri_acct["balance"] or 0) + sec_bal, 2)
                conn.execute("update member_accounts set balance=?, updated_at=? where patient_identity=?",
                             (new_bal, now, primary))
                conn.execute("delete from member_accounts where patient_identity=?", (secondary,))
                # 余额迁移必须有流水,否则主余额变了无任何 member_transactions 能解释(改钱审计断链)。
                # 记一笔"合并入账",balance_after=合并后新余额,让审计链可追溯当前余额。
                if sec_bal:
                    conn.execute(
                        "insert into member_transactions(txn_id, patient_identity, txn_type, amount, "
                        "balance_after, note, bill_id, operator, created_at) "
                        "values (?, ?, 'merge_in', ?, ?, ?, '', ?, ?)",
                        (new_id("local-mtxn"), primary, sec_bal, new_bal,
                         f"患者合并:次患者储值 {sec_bal} 元并入(次患者ID {secondary})",
                         op, now))
            else:
                # 主无账户：次账户直接改指主
                conn.execute("update member_accounts set patient_identity=?, updated_at=? where patient_identity=?",
                             (primary, now, secondary))
            moved["member_accounts"] = 1
            moved["member_balance_moved"] = sec_bal
        # 1c) 影像日期分组：主键(patient_identity, image_date)不能盲改(两边同日期撞主键)，
        # 先给主患者补上缺的日期组，再删次患者的——照 member_accounts 特例套路。
        cur = conn.execute(
            "insert or ignore into patient_image_sets(patient_identity, image_date, created_at) "
            "select ?, image_date, created_at from patient_image_sets where patient_identity=?",
            (primary, secondary))
        cur = conn.execute("delete from patient_image_sets where patient_identity=?", (secondary,))
        if cur.rowcount:
            moved["patient_image_sets"] = cur.rowcount
        # 1d) 患者标签：tag_id 独立主键可直接重指；重指后主患者同名标签去重
        # (保留 tag_id 最小的一条，确定性取舍；产品决策=标签跟过去+去重)。
        cur = conn.execute("update patient_tags set patient_identity=? where patient_identity=?",
                           (primary, secondary))
        if cur.rowcount:
            moved["patient_tags"] = cur.rowcount
            conn.execute(
                "delete from patient_tags where patient_identity=? and tag_id not in "
                "(select min(tag_id) from patient_tags where patient_identity=? group by name)",
                (primary, primary))
        # 2) 主患者空字段用次患者补全
        filled = {}
        for f in _FILL_FIELDS:
            if not str(pm[f] or "").strip() and str(se[f] or "").strip():
                filled[f] = se[f]
        if filled:
            sets = ", ".join(f"{f}=?" for f in filled)
            # 补全主患者字段=本地编辑,要留版本快照+更新 current_hash(否则版本链漏这次合并变更)
            old_snap = patient_profile_snapshot(pm)
            conn.execute("insert into patient_versions(patient_identity, version_hash, source_json, batch_id) "
                         "values (?, ?, ?, ?)", (primary, stable_hash(old_snap), stable_json(old_snap), None))
            new_snap = dict(old_snap); new_snap.update(filled); new_snap["updated_at"] = now
            # current_hash 用 new_snap(updated_at=now)算,UPDATE 必须同时写 updated_at=now,
            # 否则 row.updated_at 留旧值→current_hash != stable_hash(patient_profile_snapshot(row))。
            # 口径=合并后整行快照可复现(merge 不加 edit_source,故 hash 能从行复算)。
            conn.execute(f"update patients set {sets}, current_hash=?, updated_at=?, local_updated_at=? where patient_identity=?",
                         tuple(filled.values()) + (stable_hash(new_snap), now, now, primary))
        # 3) 标记次患者已合并(列表/计数/搜索都按 is_deleted 排除)
        conn.execute("update patients set merged_into=?, is_deleted=1, local_updated_at=? where patient_identity=?",
                     (primary, now, secondary))
        # 4) 审计：主、次各一条；次记全量快照可追溯
        audit_write(
            conn, "patient", primary, "merge_patient_master",
            new_json=json.dumps({"merged_from": secondary, "reason": reason, "filled": filled, "moved": moved},
                                ensure_ascii=False, default=str),
            operator=op, created_at=now,
        )
        audit_write(
            conn, "patient", secondary, "merge_patient_secondary",
            new_json=json.dumps({"merged_into": primary, "reason": reason, "snapshot": se_snapshot},
                                ensure_ascii=False, default=str),
            operator=op, created_at=now,
        )
        conn.commit()
    return {"moved": moved, "filled": filled}


def create_patient_merge_router(db_path):
    router = APIRouter()
    db_path = Path(db_path)

    @router.post("/api/patients/{primary}/merge")
    def merge_patient(primary: str, payload: dict):
        require_admin()   # 合并不可逆且跨表重指数据,仅管理员(前台等非管理员 403)
        payload = payload or {}
        secondary = str(payload.get("secondary") or "").strip()
        reason = str(payload.get("reason") or "").strip()
        if not secondary:
            raise HTTPException(status_code=400, detail="缺少要合并的次患者")
        if secondary == primary:
            raise HTTPException(status_code=400, detail="不能与自己合并")
        if not reason:
            raise HTTPException(status_code=400, detail="请填写合并原因")
        result = merge_patients(db_path, primary, secondary, reason)
        return {"ok": True, "primary": primary, "secondary": secondary,
                "moved": result["moved"], "filled": list(result["filled"].keys())}

    return router
