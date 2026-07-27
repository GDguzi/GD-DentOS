import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from local_app.api import create_app
from local_app.db import connect, init_db


RANGE = "date_from=2026-06-01&date_to=2026-06-30&status=all"


class ReturnVisitAdvancedQueryTest(unittest.TestCase):
    def _client(self, tmp):
        db = Path(tmp) / "clinic.sqlite3"
        init_db(db)
        with connect(db) as conn:
            conn.execute(
                "insert into patients(patient_identity,display_name,name_pinyin,chart_no,responsible_doctor,updated_at,current_hash) "
                "values('p1','患者甲','hcj','C001','主治甲','x','h1')"
            )
            conn.execute(
                "insert into return_visits(return_visit_id,patient_identity,due_time,item_name,status,visitor,return_doctor,"
                "channel,return_type,return_result,revision) values('rv1','p1','2026-06-10 09:00','事项甲','已回访',"
                "'回访人甲','回访医生甲','phone','术后','恢复稳定',7)"
            )
            conn.execute(
                "insert into return_visits(return_visit_id,patient_identity,due_time,item_name,status,visitor,return_doctor,"
                "channel,return_type,return_result,revision) values('rv2','p1','2026-06-11 09:00','事项乙','待回访',"
                "'回访人乙','回访医生乙','wechat','复诊提醒','',2)"
            )
            conn.execute(
                "insert into calls(call_id,patient_identity,direction,started_at,created_at,linked_return_visit_id) "
                "values('call1','p1','outbound','2026-06-10 10:00','2026-06-10 10:00','rv1')"
            )
            conn.execute(
                "insert into return_visit_images(image_id,return_visit_id,patient_identity,rel_path,file_name,created_at) "
                "values('img1','rv2','p1','synthetic/a.png','a.png','2026-06-11 10:00')"
            )
            conn.execute(
                "insert into patients(patient_identity,display_name,is_deleted,updated_at,current_hash) "
                "values('pdel','停用患者',1,'x','hd')"
            )
            conn.execute(
                "insert into return_visits(return_visit_id,patient_identity,due_time,item_name,status) "
                "values('hidden','pdel','2026-06-12','隐藏','待回访')"
            )
            conn.commit()
        return TestClient(create_app(db))

    def test_list_returns_people_channel_attachment_and_revision(self):
        with tempfile.TemporaryDirectory() as tmp:
            rows = self._client(tmp).get(f"/api/return-visits?{RANGE}").json()["list"]
            by_id = {r["return_visit_id"]: r for r in rows}
            self.assertEqual(set(by_id), {"rv1", "rv2"})
            row = by_id["rv1"]
            self.assertEqual((row["responsible_doctor"], row["return_doctor"], row["visitor"]),
                             ("主治甲", "回访医生甲", "回访人甲"))
            self.assertEqual((row["channel"], row["return_type"], row["return_result"], row["revision"]),
                             ("phone", "术后", "恢复稳定", 7))
            self.assertTrue(row["has_recording"])
            self.assertFalse(row["has_screenshot"])
            self.assertIn("has_image", row)
            self.assertTrue(by_id["rv2"]["has_screenshot"])

    def test_filters_q_status_doctor_visitor_and_channel(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = self._client(tmp)
            for extra, expected in (
                ("q=稳定&status=all", {"rv1"}), ("q=hcj&status=all", {"rv1", "rv2"}),
                ("status=已回访", {"rv1"}), ("doctor=主治甲&status=all", {"rv1", "rv2"}),
                ("visitor=回访人乙&status=all", {"rv2"}), ("channel=phone&status=all", {"rv1"}),
            ):
                rows = client.get(f"/api/return-visits?date_from=2026-06-01&date_to=2026-06-30&{extra}").json()["list"]
                self.assertEqual({r["return_visit_id"] for r in rows}, expected, extra)

    def test_attachment_filters(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = self._client(tmp)
            for att, expected in (("recording", {"rv1"}), ("screenshot", {"rv2"})):
                rows = client.get(f"/api/return-visits?{RANGE}&att={att}").json()["list"]
                self.assertEqual({r["return_visit_id"] for r in rows}, expected)
            self.assertEqual(client.get(f"/api/return-visits?{RANGE}&att=video").status_code, 400)


if __name__ == "__main__":
    unittest.main()
