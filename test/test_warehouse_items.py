import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from local_app.api import create_app
from local_app.db import connect, init_db


def _client(tmpdir):
    db_path = Path(tmpdir) / "clinic.sqlite3"
    init_db(db_path)
    return db_path, TestClient(create_app(db_path))


class WarehouseCategoryTest(unittest.TestCase):
    def test_category_crud_soft_deletes(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path, client = _client(tmpdir)
            created = client.post(
                "/api/stock-categories", json={"name": "种植耗材", "sort": 10}
            )
            self.assertEqual(created.status_code, 200)
            category_id = created.json()["id"]

            listed = client.get("/api/stock-categories").json()
            self.assertEqual(listed["totalcount"], 1)
            self.assertEqual(listed["categories"][0]["name"], "种植耗材")

            updated = client.put(
                f"/api/stock-categories/{category_id}",
                json={"name": "常用耗材", "sort": 20},
            )
            self.assertEqual(updated.status_code, 200)
            self.assertEqual(updated.json()["name"], "常用耗材")

            deleted = client.delete(f"/api/stock-categories/{category_id}")
            self.assertEqual(deleted.status_code, 200)
            self.assertEqual(client.get("/api/stock-categories").json()["totalcount"], 0)
            with connect(db_path) as conn:
                row = conn.execute(
                    "select is_deleted from stock_category where id = ?", (category_id,)
                ).fetchone()
            self.assertEqual(row["is_deleted"], 1)

    def test_category_requires_name(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            _, client = _client(tmpdir)
            resp = client.post("/api/stock-categories", json={"name": "  "})
            self.assertEqual(resp.status_code, 400)


class WarehouseItemTest(unittest.TestCase):
    def _category_id(self, client):
        return client.post("/api/stock-categories", json={"name": "耗材"}).json()["id"]

    def _create_item(self, client, category_id, code="ST-001", name="一次性口镜"):
        return client.post(
            "/api/stock-items",
            json={
                "code": code,
                "name": name,
                "category_id": category_id,
                "spec": "成人",
                "unit": "个",
                "brand": "本地",
                "price": 2.5,
                "is_high_value": 0,
                "min_qty": 10,
                "max_qty": 200,
                "shelf_life_days": 365,
                "status": "active",
            },
        )

    def test_item_crud_filters_and_soft_deletes(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path, client = _client(tmpdir)
            category_id = self._category_id(client)
            created = self._create_item(client, category_id)
            self.assertEqual(created.status_code, 200)
            item_id = created.json()["id"]

            by_code = client.get("/api/stock-items", params={"code": "ST-001"}).json()
            self.assertEqual(by_code["totalcount"], 1)
            self.assertEqual(by_code["items"][0]["name"], "一次性口镜")

            by_name = client.get("/api/stock-items", params={"name": "口镜"}).json()
            self.assertEqual(by_name["totalcount"], 1)
            by_category = client.get(
                "/api/stock-items", params={"category_id": category_id}
            ).json()
            self.assertEqual(by_category["totalcount"], 1)

            updated = client.put(
                f"/api/stock-items/{item_id}",
                json={"name": "一次性检查口镜", "price": 3, "min_qty": 20},
            )
            self.assertEqual(updated.status_code, 200)
            self.assertEqual(updated.json()["name"], "一次性检查口镜")
            self.assertEqual(updated.json()["min_qty"], 20)

            deleted = client.delete(f"/api/stock-items/{item_id}")
            self.assertEqual(deleted.status_code, 200)
            self.assertEqual(client.get("/api/stock-items").json()["totalcount"], 0)
            with connect(db_path) as conn:
                row = conn.execute(
                    "select is_deleted from stock_item where id = ?", (item_id,)
                ).fetchone()
            self.assertEqual(row["is_deleted"], 1)

    def test_item_rejects_required_fields_and_duplicate_code(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            _, client = _client(tmpdir)
            category_id = self._category_id(client)
            missing = client.post("/api/stock-items", json={"code": "", "name": ""})
            self.assertEqual(missing.status_code, 400)

            first = self._create_item(client, category_id)
            self.assertEqual(first.status_code, 200)
            duplicate = self._create_item(client, category_id)
            self.assertEqual(duplicate.status_code, 409)


if __name__ == "__main__":
    unittest.main()
