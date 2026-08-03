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
            "values ('p1', '张三', '2026-06-07 00:00:00', 'h1')"
        )
        pays = [
            ("pay1", "p1", "2026-06-10 09:00", 200),
            ("pay2", "p1", "2026-06-10 15:00", 300),
            ("pay3", "p1", "2026-06-11 10:00", 500),
        ]
        for pid, pat, t, amt in pays:
            conn.execute(
                "insert into payments(payment_id, patient_identity, pay_time, amount, state) "
                "values (?, ?, ?, ?, '已收')",
                (pid, pat, t, amt),
            )
        conn.commit()
    return db_path


class IncomeReportTest(unittest.TestCase):
    def test_income_by_day(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = _setup(tmpdir)
            client = TestClient(create_app(db_path))
            data = client.get("/api/reports/income?date_from=2026-06-10&date_to=2026-06-11").json()
        by_day = {d["date"]: d for d in data["days"]}
        self.assertEqual(by_day["2026-06-10"]["amount"], 500)  # 200+300
        self.assertEqual(by_day["2026-06-10"]["count"], 2)
        self.assertEqual(by_day["2026-06-11"]["amount"], 500)
        self.assertEqual(data["total_amount"], 1000)
        self.assertEqual(data["total_count"], 3)

    def test_multi_method_payment_split_consistent(self):
        # #559:多方式收款(PaidDetail)在收入统计/今日营收/工作台三处口径一致——按真实方式拆分,不整体记'多种'
        import json
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "clinic.sqlite3"
            init_db(db_path)
            with connect(db_path) as conn:
                conn.execute("insert into patients(patient_identity,display_name,updated_at,current_hash) "
                             "values('p1','张三','x','h1')")
                # 一笔多方式:现金300+微信450=750,pay_type 写'多种'
                conn.execute("insert into payments(payment_id,patient_identity,pay_time,amount,pay_type,source_json,state,updated_at) "
                             "values('pay1','p1','2026-07-04 10:00',750,'多种',?,'已收','x')",
                             (json.dumps({"PaidDetail": "现金:300||微信:450"}, ensure_ascii=False),))
                # 一笔单方式:现金100(拆不出→按 pay_type 整笔)
                conn.execute("insert into payments(payment_id,patient_identity,pay_time,amount,pay_type,source_json,state,updated_at) "
                             "values('pay2','p1','2026-07-04 11:00',100,'现金','{}','已收','x')")
                conn.commit()
            client = TestClient(create_app(db_path))
            inc = client.get("/api/reports/income?date_from=2026-07-04&date_to=2026-07-04").json()
            tod = client.get("/api/reports/today-collection?date=2026-07-04").json()
        day = inc["days"][0]
        self.assertEqual(day["methods"].get("现金"), 400.0)   # 拆分300 + 整笔100
        self.assertEqual(day["methods"].get("微信"), 450.0)
        self.assertNotIn("多种", day["methods"])              # 不再整体记'多种'
        self.assertEqual(day["count"], 2)                      # 2笔,拆方式不虚增笔数
        self.assertEqual(day["amount"], 850.0)                # 总额不变
        tod_m = {m["method"]: m["amount"] for m in tod["by_method"]}
        self.assertEqual(tod_m.get("现金"), 400.0)            # 今日营收同口径
        self.assertEqual(tod_m.get("微信"), 450.0)
        self.assertEqual(tod["collected"], 850.0)

    def test_income_bad_date_400(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = _setup(tmpdir)
            client = TestClient(create_app(db_path))
            self.assertEqual(
                client.get("/api/reports/income?date_from=2026-13-99").status_code, 400
            )

    def test_income_from_after_to_400(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = _setup(tmpdir)
            client = TestClient(create_app(db_path))
            self.assertEqual(
                client.get("/api/reports/income?date_from=2026-06-11&date_to=2026-06-10").status_code,
                400,
            )


class CashierReportTest(unittest.TestCase):
    def test_cashier_single_day_compat(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = _setup(tmpdir)
            client = TestClient(create_app(db_path))
            data = client.get("/api/reports/cashier?date_from=2026-06-10&date_to=2026-06-10").json()
        self.assertEqual(data["count"], 2)
        self.assertEqual(data["total_amount"], 500)
        self.assertEqual(data["records"][0]["display_name"], "张三")

    def test_cashier_date_range_and_by_patient(self):
        # 复审#4：日期区间 + 按患者聚合
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = _setup(tmpdir)
            client = TestClient(create_app(db_path))
            data = client.get(
                "/api/reports/cashier?date_from=2026-06-10&date_to=2026-06-11"
            ).json()
        self.assertEqual(data["count"], 3)
        self.assertEqual(data["total_amount"], 1000)
        # 三笔都是同一患者 p1 → 按患者聚合后一条
        self.assertEqual(len(data["by_patient"]), 1)
        self.assertEqual(data["by_patient"][0]["amount"], 1000)
        self.assertEqual(data["by_patient"][0]["count"], 3)

    def test_cashier_bad_date_400(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = _setup(tmpdir)
            client = TestClient(create_app(db_path))
            self.assertEqual(client.get("/api/reports/cashier?date_from=xx").status_code, 400)


class StaffWorkloadTest(unittest.TestCase):
    def _client(self, tmpdir):
        db_path = Path(tmpdir) / "clinic.sqlite3"
        init_db(db_path)
        with connect(db_path) as conn:
            conn.execute(
                "insert into patients(patient_identity, display_name, updated_at, current_hash) "
                "values ('p1', '张三', '2026-06-07 00:00:00', 'h1')"
            )
            conn.commit()
        return db_path, TestClient(create_app(db_path))

    def test_workload_aggregates_by_person_and_role(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path, client = self._client(tmpdir)
            # 王医生+李护士 配台2单(根管800 / 充填200+200)，赵医生 配台1单(种植1000)
            # #56：统计只算已划价单，故 price_now=True 一步开单并划价
            client.post("/api/patients/p1/treatment-orders", json={
                "doctor_name": "王医生", "nurse_name": "李护士", "price_now": True,
                "items": [{"item_name": "根管", "unit_price": 800, "quantity": 1}]})
            client.post("/api/patients/p1/treatment-orders", json={
                "doctor_name": "王医生", "nurse_name": "李护士", "price_now": True,
                "items": [{"item_name": "充填", "unit_price": 200, "quantity": 2}]})
            client.post("/api/patients/p1/treatment-orders", json={
                "doctor_name": "赵医生", "price_now": True,
                "items": [{"item_name": "种植", "unit_price": 1000, "quantity": 1}]})
            today = __import__("datetime").date.today().isoformat()
            data = client.get(f"/api/reports/staff-workload?date_from={today}&date_to={today}").json()
            by = {(s["role"], s["name"]): s for s in data["staff"]}
            # 王医生: 2单, 2项, 800+400=1200
            self.assertEqual(by[("医生", "王医生")]["order_count"], 2)
            self.assertEqual(by[("医生", "王医生")]["item_count"], 2)
            self.assertEqual(by[("医生", "王医生")]["amount"], 1200)
            # 李护士: 同2单
            self.assertEqual(by[("护士", "李护士")]["order_count"], 2)
            self.assertEqual(by[("护士", "李护士")]["amount"], 1200)
            # 赵医生: 1单 1000
            self.assertEqual(by[("医生", "赵医生")]["amount"], 1000)

    def test_voided_order_excluded(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path, client = self._client(tmpdir)
            oid = client.post("/api/patients/p1/treatment-orders", json={
                "doctor_name": "王医生",
                "items": [{"item_name": "根管", "unit_price": 800, "quantity": 1}]}).json()["order_id"]
            client.post(f"/api/treatment-orders/{oid}/void", json={"reason": "录错"})
            today = __import__("datetime").date.today().isoformat()
            data = client.get(f"/api/reports/staff-workload?date_from={today}&date_to={today}").json()
            self.assertEqual(data["staff"], [])

    def test_unpriced_order_excluded(self):
        # #56：未划价(recorded)处置不计入绩效金额(总价仅为录入原价)
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path, client = self._client(tmpdir)
            client.post("/api/patients/p1/treatment-orders", json={
                "doctor_name": "王医生",
                "items": [{"item_name": "根管", "unit_price": 800, "quantity": 1}]})  # 未划价
            today = __import__("datetime").date.today().isoformat()
            data = client.get(f"/api/reports/staff-workload?date_from={today}&date_to={today}").json()
            self.assertEqual(data["staff"], [])

    def test_bad_date_rejected(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path, client = self._client(tmpdir)
            self.assertEqual(client.get("/api/reports/staff-workload?date_from=xx").status_code, 400)


if __name__ == "__main__":
    unittest.main()


class IncomeMonthlyTest(unittest.TestCase):
    def test_monthly_grouping_and_cancel_exclusion(self):
        # #657:按月聚合,口径同 income——排作废原单(CancelMark非空且金额≥0),保留退费负数冲减
        import json as _json
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = _setup(tmpdir)
            with connect(db_path) as conn:
                conn.execute("insert into payments(payment_id, patient_identity, pay_time, amount, state) "
                             "values ('pay5','p1','2026-05-20 09:00', 100, '已收')")
                conn.execute("insert into payments(payment_id, patient_identity, pay_time, amount, state, source_json) "
                             "values ('payv','p1','2026-06-12 09:00', 999, '已收', ?)",
                             (_json.dumps({"CancelMark": "1"}),))
                conn.execute("insert into payments(payment_id, patient_identity, pay_time, amount, state) "
                             "values ('payr','p1','2026-06-12 10:00', -50, '退费')")
                conn.commit()
            client = TestClient(create_app(db_path))
            d = client.get("/api/reports/income-monthly?date_from=2026-05-01&date_to=2026-06-30").json()
        by_m = {m["month"]: m for m in d["months"]}
        self.assertEqual(by_m["2026-05"]["amount"], 100)
        self.assertEqual(by_m["2026-06"]["amount"], 950)   # 200+300+500-50,排除作废999
        self.assertEqual(d["total_amount"], 1050)
        self.assertEqual(by_m["2026-06"]["count"], 4)      # 退费负数计1笔,作废不计

    def test_monthly_default_range_and_validation(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = _setup(tmpdir)
            client = TestClient(create_app(db_path))
            d = client.get("/api/reports/income-monthly").json()
            self.assertTrue(d["date_from"].endswith("-01"))    # 默认近12个月从月初起
            self.assertEqual(client.get("/api/reports/income-monthly?date_from=bad").status_code, 400)
            self.assertEqual(client.get(
                "/api/reports/income-monthly?date_from=2026-07-01&date_to=2026-06-01").status_code, 400)
