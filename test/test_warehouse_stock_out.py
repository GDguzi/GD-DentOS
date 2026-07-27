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


def _seed_stock(client, qty=5):
    category_id = client.post("/api/stock-categories", json={"name": "耗材"}).json()["id"]
    item_id = client.post(
        "/api/stock-items",
        json={"code": "OUT-001", "name": "缝合线", "category_id": category_id, "unit": "包"},
    ).json()["id"]
    supplier_id = client.post("/api/suppliers", json={"name": "供应商"}).json()["id"]
    stock_in_id = client.post(
        "/api/stock-in",
        json={
            "type": "purchase",
            "supplier_id": supplier_id,
            "items": [{"stock_item_id": item_id, "qty": qty, "batch_no": "B1"}],
        },
    ).json()["id"]
    client.post(f"/api/stock-in/{stock_in_id}/confirm")
    return item_id, supplier_id


class WarehouseStockOutTest(unittest.TestCase):
    def test_create_and_confirm_decreases_balance_and_writes_ledger(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path, client = _client(tmpdir)
            item_id, _ = _seed_stock(client, qty=5)

            created = client.post(
                "/api/stock-out",
                json={
                    "type": "领用",
                    "operator": "库管",
                    "handler": "护士",
                    "receiver": "一诊室",
                    "items": [{"stock_item_id": item_id, "qty": 2}],
                },
            )
            self.assertEqual(created.status_code, 200)
            stock_out_id = created.json()["id"]
            self.assertEqual(created.json()["status"], "draft")

            listed = client.get("/api/stock-out").json()
            self.assertEqual(listed["totalcount"], 1)
            self.assertEqual(listed["stock_out"][0]["status"], "draft")

            confirmed = client.post(f"/api/stock-out/{stock_out_id}/confirm")
            self.assertEqual(confirmed.status_code, 200)
            self.assertEqual(confirmed.json()["status"], "confirmed")

            balance = client.get("/api/stock-balance", params={"item": "OUT-001"}).json()
            self.assertEqual(balance["items"][0]["qty"], 3)
            with connect(db_path) as conn:
                ledger = conn.execute(
                    """
                    select change_qty, source_type, source_id
                    from stock_ledger
                    where stock_item_id = ? and source_type = 'stock_out'
                    """,
                    (item_id,),
                ).fetchone()
            self.assertEqual(ledger["change_qty"], -2)
            self.assertEqual(ledger["source_id"], stock_out_id)

            again = client.post(f"/api/stock-out/{stock_out_id}/confirm")
            self.assertEqual(again.status_code, 409)

    def test_confirm_rejects_insufficient_stock_without_changing_balance(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            _, client = _client(tmpdir)
            item_id, _ = _seed_stock(client, qty=5)
            stock_out_id = client.post(
                "/api/stock-out",
                json={"type": "use", "items": [{"stock_item_id": item_id, "qty": 6}]},
            ).json()["id"]

            rejected = client.post(f"/api/stock-out/{stock_out_id}/confirm")
            self.assertEqual(rejected.status_code, 409)
            balance = client.get("/api/stock-balance", params={"item": "OUT-001"}).json()
            self.assertEqual(balance["items"][0]["qty"], 5)

    def test_create_rejects_bad_type_empty_items_and_unknown_item(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            _, client = _client(tmpdir)
            self.assertEqual(
                client.post(
                    "/api/stock-out",
                    json={"type": "盘亏", "items": [{"stock_item_id": "x", "qty": 1}]},
                ).status_code,
                400,
            )
            self.assertEqual(
                client.post("/api/stock-out", json={"type": "use", "items": []}).status_code,
                400,
            )
            self.assertEqual(
                client.post(
                    "/api/stock-out",
                    json={"type": "use", "items": [{"stock_item_id": "missing", "qty": 1}]},
                ).status_code,
                404,
            )


if __name__ == "__main__":
    unittest.main()
