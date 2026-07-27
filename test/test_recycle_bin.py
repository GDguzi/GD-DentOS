"""回收站 P4：软删→列出(谁删的)→还原往返；登录守卫。"""
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from local_app import auth
from local_app.api import create_app
from local_app.db import connect, init_db


class RecycleBinTest(unittest.TestCase):
    def _setup(self, tmp):
        db = Path(tmp) / "clinic.sqlite3"
        init_db(db)
        auth.ensure_seed_roles(db)   # 种子角色:reception 拿到 patient.edit(回访/标签写现已守卫)
        with connect(db) as conn:
            auth.create_user(conn, "yh", "运花", "pw123456", role="reception")
            conn.execute("insert into patients(patient_identity, display_name, updated_at, current_hash) "
                         "values ('p1','张三','2026-06-16','h1')")
            conn.commit()
        client = TestClient(create_app(db))
        client.post("/api/auth/login", json={"username": "yh", "password": "pw123456"})
        return db, client

    def test_requires_login(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "clinic.sqlite3"
            init_db(db)
            client = TestClient(create_app(db))
            self.assertEqual(client.get("/api/recycle-bin").status_code, 401)

    def test_delete_list_restore_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            db, client = self._setup(tmp)
            rv = client.post("/api/patients/p1/return-visits",
                             json={"due_time": "2026-06-20", "item_name": "种牙复诊"}).json()
            rvid = rv["return_visit_id"]
            # 软删
            self.assertEqual(client.request(
                "DELETE", f"/api/return-visits/{rvid}",
                json={"expected_revision": 1},
            ).status_code, 200)
            # 回收站列出，带删除人
            data = client.get("/api/recycle-bin").json()
            self.assertEqual(data["totalcount"], 1)
            item = data["items"][0]
            self.assertEqual(item["entity_type"], "return_visit")
            self.assertEqual(item["deleted_by"], "yh")
            self.assertIn("种牙复诊", item["title"])
            # 还原
            r = client.post("/api/recycle-bin/restore",
                            json={"entity_type": "return_visit", "entity_id": rvid})
            self.assertEqual(r.status_code, 200)
            self.assertEqual(client.get("/api/recycle-bin").json()["totalcount"], 0)
            # 还原后回访重新可见(未删)
            with connect(db) as conn:
                self.assertEqual(conn.execute("select is_deleted from return_visits where return_visit_id=?",
                                              (rvid,)).fetchone()[0], 0)

    def test_restore_voided_bill_cascade(self):
        # 还原作废账单→state=pending(合法态,非'open')+级联还原处置单/明细
        with tempfile.TemporaryDirectory() as tmp:
            db, client = self._setup(tmp)
            with connect(db) as conn:
                auth.create_user(conn, "boss", "老板", "pw123456", role="admin")
                conn.commit()
            boss = TestClient(create_app(db)); boss.post("/api/auth/login", json={"username": "boss", "password": "pw123456"})
            oid = boss.post("/api/patients/p1/treatment-orders",
                            json={"items": [{"item_name": "洁牙", "unit_price": 100, "quantity": 1}]}).json()["order_id"]
            bill = boss.post(f"/api/treatment-orders/{oid}/price", json={}).json()["bill_id"]
            boss.post(f"/api/treatment-orders/{oid}/void", json={"reason": "划价错"})
            r = boss.post("/api/recycle-bin/restore", json={"entity_type": "bill", "entity_id": bill})
            self.assertEqual(r.status_code, 200, r.text)
            with connect(db) as conn:
                self.assertEqual(conn.execute("select state from bills where bill_id=?", (bill,)).fetchone()[0], "pending")
                self.assertEqual(conn.execute("select status from treatment_orders where order_id=?", (oid,)).fetchone()[0], "priced")
                self.assertEqual(conn.execute("select count(*) from treatment_items where order_id=? and is_deleted=0", (oid,)).fetchone()[0], 1)

    def test_restore_bad_type_and_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            db, client = self._setup(tmp)
            # 坏类型 → 400(权限校验前)
            self.assertEqual(client.post("/api/recycle-bin/restore",
                             json={"entity_type": "bogus", "entity_id": "x"}).status_code, 400)
            # 不存在记录：用 admin(过权限)验 404
            with connect(db) as conn:
                auth.create_user(conn, "boss", "老板", "pw123456", role="admin")
                conn.commit()
            boss = TestClient(create_app(db)); boss.post("/api/auth/login", json={"username": "boss", "password": "pw123456"})
            self.assertEqual(boss.post("/api/recycle-bin/restore",
                             json={"entity_type": "return_visit", "entity_id": "nope"}).status_code, 404)


    def test_restore_permission_owner_or_admin(self):
        # 仅本人或 admin 可还原
        with tempfile.TemporaryDirectory() as tmp:
            db, client = self._setup(tmp)  # yh 登录
            with connect(db) as conn:
                auth.create_user(conn, "other", "其他", "pw123456", role="reception")
                auth.create_user(conn, "boss", "老板", "pw123456", role="admin")
                conn.commit()
            rvid = client.post("/api/patients/p1/return-visits",
                               json={"due_time": "2026-06-20", "item_name": "复诊"}).json()["return_visit_id"]
            client.request(
                "DELETE", f"/api/return-visits/{rvid}",
                json={"expected_revision": 1},
            )  # yh 删
            other = TestClient(create_app(db)); other.post("/api/auth/login", json={"username": "other", "password": "pw123456"})
            self.assertEqual(other.post("/api/recycle-bin/restore",
                             json={"entity_type": "return_visit", "entity_id": rvid}).status_code, 403)
            boss = TestClient(create_app(db)); boss.post("/api/auth/login", json={"username": "boss", "password": "pw123456"})
            self.assertEqual(boss.post("/api/recycle-bin/restore",
                             json={"entity_type": "return_visit", "entity_id": rvid}).status_code, 200)

    def test_voided_bill_listed(self):
        # 作废账单进回收站
        with tempfile.TemporaryDirectory() as tmp:
            db, client = self._setup(tmp)
            with connect(db) as conn:
                conn.execute("insert into bills(bill_id, patient_identity, bill_no, bill_time, total_fee, paid_fee, state) "
                             "values ('b1','p1','B1','2026-06-16',500,0,'voided')")
                conn.commit()
            items = client.get("/api/recycle-bin").json()["items"]
            self.assertTrue(any(i["entity_type"] == "bill" and i["entity_id"] == "b1" for i in items))

    def test_dispatch_delete_writes_audit(self):
        # 送消单删除写审计(回收站可显示谁删的)
        with tempfile.TemporaryDirectory() as tmp:
            db, client = self._setup(tmp)
            # 消毒接口现需 sterilize.manage:种子角色 + 以护士(有该权)操作
            auth.ensure_seed_roles(db)
            with connect(db) as conn:
                auth.create_user(conn, "ns", "护士", "pw123456", role="nurse")
                conn.commit()
            client.post("/api/auth/login", json={"username": "ns", "password": "pw123456"})
            cid = client.post("/api/instrument-categories", json={"name": "类"}).json()["id"]
            iid = client.post("/api/instruments", json={"code": "A1", "name": "手机", "category_id": cid}).json()["id"]
            did = client.post("/api/dispatch", json={"department": "种植科", "dispatcher": "张",
                                                     "items": [{"instrument_id": iid, "qty": 1}]}).json()["id"]
            client.delete(f"/api/dispatch/{did}")
            with connect(db) as conn:
                row = conn.execute("select operator from audit_logs where entity_type='sterilize_dispatch' "
                                   "and action='soft_delete_dispatch' and entity_id=?", (did,)).fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(row[0], "ns")


if __name__ == "__main__":
    unittest.main()
