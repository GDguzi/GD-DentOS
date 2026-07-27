import tempfile
import unittest
import uuid
from pathlib import Path

from fastapi.testclient import TestClient

from local_app.api import create_app
from local_app.db import connect, init_db


def _client(tmpdir):
    db_path = Path(tmpdir) / "clinic.sqlite3"
    init_db(db_path)
    with connect(db_path) as conn:
        conn.execute(
            "insert into patients(patient_identity, display_name, updated_at, current_hash) "
            "values ('p1', '测试患者', '2026-06-07 00:00:00', 'h1')"
        )
        conn.commit()
    return db_path, TestClient(create_app(db_path))


def _sample_plan():
    return {
        "plan_name": "种植修复方案",
        "plan_date": "2026-06-10",
        "doctor_name": "王医生",
        "groups": [{"group_type": "all", "items": [
            {"item_name": "种植体植入", "tooth": "36", "quantity": 1, "unit_price": 8000},
            {"item_name": "牙冠", "tooth": "36", "quantity": 1, "unit_price": 2000, "total_price": 2000},
        ]}],
    }


class TreatmentPlanCreateTest(unittest.TestCase):
    def test_non_integer_quantity_rejected(self):
        # 方案建单数量非整数也拒绝报错(与处置单同口径),整数值/大数精确接受
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path, client = _client(tmpdir)

            def post_qty(q):
                body = _sample_plan()
                body["groups"] = [{"group_type": "all", "items": [{"item_name": "种植体", "tooth": "36", "quantity": q, "unit_price": 8000}]}]
                return client.post("/api/patients/p1/treatment-plans", json=body).status_code
            self.assertEqual(post_qty("2.5"), 400)
            self.assertEqual(post_qty(2.9), 400)
            self.assertEqual(post_qty("1e3"), 400)
            self.assertEqual(post_qty("abc"), 400)
            self.assertEqual(post_qty("2"), 200)
            self.assertEqual(post_qty("2.0"), 200)

    def test_create_then_list(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path, client = _client(tmpdir)
            resp = client.post("/api/patients/p1/treatment-plans", json=_sample_plan())
            self.assertEqual(resp.status_code, 200)
            body = resp.json()
            self.assertTrue(body["plan_id"].startswith("local-plan-"))
            self.assertEqual(body["status"], "draft")
            self.assertEqual(body["item_count"], 2)
            self.assertEqual(body["total_price"], 10000)  # 8000*1 + 2000

            listed = client.get("/api/patients/p1/treatment-plans").json()
            self.assertEqual(listed["totalcount"], 1)
            plan = listed["plans"][0]
            self.assertEqual(plan["plan_name"], "种植修复方案")
            self.assertEqual(len(plan["groups"]), 1)
            gitems = plan["groups"][0]["items"]
            self.assertEqual(len(gitems), 2)
            self.assertEqual(gitems[0]["tooth"], "36")
            self.assertEqual(plan["items"], [])   # 顶层items只放未分组项(历史同步数据)

    def test_soft_delete_hides_from_list(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path, client = _client(tmpdir)
            pid = client.post("/api/patients/p1/treatment-plans", json=_sample_plan()).json()["plan_id"]
            self.assertEqual(len(client.get("/api/patients/p1/treatment-plans").json()["plans"]), 1)
            r = client.delete(f"/api/treatment-plans/{pid}")
            self.assertEqual(r.status_code, 200)
            self.assertEqual(client.get("/api/patients/p1/treatment-plans").json()["totalcount"], 0)
            # 重复删除 404
            self.assertEqual(client.delete(f"/api/treatment-plans/{pid}").status_code, 404)

    def test_deleted_plan_cannot_confirm_or_bill(self):
        # 软删后不能再走 status/bill 出账单(否则"已删"方案仍产生收费)
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path, client = _client(tmpdir)
            plan_id = client.post("/api/patients/p1/treatment-plans", json=_sample_plan()).json()["plan_id"]
            self.assertEqual(client.delete(f"/api/treatment-plans/{plan_id}").status_code, 200)
            # 软删后:确认/转划价都 404
            self.assertEqual(
                client.post(f"/api/treatment-plans/{plan_id}/status", json={"status": "confirmed"}).status_code, 404)
            self.assertEqual(
                client.post(f"/api/treatment-plans/{plan_id}/bill", json={"discount": 0}).status_code, 404)
            # 没有账单被生成
            with connect(db_path) as conn:
                n = conn.execute("select count(*) c from bills").fetchone()["c"]
            self.assertEqual(n, 0)

    def test_plan_doc_fields_persist_and_list(self):
        # P2-28：诊疗计划补打印字段 类别/标签/诊断/治疗目标/注意事项 可录、列表回传
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path, client = _client(tmpdir)
            payload = dict(_sample_plan())
            payload.update({
                "category": "种植",
                "label": "全口重建",
                "diagnosis": "36缺失，牙槽骨条件良好",
                "treatment_goals": "恢复咀嚼功能与美观",
                "precautions": "术后24小时勿漱口，按时复诊",
            })
            resp = client.post("/api/patients/p1/treatment-plans", json=payload)
            self.assertEqual(resp.status_code, 200)
            plan = client.get("/api/patients/p1/treatment-plans").json()["plans"][0]
            self.assertEqual(plan["category"], "种植")
            self.assertEqual(plan["label"], "全口重建")
            self.assertEqual(plan["diagnosis"], "36缺失，牙槽骨条件良好")
            self.assertEqual(plan["treatment_goals"], "恢复咀嚼功能与美观")
            self.assertEqual(plan["precautions"], "术后24小时勿漱口，按时复诊")

    def test_create_rejects_empty_name_or_items(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            _, client = _client(tmpdir)
            r1 = client.post("/api/patients/p1/treatment-plans",
                             json={"plan_name": "", "groups": [{"group_type": "all", "items": [{"item_name": "x"}]}]})
            self.assertEqual(r1.status_code, 400)
            r2 = client.post("/api/patients/p1/treatment-plans",
                             json={"plan_name": "空项", "groups": []})
            self.assertEqual(r2.status_code, 400)
            r3 = client.post("/api/patients/p1/treatment-plans",
                             json={"plan_name": "全空项", "groups": [{"group_type": "all", "items": [{"item_name": "  "}]}]})
            self.assertEqual(r3.status_code, 400)

    def test_create_unknown_patient_404(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            _, client = _client(tmpdir)
            resp = client.post("/api/patients/nope/treatment-plans", json=_sample_plan())
            self.assertEqual(resp.status_code, 404)


class TreatmentPlanStatusTest(unittest.TestCase):
    def _create(self, client):
        return client.post("/api/patients/p1/treatment-plans", json=_sample_plan()).json()["plan_id"]

    def test_valid_transitions_draft_confirm_done(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            _, client = _client(tmpdir)
            pid = self._create(client)
            self.assertEqual(
                client.post(f"/api/treatment-plans/{pid}/status", json={"status": "confirmed"}).json()["status"],
                "confirmed",
            )
            self.assertEqual(
                client.post(f"/api/treatment-plans/{pid}/status", json={"status": "done"}).status_code,
                200,
            )

    def test_revert_transitions(self):
        # 撤回确认(confirmed→draft)、撤回完成(done→confirmed)
        with tempfile.TemporaryDirectory() as tmpdir:
            _, client = _client(tmpdir)
            pid = self._create(client)
            status = lambda s: client.post(
                f"/api/treatment-plans/{pid}/status", json={"status": s}
            )
            status("confirmed")
            # 撤回确认 → 回到 draft
            self.assertEqual(status("draft").json()["status"], "draft")
            # 再确认→完成→撤回完成→confirmed
            status("confirmed")
            status("done")
            self.assertEqual(status("confirmed").json()["status"], "confirmed")

    def test_illegal_transition_409(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            _, client = _client(tmpdir)
            pid = self._create(client)
            # draft 不能直接 done
            self.assertEqual(
                client.post(f"/api/treatment-plans/{pid}/status", json={"status": "done"}).status_code,
                409,
            )
            # draft→draft 非法转换(draft 只能去 confirmed)
            self.assertEqual(
                client.post(f"/api/treatment-plans/{pid}/status", json={"status": "draft"}).status_code,
                409,
            )

    def test_bad_status_value_400_and_unknown_plan_404(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            _, client = _client(tmpdir)
            pid = self._create(client)
            self.assertEqual(
                client.post(f"/api/treatment-plans/{pid}/status", json={"status": "xxx"}).status_code,
                400,
            )
            self.assertEqual(
                client.post("/api/treatment-plans/nope/status", json={"status": "confirmed"}).status_code,
                404,
            )


if __name__ == "__main__":
    unittest.main()


class TreatmentPlanAuditTest(unittest.TestCase):
    def test_create_and_status_change_write_audit(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path, client = _client(tmpdir)
            resp = client.post(
                "/api/patients/p1/treatment-plans",
                json={"plan_name": "修复计划", "groups": [{"group_type": "all", "items": [{"item_name": "补牙"}]}]},
            )
            plan_id = resp.json()["plan_id"]
            client.post(f"/api/treatment-plans/{plan_id}/status", json={"status": "confirmed"})
            with connect(db_path) as conn:
                actions = [
                    r["action"]
                    for r in conn.execute(
                        "select action from audit_logs "
                        "where entity_type='treatment_plan' and entity_id=? "
                        "order by audit_id",
                        (plan_id,),
                    ).fetchall()
                ]
            self.assertEqual(
                actions, ["create_treatment_plan", "change_plan_status"]
            )


def _grouped_plan():
    return {
        "plan_name": "全口治疗",
        "plan_date": "2026-06-13",
        "doctor_name": "王医生",
        "groups": [
            {
                "group_name": "根管/补牙",
                "group_type": "all",
                "items": [
                    {"item_name": "6号根管", "tooth": "16", "quantity": 1, "unit_price": 1500},
                    {"item_name": "7号补牙", "tooth": "17", "quantity": 1, "unit_price": 800},
                ],
            },
            {
                "group_name": "缺牙处理",
                "group_type": "alt",
                "items": [
                    {"item_name": "种植", "tooth": "36", "quantity": 1, "unit_price": 9000},
                    {"item_name": "活动假牙", "tooth": "36", "quantity": 1, "unit_price": 2000},
                ],
            },
        ],
    }


class TreatmentPlanGroupTest(unittest.TestCase):
    def test_create_grouped_then_list_returns_groups(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path, client = _client(tmpdir)
            resp = client.post("/api/patients/p1/treatment-plans", json=_grouped_plan())
            self.assertEqual(resp.status_code, 200)
            # 创建时 alt 组未选 → 总价只含"都做"组(1500+800=2300)
            self.assertEqual(resp.json()["total_price"], 2300)

            plans = client.get("/api/patients/p1/treatment-plans").json()["plans"]
            self.assertEqual(len(plans), 1)
            groups = plans[0]["groups"]
            self.assertEqual(len(groups), 2)
            all_group = [g for g in groups if g["group_type"] == "all"][0]
            alt_group = [g for g in groups if g["group_type"] == "alt"][0]
            self.assertEqual(len(all_group["items"]), 2)
            self.assertEqual(len(alt_group["items"]), 2)
            self.assertEqual(alt_group["selected_item_id"], "")

    def test_select_alt_option_updates_total(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path, client = _client(tmpdir)
            resp = client.post("/api/patients/p1/treatment-plans", json=_grouped_plan())
            plan_id = resp.json()["plan_id"]
            plans = client.get("/api/patients/p1/treatment-plans").json()["plans"]
            alt_group = [g for g in plans[0]["groups"] if g["group_type"] == "alt"][0]
            implant = [i for i in alt_group["items"] if i["item_name"] == "种植"][0]

            sel = client.post(
                f"/api/treatment-plan-groups/{alt_group['group_id']}/select",
                json={"item_id": implant["item_id"]},
            )
            self.assertEqual(sel.status_code, 200)
            # 总价 = 都做2300 + 选中的种植9000 = 11300
            plans2 = client.get("/api/patients/p1/treatment-plans").json()["plans"]
            self.assertEqual(plans2[0]["total_price"], 11300)
            alt2 = [g for g in plans2[0]["groups"] if g["group_type"] == "alt"][0]
            self.assertEqual(alt2["selected_item_id"], implant["item_id"])

    def test_select_rejected_after_confirmed(self):
        # 已确认/已划价的计划不能再改二选一(否则总价与已生成账单脱节)
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path, client = _client(tmpdir)
            client.post("/api/patients/p1/treatment-plans", json=_grouped_plan())
            plan = client.get("/api/patients/p1/treatment-plans").json()["plans"][0]
            plan_id = plan["plan_id"]
            alt = [g for g in plan["groups"] if g["group_type"] == "alt"][0]
            it = alt["items"][0]["item_id"]
            client.post(f"/api/treatment-plans/{plan_id}/status", json={"status": "confirmed"})
            sel = client.post(f"/api/treatment-plan-groups/{alt['group_id']}/select", json={"item_id": it})
            self.assertEqual(sel.status_code, 409)

    def test_select_rejects_item_from_other_group(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path, client = _client(tmpdir)
            resp = client.post("/api/patients/p1/treatment-plans", json=_grouped_plan())
            plans = client.get("/api/patients/p1/treatment-plans").json()["plans"]
            alt_group = [g for g in plans[0]["groups"] if g["group_type"] == "alt"][0]
            all_group = [g for g in plans[0]["groups"] if g["group_type"] == "all"][0]
            foreign_item = all_group["items"][0]["item_id"]
            sel = client.post(
                f"/api/treatment-plan-groups/{alt_group['group_id']}/select",
                json={"item_id": foreign_item},
            )
            self.assertEqual(sel.status_code, 400)

    def test_flat_items_rejected(self):
        # 架构铁律#禁止兼容层：旧顶层items扁平双轨已删,缺groups一律400
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path, client = _client(tmpdir)
            resp = client.post("/api/patients/p1/treatment-plans", json={
                "plan_name": "旧格式", "items": [{"item_name": "补牙", "unit_price": 100}]})
            self.assertEqual(resp.status_code, 400)


class TreatmentPlanBillingTest(unittest.TestCase):
    def _grouped_with_selection(self, client, confirm=True):
        resp = client.post("/api/patients/p1/treatment-plans", json=_grouped_plan())
        plan_id = resp.json()["plan_id"]
        plans = client.get("/api/patients/p1/treatment-plans").json()["plans"]
        alt = [g for g in plans[0]["groups"] if g["group_type"] == "alt"][0]
        denture = [i for i in alt["items"] if i["item_name"] == "活动假牙"][0]
        client.post(
            f"/api/treatment-plan-groups/{alt['group_id']}/select",
            json={"item_id": denture["item_id"]},
        )
        if confirm:
            client.post(f"/api/treatment-plans/{plan_id}/status", json={"status": "confirmed"})
        return plan_id

    def test_plan_bill_has_bill_no(self):
        # 计划转划价生成的账单要有编号(此前写空串,收费单"没有账单编号")
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path, client = _client(tmpdir)
            plan_id = self._grouped_with_selection(client)
            bill_id = client.post(f"/api/treatment-plans/{plan_id}/bill", json={}).json()["bill_id"]
            with connect(db_path) as conn:
                bno = conn.execute("select bill_no from bills where bill_id=?", (bill_id,)).fetchone()[0]
            self.assertTrue(bno and len(bno) == 10, f"bill_no={bno!r}")

    def test_generate_pending_bill_only_includes_selected_alt(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path, client = _client(tmpdir)
            plan_id = self._grouped_with_selection(client)
            resp = client.post(f"/api/treatment-plans/{plan_id}/bill", json={})
            self.assertEqual(resp.status_code, 200)
            body = resp.json()
            # 都做 2300 + 选中的活动假牙 2000 = 4300（种植9000未选不计）
            self.assertEqual(body["total_fee"], 4300)
            self.assertEqual(body["item_count"], 3)
            bill_id = body["bill_id"]
            with connect(db_path) as conn:
                bill = conn.execute(
                    "select state, paid_fee, total_fee from bills where bill_id = ?", (bill_id,)
                ).fetchone()
                cnt = conn.execute(
                    "select count(*) c from treatment_items where bill_id = ?", (bill_id,)
                ).fetchone()["c"]
            self.assertEqual(bill["state"], "pending")
            self.assertEqual(bill["paid_fee"], 0)
            self.assertEqual(bill["total_fee"], 4300)
            self.assertEqual(cnt, 3)

    def test_discount_and_price_adjust(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path, client = _client(tmpdir)
            plan_id = self._grouped_with_selection(client)
            plans = client.get("/api/patients/p1/treatment-plans").json()["plans"]
            all_g = [g for g in plans[0]["groups"] if g["group_type"] == "all"][0]
            root = [i for i in all_g["items"] if i["item_name"] == "6号根管"][0]
            # 医生把根管从1500改成1200，并整单优惠300
            resp = client.post(
                f"/api/treatment-plans/{plan_id}/bill",
                json={"discount": 300, "adjustments": {root["item_id"]: {"unit_price": 1200}}},
            )
            self.assertEqual(resp.status_code, 200)
            # (1200 + 800 + 2000) - 300 = 3700
            self.assertEqual(resp.json()["total_fee"], 3700)

    def test_plan_bill_rounds_to_cents_and_settles(self):
        # (同):计划转划价的逐项/总额也须取整到分,否则带厘账单收不齐
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path, client = _client(tmpdir)
            plan_id = self._grouped_with_selection(client)
            root = self._root_item_id(client)
            resp = client.post(
                f"/api/treatment-plans/{plan_id}/bill",
                json={"adjustments": {root: {"total_fee": 50.745}}},
            )
            self.assertEqual(resp.status_code, 200, resp.text)
            total = resp.json()["total_fee"]
            self.assertEqual(total, round(total, 2), "总额必须取整到分")
            bill_id = resp.json()["bill_id"]
            with connect(db_path) as conn:
                for line in conn.execute("select total_fee from treatment_items where bill_id=?", (bill_id,)):
                    self.assertEqual(line["total_fee"], round(line["total_fee"], 2), "行金额必须取整到分")
            # 付显示的待收金额后账单必须结清
            r = client.post(f"/api/bills/{bill_id}/pay", json={"methods": [{"method": "现金", "amount": total}], "request_id": uuid.uuid4().hex})
            self.assertEqual(r.status_code, 200, r.text)
            with connect(db_path) as conn:
                self.assertEqual(conn.execute(
                    "select state from bills where bill_id=?", (bill_id,)).fetchone()["state"], "paid")

    def _root_item_id(self, client):
        plans = client.get("/api/patients/p1/treatment-plans").json()["plans"]
        all_g = [g for g in plans[0]["groups"] if g["group_type"] == "all"][0]
        return [i for i in all_g["items"] if i["item_name"] == "6号根管"][0]["item_id"]

    def test_bill_rejects_nan_inf_amounts(self):
        # 修复审计R1：discount/明细金额 nan/inf 属非法值，须400报错（原断言200静默回退，兜底行为已废）
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path, client = _client(tmpdir)
            plan_id = self._grouped_with_selection(client)
            root_id = self._root_item_id(client)
            resp = client.post(
                f"/api/treatment-plans/{plan_id}/bill",
                json={"discount": "nan", "adjustments": {root_id: {"total_fee": "inf", "unit_price": "nan"}}},
            )
            self.assertEqual(resp.status_code, 400)

    def test_bill_zero_total_settles_paid(self):
        # 整单抵到0元 → 直接结清(paid)，不留 pending 死单
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path, client = _client(tmpdir)
            plan_id = self._grouped_with_selection(client)
            resp = client.post(f"/api/treatment-plans/{plan_id}/bill", json={"discount": 999999})
            self.assertEqual(resp.status_code, 200)
            self.assertEqual(resp.json()["total_fee"], 0)
            self.assertEqual(resp.json()["state"], "paid")
            with connect(db_path) as conn:
                bill = conn.execute(
                    "select state from bills where bill_id = ?", (resp.json()["bill_id"],)
                ).fetchone()
            self.assertEqual(bill["state"], "paid")

    def test_bill_negative_qty_price_rejected(self):
        # 修复审计R1：负数量/负单价直接400报错，不再静默回退原值开单（原断言200兜底行为已废）
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path, client = _client(tmpdir)
            plan_id = self._grouped_with_selection(client)
            root_id = self._root_item_id(client)
            resp = client.post(
                f"/api/treatment-plans/{plan_id}/bill",
                json={"adjustments": {root_id: {"quantity": -3, "unit_price": -100}}},
            )
            self.assertEqual(resp.status_code, 400)
            # 被拒后计划不得被占用，合法参数仍可正常转划价
            resp = client.post(f"/api/treatment-plans/{plan_id}/bill", json={})
            self.assertEqual(resp.status_code, 200, resp.text)

    def test_bill_rejects_illegal_amounts_400(self):
        # 修复审计R1：转划价对非法 优惠/单价/数量/金额 一律400（对齐处置单划价口径），不静默按别的数开单
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path, client = _client(tmpdir)
            plan_id = self._grouped_with_selection(client)
            root_id = self._root_item_id(client)
            bad_payloads = [
                ("负数单价", {"adjustments": {root_id: {"unit_price": -100}}}),
                ("非数字单价", {"adjustments": {root_id: {"unit_price": "abc"}}}),
                ("负优惠", {"discount": -50}),
                ("非数字优惠", {"discount": "abc"}),
                ("数量为0", {"adjustments": {root_id: {"quantity": 0}}}),
                ("负数量", {"adjustments": {root_id: {"quantity": -1}}}),
                ("负本项金额", {"adjustments": {root_id: {"total_fee": -1}}}),
            ]
            for label, payload in bad_payloads:
                with self.subTest(label):
                    resp = client.post(f"/api/treatment-plans/{plan_id}/bill", json=payload)
                    self.assertEqual(resp.status_code, 400, f"{label}: {resp.text}")
            # 非法请求不得留下账单
            with connect(db_path) as conn:
                cnt = conn.execute("select count(*) c from bills").fetchone()["c"]
            self.assertEqual(cnt, 0)

    def test_bill_non_dict_adjustment_entry_ignored_not_500(self):
        # R1复核遗留：adjustments 值为非 dict(如字符串)时不得 AttributeError 500——
        # 对齐处置单 price_order 的 isinstance 口径:非 dict 改价条目视为未改价,按库内价开单
        with tempfile.TemporaryDirectory() as tmpdir:
            _db_path, client = _client(tmpdir)
            plan_id = self._grouped_with_selection(client)
            resp = client.post(f"/api/treatment-plans/{plan_id}/bill",
                               json={"adjustments": {self._root_item_id(client): "xx"}})
            self.assertEqual(resp.status_code, 200, resp.text)

    def test_negative_price_at_create_not_billed_negative(self):
        # 计划创建带负单价 → 不存负价；确认后转划价明细/账单不为负
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path, client = _client(tmpdir)
            plan_id = client.post("/api/patients/p1/treatment-plans", json={
                "plan_name": "异常价计划", "doctor_name": "王医生",
                "groups": [{"group_type": "all", "items": [
                    {"item_name": "异常项", "quantity": -2, "unit_price": -100, "total_price": -200}]}],
            }).json()["plan_id"]
            client.post(f"/api/treatment-plans/{plan_id}/status", json={"status": "confirmed"})
            resp = client.post(f"/api/treatment-plans/{plan_id}/bill", json={})
            self.assertEqual(resp.status_code, 200)
            self.assertGreaterEqual(resp.json()["total_fee"], 0)
            with connect(db_path) as conn:
                rows = conn.execute(
                    "select quantity, unit_price, total_fee from treatment_items where bill_id = ?",
                    (resp.json()["bill_id"],),
                ).fetchall()
            for r in rows:
                self.assertGreaterEqual(r["quantity"], 1)
                self.assertGreaterEqual(r["unit_price"], 0)
                self.assertGreaterEqual(r["total_fee"], 0)

    def test_bill_unselected_alt_excluded_when_none_selected(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path, client = _client(tmpdir)
            resp = client.post("/api/patients/p1/treatment-plans", json=_grouped_plan())
            plan_id = resp.json()["plan_id"]
            client.post(f"/api/treatment-plans/{plan_id}/status", json={"status": "confirmed"})
            # 未选二选一 → 只划价都做组 2300
            resp = client.post(f"/api/treatment-plans/{plan_id}/bill", json={})
            self.assertEqual(resp.status_code, 200)
            self.assertEqual(resp.json()["total_fee"], 2300)
            self.assertEqual(resp.json()["item_count"], 2)

    def test_bill_plan_not_found(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path, client = _client(tmpdir)
            resp = client.post("/api/treatment-plans/nope/bill", json={})
            self.assertEqual(resp.status_code, 404)

    def test_bill_rejects_unconfirmed_draft_plan(self):
        # 草稿计划不能直接转划价（必须先确认方案）
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path, client = _client(tmpdir)
            plan_id = self._grouped_with_selection(client, confirm=False)
            resp = client.post(f"/api/treatment-plans/{plan_id}/bill", json={})
            self.assertEqual(resp.status_code, 409)
            with connect(db_path) as conn:
                cnt = conn.execute("select count(*) c from bills").fetchone()["c"]
            self.assertEqual(cnt, 0)

    def test_bill_idempotent_rejects_second_call(self):
        # 同一计划重复转划价应被拒绝，不重复落单
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path, client = _client(tmpdir)
            plan_id = self._grouped_with_selection(client)
            first = client.post(f"/api/treatment-plans/{plan_id}/bill", json={})
            self.assertEqual(first.status_code, 200)
            second = client.post(f"/api/treatment-plans/{plan_id}/bill", json={})
            self.assertEqual(second.status_code, 409)
            with connect(db_path) as conn:
                bills = conn.execute("select count(*) c from bills").fetchone()["c"]
                items = conn.execute("select count(*) c from treatment_items").fetchone()["c"]
            self.assertEqual(bills, 1)
            self.assertEqual(items, 3)


class PlanRollbackBillTest(unittest.TestCase):
    """撤回确认(confirmed→draft)已转划价的计划,要连带处理账单,不留脱节孤儿。"""

    def _confirm_and_bill(self, client):
        plan_id = client.post("/api/patients/p1/treatment-plans", json=_sample_plan()).json()["plan_id"]
        client.post(f"/api/treatment-plans/{plan_id}/status", json={"status": "confirmed"})
        bill_id = client.post(f"/api/treatment-plans/{plan_id}/bill", json={}).json()["bill_id"]
        return plan_id, bill_id

    def test_rollback_unpaid_voids_bill_and_clears_link(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path, client = _client(tmpdir)
            plan_id, bill_id = self._confirm_and_bill(client)
            r = client.post(f"/api/treatment-plans/{plan_id}/status", json={"status": "draft"})
            self.assertEqual(r.status_code, 200)
            with connect(db_path) as conn:
                bill = conn.execute("select 1 from bills where bill_id=?", (bill_id,)).fetchone()
                items = conn.execute("select count(*) from treatment_items where bill_id=?", (bill_id,)).fetchone()[0]
                pbill = conn.execute("select bill_id, status from treatment_plans where plan_id=?", (plan_id,)).fetchone()
            self.assertIsNone(bill, "未付账单应连带硬删(不留回收站可还原的孤儿)")
            self.assertEqual(items, 0, "明细应连带删除")
            self.assertEqual(pbill[0], "", "计划 bill_id 应清空")
            self.assertEqual(pbill[1], "draft")
            # 清干净后可重新划价(不再撞"已转划价"409)
            client.post(f"/api/treatment-plans/{plan_id}/status", json={"status": "confirmed"})
            self.assertEqual(client.post(f"/api/treatment-plans/{plan_id}/bill", json={}).status_code, 200)

    def test_rollback_blocked_when_consent_bound(self):
        # 账单绑了未作废的知情同意书,硬删会留悬空引用→撤回应被拦截
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path, client = _client(tmpdir)
            plan_id, bill_id = self._confirm_and_bill(client)
            with connect(db_path) as conn:
                conn.execute("insert into consent_documents(document_id, patient_identity, bill_id, status) "
                             "values ('cd1', 'p1', ?, 'signed')", (bill_id,))
                conn.commit()
            r = client.post(f"/api/treatment-plans/{plan_id}/status", json={"status": "draft"})
            self.assertEqual(r.status_code, 409, "绑了同意书不能撤回")
            with connect(db_path) as conn:
                self.assertIsNotNone(conn.execute("select 1 from bills where bill_id=?", (bill_id,)).fetchone(),
                                     "账单不应被删")

    def test_rollback_paid_plan_blocked(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path, client = _client(tmpdir)
            plan_id, bill_id = self._confirm_and_bill(client)
            with connect(db_path) as conn:
                conn.execute("update bills set paid_fee=total_fee, state='paid' where bill_id=?", (bill_id,))
                conn.commit()
            r = client.post(f"/api/treatment-plans/{plan_id}/status", json={"status": "draft"})
            self.assertEqual(r.status_code, 409, "已收费计划不能撤回确认")
            with connect(db_path) as conn:
                state = conn.execute("select state from bills where bill_id=?", (bill_id,)).fetchone()[0]
            self.assertEqual(state, "paid", "已付账单不应被动")
