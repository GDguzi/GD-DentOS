"""消毒·器械送消单(第1环)：CRUD + 状态机 draft→submitted→audited + 撤销审核。"""
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from local_app.api import create_app
from local_app.db import init_db


class DispatchApiTest(unittest.TestCase):
    def _client(self, tmp):
        db = Path(tmp) / "clinic.sqlite3"
        init_db(db)
        client = TestClient(create_app(db))
        # 器械需挂分类；两件器械备用
        cid = client.post("/api/instrument-categories", json={"name": "类"}).json()["id"]
        i1 = client.post("/api/instruments", json={"code": "A1", "name": "手机", "category_id": cid}).json()["id"]
        i2 = client.post("/api/instruments", json={"code": "A2", "name": "探针", "category_id": cid}).json()["id"]
        return db, client, i1, i2

    def _new(self, client, items, **head):
        """建送消单：默认带科室+送消人(必填)，可用 head 覆盖。"""
        payload = {"department": "种植科", "dispatcher": "张三", "items": items}
        payload.update(head)
        return client.post("/api/dispatch", json=payload)

    def test_create_with_items_and_get(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, client, i1, i2 = self._client(tmp)
            r = client.post("/api/dispatch", json={"department": "种植科", "dispatcher": "张三",
                                                   "items": [{"instrument_id": i1, "qty": 2}, {"instrument_id": i2, "qty": 1}]})
            self.assertEqual(r.status_code, 200)
            d = r.json()
            self.assertEqual(d["status"], "draft")
            self.assertTrue(d["dispatch_no"])
            self.assertEqual(len(d["items"]), 2)
            self.assertEqual(d["items"][0]["instrument_name"], "手机")
            got = client.get(f"/api/dispatch/{d['id']}").json()
            self.assertEqual(len(got["items"]), 2)

    def test_create_requires_items_and_valid_instrument(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, client, i1, _ = self._client(tmp)
            self.assertEqual(self._new(client, []).status_code, 400)
            self.assertEqual(self._new(client, [{"instrument_id": "nope", "qty": 1}]).status_code, 400)
            self.assertEqual(self._new(client, [{"instrument_id": i1, "qty": 0}]).status_code, 400)
            self.assertEqual(self._new(client, [{"instrument_id": i1, "qty": "nan"}]).status_code, 400)
            # 科室/送消人必填
            self.assertEqual(client.post("/api/dispatch", json={"dispatcher": "张三", "items": [{"instrument_id": i1, "qty": 1}]}).status_code, 400)
            self.assertEqual(client.post("/api/dispatch", json={"department": "种植科", "items": [{"instrument_id": i1, "qty": 1}]}).status_code, 400)

    def test_disabled_instrument_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, client, i1, _ = self._client(tmp)
            client.put(f"/api/instruments/{i1}/disable")
            self.assertEqual(self._new(client, [{"instrument_id": i1, "qty": 1}]).status_code, 400)

    def test_state_machine(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, client, i1, _ = self._client(tmp)
            did = self._new(client, [{"instrument_id": i1, "qty": 1}]).json()["id"]
            # 草稿不能审核(必须先送审)
            self.assertEqual(client.post(f"/api/dispatch/{did}/audit", json={"auditor": "李"}).status_code, 409)
            # 送审
            self.assertEqual(client.post(f"/api/dispatch/{did}/submit").json()["status"], "submitted")
            # 重复送审 409
            self.assertEqual(client.post(f"/api/dispatch/{did}/submit").status_code, 409)
            # 审核须填审核人
            self.assertEqual(client.post(f"/api/dispatch/{did}/audit", json={}).status_code, 400)
            # 审核
            audited = client.post(f"/api/dispatch/{did}/audit", json={"auditor": "李主任"}).json()
            self.assertEqual(audited["status"], "audited")
            self.assertEqual(audited["auditor"], "李主任")
            self.assertTrue(audited["audited_at"])
            # 撤销审核须填操作人, 回到 submitted
            self.assertEqual(client.post(f"/api/dispatch/{did}/cancel-audit", json={}).status_code, 400)
            reverted = client.post(f"/api/dispatch/{did}/cancel-audit", json={"operator": "王"}).json()
            self.assertEqual(reverted["status"], "submitted")
            self.assertEqual(reverted["auditor"], "")

    def test_edit_and_delete_only_draft(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, client, i1, i2 = self._client(tmp)
            did = self._new(client, [{"instrument_id": i1, "qty": 1}]).json()["id"]
            # 草稿可改明细
            upd = client.put(f"/api/dispatch/{did}", json={"department": "正畸科",
                                                           "items": [{"instrument_id": i2, "qty": 3}]}).json()
            self.assertEqual(upd["department"], "正畸科")
            self.assertEqual(len(upd["items"]), 1)
            self.assertEqual(upd["items"][0]["qty"], 3)
            # 送审后不能编辑/删除
            client.post(f"/api/dispatch/{did}/submit")
            self.assertEqual(client.put(f"/api/dispatch/{did}", json={"department": "x"}).status_code, 409)
            self.assertEqual(client.delete(f"/api/dispatch/{did}").status_code, 409)

    def test_submit_blocked_when_head_cleared(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, client, i1, _ = self._client(tmp)
            did = self._new(client, [{"instrument_id": i1, "qty": 1}]).json()["id"]
            # 草稿里把科室清空 → 不能送审
            client.put(f"/api/dispatch/{did}", json={"department": ""})
            self.assertEqual(client.post(f"/api/dispatch/{did}/submit").status_code, 400)

    def test_delete_draft(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, client, i1, _ = self._client(tmp)
            did = self._new(client, [{"instrument_id": i1, "qty": 1}]).json()["id"]
            self.assertEqual(client.delete(f"/api/dispatch/{did}").status_code, 200)
            self.assertEqual(client.get(f"/api/dispatch/{did}").status_code, 404)
            self.assertEqual(client.get("/api/dispatch").json()["totalcount"], 0)

    def test_list_status_filter(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, client, i1, _ = self._client(tmp)
            d1 = self._new(client, [{"instrument_id": i1, "qty": 1}]).json()["id"]
            self._new(client, [{"instrument_id": i1, "qty": 1}])
            client.post(f"/api/dispatch/{d1}/submit")
            self.assertEqual(client.get("/api/dispatch?status=draft").json()["totalcount"], 1)
            self.assertEqual(client.get("/api/dispatch?status=submitted").json()["totalcount"], 1)


if __name__ == "__main__":
    unittest.main()
