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
        json={"code": "TAKE-001", "name": "骨粉", "category_id": category_id, "unit": "瓶"},
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
    return item_id


class WarehouseStockTakeTest(unittest.TestCase):
    def test_confirm_stock_take_adjusts_gain_and_loss(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path, client = _client(tmpdir)
            item_id = _seed_stock(client, qty=5)

            gain = client.post(
                "/api/stock-take",
                json={"operator": "库管", "items": [{"stock_item_id": item_id, "actual_qty": 7}]},
            )
            self.assertEqual(gain.status_code, 200)
            take_id = gain.json()["id"]
            self.assertEqual(gain.json()["status"], "draft")
            self.assertEqual(gain.json()["items"][0]["book_qty"], 5)
            self.assertEqual(gain.json()["items"][0]["diff"], 2)

            confirmed = client.post(f"/api/stock-take/{take_id}/confirm")
            self.assertEqual(confirmed.status_code, 200)
            self.assertEqual(confirmed.json()["status"], "confirmed")
            balance = client.get("/api/stock-balance", params={"item": "TAKE-001"}).json()
            self.assertEqual(balance["items"][0]["qty"], 7)
            with connect(db_path) as conn:
                gain_diff = conn.execute(
                    "select diff from stock_take_item where stock_take_id = ?", (take_id,)
                ).fetchone()["diff"]
                gain_ledger = conn.execute(
                    """
                    select change_qty
                    from stock_ledger
                    where source_type = 'stock_take' and source_id = ?
                    """,
                    (take_id,),
                ).fetchone()["change_qty"]
            self.assertEqual(gain_diff, 2)
            self.assertEqual(gain_ledger, 2)

            self.assertEqual(client.post(f"/api/stock-take/{take_id}/confirm").status_code, 409)

            loss_id = client.post(
                "/api/stock-take",
                json={"operator": "库管", "items": [{"stock_item_id": item_id, "actual_qty": 4}]},
            ).json()["id"]
            self.assertEqual(client.post(f"/api/stock-take/{loss_id}/confirm").status_code, 200)
            balance = client.get("/api/stock-balance", params={"item": "TAKE-001"}).json()
            self.assertEqual(balance["items"][0]["qty"], 4)
            with connect(db_path) as conn:
                loss_ledger = conn.execute(
                    """
                    select sum(change_qty) as change_qty
                    from stock_ledger
                    where source_type = 'stock_take' and source_id = ?
                    """,
                    (loss_id,),
                ).fetchone()["change_qty"]
            self.assertEqual(loss_ledger, -3)

    def test_create_rejects_empty_items_unknown_item_and_negative_actual_qty(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            _, client = _client(tmpdir)
            self.assertEqual(
                client.post("/api/stock-take", json={"items": []}).status_code,
                400,
            )
            self.assertEqual(
                client.post(
                    "/api/stock-take",
                    json={"items": [{"stock_item_id": "missing", "actual_qty": 1}]},
                ).status_code,
                404,
            )
            item_id = _seed_stock(client, qty=1)
            self.assertEqual(
                client.post(
                    "/api/stock-take",
                    json={"items": [{"stock_item_id": item_id, "actual_qty": -1}]},
                ).status_code,
                400,
            )


if __name__ == "__main__":
    unittest.main()
