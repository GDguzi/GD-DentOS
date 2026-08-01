"""预约界面设置：默认值 + 校验 + 持久化。"""
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from local_app import auth
from local_app.api import create_app
from local_app.db import connect, init_db


class AppSettingsTest(unittest.TestCase):
    def _client(self, tmp, login="boss"):
        # 全店配置写接口仅管理员：建 admin boss + 前台 qt(用于 403 用例)，默认登录 boss
        db = Path(tmp) / "clinic.sqlite3"
        init_db(db)
        with connect(db) as conn:
            auth.create_user(conn, "boss", "院长", "admin123", role="admin")
            auth.create_user(conn, "qt", "前台", "pw123456", role="reception")
            conn.commit()
        client = TestClient(create_app(db))
        if login:
            pw = {"boss": "admin123", "qt": "pw123456"}[login]
            client.post("/api/auth/login", json={"username": login, "password": pw})
        return client

    def test_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = self._client(tmp)
            s = client.get("/api/settings/appointment").json()
            self.assertEqual(s["business_start"], "08:00")
            self.assertEqual(s["business_end"], "21:30")
            self.assertEqual(s["min_slot_minutes"], 30)
            self.assertTrue(s["show_doctor_column"])

    def test_update_and_persist(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = self._client(tmp)
            r = client.put("/api/settings/appointment", json={
                "business_start": "09:00", "business_end": "20:00",
                "min_slot_minutes": 15, "show_doctor_column": False})
            self.assertEqual(r.status_code, 200)
            s = client.get("/api/settings/appointment").json()
            self.assertEqual(s["business_start"], "09:00")
            self.assertEqual(s["min_slot_minutes"], 15)
            self.assertFalse(s["show_doctor_column"])

    def test_partial_update_keeps_others(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = self._client(tmp)
            client.put("/api/settings/appointment", json={"min_slot_minutes": 10})
            s = client.get("/api/settings/appointment").json()
            self.assertEqual(s["min_slot_minutes"], 10)
            self.assertEqual(s["business_start"], "08:00")   # 未给的保持默认

    def test_validation(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = self._client(tmp)
            self.assertEqual(client.put("/api/settings/appointment", json={"business_start": "8点"}).status_code, 400)
            self.assertEqual(client.put("/api/settings/appointment", json={"business_start": "18:00", "business_end": "08:00"}).status_code, 400)
            self.assertEqual(client.put("/api/settings/appointment", json={"min_slot_minutes": 7}).status_code, 400)

    def test_clinic_default_and_update(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = self._client(tmp)
            self.assertEqual(client.get("/api/settings/clinic").json()["name"], "GD · DentOS")   # 中性默认
            r = client.put("/api/settings/clinic", json={"name": "测试口腔诊所"})
            self.assertEqual(r.status_code, 200)
            self.assertEqual(client.get("/api/settings/clinic").json()["name"], "测试口腔诊所")

    def test_clinic_empty_name_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = self._client(tmp)
            self.assertEqual(client.put("/api/settings/clinic", json={"name": "  "}).status_code, 400)
            self.assertEqual(client.put("/api/settings/clinic", json={}).status_code, 400)

    def test_rooms_default_and_update(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = self._client(tmp)
            self.assertEqual(client.get("/api/settings/rooms").json()["list"], ["诊室1", "诊室2", "诊室3", "治疗室"])
            r = client.put("/api/settings/rooms", json={"list": ["种植室", "正畸室", "  ", ""]})
            self.assertEqual(r.status_code, 200)
            self.assertEqual(client.get("/api/settings/rooms").json()["list"], ["种植室", "正畸室"])  # 空值过滤
            self.assertEqual(client.put("/api/settings/rooms", json={"list": "notalist"}).status_code, 400)


    def test_non_admin_cannot_change_settings(self):
        # 前台(reception)改诊室/诊所/营业时间 → 403
        with tempfile.TemporaryDirectory() as tmp:
            client = self._client(tmp, login="qt")
            self.assertEqual(client.put("/api/settings/rooms", json={"list": ["x"]}).status_code, 403)
            self.assertEqual(client.put("/api/settings/clinic", json={"name": "x"}).status_code, 403)
            self.assertEqual(client.put("/api/settings/appointment", json={"min_slot_minutes": 15}).status_code, 403)


if __name__ == "__main__":
    unittest.main()
