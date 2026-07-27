import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from local_app.api import create_app


class GlobalErrorHandlerTest(unittest.TestCase):
    """未预期异常应被兜底成干净的 500，不把堆栈/SQL/库路径漏给前端。"""

    def _client_on_broken_db(self, tmpdir):
        # 把 db_path 指向一个「目录」，端点内 sqlite3 连接/查询时必抛
        # OperationalError —— 模拟未预期的运行时异常（磁盘/库损坏等）。
        broken = Path(tmpdir) / "not_a_db_dir"
        broken.mkdir()
        app = create_app(db_path=broken)
        return TestClient(app, raise_server_exceptions=False)

    def test_unexpected_error_returns_clean_500(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            client = self._client_on_broken_db(tmpdir)
            resp = client.get("/api/stats")
        self.assertEqual(resp.status_code, 500)
        self.assertEqual(resp.json(), {"detail": "服务器内部错误，请稍后重试"})

    def test_500_body_leaks_no_stack_or_internals(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            client = self._client_on_broken_db(tmpdir)
            resp = client.get("/api/stats")
        body = resp.text
        for leak in ("Traceback", "sqlite3", "OperationalError", "/local_app/", ".py"):
            self.assertNotIn(leak, body)

    def test_health_still_ok(self):
        # 正常路径不受影响
        with tempfile.TemporaryDirectory() as tmpdir:
            app = create_app(db_path=Path(tmpdir) / "ok.sqlite3")
            client = TestClient(app)
            self.assertEqual(client.get("/api/health").status_code, 200)


if __name__ == "__main__":
    unittest.main()
