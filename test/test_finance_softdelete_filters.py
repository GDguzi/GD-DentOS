import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from local_app.api import create_app
from local_app.db import connect, init_db

TODAY = __import__("datetime").date.today().isoformat()


def _client(tmpdir):
    db_path = Path(tmpdir) / "clinic.sqlite3"
    init_db(db_path)
    with connect(db_path) as conn:
        conn.execute(
            "insert into patients(patient_identity, display_name, updated_at, current_hash) "
            "values ('p1', '测试患者', '2026-06-07 00:00:00', 'h1')"
        )
        # 正常收款 100 + 作废原单 50（真实作废标记是 source_json.CancelMark，非 state）
        conn.execute(
            "insert into payments(payment_id, patient_identity, pay_time, amount, state) "
            f"values ('pay1', 'p1', '{TODAY} 10:00:00', 100, '0')"
        )
        conn.execute(
            "insert into payments(payment_id, patient_identity, pay_time, amount, state, source_json) "
            f"values ('pay2', 'p1', '{TODAY} 11:00:00', 50, '900', '{{\"CancelMark\": \"收费方式选择错误\"}}')"
        )
        # 今日待回访一条 + 今日已软删回访一条
        conn.execute(
            "insert into return_visits(return_visit_id, patient_identity, due_time, "
            "item_name, status, is_deleted) "
            f"values ('rv1', 'p1', '{TODAY}', '复查', '0', 0)"
        )
        conn.execute(
            "insert into return_visits(return_visit_id, patient_identity, due_time, "
            "item_name, status, is_deleted) "
            f"values ('rv2', 'p1', '{TODAY}', '错排的回访', '0', 1)"
        )
        conn.commit()
    return db_path, TestClient(create_app(db_path))


class VoidedPaymentExcludedTest(unittest.TestCase):
    def test_income_excludes_voided(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path, client = _client(tmpdir)
            body = client.get(
                f"/api/reports/income?date_from={TODAY}&date_to={TODAY}"
            ).json()
            self.assertEqual(body["total_amount"], 100)
            self.assertEqual(body["total_count"], 1)

    def test_cashier_total_excludes_voided_but_lists_them(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path, client = _client(tmpdir)
            body = client.get(f"/api/reports/cashier?date_from={TODAY}&date_to={TODAY}").json()
            self.assertEqual(body["total_amount"], 100)
            self.assertEqual(body["count"], 2)  # 明细仍可见(含state标注)
            self.assertEqual(body["voided_count"], 1)
            agg = {a["patient_identity"]: a for a in body["by_patient"]}
            self.assertEqual(agg["p1"]["amount"], 100)

    def test_today_cash_in_excludes_voided(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path, client = _client(tmpdir)
            body = client.get(f"/api/today-work?date={TODAY}").json()
            self.assertEqual(body["summary"]["today_cash_in"], 100)

    def test_malformed_source_json_does_not_crash(self):
        # source_json 为空串/坏JSON 时,财务口径的 json_extract 不能抛 malformed JSON
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path, client = _client(tmpdir)
            with connect(db_path) as conn:
                conn.execute("insert into payments(payment_id, patient_identity, pay_time, amount, state, source_json) "
                             f"values ('pbad', 'p1', '{TODAY} 13:00:00', 80, '0', '')")        # 空串
                conn.execute("insert into payments(payment_id, patient_identity, pay_time, amount, state, source_json) "
                             f"values ('pbad2', 'p1', '{TODAY} 14:00:00', 20, '0', 'not json')")  # 坏JSON
                conn.commit()
            self.assertEqual(client.get(f"/api/reports/income?date_from={TODAY}&date_to={TODAY}").status_code, 200)
            self.assertEqual(client.get(f"/api/reports/today-collection?date={TODAY}").status_code, 200)
            self.assertEqual(client.get(f"/api/reports/cashier?date_from={TODAY}&date_to={TODAY}").status_code, 200)
            self.assertEqual(client.get(f"/api/today-work?date={TODAY}").status_code, 200)
            # 坏JSON当正常单计入(无CancelMark)：income = 100(pay1) + 80 + 20 = 200
            self.assertEqual(client.get(f"/api/reports/income?date_from={TODAY}&date_to={TODAY}").json()["total_amount"], 200)

    def test_refund_negative_kept_as_offset(self):
        # 退费负数(CancelMark但金额<0)保留作冲减,不被当作废原单剔除
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path, client = _client(tmpdir)
            with connect(db_path) as conn:
                # 正常+100(pay1) + 作废原单+50(pay2,排除) 已存在；再加退费-30(应保留冲减)
                conn.execute(
                    "insert into payments(payment_id, patient_identity, pay_time, amount, state, source_json) "
                    f"values ('pay3', 'p1', '{TODAY} 12:00:00', -30, '410', '{{\"CancelMark\": \"患者要求退费\"}}')"
                )
                conn.commit()
            body = client.get(f"/api/reports/income?date_from={TODAY}&date_to={TODAY}").json()
            self.assertEqual(body["total_amount"], 70)   # 100 - 30(退费冲减)，作废原单50不计


class SoftDeletedReturnVisitFilteredTest(unittest.TestCase):
    def test_today_work_excludes_soft_deleted(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path, client = _client(tmpdir)
            body = client.get(f"/api/today-work?date={TODAY}").json()
            self.assertEqual(body["summary"]["pending_return_visits"], 1)
            ids = {r["return_visit_id"] for r in body["return_visits"]}
            self.assertEqual(ids, {"rv1"})

    def test_patient_detail_excludes_soft_deleted(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path, client = _client(tmpdir)
            body = client.get("/api/patients/p1").json()
            ids = {r["return_visit_id"] for r in body["return_visits"]}
            self.assertEqual(ids, {"rv1"})

    def test_visit_timeline_excludes_soft_deleted(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path, client = _client(tmpdir)
            body = client.get("/api/patients/p1/visits").json()
            items = [
                rv["item_name"]
                for visit in body.get("visits", [])
                for rv in visit.get("return_visits", [])
            ]
            self.assertNotIn("错排的回访", items)
            self.assertIn("复查", items)

    def test_put_soft_deleted_return_visit_404(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path, client = _client(tmpdir)
            resp = client.put("/api/return-visits/rv2", json={
                "expected_revision": 1, "note": "改一下",
            })
            self.assertEqual(resp.status_code, 404)
            resp = client.put("/api/return-visits/rv1", json={
                "expected_revision": 1, "note": "正常改",
            })
            self.assertEqual(resp.status_code, 200)


if __name__ == "__main__":
    unittest.main()
