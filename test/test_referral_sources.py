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
        auth.create_user(conn, "qt", "前台", "pw123456", role="reception")
        conn.execute(
            "insert into referral_sources(source_id, parent_id, level, name, display_order) "
            "values ('rs-root', '', 1, '网络', 0)"
        )
        conn.commit()
    return db


def _login(c, u, p):
    assert c.post("/api/auth/login", json={"username": u, "password": p}).status_code == 200


class ReferralSourcesTest(unittest.TestCase):
    def test_read_open_but_write_admin_only_with_audit(self):
        # #261：来源树是全店配置，读放开，增删改/上移下移仅 admin 且写审计
        with tempfile.TemporaryDirectory() as tmp:
            db = _setup(tmp)
            c = TestClient(create_app(db))
            # 前台可读
            _login(c, "qt", "pw123456")
            self.assertEqual(c.get("/api/referral-sources").status_code, 200)
            # 前台写一律 403
            self.assertEqual(c.post("/api/referral-sources", json={"name": "朋友"}).status_code, 403)
            self.assertEqual(c.put("/api/referral-sources/rs-root", json={"name": "x"}).status_code, 403)
            self.assertEqual(c.delete("/api/referral-sources/rs-root").status_code, 403)
            self.assertEqual(c.post("/api/referral-sources/rs-root/move", json={"dir": "up"}).status_code, 403)
            # admin 可写
            _login(c, "admin", "admin123")
            r = c.post("/api/referral-sources", json={"parent_id": "rs-root", "name": "二级A"})
            self.assertEqual(r.status_code, 200)
            sid = r.json()["source_id"]
            self.assertEqual(r.json()["level"], 2)
            self.assertEqual(c.put(f"/api/referral-sources/{sid}", json={"name": "二级A改"}).status_code, 200)
            self.assertEqual(c.delete(f"/api/referral-sources/{sid}").status_code, 200)
            # 关键变更写了审计
            with connect(db) as conn:
                n = conn.execute(
                    "select count(*) from audit_logs where entity_type='referral_source'"
                ).fetchone()[0]
            self.assertGreaterEqual(n, 3)   # add / update / delete 各一条

    def test_move_admin_only_reorders_and_audits(self):
        # #263：上移/下移也是全店配置变更，须 admin + 写审计
        with tempfile.TemporaryDirectory() as tmp:
            db = _setup(tmp)
            c = TestClient(create_app(db))
            _login(c, "admin", "admin123")
            a = c.post("/api/referral-sources", json={"parent_id": "rs-root", "name": "甲"}).json()["source_id"]
            b = c.post("/api/referral-sources", json={"parent_id": "rs-root", "name": "乙"}).json()["source_id"]
            # 前台不能移
            _login(c, "qt", "pw123456")
            self.assertEqual(c.post(f"/api/referral-sources/{b}/move", json={"dir": "up"}).status_code, 403)
            # admin 上移 乙 → 顺序变 乙,甲，且写审计
            _login(c, "admin", "admin123")
            self.assertEqual(c.post(f"/api/referral-sources/{b}/move", json={"dir": "up"}).status_code, 200)
            with connect(db) as conn:
                order = [r[0] for r in conn.execute(
                    "select source_id from referral_sources where parent_id='rs-root' order by display_order"
                ).fetchall()]
                moved = conn.execute(
                    "select 1 from audit_logs where entity_type='referral_source' and action='move_referral_source'"
                ).fetchone()
            self.assertEqual(order, [b, a])
            self.assertIsNotNone(moved)


if __name__ == "__main__":
    unittest.main()
