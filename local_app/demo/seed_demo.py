"""合成演示库播种器（#369）——从空库生成 ~100 个**纯虚构**患者及其业务数据，
**结构上复刻真实 SaaS 导入的产出形状**：相同的列、相同的 source_json 键（应用真正会读的那些）、
相同的影像类型记录 + 同格式占位图。值全是程序生成的假值，绝不读真实库、不含真实 PII。

「一样」的边界：应用读得到的一切（列 / source_json 键 / 影像类型 / 状态码 / 日期格式 / FDI 牙位）
都与真库一致，每个功能读起来无差；只有 PII 值是假的、影像是同格式占位图。真实 SaaS row 里
那些没有任何功能读取的内部字段不逐字段复刻（无意义）。

用法（默认输出到独立子目录 data/demo/，与真库 data/clinic.sqlite3 隔离，#369/#386）：
    python -m local_app.demo.seed_demo [输出库路径] [影像目录]
    DENTAL_DB=local_app/data/demo/demo_clinic.sqlite3 \\
    DENTAL_IMAGES=local_app/data/demo/images \\
    DENTAL_PORT=8770 DENTAL_LOCAL_ONLY=1 python -m local_app.run_local
"""
import datetime as dt
import hashlib
import json
import random
import sqlite3
import sys
from pathlib import Path

from local_app.db import init_db
from local_app.pinyin_util import name_pinyin
from local_app.versioning import stable_hash

SURNAMES = list("赵钱孙李周吴郑王冯陈褚卫蒋沈韩杨朱秦尤许何吕施张孔曹严华金魏陶姜")
GIVEN = ["伟", "芳", "娜", "敏", "静", "丽", "强", "磊", "军", "洋", "勇", "艳",
         "杰", "娟", "涛", "明", "超", "霞", "平", "刚", "桂英", "建华", "晓东", "文博"]
DOCTORS = ["张医生", "李医生", "王医生", "陈医生"]
NURSES = ["护士A", "护士B", "护士C"]
PAY_METHODS = ["现金", "微信", "支付宝", "银行卡"]
ROOMS = ["1诊室", "2诊室", "3诊室", "种植室"]
VISIT_TYPES = ["初诊", "复诊", "新诊"]
GROUPS = ["普通", "VIP", "员工亲属", ""]
ADDRESSES = ["演示市演示区示范路1号", "样板城样板镇样例街20号", "测试县测试乡测试村", ""]
DIAGNOSES = ["慢性牙髓炎", "深龋", "根尖周炎", "智齿冠周炎", "牙周炎", "楔状缺损", "残根"]
# 处置/收费项目基础资料(handle_items):中性通用项目,按官方费用分类 fee_type 分组。
# 处置选择器(/api/handle-items)就读这张表,演示库必须种入,否则「选择处置」为空(#370)。
HANDLE_CATALOG = [
    # (code, name, unit, price, fee_type)
    ("CHK", "口腔检查", "次", 20.0, "检查费"),
    ("PA", "根尖片", "张", 30.0, "检查费"),
    ("PANO", "全景片", "张", 80.0, "检查费"),
    ("RCT1", "根管治疗-前牙", "颗", 600.0, "治疗费"),
    ("RCT3", "根管治疗-磨牙", "颗", 1000.0, "治疗费"),
    ("RESIN", "树脂充填", "颗", 280.0, "治疗费"),
    ("GIC", "玻璃离子充填", "颗", 180.0, "治疗费"),
    ("SCAL", "龈上洁治(洁牙)", "次", 150.0, "治疗费"),
    ("PERIO", "牙周刮治", "象限", 200.0, "治疗费"),
    ("EXT", "拔牙术-普通", "颗", 200.0, "手术费"),
    ("EXTI", "拔牙术-阻生齿", "颗", 600.0, "手术费"),
    ("IMPL", "种植体植入", "颗", 6800.0, "手术费"),
    ("PFM", "烤瓷冠", "颗", 1200.0, "修复费"),
    ("ZR", "全瓷冠", "颗", 2200.0, "修复费"),
    ("ANES", "局部麻醉", "支", 20.0, "材料费"),
]
# 处置流水(treatment_items)从目录里取一部分常用项,保证项目名/编码/价与目录一致。
TREAT_ITEMS = [(name, code, price) for (code, name, unit, price, ft) in HANDLE_CATALOG
               if code in ("RCT1", "RCT3", "RESIN", "GIC", "SCAL", "EXT", "PFM", "IMPL")]
TEETH = ["11", "12", "13", "14", "16", "21", "24", "26", "36", "37", "46", "47"]
# 影像类型(对标真实:patient_images.category=机位/类型;image_download_tasks.image_type)
IMAGE_KINDS = [("口内照", "Intraoral"), ("全景片", "Panoramic"),
               ("根尖片", "Periapical"), ("正畸组图", "Ortho")]


def _hash(kind, key):
    return stable_hash({"k": kind, "id": key, "demo": True})


def _ts(d, hh=9, mm=0):
    return f"{d.isoformat()} {hh:02d}:{mm:02d}:00"


def _sj(obj):
    """复刻导入器:source_json = 整条原始 SaaS row 的 json(sort_keys)。"""
    return json.dumps(obj, ensure_ascii=False, sort_keys=True)


def _placeholder_jpeg(path, label):
    """生成同格式占位 JPG(非真实影像)。返回 (bytes 数, content_hash)。"""
    from PIL import Image, ImageDraw
    path.parent.mkdir(parents=True, exist_ok=True)
    img = Image.new("RGB", (480, 360), (26, 30, 38))
    d = ImageDraw.Draw(img)
    d.rectangle([8, 8, 471, 351], outline=(90, 100, 120), width=2)
    d.text((18, 18), "演示影像 / DEMO", fill=(180, 190, 210))
    d.text((18, 165), label, fill=(225, 234, 248))
    d.text((18, 322), "合成占位图 · 非真实患者", fill=(120, 130, 150))
    img.save(path, "JPEG", quality=70)
    data = path.read_bytes()
    return len(data), hashlib.sha256(data).hexdigest()


def seed(db_path, images_dir=None, n_patients=100, seed=42, v3_ready=True):
    db_path = Path(db_path)
    if db_path.exists():
        db_path.unlink()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    init_db(db_path)
    if v3_ready:
        # 演示库也是空库首启:人事权限四表先建成 v3 混合形态(与 run_local 启动序列同一口径),
        # 否则用演示库启动时会触发「旧库未迁移」响亮报错。
        from local_app.v3_bootstrap import ensure_v3_personnel_schema
        ensure_v3_personnel_schema(db_path)
    # v3_ready=False 仅供回归测试构造「待迁移旧库」装置(演练 build_migrated_copy 升级路径),
    # 真实演示/生产流程不得使用——旧形状库在 run_local 首启会被 v3_bootstrap 响亮拒绝。
    images_dir = Path(images_dir) if images_dir else db_path.parent / "images"

    rnd = random.Random(seed)
    today = dt.date.today()
    yesterday = today - dt.timedelta(days=1)
    tomorrow = today + dt.timedelta(days=1)

    conn = sqlite3.connect(db_path)
    cur = conn.cursor()

    # ---------- 员工(staff_members):医生/护士等,active=1,否则处置医生下拉显示「已停用」(#372) ----------
    staff_seq = 0
    for name, role in ([(d, "医生") for d in DOCTORS] + [(n, "护士") for n in NURSES]
                       + [("赵咨询", "咨询师")]):
        staff_seq += 1
        cur.execute(
            "insert into staff_members(staff_id, name, role, active, roles, created_at, updated_at) "
            "values (?,?,?,1,?,?,?)",
            (f"demo_staff_{staff_seq:03d}", name, role, f",{role},", _ts(today), _ts(today)),
        )

    # ---------- 处置/收费项目基础资料 handle_items(处置选择器数据源, #370) ----------
    handle_by_code = {}
    for code, name, unit, price, fee_type in HANDLE_CATALOG:
        hid = f"demo_h_{code}"
        handle_by_code[code] = hid
        cur.execute(
            "insert into handle_items(handle_id, code, name, unit, price, handle_type, fee_type, "
            "name_py, stopped, created_at, updated_at) values (?,?,?,?,?,?,?,?,0,?,?)",
            (hid, code, name, unit, price, fee_type, fee_type, name_pinyin(name), _ts(today), _ts(today)),
        )

    # ---------- 同意书模板(扫荡#395:演示库缺模板→选择器空;种几份中性模板) ----------
    for i, (name, cat) in enumerate([
        ("拔牙知情同意书", "拔牙"), ("根管治疗知情同意书", "根管"),
        ("种植牙知情同意书", "种植"), ("正畸治疗知情同意书", "正畸"), ("修复治疗知情同意书", "修复"),
    ], 1):
        cur.execute(
            "insert into consent_templates(template_id, name, category, body, source, active, created_at, updated_at) "
            "values (?,?,?,?,'demo',1,?,?)",
            (f"demo_ct_{i:02d}", name, cat,
             f"（演示用占位）本人已充分了解{name[:-5]}的目的、风险及替代方案，自愿接受治疗。",
             _ts(today), _ts(today)),
        )

    # ---------- 患者(含真实 SaaS 键的 source_json) ----------
    patients = []
    for i in range(1, n_patients + 1):
        pid = f"demo_p_{i:04d}"
        name = rnd.choice(SURNAMES) + rnd.choice(GIVEN)
        phone = f"138{i:08d}"
        sex = "男" if i % 2 else "女"
        bday = today.replace(year=1980 + i) if i <= 3 else dt.date(1970 + (i % 40), 1 + (i % 12), 1 + (i % 27))
        chart = f"DEMO{i:05d}"
        group = rnd.choice(GROUPS)
        # source_json 复刻真实 SaaS 患者 row 形状(应用读 patientid/groupname/origin)
        src = {"patientid": chart, "groupname": group, "origin": "demo_seed",
               "Name": name, "Mobile": phone, "Sex": sex, "Birthday": bday.isoformat()}
        cur.execute(
            "insert into patients(patient_identity, source_customer_id, display_name, phone, sex, birthday, "
            "address, chart_no, name_pinyin, patient_group, patient_type, referral_source, responsible_doctor, "
            "updated_at, current_hash, source_json) values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (pid, chart, name, phone, sex, bday.isoformat(), rnd.choice(ADDRESSES), chart,
             # #654:分组真相源=patient_group 列(#646迁列);patient_type 是患者类型,不再误装分组
             name_pinyin(name), group, rnd.choice(["普通", "会员", ""]),
             rnd.choice(["家住附近", "朋友介绍", "网络", ""]),
             rnd.choice(DOCTORS), _ts(today), _hash("patient", pid), _sj(src)),
        )
        patients.append((pid, name, phone, chart))

    # ---------- 病历(结构化科目, content_json 与真实同形) ----------
    rec_seq = 0
    for pid, name, _, _ in patients:
        if rnd.random() > 0.6:
            continue
        for _ in range(rnd.randint(1, 2)):
            rec_seq += 1
            rid = f"demo_mr_{rec_seq:05d}"
            dgn = rnd.choice(DIAGNOSES)
            visit = rnd.choice([yesterday, today, today - dt.timedelta(days=rnd.randint(2, 60))])
            content = {"PC": dgn, "HPI": "患牙不适数日,要求诊治。", "Exam": "探诊敏感,叩痛(+)。",
                       "DG": dgn, "TR": "局麻下行相应处置。", "Plan": "按计划复诊。"}
            tooth = rnd.choice(TEETH)
            cur.execute(
                "insert into medical_records(record_id, patient_identity, record_type, visit_time, "
                "doctor_name, tooth_json, content_json, draft_status, origin, created_at, updated_at, current_hash) "
                "values (?,?,?,?,?,?,?,'','manual',?,?,?)",
                (rid, pid, rnd.choice(VISIT_TYPES), _ts(visit, rnd.randint(9, 17)), rnd.choice(DOCTORS),
                 json.dumps({"RT": [tooth]}, ensure_ascii=False), json.dumps(content, ensure_ascii=False),
                 _ts(visit), _ts(visit), _hash("mr", rid)),
            )

    # ---------- 预约(source_json 复刻 SaaS 排班 row) ----------
    appt_seq = 0

    def _add_appt(pid, day, status):
        nonlocal appt_seq
        appt_seq += 1
        aid = f"demo_a_{appt_seq:05d}"
        hh = rnd.randint(9, 17)
        doct = rnd.choice(DOCTORS)
        item = rnd.choice([n for n, _, _ in TREAT_ITEMS])
        src = {"ScheduleStartTime": _ts(day, hh), "ScheduleEndTime": _ts(day, hh + 1),
               "ScheduleDoct": doct, "ScheduleItems": item, "ScheduleStatus": status,
               "ScheduleStudyIdentity": aid, "UpdateDateTime": _ts(day)}
        cur.execute(
            "insert into appointments(appointment_id, patient_identity, study_identity, start_time, end_time, "
            "doctor_name, item_name, status, room, visit_type, register_type, updated_at, source_json) "
            "values (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (aid, pid, aid, _ts(day, hh), _ts(day, hh + 1), doct, item, status, rnd.choice(ROOMS),
             rnd.choice(VISIT_TYPES), "预约", _ts(day), _sj(src)),
        )

    for pid, *_ in rnd.sample(patients, min(8, len(patients))):
        _add_appt(pid, yesterday, "2")
    for pid, *_ in rnd.sample(patients, min(15, len(patients))):
        _add_appt(pid, today, rnd.choice(["0", "1", "2"]))
    for pid, *_ in rnd.sample(patients, min(12, len(patients))):
        _add_appt(pid, tomorrow, "0")

    # ---------- 处置单 + 处置项目 + 收费单 + 收款 ----------
    order_seq = bill_seq = item_seq = pay_seq = 0
    for pid, name, _, _ in rnd.sample(patients, min(45, len(patients))):
        order_seq += 1
        bill_seq += 1
        oid = f"demo_o_{order_seq:05d}"
        bid = f"demo_b_{bill_seq:05d}"
        bday2 = today if order_seq % 2 == 0 else today - dt.timedelta(days=rnd.randint(1, 90))
        picks = rnd.sample(TREAT_ITEMS, rnd.randint(1, 3))
        total = 0.0
        doctor = rnd.choice(DOCTORS)
        cur.execute(
            "insert into treatment_orders(order_id, order_no, patient_identity, order_date, doctor_name, "
            "nurse_name, diagnosis, status, bill_id, created_at, updated_at) values (?,?,?,?,?,?,?,?,?,?,?)",
            (oid, f"CZ{order_seq:06d}", pid, bday2.isoformat(), doctor, rnd.choice(NURSES),
             rnd.choice(DIAGNOSES), "paid", bid, _ts(bday2), _ts(bday2)),
        )
        for nm, code, price in picks:
            item_seq += 1
            qty = rnd.randint(1, 2)
            line = round(price * qty, 2)
            total += line
            tooth = rnd.choice(TEETH)
            cur.execute(
                "insert into treatment_items(treatment_item_id, patient_identity, bill_id, item_name, item_code, "
                "handleset_identity, doctor_name, unit_price, quantity, total_fee, fee_type, order_id, tooth_codes, "
                "updated_at, source_json) values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (f"demo_ti_{item_seq:05d}", pid, bid, nm, code, handle_by_code.get(code, ""), doctor,
                 price, qty, line, "治疗费", oid, tooth, _ts(bday2),
                 _sj({"ItemName": nm, "ItemCode": code, "ToothPos": tooth})),
            )
        total = round(total, 2)
        if bill_seq % 3 == 0:
            paid, state = round(total * 0.5, 2), "partial"
        else:
            paid, state = total, "paid"
        bill_src = {"DiscountFee": 0, "DiscountMemo": "", "BillRemark": "", "BillDoct": doctor,
                    "discount": 0, "BillNo": f"B{bill_seq:06d}"}
        cur.execute(
            "insert into bills(bill_id, patient_identity, bill_no, bill_time, total_fee, paid_fee, state, "
            "updated_at, source_json) values (?,?,?,?,?,?,?,?,?)",
            (bid, pid, f"B{bill_seq:06d}", _ts(bday2, rnd.randint(9, 17)), total, paid, state,
             _ts(bday2), _sj(bill_src)),
        )
        if paid > 0:
            pay_seq += 1
            method = rnd.choice(PAY_METHODS)
            cur.execute(
                "insert into payments(payment_id, patient_identity, bill_id, pay_time, amount, state, pay_type, "
                "payment_record_type, operator, updated_at, source_json) values (?,?,?,?,?,?,?,?,?,?,?)",
                (f"demo_pay_{pay_seq:05d}", pid, bid, _ts(bday2, rnd.randint(9, 17)), paid, "0", method,
                 "charge", rnd.choice(DOCTORS), _ts(bday2),
                 _sj({"pay_method": method, "CancelMark": "", "PayType": method})),
            )

    # ---------- 回访(今天到期) ----------
    rv_seq = 0
    for pid, *_ in rnd.sample(patients, min(10, len(patients))):
        rv_seq += 1
        cur.execute(
            "insert into return_visits(return_visit_id, patient_identity, due_time, item_name, status, note, updated_at) "
            "values (?,?,?,?,?,?,?)",
            (f"demo_rv_{rv_seq:04d}", pid, _ts(today, 11), "术后回访", "0", "待联系", _ts(today)),
        )

    # ---------- 影像(真实类型记录 + 同格式占位图文件) ----------
    img_seq = 0
    for pid, name, _, chart in rnd.sample(patients, min(35, len(patients))):
        idate = (today - dt.timedelta(days=rnd.randint(0, 120))).isoformat()
        for seq in range(rnd.randint(1, 4)):
            img_seq += 1
            category, en = rnd.choice(IMAGE_KINDS)
            file_name = f"{img_seq:06d}.jpg"
            rel_path = f"{idate}/{chart}/{file_name}"
            size, chash = _placeholder_jpeg(images_dir / rel_path, f"{category} / {en}")
            tooth = rnd.choice(TEETH) if category in ("根尖片", "口内照") else ""
            cur.execute(
                "insert into patient_images(image_id, patient_identity, image_date, seq, category, file_name, "
                "rel_path, size_bytes, content_hash, tooth_codes, source_folder, created_at) "
                "values (?,?,?,?,?,?,?,?,?,?,?,?)",
                (f"demo_img_{img_seq:06d}", pid, idate, seq, category, file_name, rel_path, size,
                 chash, tooth, chart, _ts(today)),
            )
            # 同步影像支线:对应的 download task(已完成),复刻 image_type 形状
            cur.execute(
                "insert into image_download_tasks(task_id, patient_identity, source_url, upload_date, image_type, "
                "archive_path, status, updated_at) values (?,?,?,?,?,?,?,?)",
                (f"demo_imgtask_{img_seq:06d}", pid, f"demo://img/{img_seq}", idate, category,
                 rel_path, "done", _ts(today)),
            )

    conn.commit()
    tables = ("patients", "medical_records", "appointments", "bills", "payments",
              "treatment_orders", "treatment_items", "return_visits", "patient_images",
              "image_download_tasks", "handle_items", "staff_members", "consent_templates")
    counts = {t: cur.execute(f"select count(*) from {t}").fetchone()[0] for t in tables}
    conn.close()
    counts["_images_dir"] = str(images_dir)
    return counts


def main(argv=None):
    argv = argv if argv is not None else sys.argv[1:]
    # 演示库默认放独立子目录 data/demo/——这样它的 DB、影像、以及 run_local 写的
    # .lan_password/.admin_initial_password 全跟着这个目录走,与真实库 data/clinic.sqlite3
    # 彻底隔离,不再共享 data/ 顶层目录(避免 dot-file 串写)。
    out = Path(argv[0]) if argv else Path(__file__).resolve().parents[1] / "data" / "demo" / "demo_clinic.sqlite3"
    imgs = Path(argv[1]) if len(argv) > 1 else None   # 不传则默认 out.parent/images = data/demo/images
    counts = seed(out, images_dir=imgs)
    print(f"已生成合成演示库：{out}")
    print(f"影像目录：{counts.pop('_images_dir')}")
    for k, v in counts.items():
        print(f"  {k}: {v}")
    print(f"\n起服务：DENTAL_DB={out} DENTAL_IMAGES={out.parent / 'images'} \\")
    print("        DENTAL_PORT=8770 DENTAL_LOCAL_ONLY=1 python3 -m local_app.run_local")
    return counts


if __name__ == "__main__":
    main()
