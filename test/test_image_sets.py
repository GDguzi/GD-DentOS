"""影像组图标记(#534跟进):组图只认医生手点;种子只预标历史真成套的期(>=6视图位)。"""
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from local_app.api import create_app
from local_app.db import connect, init_db


def _seed_images(db_path, rows):
    with connect(db_path) as conn:
        conn.execute(
            "insert into patients(patient_identity, source_customer_id, display_name, phone, updated_at, current_hash) "
            "values ('p1','c1','测试患者','100','2026-07-03 00:00:00','h1')"
        )
        for i, (day, cat) in enumerate(rows):
            conn.execute(
                "insert into patient_images(image_id, patient_identity, image_date, seq, category, file_name, rel_path) "
                "values (?,?,?,?,?,?,?)",
                (f"img{i}", "p1", day, i, cat, f"{i}.jpg", f"{day}/p1/{i}.jpg"),
            )
        conn.commit()


class ImageSetsTest(unittest.TestCase):
    def _client(self, rows):
        tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(tmpdir.cleanup)
        db_path = Path(tmpdir.name) / "clinic.sqlite3"
        init_db(db_path)
        _seed_images(db_path, rows)
        app = create_app(db_path, images_dir=Path(tmpdir.name) / "images")
        return TestClient(app), db_path

    def test_seed_marks_only_real_sets(self):
        # 6个不同视图位=成套预标;内窥镜式2张咬合像不算;散图更不算
        rows = [("2025-07-14", c) for c in ["正面像", "正面微笑像", "上颌合面像", "下颌合面像", "正面咬合像", "全景片"]]
        rows += [("2026-07-03", "正面咬合像"), ("2026-07-03", "左侧咬合像"), ("2026-07-03", "其他")]
        rows += [("2025-08-12", "SC")]
        client, _ = self._client(rows)
        data = client.get("/api/patients/p1/images").json()
        self.assertEqual(data["set_dates"], ["2025-07-14"])

    def test_mark_and_unmark_by_hand(self):
        rows = [("2026-07-03", "正面咬合像"), ("2026-07-03", "其他")]
        client, _ = self._client(rows)
        # 手点做组图
        r = client.post("/api/patients/p1/image-sets", json={"image_date": "2026-07-03"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(client.get("/api/patients/p1/images").json()["set_dates"], ["2026-07-03"])
        # 幂等
        client.post("/api/patients/p1/image-sets", json={"image_date": "2026-07-03"})
        self.assertEqual(client.get("/api/patients/p1/images").json()["set_dates"], ["2026-07-03"])
        # 取消
        r = client.delete("/api/patients/p1/image-sets/2026-07-03")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(client.get("/api/patients/p1/images").json()["set_dates"], [])

    def test_mark_rejects_bad_input(self):
        rows = [("2026-07-03", "其他")]
        client, _ = self._client(rows)
        self.assertEqual(client.post("/api/patients/p1/image-sets", json={"image_date": "2026-7-3"}).status_code, 400)
        self.assertEqual(client.post("/api/patients/p1/image-sets", json={"image_date": "2026-01-01"}).status_code, 400)  # 该日无影像
        self.assertEqual(client.post("/api/patients/nobody/image-sets", json={"image_date": "2026-07-03"}).status_code, 404)


if __name__ == "__main__":
    unittest.main()
