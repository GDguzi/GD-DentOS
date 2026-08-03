import hashlib
import json
import logging
import os
import re
import uuid
from pathlib import Path

from fastapi import APIRouter, Body, File, Form, HTTPException, UploadFile
from local_app.timeutil import now_str, today_str
from local_app.auth import audit_write, require_perm
from fastapi.responses import FileResponse

from local_app.access_authorization import has_access, require_access
from local_app.db import begin_immediate, connect
from local_app.routes.patient_common import require_patient   # 审计R2:#580 软删口径收敛到共享校验
from local_app.snapshots import row_to_dict

# 标准口内照拍摄模板：slot(引导机位) -> (category 落库分类, tooth_codes 牙位)。
# 分类名对齐原系统存量(…像)，使拍摄照片与同步影像归入同一分类。
# 颌面 8 机位落两类(上颌/下颌合面像)，组靠 tooth_codes 区分；主诉牙特写牙位拍时手选(模板值 None)。
CAPTURE_TEMPLATE = {
    "正面咬合":   ("正面咬合像", "13,12,11,21,22,23,43,42,41,31,32,33"),
    "右侧咬合":   ("右侧咬合像", "18,17,16,15,14,48,47,46,45,44"),
    "左侧咬合":   ("左侧咬合像", "24,25,26,27,28,34,35,36,37,38"),
    "上颌右·组1": ("上颌合面像", "11,12,13,14"),
    "上颌右·组2": ("上颌合面像", "15,16,17,18"),
    "上颌左·组1": ("上颌合面像", "21,22,23,24"),
    "上颌左·组2": ("上颌合面像", "25,26,27,28"),
    "下颌右·组1": ("下颌合面像", "41,42,43,44"),
    "下颌右·组2": ("下颌合面像", "45,46,47,48"),
    "下颌左·组1": ("下颌合面像", "31,32,33,34"),
    "下颌左·组2": ("下颌合面像", "35,36,37,38"),
    "主诉牙特写①": ("主诉牙特写", None),
    "主诉牙特写②": ("主诉牙特写", None),
}

_FDI_RE = re.compile(r"^[1-8][1-8]$")   # 恒牙11-48 + 乳牙51-85(象限5-8)
LOCAL_UPLOAD_SLOT = "本地上传"            # 本地选图/拖拽上传的机位哨兵(非模板)
VIDEO_SLOT = "录像"                      # 内窥镜录像的机位哨兵(非模板),存视频文件
VIDEO_MAX_BYTES = 200 * 1024 * 1024      # 录像上限 200MB(约10分钟)


def resolve_capture(slot, manual_tooth):
    """把引导机位 slot 解析成 (category, tooth_codes)。
    固定机位用模板牙位(忽略前端)；主诉牙特写用手选牙位并校验 FDI。未知 slot → 400。"""
    if slot not in CAPTURE_TEMPLATE:
        raise HTTPException(status_code=400, detail="未知拍摄机位")
    category, tpl_tooth = CAPTURE_TEMPLATE[slot]
    if tpl_tooth is not None:
        return category, tpl_tooth
    codes = [c.strip() for c in str(manual_tooth or "").split(",") if c.strip()]
    for c in codes:
        if not _FDI_RE.match(c):
            raise HTTPException(status_code=400, detail=f"非法牙位:{c}")
    return category, ",".join(codes)


ITEM_FIELDS = (
    "treatment_item_id",
    "item_name",
    "item_code",
    "doctor_name",
    "unit_price",
    "quantity",
    "total_fee",
    "fee_type",
    "is_refund",
    "is_deleted",
)


def _item_dict(row):
    return {key: row[key] for key in ITEM_FIELDS}


# 正畸标准视图位标签(与前端 image_grouping.js 的映射表逐字一致)
IMAGE_SET_SLOT_LABELS = (
    "右侧面像", "右侧面微笑像", "右45度像", "右45度微笑像", "正面像", "正面微笑像",
    "左45度微笑像", "左45度像", "左侧面微笑像", "左侧面像",
    "上颌合面像", "覆盖像", "右侧咬合像", "正面咬合像", "左侧咬合像", "下颌合面像",
    "全景片", "正位片", "侧位片", "小牙片",
)


def ensure_seed_image_sets(db_path):
    """一次性种子:历史上真正成套的期(当天>=6个不同视图位标签,SaaS正畸组图特征)预标为组图。
    此后组图与否只认医生手点(#534跟进,用户拍板:自动不判组);幂等,app_settings 记标记。"""
    with connect(db_path) as conn:
        done = conn.execute(
            "select 1 from app_settings where key = 'migration.534_seed_image_sets'"
        ).fetchone()
        if done:
            return
        placeholders = ",".join("?" for _ in IMAGE_SET_SLOT_LABELS)
        now = now_str()
        conn.execute(
            f"""
            insert or ignore into patient_image_sets(patient_identity, image_date, created_at)
            select patient_identity, substr(coalesce(image_date,''),1,10), ?
            from patient_images
            where category in ({placeholders}) and coalesce(image_date,'') != ''
            group by patient_identity, substr(coalesce(image_date,''),1,10)
            having count(distinct category) >= 6
            """,
            (now, *IMAGE_SET_SLOT_LABELS),
        )
        conn.execute(   # #579:or ignore 防新库首批并发请求都过了上面的 done 检查后撞主键 500
            "insert or ignore into app_settings(key, value, updated_at) values ('migration.534_seed_image_sets', '1', ?)",
            (now,),
        )
        conn.commit()


def create_patient_assets_router(db_path, images_dir):
    router = APIRouter()
    db_path = Path(db_path)
    images_dir = Path(images_dir)

    # 种子迁移懒执行:create_app 不碰库(坏库也要能启动,错误按请求暴露——test_error_handler 契约)
    _set_seed_done = []

    def _seed_image_sets_once():
        if _set_seed_done:
            return
        ensure_seed_image_sets(db_path)
        _set_seed_done.append(1)

    @router.get("/api/patients/{patient_identity}/treatments")
    def patient_treatments(patient_identity: str, include_deleted: bool = False):
        require_access("patient.profile.view")
        see_billing = has_access("billing.view")
        with connect(db_path) as conn:
            require_patient(conn, patient_identity)
            if see_billing:
                rows = conn.execute(
                    """
                    select
                        ti.treatment_item_id, ti.item_name, ti.item_code, ti.doctor_name,
                        ti.unit_price, ti.quantity, ti.total_fee, ti.fee_type,
                        ti.is_refund, ti.is_deleted, ti.order_id,
                        b.bill_id as linked_bill_id, b.bill_no, b.bill_time,
                        b.total_fee as bill_total_fee
                    from treatment_items ti
                    left join bills b on b.bill_id = ti.bill_id
                    where ti.patient_identity = ? and (? = 1 or ti.is_deleted = 0)
                    order by coalesce(b.bill_time, '') desc, ti.bill_id, ti.treatment_item_id
                    """,
                    (patient_identity, int(include_deleted)),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    select
                        ti.treatment_item_id, ti.item_name, ti.item_code, ti.doctor_name,
                        ti.quantity, ti.fee_type, ti.is_refund, ti.is_deleted, ti.order_id,
                        b.bill_id as linked_bill_id, b.bill_no, b.bill_time
                    from treatment_items ti
                    left join bills b on b.bill_id = ti.bill_id
                    where ti.patient_identity = ? and (? = 1 or ti.is_deleted = 0)
                    order by coalesce(b.bill_time, '') desc, ti.bill_id, ti.treatment_item_id
                    """,
                    (patient_identity, int(include_deleted)),
                ).fetchall()

        visits = []
        visit_by_bill = {}
        unlinked_items = []
        for row in rows:
            if see_billing:
                item = _item_dict(row)
            else:
                item = {
                    key: row[key]
                    for key in (
                        "treatment_item_id",
                        "item_name",
                        "item_code",
                        "doctor_name",
                        "quantity",
                        "fee_type",
                        "is_refund",
                        "is_deleted",
                    )
                }
            bill_id = row["linked_bill_id"]
            if not bill_id:
                # #381:本地处置单的未划价项目(带 order_id)已在「本地处置单」卡里展示,
                # 不再进「未关联就诊」重复出现;真正的孤儿项目(无单无账)才进这里。
                if row["order_id"]:
                    continue
                unlinked_items.append(item)
                continue
            visit = visit_by_bill.get(bill_id)
            if visit is None:
                visit = {
                    "bill_id": bill_id,
                    "bill_no": row["bill_no"],
                    "bill_time": row["bill_time"],
                    "doctor_names": [],
                    "items": [],
                }
                if see_billing:
                    visit["bill_total_fee"] = row["bill_total_fee"]
                visit_by_bill[bill_id] = visit
                visits.append(visit)
            doctor_name = row["doctor_name"]
            if doctor_name and doctor_name not in visit["doctor_names"]:
                visit["doctor_names"].append(doctor_name)
            visit["items"].append(item)

        return {
            "visits": visits,
            "unlinked_items": unlinked_items,
            "totalcount": len(rows),
        }

    @router.get("/api/patients/{patient_identity}/visits")
    def patient_visits(patient_identity: str):
        """就诊时间轴：把该患者的病历/收费/预约/回访按天聚合，按日期倒序返回。
        对应官方"就诊信息"页（按天的就诊全景）。只取正式病历（排除 AI 草稿/废弃）。
        日期取各自时间字段前 10 位，空或 0000-00-00 跳过。"""
        require_access("patient.profile.view")
        see_billing = has_access("billing.view")
        with connect(db_path) as conn:
            require_patient(conn, patient_identity)
            med_rows = conn.execute(
                """
                select record_id, record_type, doctor_name, visit_time
                from medical_records
                where patient_identity = ? and coalesce(draft_status, '') = ''
                """,
                (patient_identity,),
            ).fetchall()
            if see_billing:
                bill_rows = conn.execute(
                    "select bill_id, bill_no, bill_time, total_fee, paid_fee "
                    "from bills where patient_identity = ?",
                    (patient_identity,),
                ).fetchall()
            else:
                bill_rows = conn.execute(
                    "select bill_id, bill_no, bill_time "
                    "from bills where patient_identity = ?",
                    (patient_identity,),
                ).fetchall()
            appt_rows = conn.execute(
                "select appointment_id, start_time, doctor_name, item_name, status "
                "from appointments where patient_identity = ?",
                (patient_identity,),
            ).fetchall()
            rv_rows = conn.execute(
                "select return_visit_id, due_time, item_name, status "
                "from return_visits where patient_identity = ? "
                "and coalesce(is_deleted, 0) = 0",
                (patient_identity,),
            ).fetchall()
            # 处置明细经 bill_id 关联账单取日期（treatment_items 自身无日期列）
            if see_billing:
                ti_rows = conn.execute(
                    """
                    select ti.treatment_item_id, ti.item_name, ti.doctor_name,
                           ti.quantity, ti.total_fee, b.bill_time
                    from treatment_items ti
                    left join bills b on b.bill_id = ti.bill_id
                    where ti.patient_identity = ? and ti.is_deleted = 0
                    """,
                    (patient_identity,),
                ).fetchall()
            else:
                ti_rows = conn.execute(
                    """
                    select ti.treatment_item_id, ti.item_name, ti.doctor_name,
                           ti.quantity, b.bill_time
                    from treatment_items ti
                    left join bills b on b.bill_id = ti.bill_id
                    where ti.patient_identity = ? and ti.is_deleted = 0
                    """,
                    (patient_identity,),
                ).fetchall()

        days = {}

        def day_of(value):
            d = (value or "")[:10]
            return d if d and d != "0000-00-00" else None

        def bucket(date):
            b = days.get(date)
            if b is None:
                b = {
                    "date": date,
                    "medical_records": [],
                    "treatments": [],
                    "bills": [],
                    "appointments": [],
                    "return_visits": [],
                }
                days[date] = b
            return b

        for r in med_rows:
            d = day_of(r["visit_time"])
            if d:
                bucket(d)["medical_records"].append(
                    {
                        "record_id": r["record_id"],
                        "record_type": r["record_type"],
                        "doctor_name": r["doctor_name"],
                    }
                )
        for r in bill_rows:
            d = day_of(r["bill_time"])
            if d:
                bill = {"bill_id": r["bill_id"], "bill_no": r["bill_no"]}
                if see_billing:
                    bill["total_fee"] = r["total_fee"]
                    bill["paid_fee"] = r["paid_fee"]
                bucket(d)["bills"].append(bill)
        for r in appt_rows:
            d = day_of(r["start_time"])
            if d:
                bucket(d)["appointments"].append(
                    {
                        "appointment_id": r["appointment_id"],
                        "start_time": r["start_time"],   # 就诊行显示时分(visitApptLine 取 a.start_time);#444/#446
                        "doctor_name": r["doctor_name"],
                        "item_name": r["item_name"],
                        "status": r["status"],
                    }
                )
        for r in rv_rows:
            d = day_of(r["due_time"])
            if d:
                bucket(d)["return_visits"].append(
                    {
                        "return_visit_id": r["return_visit_id"],
                        "item_name": r["item_name"],
                        "status": r["status"],
                    }
                )

        for r in ti_rows:
            d = day_of(r["bill_time"])
            if d:
                item = {
                    "treatment_item_id": r["treatment_item_id"],
                    "item_name": r["item_name"],
                    "doctor_name": r["doctor_name"],
                    "quantity": r["quantity"],
                }
                if see_billing:
                    item["total_fee"] = r["total_fee"]
                bucket(d)["treatments"].append(item)

        visits = sorted(days.values(), key=lambda b: b["date"], reverse=True)
        return {"visits": visits, "totalcount": len(visits)}

    @router.get("/api/patients/{patient_identity}/summary")
    def patient_summary(patient_identity: str):
        require_access("patient.profile.view")
        see_billing = has_access("billing.view")
        with connect(db_path) as conn:
            patient = conn.execute(
                """
                select
                    patient_identity, source_customer_id, display_name, phone,
                    sex, birthday, address, updated_at
                from patients
                where patient_identity = ?
                """,
                (patient_identity,),
            ).fetchone()
            if not patient:
                raise HTTPException(status_code=404, detail="patient not found")
            last_visit_time = conn.execute(
                "select max(visit_time) from medical_records where patient_identity = ?",
                (patient_identity,),
            ).fetchone()[0]
            counts = {
                "medical_records": conn.execute(
                    "select count(*) from medical_records where patient_identity = ?",
                    (patient_identity,),
                ).fetchone()[0],
                "appointments": conn.execute(
                    "select count(*) from appointments where patient_identity = ?",
                    (patient_identity,),
                ).fetchone()[0],
                "return_visits": conn.execute(
                    "select count(*) from return_visits where patient_identity = ?",
                    (patient_identity,),
                ).fetchone()[0],
                "local_notes": conn.execute(
                    "select count(*) from local_notes where patient_identity = ?",
                    (patient_identity,),
                ).fetchone()[0],
                "images": conn.execute(
                    "select count(*) from patient_images where patient_identity = ?",
                    (patient_identity,),
                ).fetchone()[0],
            }
            if see_billing:
                counts.update(
                    {
                        "bills": conn.execute(
                            "select count(*) from bills where patient_identity = ?",
                            (patient_identity,),
                        ).fetchone()[0],
                        "payments": conn.execute(
                            "select count(*) from payments where patient_identity = ?",
                            (patient_identity,),
                        ).fetchone()[0],
                        "treatment_items": conn.execute(
                            "select count(*) from treatment_items where patient_identity = ?",
                            (patient_identity,),
                        ).fetchone()[0],
                    }
                )
            # 经营概览(大客户经营)：累计消费/到诊次数/首诊——一眼看全消费与到诊轨迹。
            # 累计消费口径与 today_cash_in/收银查询一致(#276)：排作废原单(CancelMark非空且金额≥0),
            # 保留退费负数冲减→净实收,避免作废收款虚高大客户识别。
            if see_billing:
                total_spend = conn.execute(
                    "select round(coalesce(sum(amount),0),2) from payments where patient_identity = ? "
                    "and (coalesce(json_extract(iif(json_valid(source_json), source_json, '{}'), '$.CancelMark'), '') = '' "
                    "     or coalesce(amount, 0) < 0)",
                    (patient_identity,),
                ).fetchone()[0]
            visit_count = conn.execute(
                "select count(*) from appointments where patient_identity = ? and coalesce(arrived_at,'') <> ''",
                (patient_identity,),
            ).fetchone()[0]
            first_visit = conn.execute(
                "select min(visit_time) from medical_records where patient_identity = ? and coalesce(visit_time,'') <> ''",
                (patient_identity,),
            ).fetchone()[0]

        business = {
            "visit_count": visit_count,
            "first_visit": first_visit or "",
            "last_visit": last_visit_time or "",
        }
        if see_billing:
            business["total_spend"] = float(total_spend or 0)

        return {
            "patient": row_to_dict(patient),
            "last_visit_time": last_visit_time,
            "counts": counts,
            "business": business,
        }

    @router.get("/api/patients/{patient_identity}/images")
    def patient_image_list(patient_identity: str, category: str = ""):
        require_perm("patient.view")   # 越权:读患者/病历数据
        _seed_image_sets_once()
        category = category.strip()
        where_sql = "where patient_identity = ?"
        params = [patient_identity]
        if category:
            where_sql += " and category = ?"
            params.append(category)
        with connect(db_path) as conn:
            require_patient(conn, patient_identity)
            images = conn.execute(
                f"""
                select image_id, image_date, seq, category, tooth_codes, file_name, size_bytes
                from patient_images
                {where_sql}
                order by coalesce(image_date, '') desc, seq asc, image_id
                """,
                tuple(params),
            ).fetchall()
            categories = conn.execute(
                """
                select category, count(*) as count
                from patient_images
                where patient_identity = ?
                group by category
                order by category
                """,
                (patient_identity,),
            ).fetchall()
            set_dates = [r["image_date"] for r in conn.execute(
                "select image_date from patient_image_sets where patient_identity = ? order by image_date",
                (patient_identity,),
            ).fetchall()]

        return {
            "list": [row_to_dict(row) for row in images],
            "categories": [row_to_dict(row) for row in categories],
            "set_dates": set_dates,
            "totalcount": len(images),
        }

    @router.get("/api/patients/{patient_identity}/images/{image_id}/file")
    def patient_image_file(patient_identity: str, image_id: str):
        require_perm("patient.view")   # 越权:读患者/病历数据
        with connect(db_path) as conn:
            row = conn.execute(
                """
                select rel_path from patient_images
                where image_id = ? and patient_identity = ?
                """,
                (image_id, patient_identity),
            ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="image not found")
        images_root = images_dir.resolve()
        file_path = (images_dir / row["rel_path"]).resolve()
        if not file_path.is_relative_to(images_root):
            raise HTTPException(status_code=404, detail="image not found")
        if not file_path.is_file():
            raise HTTPException(status_code=404, detail="image not found")
        return FileResponse(file_path)

    @router.post("/api/patients/{patient_identity}/images/{image_id}/orient")
    def orient_patient_image(patient_identity: str, image_id: str, payload: dict):
        """把灯箱里的旋转/镜像真正写回图片文件(内窥镜有些片子拍反/镜像,每次看每次转没意义)。
        变换口径与前端CSS一致:先镜像后旋转(transform: rotate(r) scale(fx,fy) 右侧先作用)。"""
        require_perm("patient.edit")   # 越权:写患者/病历数据
        payload = payload or {}
        rotate = payload.get("rotate")
        flip_x = bool(payload.get("flip_x"))
        flip_y = bool(payload.get("flip_y"))
        if rotate not in (0, 90, 180, 270):
            raise HTTPException(status_code=400, detail="rotate 只能是 0/90/180/270")
        if rotate == 0 and not flip_x and not flip_y:
            raise HTTPException(status_code=400, detail="没有需要保存的旋转/镜像")
        with connect(db_path) as conn:
            row = conn.execute(
                "select rel_path, file_name from patient_images where image_id = ? and patient_identity = ?",
                (image_id, patient_identity),
            ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="image not found")
        if re.search(r"\.(webm|mp4)$", str(row["file_name"] or ""), re.I):
            raise HTTPException(status_code=400, detail="视频不支持旋转保存")
        images_root = images_dir.resolve()
        file_path = (images_dir / row["rel_path"]).resolve()
        if not file_path.is_relative_to(images_root) or not file_path.is_file():
            raise HTTPException(status_code=404, detail="image not found")
        try:
            from PIL import Image, ImageOps
        except ImportError:
            raise HTTPException(status_code=501, detail="服务器缺少图像库(Pillow),无法保存旋转")
        try:
            with Image.open(file_path) as img:
                img.load()
                # #637:带EXIF方向标签的照片(手机翻拍),浏览器显示时已按标签转正;先把像素转成
                # "显示的样子"再做用户变换,保存后不带标签,方向才与灯箱所见一致(内窥镜片无标签,等价原样)
                out = ImageOps.exif_transpose(img)
                if flip_x:
                    out = out.transpose(Image.Transpose.FLIP_LEFT_RIGHT)
                if flip_y:
                    out = out.transpose(Image.Transpose.FLIP_TOP_BOTTOM)
                if rotate:
                    # CSS rotate 顺时针;PIL ROTATE_* 逆时针 → 90↔270 对调
                    out = out.transpose({90: Image.Transpose.ROTATE_270,
                                         180: Image.Transpose.ROTATE_180,
                                         270: Image.Transpose.ROTATE_90}[rotate])
                save_kwargs = {"quality": 95} if file_path.suffix.lower() in (".jpg", ".jpeg") else {}
                # R6+五轮P2:旋转结果写成"新文件"、锁内切库指针(同头像方案)——库切换成功前旧图分毫不动:
                # ①写一半掉盘=只丢新文件;②库更新/审计失败=响应5xx且盘上仍是旧图旧哈希,两边一致;
                # ③与并发删除/并发旋转的竞态在锁内按行现状裁决,绝不留孤儿或虚假成功。
                # 新名与旧文件名无关且定长:若沿用旧主干追加后缀,每转一次名字长一截,
                # 二十多次后撞文件名上限;定长随机名永不累积,溯源走库里 rel_path/audit
                new_name = f"r{uuid.uuid4().hex}{file_path.suffix}"
                new_path = file_path.with_name(new_name)
                try:
                    out.save(new_path, format=img.format, **save_kwargs)
                except Exception:
                    try:
                        new_path.unlink(missing_ok=True)   # 失败不留半截新文件
                    except OSError:
                        pass   # 清理自身的报错不得顶掉下面的 500(否则会被外层误判成 400"图片损坏")
                    logging.getLogger("local_app").exception("旋转保存写盘失败 image_id=%s", image_id)
                    raise HTTPException(status_code=500, detail="旋转保存写盘失败")
        except HTTPException:
            raise
        except Exception:
            # 只有真的打不开/解码失败才算"文件损坏";写盘失败已在上面按 500 上报
            raise HTTPException(status_code=400, detail="图片处理失败(文件损坏或格式不支持)")
        # 自此以下任何失败都只删新文件(读取/算哈希失败同样不得留未入库的新文件)
        try:
            data = new_path.read_bytes()
            content_hash = hashlib.sha1(data).hexdigest()   # 与本地上传同口径(sha1)
            now = now_str()
            new_rel = str(Path(row["rel_path"]).with_name(new_name))
            with connect(db_path) as conn:
                begin_immediate(conn)
                # 锁内复核行现状(五轮P2):行没了=影像已被删,rel_path 变了=被并发旋转/替换抢先——
                # 都丢弃新文件不写库
                cur = conn.execute(
                    "select rel_path from patient_images where image_id = ? and patient_identity = ?",
                    (image_id, patient_identity),
                ).fetchone()
                if not cur:
                    raise HTTPException(status_code=404, detail="image not found")
                if str(cur["rel_path"]) != str(row["rel_path"]):
                    raise HTTPException(status_code=409, detail="影像已被其他操作修改,请刷新后重试")
                conn.execute(
                    "update patient_images set rel_path = ?, file_name = ?, size_bytes = ?, content_hash = ? "
                    "where image_id = ?",
                    (new_rel, new_name, len(data), content_hash, image_id),
                )
                audit_write(
                    conn, "patient_image", image_id, "orient_patient_image",
                    new_json=json.dumps({"rotate": rotate, "flip_x": flip_x, "flip_y": flip_y},
                                        ensure_ascii=False),
                    created_at=now,
                )
                conn.commit()
        except BaseException:
            try:
                new_path.unlink(missing_ok=True)   # 库没切过去:只回收新文件,旧图旧哈希原样
            except OSError:
                # 八轮#854:清理自身的报错只记日志——不得顶掉原始的404/409/库异常
                logging.getLogger("local_app").warning("旋转失败后新文件清理失败: %s", new_path.name)
            raise
        # 指针已切换:旧文件成落选品——清理失败只记日志不改变已提交结果(同头像口径)
        try:
            file_path.unlink()
        except OSError:
            logging.getLogger("local_app").warning("旋转旧文件清理失败: %s", file_path.name)
        return {"ok": True, "content_hash": content_hash}

    # ---------- 影像组图标记:医生点"做组图"才成组,自动不判 ----------
    @router.post("/api/patients/{patient_identity}/image-sets")
    def mark_image_set(patient_identity: str, payload: dict = Body(...)):
        require_perm("patient.edit")   # 越权:写患者/病历数据
        image_date = str(payload.get("image_date") or "").strip()[:10]
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", image_date):
            raise HTTPException(status_code=400, detail="image_date 需为 YYYY-MM-DD")
        with connect(db_path) as conn:
            begin_immediate(conn)   # 审计R2复核:抢写锁使「查患者→写入」原子,合并事务无法插进两步之间(P1竞态窗口)
            require_patient(conn, patient_identity)
            has = conn.execute(
                "select 1 from patient_images where patient_identity = ? and substr(coalesce(image_date,''),1,10) = ? limit 1",
                (patient_identity, image_date),
            ).fetchone()
            if not has:
                raise HTTPException(status_code=400, detail="该日期没有影像")
            conn.execute(
                "insert or ignore into patient_image_sets(patient_identity, image_date, created_at) values (?, ?, ?)",
                (patient_identity, image_date, now_str()),
            )
            conn.commit()
        return {"ok": True, "image_date": image_date}

    @router.delete("/api/patients/{patient_identity}/image-sets/{image_date}")
    def unmark_image_set(patient_identity: str, image_date: str):
        require_perm("patient.edit")   # 越权:写患者/病历数据
        with connect(db_path) as conn:
            begin_immediate(conn)   # 审计R2复核:抢写锁使「查患者→写入」原子(P1竞态窗口)
            require_patient(conn, patient_identity)
            conn.execute(
                "delete from patient_image_sets where patient_identity = ? and image_date = ?",
                (patient_identity, str(image_date)[:10]),
            )
            conn.commit()
        return {"ok": True}

    # ---------- 患者头像：上传 + 读取 ----------
    @router.post("/api/patients/{patient_identity}/avatar")
    async def upload_avatar(patient_identity: str, file: UploadFile = File(...)):
        require_perm("patient.edit")   # 越权:写患者/病历数据
        with connect(db_path) as conn:
            require_patient(conn, patient_identity)
        ext = (Path(file.filename or "").suffix or ".jpg").lower()
        if ext not in (".jpg", ".jpeg", ".png", ".webp", ".gif"):
            raise HTTPException(status_code=400, detail="只支持 jpg/png/webp/gif 图片")
        data = await file.read()
        if len(data) > 8 * 1024 * 1024:
            raise HTTPException(status_code=400, detail="图片不要超过 8MB")
        # #104 用 patient_identity 的哈希做文件名前缀，避免不同ID清洗后碰撞串图。
        # 审计R2复核三轮:每次上传独立文件名(带随机段)——新文件永不覆盖旧文件,并发上传互不踩;
        # 库成功指向新文件之前,旧头像文件分毫不动(提交失败/竞态404只丢自己的新文件)。
        safe = hashlib.sha1(patient_identity.encode("utf-8")).hexdigest()[:20]
        avatar_dir = images_dir / "avatars"
        avatar_dir.mkdir(parents=True, exist_ok=True)
        rel = f"avatars/{safe}.{uuid.uuid4().hex[:8]}{ext}"
        final_path = images_dir / rel
        now = now_str()
        with connect(db_path) as conn:
            begin_immediate(conn)   # 首次校验到此处之间身份可能刚被合并,写前同事务再查一次(P1竞态窗口)
            try:
                require_patient(conn, patient_identity)
                final_path.write_bytes(data)   # 独立名新文件(≤8MB,毫秒级),不碰任何旧文件
                conn.execute(
                    "update patients set avatar_path = ?, local_updated_at = ? where patient_identity = ?",
                    (rel, now, patient_identity),
                )
                conn.commit()
            except BaseException:
                final_path.unlink(missing_ok=True)   # 校验/写库失败:只回收自己的新文件,旧头像分毫不动
                raise
        # 提交成功后另起短锁清落选文件:以「库当前指向」为准(不排软删行——它仍引用该文件,保留不算孤儿),
        # 只删不被指向的;并发迟到的清理也只会删被库淘汰的文件,绝不误删现任头像。
        # 清理是卫生工序:任何失败不得改变已提交上传的结果——记日志后放行,
        # 残留落选文件天然可重试:同前缀 glob 会被该患者下一次上传的清理顺路收走。
        try:
            with connect(db_path) as conn:
                begin_immediate(conn)
                row = conn.execute(
                    "select avatar_path from patients where patient_identity = ?", (patient_identity,)
                ).fetchone()
                keep = str(row["avatar_path"] or "") if row else ""
                for old in avatar_dir.glob(f"{safe}*"):
                    if f"avatars/{old.name}" != keep:
                        try:
                            old.unlink()
                        except OSError:
                            logging.getLogger("local_app").warning(
                                "头像落选文件清理失败,留待下次上传重试: %s", old.name)
                conn.commit()
        except Exception:
            logging.getLogger("local_app").exception(
                "头像清理阶段失败(上传本身已提交生效,不影响响应;残留由下次上传收走)")
        return {"ok": True, "avatar_url": f"/api/patients/{patient_identity}/avatar?t={now}"}

    @router.get("/api/patients/{patient_identity}/avatar")
    def get_avatar(patient_identity: str):
        require_perm("patient.view")   # 越权:读患者/病历数据
        with connect(db_path) as conn:
            row = conn.execute(
                "select avatar_path from patients where patient_identity = ?", (patient_identity,)
            ).fetchone()
        if not row or not row["avatar_path"]:
            raise HTTPException(status_code=404, detail="no avatar")
        images_root = images_dir.resolve()
        file_path = (images_dir / row["avatar_path"]).resolve()
        if not file_path.is_relative_to(images_root) or not file_path.is_file():
            raise HTTPException(status_code=404, detail="no avatar")
        return FileResponse(file_path)

    @router.post("/api/patients/{patient_identity}/images")
    async def upload_patient_image(
        patient_identity: str,
        file: UploadFile = File(...),
        slot: str = Form(...),
        tooth_codes: str = Form(""),
        image_date: str = Form(""),
    ):
        require_perm("patient.edit")   # 越权:写患者影像
        is_video = slot == VIDEO_SLOT
        if is_video:                           # 内窥镜录像:视频文件,无模板机位
            category, codes = VIDEO_SLOT, ""
        elif slot == LOCAL_UPLOAD_SLOT:        # 本地上传/拖拽:自由文件,无模板机位
            category, codes = LOCAL_UPLOAD_SLOT, ""
        else:
            category, codes = resolve_capture(slot, tooth_codes)
        ext = (Path(file.filename or "").suffix or ".jpg").lower()
        if is_video:
            if ext not in (".webm", ".mp4"):
                raise HTTPException(status_code=400, detail="录像只支持 webm/mp4")
        elif ext not in (".jpg", ".jpeg", ".png", ".webp"):
            raise HTTPException(status_code=400, detail="只支持 jpg/png/webp 图片")
        data = await file.read()
        if is_video:
            if len(data) > VIDEO_MAX_BYTES:
                raise HTTPException(status_code=400, detail="录像不要超过 200MB")
        elif len(data) > 8 * 1024 * 1024:
            raise HTTPException(status_code=400, detail="图片不要超过 8MB")
        with connect(db_path) as conn:
            require_patient(conn, patient_identity)   # 患者不存在 → 404,且不落孤儿文件
        day = (image_date or "").strip()[:10]
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):   # 防 image_date 里的 ../ 穿进写文件路径
            day = today_str()
        safe = hashlib.sha1(patient_identity.encode("utf-8")).hexdigest()[:20]
        sub = Path("patient-uploads") / safe / day
        (images_dir / sub).mkdir(parents=True, exist_ok=True)
        image_id = "local-img-" + uuid.uuid4().hex[:16]
        fname = image_id + ext
        rel = str(sub / fname)
        (images_dir / rel).write_bytes(data)
        content_hash = hashlib.sha1(data).hexdigest()
        now = now_str()
        with connect(db_path) as conn:
            begin_immediate(conn)   # #580复核:抢写锁使「复查身份→插入」原子——合并事务无法插进两步之间
            try:
                # #580:与患者合并竞态——第一次校验到此处之间身份可能刚被合并,落库前同事务再查一次
                require_patient(conn, patient_identity)
            except HTTPException:
                (images_dir / rel).unlink(missing_ok=True)   # 已落盘文件回收,不留孤儿
                raise
            conn.execute(
                "insert into patient_images(image_id, patient_identity, image_date, seq, category, "
                "tooth_codes, file_name, rel_path, size_bytes, content_hash, source_folder, created_at) "
                "values (?, ?, ?, 0, ?, ?, ?, ?, ?, ?, 'local-capture', ?)",
                (image_id, patient_identity, day, category, codes, fname, rel,
                 len(data), content_hash, now),
            )
            audit_write(
                conn, "patient_image", image_id, "upload_patient_image",
                new_json=json.dumps({"category": category, "tooth_codes": codes, "image_date": day},
                                    ensure_ascii=False),
                created_at=now,
            )
            conn.commit()
        return {"ok": True, "image_id": image_id, "category": category, "tooth_codes": codes}

    @router.delete("/api/patients/{patient_identity}/images/{image_id}")
    def delete_patient_image(patient_identity: str, image_id: str):
        require_perm("patient.edit")   # 越权:删患者影像
        with connect(db_path) as conn:
            # 六轮P2:锁内读 rel_path——与旋转的指针切换互斥。不抢锁先读,旋转在
            # 「读路径→删行」之间提交新路径时,下面会按旧路径清文件,新旋转文件成永久孤儿
            begin_immediate(conn)
            row = conn.execute(
                "select rel_path, category from patient_images "
                "where image_id = ? and patient_identity = ?",
                (image_id, patient_identity),
            ).fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="image not found")
            # #567：先删库记录并提交,成功后再删磁盘文件——避免提交失败时文件已不可恢复地丢、库留悬挂行
            conn.execute("delete from patient_images where image_id = ?", (image_id,))
            now = now_str()
            audit_write(
                conn, "patient_image", image_id, "delete_patient_image",
                old_json=json.dumps({"category": row["category"], "rel_path": row["rel_path"]},
                                    ensure_ascii=False),
                created_at=now,
            )
            conn.commit()
        # 库已提交,再删文件(限定在 images_dir 内,防穿越);删文件失败不影响已生效的库删除
        images_root = images_dir.resolve()
        fp = (images_dir / (row["rel_path"] or "")).resolve()
        if row["rel_path"] and fp.is_relative_to(images_root) and fp.is_file():
            try:
                fp.unlink()
            except OSError:
                pass
        return {"ok": True, "image_id": image_id}

    return router
