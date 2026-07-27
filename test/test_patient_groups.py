"""患者分类：按本地列 patient_group 逗号拆分统计分组计数。"""
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from local_app.api import create_app
from local_app.db import connect, init_db


def _add(conn, pid, groupname):
    conn.execute(
        "insert into patients(patient_identity, display_name, updated_at, current_hash, patient_group) "
        "values (?,?,?,?,?)",
        (pid, pid, "x", "h" + pid, groupname),
    )


class PatientGroupsTest(unittest.TestCase):
    def _client(self, tmp):
        db = Path(tmp) / "clinic.sqlite3"
        init_db(db)
        with connect(db) as conn:
            _add(conn, "p1", "最近患者,种植牙")
            _add(conn, "p2", "种植牙")
            _add(conn, "p3", "正畸患者,最近患者")
            _add(conn, "p4", "最近患者")
            _add(conn, "p5", "")  # 无分组
            conn.commit()
        return TestClient(create_app(db))

    def test_group_counts_split_by_comma(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = self._client(tmp).get("/api/patient-groups").json()
            self.assertEqual(data["total"], 5)
            # 最近患者：p1/p3/p4 = 3，单列出，不进 groups
            self.assertEqual(data["recent"], 3)
            counts = {g["name"]: g["count"] for g in data["groups"]}
            self.assertEqual(counts["种植牙"], 2)   # p1 + p2
            self.assertEqual(counts["正畸患者"], 1)  # p3
            self.assertNotIn("最近患者", counts)     # 系统组不混进手动分组
            # 按 count 降序
            self.assertEqual(data["groups"][0]["name"], "种植牙")

    def test_empty_db(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "clinic.sqlite3"
            init_db(db)
            data = TestClient(create_app(db)).get("/api/patient-groups").json()
            self.assertEqual(data["total"], 0)
            self.assertEqual(data["groups"], [])


if __name__ == "__main__":
    unittest.main()
