"""审计R2回归:被合并/软删的患者(is_deleted=1)不得再写入新业务数据。
预约新建 / 处置开单 / 储值充值 对软删患者必须 404,未删患者照常放行。"""
import tempfile
import unittest
import uuid
from pathlib import Path

from fastapi.testclient import TestClient

from local_app import auth
from local_app.api import create_app
from local_app.db import connect, init_db


def _client(tmp):
    db = Path(tmp) / "clinic.sqlite3"
    init_db(db)
    with connect(db) as conn:
        auth.create_user(conn, "admin", "管理员", "admin123", role="admin")
        # p_live 正常患者;p_gone 已被合并软删(patient_merge 置 is_deleted=1)
        conn.execute(
            "insert into patients(patient_identity, display_name, updated_at, current_hash) "
            "values ('p_live', '在册患者', '2026-07-01 00:00:00', 'h1')"
        )
        conn.execute(
            "insert into patients(patient_identity, display_name, updated_at, current_hash, is_deleted) "
            "values ('p_gone', '被合并患者', '2026-07-01 00:00:00', 'h2', 1)"
        )
        conn.commit()
    client = TestClient(create_app(db))
    client.post("/api/auth/login", json={"username": "admin", "password": "admin123"})
    return client


class SoftDeletedPatientWriteGuardTest(unittest.TestCase):
    def test_appointment_create_rejects_soft_deleted(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = _client(tmp)
            body = {"start_time": "2026-07-20 09:00", "doctor_name": "王医生", "item_name": "复诊"}
            r = client.post("/api/appointments", json={**body, "patient_identity": "p_gone"})
            self.assertEqual(r.status_code, 404)
            r = client.post("/api/appointments", json={**body, "patient_identity": "p_live"})
            self.assertEqual(r.status_code, 200)

    def test_treatment_order_create_rejects_soft_deleted(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = _client(tmp)
            body = {"doctor_name": "王医生",
                    "items": [{"item_name": "根管治疗", "tooth": "16", "unit_price": 800, "quantity": 1}]}
            r = client.post("/api/patients/p_gone/treatment-orders", json=body)
            self.assertEqual(r.status_code, 404)
            r = client.post("/api/patients/p_live/treatment-orders", json=body)
            self.assertEqual(r.status_code, 200)

    def test_membership_topup_rejects_soft_deleted(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = _client(tmp)
            # 审计R4:充值已强制幂等键 request_id,补上以维持"软删=404"的原测试意图
            r = client.post("/api/patients/p_gone/member-account/topup",
                            json={"amount": 100, "request_id": uuid.uuid4().hex})
            self.assertEqual(r.status_code, 404)
            r = client.post("/api/patients/p_live/member-account/topup",
                            json={"amount": 100, "request_id": uuid.uuid4().hex})
            self.assertEqual(r.status_code, 200)

    def test_topup_race_with_merge_rejected(self):
        # 审计R2复核:软删校验必须在写锁「之后」——旧序是先查患者再抢锁,
        # 查与锁之间患者被合并(软删),钱仍会进旧档案。本测试用真实写锁复现该竞态:
        # 测试线程先抢住写锁(模拟合并事务) → 充值请求阻塞在锁上 → 锁内软删患者后放锁
        # → 充值醒来必须复查出患者已删,404 且分文不入。
        import sqlite3
        import threading
        import time
        with tempfile.TemporaryDirectory() as tmp:
            client = _client(tmp)
            db_path = Path(tmp) / "clinic.sqlite3"
            blocker = sqlite3.connect(str(db_path))
            blocker.isolation_level = None
            blocker.execute("pragma busy_timeout = 5000")
            blocker.execute("begin immediate")   # 模拟合并事务先抢到写锁
            result = {}

            def worker():
                result["resp"] = client.post(
                    "/api/patients/p_live/member-account/topup",
                    json={"amount": 100, "request_id": uuid.uuid4().hex})

            t = threading.Thread(target=worker)
            t.start()
            time.sleep(0.6)   # 让请求跑过旧序的"查患者"点、阻塞在写锁上
            blocker.execute("update patients set is_deleted = 1 where patient_identity = 'p_live'")
            blocker.execute("commit")   # 合并完成,放锁
            blocker.close()
            t.join(timeout=10)
            self.assertEqual(result["resp"].status_code, 404,
                             f"竞态窗口不得给已合并档案入账: {result['resp'].text}")
            conn = sqlite3.connect(str(db_path))
            try:
                cnt = conn.execute("select count(*) from member_transactions").fetchone()[0]
            finally:
                conn.close()
            self.assertEqual(cnt, 0, "已合并档案不得留下任何储值流水")


class AvatarMergeRaceTest(unittest.TestCase):
    def test_avatar_race_keeps_old_avatar_intact(self):
        # 审计R2复核二轮:头像上传不得在校验前删旧头像——
        # 竞态 404 时旧头像文件和库指针必须原样保留,且不留半成品。
        import hashlib
        import sqlite3
        import threading
        import time
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "clinic.sqlite3"
            init_db(db)
            images = Path(tmp) / "images"; images.mkdir()
            with connect(db) as conn:
                auth.create_user(conn, "admin", "管理员", "admin123", role="admin")
                conn.execute(
                    "insert into patients(patient_identity, display_name, updated_at, current_hash) "
                    "values ('p_live', '在册患者', '2026-07-01 00:00:00', 'h1')"
                )
                conn.commit()
            client = TestClient(create_app(db, images_dir=images))
            client.post("/api/auth/login", json={"username": "admin", "password": "admin123"})
            # 先正常传一张 jpg 头像(文件名带随机段,以库指向为准找到它)
            r = client.post("/api/patients/p_live/avatar",
                            files={"file": ("a.jpg", b"OLD-AVATAR-BYTES", "image/jpeg")})
            self.assertEqual(r.status_code, 200, r.text)
            safe = hashlib.sha1(b"p_live").hexdigest()[:20]
            conn0 = sqlite3.connect(str(db)); conn0.row_factory = sqlite3.Row
            try:
                old_rel = conn0.execute(
                    "select avatar_path from patients where patient_identity='p_live'").fetchone()["avatar_path"]
            finally:
                conn0.close()
            old_avatar = images / old_rel
            self.assertEqual(old_avatar.read_bytes(), b"OLD-AVATAR-BYTES")
            # 竞态:测试线程抢住写锁(模拟合并事务),期间传第二张 png 头像
            blocker = sqlite3.connect(str(db))
            blocker.isolation_level = None
            blocker.execute("pragma busy_timeout = 5000")
            blocker.execute("begin immediate")
            result = {}

            def worker():
                result["resp"] = client.post(
                    "/api/patients/p_live/avatar",
                    files={"file": ("b.png", b"NEW-AVATAR-BYTES", "image/png")})

            t = threading.Thread(target=worker)
            t.start()
            time.sleep(0.6)
            blocker.execute("update patients set is_deleted = 1 where patient_identity = 'p_live'")
            blocker.execute("commit")
            blocker.close()
            t.join(timeout=10)
            self.assertEqual(result["resp"].status_code, 404, result["resp"].text)
            # 旧头像原样保留;库指针不变;不留 png/半成品
            self.assertEqual(old_avatar.read_bytes(), b"OLD-AVATAR-BYTES", "竞态 404 不得动旧头像")
            conn = sqlite3.connect(str(db)); conn.row_factory = sqlite3.Row
            try:
                row = conn.execute("select avatar_path from patients where patient_identity='p_live'").fetchone()
            finally:
                conn.close()
            self.assertEqual(row["avatar_path"], old_rel, "库指针不得被竞态请求改动")
            leftovers = [p.name for p in (images / "avatars").iterdir() if p.name != old_avatar.name]
            self.assertEqual(leftovers, [], f"不得留下新头像/半成品: {leftovers}")

    def test_concurrent_avatar_uploads_never_orphan_db_pointer(self):
        # 审计R2复核三轮:两个并发上传不得互删文件——无论谁赢,
        # 库指向的文件必须存在、字节正确,且头像目录只剩现任这一个文件。
        import sqlite3
        import threading
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "clinic.sqlite3"
            init_db(db)
            images = Path(tmp) / "images"; images.mkdir()
            with connect(db) as conn:
                auth.create_user(conn, "admin", "管理员", "admin123", role="admin")
                conn.execute(
                    "insert into patients(patient_identity, display_name, updated_at, current_hash) "
                    "values ('p_live', '在册患者', '2026-07-01 00:00:00', 'h1')"
                )
                conn.commit()
            client = TestClient(create_app(db, images_dir=images))
            client.post("/api/auth/login", json={"username": "admin", "password": "admin123"})
            payloads = {"A": (b"AVATAR-A", "a.jpg", "image/jpeg"), "B": (b"AVATAR-B", "b.png", "image/png")}
            for _round in range(5):   # 多轮增大交错概率;新实现任何交错都必须守住不变量
                barrier = threading.Barrier(2)

                def upload(key):
                    data, name, mime = payloads[key]
                    barrier.wait()
                    client.post("/api/patients/p_live/avatar", files={"file": (name, data, mime)})

                ts = [threading.Thread(target=upload, args=(k,)) for k in ("A", "B")]
                [t.start() for t in ts]
                [t.join(timeout=15) for t in ts]
                conn = sqlite3.connect(str(db)); conn.row_factory = sqlite3.Row
                try:
                    rel = conn.execute(
                        "select avatar_path from patients where patient_identity='p_live'").fetchone()["avatar_path"]
                finally:
                    conn.close()
                current = images / rel
                self.assertTrue(current.is_file(), f"库指向的头像必须存在: {rel}")
                self.assertIn(current.read_bytes(), (b"AVATAR-A", b"AVATAR-B"))
                names = sorted(p.name for p in (images / "avatars").iterdir())
                self.assertEqual(names, [current.name], f"目录只准剩现任头像: {names}")


if __name__ == "__main__":
    unittest.main()
