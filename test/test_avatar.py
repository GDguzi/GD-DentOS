"""患者头像：上传 + 读取 + 患者详情带 avatar_path + 校验。"""
import io
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from local_app.api import create_app
from local_app.db import connect, init_db

# 1x1 PNG
_PNG = (b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06"
        b"\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01\x00\x00\x05\x00"
        b"\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82")


class AvatarTest(unittest.TestCase):
    def _client(self, tmp):
        db = Path(tmp) / "clinic.sqlite3"
        init_db(db)
        with connect(db) as conn:
            conn.execute("insert into patients(patient_identity, display_name, updated_at, current_hash) "
                         "values ('p1','张三','x','h1')")
            conn.commit()
        return db, TestClient(create_app(db, images_dir=Path(tmp) / "img"))

    def test_upload_and_get(self):
        with tempfile.TemporaryDirectory() as tmp:
            db, client = self._client(tmp)
            # 没头像 → 404
            self.assertEqual(client.get("/api/patients/p1/avatar").status_code, 404)
            r = client.post("/api/patients/p1/avatar",
                            files={"file": ("a.png", io.BytesIO(_PNG), "image/png")})
            self.assertEqual(r.status_code, 200)
            self.assertTrue(r.json()["avatar_url"].startswith("/api/patients/p1/avatar"))
            # 现在能取到
            g = client.get("/api/patients/p1/avatar")
            self.assertEqual(g.status_code, 200)
            self.assertEqual(g.content, _PNG)
            # 患者详情带 avatar_path
            self.assertTrue(client.get("/api/patients/p1").json()["patient"]["avatar_path"])

    def test_bad_type_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            db, client = self._client(tmp)
            r = client.post("/api/patients/p1/avatar",
                            files={"file": ("a.txt", io.BytesIO(b"hi"), "text/plain")})
            self.assertEqual(r.status_code, 400)

    def test_two_patients_distinct_avatars(self):
        # #104 不同患者头像文件不碰撞、不串图
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "clinic.sqlite3"
            init_db(db)
            with connect(db) as conn:
                conn.execute("insert into patients(patient_identity, display_name, updated_at, current_hash) values ('ab','甲','x','h1')")
                conn.execute("insert into patients(patient_identity, display_name, updated_at, current_hash) values ('a b','乙','x','h2')")
                conn.commit()
            client = TestClient(create_app(db, images_dir=Path(tmp) / "img"))
            png2 = _PNG + b"\x00DIFF"   # 让两张内容不同
            client.post("/api/patients/ab/avatar", files={"file": ("a.png", io.BytesIO(_PNG), "image/png")})
            client.post("/api/patients/a b/avatar", files={"file": ("b.png", io.BytesIO(png2), "image/png")})
            self.assertEqual(client.get("/api/patients/ab/avatar").content, _PNG)
            self.assertEqual(client.get("/api/patients/a b/avatar").content, png2)  # 没被覆盖/串图

    def test_missing_patient(self):
        with tempfile.TemporaryDirectory() as tmp:
            db, client = self._client(tmp)
            r = client.post("/api/patients/nope/avatar",
                            files={"file": ("a.png", io.BytesIO(_PNG), "image/png")})
            self.assertEqual(r.status_code, 404)

    def test_cleanup_failure_does_not_fail_committed_upload(self):
        # 四轮P2:清理落选文件是卫生工序——第二段清理事务抢锁失败,不得把已提交
        # 生效的上传报成 500;新头像必须可读,残留文件由下次上传的清理顺路收走。
        from unittest import mock
        import local_app.routes.patient_assets as pa
        with tempfile.TemporaryDirectory() as tmp:
            db, client = self._client(tmp)
            real_begin = pa.begin_immediate
            calls = {"n": 0}

            def flaky_begin(conn):
                calls["n"] += 1
                if calls["n"] == 2:   # 第1次=上传写事务,第2次=清理事务
                    raise RuntimeError("database is locked (模拟清理抢锁失败)")
                return real_begin(conn)

            with mock.patch.object(pa, "begin_immediate", side_effect=flaky_begin):
                r = client.post("/api/patients/p1/avatar",
                                files={"file": ("a.png", io.BytesIO(_PNG), "image/png")})
            self.assertEqual(r.status_code, 200, "清理失败不得改变已提交上传的结果")
            self.assertEqual(client.get("/api/patients/p1/avatar").content, _PNG)

    def test_second_upload_sweeps_previous_file(self):
        # 换头像后旧文件被清,目录里只剩现任(独立文件名方案不靠覆盖,靠清理收旧)
        with tempfile.TemporaryDirectory() as tmp:
            db, client = self._client(tmp)
            png2 = _PNG + b"\x00V2"
            client.post("/api/patients/p1/avatar", files={"file": ("a.png", io.BytesIO(_PNG), "image/png")})
            client.post("/api/patients/p1/avatar", files={"file": ("b.png", io.BytesIO(png2), "image/png")})
            self.assertEqual(client.get("/api/patients/p1/avatar").content, png2)
            files = list((Path(tmp) / "img" / "avatars").iterdir())
            self.assertEqual(len(files), 1, f"落选头像应被清走: {[f.name for f in files]}")


if __name__ == "__main__":
    unittest.main()
