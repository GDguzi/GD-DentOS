import sqlite3
import tempfile
import zipfile
from pathlib import Path
import unittest

from fastapi.testclient import TestClient

from local_app import auth
from local_app.api import create_app
from local_app.db import connect, init_db


def _client(tmp):
    db = Path(tmp) / "clinic.sqlite3"
    init_db(db)
    images = Path(tmp) / "images"
    images.mkdir(parents=True, exist_ok=True)
    (images / "demo.txt").write_text("x", encoding="utf-8")  # 占位影像文件
    with connect(db) as conn:
        auth.create_user(conn, "admin", "管理员", "admin123", role="admin")
        conn.execute(
            "insert into patients(patient_identity, display_name, updated_at, current_hash) "
            "values ('p1', '测试患者', '2026-06-07 00:00:00', 'h1')"
        )
        conn.commit()
    client = TestClient(create_app(db, images_dir=images))
    client.post("/api/auth/login", json={"username": "admin", "password": "admin123"})  # 备份需管理员
    return db, images, client


class BackupTest(unittest.TestCase):
    def test_backup_db_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            db, _images, client = _client(tmp)
            resp = client.post("/api/backup", json={"include_images": False})
            self.assertEqual(resp.status_code, 200)
            name = resp.json()["name"]
            self.assertTrue(name.endswith(".db"))
            # 备份可被 sqlite 打开且含 patients 表数据
            backup_path = db.parent / "backups" / name
            self.assertTrue(backup_path.is_file())
            c = sqlite3.connect(backup_path)
            self.assertEqual(c.execute("select count(*) from patients").fetchone()[0], 1)
            c.close()

    def test_backup_with_images_zip(self):
        with tempfile.TemporaryDirectory() as tmp:
            db, _images, client = _client(tmp)
            resp = client.post("/api/backup", json={"include_images": True})
            self.assertEqual(resp.status_code, 200)
            name = resp.json()["name"]
            self.assertTrue(name.endswith(".zip"))
            zip_path = db.parent / "backups" / name
            with zipfile.ZipFile(zip_path) as zf:
                names = zf.namelist()
            self.assertTrue(any(n.endswith(".db") for n in names))
            self.assertTrue(any(n.startswith("images/") for n in names))

    def test_backup_skips_file_deleted_during_zip(self):
        # #568:打包时文件被并发删(os.walk 枚举后消失)→ 跳过该文件,备份仍成功,不 500/不残缺 zip
        from unittest import mock
        with tempfile.TemporaryDirectory() as tmp:
            db, images, client = _client(tmp)
            # 模拟 os.walk 报告一个已不存在的文件(ghost.jpg),真实文件 demo.txt 仍在
            fake_walk = [(str(images), [], ["demo.txt", "ghost.jpg"])]
            with mock.patch("local_app.routes.backup.os.walk", return_value=fake_walk):
                resp = client.post("/api/backup", json={"include_images": True})
            self.assertEqual(resp.status_code, 200, resp.text)   # 不因消失文件而 500
            zip_path = db.parent / "backups" / resp.json()["name"]
            with zipfile.ZipFile(zip_path) as zf:
                names = zf.namelist()
            self.assertIn("images/demo.txt", names)              # 存在的文件照常打包
            self.assertNotIn("images/ghost.jpg", names)          # 消失的文件被跳过

    def test_backup_does_not_swallow_real_io_error(self):
        # #568:磁盘满/权限/IO 等真故障(非 FileNotFoundError)不得被吞——备份必须失败,不伪装成功
        import errno
        from unittest import mock
        with tempfile.TemporaryDirectory() as tmp:
            db, images, client = _client(tmp)
            real_write = zipfile.ZipFile.write

            def boom(self, filename, arcname=None, *a, **k):
                if arcname and str(arcname).startswith("images"):
                    raise OSError(errno.ENOSPC, "No space left on device")
                return real_write(self, filename, arcname, *a, **k)

            ok200 = False
            with mock.patch.object(zipfile.ZipFile, "write", boom):
                try:
                    resp = client.post("/api/backup", json={"include_images": True})
                    ok200 = (resp.status_code == 200)
                except OSError:
                    ok200 = False   # 异常上抛=没被吞,也算通过
            self.assertFalse(ok200, "磁盘满等真故障不得被静默跳过、不得返回 200 成功")

    def test_failed_backup_leaves_no_halfbaked_files(self):
        # Y24(代码审计_2026-07-17):打包中途磁盘满,半截 zip/中间 db 不得以正式备份名留在盘上——
        # 正式名会进 /api/backup/list 被当成正常备份,真恢复时才发现是空炮。
        import errno
        from unittest import mock
        with tempfile.TemporaryDirectory() as tmp:
            db, _images, client = _client(tmp)
            real_write = zipfile.ZipFile.write

            def boom(self, filename, arcname=None, *a, **k):
                if arcname and str(arcname).startswith("images"):
                    raise OSError(errno.ENOSPC, "No space left on device")
                return real_write(self, filename, arcname, *a, **k)

            with mock.patch.object(zipfile.ZipFile, "write", boom):
                try:
                    resp = client.post("/api/backup", json={"include_images": True})
                    self.assertNotEqual(resp.status_code, 200)
                except OSError:
                    pass   # 异常上抛也算失败路径
            bdir = db.parent / "backups"
            leftovers = [f.name for f in bdir.iterdir()] if bdir.is_dir() else []
            self.assertEqual(leftovers, [], f"失败的备份不得留任何文件: {leftovers}")

    def test_same_second_backup_gets_suffix_not_409(self):
        # 二轮:每日自动备份同秒先落了 backup_X.db 时,手工备份不得被 409 误拒——
        # 撞名由认领逻辑原子挂 _n 后缀,两份各自完整。
        from unittest import mock
        with tempfile.TemporaryDirectory() as tmp:
            db, _images, client = _client(tmp)
            bdir = db.parent / "backups"; bdir.mkdir(parents=True, exist_ok=True)
            import local_app.routes.backup as backup_mod
            fixed = backup_mod.bj_now().replace(microsecond=0)
            stamp = fixed.strftime("%Y%m%d_%H%M%S")
            (bdir / f"backup_{stamp}.db").write_bytes(b"daily-finished-first")   # 模拟每日备份同秒先完成
            with mock.patch("local_app.routes.backup.bj_now", return_value=fixed):
                resp = client.post("/api/backup", json={"include_images": False})
            self.assertEqual(resp.status_code, 200, resp.text)
            self.assertEqual(resp.json()["name"], f"backup_{stamp}_1.db")
            self.assertEqual((bdir / f"backup_{stamp}.db").read_bytes(), b"daily-finished-first",
                             "先完成的备份不得被覆盖")

    def test_list_and_download(self):
        with tempfile.TemporaryDirectory() as tmp:
            _db, _images, client = _client(tmp)
            created = client.post("/api/backup", json={}).json()["name"]
            lst = client.get("/api/backup/list").json()
            self.assertGreaterEqual(lst["totalcount"], 1)
            self.assertIn(created, [x["name"] for x in lst["list"]])
            dl = client.get(f"/api/backup/{created}/download")
            self.assertEqual(dl.status_code, 200)

    def test_download_rejects_traversal(self):
        with tempfile.TemporaryDirectory() as tmp:
            _db, _images, client = _client(tmp)
            self.assertEqual(client.get("/api/backup/..%2F..%2Fsecret.db/download").status_code, 404)
            self.assertEqual(client.get("/api/backup/whatever.db/download").status_code, 404)

    def test_non_admin_forbidden(self):
        # #102 备份含全量隐私,非管理员不能创建/列出/下载
        with tempfile.TemporaryDirectory() as tmp:
            db, images, _ = _client(tmp)
            with connect(db) as conn:
                auth.create_user(conn, "qt", "前台", "pw123456", role="reception")
                conn.commit()
            c = TestClient(create_app(db, images_dir=images))
            self.assertEqual(c.post("/api/backup", json={}).status_code, 403)   # 未登录
            c.post("/api/auth/login", json={"username": "qt", "password": "pw123456"})
            self.assertEqual(c.post("/api/backup", json={}).status_code, 403)   # 前台非管理员
            self.assertEqual(c.get("/api/backup/list").status_code, 403)


if __name__ == "__main__":
    unittest.main()
