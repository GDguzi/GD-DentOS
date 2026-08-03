"""开源壳阶段2守卫:患者档案类字段(分组/备注)真相源=本地列,不再读 source_json。
1) 空库本地患者(无 source_json)分组计数/筛选/列表别名/备注全有值——开源空库场景。
2) 本地编辑过的备注(列)不再被旧 source_json 值遮蔽——修复半迁移暗债的回归锁。
3) 源码守卫:患者域路由不得再出现 $.groupname / patients 表的 $.remark 读取。
生产等价性已实测确认:分组列的非空值是 source_json 同名键的严格超集,无需回填。
"""
import json
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from local_app import auth
from local_app.api import create_app
from local_app.db import connect, init_db

ROUTES = Path(__file__).resolve().parent.parent / "local_app" / "routes"


class LocalColumnsAreTruthTest(unittest.TestCase):
    def _client(self, tmp):
        db = Path(tmp) / "clinic.sqlite3"
        init_db(db)
        with connect(db) as conn:
            auth.create_user(conn, "boss", "院长", "admin123", role="admin")
            conn.commit()
        client = TestClient(create_app(db))
        return db, client

    def test_local_only_patient_groups_and_remark(self):
        # 开源空库场景:本地建档患者只有列、没有 source_json,分组/备注功能必须完整
        with tempfile.TemporaryDirectory() as tmp:
            db, client = self._client(tmp)
            with connect(db) as conn:
                conn.execute(
                    "insert into patients(patient_identity, display_name, updated_at, current_hash, "
                    "patient_group, remark) values ('local1','王五','x','hl1','种植牙,年卡','对青霉素敏感')")
                conn.commit()
            groups = client.get("/api/patient-groups").json()["groups"]
            self.assertEqual({g["name"]: g["count"] for g in groups}, {"种植牙": 1, "年卡": 1})
            lst = client.get("/api/patients?group=种植牙").json()["list"]
            self.assertEqual([p["patient_identity"] for p in lst], ["local1"])
            self.assertEqual(lst[0]["groupname"], "种植牙,年卡")   # 前端别名保持
            self.assertEqual(lst[0]["remark"], "对青霉素敏感")

    def test_local_edited_remark_not_masked_by_stale_json(self):
        # 半迁移暗债回归:编辑路径写列,旧读法读 JSON→本地改的备注在列表里看不见;迁列后列必须赢
        with tempfile.TemporaryDirectory() as tmp:
            db, client = self._client(tmp)
            with connect(db) as conn:
                conn.execute(
                    "insert into patients(patient_identity, display_name, updated_at, current_hash, "
                    "remark, source_json) values ('sync1','赵六','x','hs1','本地改过的备注', ?)",
                    (json.dumps({"remark": "同步来的旧备注"}),))
                conn.commit()
            lst = client.get("/api/patients?q=赵六").json()["list"]
            self.assertEqual(lst[0]["remark"], "本地改过的备注")


class NoPatientJsonReadsTest(unittest.TestCase):
    def test_patient_routes_no_json_group_or_remark(self):
        for fname in ("patients.py", "patient_groups.py", "today_work.py"):
            text = (ROUTES / fname).read_text(encoding="utf-8")
            self.assertNotIn("$.groupname", text, f"{fname} 不得再读 source_json 分组(阶段2迁列)")
        self.assertNotIn("$.remark", (ROUTES / "patients.py").read_text(encoding="utf-8"),
                         "patients.py 不得再读 source_json 备注(阶段2迁列)")


if __name__ == "__main__":
    unittest.main()
