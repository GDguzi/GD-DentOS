import io
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from local_app.api import create_app
from local_app.db import connect, init_db

# 1x1 PNG（最小合法 PNG），用于配图上传测试
_PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01"
    b"\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
)


def _client(tmp):
    db = Path(tmp) / "clinic.sqlite3"
    init_db(db)
    with connect(db) as conn:
        conn.execute(
            "insert into patients(patient_identity, display_name, updated_at, current_hash) "
            "values ('p1', '测试患者', '2026-06-07 00:00:00', 'h1')"
        )
        conn.commit()
    return db, TestClient(create_app(db, images_dir=Path(tmp) / "images"))


class LabOrderTest(unittest.TestCase):
    def test_create_list_return_flow(self):
        with tempfile.TemporaryDirectory() as tmp:
            db, client = _client(tmp)
            resp = client.post("/api/patients/p1/lab-orders", json={
                "project_type": "全瓷冠", "tooth_codes": "16,17", "color": "A2",
                "lab_name": "现代义齿", "sent_date": "2026-06-10",
            })
            self.assertEqual(resp.status_code, 200)
            oid = resp.json()["order_id"]
            self.assertEqual(resp.json()["status"], "sent")

            lst = client.get("/api/patients/p1/lab-orders").json()
            self.assertEqual(lst["totalcount"], 1)
            self.assertEqual(lst["orders"][0]["project_type"], "全瓷冠")

            ret = client.put(f"/api/lab-orders/{oid}/return", json={"return_date": "2026-06-15"})
            self.assertEqual(ret.status_code, 200)
            self.assertEqual(ret.json()["status"], "returned")
            again = client.get("/api/patients/p1/lab-orders").json()
            self.assertEqual(again["orders"][0]["status"], "returned")

    def test_project_required(self):
        with tempfile.TemporaryDirectory() as tmp:
            _db, client = _client(tmp)
            self.assertEqual(
                client.post("/api/patients/p1/lab-orders", json={"project_type": ""}).status_code, 400)

    def test_bad_date_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            _db, client = _client(tmp)
            self.assertEqual(client.post("/api/patients/p1/lab-orders", json={
                "project_type": "冠", "sent_date": "not-a-date"}).status_code, 400)

    def test_image_upload_and_serve(self):
        with tempfile.TemporaryDirectory() as tmp:
            _db, client = _client(tmp)
            oid = client.post("/api/patients/p1/lab-orders",
                              json={"project_type": "冠"}).json()["order_id"]
            up = client.post(f"/api/lab-orders/{oid}/images",
                             files={"file": ("x.png", io.BytesIO(_PNG), "image/png")})
            self.assertEqual(up.status_code, 200)
            img_id = up.json()["img_id"]
            got = client.get(f"/api/lab-orders/images/{img_id}/file")
            self.assertEqual(got.status_code, 200)
            # 列表应带出配图
            order = client.get("/api/patients/p1/lab-orders").json()["orders"][0]
            self.assertEqual(len(order["images"]), 1)
            # 删除配图
            self.assertEqual(client.delete(f"/api/lab-orders/images/{img_id}").status_code, 200)
            self.assertEqual(client.get(f"/api/lab-orders/images/{img_id}/file").status_code, 404)

    def test_non_image_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            _db, client = _client(tmp)
            oid = client.post("/api/patients/p1/lab-orders",
                              json={"project_type": "冠"}).json()["order_id"]
            up = client.post(f"/api/lab-orders/{oid}/images",
                             files={"file": ("x.txt", io.BytesIO(b"hello"), "text/plain")})
            self.assertEqual(up.status_code, 400)

    def test_lab_config_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            _db, client = _client(tmp)
            self.assertEqual(client.get("/api/lab-config/factories").json()["list"], [])
            put = client.put("/api/lab-config/factories", json={"list": ["现代义齿", "美佳", ""]})
            self.assertEqual(put.json()["list"], ["现代义齿", "美佳"])
            self.assertEqual(client.get("/api/lab-config/factories").json()["list"], ["现代义齿", "美佳"])
            self.assertEqual(client.get("/api/lab-config/unknown").status_code, 404)

    def test_delete_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            db, client = _client(tmp)
            oid = client.post("/api/patients/p1/lab-orders",
                              json={"project_type": "冠"}).json()["order_id"]
            self.assertEqual(client.delete(f"/api/patients/p1/lab-orders/{oid}").status_code, 200)
            self.assertEqual(client.get("/api/patients/p1/lab-orders").json()["totalcount"], 0)


if __name__ == "__main__":
    unittest.main()
