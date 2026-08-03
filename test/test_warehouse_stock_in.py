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


def _seed_item_and_supplier(client):
    category_id = client.post("/api/stock-categories", json={"name": "耗材"}).json()["id"]
    item_id = client.post(
        "/api/stock-items",
        json={"code": "IN-001", "name": "种植愈合帽", "category_id": category_id, "unit": "个"},
    ).json()["id"]
    supplier_id = client.post(
        "/api/suppliers", json={"name": "示例耗材商", "contact": "李经理"}
    ).json()["id"]
    return item_id, supplier_id


class WarehouseStockInTest(unittest.TestCase):
    def test_create_draft_and_confirm_increases_balance_and_writes_ledger(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path, client = _client(tmpdir)
            item_id, supplier_id = _seed_item_and_supplier(client)

            created = client.post(
                "/api/stock-in",
                json={
                    "type": "进货",
                    "supplier_id": supplier_id,
                    "operator": "库管",
                    "handler": "李经理",
                    "items": [
                        {
                            "stock_item_id": item_id,
                            "qty": 5,
                            "unit_price": 12.5,
                            "batch_no": "B202606",
                            "expire_date": "2027-06-01",
                        }
                    ],
                },
            )
            self.assertEqual(created.status_code, 200)
            stock_in_id = created.json()["id"]
            self.assertEqual(created.json()["status"], "draft")
            self.assertEqual(created.json()["item_count"], 1)

            listed = client.get("/api/stock-in").json()
            self.assertEqual(listed["totalcount"], 1)
            self.assertEqual(listed["stock_in"][0]["status"], "draft")

            confirmed = client.post(f"/api/stock-in/{stock_in_id}/confirm")
            self.assertEqual(confirmed.status_code, 200)
            self.assertEqual(confirmed.json()["status"], "confirmed")

            with connect(db_path) as conn:
                balance = conn.execute(
                    """
                    select qty
                    from stock_balance
                    where stock_item_id = ? and batch_no = ? and expire_date = ?
                    """,
                    (item_id, "B202606", "2027-06-01"),
                ).fetchone()
                ledger = conn.execute(
                    """
                    select change_qty, source_type, source_id
                    from stock_ledger
                    where stock_item_id = ?
                    """,
                    (item_id,),
                ).fetchone()
            self.assertEqual(balance["qty"], 5)
            self.assertEqual(ledger["change_qty"], 5)
            self.assertEqual(ledger["source_type"], "stock_in")
            self.assertEqual(ledger["source_id"], stock_in_id)

            again = client.post(f"/api/stock-in/{stock_in_id}/confirm")
            self.assertEqual(again.status_code, 409)

    def test_create_rejects_bad_type_empty_items_and_unknown_item(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            _, client = _client(tmpdir)
            _, supplier_id = _seed_item_and_supplier(client)

            bad_type = client.post(
                "/api/stock-in", json={"type": "调拨", "items": [{"stock_item_id": "x", "qty": 1}]}
            )
            self.assertEqual(bad_type.status_code, 400)

            empty_items = client.post(
                "/api/stock-in", json={"type": "purchase", "supplier_id": supplier_id, "items": []}
            )
            self.assertEqual(empty_items.status_code, 400)

            unknown_item = client.post(
                "/api/stock-in",
                json={
                    "type": "purchase",
                    "supplier_id": supplier_id,
                    "items": [{"stock_item_id": "missing", "qty": 1}],
                },
            )
            self.assertEqual(unknown_item.status_code, 404)

    def test_create_rejects_nan_inf_negative_zero_qty(self):
        # 动态扫#12/#21：数量 nan/inf/负/零 必须 400(不能绕过 require_positive_qty 污染库存)
        with tempfile.TemporaryDirectory() as tmpdir:
            _, client = _client(tmpdir)
            item_id, supplier_id = _seed_item_and_supplier(client)
            for bad in ["nan", "inf", "-5", "0", -3, 0]:
                resp = client.post(
                    "/api/stock-in",
                    json={"type": "purchase", "supplier_id": supplier_id,
                          "items": [{"stock_item_id": item_id, "qty": bad}]},
                )
                self.assertEqual(resp.status_code, 400, f"qty={bad!r} 应被拒")


if __name__ == "__main__":
    unittest.main()
