"""门诊日志(对标 SaaS)：每条就诊一行 + 病历正文(诊断/措施) + 住址。"""
import json
import tempfile
import unittest
from pathlib import Path
from urllib.parse import urlencode

from fastapi.testclient import TestClient

from local_app import auth
from local_app.api import create_app
from local_app.db import connect, init_db
from local_app.services.finance_service import emr_text as _emr_text


def _client(tmp):
    db = Path(tmp) / "clinic.sqlite3"
    init_db(db)
    with connect(db) as c:
        auth.create_user(c, "admin", "管理员", "admin123", role="admin")
        c.execute("insert into patients(patient_identity, display_name, chart_no, birthday, address, "
                  "updated_at, current_hash) values ('p1','张三','260101001','2000-01-01','碧桂园3栋','2026-07-01','h1')")
        c.commit()
    client = TestClient(create_app(db))
    client.post("/api/auth/login", json={"username": "admin", "password": "admin123"})
    return db, client


def _visit(c, rid, pid, doctor, vt, day, content):
    c.execute("insert into medical_records(record_id, patient_identity, record_type, visit_time, "
              "doctor_name, content_json, current_hash) values (?,?,?,?,?,?,?)",
              (rid, pid, vt, day + " 14:30:00", doctor, json.dumps(content), "h_" + rid))


class ClinicLogTest(unittest.TestCase):
    def _q(self, client, **kw):
        return client.get("/api/store-stats/clinic-log?" + urlencode(
            {"start": "2026-07-01", "end": "2026-07-31", **kw})).json()

    def test_emr_text_helper(self):
        self.assertEqual(_emr_text(json.dumps({"TR": "去腐开髓"}), "TR"), "去腐开髓")
        self.assertEqual(_emr_text(json.dumps({"DG": ["龋齿", "牙髓炎"]}), "DG"), "龋齿；牙髓炎")
        self.assertEqual(_emr_text(json.dumps({}), "TR"), "")
        self.assertEqual(_emr_text("", "TR"), "")
        self.assertEqual(_emr_text("not json", "TR"), "")

    def test_emr_text_nested(self):
        # #687: 嵌套对象/数组套嵌套 → 递归提叶子文本,不吐 Python 字面量
        self.assertEqual(_emr_text(json.dumps({"DG": {"name": "龋齿", "detail": "牙髓炎"}}), "DG"),
                         "龋齿；牙髓炎")
        self.assertEqual(_emr_text(json.dumps({"TR": [{"op": "去腐", "steps": ["开髓", "充填"]}]}), "TR"),
                         "去腐；开髓；充填")
        # 空值/空字符串被过滤,不留孤立分隔符
        self.assertEqual(_emr_text(json.dumps({"TR": {"a": "", "b": "补牙", "c": None}}), "TR"), "补牙")
        # 不应出现花括号/方括号等结构符号
        out = _emr_text(json.dumps({"DG": {"x": {"y": "深龋"}}}), "DG")
        self.assertEqual(out, "深龋")
        self.assertNotIn("{", out)
        self.assertNotIn("[", out)

    def test_row_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            db, client = _client(tmp)
            with connect(db) as c:
                _visit(c, "r1", "p1", "测试医生B", "初诊", "2026-07-06",
                       {"DG": "龋齿", "TR": "去腐，树脂充填"})
                c.commit()
            d = self._q(client)
            self.assertEqual(d["count"], 1)
            r = d["rows"][0]
            self.assertEqual(r["chart_no"], "260101001")
            self.assertEqual(r["visit_type"], "初诊")
            self.assertEqual(r["doctor"], "测试医生B")
            self.assertEqual(r["address"], "碧桂园3栋")
            self.assertEqual(r["diagnosis"], "龋齿")
            self.assertEqual(r["treatment"], "去腐，树脂充填")
            self.assertEqual(r["department"], "")      # 本地无科室
            self.assertEqual(r["destination"], "")     # 本地无去向

    def test_doctor_filter(self):
        with tempfile.TemporaryDirectory() as tmp:
            db, client = _client(tmp)
            with connect(db) as c:
                _visit(c, "r1", "p1", "测试医生B", "初诊", "2026-07-06", {})
                _visit(c, "r2", "p1", "测试医生A", "复诊", "2026-07-06", {})
                c.commit()
            self.assertEqual(self._q(client, doctor="测试医生B")["count"], 1)
            self.assertEqual(self._q(client)["count"], 2)


if __name__ == "__main__":
    unittest.main()
