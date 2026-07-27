import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from local_app.api import create_app
from local_app.db import connect, init_db


class ReturnVisitDoctorSemanticsTest(unittest.TestCase):
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
            conn.execute(
                "insert into return_visits(return_visit_id,patient_identity,due_time,item_name,status,"
                "return_doctor,visitor,revision) values('rv1','p1','2026-06-10','事项甲','待回访',"
                "'回访医生同名','回访人甲',2)"
            )
            conn.execute(
                "insert into return_visits(return_visit_id,patient_identity,due_time,item_name,status,"
                "return_doctor,visitor,revision) values('rv2','p2','2026-06-11','事项乙','待回访',"
                "'回访医生同名','回访人乙',5)"
            )
            conn.commit()
        return TestClient(create_app(db))

    def test_three_people_fields_never_fall_back_to_each_other(self):
        with tempfile.TemporaryDirectory() as tmp:
            body = self._client(tmp).get(
                "/api/return-visits?date_from=2026-06-01&date_to=2026-06-30&status=all"
            ).json()
            row = {r["return_visit_id"]: r for r in body["list"]}["rv1"]
            self.assertEqual(row["responsible_doctor"], "主治甲")
            self.assertEqual(row["return_doctor"], "回访医生同名")
            self.assertEqual(row["visitor"], "回访人甲")

    def test_doctor_filter_only_matches_responsible_doctor(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = self._client(tmp)
            base = "/api/return-visits?date_from=2026-06-01&date_to=2026-06-30&status=all"
            got = client.get(base + "&doctor=主治甲").json()
            self.assertEqual([r["return_visit_id"] for r in got["list"]], ["rv1"])
            self.assertEqual(client.get(base + "&doctor=回访医生同名").json()["totalcount"], 0)

    def test_detail_returns_all_three_people_and_revision(self):
        with tempfile.TemporaryDirectory() as tmp:
            body = self._client(tmp).get("/api/return-visits/rv1").json()
            self.assertEqual(
                (body["responsible_doctor"], body["return_doctor"], body["visitor"], body["revision"]),
                ("主治甲", "回访医生同名", "回访人甲", 2),
            )


if __name__ == "__main__":
    unittest.main()
