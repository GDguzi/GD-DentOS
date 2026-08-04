"""出厂内置同意书种子:全新库种25套;动过模板不重种;内容无诊所专名等敏感痕迹。"""
import json
import re
import tempfile
import unittest
from pathlib import Path

from local_app.consent_seed import ensure_consent_seed, _SEED_FILE
from local_app.db import connect, init_db


class ConsentSeedTest(unittest.TestCase):
    def test_fresh_db_gets_seed_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "clinic.sqlite3"
            init_db(db)
            self.assertEqual(ensure_consent_seed(db), 25)
            self.assertEqual(ensure_consent_seed(db), 0, "第二次必须无操作")
            with connect(db) as conn:
                n, cats = conn.execute(
                    "select count(*), count(distinct category) from consent_templates "
                    "where active = 1 and source = 'builtin'").fetchone()
            self.assertEqual(n, 25)
            self.assertGreaterEqual(cats, 10)

    def test_user_touched_db_never_reseeded(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "clinic.sqlite3"
            init_db(db)
            with connect(db) as conn:
                conn.execute(
                    "insert into consent_templates(template_id, name, category, body, source, "
                    "active, created_at, updated_at) values ('t1','自建','其他','x','local',0,'','')")
                conn.commit()
            self.assertEqual(ensure_consent_seed(db), 0, "有过模板(哪怕已停用)不得补种")

    def test_seed_content_clean_and_valid(self):
        items = json.loads(_SEED_FILE.read_text(encoding="utf-8"))
        self.assertEqual(len(items), 25)
        names = [t["name"] for t in items]
        self.assertEqual(len(names), len(set(names)), "模板名不得重复")
        text = json.dumps(items, ensure_ascii=False)
        # 剥离守卫:诊所专名/第三方真实机构名/原始文件名/电话等不得进开源种子
        # 黑名单词以 unicode 转义书写:运行时等价,但敏感词字面不进入仓库文本
        for bad in ("\u53e4\u5fb7", "\u4e50\u660c", "\u53e4\u4ed4", "\u7259\u533b\u7ba1\u5bb6",
                    ".docx", ".doc", ".wps", ".dtf",
                    "\u534e\u897f", "\u6e58\u96c5", "\u534f\u548c", "\u5317\u5927\u53e3\u8154",
                    "\u4e5d\u9662", "\u7a7a\u519b\u519b\u533b", "\u7b2c\u56db\u519b\u533b"):
            self.assertNotIn(bad, text, f"种子含敏感痕迹: {bad}")
        self.assertFalse(re.findall(r"1[3-9]\d{9}", text), "种子不得含手机号")
        for t in items:
            self.assertTrue(t["name"].strip() and t["category"].strip())
            self.assertGreater(len(t["body"].strip()), 100, t["name"])


if __name__ == "__main__":
    unittest.main()
