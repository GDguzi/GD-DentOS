"""P0-2 过敏史/用药史可结构化录入（既往史已由病历 PI 科目承载，不在此重复）。
红线：落独立业务列，绝不改写 patients.source_json 镜像层；编辑写版本快照+审计。"""
import json
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from local_app.api import create_app
from local_app.db import connect, init_db


def _client(tmp):
    db = Path(tmp) / "clinic.sqlite3"
    init_db(db)
    return db, TestClient(create_app(db))


class CreateWithHistoryTest(unittest.TestCase):
    def test_create_persists_allergy_and_medication(self):
        with tempfile.TemporaryDirectory() as tmp:
            db, client = _client(tmp)
            r = client.post("/api/patients", json={
                "display_name": "过敏测试", "phone": "13800000001",
                "allergy_history": "青霉素、头孢", "medication_history": "阿司匹林长期"})
            self.assertEqual(r.status_code, 200, r.text)
            pid = r.json()["patient_identity"]
            with connect(db) as conn:
                row = conn.execute(
                    "select allergy_history, medication_history, source_json from patients where patient_identity=?",
                    (pid,)).fetchone()
            self.assertEqual(row["allergy_history"], "青霉素、头孢")
            self.assertEqual(row["medication_history"], "阿司匹林长期")
            # 镜像红线：新建本地患者 source_json 不含这些本地业务字段
            self.assertNotIn("青霉素", row["source_json"])


class UpdateHistoryTest(unittest.TestCase):
    def _make(self, client):
        return client.post("/api/patients", json={
            "display_name": "张三", "phone": "13800000002"}).json()["patient_identity"]

    def test_update_sets_history_and_writes_version_audit(self):
        with tempfile.TemporaryDirectory() as tmp:
            db, client = _client(tmp)
            pid = self._make(client)
            r = client.put(f"/api/patients/{pid}", json={
                "allergy_history": "海鲜过敏", "medication_history": "无"})
            self.assertEqual(r.status_code, 200, r.text)
            with connect(db) as conn:
                row = conn.execute(
                    "select allergy_history, medication_history, source_json from patients where patient_identity=?",
                    (pid,)).fetchone()
                vcount = conn.execute(
                    "select count(*) c from patient_versions where patient_identity=?", (pid,)).fetchone()["c"]
                audit = [a["action"] for a in conn.execute(
                    "select action from audit_logs where entity_type='patient' and entity_id=?", (pid,)).fetchall()]
            self.assertEqual(row["allergy_history"], "海鲜过敏")
            self.assertEqual(row["medication_history"], "无")
            self.assertGreaterEqual(vcount, 1)                 # 写了版本快照(哈希链)
            self.assertIn("update_patient_profile", audit)     # 审计留痕
            self.assertEqual(row["source_json"], "{}")         # 镜像层未被改写

    def test_history_in_snapshot(self):
        # 过敏/用药史进入档案快照(哈希链完整覆盖医疗史变更)
        from local_app.snapshots import patient_profile_snapshot
        with tempfile.TemporaryDirectory() as tmp:
            db, client = _client(tmp)
            pid = self._make(client)
            client.put(f"/api/patients/{pid}", json={"allergy_history": "花粉"})
            with connect(db) as conn:
                row = conn.execute("select * from patients where patient_identity=?", (pid,)).fetchone()
            snap = patient_profile_snapshot(row)
            self.assertIn("allergy_history", snap)
            self.assertEqual(snap["allergy_history"], "花粉")


if __name__ == "__main__":
    unittest.main()
