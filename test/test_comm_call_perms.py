"""修复回归：客户沟通(通话录音+沟通记录)的患者基座权限、存量库权限迁移、
患者审计纳入 call/communication。"""
import io
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from local_app import auth
from local_app.api import create_app
from local_app.db import connect, init_db

_WAV = b"RIFF\x24\x00\x00\x00WAVEfmt " + b"\x00" * 28


class BasePermTest(unittest.TestCase):
    """V2 沟通路由精确权限授权；旧通话路由患者基座继续拦截。"""
    def _setup(self, tmp):
        db = Path(tmp) / "clinic.sqlite3"
        init_db(db)
        auth.ensure_seed_roles(db)
        with connect(db) as conn:
            auth.create_user(conn, "admin", "管理员", "admin123", role="admin")
            auth.create_user(conn, "doc", "医生", "pw123456", role="doctor")   # 有 patient.view + call/comm
            # 自定义角色：只有 call/communication 权限，无 patient.view —— 复现越权面
            conn.execute("insert into roles(role_key, name, is_system) values ('callonly','仅通话',0)")
            for pk in ("call.view", "call.manage", "communication.view", "communication.manage"):
                conn.execute("insert into role_permissions(role_key, perm_key) values ('callonly', ?)", (pk,))
            auth.create_user(conn, "co", "仅通话", "pw123456", role="callonly")
            conn.execute("insert into patients(patient_identity, display_name, updated_at, current_hash) "
                         "values ('p1','张三','x','h1')")
            conn.commit()
        return db

    def _login(self, db, user, pw):
        client = TestClient(create_app(db, images_dir=Path(db).parent / "img", require_login=True))
        r = client.post("/api/auth/login", json={"username": user, "password": pw})
        self.assertEqual(r.status_code, 200, r.text)
        return client

    def test_call_only_role_cannot_list_patient_but_can_use_exact_write_permission(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = self._setup(tmp)
            co = self._login(db, "co", "pw123456")
            self.assertEqual(co.get("/api/patients/p1/calls").status_code, 403)
            self.assertEqual(co.get("/api/patients/p1/communications").status_code, 200)
            communication = co.post(
                "/api/patients/p1/communications",
                json={"channel": "phone", "direction": "outbound", "content": "x"},
            )
            self.assertEqual(communication.status_code, 200)
            communication_id = communication.json()["communication_id"]
            self.assertEqual(
                co.post(
                    "/api/patients/p1/calls",
                    files={"file": ("r.wav", io.BytesIO(_WAV), "audio/wav")},
                    data={
                        "direction": "outbound",
                        "linked_communication_id": communication_id,
                        "expected_revision": "1",
                    },
                ).status_code,
                200,
            )

    def test_role_with_patient_view_allowed(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = self._setup(tmp)
            doc = self._login(db, "doc", "pw123456")   # 医生有 patient.view + 通话/沟通权限
            self.assertEqual(doc.get("/api/patients/p1/calls").status_code, 200)
            self.assertEqual(doc.get("/api/patients/p1/communications").status_code, 200)


class PermMigrationTest(unittest.TestCase):
    """存量库已有角色不会被 ensure_seed_roles 补新权限；一次性迁移给目标角色补齐。"""
    def test_backfill_grants_target_roles(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "clinic.sqlite3"
            init_db(db)
            auth.ensure_seed_roles(db)
            # 模拟旧库：删掉所有 call/communication 权限
            with connect(db) as conn:
                conn.execute("delete from role_permissions where perm_key like 'call.%' "
                             "or perm_key like 'communication.%'")
                conn.commit()
                n0 = conn.execute("select count(*) c from role_permissions where role_key='reception' "
                                  "and perm_key='call.view'").fetchone()["c"]
            self.assertEqual(n0, 0)
            self.assertTrue(auth.ensure_comm_call_perms(db))   # 首跑生效
            with connect(db) as conn:
                got = {r["perm_key"] for r in conn.execute(
                    "select perm_key from role_permissions where role_key='reception'")}
            for pk in ("call.view", "call.manage", "communication.view", "communication.manage"):
                self.assertIn(pk, got)
            self.assertFalse(auth.ensure_comm_call_perms(db))  # 幂等:再跑跳过

    def test_backfill_does_not_touch_untargeted_role(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "clinic.sqlite3"
            init_db(db)
            auth.ensure_seed_roles(db)
            with connect(db) as conn:
                conn.execute("delete from role_permissions where perm_key like 'call.%' "
                             "or perm_key like 'communication.%'")
                conn.commit()
            auth.ensure_comm_call_perms(db)
            with connect(db) as conn:
                # 护士不在目标角色里,不应被补
                n = conn.execute("select count(*) c from role_permissions where role_key='nurse' "
                                 "and perm_key='communication.view'").fetchone()["c"]
            self.assertEqual(n, 0)


class PatientAuditTest(unittest.TestCase):
    """上传通话 / 新建沟通记录后，患者审计视图能查到 call / communication。"""
    def test_patient_audit_includes_call_and_communication(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "clinic.sqlite3"
            init_db(db)
            with connect(db) as conn:
                conn.execute("insert into patients(patient_identity, display_name, updated_at, current_hash) "
                             "values ('p1','张三','x','h1')")
                conn.commit()
            client = TestClient(create_app(db, images_dir=Path(tmp) / "img"))
            communication = client.post(
                "/api/patients/p1/communications",
                json={"channel": "phone", "direction": "outbound", "content": "问价"},
            ).json()
            client.post(
                "/api/patients/p1/calls",
                files={"file": ("r.wav", io.BytesIO(_WAV), "audio/wav")},
                data={
                    "direction": "outbound",
                    "linked_communication_id": communication["communication_id"],
                    "expected_revision": "1",
                },
            )
            body = client.get("/api/patients/p1/audit-logs").json()
            types = {row["entity_type"] for row in body["list"]}
            self.assertIn("call", types, "患者审计应含通话录音操作")
            self.assertIn("communication", types, "患者审计应含沟通记录操作")

    def test_patient_audit_traces_deleted_records(self):
        # 删除后业务行没了，删除动作审计(json 带 patient_identity)仍须按患者可追溯
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "clinic.sqlite3"
            init_db(db)
            with connect(db) as conn:
                conn.execute("insert into patients(patient_identity, display_name, updated_at, current_hash) "
                             "values ('p1','张三','x','h1')")
                conn.commit()
            client = TestClient(create_app(db, images_dir=Path(tmp) / "img"))
            communication = client.post(
                "/api/patients/p1/communications",
                json={"channel": "phone", "direction": "outbound", "content": "问价"},
            ).json()
            comm_id = communication["communication_id"]
            call_id = client.post(
                "/api/patients/p1/calls",
                files={"file": ("r.wav", io.BytesIO(_WAV), "audio/wav")},
                data={
                    "direction": "outbound",
                    "linked_communication_id": comm_id,
                    "expected_revision": "1",
                },
            ).json()["call_id"]
            client.request(
                "DELETE", f"/api/calls/{call_id}",
                json={"expected_revision": 1},
            )
            client.request(
                "DELETE", f"/api/communications/{comm_id}",
                json={"expected_revision": 2},
            )
            actions = [row["action"] for row in client.get("/api/patients/p1/audit-logs").json()["list"]]
            self.assertIn("delete_call", actions, "删通话后患者审计仍应追溯到删除动作")
            self.assertIn("delete_communication", actions, "删沟通后患者审计仍应追溯到删除动作")


if __name__ == "__main__":
    unittest.main()
