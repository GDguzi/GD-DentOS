"""操作历史(审计日志)页后端:/api/audit-logs 支持按 操作人/日期范围/关键词 筛选 + 分页。
合成 audit_logs 行,只断言筛选/分页行为。"""
import json
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from local_app.api import create_app
from local_app.db import connect, init_db


class AuditLogFiltersTest(unittest.TestCase):
    def _seed(self, tmp):
        db = Path(tmp) / "clinic.sqlite3"
        init_db(db)
        rows = [
            ("appointment", "a1", "create_appointment", "前台A", "2026-06-28 09:00:00"),
            ("payment", "p1", "confirm_payment", "收银B", "2026-06-29 10:00:00"),
            ("payment", "p2", "refund", "收银B", "2026-06-30 11:00:00"),
            ("return_visit", "r1", "create_return_visit", "前台A", "2026-06-30 12:00:00"),
        ]
        with connect(db) as conn:
            for et, eid, act, op, ts in rows:
                conn.execute(
                    "insert into audit_logs(entity_type, entity_id, action, operator, created_at) "
                    "values (?,?,?,?,?)", (et, eid, act, op, ts))
            conn.commit()
        return db

    def test_filter_by_operator(self):
        with tempfile.TemporaryDirectory() as tmp:
            c = TestClient(create_app(self._seed(tmp)))
            d = c.get("/api/audit-logs?operator=收银B").json()
            self.assertEqual(d["totalcount"], 2)
            self.assertTrue(all(r["operator"] == "收银B" for r in d["list"]))

    def test_operators_endpoint_distinct_sorted(self):
        # 操作人下拉数据源——去重+排序,空操作人不出现
        with tempfile.TemporaryDirectory() as tmp:
            db = self._seed(tmp)
            with connect(db) as conn:
                conn.execute("insert into audit_logs(entity_type, entity_id, action, operator, created_at) "
                             "values ('patient','x1','update_patient','','2026-06-30 13:00:00')")
                conn.commit()
            c = TestClient(create_app(db))
            d = c.get("/api/audit-logs/operators").json()
            self.assertEqual(d["operators"], ["前台A", "收银B"])

    def test_filter_by_date_range(self):
        with tempfile.TemporaryDirectory() as tmp:
            c = TestClient(create_app(self._seed(tmp)))
            d = c.get("/api/audit-logs?date_from=2026-06-30&date_to=2026-06-30").json()
            self.assertEqual(d["totalcount"], 2, "只今天两条")

    def test_filter_by_keyword(self):
        with tempfile.TemporaryDirectory() as tmp:
            c = TestClient(create_app(self._seed(tmp)))
            d = c.get("/api/audit-logs?q=refund").json()
            self.assertEqual(d["totalcount"], 1)
            self.assertEqual(d["list"][0]["action"], "refund")

    def test_pagination(self):
        with tempfile.TemporaryDirectory() as tmp:
            c = TestClient(create_app(self._seed(tmp)))
            d = c.get("/api/audit-logs?pagesize=2&pageno=1").json()
            self.assertEqual(len(d["list"]), 2)
            self.assertEqual(d["totalcount"], 4)

    def test_bad_date_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            c = TestClient(create_app(self._seed(tmp)))
            self.assertEqual(c.get("/api/audit-logs?date_from=notadate").status_code, 400)

    def test_handle_item_change_exposes_only_safe_projected_snapshots(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = self._seed(tmp)
            old_item = {
                "handle_id": "synthetic-handle",
                "code": "OLD",
                "name": "旧项目",
                "unit": "次",
                "price": 100,
                "fee_type": "治疗费",
                "stopped": 0,
                "source_json": {"must_not_leak": "secret"},
                "last_batch_id": "must-not-leak",
            }
            new_item = {
                **old_item,
                "code": "NEW",
                "name": "新项目",
                "price": 120,
            }
            with connect(db) as conn:
                conn.execute(
                    "insert into audit_logs(entity_type, entity_id, action, operator, "
                    "old_json, new_json, created_at) values (?,?,?,?,?,?,?)",
                    (
                        "handle_item",
                        "synthetic-handle",
                        "update_handle_item",
                        "合成管理员",
                        json.dumps(old_item, ensure_ascii=False),
                        json.dumps(new_item, ensure_ascii=False),
                        "2026-06-30 13:00:00",
                    ),
                )
                conn.commit()

            row = TestClient(create_app(db)).get(
                "/api/audit-logs?entity_type=handle_item"
            ).json()["list"][0]
            self.assertEqual(
                row["change"],
                {
                    "before": {
                        "handle_id": "synthetic-handle",
                        "code": "OLD",
                        "name": "旧项目",
                        "fee_type": "治疗费",
                        "unit": "次",
                        "price": 100,
                        "stopped": 0,
                    },
                    "after": {
                        "handle_id": "synthetic-handle",
                        "code": "NEW",
                        "name": "新项目",
                        "fee_type": "治疗费",
                        "unit": "次",
                        "price": 120,
                        "stopped": 0,
                    },
                },
            )
            self.assertNotIn("old_json", row)
            self.assertNotIn("new_json", row)
            self.assertNotIn("source_json", json.dumps(row, ensure_ascii=False))
            self.assertNotIn("last_batch_id", json.dumps(row, ensure_ascii=False))


if __name__ == "__main__":
    unittest.main()
