import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from local_app import auth
from local_app.api import create_app
from local_app.db import connect, init_db


def _setup(tmp):
    db = Path(tmp) / "clinic.sqlite3"
    init_db(db)
    with connect(db) as conn:
        auth.create_user(conn, "admin", "管理员", "admin123", role="admin")
        conn.execute("insert into patients(patient_identity, display_name, updated_at, current_hash) "
                     "values ('P','张三','x','h')")
        conn.commit()
    return db


def _login(c):
    assert c.post("/api/auth/login", json={"username": "admin", "password": "admin123"}).status_code == 200


class PatientTagsTest(unittest.TestCase):
    def test_crud_dedupe_audit(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = _setup(tmp)
            c = TestClient(create_app(db))
            _login(c)
            # 加 VIP
            r = c.post("/api/patients/P/tags", json={"name": "VIP", "color": "red"})
            self.assertEqual(r.status_code, 200)
            tid = r.json()["tag_id"]
            # 同名去重 409
            self.assertEqual(c.post("/api/patients/P/tags", json={"name": "VIP"}).status_code, 409)
            # 空名 400
            self.assertEqual(c.post("/api/patients/P/tags", json={"name": " "}).status_code, 400)
            # 非法颜色回退 gray
            r2 = c.post("/api/patients/P/tags", json={"name": "怕疼", "color": "neon"})
            self.assertEqual(r2.json()["color"], "gray")
            # 列表 2 条
            self.assertEqual(len(c.get("/api/patients/P/tags").json()["list"]), 2)
            # 删除
            self.assertEqual(c.delete(f"/api/patient-tags/{tid}").status_code, 200)
            self.assertEqual(len(c.get("/api/patients/P/tags").json()["list"]), 1)
            # 删不存在 404
            self.assertEqual(c.delete("/api/patient-tags/nope").status_code, 404)
            # 审计有 add/remove
            with connect(db) as conn:
                n = conn.execute("select count(*) from audit_logs where entity_type='patient_tag'").fetchone()[0]
            self.assertGreaterEqual(n, 3)   # add VIP + add 怕疼 + remove VIP

    def test_add_to_missing_patient_404(self):
        with tempfile.TemporaryDirectory() as tmp:
            c = TestClient(create_app(_setup(tmp)))
            _login(c)
            self.assertEqual(c.post("/api/patients/NOPE/tags", json={"name": "VIP"}).status_code, 404)


if __name__ == "__main__":
    unittest.main()
