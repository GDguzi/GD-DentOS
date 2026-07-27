"""内窥镜口内照上传：迁移 + 端点 + 模板映射 + 守卫 + 审计。"""
import io
import re
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

from fastapi import HTTPException

from local_app import auth
from local_app.api import create_app
from local_app.db import connect, init_db
from local_app.routes.patient_assets import CAPTURE_TEMPLATE, resolve_capture


def _has_col(db, table, col):
    with connect(db) as conn:
        return col in [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]


class PatientImagesMigrationTest(unittest.TestCase):
    def test_fresh_db_has_tooth_codes(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "clinic.sqlite3"
            init_db(db)
            self.assertTrue(_has_col(db, "patient_images", "tooth_codes"))

    def test_old_db_migrates_tooth_codes(self):
        # 模拟旧库:建无 tooth_codes 的 patient_images,再 init_db 应补列(幂等)
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "clinic.sqlite3"
            conn = sqlite3.connect(db)
            # 旧库真实形态:全列但缺 tooth_codes(schema 索引引用 image_date 等,故需带全)
            conn.execute(
                "create table patient_images (image_id text primary key, patient_identity text, "
                "image_date text, seq integer, category text not null default '', "
                "file_name text not null default '', rel_path text not null default '', "
                "size_bytes integer not null default 0, content_hash text not null default '', "
                "source_folder text not null default '', last_batch_id text, "
                "created_at text not null default current_timestamp)"
            )
            conn.commit(); conn.close()
            init_db(db)
            self.assertTrue(_has_col(db, "patient_images", "tooth_codes"))
            init_db(db)  # 再跑一次,幂等不报错
            self.assertTrue(_has_col(db, "patient_images", "tooth_codes"))


def _seed(tmp):
    db = Path(tmp) / "clinic.sqlite3"
    init_db(db)
    auth.ensure_seed_roles(db)
    with connect(db) as conn:
        auth.create_user(conn, "doc", "医生", "pw123456", role="doctor")   # 有 patient.edit
        auth.create_user(conn, "ns", "护士", "pw123456", role="nurse")     # 无 patient.edit
        conn.execute("insert into patients(patient_identity, display_name, updated_at, current_hash) "
                     "values ('p1','张三','2026-06-27','h1')")
        conn.commit()
    return db


def _jpg():
    return ("shot.jpg", io.BytesIO(b"\xff\xd8\xff\xe0fakejpegbytes"), "image/jpeg")


class PatientImageUploadTest(unittest.TestCase):
    def test_upload_fixed_slot_maps_category_tooth_and_audits(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = _seed(tmp)
            c = TestClient(create_app(db, images_dir=Path(tmp) / "images", require_login=True))
            c.post("/api/auth/login", json={"username": "doc", "password": "pw123456"})
            r = c.post("/api/patients/p1/images",
                       files={"file": _jpg()}, data={"slot": "上颌右·组2"})
            self.assertEqual(r.status_code, 200, r.text)
            body = r.json()
            self.assertEqual(body["category"], "上颌合面像")
            self.assertEqual(body["tooth_codes"], "15,16,17,18")
            with connect(db) as conn:
                row = conn.execute(
                    "select category, tooth_codes, source_folder, size_bytes from patient_images "
                    "where image_id=?", (body["image_id"],)).fetchone()
                self.assertEqual(row["category"], "上颌合面像")
                self.assertEqual(row["tooth_codes"], "15,16,17,18")
                self.assertEqual(row["source_folder"], "local-capture")
                self.assertGreater(row["size_bytes"], 0)
                audit = conn.execute(
                    "select operator from audit_logs where entity_type='patient_image' "
                    "and action='upload_patient_image' and entity_id=?", (body["image_id"],)).fetchone()
                self.assertIsNotNone(audit)
                self.assertEqual(audit["operator"], "doc")

    def test_upload_chief_complaint_manual_tooth(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = _seed(tmp)
            c = TestClient(create_app(db, images_dir=Path(tmp) / "images", require_login=True))
            c.post("/api/auth/login", json={"username": "doc", "password": "pw123456"})
            r = c.post("/api/patients/p1/images",
                       files={"file": _jpg()}, data={"slot": "主诉牙特写①", "tooth_codes": "36"})
            self.assertEqual(r.status_code, 200, r.text)
            self.assertEqual(r.json()["category"], "主诉牙特写")
            self.assertEqual(r.json()["tooth_codes"], "36")

    def test_upload_rejects_bad_slot_and_ext(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = _seed(tmp)
            c = TestClient(create_app(db, images_dir=Path(tmp) / "images", require_login=True))
            c.post("/api/auth/login", json={"username": "doc", "password": "pw123456"})
            self.assertEqual(c.post("/api/patients/p1/images",
                             files={"file": _jpg()}, data={"slot": "乱填"}).status_code, 400)
            bad = ("x.txt", io.BytesIO(b"hello"), "text/plain")
            self.assertEqual(c.post("/api/patients/p1/images",
                             files={"file": bad}, data={"slot": "正面咬合"}).status_code, 400)

    def test_upload_requires_patient_edit(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = _seed(tmp)
            c = TestClient(create_app(db, images_dir=Path(tmp) / "images", require_login=True))
            c.post("/api/auth/login", json={"username": "ns", "password": "pw123456"})  # 护士无 patient.edit
            self.assertEqual(c.post("/api/patients/p1/images",
                             files={"file": _jpg()}, data={"slot": "正面咬合"}).status_code, 403)

    def test_local_upload_free_category(self):
        # 本地上传/拖拽:slot=本地上传 → category 本地上传,无牙位
        with tempfile.TemporaryDirectory() as tmp:
            db = _seed(tmp)
            c = TestClient(create_app(db, images_dir=Path(tmp) / "images", require_login=True))
            c.post("/api/auth/login", json={"username": "doc", "password": "pw123456"})
            r = c.post("/api/patients/p1/images", files={"file": _jpg()}, data={"slot": "本地上传"})
            self.assertEqual(r.status_code, 200, r.text)
            self.assertEqual(r.json()["category"], "本地上传")
            self.assertEqual(r.json()["tooth_codes"], "")

    def test_delete_removes_file_row_and_audits(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = _seed(tmp)
            c = TestClient(create_app(db, images_dir=Path(tmp) / "images", require_login=True))
            c.post("/api/auth/login", json={"username": "doc", "password": "pw123456"})
            iid = c.post("/api/patients/p1/images", files={"file": _jpg()},
                         data={"slot": "正面咬合"}).json()["image_id"]
            with connect(db) as conn:
                rel = conn.execute("select rel_path from patient_images where image_id=?", (iid,)).fetchone()["rel_path"]
            fpath = Path(db).parent / "images" / rel
            self.assertTrue(fpath.is_file())
            r = c.delete(f"/api/patients/p1/images/{iid}")
            self.assertEqual(r.status_code, 200, r.text)
            self.assertFalse(fpath.exists())            # 文件已删
            with connect(db) as conn:
                self.assertIsNone(conn.execute(
                    "select 1 from patient_images where image_id=?", (iid,)).fetchone())   # 记录已删
                audit = conn.execute(
                    "select 1 from audit_logs where entity_type='patient_image' "
                    "and action='delete_patient_image' and entity_id=?", (iid,)).fetchone()
                self.assertIsNotNone(audit)             # 审计留痕

    def test_delete_keeps_file_when_db_commit_fails(self):
        # 先删库提交、成功后再删文件——库提交失败时磁盘文件不得丢失,库也不留悬挂行
        from unittest import mock
        with tempfile.TemporaryDirectory() as tmp:
            db = _seed(tmp)
            c = TestClient(create_app(db, images_dir=Path(tmp) / "images", require_login=True))
            c.post("/api/auth/login", json={"username": "doc", "password": "pw123456"})
            iid = c.post("/api/patients/p1/images", files={"file": _jpg()},
                         data={"slot": "正面咬合"}).json()["image_id"]
            with connect(db) as conn:
                rel = conn.execute("select rel_path from patient_images where image_id=?", (iid,)).fetchone()["rel_path"]
            fpath = Path(tmp) / "images" / rel
            self.assertTrue(fpath.is_file())
            # 让审计写入阶段抛错 → 事务回滚、不 commit
            # (audit 收编进 auth.audit_write 后,audit_operator 从 auth 模块解析,故障注入点跟着指过去)
            with mock.patch("local_app.auth.audit_operator", side_effect=RuntimeError("boom")):
                try:
                    c.delete(f"/api/patients/p1/images/{iid}")
                except Exception:
                    pass
            # 文件仍在(没在提交前被删),库记录也还在(回滚)
            self.assertTrue(fpath.is_file(), "DB 提交失败时文件不得被删")
            with connect(db) as conn:
                self.assertIsNotNone(conn.execute(
                    "select 1 from patient_images where image_id=?", (iid,)).fetchone(), "回滚后记录应仍在")

    def test_delete_missing_404_and_requires_edit(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = _seed(tmp)
            c = TestClient(create_app(db, images_dir=Path(tmp) / "images", require_login=True))
            c.post("/api/auth/login", json={"username": "doc", "password": "pw123456"})
            self.assertEqual(c.delete("/api/patients/p1/images/nope").status_code, 404)
            iid = c.post("/api/patients/p1/images", files={"file": _jpg()},
                         data={"slot": "正面咬合"}).json()["image_id"]
            c.post("/api/auth/login", json={"username": "ns", "password": "pw123456"})   # 护士无 patient.edit
            self.assertEqual(c.delete(f"/api/patients/p1/images/{iid}").status_code, 403)

    def test_upload_rejects_path_traversal_image_date(self):
        # image_date 带 ../ 时不得穿出 images_dir 写文件;非法日期回退今天
        with tempfile.TemporaryDirectory() as tmp:
            db = _seed(tmp)
            images = Path(tmp) / "images"
            c = TestClient(create_app(db, images_dir=images, require_login=True))
            c.post("/api/auth/login", json={"username": "doc", "password": "pw123456"})
            r = c.post("/api/patients/p1/images", files={"file": _jpg()},
                       data={"slot": "本地上传", "image_date": "../../../../pwned"})
            self.assertEqual(r.status_code, 200, r.text)
            root = images.resolve()
            escaped = [p for p in Path(tmp).rglob("*")
                       if p.is_file() and p.suffix in (".jpg", ".jpeg", ".png", ".webp")
                       and not str(p.resolve()).startswith(str(root))]
            self.assertEqual(escaped, [], f"文件穿越出 images_dir: {escaped}")
            with connect(db) as conn:
                row = conn.execute("select image_date, rel_path from patient_images "
                                   "where image_id=?", (r.json()["image_id"],)).fetchone()
                self.assertRegex(row["image_date"], r"^\d{4}-\d{2}-\d{2}$")
                self.assertNotIn("..", row["rel_path"])

    def test_list_returns_tooth_codes(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = _seed(tmp)
            c = TestClient(create_app(db, images_dir=Path(tmp) / "images", require_login=True))
            c.post("/api/auth/login", json={"username": "doc", "password": "pw123456"})
            c.post("/api/patients/p1/images", files={"file": _jpg()}, data={"slot": "正面咬合"})
            data = c.get("/api/patients/p1/images").json()
            self.assertTrue(data["list"])
            self.assertIn("tooth_codes", data["list"][0])
            self.assertEqual(data["list"][0]["category"], "正面咬合像")


class VideoUploadTest(unittest.TestCase):
    """内窥镜录像:slot=录像 → 分类"录像",允许 webm/mp4,拒绝图片扩展名之外的其他类型。"""

    def _webm(self, name="clip.webm"):
        return (name, io.BytesIO(b"\x1aE\xdf\xa3fakewebmbytes"), "video/webm")

    def test_upload_video_slot_saves_category_and_audits(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = _seed(tmp)
            c = TestClient(create_app(db, images_dir=Path(tmp) / "images", require_login=True))
            c.post("/api/auth/login", json={"username": "doc", "password": "pw123456"})
            r = c.post("/api/patients/p1/images",
                       files={"file": self._webm()}, data={"slot": "录像"})
            self.assertEqual(r.status_code, 200, r.text)
            body = r.json()
            self.assertEqual(body["category"], "录像")
            self.assertEqual(body["tooth_codes"], "")
            with connect(db) as conn:
                row = conn.execute(
                    "select category, file_name, rel_path from patient_images where image_id=?",
                    (body["image_id"],)).fetchone()
                self.assertEqual(row["category"], "录像")
                self.assertTrue(row["file_name"].endswith(".webm"))
                audit = conn.execute(
                    "select 1 from audit_logs where entity_type='patient_image' "
                    "and action='upload_patient_image' and entity_id=?", (body["image_id"],)).fetchone()
                self.assertIsNotNone(audit)

    def test_upload_video_accepts_mp4(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = _seed(tmp)
            c = TestClient(create_app(db, images_dir=Path(tmp) / "images", require_login=True))
            c.post("/api/auth/login", json={"username": "doc", "password": "pw123456"})
            mp4 = ("clip.mp4", io.BytesIO(b"\x00\x00\x00 ftypisomfakemp4"), "video/mp4")
            r = c.post("/api/patients/p1/images", files={"file": mp4}, data={"slot": "录像"})
            self.assertEqual(r.status_code, 200, r.text)

    def test_upload_video_rejects_other_ext(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = _seed(tmp)
            c = TestClient(create_app(db, images_dir=Path(tmp) / "images", require_login=True))
            c.post("/api/auth/login", json={"username": "doc", "password": "pw123456"})
            self.assertEqual(c.post("/api/patients/p1/images",
                             files={"file": _jpg()}, data={"slot": "录像"}).status_code, 400)
            bad = ("x.avi", io.BytesIO(b"RIFFfake"), "video/x-msvideo")
            self.assertEqual(c.post("/api/patients/p1/images",
                             files={"file": bad}, data={"slot": "录像"}).status_code, 400)

    def test_image_slot_still_rejects_video_ext(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = _seed(tmp)
            c = TestClient(create_app(db, images_dir=Path(tmp) / "images", require_login=True))
            c.post("/api/auth/login", json={"username": "doc", "password": "pw123456"})
            self.assertEqual(c.post("/api/patients/p1/images",
                             files={"file": self._webm()}, data={"slot": "正面咬合"}).status_code, 400)
            self.assertEqual(c.post("/api/patients/p1/images",
                             files={"file": self._webm()}, data={"slot": "本地上传"}).status_code, 400)

    def test_upload_video_size_limit(self):
        # 录像上限走 VIDEO_MAX_BYTES(200MB),图片仍是 8MB;用猴补丁把上限缩小免得测试造大文件
        from unittest import mock
        from local_app.routes import patient_assets as pa
        with tempfile.TemporaryDirectory() as tmp:
            db = _seed(tmp)
            c = TestClient(create_app(db, images_dir=Path(tmp) / "images", require_login=True))
            c.post("/api/auth/login", json={"username": "doc", "password": "pw123456"})
            with mock.patch.object(pa, "VIDEO_MAX_BYTES", 10):
                big = ("clip.webm", io.BytesIO(b"x" * 11), "video/webm")
                self.assertEqual(c.post("/api/patients/p1/images",
                                 files={"file": big}, data={"slot": "录像"}).status_code, 400)


class CaptureTemplateTest(unittest.TestCase):
    def test_fixed_slot_maps_category_and_tooth(self):
        cat, tooth = resolve_capture("上颌右·组2", "")
        self.assertEqual(cat, "上颌合面像")
        self.assertEqual(tooth, "15,16,17,18")

    def test_fixed_slot_ignores_frontend_tooth(self):
        cat, tooth = resolve_capture("正面咬合", "99,99")
        self.assertEqual(cat, "正面咬合像")
        self.assertEqual(tooth, "13,12,11,21,22,23,43,42,41,31,32,33")

    def test_chief_complaint_uses_manual_tooth(self):
        cat, tooth = resolve_capture("主诉牙特写①", "36, 37")
        self.assertEqual(cat, "主诉牙特写")
        self.assertEqual(tooth, "36,37")

    def test_chief_complaint_rejects_bad_tooth(self):
        with self.assertRaises(HTTPException):
            resolve_capture("主诉牙特写①", "99")

    def test_unknown_slot_rejected(self):
        with self.assertRaises(HTTPException):
            resolve_capture("不存在的机位", "")

    def test_template_covers_13_slots(self):
        self.assertEqual(len(CAPTURE_TEMPLATE), 13)


if __name__ == "__main__":
    unittest.main()


class PatientImageOrientTest(unittest.TestCase):
    """灯箱旋转/镜像写回文件。像素级断言变换口径=前端CSS(先镜像后旋转,CSS顺时针)。"""

    def _seed_png(self, tmp, c):
        # 2x1 PNG:左红右蓝(方向变化一目了然)
        from PIL import Image
        buf = io.BytesIO()
        img = Image.new("RGB", (2, 1))
        img.putpixel((0, 0), (255, 0, 0))
        img.putpixel((1, 0), (0, 0, 255))
        img.save(buf, format="PNG")
        buf.seek(0)
        r = c.post("/api/patients/p1/images",
                   files={"file": ("t.png", buf, "image/png")}, data={"slot": "上颌右·组2"})
        return r.json()["image_id"]

    def _pixels(self, tmp, image_id, c):
        from PIL import Image
        raw = c.get(f"/api/patients/p1/images/{image_id}/file").content
        img = Image.open(io.BytesIO(raw)).convert("RGB")
        return img.size, [img.getpixel(xy) for xy in
                          [(x, y) for y in range(img.size[1]) for x in range(img.size[0])]]

    def test_flip_x_then_rotate_matches_css_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = _seed(tmp)
            c = TestClient(create_app(db, images_dir=Path(tmp) / "images", require_login=True))
            c.post("/api/auth/login", json={"username": "doc", "password": "pw123456"})
            iid = self._seed_png(tmp, c)
            # 水平镜像:左红右蓝 → 左蓝右红
            r = c.post(f"/api/patients/p1/images/{iid}/orient", json={"rotate": 0, "flip_x": True})
            self.assertEqual(r.status_code, 200, r.text)
            size, px = self._pixels(tmp, iid, c)
            self.assertEqual(size, (2, 1))
            self.assertEqual(px, [(0, 0, 255), (255, 0, 0)])
            # 再顺时针90°:横条变竖条,蓝上红下(CSS顺时针口径:左端转到上面)
            r = c.post(f"/api/patients/p1/images/{iid}/orient", json={"rotate": 90})
            self.assertEqual(r.status_code, 200, r.text)
            size, px = self._pixels(tmp, iid, c)
            self.assertEqual(size, (1, 2))
            self.assertEqual(px, [(0, 0, 255), (255, 0, 0)])

    def test_orient_updates_hash_and_audits(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = _seed(tmp)
            c = TestClient(create_app(db, images_dir=Path(tmp) / "images", require_login=True))
            c.post("/api/auth/login", json={"username": "doc", "password": "pw123456"})
            iid = self._seed_png(tmp, c)
            with connect(db) as conn:
                before = conn.execute("select content_hash from patient_images where image_id=?", (iid,)).fetchone()[0]
            c.post(f"/api/patients/p1/images/{iid}/orient", json={"rotate": 180})
            with connect(db) as conn:
                after = conn.execute("select content_hash from patient_images where image_id=?", (iid,)).fetchone()[0]
                audits = conn.execute("select count(*) from audit_logs where entity_id=? and action='orient_patient_image'", (iid,)).fetchone()[0]
            self.assertNotEqual(before, after, "重写文件后hash必须更新")
            self.assertEqual(audits, 1)

    def test_orient_db_failure_leaves_original_intact(self):
        # 库更新/审计失败不得留下"文件已转、哈希还是旧的"的错配——
        # 指针切换方案下:5xx 时盘上仍是旧图、库里仍是旧路径旧哈希、新文件不残留。
        from unittest import mock
        import local_app.routes.patient_assets as pa
        with tempfile.TemporaryDirectory() as tmp:
            db = _seed(tmp)
            c = TestClient(create_app(db, images_dir=Path(tmp) / "images", require_login=True))
            c.post("/api/auth/login", json={"username": "doc", "password": "pw123456"})
            iid = self._seed_png(tmp, c)
            with connect(db) as conn:
                r0 = conn.execute("select rel_path, content_hash from patient_images where image_id=?",
                                  (iid,)).fetchone()
            orig_file = Path(tmp) / "images" / r0["rel_path"]
            orig_bytes = orig_file.read_bytes()
            real_audit = pa.audit_write

            def boom_audit(conn, etype, eid, action, **kw):
                if action == "orient_patient_image":
                    raise RuntimeError("模拟审计写入失败")
                return real_audit(conn, etype, eid, action, **kw)

            with mock.patch.object(pa, "audit_write", side_effect=boom_audit):
                try:
                    resp = c.post(f"/api/patients/p1/images/{iid}/orient", json={"rotate": 90})
                    self.assertGreaterEqual(resp.status_code, 500)
                except RuntimeError:
                    pass   # 异常上抛也算失败路径
            self.assertEqual(orig_file.read_bytes(), orig_bytes, "旧图必须分毫不动")
            with connect(db) as conn:
                r1 = conn.execute("select rel_path, content_hash from patient_images where image_id=?",
                                  (iid,)).fetchone()
            self.assertEqual(dict(r1), dict(r0), "库里路径/哈希必须原样")
            leftovers = [f.name for f in orig_file.parent.iterdir() if f.name != orig_file.name]
            self.assertEqual(leftovers, [], f"不得残留新文件: {leftovers}")

    def test_orient_after_delete_404_no_orphan(self):
        # 旋转与「真实删除端点」并发——删除先赢时旋转必须404,
        # 不得虚假成功;整个影像目录必须一个文件不剩(删除清了原图,旋转清了自己的新文件)
        from unittest import mock
        import local_app.routes.patient_assets as pa
        with tempfile.TemporaryDirectory() as tmp:
            db = _seed(tmp)
            c = TestClient(create_app(db, images_dir=Path(tmp) / "images", require_login=True))
            c.post("/api/auth/login", json={"username": "doc", "password": "pw123456"})
            iid = self._seed_png(tmp, c)
            with connect(db) as conn:
                rel = conn.execute("select rel_path from patient_images where image_id=?", (iid,)).fetchone()[0]
            img_dir = (Path(tmp) / "images" / rel).parent
            real_begin = pa.begin_immediate
            state = {"fired": False}

            def delete_then_lock(conn):
                # 确定性交错:旋转刚要抢锁时,真实删除端点先赢(行+原文件都被清)
                if not state["fired"]:
                    state["fired"] = True
                    with mock.patch.object(pa, "begin_immediate", real_begin):
                        dr = c.delete(f"/api/patients/p1/images/{iid}")
                        assert dr.status_code == 200, dr.text
                return real_begin(conn)

            with mock.patch.object(pa, "begin_immediate", side_effect=delete_then_lock):
                resp = c.post(f"/api/patients/p1/images/{iid}/orient", json={"rotate": 90})
            self.assertEqual(resp.status_code, 404, resp.text)
            leftovers = [f.name for f in img_dir.iterdir()]
            self.assertEqual(leftovers, [], f"目录必须清空,不得留任何孤儿: {leftovers}")
            with connect(db) as conn:
                audits = conn.execute("select count(*) from audit_logs where entity_id=? and action='orient_patient_image'",
                                      (iid,)).fetchone()[0]
            self.assertEqual(audits, 0, "没写成的旋转不得留审计")

    def test_delete_after_concurrent_rotate_cleans_current_file(self):
        # 删除抢到锁之前旋转先提交了新路径——删除必须按「锁内读到的
        # 现路径」清文件,不得按开锁前的旧路径清,否则新旋转文件成永久孤儿
        from unittest import mock
        import local_app.routes.patient_assets as pa
        with tempfile.TemporaryDirectory() as tmp:
            db = _seed(tmp)
            c = TestClient(create_app(db, images_dir=Path(tmp) / "images", require_login=True))
            c.post("/api/auth/login", json={"username": "doc", "password": "pw123456"})
            iid = self._seed_png(tmp, c)
            with connect(db) as conn:
                rel = conn.execute("select rel_path from patient_images where image_id=?", (iid,)).fetchone()[0]
            img_dir = (Path(tmp) / "images" / rel).parent
            real_begin = pa.begin_immediate
            state = {"fired": False}

            def rotate_then_lock(conn):
                # 确定性交错:删除刚要抢锁时,一次完整旋转先赢(指针已切到新文件、旧文件已清)
                if not state["fired"]:
                    state["fired"] = True
                    with mock.patch.object(pa, "begin_immediate", real_begin):
                        rr = c.post(f"/api/patients/p1/images/{iid}/orient", json={"rotate": 90})
                        assert rr.status_code == 200, rr.text
                return real_begin(conn)

            with mock.patch.object(pa, "begin_immediate", side_effect=rotate_then_lock):
                resp = c.delete(f"/api/patients/p1/images/{iid}")
            self.assertEqual(resp.status_code, 200, resp.text)
            leftovers = [f.name for f in img_dir.iterdir()]
            self.assertEqual(leftovers, [], f"删除必须清掉旋转后的现任文件: {leftovers}")
            with connect(db) as conn:
                rows = conn.execute("select count(*) from patient_images where image_id=?", (iid,)).fetchone()[0]
            self.assertEqual(rows, 0)

    def test_orient_25_consecutive_rotations_stay_healthy(self):
        # 新文件名若沿用旧主干追加后缀,每转一次名字长一截,第23次撞文件名上限
        # 且被误报400"图片损坏"。定长随机名下连转25次必须全200,目录里始终只有现任1个文件。
        with tempfile.TemporaryDirectory() as tmp:
            db = _seed(tmp)
            c = TestClient(create_app(db, images_dir=Path(tmp) / "images", require_login=True))
            c.post("/api/auth/login", json={"username": "doc", "password": "pw123456"})
            iid = self._seed_png(tmp, c)
            with connect(db) as conn:
                rel = conn.execute("select rel_path from patient_images where image_id=?", (iid,)).fetchone()[0]
            img_dir = (Path(tmp) / "images" / rel).parent
            for i in range(25):
                r = c.post(f"/api/patients/p1/images/{iid}/orient", json={"rotate": 90})
                self.assertEqual(r.status_code, 200, f"第{i + 1}次旋转: {r.text}")
                files = [f.name for f in img_dir.iterdir()]
                self.assertEqual(len(files), 1, f"第{i + 1}次后目录只该有现任1个文件: {files}")
                self.assertLess(len(files[0]), 60, f"第{i + 1}次后文件名长度必须有界: {files[0]}")
                with connect(db) as conn:   # 逐次核对库指针/文件名列与盘上一致
                    row = conn.execute("select rel_path, file_name from patient_images where image_id=?",
                                       (iid,)).fetchone()
                self.assertEqual(Path(row["rel_path"]).name, files[0], f"第{i + 1}次后库指针与盘上一致")
                self.assertEqual(row["file_name"], files[0], f"第{i + 1}次后 file_name 列与盘上一致")

    def test_orient_concurrent_rotate_409_no_leftover(self):
        # rel_path 被并发旋转改掉时走409分支——本次新文件必须清掉,零审计,原文件不动
        from unittest import mock
        import local_app.routes.patient_assets as pa
        with tempfile.TemporaryDirectory() as tmp:
            db = _seed(tmp)
            c = TestClient(create_app(db, images_dir=Path(tmp) / "images", require_login=True))
            c.post("/api/auth/login", json={"username": "doc", "password": "pw123456"})
            iid = self._seed_png(tmp, c)
            with connect(db) as conn:
                rel = conn.execute("select rel_path from patient_images where image_id=?", (iid,)).fetchone()[0]
            orig_file = Path(tmp) / "images" / rel
            real_begin = pa.begin_immediate
            state = {"fired": False}

            def swap_then_lock(conn):
                if not state["fired"]:
                    state["fired"] = True
                    with connect(db) as c2:   # 模拟并发旋转抢先:库指针已指向别的文件
                        c2.execute("update patient_images set rel_path=? where image_id=?",
                                   (rel + ".winner", iid))
                        c2.commit()
                return real_begin(conn)

            with mock.patch.object(pa, "begin_immediate", side_effect=swap_then_lock):
                resp = c.post(f"/api/patients/p1/images/{iid}/orient", json={"rotate": 90})
            self.assertEqual(resp.status_code, 409, resp.text)
            self.assertTrue(orig_file.exists(), "409时原文件必须原样")
            stray = [f.name for f in orig_file.parent.iterdir() if f.name != orig_file.name]
            self.assertEqual(stray, [], f"409分支不得留本次新文件: {stray}")
            with connect(db) as conn:
                audits = conn.execute("select count(*) from audit_logs where entity_id=? and action='orient_patient_image'",
                                      (iid,)).fetchone()[0]
            self.assertEqual(audits, 0)

    def test_orient_409_survives_cleanup_failure(self):
        # 409/404/库异常路径上,清理新文件的 unlink 自己报错不得顶掉原始状态码
        from unittest import mock
        import local_app.routes.patient_assets as pa
        with tempfile.TemporaryDirectory() as tmp:
            db = _seed(tmp)
            c = TestClient(create_app(db, images_dir=Path(tmp) / "images", require_login=True))
            c.post("/api/auth/login", json={"username": "doc", "password": "pw123456"})
            iid = self._seed_png(tmp, c)
            with connect(db) as conn:
                rel = conn.execute("select rel_path from patient_images where image_id=?", (iid,)).fetchone()[0]
            real_begin = pa.begin_immediate
            real_unlink = pa.Path.unlink
            state = {"fired": False}

            def swap_then_lock(conn):
                if not state["fired"]:
                    state["fired"] = True
                    with connect(db) as c2:   # 模拟并发旋转抢先 → 走409分支
                        c2.execute("update patient_images set rel_path=? where image_id=?",
                                   (rel + ".winner", iid))
                        c2.commit()
                return real_begin(conn)

            def broken_unlink(self, missing_ok=False):
                # 只弄坏"清理本次新文件"这一下——按实际新命名 r{uuid32}{ext} 匹配(# 旧匹配 ".r" 与新命名恒不匹配,故障从未触发,测试假绿)
                if re.fullmatch(r"r[0-9a-f]{32}\.[A-Za-z0-9]+", self.name):
                    state["unlink_failed"] = True
                    raise OSError(1, "Operation not permitted")
                return real_unlink(self, missing_ok=missing_ok)

            with mock.patch.object(pa, "begin_immediate", side_effect=swap_then_lock), \
                 mock.patch.object(pa.Path, "unlink", broken_unlink):
                resp = c.post(f"/api/patients/p1/images/{iid}/orient", json={"rotate": 90})
            self.assertEqual(resp.status_code, 409, f"清理失败不得顶掉409: {resp.text}")
            self.assertTrue(state.get("unlink_failed"), "清理故障必须真的被触发过(防假绿)")

    def test_exif_orientation_applied_before_transform(self):
        # 手机照片带EXIF方向标签,浏览器显示时已按标签转正;旋转保存必须先把像素
        # 转成"浏览器显示的样子"再做用户变换,否则存出来的方向和灯箱里看到的差90/180°。
        from PIL import Image
        with tempfile.TemporaryDirectory() as tmp:
            db = _seed(tmp)
            c = TestClient(create_app(db, images_dir=Path(tmp) / "images", require_login=True))
            c.post("/api/auth/login", json={"username": "doc", "password": "pw123456"})
            # 2x1 左红右蓝 + Orientation=6(顺时针90°显示) → 浏览器显示为竖条:红上蓝下
            buf = io.BytesIO()
            img = Image.new("RGB", (2, 1))
            img.putpixel((0, 0), (255, 0, 0))
            img.putpixel((1, 0), (0, 0, 255))
            exif = Image.Exif()
            exif[0x0112] = 6
            img.save(buf, format="PNG", exif=exif)
            buf.seek(0)
            r = c.post("/api/patients/p1/images",
                       files={"file": ("t.png", buf, "image/png")}, data={"slot": "上颌右·组2"})
            iid = r.json()["image_id"]
            # 用户对着"红上蓝下"的显示效果再顺时针90° → 应存出横条:蓝左红右
            r = c.post(f"/api/patients/p1/images/{iid}/orient", json={"rotate": 90})
            self.assertEqual(r.status_code, 200, r.text)
            size, px = self._pixels(tmp, iid, c)
            self.assertEqual(size, (2, 1))
            self.assertEqual(px, [(0, 0, 255), (255, 0, 0)])

    def test_save_failure_keeps_original_500_no_temp(self):
        # R6:保存中途失败(磁盘满等)不得毁原图——必须临时文件+原子换入;真实IO错按500上报,
        # 不得甩锅400"文件损坏";失败后不得残留临时文件。
        from PIL import Image
        with tempfile.TemporaryDirectory() as tmp:
            db = _seed(tmp)
            images = Path(tmp) / "images"
            c = TestClient(create_app(db, images_dir=images, require_login=True))
            c.post("/api/auth/login", json={"username": "doc", "password": "pw123456"})
            iid = self._seed_png(tmp, c)
            with connect(db) as conn:
                rel = conn.execute("select rel_path from patient_images where image_id=?", (iid,)).fetchone()[0]
            fpath = images / rel
            orig = fpath.read_bytes()
            before_files = sorted(p.name for p in fpath.parent.iterdir())

            def bad_save(fp, *a, **k):
                # 模拟写一半掉盘:目标文件留半截垃圾字节后抛 OSError
                Path(fp).write_bytes(b"half-written-garbage")
                raise OSError("disk full")

            with mock.patch.object(Image.Image, "save", side_effect=bad_save):
                r = c.post(f"/api/patients/p1/images/{iid}/orient", json={"rotate": 90})
            self.assertEqual(r.status_code, 500, r.text)
            self.assertEqual(fpath.read_bytes(), orig, "保存中途失败不得损毁原图")
            self.assertEqual(sorted(p.name for p in fpath.parent.iterdir()), before_files,
                             "失败后不得残留临时文件")

    def test_orient_save_and_cleanup_both_fail_still_500(self):
        # 保存失败+清理也失败的双重故障——仍必须明确500,原图不动,
        # 清理的 OSError 不得逃逸成 400"图片损坏"或未处理异常。
        from PIL import Image
        import local_app.routes.patient_assets as pa
        with tempfile.TemporaryDirectory() as tmp:
            db = _seed(tmp)
            images = Path(tmp) / "images"
            c = TestClient(create_app(db, images_dir=images, require_login=True))
            c.post("/api/auth/login", json={"username": "doc", "password": "pw123456"})
            iid = self._seed_png(tmp, c)
            with connect(db) as conn:
                rel = conn.execute("select rel_path from patient_images where image_id=?", (iid,)).fetchone()[0]
            fpath = images / rel
            orig = fpath.read_bytes()
            real_unlink = pa.Path.unlink
            state = {"unlink_failed": False}

            def bad_save(fp, *a, **k):
                Path(fp).write_bytes(b"half-written-garbage")   # 写一半掉盘
                raise OSError("disk full")

            def broken_unlink(self, missing_ok=False):
                if re.fullmatch(r"r[0-9a-f]{32}\.[A-Za-z0-9]+", self.name):
                    state["unlink_failed"] = True
                    raise OSError(1, "Operation not permitted")
                return real_unlink(self, missing_ok=missing_ok)

            with mock.patch.object(Image.Image, "save", side_effect=bad_save), \
                 mock.patch.object(pa.Path, "unlink", broken_unlink):
                r = c.post(f"/api/patients/p1/images/{iid}/orient", json={"rotate": 90})
            self.assertEqual(r.status_code, 500, f"双重故障必须仍是明确500: {r.text}")
            self.assertTrue(state["unlink_failed"], "清理故障必须真的被触发过(防假绿)")
            self.assertEqual(fpath.read_bytes(), orig, "原图必须分毫不动")

    def test_orient_decode_failure_returns_400(self):
        # 真解码失败(内容不是图片)才允许 400"文件损坏"
        with tempfile.TemporaryDirectory() as tmp:
            db = _seed(tmp)
            c = TestClient(create_app(db, images_dir=Path(tmp) / "images", require_login=True))
            c.post("/api/auth/login", json={"username": "doc", "password": "pw123456"})
            r = c.post("/api/patients/p1/images", files={"file": _jpg()}, data={"slot": "正面咬合"})
            iid = r.json()["image_id"]   # 扩展名合法但内容是假 jpeg 字节
            r = c.post(f"/api/patients/p1/images/{iid}/orient", json={"rotate": 90})
            self.assertEqual(r.status_code, 400, r.text)

    def test_orient_rejected_cases(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = _seed(tmp)
            c = TestClient(create_app(db, images_dir=Path(tmp) / "images", require_login=True))
            c.post("/api/auth/login", json={"username": "doc", "password": "pw123456"})
            iid = self._seed_png(tmp, c)
            self.assertEqual(c.post(f"/api/patients/p1/images/{iid}/orient", json={"rotate": 45}).status_code, 400)
            self.assertEqual(c.post(f"/api/patients/p1/images/{iid}/orient", json={"rotate": 0}).status_code, 400)
            self.assertEqual(c.post("/api/patients/p1/images/nope/orient", json={"rotate": 90}).status_code, 404)
            # 无 patient.edit 的角色被拒
            c2 = TestClient(create_app(db, images_dir=Path(tmp) / "images", require_login=True))
            c2.post("/api/auth/login", json={"username": "ns", "password": "pw123456"})
            self.assertEqual(c2.post(f"/api/patients/p1/images/{iid}/orient", json={"rotate": 90}).status_code, 403)


class UploadToMergedPatientTest(unittest.TestCase):
    """已软删/已被合并的患者不得再挂新影像(否则照片挂旧身份从相册消失)。"""

    def test_upload_to_soft_deleted_patient_404_no_orphan_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = _seed(tmp)
            images = Path(tmp) / "images"
            c = TestClient(create_app(db, images_dir=images))
            with connect(db) as conn:
                conn.execute("update patients set is_deleted = 1 where patient_identity = 'p1'")
                conn.commit()
            r = c.post("/api/patients/p1/images", files={"file": _jpg()}, data={"slot": "正面咬合"})
            self.assertEqual(r.status_code, 404, r.text)
            leftovers = list(images.rglob("*.jpg")) if images.exists() else []
            self.assertEqual(leftovers, [], "404 时不得留下孤儿文件")


class SeedImageSetsRaceTest(unittest.TestCase):
    """组图种子标记写入必须 insert or ignore——新库首批并发请求都过了 done 检查后,
    第二个写入者不得撞 app_settings 主键 500。"""

    def test_seed_marker_uses_insert_or_ignore(self):
        import inspect
        from local_app.routes import patient_assets
        src = inspect.getsource(patient_assets.ensure_seed_image_sets)
        self.assertIn("insert or ignore into app_settings", src,
                      "标记写入必须 or ignore,防并发窗口撞主键")

    def test_seed_idempotent_double_call(self):
        from local_app.routes.patient_assets import ensure_seed_image_sets
        with tempfile.TemporaryDirectory() as tmp:
            db = _seed(tmp)
            ensure_seed_image_sets(db)
            ensure_seed_image_sets(db)   # 再跑一次,幂等不炸
            with connect(db) as conn:
                n = conn.execute("select count(*) from app_settings "
                                 "where key='migration.534_seed_image_sets'").fetchone()[0]
            self.assertEqual(n, 1)


class UploadVsMergeRaceTest(unittest.TestCase):
    """复核:上传与患者合并并发——begin_immediate 抢写锁后「复查身份→插入」原子,
    不变量:无论谁先拿到锁,影像绝不落在被合并的旧身份上(要么404清盘,要么落主身份/被合并迁走)。"""

    def test_upload_never_lands_on_merged_identity(self):
        import threading
        import time as _t
        from local_app import auth as _auth
        from local_app.routes.patient_merge import merge_patients
        with tempfile.TemporaryDirectory() as tmp:
            db = _seed(tmp)
            images = Path(tmp) / "images"
            with connect(db) as conn:
                conn.execute("insert into patients(patient_identity, display_name, updated_at, "
                             "current_hash) values ('keep','主档','2026-07-01','h2')")
                conn.commit()
            c = TestClient(create_app(db, images_dir=images))
            # 拖慢上传事务里的 audit_operator(写锁持有期),给合并线程制造并发窗口(仿 BillingPayRace)
            real_op = _auth.audit_operator
            in_txn = threading.Event()
            first = {"v": True}
            def slow_op(*a, **k):
                if first["v"]:
                    first["v"] = False
                    in_txn.set()
                    _t.sleep(0.5)
                return real_op(*a, **k)
            _auth.audit_operator = slow_op
            self.addCleanup(lambda: setattr(_auth, "audit_operator", real_op))

            status = {}
            def upload():
                r = c.post("/api/patients/p1/images", files={"file": _jpg()}, data={"slot": "正面咬合"})
                status["code"] = r.status_code
            def merge():
                in_txn.wait(2)
                merge_patients(db, "keep", "p1", "竞态测试", operator="tester")
            t1 = threading.Thread(target=upload); t2 = threading.Thread(target=merge)
            t1.start(); t2.start(); t1.join(); t2.join()

            with connect(db) as conn:
                on_old = conn.execute("select count(*) from patient_images "
                                      "where patient_identity='p1'").fetchone()[0]
                on_keep = conn.execute("select count(*) from patient_images "
                                       "where patient_identity='keep'").fetchone()[0]
            self.assertEqual(on_old, 0, "影像绝不能留在被合并的旧身份上")
            if status.get("code") == 200:
                self.assertEqual(on_keep, 1, "上传先提交→合并应把影像迁到主身份")
            else:
                self.assertEqual(status.get("code"), 404)
                leftovers = list(images.rglob("*.jpg")) if images.exists() else []
                self.assertEqual(leftovers, [], "404 时不得留下孤儿文件")
