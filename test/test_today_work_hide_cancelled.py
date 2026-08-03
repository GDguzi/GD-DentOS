"""#418:今日工作台必须隐藏取消/疑似取消的预约(KPI/候诊队列/漏斗/就诊类型桶/明日预约都排除),
数据仍保留供历史/预约管理追溯。合成数据,只断言计数与可见 id,不碰真实库。"""
import datetime as dt
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from local_app.api import create_app
from local_app.db import connect, init_db


class TodayWorkHideCancelledTest(unittest.TestCase):
    def _seed(self, tmp):
        db = Path(tmp) / "clinic.sqlite3"
        init_db(db)
        today = dt.date.today().isoformat()
        tomorrow = (dt.date.today() + dt.timedelta(days=1)).isoformat()
        with connect(db) as conn:
            for i in range(1, 6):
                conn.execute(
                    "insert into patients(patient_identity, display_name, updated_at, current_hash) "
                    "values (?,?,?,?)", (f"p{i}", f"患者{i}", "x", f"h{i}"))

            def appt(aid, pid, status, suspect, when=today, vt="初诊"):
                conn.execute(
                    "insert into appointments(appointment_id, patient_identity, start_time, status, "
                    "suspect_cancelled, visit_type, updated_at) values (?,?,?,?,?,?,'x')",
                    (aid, pid, f"{when} 09:00:00", status, suspect, vt))

            appt("a-normal", "p1", "已确认", 0)     # 正常 → 显示
            appt("a-cancel", "p2", "3", 0)          # 已取消 → 隐藏
            appt("a-suspect", "p3", "已确认", 1)     # 疑似取消(同步标记) → 隐藏
            appt("a-tmr-ok", "p4", "已确认", 0, tomorrow)
            appt("a-tmr-cancel", "p5", "已取消", 0, tomorrow)
            conn.commit()
        return db, today

    def test_cancelled_and_suspect_hidden_everywhere(self):
        with tempfile.TemporaryDirectory() as tmp:
            db, today = self._seed(tmp)
            client = TestClient(create_app(db))
            d = client.get(f"/api/today-work?date={today}").json()
            s = d["summary"]
            queue_ids = sorted(a["appointment_id"] for a in d["appointments"])
            tmr_ids = sorted(a["appointment_id"] for a in d["tomorrow_appointments"])

            self.assertEqual(queue_ids, ["a-normal"], "候诊队列应只剩正常预约")
            self.assertEqual(tmr_ids, ["a-tmr-ok"], "明日预约应排除取消")
            self.assertEqual(s["appointments_today"], 1, "今日 KPI 应排除取消/疑似取消")
            self.assertEqual(s["tomorrow_appointments"], 1, "明日 KPI 应排除取消")
            self.assertEqual(s["appts_waiting"], 1, "候诊漏斗应排除取消/疑似取消")
            self.assertEqual(s["first_visits_today"], 1, "初诊桶应排除取消/疑似取消")

    def test_future_suspect_cancelled_not_counted_as_rebooked(self):
        # #423:约下次高亮的"未来预约"要排除取消/疑似取消;否则取消的未来约也点亮"预约"按钮
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "clinic.sqlite3"
            init_db(db)
            today = dt.date.today().isoformat()
            future = (dt.date.today() + dt.timedelta(days=5)).isoformat()
            with connect(db) as conn:
                conn.execute("insert into patients(patient_identity, display_name, updated_at, current_hash) "
                             "values ('pf','未来疑取消','x','hf')")
                conn.execute("insert into appointments(appointment_id, patient_identity, start_time, status, "
                             "suspect_cancelled, updated_at) values ('af-now','pf',?, '已确认',0,'x')",
                             (f"{today} 09:00:00",))
                conn.execute("insert into appointments(appointment_id, patient_identity, start_time, status, "
                             "suspect_cancelled, updated_at) values ('af-fut','pf',?, '已确认',1,'x')",
                             (f"{future} 09:00:00",))
                conn.commit()
            d = TestClient(create_app(db)).get(f"/api/today-work?date={today}").json()
            row = next(a for a in d["appointments"] if a["patient_identity"] == "pf")
            self.assertFalse(row["has_future_appt"], "疑似取消的未来预约不该点亮约下次")

    def test_highlight_flags_today_actions_only(self):
        # 用户口径:操作按钮深色=今天本地新增/操作了。历史欠费/旧未来预约/同步触碰都不点亮。
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "clinic.sqlite3"
            init_db(db)
            today = dt.date.today().isoformat()
            future = (dt.date.today() + dt.timedelta(days=3)).isoformat()
            with connect(db) as conn:
                conn.execute("insert into patients(patient_identity, display_name, updated_at, current_hash) "
                             "values ('p1','张','x','h1')")
                # 今天的队列预约:同步来的(created_at 空)
                conn.execute("insert into appointments(appointment_id, patient_identity, start_time, status, created_at, updated_at) "
                             "values ('a-now','p1',?, '已确认','','x')", (f"{today} 09:00:00",))
                # 历史欠费账单(应收>已付)
                conn.execute("insert into bills(bill_id, patient_identity, total_fee, paid_fee, state, bill_time) "
                             "values ('b1','p1',500,0,'pending','2026-06-01 10:00')")
                conn.commit()
                d = TestClient(create_app(db)).get(f"/api/today-work?date={today}").json()
                row = next(a for a in d["appointments"] if a["appointment_id"] == "a-now")
                self.assertFalse(row["paid_today"], "今天没收款→收费不亮")
                self.assertFalse(row["rebooked_today"], "没今天新增预约→预约不亮(历史/同步约不算)")
                self.assertFalse(row["return_visit_today"], "没今天新增回访→回访不亮")

                # 今天本地新增:未来预约 + 回访 → 应点亮
                conn.execute("insert into appointments(appointment_id, patient_identity, start_time, status, created_at, updated_at) "
                             "values ('a-next','p1',?, '已确认',?, 'x')", (f"{future} 10:00:00", f"{today} 11:00:00"))
                conn.execute("insert into return_visits(return_visit_id, patient_identity, due_time, is_deleted, created_at, updated_at) "
                             "values ('rv1','p1',?,0,?, 'x')", (future, f"{today} 11:05:00"))
                conn.commit()
                d2 = TestClient(create_app(db)).get(f"/api/today-work?date={today}").json()
                row2 = next(a for a in d2["appointments"] if a["appointment_id"] == "a-now")
                self.assertTrue(row2["rebooked_today"], "今天新增了未来预约→预约亮")
                self.assertTrue(row2["return_visit_today"], "今天新增了回访→回访亮")

    def test_rebooked_today_requires_future_not_current(self):
        # #433:今天新建的"当前到诊预约"(start=今天)不算"约了下次",预约不该亮
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "clinic.sqlite3"
            init_db(db)
            today = dt.date.today().isoformat()
            with connect(db) as conn:
                conn.execute("insert into patients(patient_identity, display_name, updated_at, current_hash) "
                             "values ('p1','张','x','h1')")
                # 今天新建、start_time 也是今天(当前这条到诊预约),无未来预约
                conn.execute("insert into appointments(appointment_id, patient_identity, start_time, status, created_at, updated_at) "
                             "values ('a-now','p1',?, '已确认',?, 'x')", (f"{today} 09:00:00", f"{today} 08:00:00"))
                conn.commit()
            d = TestClient(create_app(db)).get(f"/api/today-work?date={today}").json()
            row = next(a for a in d["appointments"] if a["appointment_id"] == "a-now")
            self.assertFalse(row["rebooked_today"], "当前到诊预约(start=今天)不算约下次,预约不该亮")

    def test_data_retained_in_db(self):
        # 隐藏只是展示层:库里 5 条预约一条不少(历史/预约管理仍可查)
        with tempfile.TemporaryDirectory() as tmp:
            db, _ = self._seed(tmp)
            with connect(db) as conn:
                self.assertEqual(
                    conn.execute("select count(*) from appointments").fetchone()[0], 5)

    def test_return_visits_share_done_semantics_and_exclude_deleted_patients(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "clinic.sqlite3"
            init_db(db)
            day = "2026-07-10"
            with connect(db) as conn:
                conn.execute(
                    "insert into patients(patient_identity,display_name,updated_at,current_hash) "
                    "values('active','合成患者','x','ha')"
                )
                conn.execute(
                    "insert into patients(patient_identity,display_name,is_deleted,updated_at,current_hash) "
                    "values('deleted','停用患者',1,'x','hd')"
                )
                conn.execute(
                    "insert into return_visits(return_visit_id,patient_identity,due_time,item_name,status,note) "
                    "values('conflict','active',?,'已回访-旧标','待回访','已回访 旧标')",
                    (f"{day} 09:00",),
                )
                conn.execute(
                    "insert into return_visits(return_visit_id,patient_identity,due_time,item_name,status) "
                    "values('hidden','deleted',?,'隐藏事项','待回访')",
                    (f"{day} 10:00",),
                )
                conn.commit()
            body = TestClient(create_app(db)).get(f"/api/today-work?date={day}").json()
            self.assertEqual(body["summary"]["pending_return_visits"], 1)
            self.assertEqual(body["summary"]["completed_return_visits"], 0)
            self.assertEqual([row["return_visit_id"] for row in body["return_visits"]], ["conflict"])


if __name__ == "__main__":
    unittest.main()
