"""预约模块后端补全：新建/取消(原因)/区间查询/统计聚合。"""
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from local_app.api import create_app
from local_app.db import connect, init_db


class AppointmentApiTest(unittest.TestCase):
    def _client(self, tmp):
        db = Path(tmp) / "clinic.sqlite3"
        init_db(db)
        with connect(db) as conn:
            conn.execute("insert into patients(patient_identity, display_name, updated_at, current_hash) "
                         "values ('p1','张三','2026-06-07','h1')")
            conn.execute("insert into patients(patient_identity, display_name, updated_at, current_hash) "
                         "values ('p2','李四','2026-06-07','h2')")
            conn.commit()
        return db, TestClient(create_app(db))

    def test_create_appointment(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, client = self._client(tmp)
            r = client.post("/api/appointments", json={
                "patient_identity": "p1", "start_time": "2026-06-20 09:00",
                "doctor_name": "王医生", "item_name": "种植复诊"})
            self.assertEqual(r.status_code, 200)
            appt = r.json()["appointment"]
            self.assertTrue(appt["appointment_id"])
            self.assertEqual(appt["status"], "已预约")
            self.assertEqual(appt["display_name"], "张三")
            # 出现在列表里
            lst = client.get("/api/appointments?date=2026-06-20").json()["list"]
            self.assertEqual(len(lst), 1)

    def test_walkin_register_enters_queue_arrived(self):
        # #345 到店登记=walk-in,人已在场→直接落「已到诊」+到达时刻,进候诊队列「到达」阶段
        with tempfile.TemporaryDirectory() as tmp:
            db, client = self._client(tmp)
            r = client.post("/api/appointments", json={
                "patient_identity": "p1", "start_time": "2026-06-20 09:00",
                "doctor_name": "王医生", "item_name": "急诊", "register_type": "到店"})
            self.assertEqual(r.status_code, 200)
            aid = r.json()["appointment"]["appointment_id"]
            with connect(db) as conn:
                row = conn.execute(
                    "select status, register_type, arrived_at from appointments where appointment_id=?",
                    (aid,)).fetchone()
            self.assertEqual(row["status"], "已到诊")
            self.assertEqual(row["register_type"], "到店")
            self.assertTrue(row["arrived_at"], "到店登记应写到达时刻")

    def test_normal_appointment_not_auto_arrived(self):
        # 普通预约(register_type 默认/预约)不被自动标到达,保持已预约
        with tempfile.TemporaryDirectory() as tmp:
            db, client = self._client(tmp)
            r = client.post("/api/appointments", json={
                "patient_identity": "p1", "start_time": "2026-06-20 09:00",
                "doctor_name": "王医生", "item_name": "复诊"})
            aid = r.json()["appointment"]["appointment_id"]
            with connect(db) as conn:
                row = conn.execute(
                    "select status, arrived_at from appointments where appointment_id=?",
                    (aid,)).fetchone()
            self.assertEqual(row["status"], "已预约")
            self.assertFalse(row["arrived_at"])

    def test_create_requires_patient_and_time(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, client = self._client(tmp)
            self.assertEqual(client.post("/api/appointments", json={"start_time": "2026-06-20 09:00"}).status_code, 400)
            self.assertEqual(client.post("/api/appointments", json={"patient_identity": "p1"}).status_code, 400)
            # 医生/项目必填
            self.assertEqual(client.post("/api/appointments", json={
                "patient_identity": "p1", "start_time": "2026-06-20 09:00", "item_name": "洁牙"}).status_code, 400)
            self.assertEqual(client.post("/api/appointments", json={
                "patient_identity": "p1", "start_time": "2026-06-20 09:00", "doctor_name": "王医生"}).status_code, 400)
            # 患者不存在
            self.assertEqual(client.post("/api/appointments", json={
                "patient_identity": "nope", "start_time": "2026-06-20 09:00",
                "doctor_name": "王医生", "item_name": "洁牙"}).status_code, 404)
            # 非法时间
            self.assertEqual(client.post("/api/appointments", json={
                "patient_identity": "p1", "start_time": "下周一"}).status_code, 400)

    def test_range_and_doctor_status_filters(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, client = self._client(tmp)
            client.post("/api/appointments", json={"patient_identity": "p1", "start_time": "2026-06-20 09:00",
                                                   "doctor_name": "王医生", "item_name": "洁牙", "status": "已预约"})
            client.post("/api/appointments", json={"patient_identity": "p2", "start_time": "2026-06-22 14:00",
                                                   "doctor_name": "李医生", "item_name": "拔牙", "status": "已到诊"})
            client.post("/api/appointments", json={"patient_identity": "p1", "start_time": "2026-06-25 10:00",
                                                   "doctor_name": "王医生", "item_name": "复诊", "status": "已预约"})
            # 区间 6-20~6-23 → 2 条
            rng = client.get("/api/appointments?date_from=2026-06-20&date_to=2026-06-23").json()
            self.assertEqual(rng["totalcount"], 2)
            # 医生过滤
            wang = client.get("/api/appointments?doctor_name=王医生").json()
            self.assertEqual(wang["totalcount"], 2)
            # 状态过滤
            arrived = client.get("/api/appointments?status=已到诊").json()
            self.assertEqual(arrived["totalcount"], 1)

    def test_summary_counts(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, client = self._client(tmp)
            client.post("/api/appointments", json={"patient_identity": "p1", "start_time": "2026-06-20 09:00",
                                                   "doctor_name": "王医生", "item_name": "洁牙", "status": "已预约"})
            client.post("/api/appointments", json={"patient_identity": "p2", "start_time": "2026-06-20 14:00",
                                                   "doctor_name": "王医生", "item_name": "拔牙", "status": "已到诊"})
            s = client.get("/api/appointments/summary?date=2026-06-20").json()
            self.assertEqual({c["key"] for c in s["by_status"]}, {"已预约", "已到诊"})
            self.assertEqual({c["key"] for c in s["by_doctor"]}, {"王医生"})
            doctor_total = sum(c["count"] for c in s["by_doctor"])
            self.assertEqual(doctor_total, 2)
            # 区间外不计入
            s2 = client.get("/api/appointments/summary?date=2026-06-21").json()
            self.assertEqual(sum(c["count"] for c in s2["by_status"]), 0)

    def test_delete_blocked_when_treated_or_paid_same_day(self):
        # 守卫:该患者当天有处置/收费 → 删预约 409(防对不上号);干净预约可删
        with tempfile.TemporaryDirectory() as tmp:
            db, client = self._client(tmp)
            mk = lambda: client.post("/api/appointments", json={
                "patient_identity": "p1", "start_time": "2026-06-20 09:00", "force": True,
                "doctor_name": "王医生", "item_name": "洁牙"}).json()["appointment"]["appointment_id"]
            # 1) 干净预约可删
            self.assertEqual(client.delete(f"/api/appointments/{mk()}").status_code, 200)
            # 2) 当天有处置 → 拒删 409
            aid = mk()
            with connect(db) as conn:
                conn.execute("insert into treatment_orders(order_id, patient_identity, order_date, status) "
                             "values ('o1','p1','2026-06-20','open')")
                conn.commit()
            self.assertEqual(client.delete(f"/api/appointments/{aid}").status_code, 409)
            # 3) 当天有收费 → 拒删 409
            aid2 = mk()
            with connect(db) as conn:
                conn.execute("insert into bills(bill_id, patient_identity, bill_no, bill_time, total_fee, paid_fee, state) "
                             "values ('b1','p1','B1','2026-06-20',100,100,'1')")
                conn.execute("insert into payments(payment_id, bill_id, patient_identity, amount, pay_time, state) "
                             "values ('pay1','b1','p1',100,'2026-06-20 10:00','0')")
                conn.commit()
            self.assertEqual(client.delete(f"/api/appointments/{aid2}").status_code, 409)

    def test_cancel_with_reason(self):
        with tempfile.TemporaryDirectory() as tmp:
            db, client = self._client(tmp)
            aid = client.post("/api/appointments", json={
                "patient_identity": "p1", "start_time": "2026-06-20 09:00",
                "doctor_name": "王医生", "item_name": "种植复诊"}).json()["appointment"]["appointment_id"]
            # 取消不带原因 → 400（中文"已取消"和数字"3"都要拦，#100）
            self.assertEqual(client.put(f"/api/appointments/{aid}", json={"status": "已取消"}).status_code, 400)
            self.assertEqual(client.put(f"/api/appointments/{aid}", json={"status": "3"}).status_code, 400)
            self.assertEqual(client.put(f"/api/appointments/{aid}", json={"status": "3", "cancel_reason": "号约满了"}).status_code, 200)
            r = client.put(f"/api/appointments/{aid}", json={"status": "已取消", "cancel_reason": "患者临时有事"})
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r.json()["appointment"]["status"], "已取消")
            with connect(db) as conn:
                row = conn.execute("select status, cancel_reason from appointments where appointment_id=?",
                                   (aid,)).fetchone()
            self.assertEqual(row["status"], "已取消")
            self.assertEqual(row["cancel_reason"], "患者临时有事")

    def test_create_with_visit_type_and_extras(self):
        # 预约用例③：创建带就诊类型 + 待确定/咨询师/患者来源(落 source_json)
        import json as _json
        with tempfile.TemporaryDirectory() as tmp:
            db, client = self._client(tmp)
            r = client.post("/api/appointments", json={
                "patient_identity": "p1", "start_time": "2026-06-20 09:00",
                "doctor_name": "王医生", "item_name": "检查、洁牙",
                "visit_type": "初诊", "appointment_type": "待确定预约",
                "consultant": "小李", "patient_source": "老带新"})
            self.assertEqual(r.status_code, 200)
            aid = r.json()["appointment"]["appointment_id"]
            with connect(db) as conn:
                row = conn.execute(
                    "select visit_type, source_json from appointments where appointment_id=?", (aid,)
                ).fetchone()
            self.assertEqual(row["visit_type"], "初诊")
            sj = _json.loads(row["source_json"])
            self.assertEqual(sj["appointment_type"], "待确定预约")
            self.assertEqual(sj["consultant"], "小李")
            self.assertEqual(sj["patient_source"], "老带新")

    def test_get_single_appointment_detail(self):
        # #275 双击改约：GET /api/appointments/{id} 回填用，含 source_json 内嵌字段
        with tempfile.TemporaryDirectory() as tmp:
            _, client = self._client(tmp)
            aid = client.post("/api/appointments", json={
                "patient_identity": "p1", "start_time": "2026-06-20 09:00",
                "doctor_name": "王医生", "item_name": "检查、洁牙",
                "visit_type": "初诊", "register_type": "预约",
                "appointment_type": "待确定预约", "consultant": "小李",
                "patient_source": "老带新", "booked_by": "前台A", "remark": "怕疼"}).json()["appointment"]["appointment_id"]
            d = client.get(f"/api/appointments/{aid}")
            self.assertEqual(d.status_code, 200)
            a = d.json()["appointment"]
            self.assertEqual(a["display_name"], "张三")
            self.assertEqual(a["item_name"], "检查、洁牙")
            self.assertEqual(a["visit_type"], "初诊")
            self.assertEqual(a["register_type"], "预约")
            self.assertEqual(a["appointment_type"], "待确定预约")
            self.assertEqual(a["consultant"], "小李")
            self.assertEqual(a["patient_source"], "老带新")
            self.assertEqual(a["booked_by"], "前台A")
            self.assertEqual(a["remark"], "怕疼")
            # 不存在 → 404
            self.assertEqual(client.get("/api/appointments/nope").status_code, 404)

    def test_edit_appointment_roundtrip(self):
        # #275：PUT 既能改时段/医生/项目，也能改 source_json 内嵌字段(咨询师/来源/预约人/备注)
        import json as _json
        with tempfile.TemporaryDirectory() as tmp:
            db, client = self._client(tmp)
            aid = client.post("/api/appointments", json={
                "patient_identity": "p1", "start_time": "2026-06-20 09:00",
                "doctor_name": "王医生", "item_name": "洁牙",
                "visit_type": "初诊", "consultant": "小李", "remark": "怕疼"}).json()["appointment"]["appointment_id"]
            r = client.put(f"/api/appointments/{aid}", json={
                "start_time": "2026-06-21 10:30", "end_time": "2026-06-21 11:30",
                "doctor_name": "李医生", "item_name": "洁牙、复查",
                "visit_type": "复诊", "register_type": "到店",
                "consultant": "小王", "patient_source": "百度", "booked_by": "前台B", "remark": ""})
            self.assertEqual(r.status_code, 200)
            with connect(db) as conn:
                row = conn.execute(
                    "select start_time, doctor_name, item_name, visit_type, register_type, source_json "
                    "from appointments where appointment_id=?", (aid,)).fetchone()
            self.assertEqual(row["start_time"], "2026-06-21 10:30")
            self.assertEqual(row["doctor_name"], "李医生")
            self.assertEqual(row["item_name"], "洁牙、复查")
            self.assertEqual(row["visit_type"], "复诊")
            self.assertEqual(row["register_type"], "到店")
            sj = _json.loads(row["source_json"])
            self.assertEqual(sj["consultant"], "小王")        # 改了
            self.assertEqual(sj["patient_source"], "百度")    # 新增
            self.assertEqual(sj["booked_by"], "前台B")
            self.assertNotIn("remark", sj)                    # 空串=清除该键
            # 回读 GET 详情一致
            a = client.get(f"/api/appointments/{aid}").json()["appointment"]
            self.assertEqual(a["doctor_name"], "李医生")
            self.assertEqual(a["consultant"], "小王")

    def test_summary_rejects_bad_date(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, client = self._client(tmp)
            self.assertEqual(client.get("/api/appointments/summary?date=明天").status_code, 400)
            self.assertEqual(client.get("/api/appointments?date_from=20260620").status_code, 400)


if __name__ == "__main__":
    unittest.main()
