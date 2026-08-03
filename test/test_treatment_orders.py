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
    with connect(db) as conn:
        auth.create_user(conn, "admin", "管理员", "admin123", role="admin")  # P1-13：退费仅 admin
        conn.execute(
            "insert into patients(patient_identity, display_name, updated_at, current_hash) "
            "values ('p1', '测试患者', '2026-06-07 00:00:00', 'h1')"
        )
        conn.commit()
    client = TestClient(create_app(db))
    client.post("/api/auth/login", json={"username": "admin", "password": "admin123"})
    return db, client


def _order_body(**over):
    body = {
        "doctor_name": "王医生", "diagnosis": "牙髓炎",
        "items": [
            {"item_name": "根管治疗", "tooth": "16", "unit_price": 800, "quantity": 1},
            {"item_name": "树脂充填", "tooth": "16", "unit_price": 200, "quantity": 2},
        ],
    }
    body.update(over)
    return body


def _create(client, **over):
    return client.post("/api/patients/p1/treatment-orders", json=_order_body(**over))


class TreatmentOrderCreateTest(unittest.TestCase):
    def test_create_records_order_no_bill(self):
        with tempfile.TemporaryDirectory() as tmp:
            db, client = _client(tmp)
            resp = _create(client)
            self.assertEqual(resp.status_code, 200)
            oid = resp.json()["order_id"]
            self.assertEqual(resp.json()["status"], "recorded")
            with connect(db) as conn:
                o = conn.execute("select status, bill_id from treatment_orders where order_id=?", (oid,)).fetchone()
                items = conn.execute("select count(*) c from treatment_items where order_id=?", (oid,)).fetchone()["c"]
                bills = conn.execute("select count(*) c from bills").fetchone()["c"]
            self.assertEqual(o["status"], "recorded")
            self.assertEqual(o["bill_id"], "")
            self.assertEqual(items, 2)
            self.assertEqual(bills, 0)  # 新增处置不出收费单

    def test_create_persists_tooth_codes_column(self):
        # 扫荡#390:牙位必须落进 treatment_items.tooth_codes 规范列(不只 source_json),否则丢牙位
        with tempfile.TemporaryDirectory() as tmp:
            db, client = _client(tmp)
            oid = _create(client).json()["order_id"]
            with connect(db) as conn:
                teeth = [r["tooth_codes"] for r in conn.execute(
                    "select tooth_codes from treatment_items where order_id=? order by item_name", (oid,))]
            # _order_body 两条都填了 tooth=16
            self.assertTrue(all(t == "16" for t in teeth), f"tooth_codes 应落库 16,得到 {teeth}")

    def test_empty_items_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            db, client = _client(tmp)
            self.assertEqual(client.post("/api/patients/p1/treatment-orders", json={"items": []}).status_code, 400)

    def test_patient_not_found(self):
        with tempfile.TemporaryDirectory() as tmp:
            db, client = _client(tmp)
            self.assertEqual(client.post("/api/patients/nope/treatment-orders", json=_order_body()).status_code, 404)

    def test_non_integer_quantity_rejected(self):
        # #557:数量非整数(如 "2.5"/2.9)拒绝报错,不静默按1/截断少收钱;整数值照常
        with tempfile.TemporaryDirectory() as tmp:
            db, client = _client(tmp)

            def post_qty(q):
                return client.post("/api/patients/p1/treatment-orders", json={
                    "doctor_name": "王医生",
                    "items": [{"item_name": "洁牙", "unit_price": 100, "quantity": q}]}).status_code
            self.assertEqual(post_qty("2.5"), 400)
            self.assertEqual(post_qty(2.9), 400)
            self.assertEqual(post_qty("abc"), 400)
            self.assertEqual(post_qty("1e3"), 400)   # 科学计数法拒绝(#557复核🟡)
            self.assertEqual(post_qty("2"), 200)     # 整数值字符串照常
            self.assertEqual(post_qty(3), 200)
            self.assertEqual(post_qty("2.0"), 200)   # 整数值的小数写法照常
            self.assertEqual(post_qty("2.00"), 200)

    def test_list_shows_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            db, client = _client(tmp)
            _create(client)
            data = client.get("/api/patients/p1/treatment-orders").json()
            self.assertEqual(len(data["orders"]), 1)
            self.assertEqual(data["orders"][0]["status"], "recorded")
            self.assertEqual(len(data["orders"][0]["items"]), 2)


class TreatmentOrderFeeTypeTest(unittest.TestCase):
    def test_create_stores_fee_type_and_list_returns_it(self):
        # #7：开单时把处置的费用分类(fee_type)落到 treatment_items，列表也返回
        with tempfile.TemporaryDirectory() as tmp:
            db, client = _client(tmp)
            resp = client.post("/api/patients/p1/treatment-orders", json={
                "doctor_name": "王医生", "diagnosis": "龋齿",
                "items": [
                    {"item_name": "树脂充填", "unit_price": 200, "quantity": 1, "fee_type": "补牙费"},
                    {"item_name": "根管治疗", "unit_price": 800, "quantity": 1, "fee_type": "牙髓费"},
                ],
            })
            self.assertEqual(resp.status_code, 200)
            oid = resp.json()["order_id"]
            with connect(db) as conn:
                rows = {r["item_name"]: r["fee_type"] for r in conn.execute(
                    "select item_name, fee_type from treatment_items where order_id=?", (oid,))}
            self.assertEqual(rows["树脂充填"], "补牙费")
            self.assertEqual(rows["根管治疗"], "牙髓费")
            items = client.get("/api/patients/p1/treatment-orders").json()["orders"][0]["items"]
            ft = {i["item_name"]: i.get("fee_type") for i in items}
            self.assertEqual(ft["树脂充填"], "补牙费")
            self.assertEqual(ft["根管治疗"], "牙髓费")


class TreatmentOrderStaffTest(unittest.TestCase):
    def test_create_stores_team_and_list_returns(self):
        # #4：配台人员(护士/咨询师/助理)落库 + 列表返回
        with tempfile.TemporaryDirectory() as tmp:
            db, client = _client(tmp)
            resp = client.post("/api/patients/p1/treatment-orders", json=_order_body(
                doctor_name="王医生", nurse_name="李护士",
                consultant_name="周咨询", assistant_name="赵助理"))
            self.assertEqual(resp.status_code, 200)
            oid = resp.json()["order_id"]
            with connect(db) as conn:
                o = conn.execute("select doctor_name, nurse_name, consultant_name, assistant_name "
                                 "from treatment_orders where order_id=?", (oid,)).fetchone()
            self.assertEqual(o["nurse_name"], "李护士")
            self.assertEqual(o["consultant_name"], "周咨询")
            self.assertEqual(o["assistant_name"], "赵助理")
            order = client.get("/api/patients/p1/treatment-orders").json()["orders"][0]
            self.assertEqual(order["nurse_name"], "李护士")
            self.assertEqual(order["consultant_name"], "周咨询")
            self.assertEqual(order["assistant_name"], "赵助理")


class TreatmentOrderPriceNowTest(unittest.TestCase):
    def test_create_with_price_now_generates_pending_bill(self):
        # #3：开单并划价 → 一步到位生成待收费单(全价)
        with tempfile.TemporaryDirectory() as tmp:
            db, client = _client(tmp)
            resp = client.post("/api/patients/p1/treatment-orders",
                               json=_order_body(price_now=True))
            self.assertEqual(resp.status_code, 200)
            body = resp.json()
            self.assertEqual(body["status"], "priced")
            self.assertTrue(body["bill_id"])
            with connect(db) as conn:
                o = conn.execute("select status, bill_id from treatment_orders where order_id=?",
                                 (body["order_id"],)).fetchone()
                bill = conn.execute("select state, total_fee from bills where bill_id=?",
                                    (o["bill_id"],)).fetchone()
            self.assertEqual(o["status"], "priced")
            self.assertEqual(bill["state"], "pending")
            self.assertEqual(bill["total_fee"], 1200)  # 800 + 200*2

    def test_create_without_price_now_stays_recorded(self):
        with tempfile.TemporaryDirectory() as tmp:
            db, client = _client(tmp)
            body = _create(client).json()
            self.assertEqual(body["status"], "recorded")
            self.assertNotIn("bill_id", body) if "bill_id" not in body else self.assertFalse(body.get("bill_id"))
            with connect(db) as conn:
                bills = conn.execute("select count(*) c from bills").fetchone()["c"]
            self.assertEqual(bills, 0)


class TreatmentOrderPriceTest(unittest.TestCase):
    def _make(self, client):
        oid = _create(client).json()["order_id"]
        items = client.get("/api/patients/p1/treatment-orders").json()["orders"][0]["items"]
        return oid, items

    def test_price_generates_pending_bill(self):
        with tempfile.TemporaryDirectory() as tmp:
            db, client = _client(tmp)
            oid, items = self._make(client)
            resp = client.post(f"/api/treatment-orders/{oid}/price", json={})
            self.assertEqual(resp.status_code, 200)
            # 800 + 200*2 = 1200
            self.assertEqual(resp.json()["total_fee"], 1200)
            self.assertEqual(resp.json()["status"], "priced")
            with connect(db) as conn:
                o = conn.execute("select status, bill_id from treatment_orders where order_id=?", (oid,)).fetchone()
                bill = conn.execute("select state, total_fee from bills where bill_id=?", (o["bill_id"],)).fetchone()
            self.assertEqual(o["status"], "priced")
            self.assertEqual(bill["state"], "pending")
            self.assertEqual(bill["total_fee"], 1200)

    def test_price_rounds_to_cents_and_bill_settles(self):
        # #550:折后带小数厘的行金额必须取整到分,否则账单永远收不齐卡在 partial
        with tempfile.TemporaryDirectory() as tmp:
            db, client = _client(tmp)
            oid = client.post("/api/patients/p1/treatment-orders", json={
                "items": [{"item_name": "洁牙", "unit_price": 19.9, "quantity": 3, "tooth": "16"}],
            }).json()["order_id"]
            iid = client.get("/api/patients/p1/treatment-orders").json()["orders"][0]["items"][0]["item_id"]
            resp = client.post(f"/api/treatment-orders/{oid}/price", json={
                "items": [{"item_id": iid, "amount": 50.745}],   # 59.7打85折,带小数厘
            })
            self.assertEqual(resp.status_code, 200)
            total = resp.json()["total_fee"]
            self.assertEqual(total, round(total, 2), "total_fee 必须取整到分")
            with connect(db) as conn:
                bill = conn.execute("select bill_id, total_fee from bills").fetchone()
                self.assertEqual(bill["total_fee"], round(bill["total_fee"], 2))
                line = conn.execute("select total_fee from treatment_items where order_id=?", (oid,)).fetchone()["total_fee"]
                self.assertEqual(line, round(line, 2), "行金额必须取整到分")
            # 付显示的待收金额后账单必须能结清(不再卡 partial)
            r = client.post(f"/api/bills/{bill['bill_id']}/pay", json={"methods": [{"method": "现金", "amount": total}], "request_id": uuid.uuid4().hex})
            self.assertEqual(r.status_code, 200, r.text)
            with connect(db) as conn:
                self.assertEqual(conn.execute(
                    "select state from bills where bill_id=?", (bill["bill_id"],)).fetchone()["state"], "paid")

    def test_refunded_bill_sets_effective_status_refunded(self):
        # #379:账单退费后,处置单 effective_status 应为 refunded(不再停在"待收费"显示撤销)
        with tempfile.TemporaryDirectory() as tmp:
            db, client = _client(tmp)
            oid, _ = self._make(client)
            client.post(f"/api/treatment-orders/{oid}/price", json={})
            with connect(db) as conn:
                bill_id = conn.execute(
                    "select bill_id from treatment_orders where order_id=?", (oid,)).fetchone()[0]
                conn.execute("update bills set state='refunded', paid_fee=0 where bill_id=?", (bill_id,))
                conn.commit()
            order = client.get("/api/patients/p1/treatment-orders").json()["orders"][0]
            self.assertEqual(order["status"], "priced")          # 底层 status 不变
            self.assertEqual(order["effective_status"], "refunded")  # 展示态变已退费

    def test_per_item_full_refund_sets_effective_status_refunded(self):
        # 扫荡#392:按处置(逐项)全退后,处置单 effective_status 也应为 refunded(不止整单退费分支)
        from local_app import auth
        with tempfile.TemporaryDirectory() as tmp:
            db, client = _client(tmp)
            oid = client.post("/api/patients/p1/treatment-orders", json={
                "items": [{"item_name": "根管", "unit_price": 800, "quantity": 1, "tooth": "16"}]}).json()["order_id"]
            bill = client.post(f"/api/treatment-orders/{oid}/price", json={}).json()["bill_id"]
            client.post(f"/api/bills/{bill}/pay", json={"methods": [{"method": "现金", "amount": 800}], "request_id": uuid.uuid4().hex})
            with connect(db) as conn:
                iid = conn.execute("select treatment_item_id from treatment_items where bill_id=?", (bill,)).fetchone()[0]
            r = client.post(f"/api/bills/{bill}/refund",
                            json={"refund_reason": "测", "items": [{"treatment_item_id": iid, "amount": 800}], "request_id": uuid.uuid4().hex})
            self.assertEqual(r.status_code, 200)
            order = client.get("/api/patients/p1/treatment-orders").json()["orders"][0]
            self.assertEqual(order["effective_status"], "refunded")

    def test_price_free_and_discount_and_overall(self):
        with tempfile.TemporaryDirectory() as tmp:
            db, client = _client(tmp)
            oid, items = self._make(client)
            root = [i for i in items if i["item_name"] == "根管治疗"][0]
            resin = [i for i in items if i["item_name"] == "树脂充填"][0]
            # 根管免费(0)，树脂打8折(200*2*0.8=320)，整单再优惠20 → 0+320-20=300
            resp = client.post(f"/api/treatment-orders/{oid}/price", json={
                "discount": 20,
                "items": [
                    {"item_id": root["item_id"], "free": True},
                    {"item_id": resin["item_id"], "amount": 320},   # 200*2打8折

                ],
            })
            self.assertEqual(resp.status_code, 200)
            self.assertEqual(resp.json()["total_fee"], 300)

    def test_price_direct_amount_per_item(self):
        # #1：每行直接填"本项金额"(折后实收价)，后端按 amount 落价，不用传折率
        with tempfile.TemporaryDirectory() as tmp:
            db, client = _client(tmp)
            oid, items = self._make(client)
            root = [i for i in items if i["item_name"] == "根管治疗"][0]
            resin = [i for i in items if i["item_name"] == "树脂充填"][0]
            # 根管原800直填500，树脂原400直填300 → 800
            resp = client.post(f"/api/treatment-orders/{oid}/price", json={
                "items": [
                    {"item_id": root["item_id"], "amount": 500},
                    {"item_id": resin["item_id"], "amount": 300},
                ],
            })
            self.assertEqual(resp.status_code, 200)
            self.assertEqual(resp.json()["total_fee"], 800)
            with connect(db) as conn:
                lines = {r["treatment_item_id"]: r["total_fee"] for r in conn.execute(
                    "select treatment_item_id, total_fee from treatment_items where order_id=?", (oid,))}
            self.assertEqual(lines[root["item_id"]], 500)
            self.assertEqual(lines[resin["item_id"]], 300)

    def test_price_free_flag_zeroes_line(self):
        # #1：免单勾选(free:true) → 该行0，优先于 amount
        with tempfile.TemporaryDirectory() as tmp:
            db, client = _client(tmp)
            oid, items = self._make(client)
            root = [i for i in items if i["item_name"] == "根管治疗"][0]
            resin = [i for i in items if i["item_name"] == "树脂充填"][0]
            resp = client.post(f"/api/treatment-orders/{oid}/price", json={
                "items": [
                    {"item_id": root["item_id"], "free": True, "amount": 500},
                    {"item_id": resin["item_id"], "amount": 300},
                ],
            })
            self.assertEqual(resp.status_code, 200)
            self.assertEqual(resp.json()["total_fee"], 300)  # 根管免单0 + 树脂300

    def test_price_invalid_amount_rejected(self):
        # 架构铁律#禁止兜底：非法金额(负/nan/非数/空)一律400报错，不再静默退回原价
        with tempfile.TemporaryDirectory() as tmp:
            db, client = _client(tmp)
            oid, items = self._make(client)
            root = [i for i in items if i["item_name"] == "根管治疗"][0]
            for bad in (-5, "nan", "abc", ""):
                resp = client.post(f"/api/treatment-orders/{oid}/price", json={
                    "items": [{"item_id": root["item_id"], "amount": bad}],
                })
                self.assertEqual(resp.status_code, 400, f"amount={bad!r} 应400")
            # 全部被拒，处置单仍是待划价、无账单产生
            with connect(db) as conn:
                o = conn.execute("select status, bill_id from treatment_orders where order_id=?", (oid,)).fetchone()
                bills = conn.execute("select count(*) c from bills").fetchone()["c"]
            self.assertEqual(o["status"], "recorded")
            self.assertEqual(bills, 0)

    def test_price_invalid_discount_rejected(self):
        # 架构铁律#禁止兜底：整单优惠非法(负/非数/inf)一律400，不再静默当0
        with tempfile.TemporaryDirectory() as tmp:
            db, client = _client(tmp)
            oid, _ = self._make(client)
            for bad in (-1, "abc", "inf"):
                resp = client.post(f"/api/treatment-orders/{oid}/price", json={"discount": bad})
                self.assertEqual(resp.status_code, 400, f"discount={bad!r} 应400")

    def test_price_invalid_unit_price_rejected(self):
        # 架构铁律#禁止兜底：划价时改单价传非法值(负/非数)一律400，不再静默保留原单价
        with tempfile.TemporaryDirectory() as tmp:
            db, client = _client(tmp)
            oid, items = self._make(client)
            root = [i for i in items if i["item_name"] == "根管治疗"][0]
            for bad in (-1, "abc"):
                resp = client.post(f"/api/treatment-orders/{oid}/price", json={
                    "items": [{"item_id": root["item_id"], "unit_price": bad}],
                })
                self.assertEqual(resp.status_code, 400, f"unit_price={bad!r} 应400")

    def test_price_twice_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            db, client = _client(tmp)
            oid, _ = self._make(client)
            client.post(f"/api/treatment-orders/{oid}/price", json={})
            self.assertEqual(client.post(f"/api/treatment-orders/{oid}/price", json={}).status_code, 409)

    def test_price_order_not_found(self):
        with tempfile.TemporaryDirectory() as tmp:
            db, client = _client(tmp)
            self.assertEqual(client.post("/api/treatment-orders/nope/price", json={}).status_code, 404)

    def test_nan_inf_rejected(self):
        # #26：nan/inf 非有限金额返回 400 而非 500
        with tempfile.TemporaryDirectory() as tmp:
            db, client = _client(tmp)
            r = client.post("/api/patients/p1/treatment-orders",
                            json={"items": [{"item_name": "X", "unit_price": "nan", "quantity": 1}]})
            self.assertEqual(r.status_code, 400)
            oid = _create(client).json()["order_id"]
            r2 = client.post(f"/api/treatment-orders/{oid}/price", json={"discount": "inf"})
            # 架构铁律#禁止兜底：inf 优惠 400 报错，不再静默规整为 0
            self.assertEqual(r2.status_code, 400)

    def test_all_free_settles_directly(self):
        # #27：全免费/0元 → 直接已收费，不需走收款
        with tempfile.TemporaryDirectory() as tmp:
            db, client = _client(tmp)
            oid, items = self._make(client)
            adj = [{"item_id": i["item_id"], "free": True} for i in items]
            resp = client.post(f"/api/treatment-orders/{oid}/price", json={"items": adj})
            self.assertEqual(resp.json()["total_fee"], 0)
            order = client.get("/api/patients/p1/treatment-orders").json()["orders"][0]
            self.assertEqual(order["effective_status"], "paid")


class TreatmentOrderVoidTest(unittest.TestCase):
    def test_void_recorded_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            db, client = _client(tmp)
            oid = _create(client).json()["order_id"]
            resp = client.post(f"/api/treatment-orders/{oid}/void", json={"reason": "录错"})
            self.assertEqual(resp.status_code, 200)
            with connect(db) as conn:
                self.assertEqual(conn.execute("select status from treatment_orders where order_id=?", (oid,)).fetchone()["status"], "voided")

    def test_void_priced_unpaid_voids_bill(self):
        with tempfile.TemporaryDirectory() as tmp:
            db, client = _client(tmp)
            oid = _create(client).json()["order_id"]
            client.post(f"/api/treatment-orders/{oid}/price", json={})
            resp = client.post(f"/api/treatment-orders/{oid}/void", json={"reason": "划价错"})
            self.assertEqual(resp.status_code, 200)

    def test_void_paid_order_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            db, client = _client(tmp)
            oid = _create(client).json()["order_id"]
            priced = client.post(f"/api/treatment-orders/{oid}/price", json={}).json()
            client.post(f"/api/bills/{priced['bill_id']}/pay", json={"methods": [{"method": "现金", "amount": priced["total_fee"]}], "request_id": uuid.uuid4().hex})
            resp = client.post(f"/api/treatment-orders/{oid}/void", json={"reason": "x"})
            self.assertEqual(resp.status_code, 409)

    def test_void_refunded_bill_rejected(self):
        # 审查#2/#3：已整单退费(state=refunded,paid_fee=0)的处置单不能再撤销,否则退费终态被覆盖
        with tempfile.TemporaryDirectory() as tmp:
            db, client = _client(tmp)
            oid = _create(client).json()["order_id"]
            priced = client.post(f"/api/treatment-orders/{oid}/price", json={}).json()
            bill = priced["bill_id"]
            client.post(f"/api/bills/{bill}/pay", json={"methods": [{"method": "现金", "amount": priced["total_fee"]}], "request_id": uuid.uuid4().hex})
            client.post(f"/api/bills/{bill}/refund", json={"refund_reason": "整单退", "request_id": uuid.uuid4().hex})
            resp = client.post(f"/api/treatment-orders/{oid}/void", json={"reason": "x"})
            self.assertEqual(resp.status_code, 409)
            with connect(db) as conn:
                st = conn.execute("select state from bills where bill_id=?", (bill,)).fetchone()[0]
            self.assertEqual(st, "refunded")   # 状态仍是 refunded,未被覆盖成 voided

    def test_void_zero_settled_rejected(self):
        # 审查#12：0元已结清(state=paid,paid_fee=0)不能撤销,绕过"已收费不能撤销"红线
        with tempfile.TemporaryDirectory() as tmp:
            db, client = _client(tmp)
            oid = client.post("/api/patients/p1/treatment-orders", json={
                "items": [{"item_name": "免费复查", "unit_price": 0, "quantity": 1}]}).json()["order_id"]
            client.post(f"/api/treatment-orders/{oid}/price", json={})  # 0元→直接 state=paid
            resp = client.post(f"/api/treatment-orders/{oid}/void", json={"reason": "x"})
            self.assertEqual(resp.status_code, 409)

    def test_void_empty_reason_rejected(self):
        # #48：撤销必须填理由，空理由(含API直传{})返回400
        with tempfile.TemporaryDirectory() as tmp:
            db, client = _client(tmp)
            oid = _create(client).json()["order_id"]
            self.assertEqual(client.post(f"/api/treatment-orders/{oid}/void", json={}).status_code, 400)
            self.assertEqual(client.post(f"/api/treatment-orders/{oid}/void", json={"reason": "  "}).status_code, 400)

    def test_paid_status_derived(self):
        with tempfile.TemporaryDirectory() as tmp:
            db, client = _client(tmp)
            oid = _create(client).json()["order_id"]
            priced = client.post(f"/api/treatment-orders/{oid}/price", json={}).json()
            client.post(f"/api/bills/{priced['bill_id']}/pay", json={"methods": [{"method": "现金", "amount": priced["total_fee"]}], "request_id": uuid.uuid4().hex})
            order = client.get("/api/patients/p1/treatment-orders").json()["orders"][0]
            self.assertEqual(order["effective_status"], "paid")

    def test_void_records_reason_in_audit(self):
        # #9：撤销填理由 → 记入审计
        with tempfile.TemporaryDirectory() as tmp:
            db, client = _client(tmp)
            oid = _create(client).json()["order_id"]
            resp = client.post(f"/api/treatment-orders/{oid}/void", json={"reason": "录错患者"})
            self.assertEqual(resp.status_code, 200)
            with connect(db) as conn:
                new_json = conn.execute(
                    "select new_json from audit_logs where entity_id=? and action='void_treatment_order'",
                    (oid,)).fetchone()["new_json"]
            self.assertIn("录错患者", new_json)

    def test_void_by_bill_voids_order(self):
        # #9：收费处也能撤 → 经 bill_id 找到处置单撤销(未收费)
        with tempfile.TemporaryDirectory() as tmp:
            db, client = _client(tmp)
            oid = _create(client).json()["order_id"]
            bill_id = client.post(f"/api/treatment-orders/{oid}/price", json={}).json()["bill_id"]
            resp = client.post(f"/api/bills/{bill_id}/void-order", json={"reason": "划价有误"})
            self.assertEqual(resp.status_code, 200)
            with connect(db) as conn:
                self.assertEqual(conn.execute("select status from treatment_orders where order_id=?", (oid,)).fetchone()["status"], "voided")
                self.assertEqual(conn.execute("select state from bills where bill_id=?", (bill_id,)).fetchone()["state"], "voided")

    def test_void_by_bill_paid_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            db, client = _client(tmp)
            oid = _create(client).json()["order_id"]
            priced = client.post(f"/api/treatment-orders/{oid}/price", json={}).json()
            client.post(f"/api/bills/{priced['bill_id']}/pay", json={"methods": [{"method": "现金", "amount": priced["total_fee"]}], "request_id": uuid.uuid4().hex})
            resp = client.post(f"/api/bills/{priced['bill_id']}/void-order", json={"reason": "x"})
            self.assertEqual(resp.status_code, 409)

    def test_void_by_bill_no_local_order_404(self):
        with tempfile.TemporaryDirectory() as tmp:
            db, client = _client(tmp)
            self.assertEqual(client.post("/api/bills/nope/void-order", json={}).status_code, 404)


class TodayVisitGateTest(unittest.TestCase):
    """先挂号后处置：today-visit 端点 + 处置默认带挂号医生。"""

    def _add_today_appt(self, db, doctor="王医生", status="已到诊"):
        import datetime as _dt
        today = _dt.date.today().isoformat()
        with connect(db) as conn:
            conn.execute(
                "insert into appointments(appointment_id, patient_identity, start_time, "
                "doctor_name, item_name, status, arrived_at, updated_at) "
                "values ('local-appt-t1', 'p1', ?, ?, '洗牙', ?, ?, ?)",
                (today + " 09:00:00", doctor, status, today + " 09:05:00", today + " 09:05:00"),
            )
            conn.commit()

    def test_today_visit_false_when_no_appt(self):
        with tempfile.TemporaryDirectory() as tmp:
            db, client = _client(tmp)
            self.assertFalse(client.get("/api/patients/p1/today-visit").json()["has_today"])

    def test_today_visit_true_with_doctor(self):
        with tempfile.TemporaryDirectory() as tmp:
            db, client = _client(tmp)
            self._add_today_appt(db, doctor="王医生")
            d = client.get("/api/patients/p1/today-visit").json()
            self.assertTrue(d["has_today"])
            self.assertEqual(d["doctor_name"], "王医生")

    def test_today_visit_excludes_cancelled(self):
        with tempfile.TemporaryDirectory() as tmp:
            db, client = _client(tmp)
            self._add_today_appt(db, status="已取消")
            self.assertFalse(client.get("/api/patients/p1/today-visit").json()["has_today"])

    def test_today_visit_true_for_triaged_and_finished(self):
        # #542:已分诊/已完成的今日预约同样算"今天已挂号",不得再弹挂号横幅
        for status in ("已分诊", "已完成"):
            with tempfile.TemporaryDirectory() as tmp:
                db, client = _client(tmp)
                self._add_today_appt(db, status=status)
                d = client.get("/api/patients/p1/today-visit").json()
                self.assertTrue(d["has_today"], f"status={status} 应算已挂号")
                self.assertTrue(d["arrived"])

    def test_today_visit_false_for_yesterday_appt(self):
        # #542 机理留档:预约若落在昨天(如 #541 修前前端用漂移时区建的日期),
        # 后端按北京"今天"查不到→横幅误弹。日期口径由 #541 前后端统一北京修复。
        import datetime as _dt
        yesterday = (_dt.date.today() - _dt.timedelta(days=1)).isoformat()
        with tempfile.TemporaryDirectory() as tmp:
            db, client = _client(tmp)
            with connect(db) as conn:
                conn.execute(
                    "insert into appointments(appointment_id, patient_identity, start_time, "
                    "doctor_name, item_name, status, arrived_at, updated_at) "
                    "values ('local-appt-y1', 'p1', ?, '王医生', '洗牙', '已到诊', ?, ?)",
                    (yesterday + " 09:00:00", yesterday + " 09:05:00", yesterday + " 09:05:00"),
                )
                conn.commit()
            self.assertFalse(client.get("/api/patients/p1/today-visit").json()["has_today"])

    def test_order_defaults_to_registration_doctor(self):
        # 不传 doctor_name → 兜底取今日挂号医生
        with tempfile.TemporaryDirectory() as tmp:
            db, client = _client(tmp)
            self._add_today_appt(db, doctor="王医生")
            oid = _create(client, doctor_name="").json()["order_id"]
            with connect(db) as conn:
                row = conn.execute("select doctor_name from treatment_orders where order_id = ?", (oid,)).fetchone()
                item = conn.execute("select doctor_name from treatment_items where order_id = ?", (oid,)).fetchone()
            self.assertEqual(row["doctor_name"], "王医生")
            self.assertEqual(item["doctor_name"], "王医生")

    def test_explicit_doctor_not_overridden(self):
        with tempfile.TemporaryDirectory() as tmp:
            db, client = _client(tmp)
            self._add_today_appt(db, doctor="王医生")
            oid = _create(client, doctor_name="李医生").json()["order_id"]
            with connect(db) as conn:
                row = conn.execute("select doctor_name from treatment_orders where order_id = ?", (oid,)).fetchone()
            self.assertEqual(row["doctor_name"], "李医生")

    def test_order_no_appt_doctor_stays_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            db, client = _client(tmp)
            oid = _create(client, doctor_name="").json()["order_id"]
            with connect(db) as conn:
                row = conn.execute("select doctor_name from treatment_orders where order_id = ?", (oid,)).fetchone()
            self.assertEqual(row["doctor_name"], "")


    def test_create_assigns_local_order_no(self):
        # 本地处置单号 CZ+YYMMDD+流水(避开SaaS的B号),同日递增
        with tempfile.TemporaryDirectory() as tmp:
            db, client = _client(tmp)
            r1 = _create(client).json()
            r2 = _create(client).json()
            self.assertTrue(r1["order_no"].startswith("CZ"), r1["order_no"])
            self.assertEqual(len(r1["order_no"]), 12)
            self.assertEqual(int(r2["order_no"][8:]), int(r1["order_no"][8:]) + 1)


class TreatmentOrderEmptyPriceTest(unittest.TestCase):
    def test_empty_order_cannot_price(self):
        import datetime
        today = datetime.date.today().isoformat()
        with tempfile.TemporaryDirectory() as tmp:
            db, client = _client(tmp)
            with connect(db) as conn:
                conn.execute("insert into appointments(appointment_id, patient_identity, start_time, status, "
                             "arrived_at, updated_at) values ('a1','p1',?, '已到达',?, 'x')",
                             (today+" 09:00", today+" 09:05"))
                conn.commit()
            oid = client.post("/api/patients/p1/treatment-orders/ensure-arrival").json()["order_id"]
            r = client.post(f"/api/treatment-orders/{oid}/price", json={})
            self.assertEqual(r.status_code, 409)   # 空单不能划价


class TreatmentOrderCloseOnLeaveTest(unittest.TestCase):
    def test_edit_rejected_after_patient_left(self):
        import datetime
        today = datetime.date.today().isoformat()
        with tempfile.TemporaryDirectory() as tmp:
            db, client = _client(tmp)
            oid = _create(client).json()["order_id"]
            with connect(db) as conn:
                conn.execute("insert into appointments(appointment_id, patient_identity, start_time, status, "
                             "arrived_at, finished_at, updated_at) values ('a1','p1',?, '患者离开',?,?, 'x')",
                             (today+" 09:00", today+" 09:05", today+" 10:00"))
                conn.commit()
            orders = client.get("/api/patients/p1/treatment-orders").json()["orders"]
            self.assertTrue(orders[0]["closed"])           # 列表标记已关闭
            r = client.put(f"/api/treatment-orders/{oid}",
                           json={"items": [{"item_name": "x", "unit_price": 1, "quantity": 1}]})
            self.assertEqual(r.status_code, 409)           # 离开后编辑被拒


class TreatmentOrderArrivalTest(unittest.TestCase):
    def _arrive(self, db, today):
        with connect(db) as conn:
            conn.execute("insert into appointments(appointment_id, patient_identity, start_time, doctor_name, status, arrived_at, updated_at) "
                         "values ('a1','p1',?, '王医生','已到达',?, 'x')", (today+" 09:00", today+" 09:05"))
            conn.commit()

    def test_ensure_arrival_skips_finished_visit(self):
        # 回归#235/#252:已离开(finished_at)的就诊不再自动建空单,只是查看已结束就诊不该冒空号单
        import datetime
        today = datetime.date.today().isoformat()
        with tempfile.TemporaryDirectory() as tmp:
            db, client = _client(tmp)
            with connect(db) as conn:
                conn.execute("insert into appointments(appointment_id, patient_identity, start_time, doctor_name, status, "
                             "arrived_at, finished_at, updated_at) values ('a1','p1',?, '王医生','患者离开',?,?, 'x')",
                             (today+" 09:00", today+" 09:05", today+" 10:00"))
                conn.commit()
            r = client.post("/api/patients/p1/treatment-orders/ensure-arrival").json()
            self.assertFalse(r["created"], "已离开就诊不该自动建空单")
            self.assertEqual(r.get("reason"), "已离开")
            self.assertEqual(len(client.get("/api/patients/p1/treatment-orders").json()["orders"]), 0)

    def test_ensure_arrival_creates_for_completed_not_left(self):
        # 用户需求:已完成(治疗做完但没离开,finished_at 已设)仍要有空处置单;只有已离开才不建
        import datetime
        today = datetime.date.today().isoformat()
        with tempfile.TemporaryDirectory() as tmp:
            db, client = _client(tmp)
            with connect(db) as conn:
                conn.execute("insert into appointments(appointment_id, patient_identity, start_time, doctor_name, status, "
                             "arrived_at, finished_at, updated_at) values ('a1','p1',?, '王医生','已完成',?,?, 'x')",
                             (today+" 09:00", today+" 09:05", today+" 10:00"))
                conn.commit()
            r = client.post("/api/patients/p1/treatment-orders/ensure-arrival").json()
            self.assertTrue(r["created"], "已完成(未离开)应仍建空处置单")
            self.assertEqual(len(client.get("/api/patients/p1/treatment-orders").json()["orders"]), 1)

    def test_completed_not_left_order_editable_left_locked(self):
        # #266:已完成(未离开)空处置单要能编辑录治疗(closed=false+PUT可改);已离开才锁定
        import datetime
        today = datetime.date.today().isoformat()
        with tempfile.TemporaryDirectory() as tmp:
            db, client = _client(tmp)
            with connect(db) as conn:
                conn.execute("insert into appointments(appointment_id, patient_identity, start_time, doctor_name, status, "
                             "arrived_at, finished_at, updated_at) values ('a1','p1',?, '王医生','已完成',?,?, 'x')",
                             (today+" 09:00", today+" 09:05", today+" 10:00"))
                conn.commit()
            oid = client.post("/api/patients/p1/treatment-orders/ensure-arrival").json()["order_id"]
            order = next(o for o in client.get("/api/patients/p1/treatment-orders").json()["orders"] if o["order_id"] == oid)
            self.assertFalse(order["closed"], "已完成未离开不该锁定")
            r = client.put(f"/api/treatment-orders/{oid}", json={"doctor_name": "王医生",
                "items": [{"item_name": "补牙", "tooth": "16", "unit_price": 200, "quantity": 1}]})
            self.assertEqual(r.status_code, 200, r.text)
            with connect(db) as conn:
                conn.execute("update appointments set status='患者离开' where appointment_id='a1'")
                conn.commit()
            order2 = next(o for o in client.get("/api/patients/p1/treatment-orders").json()["orders"] if o["order_id"] == oid)
            self.assertTrue(order2["closed"], "已离开应锁定")
            r2 = client.put(f"/api/treatment-orders/{oid}", json={"doctor_name": "王医生",
                "items": [{"item_name": "拔牙", "tooth": "16", "unit_price": 100, "quantity": 1}]})
            self.assertEqual(r2.status_code, 409, "已离开不可编辑")

    def test_ensure_arrival_creates_empty_numbered_order(self):
        import datetime
        today = datetime.date.today().isoformat()
        with tempfile.TemporaryDirectory() as tmp:
            db, client = _client(tmp)
            self._arrive(db, today)
            r = client.post("/api/patients/p1/treatment-orders/ensure-arrival").json()
            self.assertTrue(r["created"])
            self.assertTrue(r["order_no"].startswith("CZ"))
            r2 = client.post("/api/patients/p1/treatment-orders/ensure-arrival").json()
            self.assertFalse(r2["created"])           # 幂等,不重建
            self.assertEqual(r2["order_no"], r["order_no"])
            orders = client.get("/api/patients/p1/treatment-orders").json()["orders"]
            self.assertEqual(len(orders), 1)
            self.assertEqual(len(orders[0]["items"]), 0)   # 空单无明细

    def test_ensure_arrival_not_arrived_no_create(self):
        with tempfile.TemporaryDirectory() as tmp:
            db, client = _client(tmp)
            r = client.post("/api/patients/p1/treatment-orders/ensure-arrival").json()
            self.assertFalse(r["created"])             # 没到达不建

    def test_ensure_arrival_no_recreate_after_priced(self):
        # 回归bug:划价后单子变"待收费",ensure-arrival 不该再冒一张空单(只看待划价的旧逻辑会误建)
        import datetime
        today = datetime.date.today().isoformat()
        with tempfile.TemporaryDirectory() as tmp:
            db, client = _client(tmp)
            self._arrive(db, today)
            oid = _create(client).json()["order_id"]            # 有明细的待划价单
            self.assertEqual(client.post(f"/api/treatment-orders/{oid}/price", json={}).status_code, 200)
            r = client.post("/api/patients/p1/treatment-orders/ensure-arrival").json()
            self.assertFalse(r["created"], "今日已有已划价单,不该再自动建空单")
            orders = client.get("/api/patients/p1/treatment-orders").json()["orders"]
            self.assertEqual(len(orders), 1, "应只有那张已划价单,没冒出空单")
            self.assertEqual(orders[0]["status"], "priced")


class TreatmentOrderEditTest(unittest.TestCase):
    """编辑待划价处置单:原地改项目,不必撤销重建(避免堆重复单)。"""

    def test_edit_replaces_items_in_place(self):
        with tempfile.TemporaryDirectory() as tmp:
            db, client = _client(tmp)
            oid = _create(client).json()["order_id"]   # 原2项(根管800+树脂200x2)
            r = client.put(f"/api/treatment-orders/{oid}", json={
                "items": [{"item_name": "口腔全面检查", "tooth": "", "unit_price": 120, "quantity": 1}]})
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r.json()["item_count"], 1)
            with connect(db) as conn:
                names = [x[0] for x in conn.execute(
                    "select item_name from treatment_items where order_id=? and coalesce(is_deleted,0)=0", (oid,)).fetchall()]
                status = conn.execute("select status from treatment_orders where order_id=?", (oid,)).fetchone()[0]
                norders = conn.execute("select count(*) from treatment_orders").fetchone()[0]
            self.assertEqual(names, ["口腔全面检查"], "明细应被整批替换")
            self.assertEqual(status, "recorded", "仍是待划价")
            self.assertEqual(norders, 1, "原地改,不另起新单")

    def test_edit_rejected_when_consent_signed(self):
        # 回归#231/#251:已绑未作废知情同意书的处置单不能直接改项目(防签的同意书与实际处置错配)
        with tempfile.TemporaryDirectory() as tmp:
            db, client = _client(tmp)
            oid = _create(client).json()["order_id"]
            with connect(db) as conn:
                conn.execute("insert into consent_documents(document_id, patient_identity, order_id, status) "
                             "values ('cd1','p1',?, 'signed')", (oid,))
                conn.commit()
            r = client.put(f"/api/treatment-orders/{oid}",
                           json={"items": [{"item_name": "拔牙", "unit_price": 100, "quantity": 1}]})
            self.assertEqual(r.status_code, 409, "签了同意书不能直接改项目")
            with connect(db) as conn:   # 作废同意书后可改
                conn.execute("update consent_documents set status='voided' where document_id='cd1'")
                conn.commit()
            r2 = client.put(f"/api/treatment-orders/{oid}",
                            json={"items": [{"item_name": "拔牙", "unit_price": 100, "quantity": 1}]})
            self.assertEqual(r2.status_code, 200, "作废同意书后可改")

    def test_edit_priced_order_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            db, client = _client(tmp)
            oid = _create(client).json()["order_id"]
            self.assertEqual(client.post(f"/api/treatment-orders/{oid}/price", json={}).status_code, 200)
            r = client.put(f"/api/treatment-orders/{oid}", json={
                "items": [{"item_name": "改不了", "unit_price": 1, "quantity": 1}]})
            self.assertEqual(r.status_code, 409, "已划价单不可编辑")

    def test_edit_missing_order_404(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, client = _client(tmp)
            self.assertEqual(client.put("/api/treatment-orders/nope", json={
                "items": [{"item_name": "x", "unit_price": 1, "quantity": 1}]}).status_code, 404)
