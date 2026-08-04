"""店内统计 · 医生统计(病历为轴)口径回归。

合成诊室场景，锁住（含 #672~#676 复审修复）：
- 初诊/复诊 量按病历 record_type；
- 实收按收款时间 pay_time 落区间、剔除作废原单、退费负数冲减；
- 实收以区间内全部收款为全集，账单连不上就诊（含无病历患者）进「未归类」；
- 应收排除作废账单、扣 DiscountFee 净额；
- 账单→就诊：study 精确优先，退化 同患者+同日 取当日最早就诊。
"""
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
        for pid, name in [("p1", "张三"), ("p2", "李四"), ("p3", "王五")]:
            c.execute("insert into patients(patient_identity, display_name, updated_at, current_hash) "
                      "values (?,?,?,?)", (pid, name, "2026-07-01", "h_" + pid))
        c.commit()
    client = TestClient(create_app(db))
    client.post("/api/auth/login", json={"username": "admin", "password": "admin123"})
    return db, client


def _visit(c, rid, pid, doctor, vt, day, study="", hhmm="09:00:00", nurse=""):
    content = json.dumps({"Nurse": nurse}) if nurse else "{}"
    c.execute("insert into medical_records(record_id, patient_identity, study_identity, record_type, "
              "visit_time, doctor_name, content_json, current_hash) values (?,?,?,?,?,?,?,?)",
              (rid, pid, study, vt, day + " " + hhmm, doctor, content, "h_" + rid))


def _bill(c, bid, pid, day, total, study="", state="paid", discount=None):
    sj = {"BillStudyIdentity": study}
    if discount is not None:
        sj["DiscountFee"] = discount
    c.execute("insert into bills(bill_id, patient_identity, bill_time, total_fee, paid_fee, state, "
              "bill_doctor, source_json, updated_at) values (?,?,?,?,?,?,?,?,?)",
              (bid, pid, day + " 09:30:00", total, 0, state, "", json.dumps(sj), day))


def _pay(c, payid, pid, bid, day, amount, cancel_mark=None):
    sj = json.dumps({"CancelMark": cancel_mark} if cancel_mark else {})
    c.execute("insert into payments(payment_id, patient_identity, bill_id, pay_time, amount, state, "
              "pay_type, source_json, updated_at) values (?,?,?,?,?,?,?,?,?)",
              (payid, pid, bid, day + " 10:00:00", amount, "paid", "现金", sj, day))


class DoctorPerfTest(unittest.TestCase):
    def _q(self, client, start="2026-07-01", end="2026-07-31", **kw):
        p = {"role": "doctor", "start": start, "end": end, **kw}
        return client.get("/api/store-stats/role-perf?" + urlencode(p)).json()

    def _row(self, d, name):
        return [r for r in d["rows"] if r["name"] == name][0]

    def test_first_visit_exact_study_match(self):
        with tempfile.TemporaryDirectory() as tmp:
            db, client = _client(tmp)
            with connect(db) as c:
                _visit(c, "r1", "p1", "测试医生B", "初诊", "2026-07-02", study="S1")
                _bill(c, "b1", "p1", "2026-07-02", 1000, study="S1")
                _pay(c, "pay1", "p1", "b1", "2026-07-02", 800)
                c.commit()
            row = self._row(self._q(client), "测试医生B")
            self.assertEqual(row["first"]["visit"], 1)
            self.assertEqual(row["first"]["deal"], 1)
            self.assertEqual(row["first"]["paid"], 800.0)
            self.assertEqual(row["first"]["receivable"], 1000.0)
            self.assertEqual(row["revisit"]["visit"], 0)

    def test_revisit_fallback_patient_date_match(self):
        with tempfile.TemporaryDirectory() as tmp:
            db, client = _client(tmp)
            with connect(db) as c:
                _visit(c, "r2", "p2", "测试医生A", "复诊", "2026-07-03", study="S9")
                _bill(c, "b2", "p2", "2026-07-03", 500, study="")   # 无 study → 同患者同日退化
                _pay(c, "pay2", "p2", "b2", "2026-07-03", 500)
                c.commit()
            row = self._row(self._q(client), "测试医生A")
            self.assertEqual(row["revisit"]["visit"], 1)
            self.assertEqual(row["revisit"]["paid"], 500.0)
            self.assertEqual(row["revisit"]["deal"], 1)

    def test_refund_and_cancel(self):
        with tempfile.TemporaryDirectory() as tmp:
            db, client = _client(tmp)
            with connect(db) as c:
                _visit(c, "r3", "p1", "测试医生B", "初诊", "2026-07-04", study="S3")
                _bill(c, "b3", "p1", "2026-07-04", 1000, study="S3")
                _pay(c, "pay3", "p1", "b3", "2026-07-04", 1000)
                _pay(c, "pay3r", "p1", "b3", "2026-07-04", -300)                 # 退费冲减
                _pay(c, "pay3c", "p1", "b3", "2026-07-04", 999, cancel_mark="选错")  # 作废原单排除
                c.commit()
            self.assertEqual(self._row(self._q(client), "测试医生B")["first"]["paid"], 700.0)

    def test_paid_filtered_by_pay_time(self):
        # #673: 同一账单跨月收款，查 7 月只计 7 月现金流
        with tempfile.TemporaryDirectory() as tmp:
            db, client = _client(tmp)
            with connect(db) as c:
                _visit(c, "r7", "p1", "测试医生B", "初诊", "2026-07-10", study="S7")
                _bill(c, "b7", "p1", "2026-07-10", 1000, study="S7")
                _pay(c, "pay7a", "p1", "b7", "2026-07-10", 400)   # 7月
                _pay(c, "pay7b", "p1", "b7", "2026-08-05", 600)   # 8月
                c.commit()
            row = self._row(self._q(client, start="2026-07-01", end="2026-07-31"), "测试医生B")
            self.assertEqual(row["first"]["paid"], 400.0)         # 只算7月现金流

    def test_unclassified_covers_no_record_patient(self):
        # #673: 区间内无任何病历患者的收款也要进未归类(与收银闭合)
        with tempfile.TemporaryDirectory() as tmp:
            db, client = _client(tmp)
            with connect(db) as c:
                _visit(c, "r4", "p1", "测试医生B", "初诊", "2026-07-05", study="S4")
                _bill(c, "b4", "p1", "2026-07-05", 200, study="S4")
                _pay(c, "pay4", "p1", "b4", "2026-07-05", 200)
                # p3 完全无病历，但有账单收款
                _bill(c, "b6", "p3", "2026-07-06", 600, study="")
                _pay(c, "pay6", "p3", "b6", "2026-07-06", 600)
                c.commit()
            d = self._q(client)
            self.assertEqual(d["unclassified"]["first"]["paid"], 600.0)
            self.assertEqual(self._row(d, "测试医生B")["first"]["paid"], 200.0)

    def test_voided_bill_receivable_excluded(self):
        # #674: 作废账单应收不计入
        with tempfile.TemporaryDirectory() as tmp:
            db, client = _client(tmp)
            with connect(db) as c:
                _visit(c, "r8", "p1", "测试医生B", "初诊", "2026-07-08", study="S8")
                _bill(c, "b8", "p1", "2026-07-08", 1000, study="S8", state="900")  # SaaS撤单码
                c.commit()
            d = self._q(client)
            rows = [r for r in d["rows"] if r["name"] == "测试医生B"]
            recv = rows[0]["first"]["receivable"] if rows else 0
            self.assertEqual(recv, 0)   # 作废单不进应收

    def test_receivable_nets_discount(self):
        # #675: 应收扣 DiscountFee 净额
        with tempfile.TemporaryDirectory() as tmp:
            db, client = _client(tmp)
            with connect(db) as c:
                _visit(c, "r9", "p1", "测试医生B", "初诊", "2026-07-09", study="S10")
                _bill(c, "b9", "p1", "2026-07-09", 1000, study="S10", discount="300")
                _pay(c, "pay9", "p1", "b9", "2026-07-09", 700)
                c.commit()
            row = self._row(self._q(client), "测试医生B")
            self.assertEqual(row["first"]["receivable"], 700.0)   # 1000 - 300
            self.assertEqual(row["first"]["paid"], 700.0)

    def test_discount_with_thousands_comma(self):
        # #675: DiscountFee 带千分位逗号 "1,400.00"
        with tempfile.TemporaryDirectory() as tmp:
            db, client = _client(tmp)
            with connect(db) as c:
                _visit(c, "r10", "p1", "测试医生B", "初诊", "2026-07-11", study="S11")
                _bill(c, "b10", "p1", "2026-07-11", 5000, study="S11", discount="1,400.00")
                c.commit()
            row = self._row(self._q(client), "测试医生B")
            self.assertEqual(row["first"]["receivable"], 3600.0)   # 5000 - 1400

    def test_same_day_multi_record_earliest(self):
        # #676: 同患者同日多病历，退化匹配取当日最早就诊
        with tempfile.TemporaryDirectory() as tmp:
            db, client = _client(tmp)
            with connect(db) as c:
                _visit(c, "rE1", "p1", "测试医生B", "初诊", "2026-07-12", study="", hhmm="09:00:00")
                _visit(c, "rE2", "p1", "测试医生A", "复诊", "2026-07-12", study="", hhmm="14:00:00")
                _bill(c, "bE", "p1", "2026-07-12", 800, study="")   # 无 study → 退化到当日最早(初诊/测试医生B)
                _pay(c, "payE", "p1", "bE", "2026-07-12", 800)
                c.commit()
            d = self._q(client)
            self.assertEqual(self._row(d, "测试医生B")["first"]["paid"], 800.0)
            self.assertEqual(self._row(d, "测试医生A")["revisit"]["paid"], 0.0)

    def test_total_and_deal(self):
        with tempfile.TemporaryDirectory() as tmp:
            db, client = _client(tmp)
            with connect(db) as c:
                _visit(c, "r5", "p1", "测试医生B", "初诊", "2026-07-07", study="SA")
                _bill(c, "bA", "p1", "2026-07-07", 300, study="SA")
                _pay(c, "payA", "p1", "bA", "2026-07-07", 300)
                _visit(c, "r6", "p2", "测试医生A", "复诊", "2026-07-07", study="SB")
                _bill(c, "bB", "p2", "2026-07-07", 400, study="SB")
                _pay(c, "payB", "p2", "bB", "2026-07-07", 400)
                c.commit()
            d = self._q(client)
            self.assertEqual(d["total"]["first"]["paid"], 300.0)
            self.assertEqual(d["total"]["revisit"]["paid"], 400.0)
            self.assertEqual(d["total"]["first"]["deal"] + d["total"]["revisit"]["deal"], 2)

    def test_staff_filter_no_cross_doctor_unclassified(self):
        # #678: 按单医生筛选时，别的医生的收款不得落进当前视图的未归类(防串账)
        with tempfile.TemporaryDirectory() as tmp:
            db, client = _client(tmp)
            with connect(db) as c:
                _visit(c, "rF1", "p1", "测试医生B", "初诊", "2026-07-15", study="SF1")
                _bill(c, "bF1", "p1", "2026-07-15", 300, study="SF1")
                _pay(c, "payF1", "p1", "bF1", "2026-07-15", 300)
                _visit(c, "rF2", "p2", "测试医生A", "复诊", "2026-07-15", study="SF2")
                _bill(c, "bF2", "p2", "2026-07-15", 500, study="SF2")
                _pay(c, "payF2", "p2", "bF2", "2026-07-15", 500)
                c.commit()
            d = self._q(client, staff="测试医生B")
            self.assertEqual(len(d["rows"]), 1)
            self.assertEqual(d["rows"][0]["name"], "测试医生B")
            self.assertEqual(d["rows"][0]["first"]["paid"], 300.0)
            self.assertEqual(d["unclassified"]["first"]["paid"], 0.0)   # 测试医生A的500不进来

    def test_config_role_nurse_receivable_and_paid(self):
        # 助理/咨询师(处置单为轴)路径持久回归:应收扣优惠、作废单不计、实收按收款时间
        with tempfile.TemporaryDirectory() as tmp:
            db, client = _client(tmp)
            with connect(db) as c:
                _bill(c, "bn1", "p1", "2026-07-16", 1000, discount="300")
                _pay(c, "payn1", "p1", "bn1", "2026-07-16", 700)
                c.execute("insert into treatment_orders(order_id, patient_identity, visit_type, "
                          "assistant_name, status, bill_id, order_date, created_at) "
                          "values ('o1','p1','初诊','小助理','priced','bn1','2026-07-16','2026-07-16')")
                # 作废处置单不计入
                c.execute("insert into treatment_orders(order_id, patient_identity, visit_type, "
                          "assistant_name, status, bill_id, order_date, created_at) "
                          "values ('o2','p2','复诊','小助理','voided','','2026-07-16','2026-07-16')")
                c.commit()
            d = self._q(client, role="assistant")
            row = [r for r in d["rows"] if r["name"] == "小助理"][0]
            self.assertEqual(row["first"]["visit"], 1)
            self.assertEqual(row["first"]["paid"], 700.0)
            self.assertEqual(row["first"]["receivable"], 700.0)   # 1000 - 300
            self.assertEqual(row["revisit"]["visit"], 0)          # 作废单不计
            self.assertIsNone(d["unclassified"])                  # 非医生角色无未归类

    def test_nurse_perf_from_medical_record(self):
        # 护士统计=病历为轴,护士名取 content_json.Nurse(不是处置单)
        with tempfile.TemporaryDirectory() as tmp:
            db, client = _client(tmp)
            with connect(db) as c:
                _visit(c, "n1", "p1", "测试医生B", "初诊", "2026-07-02", study="SN1", nurse="护士小陈")
                _bill(c, "bn1", "p1", "2026-07-02", 1000, study="SN1")
                _pay(c, "payn1", "p1", "bn1", "2026-07-02", 800)
                # 一条没护士名的病历,不进护士统计
                _visit(c, "n2", "p2", "测试医生A", "复诊", "2026-07-02", study="SN2")
                c.commit()
            d = client.get("/api/store-stats/role-perf?" + urlencode(
                {"role": "nurse", "start": "2026-07-01", "end": "2026-07-31"})).json()
            self.assertEqual(len(d["rows"]), 1)
            self.assertEqual(d["rows"][0]["name"], "护士小陈")
            self.assertEqual(d["rows"][0]["first"]["visit"], 1)
            self.assertEqual(d["rows"][0]["first"]["paid"], 800.0)

    def test_bad_role_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            db, client = _client(tmp)
            self.assertEqual(client.get("/api/store-stats/role-perf?role=hacker").status_code, 400)


if __name__ == "__main__":
    unittest.main()


def _appt(c, aid, pid, doctor, day, status="1", hhmm="09:00:00"):
    c.execute("insert into appointments(appointment_id, patient_identity, start_time, "
              "doctor_name, item_name, status, updated_at) values (?,?,?,?,?,?,?)",
              (aid, pid, day + " " + hhmm, doctor, "项目", status, day))


def _order(c, oid, pid, doctor, day, bill_id="", status="paid"):
    c.execute("insert into treatment_orders(order_id, order_no, patient_identity, order_date, "
              "doctor_name, status, bill_id, updated_at) values (?,?,?,?,?,?,?,?)",
              (oid, "CZ" + oid, pid, day + " 09:10:00", doctor, status, bill_id, day))


class DoctorAttributionFallbackTest(unittest.TestCase):
    """GD-07:病历医生为空时按解析链归类(study处置医生→同日唯一预约→同日唯一处置);
    同级多候选进未归类,不得随便挂人;病历自身医生始终优先。"""

    def _q(self, client, **kw):
        p = {"role": "doctor", "start": "2026-07-01", "end": "2026-07-31", **kw}
        return client.get("/api/store-stats/role-perf?" + urlencode(p)).json()

    def _row(self, d, name):
        rows = [r for r in d["rows"] if r["name"] == name]
        return rows[0] if rows else None

    def test_empty_record_doctor_resolved_by_unique_study_order_doctor(self):
        with tempfile.TemporaryDirectory() as tmp:
            db, client = _client(tmp)
            with connect(db) as c:
                _visit(c, "r1", "p1", "", "初诊", "2026-07-10", study="S1")
                _order(c, "o1", "p1", "医生A", "2026-07-10", bill_id="b1")
                _bill(c, "b1", "p1", "2026-07-10", 100, study="S1")
                _pay(c, "pay1", "p1", "b1", "2026-07-10", 100)
                c.commit()
            d = self._q(client)
            row = self._row(d, "医生A")
            self.assertIsNotNone(row, d["rows"])
            self.assertEqual(row["first"]["visit"], 1)
            self.assertEqual(row["first"]["paid"], 100)

    def test_empty_record_doctor_falls_back_to_unique_appointment(self):
        with tempfile.TemporaryDirectory() as tmp:
            db, client = _client(tmp)
            with connect(db) as c:
                _visit(c, "r1", "p1", "", "复诊", "2026-07-11")
                _appt(c, "a1", "p1", "医生B", "2026-07-11")
                _appt(c, "a2", "p1", "医生X", "2026-07-11", status="3")   # 已取消不算候选
                c.commit()
            d = self._q(client)
            row = self._row(d, "医生B")
            self.assertIsNotNone(row, d["rows"])
            self.assertEqual(row["revisit"]["visit"], 1)

    def test_empty_record_doctor_falls_back_to_unique_same_day_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            db, client = _client(tmp)
            with connect(db) as c:
                _visit(c, "r1", "p1", "", "初诊", "2026-07-12")
                _order(c, "o1", "p1", "医生H", "2026-07-12")
                c.commit()
            d = self._q(client)
            row = self._row(d, "医生H")
            self.assertIsNotNone(row, d["rows"])
            self.assertEqual(row["first"]["visit"], 1)

    def test_record_own_doctor_always_wins(self):
        with tempfile.TemporaryDirectory() as tmp:
            db, client = _client(tmp)
            with connect(db) as c:
                _visit(c, "r1", "p1", "医生C", "初诊", "2026-07-13", study="S2")
                _order(c, "o1", "p1", "医生D", "2026-07-13", bill_id="b1")
                _bill(c, "b1", "p1", "2026-07-13", 80, study="S2")
                c.commit()
            d = self._q(client)
            self.assertIsNotNone(self._row(d, "医生C"))
            self.assertIsNone(self._row(d, "医生D"))

    def test_ambiguous_candidates_go_unclassified_without_double_count(self):
        with tempfile.TemporaryDirectory() as tmp:
            db, client = _client(tmp)
            with connect(db) as c:
                _visit(c, "r1", "p1", "", "初诊", "2026-07-14")
                _appt(c, "a1", "p1", "医生E", "2026-07-14")
                _appt(c, "a2", "p1", "医生F", "2026-07-14", hhmm="10:00:00")
                _bill(c, "b1", "p1", "2026-07-14", 100)
                _pay(c, "pay1", "p1", "b1", "2026-07-14", 100)
                c.commit()
            d = self._q(client)
            self.assertIsNone(self._row(d, "医生E"))
            self.assertIsNone(self._row(d, "医生F"))
            self.assertEqual(d["unclassified"]["first"]["visit"], 1)
            self.assertEqual(d["unclassified"]["first"]["paid"], 100)
            self.assertEqual(d["total"]["first"]["paid"], 0)
