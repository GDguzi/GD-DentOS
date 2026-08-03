"""收入统计欠款流水：期初/期末欠款 + 每日新增/补交欠款，锁住勾稽恒等式。"""
import json
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from local_app import auth
from local_app.api import create_app
from local_app.db import connect, init_db


def _client(tmp):
    db = Path(tmp) / "clinic.sqlite3"
    init_db(db)
    with connect(db) as c:
        auth.create_user(c, "admin", "管理员", "admin123", role="admin")
        for pid in ("p1", "p2"):
            c.execute("insert into patients(patient_identity, display_name, updated_at, current_hash) "
                      "values (?,?,?,?)", (pid, pid, "2026-05-01", "h_" + pid))
        c.commit()
    client = TestClient(create_app(db))
    client.post("/api/auth/login", json={"username": "admin", "password": "admin123"})
    return db, client


def _bill(c, bid, pid, day, total, state="paid", discount=None):
    sj = {}
    if discount is not None:
        sj["DiscountFee"] = discount
    c.execute("insert into bills(bill_id, patient_identity, bill_time, total_fee, paid_fee, state, "
              "source_json, updated_at) values (?,?,?,?,?,?,?,?)",
              (bid, pid, day + " 09:00:00", total, 0, state, json.dumps(sj), day))


def _pay(c, payid, pid, bid, day, amount, cancel_mark=None):
    sj = json.dumps({"CancelMark": cancel_mark} if cancel_mark else {})
    c.execute("insert into payments(payment_id, patient_identity, bill_id, pay_time, amount, state, "
              "pay_type, source_json, updated_at) values (?,?,?,?,?,?,?,?,?)",
              (payid, pid, bid, day + " 10:00:00", amount, "paid", "现金", sj, day))
    # 模拟真实收款:同步账单 paid_fee(欠款绝对值以 paid_fee 锚定)
    if not cancel_mark:
        c.execute("update bills set paid_fee = paid_fee + ? where bill_id = ?", (amount, bid))


class IncomeArrearsTest(unittest.TestCase):
    def test_arrears_flows_and_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            db, client = _client(tmp)
            with connect(db) as c:
                # 5月遗留欠款:05-20 开单1000,未收 → 期初(06-01)=1000
                _bill(c, "bMay", "p2", "2026-05-20", 1000)
                # 06-01 新开5000,当天收2000 → 新增欠款(06-01)=3000
                _bill(c, "bA", "p1", "2026-06-01", 5000)
                _pay(c, "payA1", "p1", "bA", "2026-06-01", 2000)
                # 06-05 补交往期账单bA 1000 → 补交欠款(06-05)=1000
                _pay(c, "payA2", "p1", "bA", "2026-06-05", 1000)
                c.commit()
            d = client.get("/api/reports/income?date_from=2026-06-01&date_to=2026-06-30").json()
            self.assertEqual(d["opening_arrears"], 1000.0)            # 期初=5月遗留
            self.assertEqual(d["total_new_debt"], 3000.0)            # 新增
            self.assertEqual(d["total_repay_debt"], 1000.0)          # 补交
            self.assertEqual(d["closing_arrears"], 3000.0)           # 1000+3000-1000
            # 勾稽恒等式
            self.assertEqual(d["closing_arrears"],
                             round(d["opening_arrears"] + d["total_new_debt"] - d["total_repay_debt"], 2))
            by_day = {r["date"]: r for r in d["days"]}
            self.assertEqual(by_day["2026-06-01"]["new_debt"], 3000.0)
            self.assertEqual(by_day["2026-06-05"]["repay_debt"], 1000.0)

    def test_discount_reduces_new_debt(self):
        # 优惠计入净应收:开单1000优惠300当天全收700 → 无新增欠款
        with tempfile.TemporaryDirectory() as tmp:
            db, client = _client(tmp)
            with connect(db) as c:
                _bill(c, "bD", "p1", "2026-06-10", 1000, discount="300")
                _pay(c, "payD", "p1", "bD", "2026-06-10", 700)
                c.commit()
            d = client.get("/api/reports/income?date_from=2026-06-01&date_to=2026-06-30").json()
            self.assertEqual(d["total_new_debt"], 0.0)      # 净应收700=当天收700
            self.assertEqual(d["closing_arrears"], 0.0)

    def test_prepay_empty_bill_not_inflate_closing(self):
        # #682: 期后空 bill_id 预交(不改 paid_fee)不得抬高历史期末欠款
        with tempfile.TemporaryDirectory() as tmp:
            db, client = _client(tmp)
            with connect(db) as c:
                _bill(c, "bA", "p1", "2026-06-01", 1000)     # 06-01 欠1000,未收
                # 07-05 一笔空 bill_id 预交 500(不更新任何账单 paid_fee)
                c.execute("insert into payments(payment_id, patient_identity, bill_id, pay_time, amount, "
                          "state, pay_type, source_json, updated_at) "
                          "values ('pre','p1','','2026-07-05 10:00:00',500,'paid','现金','{}','2026-07-05')")
                c.commit()
            d = client.get("/api/reports/income.csv?date_from=2026-06-01&date_to=2026-06-30")  # 触发同逻辑
            j = client.get("/api/reports/income?date_from=2026-06-01&date_to=2026-06-30").json()
            self.assertEqual(j["closing_arrears"], 1000.0)   # 不是被预交抬高的 1500
            self.assertEqual(j["opening_arrears"], 0.0)      # 不是 500
            self.assertEqual(j["total_new_debt"], 1000.0)

    def test_income_csv_export(self):
        with tempfile.TemporaryDirectory() as tmp:
            db, client = _client(tmp)
            with connect(db) as c:
                _bill(c, "bA", "p1", "2026-06-02", 1000)
                _pay(c, "payA", "p1", "bA", "2026-06-02", 600)
                c.commit()
            r = client.get("/api/reports/income.csv?date_from=2026-06-01&date_to=2026-06-30")
            self.assertEqual(r.status_code, 200)
            self.assertIn("text/csv", r.headers["content-type"])
            text = r.content.decode("utf-8-sig")
            self.assertIn("应收款项", text)
            self.assertIn("新增欠款", text)
            self.assertIn("补交欠款", text)
            self.assertIn("期初欠款", text)
            self.assertIn("总合计", text)

    def test_voided_bill_excluded_from_arrears(self):
        with tempfile.TemporaryDirectory() as tmp:
            db, client = _client(tmp)
            with connect(db) as c:
                _bill(c, "bV", "p1", "2026-06-12", 8000, state="900")   # 作废
                c.commit()
            d = client.get("/api/reports/income?date_from=2026-06-01&date_to=2026-06-30").json()
            self.assertEqual(d["total_new_debt"], 0.0)
            self.assertEqual(d["closing_arrears"], 0.0)


if __name__ == "__main__":
    unittest.main()
