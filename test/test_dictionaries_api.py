"""GET /api/dictionaries 端点：前端字典消费(主诉/症状/来源等下拉)。补审查发现的覆盖盲区。"""
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from local_app.api import create_app
from local_app.db import connect, init_db


def _client(tmp):
    db = Path(tmp) / "clinic.sqlite3"
    init_db(db)
    with connect(db) as conn:
        rows = [
            ("d1", "PatientSource", "朋友介绍", "", 2, 0),
            ("d2", "PatientSource", "网络", "", 1, 0),
            ("d3", "PatientSource", "停用来源", "", 3, 1),   # stopped=1 不应返回
            ("d4", "Symptom", "牙痛", "", 1, 0),
        ]
        for did, dtype, name, value, order, stopped in rows:
            conn.execute(
                "insert into dictionaries(dict_id, dict_type, name, value, display_order, stopped) "
                "values (?, ?, ?, ?, ?, ?)", (did, dtype, name, value, order, stopped))
        conn.commit()
    return TestClient(create_app(db))


class DictionariesApiTest(unittest.TestCase):
    def test_all_excludes_stopped(self):
        with tempfile.TemporaryDirectory() as tmp:
            items = _client(tmp).get("/api/dictionaries").json()["items"]
            ids = [i["dict_id"] for i in items]
            self.assertNotIn("d3", ids)          # 停用项排除
            self.assertEqual(len(items), 3)

    def test_filter_by_type(self):
        with tempfile.TemporaryDirectory() as tmp:
            items = _client(tmp).get("/api/dictionaries?type=PatientSource").json()["items"]
            self.assertTrue(all(i["dict_type"] == "PatientSource" for i in items))
            self.assertEqual(len(items), 2)      # d1,d2(d3停用)

    def test_ordered_by_display_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            items = _client(tmp).get("/api/dictionaries?type=PatientSource").json()["items"]
            # display_order: 网络(1) 在 朋友介绍(2) 前
            self.assertEqual([i["name"] for i in items], ["网络", "朋友介绍"])

    def test_unknown_type_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            items = _client(tmp).get("/api/dictionaries?type=Nope").json()["items"]
            self.assertEqual(items, [])


if __name__ == "__main__":
    unittest.main()
