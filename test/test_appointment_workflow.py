"""P1-2 爽约标记 + P1-3 预约时段冲突提示(不硬阻断,force 二次确认)。"""
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from local_app.api import create_app
from local_app.models import appt_stage as _appt_stage
from local_app.db import connect, init_db


def _client(tmp):
    db = Path(tmp) / "clinic.sqlite3"
    init_db(db)
    c = TestClient(create_app(db))
    c.post("/api/patients", json={"display_name": "约测试", "phone": "13800000007"})
    with connect(db) as conn:
        pid = conn.execute("select patient_identity from patients limit 1").fetchone()[0]
    return db, c, pid


def _appt(c, pid, doctor, start, end="", item="洗牙", **extra):
    return c.post("/api/appointments", json={
        "patient_identity": pid, "doctor_name": doctor, "item_name": item,
        "start_time": start, "end_time": end, **extra})


class NoShowTest(unittest.TestCase):
    def test_appt_stage_noshow_excluded(self):
        # 爽约映射到 <0 阶段(与取消同样排除出候诊漏斗)
        self.assertEqual(_appt_stage("已爽约"), -1)
        self.assertTrue(_appt_stage("已爽约") < 0)

    def test_mark_noshow_via_put(self):
        with tempfile.TemporaryDirectory() as tmp:
            db, c, pid = _client(tmp)
            aid = _appt(c, pid, "王医生", "2026-06-20 09:00", "2026-06-20 09:30").json()["appointment"]["appointment_id"]
            r = c.put(f"/api/appointments/{aid}", json={"status": "已爽约"})
            self.assertEqual(r.status_code, 200, r.text)
            with connect(db) as conn:
                st = conn.execute("select status from appointments where appointment_id=?", (aid,)).fetchone()[0]
            self.assertEqual(st, "已爽约")


class DeleteTest(unittest.TestCase):
    def test_delete_removes_appt_and_writes_audit(self):
        # 队列删除：硬删预约 + 审计留 old 快照；不存在返回 404
        with tempfile.TemporaryDirectory() as tmp:
            db, c, pid = _client(tmp)
            aid = _appt(c, pid, "王医生", "2026-06-20 09:00", "2026-06-20 09:30").json()["appointment"]["appointment_id"]
            r = c.delete(f"/api/appointments/{aid}")
            self.assertEqual(r.status_code, 200, r.text)
            with connect(db) as conn:
                gone = conn.execute("select 1 from appointments where appointment_id=?", (aid,)).fetchone()
                audited = conn.execute(
                    "select 1 from audit_logs where entity_type='appointment' and entity_id=? and action='delete_appointment'",
                    (aid,)).fetchone()
            self.assertIsNone(gone)
            self.assertIsNotNone(audited)
            self.assertEqual(c.delete(f"/api/appointments/{aid}").status_code, 404)


class ConflictTest(unittest.TestCase):
    def test_overlap_same_doctor_warns_without_insert(self):
        with tempfile.TemporaryDirectory() as tmp:
            db, c, pid = _client(tmp)
            _appt(c, pid, "王医生", "2026-06-20 09:00", "2026-06-20 09:30")
            r = _appt(c, pid, "王医生", "2026-06-20 09:15", "2026-06-20 09:45", item="补牙")
            self.assertEqual(r.status_code, 200, r.text)
            self.assertTrue(r.json().get("conflict"))
            self.assertIn("warning", r.json())
            with connect(db) as conn:
                n = conn.execute("select count(*) from appointments").fetchone()[0]
            self.assertEqual(n, 1)  # 冲突未插入

    def test_force_inserts_despite_conflict(self):
        with tempfile.TemporaryDirectory() as tmp:
            db, c, pid = _client(tmp)
            _appt(c, pid, "王医生", "2026-06-20 09:00", "2026-06-20 09:30")
            r = _appt(c, pid, "王医生", "2026-06-20 09:15", "2026-06-20 09:45", item="补牙", force=True)
            self.assertEqual(r.status_code, 200, r.text)
            self.assertIn("appointment", r.json())
            with connect(db) as conn:
                n = conn.execute("select count(*) from appointments").fetchone()[0]
            self.assertEqual(n, 2)

    def test_different_doctor_no_conflict(self):
        with tempfile.TemporaryDirectory() as tmp:
            db, c, pid = _client(tmp)
            _appt(c, pid, "王医生", "2026-06-20 09:00", "2026-06-20 09:30")
            r = _appt(c, pid, "李医生", "2026-06-20 09:00", "2026-06-20 09:30", item="补牙")
            self.assertEqual(r.status_code, 200, r.text)
            self.assertIn("appointment", r.json())
            self.assertFalse(r.json().get("conflict"))

    def test_non_overlapping_time_no_conflict(self):
        with tempfile.TemporaryDirectory() as tmp:
            db, c, pid = _client(tmp)
            _appt(c, pid, "王医生", "2026-06-20 09:00", "2026-06-20 09:30")
            r = _appt(c, pid, "王医生", "2026-06-20 10:00", "2026-06-20 10:30", item="补牙")
            self.assertEqual(r.status_code, 200, r.text)
            self.assertIn("appointment", r.json())
            self.assertFalse(r.json().get("conflict"))

    def test_touching_boundary_no_conflict(self):
        # #132 端点相接(09:30 接 09:30)正常连排，不应误判冲突
        with tempfile.TemporaryDirectory() as tmp:
            db, c, pid = _client(tmp)
            _appt(c, pid, "王医生", "2026-06-20 09:00", "2026-06-20 09:30")
            r = _appt(c, pid, "王医生", "2026-06-20 09:30", "2026-06-20 10:00", item="补牙")
            self.assertEqual(r.status_code, 200, r.text)
            self.assertIn("appointment", r.json())
            self.assertFalse(r.json().get("conflict"))

    def test_same_room_different_doctor_conflict(self):
        # #133 同诊室不同医生同时段也冲突
        with tempfile.TemporaryDirectory() as tmp:
            db, c, pid = _client(tmp)
            _appt(c, pid, "王医生", "2026-06-20 09:00", "2026-06-20 09:30", room="1诊室")
            r = _appt(c, pid, "李医生", "2026-06-20 09:15", "2026-06-20 09:45", item="补牙", room="1诊室")
            self.assertTrue(r.json().get("conflict"))
            with connect(db) as conn:
                n = conn.execute("select count(*) from appointments").fetchone()[0]
            self.assertEqual(n, 1)

    def test_room_persisted(self):
        # #133 room 不再被丢弃
        with tempfile.TemporaryDirectory() as tmp:
            db, c, pid = _client(tmp)
            aid = _appt(c, pid, "王医生", "2026-06-20 11:00", "2026-06-20 11:30", room="2诊室").json()["appointment"]["appointment_id"]
            with connect(db) as conn:
                room = conn.execute("select room from appointments where appointment_id=?", (aid,)).fetchone()[0]
            self.assertEqual(room, "2诊室")

    def test_noshow_clears_arrival_timestamp(self):
        # #134 已到诊后标爽约 → 清掉到达时间戳，避免矛盾记录
        with tempfile.TemporaryDirectory() as tmp:
            db, c, pid = _client(tmp)
            aid = _appt(c, pid, "王医生", "2026-06-20 14:00", "2026-06-20 14:30").json()["appointment"]["appointment_id"]
            c.put(f"/api/appointments/{aid}", json={"status": "已到诊"})
            with connect(db) as conn:
                arrived = conn.execute("select arrived_at from appointments where appointment_id=?", (aid,)).fetchone()[0]
            self.assertTrue(arrived)   # 到诊后有时间戳
            c.put(f"/api/appointments/{aid}", json={"status": "已爽约"})
            with connect(db) as conn:
                arrived2 = conn.execute("select arrived_at from appointments where appointment_id=?", (aid,)).fetchone()[0]
            self.assertEqual(arrived2 or "", "")   # 爽约后清空


if __name__ == "__main__":
    unittest.main()
