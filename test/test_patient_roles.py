"""P1-6 患者绑定责任医生/咨询师(可编辑)。落独立业务列,不动 source_json;编辑写版本+审计。"""
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


class PatientRolesTest(unittest.TestCase):
    def test_create_and_edit_responsible_doctor_consultant(self):
        with tempfile.TemporaryDirectory() as tmp:
            db, c = _client(tmp)
            pid = c.post("/api/patients", json={
                "display_name": "角色测试", "phone": "13500000001",
                "responsible_doctor": "王医生", "consultant": "李咨询"}).json()["patient_identity"]
            with connect(db) as conn:
                row = conn.execute("select responsible_doctor, consultant, source_json from patients where patient_identity=?", (pid,)).fetchone()
            self.assertEqual(row["responsible_doctor"], "王医生")
            self.assertEqual(row["consultant"], "李咨询")
            self.assertEqual(row["source_json"], "{}")
            # 编辑
            c.put(f"/api/patients/{pid}", json={"responsible_doctor": "张医生"})
            with connect(db) as conn:
                rd = conn.execute("select responsible_doctor from patients where patient_identity=?", (pid,)).fetchone()[0]
                vcount = conn.execute("select count(*) from patient_versions where patient_identity=?", (pid,)).fetchone()[0]
            self.assertEqual(rd, "张医生")
            self.assertGreaterEqual(vcount, 1)

    def test_roles_in_snapshot(self):
        from local_app.snapshots import patient_profile_snapshot
        with tempfile.TemporaryDirectory() as tmp:
            db, c = _client(tmp)
            pid = c.post("/api/patients", json={"display_name": "甲", "phone": "13500000002", "consultant": "C"}).json()["patient_identity"]
            with connect(db) as conn:
                row = conn.execute("select * from patients where patient_identity=?", (pid,)).fetchone()
            snap = patient_profile_snapshot(row)
            self.assertIn("responsible_doctor", snap)
            self.assertIn("consultant", snap)


if __name__ == "__main__":
    unittest.main()
