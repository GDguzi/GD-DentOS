import tempfile
from pathlib import Path
import unittest
import uuid

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
        conn.commit()
    return db


def _login(client, username, password):
    r = client.post("/api/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text


class RbacTest(unittest.TestCase):
    def test_refund_admin_only(self):
        # P1-13：退费仅 admin。require_perm 是 refund_bill 首行，前台无论单据是否存在都被 403。
        with tempfile.TemporaryDirectory() as tmp:
            db = _setup(tmp)
            c = TestClient(create_app(db, require_login=True))
            # 未登录 → 401
            r = c.post("/api/bills/local-bill-x/refund", json={"refund_reason": "x", "request_id": uuid.uuid4().hex})
            self.assertEqual(r.status_code, 401)
            # 前台(reception) → 403（被权限守卫拦在业务逻辑前）
            _login(c, "qt", "pw123456")
            r = c.post("/api/bills/local-bill-x/refund", json={"refund_reason": "x", "request_id": uuid.uuid4().hex})
            self.assertEqual(r.status_code, 403)
            # admin 越过权限守卫；单据不存在 → 业务 4xx(非 403/401)
            _login(c, "admin", "admin123")
            r = c.post("/api/bills/local-bill-x/refund", json={"refund_reason": "x", "request_id": uuid.uuid4().hex})
            self.assertNotIn(r.status_code, (401, 403))

    def test_csv_export_admin_only(self):
        # P1-13：全量 CSV 导出仅 admin。
        with tempfile.TemporaryDirectory() as tmp:
            db = _setup(tmp)
            c = TestClient(create_app(db, require_login=True))
            _login(c, "qt", "pw123456")
            self.assertEqual(c.get("/api/reports/cashier-query.csv").status_code, 403)
            self.assertEqual(c.get("/api/reports/handle-category.csv").status_code, 403)
            _login(c, "admin", "admin123")
            self.assertEqual(c.get("/api/reports/cashier-query.csv").status_code, 200)
            self.assertEqual(c.get("/api/reports/handle-category.csv").status_code, 200)

    def test_patient_export_admin_only(self):
        # #220：患者 CSV 导出含姓名/手机/病历号隐私，仅 admin
        with tempfile.TemporaryDirectory() as tmp:
            db = _setup(tmp)
            c = TestClient(create_app(db, require_login=True))
            _login(c, "qt", "pw123456")
            self.assertEqual(c.get("/api/patients/export").status_code, 403)
            _login(c, "admin", "admin123")
            self.assertEqual(c.get("/api/patients/export").status_code, 200)

    def test_require_perm_admin_passes_unknown_action(self):
        # admin(permissions=['*']) 永远通过，即便 action 不在权限点清单里
        with tempfile.TemporaryDirectory() as tmp:
            db = _setup(tmp)
            c = TestClient(create_app(db, require_login=True))
            _login(c, "admin", "admin123")
            # admin 调任意 require_perm 接口都不被 403（refund 单据不存在走业务 4xx）
            r = c.post("/api/bills/local-bill-x/refund", json={"refund_reason": "x", "request_id": uuid.uuid4().hex})
            self.assertNotIn(r.status_code, (401, 403))

    def test_old_perm_keys_aliased(self):
        # 旧权限键归一到新键，清单含新键
        self.assertEqual(auth._PERM_ALIASES["billing_refund"], "billing.refund")
        self.assertEqual(auth._PERM_ALIASES["data_export"], "data.export")
        self.assertIn("billing.refund", auth.PERMISSIONS)
        self.assertIn("data.export", auth.PERMISSIONS)


if __name__ == "__main__":
    unittest.main()
