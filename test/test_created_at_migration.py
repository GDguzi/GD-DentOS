"""#432:created_at 补列不能丢。旧库(appointments/return_visits 无 created_at)跑启动
后必须补上该列,否则今日工作台查询/新增预约回访/同步导入都会 no such column。
Phase3 第二刀后补列走 schema.sql 差分(_apply_schema_diff),本测试意图不变、实现跟随。"""
import sqlite3
import tempfile
import unittest
from pathlib import Path

from local_app.db import _apply_schema_diff, connect


class CreatedAtMigrationTest(unittest.TestCase):
    def test_old_db_gets_created_at_column(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "old.sqlite3"
            c0 = sqlite3.connect(db)
            c0.execute("create table appointments(appointment_id text primary key, patient_identity text)")
            c0.execute("create table return_visits(return_visit_id text primary key, patient_identity text)")
            c0.commit()
            c0.close()
            conn = connect(db)
            _apply_schema_diff(conn)
            conn.commit()
            cols_a = {r[1] for r in conn.execute("pragma table_info(appointments)")}
            cols_r = {r[1] for r in conn.execute("pragma table_info(return_visits)")}
            conn.close()
            self.assertIn("created_at", cols_a, "appointments 升级后应补 created_at")
            self.assertIn("created_at", cols_r, "return_visits 升级后应补 created_at")


if __name__ == "__main__":
    unittest.main()
