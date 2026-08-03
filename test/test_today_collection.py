import tempfile
import unittest
import uuid
from pathlib import Path

from fastapi.testclient import TestClient

from local_app.api import create_app
from local_app.db import connect, init_db


def _client(tmp):
    db = Path(tmp) / "clinic.sqlite3"
    init_db(db)
    with connect(db) as conn:
        conn.execute(
            "insert into patients(patient_identity, display_name, updated_at, current_hash) "
            "values ('p1', '测试患者', '2026-06-07 00:00:00', 'h1')"
        )
        conn.commit()
    return db, TestClient(create_app(db))


def _order_paid(client, total, methods):
    """新增处置→划价→按 methods 分笔收款。methods=[(amount, method), ...]"""
    oid = client.post("/api/patients/p1/treatment-orders", json={
        "items": [{"item_name": "处置", "unit_price": total, "quantity": 1}]}).json()["order_id"]
    bill = client.post(f"/api/treatment-orders/{oid}/price", json={}).json()["bill_id"]
    for amt, m in methods:
        client.post(f"/api/bills/{bill}/pay", json={"methods": [{"method": m, "amount": amt}], "request_id": uuid.uuid4().hex})
    return bill


class TodayCollectionTest(unittest.TestCase):
    def _day(self, db, bill):
        with connect(db) as conn:
            t = conn.execute("select pay_time from payments where bill_id=? limit 1", (bill,)).fetchone()["pay_time"]
        return t[:10]

    def test_collection_by_method_and_pending(self):
        with tempfile.TemporaryDirectory() as tmp:
            db, client = _client(tmp)
            b1 = _order_paid(client, 1000, [(600, "现金"), (400, "微信")])  # 全收
            # 一张只收一半的（partial 待收）
            oid = client.post("/api/patients/p1/treatment-orders", json={
                "items": [{"item_name": "X", "unit_price": 500, "quantity": 1}]}).json()["order_id"]
            b2 = client.post(f"/api/treatment-orders/{oid}/price", json={}).json()["bill_id"]
            client.post(f"/api/bills/{b2}/pay", json={"methods": [{"method": "现金", "amount": 200}], "request_id": uuid.uuid4().hex})

            day = self._day(db, b1)
            data = client.get(f"/api/reports/today-collection?date={day}").json()
            self.assertEqual(data["collected"], 1200)  # 600+400+200
            self.assertEqual(data["count"], 3)
            methods = {m["method"]: m["amount"] for m in data["by_method"]}
            self.assertEqual(methods["现金"], 800)  # 600+200
            self.assertEqual(methods["微信"], 400)
            # b2 仍待收 300
            self.assertEqual(data["pending_count"], 1)
            self.assertEqual(data["pending_total"], 300)

    def test_by_method_uses_pay_type_column(self):
        # 看板#211:同步收款只有 pay_type 列(source_json无pay_method)→仍按pay_type透视,不落"未填"
        with tempfile.TemporaryDirectory() as tmp:
            db, client = _client(tmp)
            with connect(db) as conn:
                conn.execute("insert into payments(payment_id,patient_identity,bill_id,pay_time,amount,state,pay_type,source_json,updated_at) "
                             "values ('pp','p1','','2026-06-10 09:00',500,'paid','扫码付','{}','2026-06-10')")
                conn.commit()
            data = client.get("/api/reports/today-collection?date=2026-06-10").json()
            methods = {m["method"]: m["amount"] for m in data["by_method"]}
            self.assertEqual(methods.get("扫码付"), 500)
            self.assertNotIn("未填", methods)

    def test_voided_payment_excluded(self):
        # #1：作废原单(source_json.CancelMark非空且金额≥0)不计入实收
        with tempfile.TemporaryDirectory() as tmp:
            db, client = _client(tmp)
            b1 = _order_paid(client, 100, [(100, "现金")])
            day = self._day(db, b1)
            # 手插一笔同日作废原单(CancelMark标记)
            with connect(db) as conn:
                conn.execute(
                    "insert into payments(payment_id, patient_identity, bill_id, pay_time, amount, state, source_json, updated_at) "
                    "values ('pv', 'p1', ?, ?, 50, '900', '{\"CancelMark\": \"收费方式选择错误\"}', ?)",
                    (b1, day + " 10:00:00", day + " 10:00:00"))
                conn.commit()
            data = client.get(f"/api/reports/today-collection?date={day}").json()
            self.assertEqual(data["collected"], 100)  # 作废50不计
            self.assertEqual(data["count"], 1)

    def test_pending_filtered_by_date(self):
        # #39：待收按当天账单过滤
        with tempfile.TemporaryDirectory() as tmp:
            db, client = _client(tmp)
            oid = client.post("/api/patients/p1/treatment-orders", json={
                "items": [{"item_name": "X", "unit_price": 300, "quantity": 1}]}).json()["order_id"]
            client.post(f"/api/treatment-orders/{oid}/price", json={})  # 今天的待收300
            # 查一个无账单的别的日期 → 待收0
            other = client.get("/api/reports/today-collection?date=2020-01-01").json()
            self.assertEqual(other["pending_total"], 0)
            self.assertEqual(other["pending_count"], 0)

    def test_empty_day(self):
        with tempfile.TemporaryDirectory() as tmp:
            db, client = _client(tmp)
            data = client.get("/api/reports/today-collection?date=2020-01-01").json()
            self.assertEqual(data["collected"], 0)
            self.assertEqual(data["count"], 0)
            self.assertEqual(data["by_method"], [])

    def test_bad_date_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            db, client = _client(tmp)
            self.assertEqual(client.get("/api/reports/today-collection?date=99999999").status_code, 400)


class SaasStatePendingTest(unittest.TestCase):
    """#574:today-collection 的待收不能只认本地 state('pending'/'partial')——
    当天 SaaS 同步来的未结单 state 是数字码,须按净欠费>0口径统计(与工作台/收费管理一致)。"""

    def test_pending_includes_saas_numeric_state_bill(self):
        from local_app.timeutil import today_str
        with tempfile.TemporaryDirectory() as tmp:
            db, client = _client(tmp)
            day = today_str()
            with connect(db) as conn:
                conn.execute(
                    "insert into bills(bill_id, patient_identity, total_fee, paid_fee, state, "
                    "bill_time, source_json, updated_at) values "
                    f"('saas-pend-1', 'p1', 300, 100, '1', '{day} 09:00:00', '{{}}', '{day} 09:00:00')")
                # 作废的 SaaS 单(state=900)不得计入
                conn.execute(
                    "insert into bills(bill_id, patient_identity, total_fee, paid_fee, state, "
                    "bill_time, source_json, updated_at) values "
                    f"('saas-void-1', 'p1', 999, 0, '900', '{day} 09:30:00', '{{}}', '{day} 09:30:00')")
                conn.commit()
            d = client.get(f"/api/reports/today-collection?date={day}").json()
            self.assertEqual(d["pending_count"], 1, d)
            self.assertEqual(d["pending_total"], 200, "净欠费=300-100=200")
