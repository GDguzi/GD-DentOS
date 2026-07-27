"""拍照建档 OCR 接口：GET /api/ocr/status、POST /api/ocr/patient-card。"""
import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

from local_app import auth
from local_app.api import create_app
from local_app.db import connect, init_db
from local_app.ocr import TextBox


def _admin_client(db):
    with connect(db) as conn:
        if not conn.execute("select 1 from users where username='admin'").fetchone():
            auth.create_user(conn, "admin", "管理员", "admin123", role="admin")
            conn.commit()
    c = TestClient(create_app(db))
    c.post("/api/auth/login", json={"username": "admin", "password": "admin123"})
    return c


class TestOcrStatus(unittest.TestCase):
    def _client(self, tmp):
        db = Path(tmp) / "clinic.sqlite3"
        init_db(db)
        return _admin_client(db)

    def test_status_reports_engine(self):
        with tempfile.TemporaryDirectory() as tmp:
            c = self._client(tmp)
            r = c.get("/api/ocr/status")
            self.assertEqual(r.status_code, 200)
            body = r.json()
            self.assertIsInstance(body["available"], bool)
            if body["available"]:
                self.assertEqual(body["engine"], "macos-vision")

    def test_status_false_when_engine_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            c = self._client(tmp)
            with mock.patch("local_app.ocr.available", return_value=False), \
                 mock.patch("local_app.ocr.engine_name", return_value=None):
                r = c.get("/api/ocr/status")
            self.assertEqual(r.status_code, 200)
            self.assertFalse(r.json()["available"])
            self.assertIsNone(r.json()["engine"])


_JPEG_HEADER = b"\xff\xd8\xff" + b"\x00" * 13   # 合法 jpeg 文件头，够骗过嗅探


class TestOcrRecognize(unittest.TestCase):
    def _client(self, tmp):
        db = Path(tmp) / "clinic.sqlite3"
        init_db(db)
        return _admin_client(db)

    def _post(self, c, content=_JPEG_HEADER, headers=None):
        return c.post(
            "/api/ocr/patient-card",
            content=content,                                  # raw body，不是 multipart
            headers=headers if headers is not None else {"Content-Type": "image/jpeg"},
        )

    def test_503_when_engine_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            c = self._client(tmp)
            with mock.patch("local_app.ocr.available", return_value=False):
                r = self._post(c)
            self.assertEqual(r.status_code, 503)

    def test_runtime_engine_failure_returns_503_not_500(self):
        """引擎运行时挂了(熔断时抛 OcrUnavailable)，前台该收到明确的 503，不是甩 500。"""
        from local_app import ocr
        with tempfile.TemporaryDirectory() as tmp:
            c = self._client(tmp)
            with mock.patch("local_app.ocr.available", return_value=True), \
                 mock.patch("local_app.ocr.recognize",
                            side_effect=ocr.OcrUnavailable("识别引擎调用失败，请改用手工录入")):
                r = self._post(c)
            self.assertEqual(r.status_code, 503)
            self.assertIn("手工", r.json()["detail"])

    def test_rejects_empty_body(self):
        with tempfile.TemporaryDirectory() as tmp:
            c = self._client(tmp)
            with mock.patch("local_app.ocr.available", return_value=True):
                r = self._post(c, content=b"")
            self.assertEqual(r.status_code, 400)

    def test_rejects_non_image_content(self):
        with tempfile.TemporaryDirectory() as tmp:
            c = self._client(tmp)
            with mock.patch("local_app.ocr.available", return_value=True):
                r = self._post(c, content=b"not an image at all")
            self.assertEqual(r.status_code, 400)

    def test_rejects_oversize(self):
        with tempfile.TemporaryDirectory() as tmp:
            c = self._client(tmp)
            oversize = _JPEG_HEADER + b"x" * (8 * 1024 * 1024 + 1)
            with mock.patch("local_app.ocr.available", return_value=True):
                r = self._post(c, content=oversize)
            self.assertEqual(r.status_code, 400)

    def test_rejects_oversize_by_declared_content_length(self):
        """声明了超大 Content-Length 就该直接 400，不用真的发 8MB 数据。"""
        with tempfile.TemporaryDirectory() as tmp:
            c = self._client(tmp)
            with mock.patch("local_app.ocr.available", return_value=True):
                r = self._post(c, content=_JPEG_HEADER,
                                headers={"Content-Type": "image/jpeg",
                                         "Content-Length": str(9 * 1024 * 1024)})
            self.assertEqual(r.status_code, 400)

    def test_returns_parsed_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            c = self._client(tmp)
            boxes = [
                TextBox("姓名陈明远", 0.9, 0.09, 0.78, 0.3, 0.05),
                TextBox("公民身份号码", 0.9, 0.09, 0.15, 0.3, 0.05),
                TextBox("990101199003070015", 0.9, 0.35, 0.12, 0.3, 0.05),
            ]
            with mock.patch("local_app.ocr.available", return_value=True), \
                 mock.patch("local_app.ocr.recognize", return_value=boxes):
                r = self._post(c)
            self.assertEqual(r.status_code, 200)
            body = r.json()
            self.assertEqual(body["fields"]["id_card"], "990101199003070015")
            self.assertEqual(body["fields"]["birthday"], "1990-03-07")
            self.assertEqual(body["kind"], "id_card")
            self.assertIn("lines", body)

    def test_field_keys_match_patient_create_contract(self):
        from local_app.routes.patient_create import _FIELDS
        for key in ("display_name", "phone", "sex", "birthday", "address", "id_card"):
            self.assertIn(key, _FIELDS)

    def test_request_admission_bounds_body_readers(self):
        """未获准的请求必须在读取 body 前等待，压缩图片持有量不能随请求数线性增长。"""
        from local_app.routes import ocr as ocr_routes

        async def scenario():
            router = ocr_routes.create_ocr_router()
            endpoint = next(
                route.endpoint
                for route in router.routes
                if route.path == "/api/ocr/patient-card"
            )
            entered = 0
            release_reads = asyncio.Event()

            async def blocked_read(_request, _limit):
                nonlocal entered
                entered += 1
                await release_reads.wait()
                return _JPEG_HEADER

            with mock.patch.object(ocr_routes, "_read_limited", side_effect=blocked_read), \
                 mock.patch.object(ocr_routes, "require_perm"), \
                 mock.patch.object(ocr_routes.ocr, "available", return_value=True), \
                 mock.patch.object(ocr_routes.ocr, "recognize", return_value=[]), \
                 mock.patch.object(ocr_routes.ocr, "parse_patient_card", return_value={}):
                tasks = [asyncio.create_task(endpoint(object())) for _ in range(12)]
                for _ in range(12):
                    await asyncio.sleep(0)
                entered_before_release = entered
                release_reads.set()
                await asyncio.gather(*tasks)
            return entered_before_release

        self.assertEqual(asyncio.run(scenario()), 4)


if __name__ == "__main__":
    unittest.main()
