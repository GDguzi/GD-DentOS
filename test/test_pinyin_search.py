"""患者拼音搜索：name_pinyin 列参与 /api/patients 检索（全拼/首字母/大小写）。"""
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from local_app.api import create_app
from local_app.db import connect, init_db
from local_app.pinyin_util import name_pinyin


class PinyinSearchTest(unittest.TestCase):
    def _client(self, tmp):
        db = Path(tmp) / "clinic.sqlite3"
        init_db(db)
        with connect(db) as conn:
            conn.execute(
                "insert into patients(patient_identity, display_name, name_pinyin, updated_at, current_hash) "
                "values ('p1', '罗小云', 'luoxiaoyun lxy', '2026-06-15', 'h1')"
            )
            conn.execute(
                "insert into patients(patient_identity, display_name, name_pinyin, updated_at, current_hash) "
                "values ('p2', '赵大力', 'zhaodali zdl', '2026-06-15', 'h2')"
            )
            conn.commit()
        return TestClient(create_app(db))

    def _names(self, client, q):
        return {r["display_name"] for r in client.get(f"/api/patients?q={q}").json()["list"]}

    def test_search_by_initials(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = self._client(tmp)
            self.assertEqual(self._names(client, "lxy"), {"罗小云"})

    def test_search_by_full_pinyin(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = self._client(tmp)
            self.assertEqual(self._names(client, "luoxiao"), {"罗小云"})

    def test_search_case_insensitive(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = self._client(tmp)
            self.assertEqual(self._names(client, "ZDL"), {"赵大力"})

    def test_chinese_still_works(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = self._client(tmp)
            self.assertEqual(self._names(client, "赵大力"), {"赵大力"})

    def test_name_pinyin_empty_input(self):
        # 软依赖：空名安全返回空串（不依赖 pypinyin 是否安装）
        self.assertEqual(name_pinyin(""), "")
        self.assertEqual(name_pinyin(None), "")


if __name__ == "__main__":
    unittest.main()
