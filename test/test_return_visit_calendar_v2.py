import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from local_app.api import create_app
from local_app.db import connect, init_db


class ReturnVisitCalendarV2Test(unittest.TestCase):
    def _client(self, tmp):
        db = Path(tmp) / "clinic.sqlite3"
        init_db(db)
        with connect(db) as conn:
            conn.execute(
                "insert into patients(patient_identity,display_name,responsible_doctor,updated_at,current_hash) "
                "values('p1','患者甲','主治甲','x','h1')"
            )
            conn.execute(
                "insert into patients(patient_identity,display_name,responsible_doctor,updated_at,current_hash) "
                "values('p2','患者乙','主治乙','x','h2')"
            )
            rows = [
                ("done1", "p1", "2026-06-01 09:00", "2026-06-10", "结果乙", "已回访", "phone", "满意", "回访人甲"),
                ("done2", "p1", "2026-06-11 09:00", "", "结果甲", "完成", "wechat", "不满意", "回访人甲"),
                ("pending", "p1", "2026-06-12 09:00", "", "结果甲", "待回访", "", "", "回访人甲"),
                ("other", "p2", "2026-06-13 09:00", "", "别的", "待回访", "sms", "一般", "回访人乙"),
            ]
            for rid, pid, due, actual, result, status, channel, satisfaction, visitor in rows:
                conn.execute(
                    "insert into return_visits(return_visit_id,patient_identity,due_time,actual_date,item_name,"
                    "return_result,status,channel,satisfaction,visitor) values(?,?,?,?, '事项',?,?,?,?,?)",
                    (rid, pid, due, actual, result, status, channel, satisfaction, visitor),
                )
            conn.execute(
                "insert into patients(patient_identity,display_name,is_deleted,updated_at,current_hash) "
                "values('pdel','停用患者',1,'x','hd')"
            )
            conn.execute(
                "insert into return_visits(return_visit_id,patient_identity,due_time,item_name,status) "
                "values('hidden','pdel','2026-06-14 09:00','隐藏','待回访')"
            )
            conn.commit()
        return TestClient(create_app(db))

    def test_summary_uses_same_filtered_rows_as_days(self):
        with tempfile.TemporaryDirectory() as tmp:
            body = self._client(tmp).get(
                "/api/return-visits/calendar?month=2026-06&doctor=主治甲&visitor=回访人甲"
            ).json()
            self.assertEqual(body["summary"]["total"], sum(day["count"] for day in body["days"]))
            self.assertEqual(body["summary"]["total"], 3)
            self.assertEqual(body["summary"]["completed"], 2)
            self.assertEqual(body["summary"]["pending"], 1)
            self.assertEqual(body["summary"]["overdue"], 1)
            self.assertEqual(body["summary"]["completion_rate"], 66.7)
            self.assertEqual(
                body["summary"]["problem_counts"],
                {"needs_attention": 2, "overdue": 1, "low_satisfaction": 1},
            )

    def test_summary_has_fixed_channels_results_and_disabled_narrative(self):
        with tempfile.TemporaryDirectory() as tmp:
            summary = self._client(tmp).get("/api/return-visits/calendar?month=2026-06").json()["summary"]
            self.assertEqual(summary["channel_counts"], [
                {"channel": "phone", "count": 1}, {"channel": "wechat", "count": 1},
                {"channel": "other", "count": 1}, {"channel": "unknown", "count": 1},
            ])
            self.assertEqual(summary["top_results"], [
                {"label": "结果甲", "count": 2}, {"label": "别的", "count": 1},
                {"label": "结果乙", "count": 1},
            ])
            self.assertEqual(summary["narrative"], {"status": "disabled", "text": ""})

    def test_days_keep_done_name_and_completed_actual_date_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            days = {d["date"]: d for d in self._client(tmp).get(
                "/api/return-visits/calendar?month=2026-06&doctor=主治甲"
            ).json()["days"]}
            self.assertEqual(days["2026-06-10"]["done"], 1)
            self.assertEqual(days["2026-06-11"]["done"], 1)
            self.assertNotIn("completed", days["2026-06-10"])

    def test_month_is_strictly_zero_padded(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = self._client(tmp)
            for bad in ("2026-7", " 2026-07", "2026-07 ", "", "2026-00", "2026-13", "not-a-date"):
                self.assertEqual(client.get("/api/return-visits/calendar", params={"month": bad}).status_code, 400, bad)

    def test_empty_month_summary_is_all_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            summary = self._client(tmp).get("/api/return-visits/calendar?month=2025-01").json()["summary"]
            self.assertEqual(summary["total"], 0)
            self.assertEqual(summary["completion_rate"], 0.0)
            self.assertEqual(sum(x["count"] for x in summary["channel_counts"]), 0)

    def test_narrative_executor_receives_only_numeric_whitelist(self):
        from local_app.rv_query import build_monthly_narrative

        summary = {
            "total": 1, "completed": 1, "pending": 0, "overdue": 0, "completion_rate": 100.0,
            "channel_counts": [{"channel": "phone", "count": 1}, {"channel": "敏感渠道文本", "count": 99}],
            "problem_counts": {"needs_attention": 0, "overdue": 0, "low_satisfaction": 0,
                               "敏感问题文本": 99},
            "top_results": [{"label": "敏感业务文本", "count": 1}],
            "patient": "不可发送", "content": "不可发送",
        }
        received = []
        out = build_monthly_narrative(summary, executor=lambda payload: received.append(payload) or "聚合说明")
        self.assertEqual(out, {"status": "ready", "text": "聚合说明"})
        self.assertEqual(set(received[0]), {
            "total", "completed", "pending", "overdue", "completion_rate", "channel_counts", "problem_counts",
        })
        self.assertNotIn("敏感业务文本", repr(received[0]))
        self.assertNotIn("敏感渠道文本", repr(received[0]))
        self.assertNotIn("敏感问题文本", repr(received[0]))
        failed = build_monthly_narrative(summary, executor=lambda _payload: (_ for _ in ()).throw(TimeoutError()))
        self.assertEqual(failed, {"status": "failed", "text": ""})

    def test_narrative_malformed_nested_aggregates_fail_closed(self):
        from local_app.rv_query import build_monthly_narrative

        base = {
            "total": 0, "completed": 0, "pending": 0, "overdue": 0, "completion_rate": 0.0,
            "channel_counts": [],
            "problem_counts": {"needs_attention": 0, "overdue": 0, "low_satisfaction": 0},
        }
        for malformed in (
            {**base, "channel_counts": None},
            {**base, "problem_counts": None},
        ):
            self.assertEqual(
                build_monthly_narrative(malformed, executor=lambda _payload: "不应生成"),
                {"status": "failed", "text": ""},
            )

    def test_low_satisfaction_deduplicates_attention_with_fixed_today(self):
        from local_app.rv_query import build_month_calendar

        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "calendar.sqlite3"
            init_db(db)
            with connect(db) as conn:
                conn.execute(
                    "insert into patients(patient_identity,display_name,updated_at,current_hash) "
                    "values('p1','合成患者','x','h1')"
                )
                rows = [
                    ("overlap", "2026-07-01", "待回访", "不满意"),
                    ("low2", "2026-07-02", "已回访", "非常不满意"),
                    ("low3", "2026-07-03", "已回访", "差"),
                    ("low4", "2026-07-04", "已回访", "很差"),
                    ("normal", "2026-07-05", "已回访", "一般"),
                ]
                for rid, due, status, satisfaction in rows:
                    conn.execute(
                        "insert into return_visits(return_visit_id,patient_identity,due_time,item_name,status,satisfaction) "
                        "values(?, 'p1', ?, '事项', ?, ?)",
                        (rid, due, status, satisfaction),
                    )
                body = build_month_calendar(conn, "2026-07", today="2026-07-20")
            self.assertEqual(body["summary"]["problem_counts"], {
                "needs_attention": 4, "overdue": 1, "low_satisfaction": 4,
            })

    def test_each_calendar_filter_keeps_summary_and_days_in_lockstep(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = self._client(tmp)
            cases = (
                ("q=结果乙", 1), ("status=完成", 1), ("doctor=主治乙", 1),
                ("visitor=回访人乙", 1), ("channel=wechat", 1),
            )
            for query, expected in cases:
                body = client.get(f"/api/return-visits/calendar?month=2026-06&{query}").json()
                self.assertEqual(body["summary"]["total"], expected, query)
                self.assertEqual(body["summary"]["total"], sum(day["count"] for day in body["days"]), query)

    def test_top_results_limits_five_and_sorts_count_then_label(self):
        from local_app.rv_query import build_month_calendar

        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "top.sqlite3"
            init_db(db)
            with connect(db) as conn:
                conn.execute(
                    "insert into patients(patient_identity,display_name,updated_at,current_hash) "
                    "values('p1','合成患者','x','h1')"
                )
                counts = {"A": 3, "B": 3, "C": 2, "D": 2, "E": 1, "F": 1, "G": 1}
                n = 0
                for label, count in counts.items():
                    for _ in range(count):
                        n += 1
                        conn.execute(
                            "insert into return_visits(return_visit_id,patient_identity,due_time,item_name,status,return_result) "
                            "values(?, 'p1', '2026-07-10', '事项', '已回访', ?)",
                            (f"rv{n}", label),
                        )
                body = build_month_calendar(conn, "2026-07", today="2026-07-20")
            self.assertEqual(body["summary"]["top_results"], [
                {"label": "A", "count": 3}, {"label": "B", "count": 3},
                {"label": "C", "count": 2}, {"label": "D", "count": 2},
                {"label": "E", "count": 1},
            ])


if __name__ == "__main__":
    unittest.main()


class ReturnVisitChannelFallbackTest(ReturnVisitCalendarV2Test):
    """GD-06:月度统计 channel 优先、旧数据中文 return_type 回退;筛选与汇总同一口径。"""

    def _seed_july_legacy(self, tmp):
        client = self._client(tmp)
        db = Path(tmp) / "clinic.sqlite3"
        with connect(db) as conn:
            rows = [
                ("jl-phone", "电话", ""),
                ("jl-wechat", "微信", ""),
                ("jl-visit", "到店", ""),
                ("jl-none", "", ""),
                ("jl-mixed", "微信", "phone"),   # 显式 channel 以它为准
            ]
            for rid, rtype, channel in rows:
                conn.execute(
                    "insert into return_visits(return_visit_id,patient_identity,due_time,"
                    "item_name,status,return_type,channel) "
                    "values(?, 'p1', '2026-07-05 09:00', '事项', '待回访', ?, ?)",
                    (rid, rtype, channel),
                )
            conn.commit()
        return client

    def test_legacy_return_type_maps_into_channel_counts(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = self._seed_july_legacy(tmp)
            summary = client.get("/api/return-visits/calendar?month=2026-07").json()["summary"]
            self.assertEqual(summary["channel_counts"], [
                {"channel": "phone", "count": 2},
                {"channel": "wechat", "count": 1},
                {"channel": "other", "count": 1},
                {"channel": "unknown", "count": 1},
            ])

    def test_channel_filter_and_summary_share_normalization(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = self._seed_july_legacy(tmp)
            body = client.get("/api/return-visits/calendar?month=2026-07&channel=wechat").json()
            self.assertEqual(body["summary"]["total"], 1)
            self.assertEqual(sum(day["count"] for day in body["days"]), 1)
            self.assertEqual(body["summary"]["channel_counts"], [
                {"channel": "phone", "count": 0},
                {"channel": "wechat", "count": 1},
                {"channel": "other", "count": 0},
                {"channel": "unknown", "count": 0},
            ])
