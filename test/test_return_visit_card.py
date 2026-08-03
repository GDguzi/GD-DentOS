"""客户通回访卡片：列表倒序(最近在前) + 回访详情 + 微信回访截图上传/回放/删除(#712 防孤儿口径)。"""
import io
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from local_app.api import create_app
from local_app.db import connect, init_db

_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


class ReturnVisitCardTest(unittest.TestCase):
    def _client(self, tmp):
        db = Path(tmp) / "clinic.sqlite3"
        init_db(db)
        with connect(db) as conn:
            conn.execute("insert into patients(patient_identity, display_name, updated_at, current_hash) "
                         "values ('p1','张三','x','h1')")
            # 两条回访:2019 老 + 2026 新,验证倒序
            conn.execute("insert into return_visits(return_visit_id, patient_identity, due_time, item_name, status) "
                         "values ('rv-old','p1','2019-03-15 09:00','洁牙老回访','待回访')")
            conn.execute("insert into return_visits(return_visit_id, patient_identity, due_time, item_name, status) "
                         "values ('rv-new','p1','2026-07-10 09:00','复诊新回访','待回访')")
            conn.commit()
        return db, TestClient(create_app(db, images_dir=Path(tmp) / "img"))

    def _upload_image(self, client, return_visit_id="rv-new", revision=1, **file):
        upload = file or {
            "file": ("chat.png", io.BytesIO(_PNG), "image/png")
        }
        return client.post(
            f"/api/return-visits/{return_visit_id}/images",
            files=upload,
            data={"expected_revision": str(revision)},
        )

    def test_list_sorted_recent_first(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, client = self._client(tmp)
            rows = client.get("/api/return-visits?status=all").json()["list"]
            self.assertEqual(rows[0]["return_visit_id"], "rv-new", "最近的回访应排最前(倒序)")
            self.assertEqual(rows[-1]["return_visit_id"], "rv-old", "2019 老回访应沉到最后")

    def test_detail_returns_fields_and_images(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, client = self._client(tmp)
            d = client.get("/api/return-visits/rv-new").json()
            self.assertEqual(d["item_name"], "复诊新回访")
            self.assertEqual(d["display_name"], "张三")
            self.assertEqual(d["images"], [])

    def test_detail_unknown_404(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, client = self._client(tmp)
            self.assertEqual(client.get("/api/return-visits/nope").status_code, 404)

    def test_image_upload_view_delete(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, client = self._client(tmp)
            up = self._upload_image(client)
            self.assertEqual(up.status_code, 200)
            img_id = up.json()["image_id"]
            d = client.get("/api/return-visits/rv-new").json()
            self.assertEqual(len(d["images"]), 1)
            g = client.get(f"/api/return-visits/rv-new/images/{img_id}/file")
            self.assertEqual(g.status_code, 200)
            self.assertEqual(g.content, _PNG)
            de = client.request(
                "DELETE",
                f"/api/return-visits/rv-new/images/{img_id}",
                json={"expected_revision": 2},
            )
            self.assertEqual(de.status_code, 200)
            self.assertIs(de.json()["file_removed"], True)
            self.assertEqual(client.get(f"/api/return-visits/rv-new/images/{img_id}/file").status_code, 404)

    def test_image_bad_ext_empty_unknown_rv(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, client = self._client(tmp)
            self.assertEqual(self._upload_image(
                client,
                file=("x.txt", io.BytesIO(b"hi"), "text/plain"),
            ).status_code, 400)
            self.assertEqual(self._upload_image(
                client,
                file=("x.png", io.BytesIO(b""), "image/png"),
            ).status_code, 400)
            self.assertEqual(
                self._upload_image(client, "nope").status_code,
                404,
            )

    def test_soft_deleted_return_visit_images_404(self):
        # 修3:回访软删后,详情/截图文件/删截图 一律 404(不外泄软删回访的微信截图)
        with tempfile.TemporaryDirectory() as tmp:
            _, client = self._client(tmp)
            img_id = self._upload_image(client).json()["image_id"]
            self.assertEqual(client.request(
                "DELETE", "/api/return-visits/rv-new",
                json={"expected_revision": 2},
            ).status_code, 200)   # 软删回访
            self.assertEqual(client.get("/api/return-visits/rv-new").status_code, 404)
            self.assertEqual(client.get(f"/api/return-visits/rv-new/images/{img_id}/file").status_code, 404)
            self.assertEqual(client.request(
                "DELETE",
                f"/api/return-visits/rv-new/images/{img_id}",
                json={"expected_revision": 2},
            ).status_code, 404)

    def test_image_upload_db_failure_no_orphan(self):
        # #712 入库失败(drop 表)时清盘,不留孤儿截图
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "clinic.sqlite3"
            init_db(db)
            with connect(db) as conn:
                conn.execute("insert into patients(patient_identity, display_name, updated_at, current_hash) "
                             "values ('p1','张三','x','h1')")
                conn.execute("insert into return_visits(return_visit_id, patient_identity, due_time, item_name) "
                             "values ('rv1','p1','2026-07-10 09:00','x')")
                conn.execute("drop table return_visit_images")
                conn.commit()
            img = Path(tmp) / "img"
            client = TestClient(create_app(db, images_dir=img), raise_server_exceptions=False)
            r = self._upload_image(client, "rv1")
            self.assertEqual(r.status_code, 500)
            with connect(db) as conn:
                self.assertEqual(
                    conn.execute(
                        "select count(*) from attachment_file_ops"
                    ).fetchone()[0],
                    1,
                )
            init_db(db)
            from local_app.attachment_file_ops import recover_attachment_file_ops
            recover_attachment_file_ops(db, img)
            self.assertEqual(list((img / "return-visit-images").rglob("*.png")), [])

    def test_call_recording_links_to_return_visit(self):
        # 电话回访录音:calls 带 linked_return_visit_id → 列表接口必须回读该字段(前端按它归组录音)
        with tempfile.TemporaryDirectory() as tmp:
            db, client = self._client(tmp)
            _WAV = b"RIFF\x24\x00\x00\x00WAVEfmt " + b"\x00" * 28
            up = client.post("/api/patients/p1/calls",
                             files={"file": ("r.wav", io.BytesIO(_WAV), "audio/wav")},
                             data={"direction": "outbound", "linked_return_visit_id": "rv-new",
                                   "expected_revision": "1"})
            self.assertEqual(up.status_code, 200)
            call = client.get("/api/patients/p1/calls").json()["calls"][0]
            self.assertEqual(call["call_id"], up.json()["call_id"])
            # 关键回归:列表必须回读 linked_return_visit_id,否则回访卡片录音区恒空
            self.assertEqual(call["linked_return_visit_id"], "rv-new")

    def test_call_link_cross_patient_return_visit_rejected(self):
        # #723 电话回访录音弱关联必须同患者:把 p2 的回访挂到 p1 的录音 → 400,且不留孤儿
        with tempfile.TemporaryDirectory() as tmp:
            db, client = self._client(tmp)
            _WAV = b"RIFF\x24\x00\x00\x00WAVEfmt " + b"\x00" * 28
            with connect(db) as conn:
                conn.execute("insert into patients(patient_identity, display_name, updated_at, current_hash) "
                             "values ('p2','李四','x','h2')")
                conn.execute("insert into return_visits(return_visit_id, patient_identity, due_time, item_name) "
                             "values ('rv-p2','p2','2026-07-10 09:00','p2回访')")
                conn.commit()
            r = client.post("/api/patients/p1/calls",
                            files={"file": ("r.wav", io.BytesIO(_WAV), "audio/wav")},
                            data={"direction": "outbound", "linked_return_visit_id": "rv-p2",
                                  "expected_revision": "1"})
            self.assertEqual(r.status_code, 400)
            r2 = client.post("/api/patients/p1/calls",
                             files={"file": ("r.wav", io.BytesIO(_WAV), "audio/wav")},
                             data={"direction": "outbound", "linked_return_visit_id": "nope",
                                   "expected_revision": "1"})
            self.assertEqual(r2.status_code, 404)
            self.assertEqual(client.get("/api/patients/p1/calls").json()["totalcount"], 0)


if __name__ == "__main__":
    unittest.main()
