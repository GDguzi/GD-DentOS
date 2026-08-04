"""知情同意书 (#15)：模板列表/详情 + 签署件创建/检索。
#15a 模板查询；#15b 签署件(填充+手写签名+内容哈希,防篡改;可信时间戳TSA二期接)。
模板=诊所现行同意书正文；签署件=填充后正文快照+患者/医生手写签名+内容哈希。"""
import json
import re
from pathlib import Path

from fastapi import APIRouter, HTTPException

from local_app.timeutil import now_str
from local_app.auth import audit_write, require_perm
from local_app.db import begin_immediate, connect, new_id
from local_app.versioning import stable_hash

# #63：手写签名必须是 canvas 产生的 data:image base64，挡注入/超大
_SIGN_RE = re.compile(r"^data:image/(png|jpeg);base64,[A-Za-z0-9+/=]+$")
_SIGN_MAX = 3_000_000


def _valid_sign(s):
    return bool(s) and len(s) <= _SIGN_MAX and bool(_SIGN_RE.match(s))




def _consent_hash(doc):
    """对签署件的实质内容算确定性哈希(防篡改)：患者/账单/处置单/模板归属 + 正文+字段+双方签名+模板名+签署时间。
    #59：纳入 patient_identity/bill_id/template_id；#74/#75：纳入 order_id，改任一项哈希即变；TSA(二期)对此哈希签发可信时间戳。"""
    return stable_hash({
        "patient_identity": doc.get("patient_identity", ""),
        "bill_id": doc.get("bill_id", ""),
        "order_id": doc.get("order_id", ""),
        "template_id": doc.get("template_id", ""),
        "template_name": doc.get("template_name", ""),
        "content_text": doc.get("content_text", ""),
        "content_json": doc.get("content_json", {}),
        "patient_sign": doc.get("patient_sign", ""),
        "doctor_sign": doc.get("doctor_sign", ""),
        "signed_at": doc.get("signed_at", ""),
    })


def create_consent_forms_router(db_path):
    router = APIRouter()
    db_path = Path(db_path)

    @router.get("/api/consent-templates")
    def list_templates(category: str = ""):
        """在用同意书模板列表（不含正文，供选择）。可按类别筛选。"""
        category = (category or "").strip()
        with connect(db_path) as conn:
            if category:
                rows = conn.execute(
                    "select template_id, name, category from consent_templates "
                    "where active = 1 and category = ? order by name",
                    (category,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "select template_id, name, category from consent_templates "
                    "where active = 1 order by category, name"
                ).fetchall()
        items = [{"template_id": r["template_id"], "name": r["name"], "category": r["category"]} for r in rows]
        return {"templates": items, "totalcount": len(items)}

    @router.post("/api/consent-templates")
    def create_template(payload: dict):
        """签署弹窗内直接新建模板(名称/类别/正文都必填);同名在用模板拒绝防重。"""
        require_perm("consent.manage")
        payload = payload or {}
        name = str(payload.get("name") or "").strip()
        category = str(payload.get("category") or "").strip()
        body = str(payload.get("body") or "").strip()
        if not name or not category or not body:
            raise HTTPException(status_code=400, detail="模板名称、类别、正文都不能为空")
        now = now_str()
        template_id = new_id("consent-tpl")
        with connect(db_path) as conn:
            begin_immediate(conn)
            dup = conn.execute(
                "select 1 from consent_templates where active = 1 and name = ?",
                (name,),
            ).fetchone()
            if dup:
                raise HTTPException(status_code=409, detail="已有同名的在用模板")
            conn.execute(
                "insert into consent_templates(template_id, name, category, body, source, "
                "active, created_at, updated_at) values (?, ?, ?, ?, 'local', 1, ?, ?)",
                (template_id, name, category, body, now, now),
            )
            conn.commit()
        return {"template_id": template_id, "name": name, "category": category}

    @router.get("/api/consent-templates/{template_id}")
    def get_template(template_id: str):
        """单个模板详情（含正文 body）。"""
        with connect(db_path) as conn:
            r = conn.execute(
                "select template_id, name, category, body, source from consent_templates "
                "where template_id = ? and active = 1",
                (template_id,),
            ).fetchone()
        if not r:
            raise HTTPException(status_code=404, detail="同意书模板不存在")
        return {"template_id": r["template_id"], "name": r["name"], "category": r["category"],
                "body": r["body"], "source": r["source"]}

    # ---------- 签署件（#15b）：填充+手写签名 → 内容哈希入库 ----------
    @router.post("/api/patients/{patient_identity}/consent-documents")
    def create_document(patient_identity: str, payload: dict):
        require_perm("consent.manage")
        payload = payload or {}
        template_name = str(payload.get("template_name") or "").strip()
        content_text = str(payload.get("content_text") or "").strip()
        patient_sign = str(payload.get("patient_sign") or "").strip()
        doctor_sign = str(payload.get("doctor_sign") or "").strip()
        # 签署方式：electronic(电子签,默认) / paper(纸质:打印后手签,系统只留档不存签名图)
        sign_method = "paper" if str(payload.get("sign_method") or "").strip() == "paper" else "electronic"
        if not content_text:
            raise HTTPException(status_code=400, detail="同意书正文不能为空")
        if sign_method == "paper":
            patient_sign = ""   # 纸质签署:签在打印件上,系统不存电子签名图
            doctor_sign = ""
        else:
            if not patient_sign:
                raise HTTPException(status_code=400, detail="缺少患者签名")
            if not doctor_sign:  # #60：医疗知情同意书必须有医生签名
                raise HTTPException(status_code=400, detail="缺少医生签名")
            # #63：签名必须是 canvas 的 data:image base64，挡注入/超大负载
            if not _valid_sign(patient_sign):
                raise HTTPException(status_code=400, detail="患者签名格式无效")
            if not _valid_sign(doctor_sign):
                raise HTTPException(status_code=400, detail="医生签名格式无效")
        content_json = payload.get("content_json")
        if not isinstance(content_json, dict):
            content_json = {}
        content_json["sign_method"] = sign_method   # 留痕:电子签 or 纸质
        bill_id = str(payload.get("bill_id") or "").strip()
        order_id = str(payload.get("order_id") or "").strip()
        template_id = str(payload.get("template_id") or "").strip()
        now = now_str()
        document_id = new_id("consent-doc")
        with connect(db_path) as conn:
            begin_immediate(conn)   # 抢写锁:纸质签署去重在锁内做,防双击/多设备/重试重复落档
            if not conn.execute("select 1 from patients where patient_identity = ?", (patient_identity,)).fetchone():
                raise HTTPException(status_code=404, detail="patient not found")
            # #59：账单必须属于本患者，防止把同意书绑到别人的费用单
            if bill_id and not conn.execute(
                "select 1 from bills where bill_id = ? and patient_identity = ?", (bill_id, patient_identity)
            ).fetchone():
                raise HTTPException(status_code=400, detail="账单不存在或不属于该患者")
            # #74/#75：处置单必须属于本患者，未划价先签也能稳定追溯到处置单
            if order_id and not conn.execute(
                "select 1 from treatment_orders where order_id = ? and patient_identity = ?", (order_id, patient_identity)
            ).fetchone():
                raise HTTPException(status_code=400, detail="处置单不存在或不属于该患者")
            # #17/#112：声明的模板必须真实存在且启用(active=1)，停用/废弃模板不能用于新签署
            if template_id and not conn.execute(
                "select 1 from consent_templates where template_id = ? and active = 1", (template_id,)
            ).fetchone():
                raise HTTPException(status_code=400, detail="同意书模板不存在或已停用")
            # 纸质签署幂等：同 患者+处置单+模板 已有未作废的纸质记录 → 复用,不重复落档。
            # (电子签每次签名都是有意的,不去重;只对 paper 去重,挡双击/多设备/重试)
            # "同模板"判定优先用 template_id(唯一主键)：同名不同模板(template_id 不同)绝不互相复用;
            # 仅无模板纸质或老记录(template_id 为空)才回退按 template_name 比对。
            if sign_method == "paper":
                _paper = "status != 'voided' and json_extract(iif(json_valid(content_json), content_json, '{}'), '$.sign_method') = 'paper'"
                if template_id:
                    dup = conn.execute(
                        "select document_id, content_hash, signed_at from consent_documents "
                        "where patient_identity = ? and coalesce(order_id, '') = ? and coalesce(template_id, '') = ? "
                        "and " + _paper + " order by created_at desc limit 1",
                        (patient_identity, order_id, template_id)).fetchone()
                else:
                    dup = conn.execute(
                        "select document_id, content_hash, signed_at from consent_documents "
                        "where patient_identity = ? and coalesce(order_id, '') = ? and coalesce(template_id, '') = '' "
                        "and template_name = ? and " + _paper + " order by created_at desc limit 1",
                        (patient_identity, order_id, template_name)).fetchone()
                if dup:
                    return {"document_id": dup["document_id"], "content_hash": dup["content_hash"],
                            "signed_at": dup["signed_at"], "status": "signed",
                            "sign_method": "paper", "reused": True}
            doc = {
                "patient_identity": patient_identity, "bill_id": bill_id, "order_id": order_id,
                "template_id": template_id,
                "template_name": template_name, "content_text": content_text, "content_json": content_json,
                "patient_sign": patient_sign, "doctor_sign": doctor_sign, "signed_at": now,
            }
            content_hash = _consent_hash(doc)
            conn.execute(
                """
                insert into consent_documents(
                    document_id, patient_identity, template_id, template_name, bill_id, order_id,
                    content_text, content_json, patient_sign, doctor_sign, content_hash,
                    tsa_token, status, signed_at, created_at, updated_at)
                values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '', 'signed', ?, ?, ?)
                """,
                (document_id, patient_identity, template_id,
                 template_name, bill_id, order_id,
                 content_text, json.dumps(content_json, ensure_ascii=False),
                 patient_sign, doctor_sign, content_hash, now, now, now),
            )
            audit_write(conn, "consent_document", document_id, "sign_consent",
                        new_json=json.dumps({"template_name": template_name, "content_hash": content_hash,
                                             "bill_id": bill_id, "order_id": order_id}, ensure_ascii=False),
                        created_at=now)
            conn.commit()
        return {"document_id": document_id, "content_hash": content_hash, "signed_at": now,
                "status": "signed", "sign_method": sign_method}

    @router.get("/api/patients/{patient_identity}/consent-documents")
    def list_documents(patient_identity: str):
        """该患者的签署件列表（不含签名图/正文，轻量）。"""
        require_perm("consent.manage")   # #482：知情同意签署件读守卫用本模块对应权限
        with connect(db_path) as conn:
            rows = conn.execute(
                "select document_id, template_id, template_name, bill_id, order_id, content_hash, tsa_token, status, signed_at, "
                "json_extract(iif(json_valid(content_json), content_json, '{}'), '$.sign_method') as sign_method "
                "from consent_documents where patient_identity = ? order by created_at desc",
                (patient_identity,),
            ).fetchall()
        docs = [{"document_id": r["document_id"], "template_id": r["template_id"],
                 "template_name": r["template_name"], "bill_id": r["bill_id"],
                 "order_id": r["order_id"],
                 "content_hash": r["content_hash"], "has_tsa": bool(r["tsa_token"]), "status": r["status"],
                 "signed_at": r["signed_at"],
                 # 旧件没记 sign_method 的都是电子签(纸质留档机制后加)
                 "sign_method": r["sign_method"] or "electronic"} for r in rows]
        return {"documents": docs, "totalcount": len(docs)}

    @router.get("/api/consent-documents/{document_id}")
    def get_document(document_id: str):
        """单份签署件完整内容（含正文+签名图+哈希），并核验哈希是否一致(防篡改)。"""
        require_perm("consent.manage")   # #482：签署件完整内容读守卫用本模块对应权限
        with connect(db_path) as conn:
            r = conn.execute("select * from consent_documents where document_id = ?", (document_id,)).fetchone()
        if not r:
            raise HTTPException(status_code=404, detail="同意书签署件不存在")
        try:
            content_json = json.loads(r["content_json"] or "{}")
        except (ValueError, TypeError):
            content_json = {}
        recomputed = _consent_hash({
            "patient_identity": r["patient_identity"], "bill_id": r["bill_id"], "order_id": r["order_id"],
            "template_id": r["template_id"],
            "template_name": r["template_name"], "content_text": r["content_text"],
            "content_json": content_json, "patient_sign": r["patient_sign"],
            "doctor_sign": r["doctor_sign"], "signed_at": r["signed_at"],
        })
        return {
            "document_id": r["document_id"], "patient_identity": r["patient_identity"],
            "template_id": r["template_id"], "template_name": r["template_name"], "bill_id": r["bill_id"],
            "order_id": r["order_id"],
            "content_text": r["content_text"], "content_json": content_json,
            "patient_sign": r["patient_sign"], "doctor_sign": r["doctor_sign"],
            "content_hash": r["content_hash"], "tsa_token": r["tsa_token"], "status": r["status"],
            "signed_at": r["signed_at"],
            "hash_valid": recomputed == r["content_hash"],  # 防篡改自检
        }

    @router.post("/api/consent-documents/{document_id}/void")
    def void_document(document_id: str, payload: dict = None):
        require_perm("consent.manage")
        """作废一份签署件（签了不能改，改只能作废重签）。不删数据，只置 voided + 写审计留痕；
        重签即另起一份 create_document。已作废的不可重复作废。"""
        payload = payload or {}
        reason = str(payload.get("reason") or "").strip()
        now = now_str()
        with connect(db_path) as conn:
            begin_immediate(conn)   # 抢写锁，防双击/并发作废致重复审计
            r = conn.execute(
                "select status from consent_documents where document_id = ?", (document_id,)
            ).fetchone()
            if not r:
                raise HTTPException(status_code=404, detail="同意书签署件不存在")
            if r["status"] == "voided":
                raise HTTPException(status_code=400, detail="该签署件已作废")
            conn.execute(
                "update consent_documents set status='voided', updated_at=? where document_id=?",
                (now, document_id),
            )
            audit_write(conn, "consent_document", document_id, "void_consent",
                        old_json=json.dumps({"status": r["status"]}, ensure_ascii=False),
                        new_json=json.dumps({"status": "voided", "reason": reason}, ensure_ascii=False),
                        created_at=now)
            conn.commit()
        return {"document_id": document_id, "status": "voided", "voided_at": now, "reason": reason}

    return router
