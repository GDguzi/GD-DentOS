import concurrent.futures
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

from local_app.routes import patient_create as patient_create_module
from local_app.api import create_app
from local_app.db import begin_immediate, connect, init_db


def _client(tmpdir):
    db_path = Path(tmpdir) / "clinic.sqlite3"
    init_db(db_path)
    return db_path, TestClient(create_app(db_path))


class PatientCreateTest(unittest.TestCase):
    def test_request_id_retry_replays_same_patient_without_duplicate_audit(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path, client = _client(tmpdir)
            request_id = "a8098c1a-f86e-11da-bd1a-00112444be1e"
            payload = {
                "request_id": request_id,
                "display_name": "重试患者",
                "phone": "13800000000",
                "remark": "同一请求重放",
            }

            first = client.post("/api/patients", json=payload)
            second = client.post("/api/patients", json=payload)

            self.assertEqual(first.status_code, 200)
            self.assertEqual(second.status_code, 200)
            expected_pid = "local-pat-a8098c1af86e11dabd1a00112444be1e"
            self.assertEqual(first.json()["patient_identity"], expected_pid)
            self.assertEqual(second.json()["patient_identity"], expected_pid)
            self.assertIs(second.json()["replayed"], True)
            with connect(db_path) as conn:
                patient_count = conn.execute(
                    "select count(*) from patients",
                ).fetchone()[0]
                audit_count = conn.execute(
                    "select count(*) from audit_logs where action = 'create_patient'",
                ).fetchone()[0]
            self.assertEqual(patient_count, 1)
            self.assertEqual(audit_count, 1)

    def test_request_id_retry_with_different_patient_fields_returns_409(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            _, client = _client(tmpdir)
            payload = {
                "request_id": "087b87ae-2f9b-4eef-82d7-7200378c74d8",
                "display_name": "冲突患者",
                "phone": "13800000001",
            }
            self.assertEqual(client.post("/api/patients", json=payload).status_code, 200)

            conflict = client.post(
                "/api/patients",
                json={**payload, "phone": "13800000002"},
            )

            self.assertEqual(conflict.status_code, 409)

    def test_request_id_replay_uses_original_fields_after_patient_edit(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path, client = _client(tmpdir)
            payload = {
                "request_id": "fa7698db-a9ac-41ca-9c8b-336c4d62cdbf",
                "display_name": "首次建档患者",
                "phone": "13800000006",
                "remark": "首次内容",
            }
            created = client.post("/api/patients", json=payload)
            self.assertEqual(created.status_code, 200)
            patient_identity = created.json()["patient_identity"]
            edited = client.put(
                f"/api/patients/{patient_identity}",
                json={
                    "display_name": "编辑后患者",
                    "phone": "13800000007",
                    "remark": "编辑后内容",
                },
            )
            self.assertEqual(edited.status_code, 200)
            with connect(db_path) as conn:
                before_counts = (
                    conn.execute("select count(*) from patients").fetchone()[0],
                    conn.execute(
                        "select count(*) from audit_logs where entity_id = ?",
                        (patient_identity,),
                    ).fetchone()[0],
                )

            replay = client.post("/api/patients", json=payload)

            self.assertEqual(replay.status_code, 200)
            self.assertEqual(replay.json()["patient_identity"], patient_identity)
            self.assertIs(replay.json()["replayed"], True)
            with connect(db_path) as conn:
                after_counts = (
                    conn.execute("select count(*) from patients").fetchone()[0],
                    conn.execute(
                        "select count(*) from audit_logs where entity_id = ?",
                        (patient_identity,),
                    ).fetchone()[0],
                )
            self.assertEqual(after_counts, before_counts)

    def test_request_id_replay_rejects_current_fields_after_patient_edit(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            _, client = _client(tmpdir)
            payload = {
                "request_id": "9bc5a552-8659-4390-91da-405a5da946a6",
                "display_name": "首次建档患者",
                "phone": "13800000008",
                "remark": "首次内容",
            }
            created = client.post("/api/patients", json=payload)
            self.assertEqual(created.status_code, 200)
            patient_identity = created.json()["patient_identity"]
            edited_payload = {
                **payload,
                "display_name": "编辑后患者",
                "phone": "13800000009",
                "remark": "编辑后内容",
            }
            edited = client.put(
                f"/api/patients/{patient_identity}",
                json=edited_payload,
            )
            self.assertEqual(edited.status_code, 200)

            replay = client.post("/api/patients", json=edited_payload)

            self.assertEqual(replay.status_code, 409)

    def test_request_id_replay_fails_closed_without_valid_create_audit(self):
        for audit_state in ("missing", "invalid_json"):
            with self.subTest(audit_state=audit_state):
                with tempfile.TemporaryDirectory() as tmpdir:
                    db_path, client = _client(tmpdir)
                    payload = {
                        "request_id": "ff452c5b-d092-4a9f-9709-73be7f259fd2",
                        "display_name": "审计守卫患者",
                        "phone": "13800000010",
                    }
                    created = client.post("/api/patients", json=payload)
                    self.assertEqual(created.status_code, 200)
                    patient_identity = created.json()["patient_identity"]
                    with connect(db_path) as conn:
                        if audit_state == "missing":
                            conn.execute(
                                "delete from audit_logs where entity_id = ? "
                                "and action = 'create_patient'",
                                (patient_identity,),
                            )
                        else:
                            conn.execute(
                                "update audit_logs set new_json = '{' where entity_id = ? "
                                "and action = 'create_patient'",
                                (patient_identity,),
                            )

                    replay = client.post("/api/patients", json=payload)

                    self.assertEqual(replay.status_code, 409)

    def test_concurrent_same_request_id_creates_one_patient_and_one_audit(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path, _ = _client(tmpdir)
            clients = (TestClient(create_app(db_path)), TestClient(create_app(db_path)))
            payload = {
                "request_id": "637f9f92-7269-4d9f-b954-3d5e7b898a6f",
                "display_name": "并发患者",
                "phone": "13800000003",
            }
            barrier = threading.Barrier(2)

            def synchronized_begin(conn):
                barrier.wait(timeout=5)
                return begin_immediate(conn)

            def create(index):
                return clients[index].post("/api/patients", json=payload)

            with mock.patch.object(
                patient_create_module,
                "begin_immediate",
                side_effect=synchronized_begin,
                create=True,
            ):
                with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
                    responses = list(executor.map(create, range(2)))

            self.assertEqual([response.status_code for response in responses], [200, 200])
            patient_ids = {response.json()["patient_identity"] for response in responses}
            self.assertEqual(len(patient_ids), 1)
            self.assertCountEqual(
                [response.json().get("replayed") for response in responses],
                [False, True],
            )
            with connect(db_path) as conn:
                patient_count = conn.execute(
                    "select count(*) from patients",
                ).fetchone()[0]
                audit_count = conn.execute(
                    "select count(*) from audit_logs where action = 'create_patient'",
                ).fetchone()[0]
            self.assertEqual(patient_count, 1)
            self.assertEqual(audit_count, 1)

    def test_create_without_request_id_keeps_non_idempotent_behavior(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            _, client = _client(tmpdir)
            payload = {"display_name": "普通患者", "phone": "13800000004"}

            first = client.post("/api/patients", json=payload)
            second = client.post("/api/patients", json=payload)

            self.assertEqual(first.status_code, 200)
            self.assertEqual(second.status_code, 200)
            self.assertNotEqual(
                first.json()["patient_identity"],
                second.json()["patient_identity"],
            )
            self.assertNotIn("replayed", first.json())
            self.assertNotIn("replayed", second.json())

    def test_invalid_request_id_returns_400(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            _, client = _client(tmpdir)
            response = client.post(
                "/api/patients",
                json={
                    "request_id": "not-a-uuid",
                    "display_name": "非法请求患者",
                    "phone": "13800000005",
                },
            )
            self.assertEqual(response.status_code, 400)

    def test_create_then_visible_in_detail(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path, client = _client(tmpdir)
            resp = client.post(
                "/api/patients",
                json={"display_name": "新患者", "phone": "x", "sex": "男",
                      "birthday": "1990-01-01", "address": "某地"},
            )
            self.assertEqual(resp.status_code, 200)
            pid = resp.json()["patient_identity"]
            self.assertTrue(pid.startswith("local-pat-"))

            detail = client.get(f"/api/patients/{pid}")
            self.assertEqual(detail.status_code, 200)
            self.assertEqual(detail.json()["patient"]["display_name"], "新患者")

    def test_create_writes_audit(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path, client = _client(tmpdir)
            pid = client.post(
                "/api/patients", json={"display_name": "审计患者", "phone": "13800000000"}
            ).json()["patient_identity"]
            with connect(db_path) as conn:
                row = conn.execute(
                    "select action from audit_logs where entity_type='patient' and entity_id=?",
                    (pid,),
                ).fetchone()
            self.assertEqual(row["action"], "create_patient")

    def test_create_persists_extended_fields_and_remark(self):
        # 完整建档表单：身份证/微信/职业/工作单位/患者类型/三级来源/备注等全部落库
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path, client = _client(tmpdir)
            payload = {
                "display_name": "全字段患者", "phone": "13800000001", "sex": "女",
                "birthday": "1988-06-15", "address": "江苏省南京市", "id_card": "99010619880615018X",
                "wechat": "wx_test", "email": "a@b.com", "occupation": "教师", "work_unit": "某小学",
                "patient_type": "初诊", "allergy_history": "青霉素", "medication_history": "无",
                "responsible_doctor": "王医生", "consultant": "李咨询",
                "referral_source": "转介绍", "referral_source2": "老患者介绍", "referral_source3": "张三",
                "remark": "前台备注：需提前提醒",
            }
            pid = client.post("/api/patients", json=payload).json()["patient_identity"]
            with connect(db_path) as conn:
                row = conn.execute(
                    "select id_card, occupation, work_unit, patient_type, "
                    "referral_source3, remark from patients where patient_identity=?",
                    (pid,),
                ).fetchone()
            self.assertEqual(row["id_card"], "99010619880615018X")
            self.assertEqual(row["occupation"], "教师")
            self.assertEqual(row["work_unit"], "某小学")
            self.assertEqual(row["patient_type"], "初诊")
            self.assertEqual(row["referral_source3"], "张三")
            self.assertEqual(row["remark"], "前台备注：需提前提醒")

    def test_update_persists_new_referral_introducer_remark_fields(self):
        # #262：建档新增的来源4/介绍人/机主/备注，编辑白名单+UPDATE 也要覆盖
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path, client = _client(tmpdir)
            pid = client.post(
                "/api/patients", json={"display_name": "编辑测试", "phone": "13800000001"}
            ).json()["patient_identity"]
            r = client.put(f"/api/patients/{pid}", json={
                "referral_source4": "家住附近/碧桂园", "introducer_type": "患者介绍",
                "introducer_name": "张三", "phone_vestee": "爸爸", "remark": "前台备注"})
            self.assertEqual(r.status_code, 200)
            with connect(db_path) as conn:
                row = conn.execute(
                    "select referral_source4, introducer_type, introducer_name, phone_vestee, remark "
                    "from patients where patient_identity=?", (pid,)
                ).fetchone()
            self.assertEqual(row["referral_source4"], "家住附近/碧桂园")
            self.assertEqual(row["introducer_name"], "张三")
            self.assertEqual(row["phone_vestee"], "爸爸")
            self.assertEqual(row["remark"], "前台备注")

    def test_profile_snapshot_covers_new_fields(self):
        # #262：审计/版本快照要含新列，否则版本链不完整
        from local_app.snapshots import patient_profile_snapshot
        snap = patient_profile_snapshot({
            "phone_vestee": "本人", "referral_source4": "x", "introducer_type": "患者介绍",
            "introducer_name": "李四", "remark": "备注"})
        for k in ("phone_vestee", "referral_source4", "introducer_type", "introducer_name", "remark"):
            self.assertIn(k, snap)

    def test_empty_name_400(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            _, client = _client(tmpdir)
            self.assertEqual(
                client.post("/api/patients", json={"phone": "x"}).status_code, 400
            )

    def test_empty_phone_400(self):
        # 复审#3：电话最小必填，避免"有名无电话"患者
        with tempfile.TemporaryDirectory() as tmpdir:
            _, client = _client(tmpdir)
            self.assertEqual(
                client.post("/api/patients", json={"display_name": "无电话"}).status_code,
                400,
            )


    def test_conflicts_carry_distinct_machine_codes(self):
        """三种 409 必须带可区分的 code：前端只能对「同号不同内容」换号重试，
        其余 409(患者行已存在)换号会建出第二份档案。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path, client = _client(tmpdir)
            request_id = "44444444-4444-4444-4444-444444444444"
            payload = {
                "request_id": request_id,
                "display_name": "冲突码患者",
                "phone": "13800000009",
            }
            self.assertEqual(client.post("/api/patients", json=payload).status_code, 200)

            changed = client.post(
                "/api/patients", json={**payload, "display_name": "冲突码患者改"}
            )
            self.assertEqual(changed.status_code, 409)
            self.assertEqual(
                changed.json()["detail"]["code"], "request_id_payload_conflict"
            )

            with connect(db_path) as conn:
                conn.execute(
                    "update audit_logs set new_json='{broken' "
                    "where entity_type='patient' and action='create_patient'"
                )
                conn.commit()
            broken = client.post("/api/patients", json=payload)
            self.assertEqual(broken.status_code, 409)
            self.assertEqual(
                broken.json()["detail"]["code"], "request_id_snapshot_broken"
            )

if __name__ == "__main__":
    unittest.main()
