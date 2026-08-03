"""#656 回归:今日工作检查器的 billing.view 分权必须挡住一切金额文本。
护士(有 patient.view 无 billing.view)调 /api/today-inspection:账单检查 detail 不得
出现"欠费N元"等具体金额;管理员照常可见。合成库复现确认的权限泄露。"""
import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from local_app import auth
from local_app.api import create_app
from local_app.db import connect, init_db


class InspectionBillingPermTest(unittest.TestCase):
    def _make(self, tmp):
        db = Path(tmp) / "clinic.sqlite3"
        init_db(db)
        auth.ensure_seed_roles(db)   # 预置角色默认权限(护士=有patient.view无billing.view)
        today = dt.date.today().isoformat()
        with connect(db) as conn:
            auth.create_user(conn, "boss", "院长", "admin123", role="admin")
            auth.create_user(conn, "hs", "护士", "pw123456", role="nurse")
            conn.execute("insert into patients(patient_identity, display_name, updated_at, current_hash) "
                         "values ('p1','张三','x','h1')")
            conn.execute("insert into bills(bill_id, patient_identity, bill_time, total_fee, paid_fee, state, updated_at) "
                         f"values ('b1','p1','{today} 10:00:00',100,0,'pending','x')")
            # #660:处置单+带金额项目——item_summary/金额检查detail 也不得向无权角色泄露金额
            conn.execute("insert into treatment_orders(order_id, order_no, patient_identity, order_date, status, bill_id, updated_at) "
                         f"values ('o1','CZ1','p1','{today} 10:00:00','priced','b1','x')")
            conn.execute("insert into treatment_items(treatment_item_id, order_id, patient_identity, item_name, unit_price, quantity, total_fee, updated_at) "
                         "values ('ti1','o1','p1','根管治疗',100,1,100,'x')")
            conn.commit()
        return db

    def _login(self, db, user, pw):
        client = TestClient(create_app(db))
        client.post("/api/auth/login", json={"username": user, "password": pw})
        return client

    def test_nurse_sees_no_amount_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = self._make(tmp)
            body = self._login(db, "hs", "pw123456").get("/api/today-inspection").json()
            dump = json.dumps(body, ensure_ascii=False)
            self.assertNotIn("欠费1", dump, "无 billing.view 不得泄露具体金额")
            self.assertNotIn("100.0", dump)
            # #689(收严):财务检查项对无 billing.view 一律不生成,连中性"有未结清账单"也不外泄
            self.assertNotIn("有未结清账单", dump)
            self.assertNotIn("收款", dump)
            # #660:处置单侧同样零金额——item_summary 只留项目名
            self.assertNotIn("元)", dump)
            self.assertNotIn("应收", dump)
            self.assertNotIn("≠", dump)
            self.assertIn("根管治疗", dump)   # 项目名保留,前台仍知道做了什么

    def test_admin_sees_amounts(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = self._make(tmp)
            body = self._login(db, "boss", "admin123").get("/api/today-inspection").json()
            self.assertIn("欠费", json.dumps(body, ensure_ascii=False))

    def _make_discounted(self, tmp):
        """打折已结清单:总费用1000、优惠300(净应收700)、项目700、实收700 —— 合法,不该报金额异常。"""
        db = Path(tmp) / "clinic.sqlite3"
        init_db(db)
        auth.ensure_seed_roles(db)
        today = dt.date.today().isoformat()
        with connect(db) as conn:
            auth.create_user(conn, "boss", "院长", "admin123", role="admin")
            auth.create_user(conn, "hs", "护士", "pw123456", role="nurse")
            conn.execute("insert into patients(patient_identity, display_name, updated_at, current_hash) "
                         "values ('p1','张三','x','h1')")
            conn.execute("insert into bills(bill_id, patient_identity, bill_time, total_fee, paid_fee, state, source_json, updated_at) "
                         f"values ('b1','p1','{today} 10:00:00',1000,700,'paid',?,'x')",
                         (json.dumps({"DiscountFee": "300"}),))
            conn.execute("insert into treatment_orders(order_id, order_no, patient_identity, order_date, status, bill_id, updated_at) "
                         f"values ('o1','CZ1','p1','{today} 10:00:00','paid','b1','x')")
            conn.execute("insert into treatment_items(treatment_item_id, order_id, patient_identity, item_name, unit_price, quantity, total_fee, updated_at) "
                         "values ('ti1','o1','p1','根管治疗',700,1,700,'x')")
            conn.execute("insert into payments(payment_id, patient_identity, bill_id, pay_time, amount, state, pay_type, source_json, updated_at) "
                         f"values ('pay1','p1','b1','{today} 10:30:00',700,'paid','现金','{{}}','x')")
            # 已到诊+已收款的预约:验证预约区收款检查也按 billing.view 门控(#689)
            conn.execute("insert into appointments(appointment_id, patient_identity, start_time, status, updated_at) "
                         f"values ('a1','p1','{today} 10:00:00','已到诊','x')")
            conn.commit()
        return db

    def test_discounted_paid_bill_no_amount_alarm(self):
        # #688:打折已结清单不得被误报金额异常/待收款(净应收700=项目700=实收700)
        with tempfile.TemporaryDirectory() as tmp:
            db = self._make_discounted(tmp)
            body = self._login(db, "boss", "admin123").get("/api/today-inspection").json()
            dump = json.dumps(body, ensure_ascii=False)
            self.assertNotIn("≠", dump, "#688:净应收口径下打折单不应报金额不一致")
            self.assertEqual(body["summary"]["error"], 0, "#688:合法打折单不应产生金额异常error")
            for o in body["orders"]:
                self.assertNotEqual(o["overall"], "error")

    def test_nurse_no_financial_check_items(self):
        # #689:护士(无 billing.view)响应里不得出现 金额/收款 检查项,也不因财务项增加 error
        with tempfile.TemporaryDirectory() as tmp:
            db = self._make_discounted(tmp)
            body = self._login(db, "hs", "pw123456").get("/api/today-inspection").json()
            for group in ("appointments", "orders", "bills"):
                for item in body.get(group, []):
                    names = [c["check"] for c in item.get("checks", [])]
                    self.assertNotIn("金额", names, "#689:无billing.view不应出现金额检查项")
                    self.assertNotIn("收款", names, "#689:无billing.view一律不应出现收款检查项(连ok也泄露收款事实)")


if __name__ == "__main__":
    unittest.main()
