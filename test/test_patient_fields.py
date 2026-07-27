"""P0-3 建档表单扩字段：身份证/微信/邮箱/职业/工作单位/患者类型。
红线：落独立业务列，不改写 source_json 镜像；编辑写版本快照+审计；新字段选填不破坏现有建档校验。"""
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from local_app.api import create_app
from local_app.db import connect, init_db

_NEW = {
    "id_card": "990107199001010017", "wechat": "wx_test", "email": "a@b.com",
    "occupation": "教师", "work_unit": "示例小学", "patient_type": "普通",
}


def _client(tmp):
    db = Path(tmp) / "clinic.sqlite3"
    init_db(db)
    return db, TestClient(create_app(db))


class CreateExtraFieldsTest(unittest.TestCase):
    def test_create_persists_extra_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            db, client = _client(tmp)
            r = client.post("/api/patients", json={
                "display_name": "扩字段测试", "phone": "13800000003", **_NEW})
            self.assertEqual(r.status_code, 200, r.text)
            pid = r.json()["patient_identity"]
            with connect(db) as conn:
                row = conn.execute(
                    "select id_card, wechat, email, occupation, work_unit, patient_type, source_json "
                    "from patients where patient_identity=?", (pid,)).fetchone()
            for k, v in _NEW.items():
                self.assertEqual(row[k], v, k)
            self.assertEqual(row["source_json"], "{}")          # 镜像红线

    def test_create_still_requires_name_phone(self):
        # 新字段选填，不破坏现有姓名/电话必填
        with tempfile.TemporaryDirectory() as tmp:
            db, client = _client(tmp)
            self.assertEqual(client.post("/api/patients", json={"display_name": "无电话", **_NEW}).status_code, 400)
            self.assertEqual(client.post("/api/patients", json={"phone": "13800000004", **_NEW}).status_code, 400)


class UpdateExtraFieldsTest(unittest.TestCase):
    def test_update_sets_extra_fields_with_version(self):
        with tempfile.TemporaryDirectory() as tmp:
            db, client = _client(tmp)
            pid = client.post("/api/patients", json={
                "display_name": "李四", "phone": "13800000005"}).json()["patient_identity"]
            r = client.put(f"/api/patients/{pid}", json=_NEW)
            self.assertEqual(r.status_code, 200, r.text)
            with connect(db) as conn:
                row = conn.execute(
                    "select id_card, occupation, patient_type, source_json from patients where patient_identity=?",
                    (pid,)).fetchone()
                vcount = conn.execute(
                    "select count(*) c from patient_versions where patient_identity=?", (pid,)).fetchone()["c"]
            self.assertEqual(row["id_card"], _NEW["id_card"])
            self.assertEqual(row["occupation"], "教师")
            self.assertEqual(row["patient_type"], "普通")
            self.assertGreaterEqual(vcount, 1)
            self.assertEqual(row["source_json"], "{}")

    def test_extra_fields_in_snapshot(self):
        from local_app.snapshots import patient_profile_snapshot
        with tempfile.TemporaryDirectory() as tmp:
            db, client = _client(tmp)
            pid = client.post("/api/patients", json={
                "display_name": "王五", "phone": "13800000006", "id_card": "X1"}).json()["patient_identity"]
            with connect(db) as conn:
                row = conn.execute("select * from patients where patient_identity=?", (pid,)).fetchone()
            snap = patient_profile_snapshot(row)
            for k in ("id_card", "occupation", "patient_type"):
                self.assertIn(k, snap)


if __name__ == "__main__":
    unittest.main()
