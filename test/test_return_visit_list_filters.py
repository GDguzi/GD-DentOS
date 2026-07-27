"""回访列表筛选(外部系统化)：date区间/状态/主治医生/回访人 + 默认待回访工作台口径不变。"""
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from local_app.api import create_app
from local_app.db import connect, init_db


class ReturnVisitListFilterTest(unittest.TestCase):
    def _client(self, tmp):
        db = Path(tmp) / "clinic.sqlite3"
        init_db(db)
        with connect(db) as c:
            c.execute("insert into patients(patient_identity,display_name,responsible_doctor,updated_at,current_hash) "
                      "values('p1','张三','主治甲','2026-06-01','h1')")
            c.execute("insert into patients(patient_identity,display_name,responsible_doctor,updated_at,current_hash) "
                      "values('p2','李四','主治乙','2026-06-01','h2')")
            # 待回访(主治甲/回访医生王医生/回访人甲, 到期6-10)
            c.execute("insert into return_visits(return_visit_id,patient_identity,due_time,item_name,status,return_doctor,visitor) "
                      "values('rv1','p1','2026-06-10','术后复查','待回访','王医生','甲')")
            # 已回访(主治乙/回访医生李医生/回访人乙, 到期6-20)
            c.execute("insert into return_visits(return_visit_id,patient_identity,due_time,item_name,status,return_doctor,visitor,actual_date) "
                      "values('rv2','p2','2026-06-20','洁牙回访','已回访','李医生','乙','2026-06-19')")
            c.commit()
        return db, TestClient(create_app(db))

    def test_default_is_pending_workbench(self):
        # 不给参数 → 今日待回访口径(只到期且未回访)。6-10待回访在今天(2026-06-15)之前到期→出现; 已回访不出现
        with tempfile.TemporaryDirectory() as tmp:
            _, client = self._client(tmp)
            lst = client.get("/api/return-visits").json()["list"]
            ids = {r["return_visit_id"] for r in lst}
            self.assertIn("rv1", ids)
            self.assertNotIn("rv2", ids)   # 已回访默认排除

    def test_date_range(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, client = self._client(tmp)
            # 区间含两条, 但默认仍排除已回访 → 只 rv1
            r = client.get("/api/return-visits?date_from=2026-06-01&date_to=2026-06-30").json()
            self.assertEqual({x["return_visit_id"] for x in r["list"]}, {"rv1"})

    def test_status_filter_shows_done(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, client = self._client(tmp)
            r = client.get("/api/return-visits?date_from=2026-06-01&date_to=2026-06-30&status=已回访").json()
            self.assertEqual({x["return_visit_id"] for x in r["list"]}, {"rv2"})
            self.assertEqual(r["list"][0]["return_doctor"], "李医生")
            self.assertEqual(r["list"][0]["visitor"], "乙")

    def test_doctor_and_visitor_filter(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, client = self._client(tmp)
            byDoc = client.get("/api/return-visits?date_from=2026-06-01&date_to=2026-06-30&status=已回访&doctor=主治乙").json()
            self.assertEqual({x["return_visit_id"] for x in byDoc["list"]}, {"rv2"})
            byReturnDoctor = client.get("/api/return-visits?date_from=2026-06-01&date_to=2026-06-30&status=已回访&doctor=李医生").json()
            self.assertEqual(byReturnDoctor["totalcount"], 0)
            none = client.get("/api/return-visits?date_from=2026-06-01&date_to=2026-06-30&doctor=不存在").json()
            self.assertEqual(none["totalcount"], 0)
            byVisitor = client.get("/api/return-visits?date_from=2026-06-01&date_to=2026-06-30&visitor=甲").json()
            self.assertEqual({x["return_visit_id"] for x in byVisitor["list"]}, {"rv1"})

    def test_status_all_includes_done(self):
        # status=all → 待回访+已回访都出
        with tempfile.TemporaryDirectory() as tmp:
            _, client = self._client(tmp)
            r = client.get("/api/return-visits?date_from=2026-06-01&date_to=2026-06-30&status=all").json()
            self.assertEqual({x["return_visit_id"] for x in r["list"]}, {"rv1", "rv2"})

    def test_returns_new_columns(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, client = self._client(tmp)
            row = client.get("/api/return-visits").json()["list"][0]
            for col in ("return_doctor", "visitor", "actual_date"):
                self.assertIn(col, row)


if __name__ == "__main__":
    unittest.main()
