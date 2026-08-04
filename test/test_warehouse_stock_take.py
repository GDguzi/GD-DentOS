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

    # ---- GD-08 补充守卫 ----

    def test_server_ignores_forged_book_qty_and_diff(self):
        # 账面数与差异以后端保存时重算为准,浏览器伪造的 book_qty/diff 不得入库
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path, client = _client(tmpdir)
            item_id = _seed_stock(client, qty=5)
            resp = client.post("/api/stock-take", json={
                "operator": "库管",
                "items": [{"stock_item_id": item_id, "actual_qty": 5,
                           "book_qty": 999, "diff": 999}],
            })
            self.assertEqual(resp.status_code, 200)
            row = resp.json()["items"][0]
            self.assertEqual(row["book_qty"], 5)
            self.assertEqual(row["diff"], 0)
            with connect(db_path) as conn:
                db_row = conn.execute(
                    "select book_qty, diff from stock_take_item where stock_take_id = ?",
                    (resp.json()["id"],),
                ).fetchone()
            self.assertEqual(db_row["book_qty"], 5)
            self.assertEqual(db_row["diff"], 0)

    def test_failed_loss_confirm_leaves_no_partial_change(self):
        # 盘亏库存不足确认失败:同单其他行的盘盈也必须回滚,不得留部分库存变更
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path, client = _client(tmpdir)
            item_a = _seed_stock(client, qty=5)
            category_id = client.get("/api/stock-items").json()["items"][0]["category_id"]
            item_b = client.post(
                "/api/stock-items",
                json={"code": "TAKE-002", "name": "膜", "category_id": category_id, "unit": "张"},
            ).json()["id"]
            supplier_id = client.post("/api/suppliers", json={"name": "供二"}).json()["id"]
            si = client.post("/api/stock-in", json={
                "type": "purchase", "supplier_id": supplier_id,
                "items": [{"stock_item_id": item_b, "qty": 1, "batch_no": "B2"}],
            }).json()["id"]
            client.post(f"/api/stock-in/{si}/confirm")

            # 草稿:A 盘盈+2,B 盘亏-1(此刻账面1,可行)
            take_id = client.post("/api/stock-take", json={
                "operator": "库管",
                "items": [
                    {"stock_item_id": item_a, "actual_qty": 7},
                    {"stock_item_id": item_b, "actual_qty": 0},
                ],
            }).json()["id"]
            # 先用另一张盘点单把 B 清零 → 上面草稿的 -1 变成库存不足
            other = client.post("/api/stock-take", json={
                "operator": "库管",
                "items": [{"stock_item_id": item_b, "actual_qty": 0}],
            }).json()["id"]
            self.assertEqual(client.post(f"/api/stock-take/{other}/confirm").status_code, 200)

            resp = client.post(f"/api/stock-take/{take_id}/confirm")
            self.assertEqual(resp.status_code, 409)
            balance = client.get("/api/stock-balance", params={"item": "TAKE-001"}).json()
            self.assertEqual(balance["items"][0]["qty"], 5, "A 的盘盈必须随失败回滚")
            with connect(db_path) as conn:
                status = conn.execute(
                    "select status from stock_take where id = ?", (take_id,)
                ).fetchone()["status"]
            self.assertEqual(status, "draft")


if __name__ == "__main__":
    unittest.main()
