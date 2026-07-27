"""技工单：送技工所做义齿/正畸等。
单据(lab_orders) + 配图(lab_order_images)，技工所/项目/比色候选项存 app_settings(KV)。
状态 sent已送出 → returned已返回(标记返回日期)。仿处置单 treatment_orders 的两层结构。"""
import json
from pathlib import Path

from fastapi import APIRouter, HTTPException, UploadFile, File
from fastapi.responses import FileResponse

from local_app.timeutil import now_str, today_str
from local_app.auth import audit_write, require_perm
from local_app.db import connect, new_id
from local_app.validation import valid_date_param

# lab-config 支持的三类候选项 → 对应 app_settings 的 key
_CONFIG_KINDS = {"factories": "lab_factories", "projects": "lab_projects", "colors": "lab_colors"}
_ALLOWED_IMG_SUFFIX = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"}
_MAX_IMG_BYTES = 20 * 1024 * 1024  # 单张配图上限 20MB(手机拍照足够)




def _audit(conn, order_id, action, new, now):
    audit_write(conn, "lab_order", order_id, action,
                new_json=json.dumps(new, ensure_ascii=False), created_at=now)


def _clean_date(value, field):
    """日期可空；非空则按 valid_date_param 校验。"""
    value = str(value or "").strip()
    if value:
        valid_date_param(value, field)
    return value


def create_lab_orders_router(db_path, images_dir):
    router = APIRouter()
    db_path = Path(db_path)
    images_dir = Path(images_dir)

    def _images_of(conn, order_id):
        rows = conn.execute(
            "select img_id, file_name, created_at from lab_order_images "
            "where lab_order_id = ? order by created_at, img_id",
            (order_id,),
        ).fetchall()
        return [{"img_id": r["img_id"], "file_name": r["file_name"],
                 "url": f"/api/lab-orders/images/{r['img_id']}/file"} for r in rows]

    # ---------- 在制技工台账（跨患者，P1-11） ----------
    @router.get("/api/lab-orders/in-progress")
    def in_progress_ledger(overdue_days: int = 7):
        """跨患者列出在制(status=sent)技工单：join 患者名，按送出日升序(最早最该催在前)，
        送出超过 overdue_days 天标 overdue。供工作台「在制义齿」催单。"""
        require_perm("lab_order.manage")   # 技工单(含患者名)读守卫用本模块对应权限
        with connect(db_path) as conn:
            rows = [dict(r) for r in conn.execute(
                """
                select l.order_id, l.patient_identity,
                       coalesce(p.display_name, '') as patient_name,
                       l.project_type, l.lab_name, l.tooth_codes, l.color,
                       l.sent_date, l.notes,
                       cast(julianday('now', 'localtime') - julianday(nullif(l.sent_date, '')) as integer) as days_sent
                from lab_orders l
                left join patients p on p.patient_identity = l.patient_identity
                where l.status = 'sent'
                order by (case when coalesce(l.sent_date, '') = '' then 1 else 0 end), l.sent_date asc, l.created_at asc
                """,
            ).fetchall()]
        for r in rows:
            ds = r.get("days_sent")
            r["overdue"] = ds is not None and ds > overdue_days
        return {"rows": rows, "count": len(rows), "overdue_days": overdue_days,
                "overdue_count": sum(1 for r in rows if r["overdue"])}

    # ---------- 列表（含配图） ----------
    @router.get("/api/patients/{patient_identity}/lab-orders")
    def list_orders(patient_identity: str):
        require_perm("lab_order.manage")   # 技工单读守卫用本模块对应权限
        with connect(db_path) as conn:
            if not conn.execute("select 1 from patients where patient_identity = ?", (patient_identity,)).fetchone():
                raise HTTPException(status_code=404, detail="patient not found")
            rows = conn.execute(
                "select order_id, tooth_codes, project_type, color, lab_name, sent_date, "
                "return_date, status, notes, created_at, updated_at "
                "from lab_orders where patient_identity = ? order by created_at desc",
                (patient_identity,),
            ).fetchall()
            orders = []
            for r in rows:
                o = {k: r[k] for k in r.keys()}
                o["images"] = _images_of(conn, r["order_id"])
                orders.append(o)
        return {"orders": orders, "totalcount": len(orders)}

    # ---------- 新增 ----------
    @router.post("/api/patients/{patient_identity}/lab-orders")
    def create_order(patient_identity: str, payload: dict):
        require_perm("lab_order.manage")
        payload = payload or {}
        project_type = str(payload.get("project_type") or "").strip()
        if not project_type:
            raise HTTPException(status_code=400, detail="项目必填")
        sent_date = _clean_date(payload.get("sent_date"), "sent_date")
        return_date = _clean_date(payload.get("return_date"), "return_date")
        now = now_str()
        order_id = new_id("local-lab")
        with connect(db_path) as conn:
            if not conn.execute("select 1 from patients where patient_identity = ?", (patient_identity,)).fetchone():
                raise HTTPException(status_code=404, detail="patient not found")
            conn.execute(
                "insert into lab_orders(order_id, patient_identity, tooth_codes, project_type, color, "
                "lab_name, sent_date, return_date, status, notes, created_at, updated_at) "
                "values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (order_id, patient_identity,
                 str(payload.get("tooth_codes") or "").strip(), project_type,
                 str(payload.get("color") or "").strip(), str(payload.get("lab_name") or "").strip(),
                 sent_date, return_date,
                 "returned" if return_date else "sent",
                 str(payload.get("notes") or "").strip(), now, now),
            )
            _audit(conn, order_id, "create_lab_order", {"patient": patient_identity, "project": project_type}, now)
            conn.commit()
        return {"order_id": order_id, "status": "returned" if return_date else "sent"}

    # ---------- 编辑 ----------
    @router.put("/api/patients/{patient_identity}/lab-orders/{order_id}")
    def update_order(patient_identity: str, order_id: str, payload: dict):
        require_perm("lab_order.manage")
        payload = payload or {}
        fields = ["tooth_codes", "project_type", "color", "lab_name", "sent_date", "return_date", "notes"]
        updates = {f: str(payload.get(f) or "").strip() for f in fields if f in payload}
        if "sent_date" in updates:
            updates["sent_date"] = _clean_date(updates["sent_date"], "sent_date")
        if "return_date" in updates:
            updates["return_date"] = _clean_date(updates["return_date"], "return_date")
        if "project_type" in updates and not updates["project_type"]:
            raise HTTPException(status_code=400, detail="项目不能清空")
        if not updates:
            raise HTTPException(status_code=400, detail="no editable fields")
        now = now_str()
        with connect(db_path) as conn:
            order = conn.execute(
                "select status, return_date from lab_orders where order_id = ? and patient_identity = ?",
                (order_id, patient_identity),
            ).fetchone()
            if not order:
                raise HTTPException(status_code=404, detail="技工单不存在")
            # 返回日期决定状态：填了=已返回，清空=回到已送出
            new_return = updates.get("return_date", order["return_date"])
            set_cols = ", ".join(f"{f} = ?" for f in updates)
            params = list(updates.values())
            conn.execute(
                f"update lab_orders set {set_cols}, status = ?, updated_at = ? where order_id = ?",
                params + ["returned" if new_return else "sent", now, order_id],
            )
            _audit(conn, order_id, "update_lab_order", updates, now)
            conn.commit()
        return {"order_id": order_id, "status": "returned" if new_return else "sent"}

    # ---------- 标记返回 ----------
    @router.put("/api/lab-orders/{order_id}/return")
    def mark_returned(order_id: str, payload: dict = None):
        require_perm("lab_order.manage")
        payload = payload or {}
        return_date = _clean_date(payload.get("return_date") or today_str(), "return_date")
        now = now_str()
        with connect(db_path) as conn:
            if not conn.execute("select 1 from lab_orders where order_id = ?", (order_id,)).fetchone():
                raise HTTPException(status_code=404, detail="技工单不存在")
            conn.execute(
                "update lab_orders set return_date = ?, status = 'returned', updated_at = ? where order_id = ?",
                (return_date, now, order_id),
            )
            _audit(conn, order_id, "return_lab_order", {"return_date": return_date}, now)
            conn.commit()
        return {"order_id": order_id, "status": "returned", "return_date": return_date}

    # ---------- 删除（连带配图文件） ----------
    @router.delete("/api/patients/{patient_identity}/lab-orders/{order_id}")
    def delete_order(patient_identity: str, order_id: str):
        require_perm("lab_order.manage")
        now = now_str()
        with connect(db_path) as conn:
            if not conn.execute(
                "select 1 from lab_orders where order_id = ? and patient_identity = ?",
                (order_id, patient_identity),
            ).fetchone():
                raise HTTPException(status_code=404, detail="技工单不存在")
            imgs = conn.execute(
                "select rel_path from lab_order_images where lab_order_id = ?", (order_id,)
            ).fetchall()
            conn.execute("delete from lab_order_images where lab_order_id = ?", (order_id,))
            conn.execute("delete from lab_orders where order_id = ?", (order_id,))
            _audit(conn, order_id, "delete_lab_order", {"deleted": True}, now)
            conn.commit()
        # DB 删成功后再清磁盘文件，失败不影响数据一致性
        for r in imgs:
            _safe_unlink(images_dir, r["rel_path"])
        return {"order_id": order_id, "deleted": True}

    # ---------- 上传配图（multipart） ----------
    @router.post("/api/lab-orders/{order_id}/images")
    def upload_image(order_id: str, file: UploadFile = File(...)):
        require_perm("lab_order.manage")
        suffix = Path(file.filename or "").suffix.lower()
        if suffix not in _ALLOWED_IMG_SUFFIX:
            raise HTTPException(status_code=400, detail="仅支持图片文件")
        data = file.file.read(_MAX_IMG_BYTES + 1)
        if len(data) > _MAX_IMG_BYTES:
            raise HTTPException(status_code=400, detail="图片超过 20MB")
        if not data:
            raise HTTPException(status_code=400, detail="空文件")
        now = now_str()
        img_id = new_id("local-labimg")
        rel_path = f"lab_orders/{order_id}/{img_id}{suffix}"
        with connect(db_path) as conn:
            if not conn.execute("select 1 from lab_orders where order_id = ?", (order_id,)).fetchone():
                raise HTTPException(status_code=404, detail="技工单不存在")
            dest = images_dir / rel_path
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data)
            conn.execute(
                "insert into lab_order_images(img_id, lab_order_id, rel_path, file_name, created_at) "
                "values (?, ?, ?, ?, ?)",
                (img_id, order_id, rel_path, str(file.filename or "")[:200], now),
            )
            _audit(conn, order_id, "add_lab_order_image", {"img_id": img_id}, now)
            conn.commit()
        return {"img_id": img_id, "url": f"/api/lab-orders/images/{img_id}/file"}

    # ---------- 配图文件 ----------
    @router.get("/api/lab-orders/images/{img_id}/file")
    def image_file(img_id: str):
        require_perm("lab_order.manage")   # 技工单图片读守卫用本模块对应权限
        with connect(db_path) as conn:
            row = conn.execute(
                "select rel_path from lab_order_images where img_id = ?", (img_id,)
            ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="image not found")
        images_root = images_dir.resolve()
        file_path = (images_dir / row["rel_path"]).resolve()
        if not file_path.is_relative_to(images_root) or not file_path.is_file():
            raise HTTPException(status_code=404, detail="image not found")
        return FileResponse(file_path)

    # ---------- 删除配图 ----------
    @router.delete("/api/lab-orders/images/{img_id}")
    def delete_image(img_id: str):
        require_perm("lab_order.manage")
        with connect(db_path) as conn:
            row = conn.execute(
                "select rel_path, lab_order_id from lab_order_images where img_id = ?", (img_id,)
            ).fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="image not found")
            conn.execute("delete from lab_order_images where img_id = ?", (img_id,))
            _audit(conn, row["lab_order_id"], "delete_lab_order_image", {"img_id": img_id}, now_str())
            conn.commit()
        _safe_unlink(images_dir, row["rel_path"])
        return {"img_id": img_id, "deleted": True}

    # ---------- 技工所/项目/比色候选项（KV） ----------
    @router.get("/api/lab-config/{kind}")
    def get_config(kind: str):
        key = _CONFIG_KINDS.get(kind)
        if not key:
            raise HTTPException(status_code=404, detail="未知配置类型")
        with connect(db_path) as conn:
            row = conn.execute("select value from app_settings where key = ?", (key,)).fetchone()
        try:
            data = json.loads(row["value"]) if row else {}
        except (ValueError, TypeError):
            data = {}
        return {"list": data.get("list", []) if isinstance(data, dict) else []}

    @router.put("/api/lab-config/{kind}")
    def put_config(kind: str, payload: dict):
        require_perm("lab_order.manage")
        key = _CONFIG_KINDS.get(kind)
        if not key:
            raise HTTPException(status_code=404, detail="未知配置类型")
        payload = payload or {}
        raw = payload.get("list")
        if not isinstance(raw, list):
            raise HTTPException(status_code=400, detail="list 格式不对")
        items = [str(x).strip() for x in raw if str(x).strip()]
        value = json.dumps({"list": items}, ensure_ascii=False)
        with connect(db_path) as conn:
            conn.execute(
                "insert into app_settings(key, value, updated_at) values (?, ?, ?) "
                "on conflict(key) do update set value = excluded.value, updated_at = excluded.updated_at",
                (key, value, now_str()),
            )
            conn.commit()
        return {"list": items}

    return router


def _safe_unlink(images_dir, rel_path):
    """删除 images_dir 下的相对路径文件，校验不逃逸；失败静默(文件可能已不存在)。"""
    try:
        images_root = images_dir.resolve()
        target = (images_dir / rel_path).resolve()
        if target.is_relative_to(images_root) and target.is_file():
            target.unlink()
    except OSError:
        pass
