"""撤单/作废账单(历史数字撤单码 900/500/400/200 + 本地 voided)不计入合计/欠费/营收。

背景：导入的历史账单用数字码标记撤单；本地之前只认字符串 'voided'，漏了数字撤单码，
导致撤单单进了应收/欠费/合计(冒出大量幽灵欠费)。
"""
import datetime as dt
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from local_app.api import create_app
from local_app.db import connect, init_db

TODAY = dt.date.today().isoformat()


def _client(tmp):
    db = Path(tmp) / "clinic.sqlite3"
    init_db(db)
    with connect(db) as conn:
        conn.execute(
            "insert into patients(patient_identity, display_name, updated_at, current_hash) "
            "values ('p1', '测试患者', '2026-06-07 00:00:00', 'h1')"
        )
        # 有效单(state 100): 应收1000 已收600 → 欠400
        conn.execute(
            "insert into bills(bill_id, patient_identity, bill_no, bill_time, total_fee, paid_fee, state, source_json, updated_at) "
            "values ('b-ok', 'p1', 'B100', ?, 1000, 600, '100', '{}', ?)",
            (TODAY + " 09:00:00", TODAY + " 09:00:00"))
        # 撤单单(state 900): 应收5000 已收4000 → 本不该算的1000幽灵欠费
        conn.execute(
            "insert into bills(bill_id, patient_identity, bill_no, bill_time, total_fee, paid_fee, state, source_json, updated_at) "
            "values ('b-void', 'p1', 'B900', ?, 5000, 4000, '900', "
            "'{\"CancelMark\": \"收费方式选择错误\"}', ?)",
            (TODAY + " 10:00:00", TODAY + " 10:00:00"))
        conn.commit()
    return db, TestClient(create_app(db))


class VoidedBillExclusionTest(unittest.TestCase):
    def test_stats_money_excludes_voided(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, client = _client(tmp)
            money = client.get("/api/stats").json()["money"]
            self.assertEqual(money["bill_total_fee"], 1000)   # 不含撤单5000
            self.assertEqual(money["bill_paid_fee"], 600)     # 不含撤单4000

    def test_patient_detail_marks_is_void(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, client = _client(tmp)
            bills = {b["bill_no"]: b for b in client.get("/api/patients/p1").json()["bills"]}
            self.assertEqual(bills["B100"]["is_void"], 0)
            self.assertEqual(bills["B900"]["is_void"], 1)
            self.assertEqual(bills["B900"]["cancel_mark"], "收费方式选择错误")

    def test_arrears_report_excludes_voided(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, client = _client(tmp)
            data = client.get("/api/reports/arrears").json()
            self.assertEqual(data["total_arrears"], 400)      # 撤单的1000不算
            self.assertEqual(data["count"], 1)
            self.assertEqual(data["rows"][0]["bill_no"], "B100")

    def test_bills_unpaid_only_excludes_voided(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, client = _client(tmp)
            rows = client.get("/api/bills?unpaid_only=true").json()["list"]
            nos = {r["bill_no"] for r in rows}
            self.assertIn("B100", nos)
            self.assertNotIn("B900", nos)               # 撤单单不当欠费列出

    def test_today_work_receivable_excludes_voided(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, client = _client(tmp)
            s = client.get("/api/today-work").json()["summary"]
            self.assertEqual(s["today_receivable"], 1000)     # 不含撤单5000
            self.assertEqual(s["today_paid"], 600)            # 不含撤单4000
            self.assertEqual(s["today_unpaid_amount"], 400)   # 不含撤单1000


def _add_bill(db, bill_no, total, paid, state, discount=0, when=None):
    when = when or (TODAY + " 11:00:00")
    sj = '{"DiscountFee": "%.2f"}' % discount if discount else "{}"
    with connect(db) as conn:
        conn.execute(
            "insert into bills(bill_id, patient_identity, bill_no, bill_time, total_fee, paid_fee, state, source_json, updated_at) "
            "values (?, 'p1', ?, ?, ?, ?, ?, ?, ?)",
            ("id-" + bill_no, bill_no, when, total, paid, state, sj, when))
        conn.commit()


class DiscountArrearsTest(unittest.TestCase):
    """打折单：应收=总价-优惠(DiscountFee)，本地之前不扣优惠→冒幽灵欠费。"""

    def test_discount_settled_not_arrears(self):
        # 总价1000 优惠300 已付700 → 净应收700=已付 → 欠0(不是300)
        with tempfile.TemporaryDirectory() as tmp:
            db, client = _client(tmp)
            with connect(db) as conn:   # 清掉默认两条干扰
                conn.execute("delete from bills"); conn.commit()
            _add_bill(db, "BD1", 1000, 700, "100", discount=300)
            data = client.get("/api/reports/arrears").json()
            self.assertEqual(data["count"], 0)            # 扣优惠后已收清,不算欠费
            self.assertEqual(data["total_arrears"], 0)

    def test_discount_partial_arrears_net(self):
        # 总价1000 优惠300 已付500 → 净应收700 → 真欠200(不是500)
        with tempfile.TemporaryDirectory() as tmp:
            db, client = _client(tmp)
            with connect(db) as conn:
                conn.execute("delete from bills"); conn.commit()
            _add_bill(db, "BD2", 1000, 500, "100", discount=300)
            data = client.get("/api/reports/arrears").json()
            self.assertEqual(data["count"], 1)
            self.assertEqual(data["rows"][0]["arrears"], 200)   # 700-500,非500
            self.assertEqual(data["total_arrears"], 200)

    def test_patient_detail_net_receivable(self):
        with tempfile.TemporaryDirectory() as tmp:
            db, client = _client(tmp)
            with connect(db) as conn:
                conn.execute("delete from bills"); conn.commit()
            _add_bill(db, "BD3", 1000, 700, "100", discount=300)
            bill = client.get("/api/patients/p1").json()["bills"][0]
            self.assertEqual(bill["net_receivable"], 700)       # 总价1000-优惠300
            self.assertEqual(bill["is_void"], 0)

    def test_discount_with_thousands_comma(self):
        # 大额优惠 DiscountFee 可能带千分位逗号 "1,400.00"。
        # SQLite 字符串转数值读到逗号即停→当成1→9400-1=9399 凭空欠1399。修:取数前 replace 去逗号。
        with tempfile.TemporaryDirectory() as tmp:
            db, client = _client(tmp)
            with connect(db) as conn:
                conn.execute("delete from bills")
                conn.execute(
                    "insert into bills(bill_id, patient_identity, bill_no, bill_time, total_fee, paid_fee, "
                    "state, source_json, updated_at) "
                    "values ('idC', 'p1', 'BCOMMA', ?, 9400, 8000, '100', "
                    "'{\"DiscountFee\": \"1,400.00\"}', ?)",
                    (TODAY + " 09:00:00", TODAY + " 09:00:00"))
                conn.commit()
            bill = client.get("/api/patients/p1").json()["bills"][0]
            self.assertEqual(bill["net_receivable"], 8000)      # 9400-1400(非9399)
            self.assertEqual(float(bill["discount_fee"]), 1400) # 减免列拿到1400(非"1,400.00"致NaN)
            arr = client.get("/api/reports/arrears").json()
            self.assertEqual(arr["count"], 0)                   # 已付8000=应收8000→不欠费
            self.assertEqual(arr["total_arrears"], 0)


if __name__ == "__main__":
    unittest.main()
