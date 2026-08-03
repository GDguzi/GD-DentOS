"""今日候诊队列实时状态：每个患者今天处置了没/病历写了没/欠多少钱。"""
import datetime as dt
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from local_app.api import create_app
from local_app.db import connect, init_db

TODAY = dt.date.today().isoformat()


class QueueStatusTest(unittest.TestCase):
    def _setup(self, tmp):
        db = Path(tmp) / "clinic.sqlite3"
        init_db(db)
        with connect(db) as conn:
            conn.execute("insert into patients(patient_identity, display_name, updated_at, current_hash) "
                         "values ('p1','张三','x','h1')")
            conn.execute("insert into patients(patient_identity, display_name, updated_at, current_hash) "
                         "values ('p2','李四','x','h2')")
            conn.execute("insert into appointments(appointment_id, patient_identity, start_time, doctor_name, item_name, status, updated_at) "
                         f"values ('a1','p1','{TODAY} 09:00','王','复查','0','x')")
            conn.execute("insert into appointments(appointment_id, patient_identity, start_time, doctor_name, item_name, status, updated_at) "
                         f"values ('a2','p2','{TODAY} 10:00','王','洁牙','0','x')")
            # p1: 今天有处置 + 欠费；p2: 啥也没有
            conn.execute("insert into treatment_orders(order_id, patient_identity, order_date, status) "
                         f"values ('o1','p1','{TODAY} 09:30','open')")
            conn.execute("insert into bills(bill_id, patient_identity, bill_no, bill_time, total_fee, paid_fee, state) "
                         f"values ('b1','p1','B1','{TODAY}', 500, 200, 'open')")
            conn.commit()
        return TestClient(create_app(db))

    def test_queue_rows_have_live_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = self._setup(tmp)
            appts = {a["appointment_id"]: a for a in client.get(f"/api/today-work?date={TODAY}").json()["appointments"]}
            self.assertTrue(appts["a1"]["treated_today"])      # p1 今天有处置
            self.assertFalse(appts["a1"]["record_today"])      # 没写病历
            self.assertEqual(appts["a1"]["unpaid_amount"], 300)  # 欠 500-200
            self.assertFalse(appts["a2"]["treated_today"])     # p2 啥也没
            self.assertEqual(appts["a2"]["unpaid_amount"], 0)

    def test_arrival_departure_timestamps(self):
        # 推进状态打时间戳：到诊(1)记 arrived_at、完成(2)记 finished_at
        with tempfile.TemporaryDirectory() as tmp:
            client = self._setup(tmp)
            client.put("/api/appointments/a1", json={"status": "1"})  # 到诊
            a = client.get(f"/api/today-work?date={TODAY}").json()["appointments"]
            a1 = {x["appointment_id"]: x for x in a}["a1"]
            self.assertTrue(a1["arrived_at"])
            self.assertFalse(a1["finished_at"])
            arrived_first = a1["arrived_at"]
            client.put("/api/appointments/a1", json={"status": "2"})  # 完成
            a1b = {x["appointment_id"]: x for x in client.get(f"/api/today-work?date={TODAY}").json()["appointments"]}["a1"]
            self.assertTrue(a1b["finished_at"])
            self.assertEqual(a1b["arrived_at"], arrived_first)  # 到达时刻不被覆盖

    def test_visit_stage_flow_and_revert(self):
        # 就诊状态流(对标官方)：确认→到达→分诊→完成治疗→离开 + 回退清时间戳
        with tempfile.TemporaryDirectory() as tmp:
            client = self._setup(tmp)
            def put(s):
                return client.put("/api/appointments/a1", json={"status": s})
            def a1():
                rows = client.get(f"/api/today-work?date={TODAY}").json()
                return {x["appointment_id"]: x for x in rows["appointments"]}["a1"], rows["summary"]

            put("已确认"); a, s = a1()
            self.assertFalse(a["arrived_at"])               # 确认阶段还没到达
            put("已到诊"); a, s = a1()
            self.assertTrue(a["arrived_at"]); self.assertFalse(a["finished_at"])
            put("已分诊"); a, s = a1()
            self.assertTrue(a["arrived_at"])                 # 分诊仍在"已到诊"桶
            self.assertGreaterEqual(s["appts_arrived"], 1)
            put("已完成"); a, s = a1()
            self.assertTrue(a["finished_at"]); arrived = a["arrived_at"]
            # 回退到已到诊 → 清完成戳、保留到达戳
            put("已到诊"); a, s = a1()
            self.assertFalse(a["finished_at"]); self.assertEqual(a["arrived_at"], arrived)
            # 回退到已确认 → 清到达戳
            put("已确认"); a, s = a1()
            self.assertFalse(a["arrived_at"])

    def test_finish_chinese_status_stamps_finished_at(self):
        # 动态扫#5：前端日视图写 status="完成"(非"已完成"/2)也要打 finished_at,与漏斗口径一致
        with tempfile.TemporaryDirectory() as tmp:
            client = self._setup(tmp)
            client.put("/api/appointments/a1", json={"status": "完成"})
            a1 = {x["appointment_id"]: x for x in client.get(f"/api/today-work?date={TODAY}").json()["appointments"]}["a1"]
            self.assertTrue(a1["finished_at"])

    def test_visit_type_drives_kpi(self):
        # 分诊填的就诊类型(初/复/新诊)驱动顶部 KPI 初/复/新诊
        with tempfile.TemporaryDirectory() as tmp:
            client = self._setup(tmp)  # a1(p1) + a2(p2) 两条今日预约
            client.put("/api/appointments/a1", json={"visit_type": "初诊"})
            client.put("/api/appointments/a2", json={"visit_type": "复诊"})
            s = client.get(f"/api/today-work?date={TODAY}").json()["summary"]
            self.assertEqual(s["first_visits_today"], 1)
            self.assertEqual(s["revisits_today"], 1)
            self.assertEqual(s["new_visits_today"], 0)
            self.assertEqual(s["visits_today"], 2)

    def test_dianzhen_counts_as_new_visit_kpi(self):
        # #452：SaaS 同步来的「点诊」= 本地「新诊」,须并入 new_visits 桶,不能漏统计。
        with tempfile.TemporaryDirectory() as tmp:
            client = self._setup(tmp)
            client.put("/api/appointments/a1", json={"visit_type": "点诊"})   # SaaS 术语
            client.put("/api/appointments/a2", json={"visit_type": "新诊"})   # 本地术语
            s = client.get(f"/api/today-work?date={TODAY}").json()["summary"]
            self.assertEqual(s["new_visits_today"], 2, "点诊应与新诊同桶")
            self.assertEqual(s["first_visits_today"], 0)
            self.assertEqual(s["revisits_today"], 0)

    def test_room_assignment(self):
        # 候诊队列分诊室：PUT 预约 room → today-work 返回该诊室
        with tempfile.TemporaryDirectory() as tmp:
            client = self._setup(tmp)
            self.assertEqual(client.put("/api/appointments/a1", json={"room": "种植室"}).status_code, 200)
            a = {x["appointment_id"]: x for x in client.get(f"/api/today-work?date={TODAY}").json()["appointments"]}["a1"]
            self.assertEqual(a["room"], "种植室")


    def test_cancelled_excluded_from_today_kpi(self):
        # 审查#21：今日就诊KPI(总数+初复新)排除已取消/爽约,口径一致
        with tempfile.TemporaryDirectory() as tmp:
            client = self._setup(tmp)
            client.put("/api/appointments/a2", json={"status": "已取消", "cancel_reason": "x"})
            s = client.get(f"/api/today-work?date={TODAY}").json()["summary"]
            self.assertEqual(s["appointments_today"], 1)   # a2取消,只剩a1
            self.assertEqual(s["visits_today"], 1)

    def test_voided_treatment_and_bill_excluded(self):
        # #97 已撤销处置/作废账单不算"已处置/欠费"
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "clinic.sqlite3"
            init_db(db)
            with connect(db) as conn:
                conn.execute("insert into patients(patient_identity, display_name, updated_at, current_hash) values ('p1','张三','x','h1')")
                conn.execute("insert into appointments(appointment_id, patient_identity, start_time, doctor_name, item_name, status, updated_at) "
                             f"values ('a1','p1','{TODAY} 09:00','王','复查','0','x')")
                conn.execute("insert into treatment_orders(order_id, patient_identity, order_date, status) "
                             f"values ('o1','p1','{TODAY} 09:30','voided')")  # 已撤销
                conn.execute("insert into bills(bill_id, patient_identity, bill_no, bill_time, total_fee, paid_fee, state) "
                             f"values ('b1','p1','B1','{TODAY}', 500, 0, 'voided')")  # 已作废
                conn.commit()
            client = TestClient(create_app(db))
            body = client.get(f"/api/today-work?date={TODAY}").json()
            a = {x["appointment_id"]: x for x in body["appointments"]}["a1"]
            self.assertFalse(a["treated_today"])     # 撤销的处置不算
            self.assertEqual(a["unpaid_amount"], 0)  # 作废的账单不算欠费
            # #99 首页汇总/列表也排除作废账单
            self.assertEqual(body["summary"]["unpaid_bills"], 0)
            self.assertEqual(body["summary"]["today_unpaid_amount"], 0)
            self.assertEqual(len(body["unpaid_bills"]), 0)


if __name__ == "__main__":
    unittest.main()


class TodayWorkLimitTest(unittest.TestCase):
    def test_today_work_returns_more_than_20_appointments(self):
        # #121：今日预约超 20 条要全返回(候诊队列/患者今日tab 不能只显示前 20)
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "clinic.sqlite3"
            init_db(db)
            with connect(db) as conn:
                for i in range(25):
                    pid = f"p{i}"
                    conn.execute("insert into patients(patient_identity, display_name, updated_at, current_hash) "
                                 "values (?,?,'x',?)", (pid, f"患者{i}", "h" + str(i)))
                    conn.execute("insert into appointments(appointment_id, patient_identity, start_time, doctor_name, item_name, status, updated_at) "
                                 "values (?,?,?,'王','洁牙','0','x')", (f"a{i}", pid, f"{TODAY} {8 + i % 10:02d}:{i % 60:02d}"))
                conn.commit()
            client = TestClient(create_app(db))
            data = client.get(f"/api/today-work?date={TODAY}").json()
            self.assertEqual(data["summary"]["appointments_today"], 25)
            self.assertEqual(len(data["appointments"]), 25)


class CancelTimestampTest(unittest.TestCase):
    def _setup(self, tmp):
        db = Path(tmp) / "clinic.sqlite3"
        init_db(db)
        with connect(db) as conn:
            conn.execute("insert into patients(patient_identity, display_name, updated_at, current_hash) values ('p1','张','x','h1')")
            conn.execute("insert into appointments(appointment_id, patient_identity, start_time, doctor_name, item_name, status, updated_at) "
                         f"values ('a1','p1','{TODAY} 09:00','王','洁牙','0','x')")
            conn.commit()
        return db, TestClient(create_app(db))

    def test_cancel_clears_arrival_timestamp(self):
        # 审查#22：取消已到达的预约要清 arrived_at,不留"已取消却有到达时间"的矛盾记录
        with tempfile.TemporaryDirectory() as tmp:
            db, client = self._setup(tmp)
            client.put("/api/appointments/a1", json={"status": "已到诊"})  # arrived_at 打戳
            a = {x["appointment_id"]: x for x in client.get(f"/api/today-work?date={TODAY}").json()["appointments"]}["a1"]
            self.assertTrue(a["arrived_at"])
            client.put("/api/appointments/a1", json={"status": "已取消", "cancel_reason": "改约"})
            # #418:取消后预约从今日工作台隐藏(展示层),清戳是数据层校验 → 直接读库核对
            with connect(db) as conn:
                arrived, finished = conn.execute(
                    "select coalesce(arrived_at, ''), coalesce(finished_at, '') "
                    "from appointments where appointment_id = 'a1'"
                ).fetchone()
            self.assertEqual(arrived, "")
            self.assertEqual(finished, "")

    def test_version_snapshot_includes_timestamps(self):
        # 审查#7：版本快照要含 arrived_at/room/visit_type 等状态机字段
        with tempfile.TemporaryDirectory() as tmp:
            db, client = self._setup(tmp)
            client.put("/api/appointments/a1", json={"status": "已到诊", "room": "诊室2"})
            with connect(db) as conn:
                snaps = [r[0] for r in conn.execute("select snapshot_json from appointment_versions where appointment_id='a1'")]
            self.assertTrue(any("arrived_at" in s and "room" in s and "visit_type" in s for s in snaps))


class ApptNoopTest(unittest.TestCase):
    def test_noop_update_does_not_insert_version(self):
        # 审查#20：相同值再 PUT 不灌冗余版本(no-op 守卫)
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "clinic.sqlite3"
            init_db(db)
            with connect(db) as conn:
                conn.execute("insert into patients(patient_identity, display_name, updated_at, current_hash) values ('p1','张','x','h1')")
                conn.execute("insert into appointments(appointment_id, patient_identity, start_time, doctor_name, item_name, status, updated_at) "
                             f"values ('a1','p1','{TODAY} 09:00','王','洁牙','已确认','x')")
                conn.commit()
            client = TestClient(create_app(db))

            def vcount():
                with connect(db) as conn:
                    return conn.execute("select count(*) from appointment_versions where appointment_id='a1'").fetchone()[0]

            client.put("/api/appointments/a1", json={"doctor_name": "李"})   # 真改 → 1版本
            self.assertEqual(vcount(), 1)
            client.put("/api/appointments/a1", json={"doctor_name": "李"})   # 相同值 → no-op,版本不增
            self.assertEqual(vcount(), 1)
