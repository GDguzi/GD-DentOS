"""消毒/灭菌追溯 · 器械库(首期)：分类 + 器械 CRUD + 停用 + 编号唯一(未删范围)。"""
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from local_app.api import create_app
from local_app.db import init_db


class SterilizationApiTest(unittest.TestCase):
    def _client(self, tmp):
        db = Path(tmp) / "clinic.sqlite3"
        init_db(db)
        return db, TestClient(create_app(db))

    def test_category_crud(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, client = self._client(tmp)
            cid = client.post("/api/instrument-categories", json={"name": "手机类"}).json()["id"]
            self.assertTrue(cid)
            cats = client.get("/api/instrument-categories").json()["categories"]
            self.assertEqual([c["name"] for c in cats], ["手机类"])
            client.put(f"/api/instrument-categories/{cid}", json={"name": "高速手机"})
            self.assertEqual(client.get("/api/instrument-categories").json()["categories"][0]["name"], "高速手机")
            client.delete(f"/api/instrument-categories/{cid}")
            self.assertEqual(client.get("/api/instrument-categories").json()["totalcount"], 0)

    def test_category_name_required(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, client = self._client(tmp)
            self.assertEqual(client.post("/api/instrument-categories", json={"name": "  "}).status_code, 400)

    def test_instrument_crud_and_category_link(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, client = self._client(tmp)
            cid = client.post("/api/instrument-categories", json={"name": "手机类"}).json()["id"]
            r = client.post("/api/instruments", json={"code": "QX-001", "name": "高速涡轮手机", "category_id": cid, "spec": "4孔"})
            self.assertEqual(r.status_code, 200)
            iid = r.json()["id"]
            lst = client.get("/api/instruments").json()["instruments"]
            self.assertEqual(len(lst), 1)
            self.assertEqual(lst[0]["category_name"], "手机类")
            # 编辑
            client.put(f"/api/instruments/{iid}", json={"name": "高速手机A型"})
            self.assertEqual(client.get("/api/instruments").json()["instruments"][0]["name"], "高速手机A型")

    def test_instrument_code_name_required_and_bad_category(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, client = self._client(tmp)
            self.assertEqual(client.post("/api/instruments", json={"name": "x"}).status_code, 400)
            self.assertEqual(client.post("/api/instruments", json={"code": "c1"}).status_code, 400)
            # 分类必填
            self.assertEqual(client.post("/api/instruments", json={"code": "c1", "name": "x"}).status_code, 400)
            self.assertEqual(client.post("/api/instruments", json={
                "code": "c1", "name": "x", "category_id": "nope"}).status_code, 400)

    def test_instrument_code_unique_among_active(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, client = self._client(tmp)
            cid = client.post("/api/instrument-categories", json={"name": "类"}).json()["id"]
            iid = client.post("/api/instruments", json={"code": "DUP", "name": "甲", "category_id": cid}).json()["id"]
            # 同编号重复 → 409
            self.assertEqual(client.post("/api/instruments", json={"code": "DUP", "name": "乙", "category_id": cid}).status_code, 409)
            # 软删后该编号可复用
            client.delete(f"/api/instruments/{iid}")
            self.assertEqual(client.post("/api/instruments", json={"code": "DUP", "name": "丙", "category_id": cid}).status_code, 200)

    def test_disable_instrument(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, client = self._client(tmp)
            cid = client.post("/api/instrument-categories", json={"name": "类"}).json()["id"]
            iid = client.post("/api/instruments", json={"code": "S1", "name": "探针", "category_id": cid}).json()["id"]
            r = client.put(f"/api/instruments/{iid}/disable")
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r.json()["status"], "disabled")
            # 仍在列表(停用≠删除)，可按 status 过滤
            self.assertEqual(client.get("/api/instruments?status=disabled").json()["totalcount"], 1)
            self.assertEqual(client.get("/api/instruments?status=active").json()["totalcount"], 0)

    def test_delete_and_404(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, client = self._client(tmp)
            cid = client.post("/api/instrument-categories", json={"name": "类"}).json()["id"]
            iid = client.post("/api/instruments", json={"code": "X1", "name": "镊子", "category_id": cid}).json()["id"]
            client.delete(f"/api/instruments/{iid}")
            self.assertEqual(client.get("/api/instruments").json()["totalcount"], 0)
            self.assertEqual(client.put(f"/api/instruments/{iid}", json={"name": "y"}).status_code, 404)
            self.assertEqual(client.delete("/api/instruments/nope").status_code, 404)


    def test_update_cannot_clear_category(self):
        # #86 编辑也不能把分类改空
        with tempfile.TemporaryDirectory() as tmp:
            _, client = self._client(tmp)
            cid = client.post("/api/instrument-categories", json={"name": "类"}).json()["id"]
            iid = client.post("/api/instruments", json={"code": "C1", "name": "器", "category_id": cid}).json()["id"]
            self.assertEqual(client.put(f"/api/instruments/{iid}", json={"category_id": ""}).status_code, 400)

    def test_delete_category_with_instruments_rejected(self):
        # #87 后端：分类下有器械时删除被拦(409),不留孤儿器械
        with tempfile.TemporaryDirectory() as tmp:
            _, client = self._client(tmp)
            cid = client.post("/api/instrument-categories", json={"name": "类"}).json()["id"]
            client.post("/api/instruments", json={"code": "C1", "name": "器", "category_id": cid})
            self.assertEqual(client.delete(f"/api/instrument-categories/{cid}").status_code, 409)


if __name__ == "__main__":
    unittest.main()
