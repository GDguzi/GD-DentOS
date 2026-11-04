"""拍照建档 OCR 接口：GET /api/ocr/status、POST /api/ocr/patient-card。"""
import asyncio
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from fastapi import HTTPException
from fastapi.testclient import TestClient
from starlette.requests import Request

from local_app import auth
from local_app.api import create_app
from local_app.db import connect, init_db
from local_app.ocr import TextBox


def _admin_client(db):
    with connect(db) as conn:
        if not conn.execute("select 1 from users where username='admin'").fetchone():
            auth.create_user(conn, "admin", "管理员", "admin123", role="admin")
            conn.commit()
    c = TestClient(create_app(db, images_dir=Path(db).parent / "images", require_login=True))
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

    def _assert_body_at_limit(self, chunked, over_limit=False):
        with tempfile.TemporaryDirectory() as tmp:
            c = self._client(tmp)
            size = 25 * 1024 * 1024 + int(over_limit)
            payload = _JPEG_HEADER + b"x" * (size - len(_JPEG_HEADER))
            self.assertEqual(len(payload), size)
            boxes = [TextBox("公民身份号码990101199003070015", 0.9, 0.09, 0.15, 0.6, 0.05)]
            headers = {"Content-Type": "image/jpeg"}
            if chunked:
                content = (payload[i:i + 1024 * 1024] for i in range(0, len(payload), 1024 * 1024))
            else:
                content = payload
                headers["Content-Length"] = str(len(payload))
            with mock.patch("local_app.ocr.available", return_value=True), \
                 mock.patch("local_app.ocr.recognize", return_value=boxes) as recognize:
                r = self._post(c, content=content, headers=headers)
            if over_limit:
                self.assertEqual(r.status_code, 400, r.text)
                self.assertIn("25MB", r.json()["detail"])
                recognize.assert_not_called()
            else:
                self.assertEqual(r.status_code, 200, r.text)
                self.assertEqual(r.json()["fields"]["id_card"], "990101199003070015")
                self.assertEqual(r.json()["fields"]["birthday"], "1990-03-07")
                self.assertEqual(r.json()["kind"], "id_card")
                recognize.assert_called_once_with(payload)
            if chunked:
                self.assertNotIn("content-length", r.request.headers)
                self.assertEqual(r.request.headers["transfer-encoding"], "chunked")
            else:
                self.assertEqual(r.request.headers["content-length"], str(len(payload)))

    def test_exact_25mb_chunked_body_reaches_recognizer_without_content_length(self):
        self._assert_body_at_limit(chunked=True)

    def test_exact_25mb_body_reaches_recognizer_with_content_length(self):
        self._assert_body_at_limit(chunked=False)

    def test_25mb_plus_one_chunked_body_is_rejected(self):
        self._assert_body_at_limit(chunked=True, over_limit=True)

    def test_25mb_plus_one_body_is_rejected_with_content_length(self):
        self._assert_body_at_limit(chunked=False, over_limit=True)

    def test_declared_oversize_is_rejected_before_receiving_body(self):
        from local_app.routes import ocr as ocr_routes

        async def scenario():
            endpoint = next(route.endpoint for route in ocr_routes.create_ocr_router().routes
                            if route.path == "/api/ocr/patient-card")
            receive = mock.AsyncMock(side_effect=AssertionError("body must not be read"))
            request = Request({"type": "http", "headers": [
                (b"content-length", str(25 * 1024 * 1024 + 1).encode("ascii")),
            ]}, receive=receive)
            with mock.patch.object(ocr_routes, "require_perm"), \
                 mock.patch.object(ocr_routes.ocr, "available", return_value=True), \
                 mock.patch.object(ocr_routes.ocr, "recognize") as recognize:
                with self.assertRaises(HTTPException) as caught:
                    await endpoint(request)
                self.assertEqual(caught.exception.status_code, 400)
                self.assertIn("25MB", caught.exception.detail)
                receive.assert_not_awaited()
                recognize.assert_not_called()

        asyncio.run(scenario())

    def test_permission_denial_does_not_recognize_image(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "clinic.sqlite3"
            init_db(db)
            with connect(db) as conn:
                auth.create_user(conn, "test-reader", "Synthetic reader", "test-password", role="nurse")
                conn.commit()
            with TestClient(create_app(db, images_dir=Path(tmp) / "images", require_login=True)) as c:
                login = c.post("/api/auth/login", json={
                    "username": "test-reader", "password": "test-password",
                })
                self.assertEqual(login.status_code, 200, login.text)
                with mock.patch("local_app.ocr.recognize") as recognize:
                    r = self._post(c)
                self.assertEqual(r.status_code, 403, r.text)
                recognize.assert_not_called()

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

    def test_decode_concurrency_stays_at_two(self):
        """真实请求读取与线程池保持运行,仅阻塞合成识别器来观察同时进入的数量。"""
        from local_app.routes import ocr as ocr_routes

        async def scenario():
            endpoint = next(route.endpoint for route in ocr_routes.create_ocr_router().routes
                            if route.path == "/api/ocr/patient-card")
            loop = asyncio.get_running_loop()
            two_entered = asyncio.Event()
            release = threading.Event()
            lock = threading.Lock()
            active = peak = 0

            def recognize(_data):
                nonlocal active, peak
                with lock:
                    active += 1
                    peak = max(peak, active)
                    if active == 2:
                        loop.call_soon_threadsafe(two_entered.set)
                try:
                    if not release.wait(timeout=3):
                        raise AssertionError("synthetic recognizer was not released")
                    return []
                finally:
                    with lock:
                        active -= 1

            async def receive():
                return {"type": "http.request", "body": _JPEG_HEADER, "more_body": False}

            with mock.patch.object(ocr_routes, "require_perm"), \
                 mock.patch.object(ocr_routes.ocr, "available", return_value=True), \
                 mock.patch.object(ocr_routes.ocr, "recognize", side_effect=recognize):
                tasks = [asyncio.create_task(endpoint(Request(
                    {"type": "http", "headers": []}, receive=receive,
                ))) for _ in range(8)]
                try:
                    await asyncio.wait_for(two_entered.wait(), timeout=2)
                    await asyncio.sleep(0.05)
                finally:
                    release.set()
                    await asyncio.gather(*tasks)
            return peak

        self.assertEqual(asyncio.run(scenario()), 2)


if __name__ == "__main__":
    unittest.main()
