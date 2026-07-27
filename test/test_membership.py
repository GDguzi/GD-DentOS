import tempfile
import unittest
import uuid
from pathlib import Path

from fastapi.testclient import TestClient

from local_app import auth
from local_app.api import create_app
from local_app.db import connect, init_db


def _client(tmpdir, login="boss"):
    db_path = Path(tmpdir) / "clinic.sqlite3"
    init_db(db_path)
    auth.ensure_seed_roles(db_path)   # member-account 读按 billing.view 守卫,需种子角色权限,前台才有 billing.view
    with connect(db_path) as conn:
        conn.execute(
            "insert into patients(patient_identity, display_name, updated_at, current_hash) "
            "values ('p1', '测试患者', '2026-06-19 00:00:00', 'h1')"
        )
        # 会员改钱/开关需管理员→测试建 admin boss + 前台 qt(qt 用于 403)
        auth.create_user(conn, "boss", "院长", "admin123", role="admin")
        auth.create_user(conn, "qt", "前台", "pw123456", role="reception")
        conn.commit()
    client = TestClient(create_app(db_path))
    if login:
        client.post("/api/auth/login", json={"username": login,
                    "password": {"boss": "admin123", "qt": "pw123456"}[login]})
    return db_path, client


class MemberTopupTest(unittest.TestCase):
    def test_topup_increases_balance(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, client = _client(tmp)
            self.assertEqual(client.get("/api/patients/p1/member-account").json()["balance"], 0)
            r = client.post("/api/patients/p1/member-account/topup", json={"amount": 1000, "request_id": uuid.uuid4().hex})
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r.json()["balance"], 1000)
            r2 = client.post("/api/patients/p1/member-account/topup", json={"amount": 500, "request_id": uuid.uuid4().hex})
            self.assertEqual(r2.json()["balance"], 1500)
            acc = client.get("/api/patients/p1/member-account").json()
            self.assertEqual(acc["balance"], 1500)
            self.assertEqual(len(acc["transactions"]), 2)

    def test_topup_rejects_bad_amount(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, client = _client(tmp)
            for bad in [0, -100, "abc"]:
                self.assertEqual(
                    client.post("/api/patients/p1/member-account/topup", json={"amount": bad, "request_id": uuid.uuid4().hex}).status_code,
                    400, f"amount={bad}")

    def test_subcent_amount_rejected(self):
        # 四舍五入到分后为 0 的亚分额(0.004)应 400，不是 200 空操作
        with tempfile.TemporaryDirectory() as tmp:
            _, client = _client(tmp)
            self.assertEqual(
                client.post("/api/patients/p1/member-account/topup", json={"amount": 0.004, "request_id": uuid.uuid4().hex}).status_code, 400)
            # 0.01 是合法最小额
            self.assertEqual(
                client.post("/api/patients/p1/member-account/topup", json={"amount": 0.01, "request_id": uuid.uuid4().hex}).status_code, 200)

    def test_concurrent_consume_no_overdraft(self):
        # 并发同患者消费不能把余额扣成负数(写锁串行化)
        import threading
        with tempfile.TemporaryDirectory() as tmp:
            db_path, client = _client(tmp)
            client.post("/api/patients/p1/member-account/topup", json={"amount": 1000, "request_id": uuid.uuid4().hex})
            results = []

            def consume():
                r = client.post("/api/patients/p1/member-account/consume", json={"amount": 600, "request_id": uuid.uuid4().hex})
                results.append(r.status_code)

            threads = [threading.Thread(target=consume) for _ in range(2)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            # 两笔各 600，余额 1000 → 只能成功一笔，另一笔余额不足
            self.assertEqual(sorted(results), [200, 400], f"results={results}")
            self.assertEqual(client.get("/api/patients/p1/member-account").json()["balance"], 400)

    def test_unknown_patient_404(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, client = _client(tmp)
            self.assertEqual(client.get("/api/patients/nope/member-account").status_code, 404)
            self.assertEqual(
                client.post("/api/patients/nope/member-account/topup", json={"amount": 1, "request_id": uuid.uuid4().hex}).status_code, 404)


class MemberConsumeTest(unittest.TestCase):
    def test_consume_deducts_and_blocks_overdraft(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path, client = _client(tmp)
            client.post("/api/patients/p1/member-account/topup", json={"amount": 1000, "request_id": uuid.uuid4().hex})
            r = client.post("/api/patients/p1/member-account/consume", json={"amount": 300, "bill_id": "local-bill-1", "request_id": uuid.uuid4().hex})
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r.json()["balance"], 700)
            # 超额消费被挡，余额不变
            r2 = client.post("/api/patients/p1/member-account/consume", json={"amount": 999, "request_id": uuid.uuid4().hex})
            self.assertEqual(r2.status_code, 400)
            self.assertEqual(client.get("/api/patients/p1/member-account").json()["balance"], 700)

    def test_consume_records_bill_link(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path, client = _client(tmp)
            client.post("/api/patients/p1/member-account/topup", json={"amount": 200, "request_id": uuid.uuid4().hex})
            client.post("/api/patients/p1/member-account/consume", json={"amount": 50, "bill_id": "local-bill-9", "request_id": uuid.uuid4().hex})
            with connect(db_path) as conn:
                row = conn.execute(
                    "select bill_id, txn_type from member_transactions where txn_type='consume'").fetchone()
            self.assertEqual(row["bill_id"], "local-bill-9")


class MemberRefundTest(unittest.TestCase):
    def test_refund_cash_deducts_balance(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, client = _client(tmp)
            client.post("/api/patients/p1/member-account/topup", json={"amount": 1000, "request_id": uuid.uuid4().hex})
            r = client.post("/api/patients/p1/member-account/refund", json={"amount": 400, "request_id": uuid.uuid4().hex})
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r.json()["balance"], 600)

    def test_refund_cannot_exceed_balance(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, client = _client(tmp)
            client.post("/api/patients/p1/member-account/topup", json={"amount": 100, "request_id": uuid.uuid4().hex})
            self.assertEqual(
                client.post("/api/patients/p1/member-account/refund", json={"amount": 200, "request_id": uuid.uuid4().hex}).status_code, 400)
            self.assertEqual(client.get("/api/patients/p1/member-account").json()["balance"], 100)


class MemberLedgerTest(unittest.TestCase):
    def test_full_flow_balance_chain(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, client = _client(tmp)
            client.post("/api/patients/p1/member-account/topup", json={"amount": 1000, "request_id": uuid.uuid4().hex})
            client.post("/api/patients/p1/member-account/consume", json={"amount": 300, "request_id": uuid.uuid4().hex})
            client.post("/api/patients/p1/member-account/refund", json={"amount": 200, "request_id": uuid.uuid4().hex})
            acc = client.get("/api/patients/p1/member-account").json()
            self.assertEqual(acc["balance"], 500)   # 1000 - 300 - 200
            self.assertEqual(len(acc["transactions"]), 3)
            # 每笔 balance_after 都记录(审计链)
            types = {t["txn_type"] for t in acc["transactions"]}
            self.assertEqual(types, {"topup", "consume", "refund"})


class MemberToggleTest(unittest.TestCase):
    def test_default_enabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, client = _client(tmp)
            self.assertTrue(client.get("/api/settings/features").json()["membership_enabled"])

    def test_string_false_disables(self):
        # 前端可能传字符串 "false"，必须真关闭(不能 bool("false")=True 误判为开)
        with tempfile.TemporaryDirectory() as tmp:
            _, client = _client(tmp)
            r = client.put("/api/settings/features", json={"membership_enabled": "false"})
            self.assertFalse(r.json()["membership_enabled"])
            self.assertEqual(
                client.post("/api/patients/p1/member-account/topup", json={"amount": 100, "request_id": uuid.uuid4().hex}).status_code, 403)
            # 字符串 "true" 仍开启
            client.put("/api/settings/features", json={"membership_enabled": "true"})
            self.assertEqual(
                client.post("/api/patients/p1/member-account/topup", json={"amount": 100, "request_id": uuid.uuid4().hex}).status_code, 200)

    def test_disable_blocks_mutations_but_allows_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, client = _client(tmp)
            client.post("/api/patients/p1/member-account/topup", json={"amount": 500, "request_id": uuid.uuid4().hex})
            # 关闭会员开关
            r = client.put("/api/settings/features", json={"membership_enabled": False})
            self.assertEqual(r.status_code, 200)
            self.assertFalse(r.json()["membership_enabled"])
            # 充值/消费/退储值全被挡
            self.assertEqual(client.post("/api/patients/p1/member-account/topup", json={"amount": 100, "request_id": uuid.uuid4().hex}).status_code, 403)
            self.assertEqual(client.post("/api/patients/p1/member-account/consume", json={"amount": 100, "request_id": uuid.uuid4().hex}).status_code, 403)
            self.assertEqual(client.post("/api/patients/p1/member-account/refund", json={"amount": 100, "request_id": uuid.uuid4().hex}).status_code, 403)
            # 仍能读余额/流水(看历史)
            self.assertEqual(client.get("/api/patients/p1/member-account").json()["balance"], 500)
            # 重新开启后恢复
            client.put("/api/settings/features", json={"membership_enabled": True})
            self.assertEqual(client.post("/api/patients/p1/member-account/topup", json={"amount": 100, "request_id": uuid.uuid4().hex}).status_code, 200)


if __name__ == "__main__":
    unittest.main()


class MemberPermissionTest(unittest.TestCase):
    def test_non_admin_cannot_change_balance_or_toggle(self):
        # 前台(reception)不能充值/退现/改会员开关 → 403
        with tempfile.TemporaryDirectory() as tmp:
            db, client = _client(tmp, login="qt")   # 前台登录
            self.assertEqual(client.post("/api/patients/p1/member-account/topup", json={"amount": 100, "request_id": uuid.uuid4().hex}).status_code, 403)
            self.assertEqual(client.post("/api/patients/p1/member-account/refund", json={"amount": 50, "request_id": uuid.uuid4().hex}).status_code, 403)
            self.assertEqual(client.put("/api/settings/features", json={"membership_enabled": False}).status_code, 403)
            # 但读余额可以(非改钱)
            self.assertEqual(client.get("/api/patients/p1/member-account").status_code, 200)


class MemberIdempotencyTest(unittest.TestCase):
    """审计R4：充值/消费/退储值幂等——同号同载荷重放首次结果只入账一次,
    同号异载荷 409,缺号 400(复用 payment_requests 范式,口径对齐)。"""

    def test_topup_same_request_id_only_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, client = _client(tmp)
            body = {"amount": 100, "request_id": uuid.uuid4().hex}
            r1 = client.post("/api/patients/p1/member-account/topup", json=body)
            self.assertEqual(r1.status_code, 200, r1.text)
            r2 = client.post("/api/patients/p1/member-account/topup", json=body)
            self.assertEqual(r2.status_code, 200)
            self.assertEqual(r2.json(), r1.json())   # 重放=原响应
            acc = client.get("/api/patients/p1/member-account").json()
            self.assertEqual(acc["balance"], 100)    # 只入账一次
            self.assertEqual(len(acc["transactions"]), 1)

    def test_consume_and_refund_same_request_id_only_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, client = _client(tmp)
            client.post("/api/patients/p1/member-account/topup",
                        json={"amount": 1000, "request_id": uuid.uuid4().hex})
            for act, amount, want_balance in [("consume", 300, 700), ("refund", 200, 500)]:
                body = {"amount": amount, "request_id": uuid.uuid4().hex}
                r1 = client.post(f"/api/patients/p1/member-account/{act}", json=body)
                self.assertEqual(r1.status_code, 200, r1.text)
                r2 = client.post(f"/api/patients/p1/member-account/{act}", json=body)
                self.assertEqual(r2.status_code, 200)
                self.assertEqual(r2.json(), r1.json())
                self.assertEqual(
                    client.get("/api/patients/p1/member-account").json()["balance"],
                    want_balance, act)   # 只扣/退一次
            self.assertEqual(
                len(client.get("/api/patients/p1/member-account").json()["transactions"]), 3)

    def test_same_request_id_different_payload_409(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, client = _client(tmp)
            rid = uuid.uuid4().hex
            client.post("/api/patients/p1/member-account/topup",
                        json={"amount": 100, "request_id": rid})
            r = client.post("/api/patients/p1/member-account/topup",
                            json={"amount": 200, "request_id": rid})
            self.assertEqual(r.status_code, 409)
            self.assertEqual(client.get("/api/patients/p1/member-account").json()["balance"], 100)

    def test_missing_request_id_400(self):
        # 旧调用不带 request_id：按 口径改钱路径必填,缺号/空号一律 400,不落账
        with tempfile.TemporaryDirectory() as tmp:
            _, client = _client(tmp)
            client.post("/api/patients/p1/member-account/topup",
                        json={"amount": 100, "request_id": uuid.uuid4().hex})
            for act in ["topup", "consume", "refund"]:
                self.assertEqual(client.post(f"/api/patients/p1/member-account/{act}",
                                             json={"amount": 10}).status_code, 400, act)
                self.assertEqual(client.post(f"/api/patients/p1/member-account/{act}",
                                             json={"amount": 10, "request_id": "  "}).status_code, 400, act)
            self.assertEqual(client.get("/api/patients/p1/member-account").json()["balance"], 100)

    def test_failed_validation_does_not_burn_request_id(self):
        # 与 同口径：校验失败(余额不足)不落幂等登记,改对金额后同号可重来
        with tempfile.TemporaryDirectory() as tmp:
            _, client = _client(tmp)
            client.post("/api/patients/p1/member-account/topup",
                        json={"amount": 100, "request_id": uuid.uuid4().hex})
            rid = uuid.uuid4().hex
            r = client.post("/api/patients/p1/member-account/consume",
                            json={"amount": 999, "request_id": rid})
            self.assertEqual(r.status_code, 400)
            r2 = client.post("/api/patients/p1/member-account/consume",
                             json={"amount": 50, "request_id": rid})
            self.assertEqual(r2.status_code, 200)
            self.assertEqual(r2.json()["balance"], 50)


class MemberAuditInPatientViewTest(unittest.TestCase):
    def test_member_txn_appears_in_patient_audit(self):
        # 会员充值流水要出现在患者审计页 + 审计是JSON
        with tempfile.TemporaryDirectory() as tmp:
            db, client = _client(tmp)
            client.post("/api/patients/p1/member-account/topup", json={"amount": 500, "request_id": uuid.uuid4().hex})
            rows = client.get("/api/patients/p1/audit-logs").json()["list"]
            self.assertTrue(any(r.get("entity_type") == "member_account" for r in rows), rows)
