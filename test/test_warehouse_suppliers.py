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


class WarehouseSupplierTest(unittest.TestCase):
    def test_supplier_crud_soft_deletes(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path, client = _client(tmpdir)
            created = client.post(
                "/api/suppliers",
                json={
                    "name": "示例耗材商",
                    "contact": "李经理",
                    "phone": "13800000000",
                    "note": "月结",
                },
            )
            self.assertEqual(created.status_code, 200)
            supplier_id = created.json()["id"]

            listed = client.get("/api/suppliers", params={"name": "耗材"}).json()
            self.assertEqual(listed["totalcount"], 1)
            self.assertEqual(listed["suppliers"][0]["contact"], "李经理")

            updated = client.put(
                f"/api/suppliers/{supplier_id}",
                json={"contact": "王经理", "phone": "13900000000"},
            )
            self.assertEqual(updated.status_code, 200)
            self.assertEqual(updated.json()["contact"], "王经理")

            deleted = client.delete(f"/api/suppliers/{supplier_id}")
            self.assertEqual(deleted.status_code, 200)
            self.assertEqual(client.get("/api/suppliers").json()["totalcount"], 0)
            with connect(db_path) as conn:
                row = conn.execute(
                    "select is_deleted from supplier where id = ?", (supplier_id,)
                ).fetchone()
            self.assertEqual(row["is_deleted"], 1)

    def test_supplier_rejects_empty_name_and_unknown_id(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            _, client = _client(tmpdir)
            empty = client.post("/api/suppliers", json={"name": " "})
            self.assertEqual(empty.status_code, 400)
            self.assertEqual(
                client.put("/api/suppliers/nope", json={"name": "x"}).status_code,
                404,
            )
            self.assertEqual(client.delete("/api/suppliers/nope").status_code, 404)


if __name__ == "__main__":
    unittest.main()
