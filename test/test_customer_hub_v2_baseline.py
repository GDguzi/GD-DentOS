import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from local_app.api import create_app
from local_app.db import connect, init_db


DAY = "2026-07-10"


class CustomerHubV2BaselineTest(unittest.TestCase):
    def _client(self, tmp):
        db = Path(tmp) / "clinic.sqlite3"
        init_db(db)
        with connect(db) as conn:
            conn.execute(
                "insert into patients(patient_identity,display_name,phone,responsible_doctor,updated_at,current_hash) "
                "values('p1','患者甲','13000000001','主治甲','x','h1')"
            )
            conn.execute(
                "insert into return_visits(return_visit_id,patient_identity,due_time,item_name,status,"
                "return_doctor,visitor,channel,return_result,revision) "
                "values('pending','p1',?,'待办','待回访','回访医生甲','回访人甲','phone','',3)",
                (f"{DAY} 09:00",),
            )
            conn.execute(
                "insert into return_visits(return_visit_id,patient_identity,due_time,actual_date,item_name,status,"
                "return_doctor,visitor,channel,return_result,revision) "
                "values('done','p1','2026-07-01 09:00',?,'完成','已回访','回访医生乙','回访人乙',"
                "'wechat','结果甲',4)",
                (DAY,),
            )
            conn.execute(
                "insert into patients(patient_identity,display_name,is_deleted,updated_at,current_hash) "
                "values('pdel','停用患者',1,'x','hd')"
            )
            conn.execute(
                "insert into return_visits(return_visit_id,patient_identity,due_time,item_name,status) "
                "values('hidden','pdel',?,'不可见','待回访')",
                (f"{DAY} 08:00",),
            )
            conn.commit()
        return TestClient(create_app(db, images_dir=Path(tmp) / "images"))

    def test_today_uses_unified_rows_and_excludes_deleted_patient(self):
        with tempfile.TemporaryDirectory() as tmp:
            body = self._client(tmp).get(f"/api/customer-hub/today?date={DAY}").json()
            rv = body["return_visits"]
            self.assertEqual(rv["pending_count"], 1)
            self.assertEqual(rv["done_count"], 1)
            self.assertEqual([r["return_visit_id"] for r in rv["pending"]], ["pending"])
            self.assertEqual([r["return_visit_id"] for r in rv["done"]], ["done"])

    def test_today_rows_include_read_model_fields_and_revision(self):
        with tempfile.TemporaryDirectory() as tmp:
            rv = self._client(tmp).get(f"/api/customer-hub/today?date={DAY}").json()["return_visits"]
            for row in rv["pending"] + rv["done"]:
                self.assertLessEqual(
                    {"responsible_doctor", "return_doctor", "visitor", "channel", "return_result", "revision"},
                    set(row),
                )


if __name__ == "__main__":
    unittest.main()
