import json
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from local_app.api import create_app
from local_app.db import connect, init_db


def _setup(tmpdir):
    db_path = Path(tmpdir) / "clinic.sqlite3"
    init_db(db_path)
    with connect(db_path) as conn:
        conn.execute(
            "insert into patients(patient_identity, display_name, updated_at, current_hash) "
            "values ('p1', '测试患者', '2026-06-07 00:00:00', 'h1')"
        )
        # 病历：两条正式(06-01)、一条AI草稿(06-09 应排除)、一条空就诊时间(应跳过)
        med = [
            ("r1", "2026-06-01 09:30", "", "初诊", "王医生"),
            ("r2", "2026-06-01 10:00", "", "复诊", "李医生"),
            ("r3", "2026-06-09 09:00", "ai_draft", "", "张医生"),
            ("r4", "", "", "复诊", "王医生"),
        ]
        for rid, vt, ds, rt, doc in med:
            conn.execute(
                "insert into medical_records(record_id, patient_identity, visit_time, "
                "draft_status, record_type, doctor_name, content_json, current_hash, updated_at) "
                "values (?, 'p1', ?, ?, ?, ?, ?, ?, '2026-06-07 00:00:00')",
                (rid, vt, ds, rt, doc, json.dumps({"PC": "x"}), "h-" + rid),
            )
        # 收费：06-01 与 06-09 各一
        conn.execute(
            "insert into bills(bill_id, patient_identity, bill_no, bill_time, total_fee, paid_fee) "
            "values ('b1', 'p1', 'NO1', '2026-06-01 11:00', 200, 200)"
        )
        conn.execute(
            "insert into bills(bill_id, patient_identity, bill_no, bill_time, total_fee, paid_fee) "
            "values ('b2', 'p1', 'NO2', '2026-06-09 15:00', 500, 0)"
        )
        # 预约：06-01
        conn.execute(
            "insert into appointments(appointment_id, patient_identity, start_time, "
            "doctor_name, item_name, status) "
            "values ('a1', 'p1', '2026-06-01 09:00', '王医生', '洁牙', 1)"
        )
        # 回访：06-09
        conn.execute(
            "insert into return_visits(return_visit_id, patient_identity, due_time, item_name, status) "
            "values ('rv1', 'p1', '2026-06-09 00:00', '术后回访', 0)"
        )
        # 处置明细：经 bill 关联日期（t1/t2→b1 06-01；已删 t3 应排除）
        for tid, name, fee, deleted in [
            ("t1", "树脂充填", 200, 0),
            ("t2", "局部麻醉", 0, 0),
            ("t3", "已删项", 50, 1),
        ]:
            conn.execute(
                "insert into treatment_items(treatment_item_id, patient_identity, bill_id, "
                "item_name, doctor_name, quantity, total_fee, is_deleted) "
                "values (?, 'p1', 'b1', ?, '王医生', 1, ?, ?)",
                (tid, name, fee, deleted),
            )
        conn.commit()
    return db_path


class PatientVisitsTimelineTest(unittest.TestCase):
    def test_visits_grouped_by_day_desc(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = _setup(tmpdir)
            client = TestClient(create_app(db_path))
            data = client.get("/api/patients/p1/visits").json()

        visits = data["visits"]
        # 两天：06-09 在前（倒序），06-01 在后；空日期 r4 不产生新天
        self.assertEqual([v["date"] for v in visits], ["2026-06-09", "2026-06-01"])
        self.assertEqual(data["totalcount"], 2)

        d0609 = visits[0]
        d0601 = visits[1]
        # 06-01：2 病历 + 1 收费 + 1 预约 + 0 回访 + 2 处置(已删的排除)
        self.assertEqual(len(d0601["medical_records"]), 2)
        self.assertEqual(len(d0601["bills"]), 1)
        self.assertEqual(len(d0601["appointments"]), 1)
        # #444/#446 就诊行要显示时分:预约对象必须带 start_time(SQL 查了不能在组装时漏放)
        self.assertEqual(d0601["appointments"][0]["start_time"], "2026-06-01 09:00")
        self.assertEqual(len(d0601["return_visits"]), 0)
        self.assertEqual(len(d0601["treatments"]), 2)
        self.assertEqual(
            {t["item_name"] for t in d0601["treatments"]}, {"树脂充填", "局部麻醉"}
        )
        # 06-09：AI草稿病历被排除 → 0 病历；1 收费 + 0 预约 + 1 回访
        self.assertEqual(len(d0609["medical_records"]), 0)
        self.assertEqual(len(d0609["bills"]), 1)
        self.assertEqual(len(d0609["return_visits"]), 1)

    def test_ai_draft_and_empty_dates_excluded(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = _setup(tmpdir)
            client = TestClient(create_app(db_path))
            visits = client.get("/api/patients/p1/visits").json()["visits"]
        all_record_ids = [
            r["record_id"] for v in visits for r in v["medical_records"]
        ]
        self.assertIn("r1", all_record_ids)
        self.assertIn("r2", all_record_ids)
        self.assertNotIn("r3", all_record_ids)  # ai_draft 排除
        self.assertNotIn("r4", all_record_ids)  # 空就诊时间跳过

    def test_unknown_patient_404(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = _setup(tmpdir)
            client = TestClient(create_app(db_path))
            resp = client.get("/api/patients/nope/visits")
        self.assertEqual(resp.status_code, 404)


if __name__ == "__main__":
    unittest.main()
