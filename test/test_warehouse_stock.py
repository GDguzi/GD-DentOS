import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from local_app.api import create_app
from local_app.db import init_db


def _client(tmpdir):
    db_path = Path(tmpdir) / "clinic.sqlite3"
    init_db(db_path)
    return TestClient(create_app(db_path))


def _seed_low_stock(client):
    category_id = client.post("/api/stock-categories", json={"name": "种植"}).json()["id"]
    item_id = client.post(
        "/api/stock-items",
        json={
            "code": "BAL-001",
            "name": "愈合基台",
            "category_id": category_id,
            "unit": "个",
            "min_qty": 10,
            "max_qty": 100,
        },
    ).json()["id"]
    supplier_id = client.post("/api/suppliers", json={"name": "供应商"}).json()["id"]
    stock_in_id = client.post(
        "/api/stock-in",
        json={
            "type": "purchase",
            "supplier_id": supplier_id,
            "items": [
                {
                    "stock_item_id": item_id,
                    "qty": 5,
                    "batch_no": "B1",
                    "expire_date": "2027-01-01",
                }
            ],
        },
    ).json()["id"]
    client.post(f"/api/stock-in/{stock_in_id}/confirm")
    return item_id, category_id


class WarehouseStockBalanceTest(unittest.TestCase):
    def test_stock_balance_summary_and_detail_filters(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            client = _client(tmpdir)
            item_id, category_id = _seed_low_stock(client)

            summary = client.get("/api/stock-balance", params={"item": "BAL-001"}).json()
            self.assertEqual(summary["totalcount"], 1)
            row = summary["items"][0]
            self.assertEqual(row["stock_item_id"], item_id)
            self.assertEqual(row["code"], "BAL-001")
            self.assertEqual(row["qty"], 5)

            by_category = client.get(
                "/api/stock-balance", params={"category_id": category_id}
            ).json()
            self.assertEqual(by_category["totalcount"], 1)

            detail = client.get("/api/stock-balance", params={"detail": "1"}).json()
            self.assertEqual(detail["totalcount"], 1)
            self.assertEqual(detail["items"][0]["batch_no"], "B1")
            self.assertEqual(detail["items"][0]["expire_date"], "2027-01-01")

    def test_low_stock_alerts(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            client = _client(tmpdir)
            item_id, _ = _seed_low_stock(client)

            alerts = client.get("/api/stock-balance/alerts").json()
            self.assertEqual(alerts["totalcount"], 1)
            alert = alerts["alerts"][0]
            self.assertEqual(alert["stock_item_id"], item_id)
            self.assertEqual(alert["current_qty"], 5)
            self.assertEqual(alert["min_qty"], 10)
            self.assertEqual(alert["reason"], "low_stock")


if __name__ == "__main__":
    unittest.main()
