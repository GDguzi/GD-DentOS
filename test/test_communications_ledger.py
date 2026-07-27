import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from local_app.api import create_app
from local_app.db import connect, init_db
from customer_hub_access_fixture import CustomerHubAccessFixture


class CommunicationsLedgerTest(unittest.TestCase):
    def _seed(self, tmp):
        db = Path(tmp) / "clinic.sqlite3"
        init_db(db)
        with connect(db) as conn:
            conn.execute("insert into patients(patient_identity,display_name,updated_at,current_hash) values('p1','患者甲','x','h1')")
            conn.execute("insert into patients(patient_identity,display_name,updated_at,current_hash) values('p2','患者乙','x','h2')")
            conn.execute("insert into patients(patient_identity,display_name,is_deleted,updated_at,current_hash) values('pd','停用患者',1,'x','hd')")
            rows = [
                ("c0", "p1", "phone", "inbound", "咨询", "人员甲", "2026-07-15 09:00", "内容甲"),
                ("c1", "p2", "wechat", "", "提醒", "人员乙", "2026-07-16 09:00", "内容乙"),
                ("c2", "p1", "phone", "outbound", "复诊", "人员甲", "2026-07-17 09:00", "内容丙"),
                ("same-b", "p1", "phone", "inbound", "同刻", "人员甲", "2026-07-18 09:00", "稳定乙"),
                ("same-a", "p1", "phone", "inbound", "同刻", "人员甲", "2026-07-18 09:00", "稳定甲"),
                ("hidden", "pd", "phone", "inbound", "隐藏", "人员甲", "2026-07-19 09:00", "不可见"),
            ]
            for row in rows:
                conn.execute(
                    "insert into communications(communication_id,patient_identity,channel,direction,reason,operator,"
                    "contacted_at,content,created_at) values(?,?,?,?,?,?,?,?,?)",
                    (*row, row[6]),
                )
            conn.commit()
        return db

    def test_ledger_desc_paged_with_active_patient_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = TestClient(create_app(self._seed(tmp), images_dir=Path(tmp) / "images"))
            page1 = client.get("/api/communications?pagesize=3&pageno=1").json()
            self.assertEqual(page1["totalcount"], 5)
            self.assertEqual([r["communication_id"] for r in page1["list"]], ["same-b", "same-a", "c2"])
            self.assertEqual(page1["list"][0]["display_name"], "患者甲")
            page2 = client.get("/api/communications?pagesize=3&pageno=2").json()
            self.assertEqual([r["communication_id"] for r in page2["list"]], ["c1", "c0"])

    def test_ledger_supports_spec_filters(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = TestClient(create_app(self._seed(tmp)))
            cases = (
                ("channel=wechat", {"c1"}), ("direction=outbound", {"c2"}),
                ("reason=提醒", {"c1"}), ("operator=人员乙", {"c1"}),
                ("q=内容乙", {"c1"}), ("date_from=2026-07-16&date_to=2026-07-16", {"c1"}),
            )
            for qs, expected in cases:
                rows = client.get(f"/api/communications?{qs}").json()["list"]
                self.assertEqual({r["communication_id"] for r in rows}, expected, qs)

    def test_ledger_rejects_bad_or_reversed_dates(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = TestClient(create_app(self._seed(tmp)))
            for qs in ("date_from=2026-99-01", "date_to=bad", "date_from=2026-07-20&date_to=2026-07-01"):
                self.assertEqual(client.get(f"/api/communications?{qs}").status_code, 400, qs)

    def test_ledger_policy_requires_patient_and_communication_view(self):
        from local_app.access_policy import ROUTE_POLICY_BY_KEY

        policy = ROUTE_POLICY_BY_KEY[("GET", "/api/communications")]
        self.assertEqual(policy.policy_type, "ALL_OF")
        self.assertEqual(policy.permissions, ("patient.profile.view", "communication.view"))

    def test_ledger_all_of_is_enforced_by_access_v3(self):
        fixture = CustomerHubAccessFixture()
        try:
            only_patient = fixture.client_with_permissions(("patient.profile.view",))
            self.assertEqual(only_patient.get("/api/communications").status_code, 403)
            only_communication = fixture.client_with_permissions(("communication.view",))
            self.assertEqual(only_communication.get("/api/communications").status_code, 403)
            both = fixture.client_with_permissions(("patient.profile.view", "communication.view"))
            self.assertEqual(both.get("/api/communications").status_code, 200)
        finally:
            fixture.close()

    def test_patient_communications_keep_created_order_with_stable_id_tiebreak(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "clinic.sqlite3"
            init_db(db)
            with connect(db) as conn:
                conn.execute(
                    "insert into patients(patient_identity,display_name,updated_at,current_hash) "
                    "values('p1','合成患者','x','h1')"
                )
                rows = (
                    ("z-old", "2026-07-10 09:00"),
                    ("a-new", "2026-07-10 10:00"),
                    ("b-new", "2026-07-10 10:00"),
                )
                for cid, created in rows:
                    conn.execute(
                        "insert into communications(communication_id,patient_identity,channel,contacted_at,created_at) "
                        "values(?, 'p1', 'phone', '2026-07-10 12:00', ?)",
                        (cid, created),
                    )
                conn.commit()
            body = TestClient(create_app(db)).get("/api/patients/p1/communications").json()
            self.assertEqual(
                [row["communication_id"] for row in body["communications"]],
                ["b-new", "a-new", "z-old"],
            )


if __name__ == "__main__":
    unittest.main()
