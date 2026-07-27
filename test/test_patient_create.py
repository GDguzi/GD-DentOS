import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from local_app.api import create_app
from local_app.db import connect, init_db


def _client(tmpdir):
    db_path = Path(tmpdir) / "clinic.sqlite3"
    init_db(db_path)
    return db_path, TestClient(create_app(db_path))


class PatientCreateTest(unittest.TestCase):
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
        # 建档新增的来源4/介绍人/机主/备注，编辑白名单+UPDATE 也要覆盖
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
        # 审计/版本快照要含新列，否则版本链不完整
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
        # 电话最小必填，避免"有名无电话"患者
        with tempfile.TemporaryDirectory() as tmpdir:
            _, client = _client(tmpdir)
            self.assertEqual(
                client.post("/api/patients", json={"display_name": "无电话"}).status_code,
                400,
            )


if __name__ == "__main__":
    unittest.main()
