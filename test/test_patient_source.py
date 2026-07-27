"""Phase A：患者来源(referral_source/2/3) + 本地自建病历号(chart_no, YYMMDD+流水)。"""
import datetime as dt
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from local_app.api import create_app
from local_app.db import connect, init_db


def _client(tmp):
    db = Path(tmp) / "clinic.sqlite3"
    init_db(db)
    return db, TestClient(create_app(db))


def _create(client, name, phone, **extra):
    payload = {"display_name": name, "phone": phone}
    payload.update(extra)
    r = client.post("/api/patients", json=payload)
    assert r.status_code == 200, r.text
    return r.json()["patient_identity"]


class PatientSourceTest(unittest.TestCase):
    def test_create_stores_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            db, client = _client(tmp)
            pid = _create(client, "张三", "13800000000",
                          referral_source="朋友介绍", referral_source2="门诊", referral_source3="碧桂园")
            with connect(db) as c:
                row = c.execute("select referral_source, referral_source2, referral_source3 from patients where patient_identity=?", (pid,)).fetchone()
            self.assertEqual(row["referral_source"], "朋友介绍")
            self.assertEqual(row["referral_source2"], "门诊")
            self.assertEqual(row["referral_source3"], "碧桂园")

    def test_chart_no_format_and_sequence(self):
        with tempfile.TemporaryDirectory() as tmp:
            db, client = _client(tmp)
            p1 = _create(client, "甲", "13800000001")
            p2 = _create(client, "乙", "13800000002")
            with connect(db) as c:
                c1 = c.execute("select chart_no from patients where patient_identity=?", (p1,)).fetchone()[0]
                c2 = c.execute("select chart_no from patients where patient_identity=?", (p2,)).fetchone()[0]
            prefix = dt.date.today().strftime("%y%m%d")
            self.assertTrue(c1.startswith(prefix), c1)
            self.assertEqual(len(c1), 9)            # YYMMDD(6)+3位流水
            self.assertEqual(c1, prefix + "001")
            self.assertEqual(c2, prefix + "002")     # 流水递增

    def test_chart_no_continues_past_imported(self):
        # 同步来的 外部患者当天已占 005 → 本地新建续到 006(不撞号)
        with tempfile.TemporaryDirectory() as tmp:
            db, client = _client(tmp)
            prefix = dt.date.today().strftime("%y%m%d")
            with connect(db) as c:
                c.execute("insert into patients(patient_identity, display_name, chart_no, updated_at, current_hash) "
                          "values ('imported-x','同步患者',?, '2026-01-01','h')", (prefix + "005",))
                c.commit()
            p = _create(client, "本地新", "13800000003")
            with connect(db) as c:
                cn = c.execute("select chart_no from patients where patient_identity=?", (p,)).fetchone()[0]
            self.assertEqual(cn, prefix + "006")

    def test_chart_no_avoids_imported_chart_no(self):
        # 同步患者的 外部系统 病历号在 source_json.patientid(非 chart_no 列)→本地新建也要避开
        with tempfile.TemporaryDirectory() as tmp:
            db, client = _client(tmp)
            prefix = dt.date.today().strftime("%y%m%d")
            with connect(db) as c:
                c.execute("insert into patients(patient_identity, display_name, source_json, updated_at, current_hash) "
                          "values ('imported-y','同步', ?, '2026-01-01','h')",
                          ('{"patientid": "%s007"}' % prefix,))
                c.commit()
            p = _create(client, "本地", "13800000099")
            with connect(db) as c:
                cn = c.execute("select chart_no from patients where patient_identity=?", (p,)).fetchone()[0]
            self.assertEqual(cn, prefix + "008")   # 续在 外部系统 的 007 之后

    def test_chart_no_unique_index_blocks_dup(self):
        with tempfile.TemporaryDirectory() as tmp:
            db, _ = _client(tmp)
            with connect(db) as c:
                c.execute("insert into patients(patient_identity, display_name, chart_no, updated_at, current_hash) "
                          "values ('a','甲','260101001','x','h')")
                c.commit()
                with self.assertRaises(Exception):
                    c.execute("insert into patients(patient_identity, display_name, chart_no, updated_at, current_hash) "
                              "values ('b','乙','260101001','x','h')")
                    c.commit()

    def test_update_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            db, client = _client(tmp)
            pid = _create(client, "丙", "13800000004")
            r = client.put(f"/api/patients/{pid}", json={"referral_source": "网络咨询"})
            self.assertEqual(r.status_code, 200, r.text)
            with connect(db) as c:
                v = c.execute("select referral_source from patients where patient_identity=?", (pid,)).fetchone()[0]
            self.assertEqual(v, "网络咨询")

    def test_detail_returns_source_and_chart(self):
        with tempfile.TemporaryDirectory() as tmp:
            db, client = _client(tmp)
            pid = _create(client, "丁", "13800000005", referral_source="老患者介绍")
            d = client.get(f"/api/patients/{pid}").json()
            self.assertEqual(d["patient"]["referral_source"], "老患者介绍")
            self.assertTrue(d["patient"]["chart_no"])


if __name__ == "__main__":
    unittest.main()
