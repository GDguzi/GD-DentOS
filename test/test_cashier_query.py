"""Phase C 收银查询报表：逐笔收款 + 付款方式透视 + 处置类别分摊 + CSV 导出。"""
import tempfile
import unittest
import uuid
from pathlib import Path

from fastapi.testclient import TestClient

from local_app import auth
from local_app.api import create_app
from local_app.db import connect, init_db


def _client(tmp):
    db = Path(tmp) / "clinic.sqlite3"
    init_db(db)
    with connect(db) as c:
        auth.create_user(c, "admin", "管理员", "admin123", role="admin")  # P1-13：CSV 导出仅 admin
        c.execute("insert into patients(patient_identity, display_name, chart_no, referral_source, updated_at, current_hash) "
                  "values ('p1','张三','260101001','朋友介绍','2026-06-07','h1')")
        c.commit()
    client = TestClient(create_app(db))
    client.post("/api/auth/login", json={"username": "admin", "password": "admin123"})
    return db, client


def _bill(client, items, doctor="测试医生B"):
    oid = client.post("/api/patients/p1/treatment-orders",
                      json={"doctor_name": doctor, "items": items}).json()["order_id"]
    return client.post(f"/api/treatment-orders/{oid}/price", json={}).json()["bill_id"]


class CashierQueryTest(unittest.TestCase):
    def test_method_and_category_pivot(self):
        with tempfile.TemporaryDirectory() as tmp:
            db, client = _client(tmp)
            b = _bill(client, [{"item_name": "种植体", "unit_price": 600, "quantity": 1, "fee_type": "种植费"},
                               {"item_name": "消炎药", "unit_price": 400, "quantity": 1, "fee_type": "药品费"}])
            client.post(f"/api/bills/{b}/pay", json={"methods": [{"method": "现金", "amount": 1000}], "cashier": "前台A", "request_id": uuid.uuid4().hex})
            d = client.get("/api/reports/cashier-query?date_from=2026-01-01&date_to=2030-01-01").json()
            self.assertEqual(d["count"], 1)
            row = d["rows"][0]
            self.assertEqual(row["amount"], 1000)
            self.assertEqual(row["pay_type"], "现金")
            self.assertEqual(row["doctor"], "测试医生B")
            self.assertEqual(row["chart_no"], "260101001")
            self.assertEqual(row["referral_source"], "朋友介绍")
            self.assertEqual(row["operator"], "前台A")
            # 处置类别分摊:600种植+400药品,合计=总收入
            self.assertEqual(row["categories"]["种植费"], 600)
            self.assertEqual(row["categories"]["药品费"], 400)
            self.assertEqual(round(sum(row["categories"].values()), 2), row["amount"])
            self.assertEqual(d["by_method"]["现金"], 1000)
            self.assertEqual(d["by_category"]["种植费"], 600)

    def test_split_payment_allocates_each(self):
        with tempfile.TemporaryDirectory() as tmp:
            db, client = _client(tmp)
            b = _bill(client, [{"item_name": "种植体", "unit_price": 800, "quantity": 1, "fee_type": "种植费"},
                               {"item_name": "拔牙", "unit_price": 200, "quantity": 1, "fee_type": "拔牙费"}])
            client.post(f"/api/bills/{b}/pay", json={"methods": [{"method": "现金", "amount": 600}, {"method": "云闪付", "amount": 400}], "request_id": uuid.uuid4().hex})
            d = client.get("/api/reports/cashier-query?date_from=2026-01-01&date_to=2030-01-01").json()
            self.assertEqual(d["count"], 2)                       # 两笔(每方式一行)
            for row in d["rows"]:
                self.assertEqual(round(sum(row["categories"].values()), 2), row["amount"])  # 每行分摊合计=该行金额
            self.assertEqual(round(d["total_amount"], 2), 1000)
            self.assertEqual(d["by_method"], {"现金": 600, "云闪付": 400})

    def test_filter_by_method(self):
        with tempfile.TemporaryDirectory() as tmp:
            db, client = _client(tmp)
            b = _bill(client, [{"item_name": "x", "unit_price": 1000, "quantity": 1, "fee_type": "种植费"}])
            client.post(f"/api/bills/{b}/pay", json={"methods": [{"method": "现金", "amount": 600}, {"method": "云闪付", "amount": 400}], "request_id": uuid.uuid4().hex})
            d = client.get("/api/reports/cashier-query?date_from=2026-01-01&date_to=2030-01-01&method=现金").json()
            self.assertEqual(d["count"], 1)
            self.assertEqual(d["rows"][0]["pay_type"], "现金")

    def test_csv_export(self):
        with tempfile.TemporaryDirectory() as tmp:
            db, client = _client(tmp)
            b = _bill(client, [{"item_name": "x", "unit_price": 500, "quantity": 1, "fee_type": "洗牙费"}])
            client.post(f"/api/bills/{b}/pay", json={"methods": [{"method": "微信", "amount": 500}], "request_id": uuid.uuid4().hex})
            r = client.get("/api/reports/cashier-query.csv?date_from=2026-01-01&date_to=2030-01-01")
            self.assertEqual(r.status_code, 200)
            self.assertIn("text/csv", r.headers["content-type"])
            text = r.content.decode("utf-8-sig")
            self.assertIn("收款时间", text)
            self.assertIn("总合计", text)
            self.assertIn("洗牙费", text)


    def test_filter_doctor_and_source_and_voided_and_nobill(self):
        with tempfile.TemporaryDirectory() as tmp:
            db, client = _client(tmp)
            b = _bill(client, [{"item_name": "x", "unit_price": 300, "quantity": 1, "fee_type": "补牙费"}], doctor="测试医生A")
            client.post(f"/api/bills/{b}/pay", json={"methods": [{"method": "现金", "amount": 300}], "request_id": uuid.uuid4().hex})
            # 直插一笔作废原单(CancelMark) + 一笔无账单付款
            with connect(db) as c:
                c.execute("insert into payments(payment_id,patient_identity,bill_id,pay_time,amount,state,pay_type,source_json,updated_at) "
                          "values ('pv','p1','','2026-06-10',999,'paid','现金','{\"CancelMark\":\"收费方式选择错误\"}','2026-06-10')")
                c.execute("insert into payments(payment_id,patient_identity,bill_id,pay_time,amount,state,pay_type,source_json,updated_at) "
                          "values ('pnb','p1','','2026-06-10',88,'paid','微信','{}','2026-06-10')")
                c.commit()
            d = client.get("/api/reports/cashier-query?date_from=2026-01-01&date_to=2030-01-01").json()
            # 作废原单(999)被排除;无账单付款(88)→未分类
            self.assertNotIn(999, [r["amount"] for r in d["rows"]])
            nobill = [r for r in d["rows"] if r["amount"] == 88][0]
            self.assertEqual(nobill["categories"], {"未分类": 88})
            # 医生筛选
            self.assertEqual(client.get("/api/reports/cashier-query?date_from=2026-01-01&date_to=2030-01-01&doctor=测试医生A").json()["count"], 1)
            self.assertEqual(client.get("/api/reports/cashier-query?date_from=2026-01-01&date_to=2030-01-01&doctor=不存在").json()["count"], 0)
            # 来源筛选(p1全=朋友介绍;作废999已排除,剩 300+88=2 笔)
            self.assertEqual(client.get("/api/reports/cashier-query?date_from=2026-01-01&date_to=2030-01-01&source=朋友介绍").json()["count"], 2)

    def test_chart_no_fallback_to_source_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            db, client = _client(tmp)
            with connect(db) as c:
                c.execute("insert into patients(patient_identity,display_name,source_json,updated_at,current_hash) "
                          "values ('p2','李四','{\"patientid\":\"209999001\"}','2026-06-07','h2')")
                oid_total = 200
                c.commit()
            oid = client.post("/api/patients/p2/treatment-orders", json={"items": [{"item_name": "x", "unit_price": 200, "quantity": 1, "fee_type": "检查费"}]}).json()["order_id"]
            bill = client.post(f"/api/treatment-orders/{oid}/price", json={}).json()["bill_id"]
            client.post(f"/api/bills/{bill}/pay", json={"methods": [{"method": "现金", "amount": 200}], "request_id": uuid.uuid4().hex})
            d = client.get("/api/reports/cashier-query?date_from=2026-01-01&date_to=2030-01-01").json()
            row = [r for r in d["rows"] if r["display_name"] == "李四"][0]
            self.assertEqual(row["chart_no"], "209999001")   # 无本地chart_no→回退source_json.patientid

    def test_csv_dynamic_method_column(self):
        with tempfile.TemporaryDirectory() as tmp:
            db, client = _client(tmp)
            b = _bill(client, [{"item_name": "x", "unit_price": 500, "quantity": 1, "fee_type": "种植费"}])
            client.post(f"/api/bills/{b}/pay", json={"methods": [{"method": "会员卡", "amount": 500}], "request_id": uuid.uuid4().hex})  # 固定列表外的方式
            text = client.get("/api/reports/cashier-query.csv?date_from=2026-01-01&date_to=2030-01-01").content.decode("utf-8-sig")
            self.assertIn("会员卡", text.splitlines()[0])     # 动态加进表头列
            self.assertTrue(text.startswith("收款时间"))       # BOM 被 utf-8-sig 解码掉

    def test_operator_filter(self):
        # 收银查询按收银员筛选
        with tempfile.TemporaryDirectory() as tmp:
            db, client = _client(tmp)
            b1 = _bill(client, [{"item_name": "x", "unit_price": 100, "quantity": 1, "fee_type": "洗牙费"}])
            client.post(f"/api/bills/{b1}/pay", json={"methods": [{"method": "现金", "amount": 100}], "cashier": "前台A", "request_id": uuid.uuid4().hex})
            b2 = _bill(client, [{"item_name": "y", "unit_price": 200, "quantity": 1, "fee_type": "洗牙费"}])
            client.post(f"/api/bills/{b2}/pay", json={"methods": [{"method": "现金", "amount": 200}], "cashier": "前台B", "request_id": uuid.uuid4().hex})
            q = "date_from=2026-01-01&date_to=2030-01-01"
            self.assertEqual(client.get(f"/api/reports/cashier-query?{q}").json()["count"], 2)
            d = client.get(f"/api/reports/cashier-query?{q}&operator=前台A").json()
            self.assertEqual(d["count"], 1)
            self.assertEqual(d["rows"][0]["operator"], "前台A")

    def test_phone_and_multilevel_source_columns(self):
        # 对齐通用口径 收银查询:联系电话 + 患者来源/二级/三级来源逐笔带出并进 CSV
        with tempfile.TemporaryDirectory() as tmp:
            db, client = _client(tmp)
            with connect(db) as c:
                c.execute("update patients set phone='13800000001', referral_source='朋友介绍', "
                          "referral_source2='门诊', referral_source3='碧桂园' where patient_identity='p1'")
                c.commit()
            b = _bill(client, [{"item_name": "x", "unit_price": 100, "quantity": 1, "fee_type": "洗牙费"}])
            client.post(f"/api/bills/{b}/pay", json={"methods": [{"method": "现金", "amount": 100}], "request_id": uuid.uuid4().hex})
            q = "date_from=2026-01-01&date_to=2030-01-01"
            row = client.get(f"/api/reports/cashier-query?{q}").json()["rows"][0]
            self.assertEqual(row["phone"], "13800000001")
            self.assertEqual(row["referral_source2"], "门诊")
            self.assertEqual(row["referral_source3"], "碧桂园")
            text = client.get(f"/api/reports/cashier-query.csv?{q}").content.decode("utf-8-sig")
            self.assertIn("联系电话", text)
            self.assertIn("13800000001", text)
            self.assertIn("碧桂园", text)

    def test_income_by_method_and_receivable(self):
        # 营收日报 日×付款方式透视 + 应收列
        with tempfile.TemporaryDirectory() as tmp:
            db, client = _client(tmp)
            b = _bill(client, [{"item_name": "x", "unit_price": 1000, "quantity": 1, "fee_type": "种植费"}])
            client.post(f"/api/bills/{b}/pay", json={"methods": [{"method": "现金", "amount": 600}, {"method": "云闪付", "amount": 400}], "request_id": uuid.uuid4().hex})
            d = client.get("/api/reports/income?date_from=2026-01-01&date_to=2030-01-01").json()
            self.assertIn("现金", d["methods"])
            self.assertIn("云闪付", d["methods"])
            self.assertEqual(d["total_amount"], 1000)
            self.assertEqual(d["total_receivable"], 1000)        # 账单净应收
            day = d["days"][0]
            self.assertEqual(day["methods"]["现金"], 600)
            self.assertEqual(day["methods"]["云闪付"], 400)
            self.assertEqual(day["receivable"], 1000)

    def test_paytype_parse_from_paiddetail(self):
        from local_app.paid_detail import paytype_from_detail
        self.assertEqual(paytype_from_detail("云闪付:1233.00"), "云闪付")
        self.assertEqual(paytype_from_detail("云闪付:200||现金:800"), "多种")
        self.assertEqual(paytype_from_detail(""), "")


if __name__ == "__main__":
    unittest.main()
