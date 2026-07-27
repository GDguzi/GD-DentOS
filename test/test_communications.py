"""沟通记录（客户沟通→沟通咨询）：电话/微信记录 CRUD + 微信截图上传/回放/删除 + 录音弱关联。"""
import io
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from local_app.api import create_app
from local_app.db import connect, init_db

_PNG = (b"\x89PNG\r\n\x1a\n" + b"\x00" * 64)   # 够当一张"截图"字节
_WAV = b"RIFF\x24\x00\x00\x00WAVEfmt " + b"\x00" * 28


class CommunicationTest(unittest.TestCase):
    def _client(self, tmp):
        db = Path(tmp) / "clinic.sqlite3"
        init_db(db)
        with connect(db) as conn:
            conn.execute("insert into patients(patient_identity, display_name, updated_at, current_hash) "
                         "values ('p1','测试患者','x','h1')")
            conn.commit()
        return db, TestClient(create_app(db, images_dir=Path(tmp) / "img"))

    def _create(self, client, **fields):
        payload = {"channel": "wechat", "direction": "",
                   "content": "问种植牙价格", "deal_status": "意向"}
        payload.update(fields)
        if payload["channel"] == "phone" and not payload["direction"]:
            payload["direction"] = "outbound"
        return client.post("/api/patients/p1/communications", json=payload)

    def _upload_image(self, client, communication_id, revision=1, **file):
        upload = file or {
            "file": ("chat.png", io.BytesIO(_PNG), "image/png")
        }
        return client.post(
            f"/api/communications/{communication_id}/images",
            files=upload,
            data={"expected_revision": str(revision)},
        )

    def test_create_then_list(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, client = self._client(tmp)
            r = self._create(client, channel="phone", content="电话回访约复诊")
            self.assertEqual(r.status_code, 200)
            self.assertTrue(r.json()["communication_id"].startswith("local-comm-"))
            listed = client.get("/api/patients/p1/communications").json()
            self.assertEqual(listed["totalcount"], 1)
            c = listed["communications"][0]
            self.assertEqual(c["channel"], "phone")
            self.assertEqual(c["content"], "电话回访约复诊")
            self.assertEqual(c["operator"], "local_user")
            self.assertEqual(c["images"], [])

    def test_bad_channel_400(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, client = self._client(tmp)
            self.assertEqual(self._create(client, channel="sms").status_code, 400)

    def test_unknown_patient_404(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, client = self._client(tmp)
            r = client.post("/api/patients/nope/communications",
                            json={"channel": "phone", "content": "x"})
            self.assertEqual(r.status_code, 404)

    def test_patch_and_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, client = self._client(tmp)
            cid = self._create(client).json()["communication_id"]
            self.assertEqual(client.patch(f"/api/communications/{cid}",
                                          json={"expected_revision": 1,
                                                "deal_status": "已约"}).status_code, 200)
            self.assertEqual(client.patch(f"/api/communications/{cid}", json={}).status_code, 400)
            c = client.get("/api/patients/p1/communications").json()["communications"][0]
            self.assertEqual(c["deal_status"], "已约")

    def test_wechat_screenshot_upload_view_delete(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, client = self._client(tmp)
            cid = self._create(client).json()["communication_id"]
            up = self._upload_image(client, cid)
            self.assertEqual(up.status_code, 200)
            img_id = up.json()["image_id"]
            c = client.get("/api/patients/p1/communications").json()["communications"][0]
            self.assertEqual(len(c["images"]), 1)
            g = client.get(f"/api/communications/{cid}/images/{img_id}/file")
            self.assertEqual(g.status_code, 200)
            self.assertEqual(g.content, _PNG)
            d = client.request(
                "DELETE", f"/api/communications/{cid}/images/{img_id}",
                json={"expected_revision": 2},
            )
            self.assertEqual(d.status_code, 200)
            self.assertIs(d.json()["file_removed"], True)
            self.assertEqual(client.get(f"/api/communications/{cid}/images/{img_id}/file").status_code, 404)

    def test_image_bad_ext_and_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, client = self._client(tmp)
            cid = self._create(client).json()["communication_id"]
            self.assertEqual(self._upload_image(
                client, cid, file=("x.txt", io.BytesIO(b"hi"), "text/plain")
            ).status_code, 400)
            self.assertEqual(self._upload_image(
                client, cid, file=("x.png", io.BytesIO(b""), "image/png")
            ).status_code, 400)
            self.assertEqual(self._upload_image(
                client, cid, file=("x.png", io.BytesIO(b"not image"), "image/png")
            ).status_code, 400)

    def test_image_unknown_comm_404(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, client = self._client(tmp)
            self.assertEqual(self._upload_image(client, "nope").status_code, 404)

    def test_delete_communication_requires_images_deleted_through_saga(self):
        with tempfile.TemporaryDirectory() as tmp:
            db, client = self._client(tmp)
            cid = self._create(client).json()["communication_id"]
            image_id = self._upload_image(client, cid).json()["image_id"]
            r = client.request(
                "DELETE", f"/api/communications/{cid}",
                json={"expected_revision": 2},
            )
            self.assertEqual(r.status_code, 409)
            self.assertEqual(r.json(), {"detail": "communication_has_images"})
            removed = client.request(
                "DELETE", f"/api/communications/{cid}/images/{image_id}",
                json={"expected_revision": 2},
            )
            self.assertEqual(removed.status_code, 200)
            r = client.request(
                "DELETE", f"/api/communications/{cid}",
                json={"expected_revision": 3},
            )
            self.assertEqual(r.status_code, 200)
            self.assertEqual(client.get("/api/patients/p1/communications").json()["totalcount"], 0)
            with connect(db) as conn:
                n = conn.execute("select count(*) c from communication_images").fetchone()["c"]
            self.assertEqual(n, 0)
            self.assertEqual(list((Path(tmp) / "img" / "communication-images").rglob("*.png")), [])

    def test_image_upload_db_failure_no_orphan(self):
        # 口径:入库失败(drop 表模拟)时,已落盘截图必须清掉不留孤儿
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "clinic.sqlite3"
            init_db(db)
            with connect(db) as conn:
                conn.execute("insert into patients(patient_identity, display_name, updated_at, current_hash) "
                             "values ('p1','测试患者','x','h1')")
                conn.commit()
            img = Path(tmp) / "img"
            client = TestClient(create_app(db, images_dir=img))
            cid = self._create(client).json()["communication_id"]
            with connect(db) as conn:
                conn.execute("drop table communication_images")
                conn.commit()
            client2 = TestClient(create_app(db, images_dir=img), raise_server_exceptions=False)
            r = self._upload_image(client2, cid)
            self.assertEqual(r.status_code, 500)
            with connect(db) as conn:
                self.assertEqual(conn.execute("select count(*) from attachment_file_ops").fetchone()[0], 1)
            init_db(db)
            from local_app.attachment_file_ops import recover_attachment_file_ops
            recover_attachment_file_ops(db, img)
            self.assertEqual(list((img / "communication-images").rglob("*.png")), [])

    def test_delete_image_tombstone_move_failure_keeps_business_and_log(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as tmp:
            db, client = self._client(tmp)
            cid = self._create(client).json()["communication_id"]
            img_id = self._upload_image(client, cid).json()["image_id"]
            with patch("pathlib.Path.unlink", side_effect=OSError("boom")):
                r = client.request(
                    "DELETE", f"/api/communications/{cid}/images/{img_id}",
                    json={"expected_revision": 2},
                )
            self.assertEqual(r.status_code, 500)
            with connect(db) as conn:
                self.assertEqual(conn.execute("select status from attachment_file_ops").fetchone()[0], "staging_delete")
                self.assertEqual(conn.execute("select count(*) from communication_images").fetchone()[0], 1)
            from local_app.attachment_file_ops import recover_attachment_file_ops
            recover_attachment_file_ops(db, Path(tmp) / "img")
            with connect(db) as conn:
                self.assertEqual(conn.execute("select count(*) from attachment_file_ops").fetchone()[0], 0)
                self.assertEqual(conn.execute("select count(*) from communication_images").fetchone()[0], 1)

    def test_parent_delete_does_not_bypass_image_saga(self):
        with tempfile.TemporaryDirectory() as tmp:
            db, client = self._client(tmp)
            cid = self._create(client).json()["communication_id"]
            self._upload_image(client, cid)
            r = client.request(
                "DELETE", f"/api/communications/{cid}",
                json={"expected_revision": 2},
            )
            self.assertEqual(r.status_code, 409)
            with connect(db) as conn:
                self.assertEqual(conn.execute("select count(*) from communications where communication_id=?", (cid,)).fetchone()[0], 1)
                self.assertEqual(conn.execute("select count(*) from communication_images where communication_id=?", (cid,)).fetchone()[0], 1)

    def test_call_recording_links_to_communication(self):
        # 电话咨询录音:calls 上传带 linked_communication_id,列表能读回(前端按此归组)
        with tempfile.TemporaryDirectory() as tmp:
            _, client = self._client(tmp)
            cid = self._create(client, channel="phone").json()["communication_id"]
            up = client.post("/api/patients/p1/calls",
                             files={"file": ("rec.wav", io.BytesIO(_WAV), "audio/wav")},
                             data={"direction": "outbound", "linked_communication_id": cid,
                                   "expected_revision": "1"})
            self.assertEqual(up.status_code, 200)
            call = client.get("/api/patients/p1/calls").json()["calls"][0]
            self.assertEqual(call["linked_communication_id"], cid)

    def test_call_link_cross_patient_rejected(self):
        # 录音弱关联必须属于同一患者:传别的患者的沟通/关联 ID 一律拒绝,且不留孤儿录音
        with tempfile.TemporaryDirectory() as tmp:
            db, client = self._client(tmp)
            with connect(db) as conn:
                conn.execute("insert into patients(patient_identity, display_name, updated_at, current_hash) "
                             "values ('p2','李四','x','h2')")
                conn.commit()
            p2_comm = client.post("/api/patients/p2/communications",
                                  json={"channel": "phone", "direction": "outbound",
                                        "content": "p2"}).json()["communication_id"]
            # 把 p2 的沟通 ID 挂到 p1 的录音 → 400
            r = client.post("/api/patients/p1/calls",
                            files={"file": ("rec.wav", io.BytesIO(_WAV), "audio/wav")},
                            data={"direction": "outbound", "linked_communication_id": p2_comm,
                                  "expected_revision": "1"})
            self.assertEqual(r.status_code, 400)
            # 不存在的关联 → 404
            r2 = client.post("/api/patients/p1/calls",
                             files={"file": ("rec.wav", io.BytesIO(_WAV), "audio/wav")},
                             data={"direction": "outbound", "linked_communication_id": "nope",
                                   "expected_revision": "1"})
            self.assertEqual(r2.status_code, 404)
            self.assertEqual(client.get("/api/patients/p1/calls").json()["totalcount"], 0)  # 都被拒,无孤儿行
            self.assertEqual(list((Path(tmp) / "img" / "call-recordings").rglob("*.wav")), [])

    def test_call_link_wechat_communication_rejected(self):
        # 录音只能关联电话沟通(channel=phone),微信沟通不给挂录音
        with tempfile.TemporaryDirectory() as tmp:
            _, client = self._client(tmp)
            wechat = self._create(client, channel="wechat").json()["communication_id"]
            r = client.post("/api/patients/p1/calls",
                            files={"file": ("rec.wav", io.BytesIO(_WAV), "audio/wav")},
                            data={"direction": "outbound", "linked_communication_id": wechat,
                                  "expected_revision": "1"})
            self.assertEqual(r.status_code, 400)


if __name__ == "__main__":
    unittest.main()
