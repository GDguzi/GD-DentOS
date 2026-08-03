"""客户通今日总览：今日回访(待/已) + 今日沟通 + 今日录音聚合，按 call/communication 权限分别门控。"""
import io
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from local_app import auth
from local_app.api import create_app
from local_app.db import connect, init_db

_WAV = b"RIFF\x24\x00\x00\x00WAVEfmt " + b"\x00" * 28
_DAY = "2026-07-10"


class CustomerHubTest(unittest.TestCase):
    def _seed(self, tmp):
        db = Path(tmp) / "clinic.sqlite3"
        init_db(db)
        with connect(db) as conn:
            conn.execute("insert into patients(patient_identity, display_name, updated_at, current_hash) "
                         "values ('p1','张三','x','h1')")
            # 今日待回访(due 当天未回访) + 今日已回访(actual 当天已回访)
            conn.execute("insert into return_visits(return_visit_id, patient_identity, due_time, item_name, status) "
                         f"values ('rv1','p1','{_DAY} 09:00','洁牙回访','待回访')")
            conn.execute("insert into return_visits(return_visit_id, patient_identity, due_time, actual_date, item_name, status) "
                         f"values ('rv2','p1','{_DAY} 09:00','{_DAY}','复查','已回访')")
            conn.execute("insert into communications(communication_id, patient_identity, channel, contacted_at, content, created_at) "
                         f"values ('c1','p1','wechat','{_DAY} 10:00','问价','{_DAY} 10:00')")
            conn.execute("insert into calls(call_id, patient_identity, direction, started_at, created_at) "
                         f"values ('k1','p1','outbound','{_DAY} 11:00','{_DAY} 11:00')")
            conn.commit()
        return db

    def test_today_overview_all_blocks(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = self._seed(tmp)
            client = TestClient(create_app(db, images_dir=Path(tmp) / "img"))
            body = client.get(f"/api/customer-hub/today?date={_DAY}").json()
            self.assertEqual(body["return_visits"]["pending_count"], 1)
            self.assertEqual(body["return_visits"]["done_count"], 1)
            self.assertEqual(body["return_visits"]["pending"][0]["item_name"], "洁牙回访")
            self.assertEqual(len(body["communications"]), 1)
            self.assertEqual(body["communications"][0]["channel"], "wechat")
            self.assertEqual(len(body["calls"]), 1)
            self.assertEqual(body["calls"][0]["direction"], "outbound")

    def test_today_call_entity_includes_current_revision(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = self._seed(tmp)
            with connect(db) as conn:
                conn.execute("update calls set revision = 7 where call_id = 'k1'")
                conn.commit()
            client = TestClient(create_app(db, images_dir=Path(tmp) / "img"))
            body = client.get(f"/api/customer-hub/today?date={_DAY}").json()
            self.assertEqual(body["calls"][0]["revision"], 7)
            patient_calls = client.get("/api/patients/p1/calls").json()["calls"]
            self.assertEqual(patient_calls[0]["revision"], 7)

    def test_other_day_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = self._seed(tmp)
            client = TestClient(create_app(db, images_dir=Path(tmp) / "img"))
            body = client.get("/api/customer-hub/today?date=2026-07-01").json()
            self.assertEqual(body["return_visits"]["pending_count"], 0)
            self.assertEqual(body["communications"], [])
            self.assertEqual(body["calls"], [])

    def test_perm_gating_nurse_no_call_or_communication(self):
        # 护士有 patient.view 但无 call.view/communication.view → 只见回访块,不返回沟通/录音块
        with tempfile.TemporaryDirectory() as tmp:
            db = self._seed(tmp)
            auth.ensure_seed_roles(db)
            with connect(db) as conn:
                auth.create_user(conn, "hs", "护士", "pw123456", role="nurse")
                conn.commit()
            client = TestClient(create_app(db, images_dir=Path(tmp) / "img", require_login=True))
            client.post("/api/auth/login", json={"username": "hs", "password": "pw123456"})
            body = client.get(f"/api/customer-hub/today?date={_DAY}").json()
            self.assertIn("return_visits", body)
            self.assertNotIn("communications", body, "无 communication.view 不应返回沟通块")
            self.assertNotIn("calls", body, "无 call.view 不应返回录音块")

    def test_bad_date_param_400(self):
        # #729 坏日期不静默返回空,一律 400(收紧到 YYYY-MM-DD,防 substr 永不匹配伪装成"无记录")
        with tempfile.TemporaryDirectory() as tmp:
            db = self._seed(tmp)
            client = TestClient(create_app(db, images_dir=Path(tmp) / "img"))
            for bad in ("20260710", "2026-99-99", "2026-W28-5", "2026/07/10"):
                self.assertEqual(client.get(f"/api/customer-hub/today?date={bad}").status_code, 400, bad)

    def test_perm_gating_requires_patient_view(self):
        # 无 patient.view 一律 403(基座权限)
        with tempfile.TemporaryDirectory() as tmp:
            db = self._seed(tmp)
            auth.ensure_seed_roles(db)
            with connect(db) as conn:
                # 自定义角色:无任何患者权限
                conn.execute("insert into roles(role_key, name, is_system) values ('noview','无权',0)")
                auth.create_user(conn, "nv", "无权", "pw123456", role="noview")
                conn.commit()
            client = TestClient(create_app(db, images_dir=Path(tmp) / "img", require_login=True))
            client.post("/api/auth/login", json={"username": "nv", "password": "pw123456"})
            self.assertEqual(client.get(f"/api/customer-hub/today?date={_DAY}").status_code, 403)


if __name__ == "__main__":
    unittest.main()
