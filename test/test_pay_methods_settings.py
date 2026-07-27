"""付款方式自定义（收费弹窗⚙️设置）——默认8种、校验、持久化、billing.pay守卫、审计。"""
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from local_app import auth
from local_app.api import create_app
from local_app.db import connect, init_db

_DEFAULT8 = ["现金", "微信", "支付宝", "银行卡", "云闪付", "社保卡", "医保", "其他"]


class PayMethodsSettingsTest(unittest.TestCase):
    def _client(self, tmp, login="qt"):
        # 产品决策「能收费就能改」：前台 qt(reception,有 billing.pay)当主角，护士 hs(无 billing.pay)当 403 反例
        db = Path(tmp) / "clinic.sqlite3"
        init_db(db)
        auth.ensure_seed_roles(db)
        with connect(db) as conn:
            auth.create_user(conn, "qt", "前台", "pw123456", role="reception")
            auth.create_user(conn, "hs", "护士", "pw123456", role="nurse")
            conn.commit()
        client = TestClient(create_app(db))
        if login:
            client.post("/api/auth/login", json={"username": login, "password": "pw123456"})
        return client, db

    def test_default_is_legacy_8(self):
        with tempfile.TemporaryDirectory() as tmp:
            c, _ = self._client(tmp)
            self.assertEqual(c.get("/api/settings/pay-methods").json()["list"], _DEFAULT8)

    def test_put_persists_and_get_reads_back(self):
        with tempfile.TemporaryDirectory() as tmp:
            c, _ = self._client(tmp)
            r = c.put("/api/settings/pay-methods", json={"list": ["现金", "云闪付"]})
            self.assertEqual(r.status_code, 200, r.text)
            self.assertEqual(c.get("/api/settings/pay-methods").json()["list"], ["现金", "云闪付"])

    def test_trim_dedupe_keep_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            c, _ = self._client(tmp)
            c.put("/api/settings/pay-methods", json={"list": [" 现金 ", "云闪付", "现金", "", "  "]})
            self.assertEqual(c.get("/api/settings/pay-methods").json()["list"], ["现金", "云闪付"])

    def test_rejects_empty_and_bad_shape_and_too_long(self):
        with tempfile.TemporaryDirectory() as tmp:
            c, _ = self._client(tmp)
            self.assertEqual(c.put("/api/settings/pay-methods", json={"list": []}).status_code, 400)
            self.assertEqual(c.put("/api/settings/pay-methods", json={"list": ["", "  "]}).status_code, 400)
            self.assertEqual(c.put("/api/settings/pay-methods", json={"list": "现金"}).status_code, 400)
            self.assertEqual(c.put("/api/settings/pay-methods", json={"list": ["现"* 21]}).status_code, 400)

    def test_requires_billing_pay(self):
        with tempfile.TemporaryDirectory() as tmp:
            c, _ = self._client(tmp, login="hs")   # 护士无 billing.pay
            self.assertEqual(c.put("/api/settings/pay-methods", json={"list": ["现金"]}).status_code, 403)
            # 读不设额外守卫(登录即可,与其他 GET settings 同口径)
            self.assertEqual(c.get("/api/settings/pay-methods").status_code, 200)

    def test_put_writes_audit(self):
        with tempfile.TemporaryDirectory() as tmp:
            c, db = self._client(tmp)
            c.put("/api/settings/pay-methods", json={"list": ["现金", "云闪付"]})
            with connect(db) as conn:
                n = conn.execute(
                    "select count(*) from audit_logs where entity_type='pay_methods' "
                    "and action='update_pay_methods'").fetchone()[0]
            self.assertEqual(n, 1)


if __name__ == "__main__":
    unittest.main()
