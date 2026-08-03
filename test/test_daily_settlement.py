"""日结报表明细(对标 SaaS)：每条就诊一行 + 患者信息 + 账单财务。"""
import json
import tempfile
import unittest
from pathlib import Path
from urllib.parse import urlencode

from fastapi.testclient import TestClient

from local_app import auth
from local_app.api import create_app
from local_app.db import connect, init_db


def _client(tmp):
    db = Path(tmp) / "clinic.sqlite3"
    init_db(db)
    with connect(db) as c:
        auth.create_user(c, "admin", "管理员", "admin123", role="admin")
        c.execute("insert into patients(patient_identity, display_name, chart_no, sex, birthday, phone, "
                  "referral_source, referral_source2, consultant, updated_at, current_hash) "
                  "values ('p1','张三','260101001','男','1990-05-01','13800000001','朋友介绍','门诊','小咨','2026-07-01','h1')")
        c.commit()
    client = TestClient(create_app(db))
    client.post("/api/auth/login", json={"username": "admin", "password": "admin123"})
    return db, client


def _visit(c, rid, pid, doctor, vt, day, study=""):
    c.execute("insert into medical_records(record_id, patient_identity, study_identity, record_type, "
              "visit_time, doctor_name, current_hash) values (?,?,?,?,?,?,?)",
              (rid, pid, study, vt, day + " 09:00:00", doctor, "h_" + rid))


def _bill(c, bid, pid, day, total, paid, study="", discount=None, state="paid"):
    sj = {"BillStudyIdentity": study}
    if discount is not None:
        sj["DiscountFee"] = discount
    c.execute("insert into bills(bill_id, patient_identity, bill_time, total_fee, paid_fee, state, "
              "source_json, updated_at) values (?,?,?,?,?,?,?,?)",
              (bid, pid, day + " 09:30:00", total, paid, state, json.dumps(sj), day))


class DailySettlementTest(unittest.TestCase):
    def _q(self, client, **kw):
        return client.get("/api/store-stats/daily-settlement?" + urlencode(
            {"start": "2026-07-01", "end": "2026-07-31", **kw})).json()

    def test_row_with_patient_and_finance(self):
        with tempfile.TemporaryDirectory() as tmp:
            db, client = _client(tmp)
            with connect(db) as c:
                _visit(c, "r1", "p1", "测试医生B", "初诊", "2026-07-02", study="S1")
                _bill(c, "b1", "p1", "2026-07-02", 1000, 700, study="S1", discount="100")
                c.execute("insert into treatment_items(treatment_item_id, bill_id, patient_identity, item_name, total_fee) "
                          "values ('ti1','b1','p1','种植体',1000)")
                c.commit()
            d = self._q(client)
            self.assertEqual(d["count"], 1)
            r = d["rows"][0]
            self.assertEqual(r["chart_no"], "260101001")
            self.assertEqual(r["sex"], "男")
            self.assertEqual(r["age"], "36")           # 1990-05-01 → 2026-07-08 周岁36
            self.assertEqual(r["referral_source"], "朋友介绍/门诊")
            self.assertEqual(r["visit_type"], "初诊")
            self.assertEqual(r["consultant"], "小咨")
            self.assertEqual(r["items"], "种植体")
            self.assertEqual(r["total_fee"], 1000.0)
            self.assertEqual(r["receivable"], 900.0)    # 1000-100
            self.assertEqual(r["paid"], 700.0)
            self.assertEqual(r["discount"], 100.0)
            self.assertEqual(r["arrears"], 200.0)       # 900-700

    def test_totals_and_csv(self):
        with tempfile.TemporaryDirectory() as tmp:
            db, client = _client(tmp)
            with connect(db) as c:
                _visit(c, "r1", "p1", "测试医生B", "初诊", "2026-07-02", study="S1")
                _bill(c, "b1", "p1", "2026-07-02", 500, 500, study="S1")
                _visit(c, "r2", "p1", "测试医生A", "复诊", "2026-07-03", study="S2")
                _bill(c, "b2", "p1", "2026-07-03", 300, 200, study="S2")
                c.commit()
            d = self._q(client)
            self.assertEqual(d["count"], 2)
            self.assertEqual(d["total"]["total_fee"], 800.0)
            self.assertEqual(d["total"]["paid"], 700.0)
            csv = client.get("/api/store-stats/daily-settlement.csv?start=2026-07-01&end=2026-07-31")
            self.assertEqual(csv.status_code, 200)
            text = csv.content.decode("utf-8-sig")
            self.assertIn("就诊日期", text)
            self.assertIn("初诊", text)

    def test_same_study_multiple_bills_aggregate(self):
        # #683: 同一就诊(study)两张账单 → 聚合(300),不只取第一张
        with tempfile.TemporaryDirectory() as tmp:
            db, client = _client(tmp)
            with connect(db) as c:
                _visit(c, "r1", "p1", "测试医生B", "初诊", "2026-07-02", study="S1")
                _bill(c, "b1", "p1", "2026-07-02", 100, 100, study="S1")
                _bill(c, "b2", "p1", "2026-07-02", 200, 150, study="S1")
                c.commit()
            d = self._q(client)
            self.assertEqual(d["count"], 1)
            self.assertEqual(d["rows"][0]["total_fee"], 300.0)     # 100+200
            self.assertEqual(d["rows"][0]["paid"], 250.0)          # 100+150

    def test_same_day_bills_not_duplicated(self):
        # #683: 同患者同日两就诊 + 两张无study账单 → 每张只用一次、不重复计第一张
        with tempfile.TemporaryDirectory() as tmp:
            db, client = _client(tmp)
            with connect(db) as c:
                _visit(c, "r1", "p1", "测试医生B", "初诊", "2026-07-02", study="")
                _visit(c, "r2", "p1", "测试医生A", "复诊", "2026-07-02", study="")
                _bill(c, "b1", "p1", "2026-07-02", 100, 100)
                _bill(c, "b2", "p1", "2026-07-02", 200, 200)
                c.commit()
            d = self._q(client)
            self.assertEqual(d["count"], 2)
            # 两张账单各计一次,总额=300(不是重复的200或漏计的100)
            self.assertEqual(d["total"]["total_fee"], 300.0)
            self.assertEqual(d["total"]["paid"], 300.0)

    def test_empty_record_type_excluded_no_duplicate(self):
        # #691: 同患者同日多一条空 record_type 的草稿病历,不得在日结里产生重复行
        with tempfile.TemporaryDirectory() as tmp:
            db, client = _client(tmp)
            with connect(db) as c:
                _visit(c, "r1", "p1", "测试医生B", "复诊", "2026-07-02", study="S1")
                _visit(c, "r1e", "p1", "测试医生B", "", "2026-07-02", study="")   # 空类型草稿
                c.commit()
            d = self._q(client)
            self.assertEqual(d["count"], 1)                       # 只1条,空类型那条被排除
            self.assertEqual(d["rows"][0]["visit_type"], "复诊")

    def test_invalid_birthday_returns_empty_age(self):
        # #684: 非法生日不猜年龄
        from local_app.services.finance_service import age_from_birthday as _age_from_birthday
        self.assertEqual(_age_from_birthday("1990-13-01"), "")   # 13月
        self.assertEqual(_age_from_birthday("1990-02-30"), "")   # 2-30不存在
        self.assertEqual(_age_from_birthday(""), "")
        self.assertEqual(_age_from_birthday("not-a-date"), "")
        self.assertEqual(_age_from_birthday("2000-02-29"), "26") # 闰年合法

    def test_consultant_filter(self):
        with tempfile.TemporaryDirectory() as tmp:
            db, client = _client(tmp)
            with connect(db) as c:
                _visit(c, "r1", "p1", "测试医生B", "初诊", "2026-07-02", study="S1")
                c.commit()
            self.assertEqual(self._q(client, consultant="小咨")["count"], 1)
            self.assertEqual(self._q(client, consultant="别人")["count"], 0)

    def test_softdeleted_patient_history_stays_in_settlement_813(self):
        # #813②:软删患者的历史就诊+已收账单仍进日结(财务史实不可变),与收入报表口径一致
        with tempfile.TemporaryDirectory() as tmp:
            db, client = _client(tmp)
            with connect(db) as c:
                _visit(c, "r1", "p1", "测试医生B", "初诊", "2026-07-02", study="S1")
                _bill(c, "b1", "p1", "2026-07-02", 100, 100, study="S1")
                c.execute("insert into payments(payment_id, patient_identity, bill_id, pay_time, amount, "
                          "payment_record_type, updated_at) values ('pay1','p1','b1',"
                          "'2026-07-02 09:35:00',100,'charge','2026-07-02')")
                c.commit()
            before = self._q(client)
            with connect(db) as c:
                c.execute("update patients set is_deleted=1 where patient_identity='p1'")
                c.commit()
            after = self._q(client)
            self.assertEqual(after["count"], 1)
            self.assertEqual(after["rows"][0]["paid"], before["rows"][0]["paid"])
            # 与同区间收入报表实收闭合
            inc = client.get("/api/reports/income?date_from=2026-07-01&date_to=2026-07-31").json()
            self.assertEqual(inc["total_amount"], after["rows"][0]["paid"])


if __name__ == "__main__":
    unittest.main()
