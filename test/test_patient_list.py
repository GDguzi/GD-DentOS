"""患者列表端点增强：group 分类筛选 / scope=recent|today / 卡片 badge 字段。"""
import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from local_app import auth
from local_app.api import create_app
from local_app.db import connect, init_db


def _admin_client(db):
    """带 admin 登录的 client（患者导出 #220 需 data_export 权限）。"""
    with connect(db) as conn:
        if not conn.execute("select 1 from users where username='admin'").fetchone():
            auth.create_user(conn, "admin", "管理员", "admin123", role="admin")
            conn.commit()
    c = TestClient(create_app(db))
    c.post("/api/auth/login", json={"username": "admin", "password": "admin123"})
    return c

TODAY = dt.date.today().isoformat()
PAST = (dt.date.today() - dt.timedelta(days=10)).isoformat()
FUTURE = (dt.date.today() + dt.timedelta(days=10)).isoformat()


class PatientListTest(unittest.TestCase):
    def _client(self, tmp):
        db = Path(tmp) / "clinic.sqlite3"
        init_db(db)
        with connect(db) as conn:
            # 开源壳阶段2:分组真相源=patient_group 列(同步建档#454/回填已落列)
            conn.execute(
                "insert into patients(patient_identity, display_name, updated_at, current_hash, patient_group) "
                "values ('p1','张三','x','h1','种植牙,最近患者')")
            conn.execute(
                "insert into patients(patient_identity, display_name, updated_at, current_hash, patient_group) "
                "values ('p2','李四','x','h2','正畸患者')")
            # p1：今天有预约(复诊)+未来预约+病历+回访；p2：仅 10 天前来过
            conn.execute("insert into appointments(appointment_id, patient_identity, start_time, doctor_name, item_name, status, visit_type, updated_at) "
                         f"values ('a1','p1','{TODAY} 09:00','王医生','复查','1','复诊','x')")
            conn.execute("insert into appointments(appointment_id, patient_identity, start_time, doctor_name, item_name, status, updated_at) "
                         f"values ('a2','p1','{FUTURE} 09:00','王医生','复查','0','x')")
            conn.execute("insert into appointments(appointment_id, patient_identity, start_time, doctor_name, item_name, status, updated_at) "
                         f"values ('a3','p2','{PAST} 09:00','李医生','洁牙','2','x')")
            conn.execute("insert into medical_records(record_id, patient_identity, created_at, updated_at, current_hash) "
                         f"values ('m1','p1','{PAST}','{PAST}','rh1')")
            conn.execute("insert into return_visits(return_visit_id, patient_identity, due_time, item_name, status, updated_at) "
                         f"values ('r1','p1','{FUTURE}','复查回访','待回访','x')")
            conn.commit()
        return _admin_client(db)

    def test_search_by_chart_no(self):
        # 看板#210:搜索框支持病历号(chart_no 列 + 回退 source_json.patientid)
        with tempfile.TemporaryDirectory() as tmp:
            c = self._client(tmp)
            db = Path(tmp) / "clinic.sqlite3"
            with connect(db) as conn:
                conn.execute("update patients set chart_no='260101007' where patient_identity='p1'")
                conn.execute("update patients set source_json=? where patient_identity='p2'",
                             (json.dumps({"patientid": "209999003"}),))
                conn.commit()
            ids = [p["patient_identity"] for p in c.get("/api/patients?q=260101007").json()["list"]]
            self.assertEqual(ids, ["p1"])
            ids2 = [p["patient_identity"] for p in c.get("/api/patients?q=209999003").json()["list"]]
            self.assertEqual(ids2, ["p2"])   # 回退 source_json.patientid

    def test_group_filter(self):
        with tempfile.TemporaryDirectory() as tmp:
            c = self._client(tmp)
            ids = [p["patient_identity"] for p in c.get("/api/patients?group=种植牙").json()["list"]]
            self.assertEqual(ids, ["p1"])
            ids2 = [p["patient_identity"] for p in c.get("/api/patients?group=正畸患者").json()["list"]]
            self.assertEqual(ids2, ["p2"])

    def test_scope_today_and_recent(self):
        with tempfile.TemporaryDirectory() as tmp:
            c = self._client(tmp)
            today_ids = [p["patient_identity"] for p in c.get("/api/patients?scope=today").json()["list"]]
            self.assertEqual(today_ids, ["p1"])           # 只有 p1 今天有约
            # #109 最近 = SaaS「最近患者」分组(p1 groupname 含最近患者, p2 不含)，与计数同源
            recent_ids = {p["patient_identity"] for p in c.get("/api/patients?scope=recent").json()["list"]}
            self.assertEqual(recent_ids, {"p1"})

    def test_group_filter_space_tolerant(self):
        # 动态扫#108：groupname 带空格("正畸患者, 种植牙")时筛选仍命中，与计数口径一致
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "clinic.sqlite3"
            init_db(db)
            with connect(db) as conn:
                conn.execute("insert into patients(patient_identity, display_name, updated_at, current_hash, patient_group) "
                             "values ('s1','空格患者','x','hs','正畸患者, 种植牙')")
                conn.commit()
            c = TestClient(create_app(db))
            ids = [p["patient_identity"] for p in c.get("/api/patients?group=种植牙").json()["list"]]
            self.assertEqual(ids, ["s1"])

    def test_badge_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            c = self._client(tmp)
            rows = {p["patient_identity"]: p for p in c.get("/api/patients").json()["list"]}
            p1 = rows["p1"]
            self.assertEqual(p1["last_visit_type"], "复诊")
            self.assertEqual(p1["last_doctor"], "王医生")
            self.assertTrue(p1["has_future_appt"])
            self.assertTrue(p1["has_return_visit"])
            self.assertTrue(p1["has_record"])
            self.assertFalse(p1["has_image"])
            self.assertIn("种植牙", p1["groupname"])
            p2 = rows["p2"]
            self.assertFalse(p2["has_future_appt"])
            self.assertFalse(p2["has_return_visit"])
            self.assertFalse(p2["has_record"])


    def test_doctor_and_first_doctor_filter(self):
        # T1：主治医生(有该医生预约)/初诊医生(最早预约医生)筛选
        with tempfile.TemporaryDirectory() as tmp:
            c = self._client(tmp)
            wang = {p["patient_identity"] for p in c.get("/api/patients?doctor=王医生").json()["list"]}
            self.assertEqual(wang, {"p1"})
            fd = {p["patient_identity"] for p in c.get("/api/patients?first_doctor=李医生").json()["list"]}
            self.assertEqual(fd, {"p2"})
            # 列表行带 first_doctor 字段
            rows = {p["patient_identity"]: p for p in c.get("/api/patients").json()["list"]}
            self.assertEqual(rows["p2"]["first_doctor"], "李医生")

    def test_export_csv(self):
        # T2：导出CSV(BOM+表头+数据),Excel可开
        with tempfile.TemporaryDirectory() as tmp:
            c = self._client(tmp)
            r = c.get("/api/patients/export")
            self.assertEqual(r.status_code, 200)
            self.assertIn("text/csv", r.headers.get("content-type", ""))
            text = r.content.decode("utf-8-sig")
            self.assertIn("姓名", text)
            self.assertIn("张三", text)

    def test_export_search_by_chart_no(self):
        # 看板#218:导出 q 口径与列表一致——按病历号筛选后导出也只含命中患者(非空CSV)
        with tempfile.TemporaryDirectory() as tmp:
            c = self._client(tmp)
            db = Path(tmp) / "clinic.sqlite3"
            with connect(db) as conn:
                conn.execute("update patients set chart_no='260101007' where patient_identity='p1'")
                conn.commit()
            text = c.get("/api/patients/export?q=260101007").content.decode("utf-8-sig")
            self.assertIn("张三", text)        # 命中导出
            self.assertNotIn("李四", text)     # 非命中不导出

    def test_export_respects_birthday_filter(self):
        # #124：在"今日生日"筛选里导出,只导出今天生日的患者,不能导出全量
        other = (dt.date.today() - dt.timedelta(days=40)).isoformat()  # 40天前→MM-DD 必不等于今天
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "clinic.sqlite3"
            init_db(db)
            with connect(db) as conn:
                conn.execute("insert into patients(patient_identity, display_name, birthday, updated_at, current_hash) "
                             "values ('bd','寿星',?,'x','h1')", ("1990-" + TODAY[5:],))
                conn.execute("insert into patients(patient_identity, display_name, birthday, updated_at, current_hash) "
                             "values ('ot','路人',?,'x','h2')", ("1988-" + other[5:],))
                conn.commit()
            c = _admin_client(db)
            text = c.get(f"/api/patients/export?birthday_on={TODAY}").content.decode("utf-8-sig")
            self.assertIn("寿星", text)
            self.assertNotIn("路人", text)

    def test_has_record_excludes_ai_draft(self):
        # 动态扫#7：has_record 只算正式病历(draft_status=''), AI草稿不点亮"史"
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "clinic.sqlite3"
            init_db(db)
            with connect(db) as conn:
                conn.execute("insert into patients(patient_identity, display_name, updated_at, current_hash) values ('d1','草稿患者','x','h9')")
                conn.execute("insert into medical_records(record_id, patient_identity, draft_status, created_at, updated_at, current_hash) "
                             f"values ('mr1','d1','ai_draft','{PAST}','{PAST}','rh9')")
                conn.commit()
            c = TestClient(create_app(db))
            row = {p["patient_identity"]: p for p in c.get("/api/patients").json()["list"]}["d1"]
            self.assertFalse(row["has_record"])   # 只有AI草稿 → 不算有病历

    def test_group_filter_escapes_wildcards(self):
        # 动态扫#14：group 含 LIKE 通配符 % 不能匹配全部
        with tempfile.TemporaryDirectory() as tmp:
            c = self._client(tmp)
            # group='%' 字面不存在该分组 → 0 命中(不是匹配所有人)
            self.assertEqual(c.get("/api/patients?group=%25").json()["totalcount"], 0)


if __name__ == "__main__":
    unittest.main()
