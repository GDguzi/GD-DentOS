"""桌面一键启动器合同测试（Windows 一键 exe 的同一入口）。

合同：
1. 数据目录：PyInstaller 冻结时 = exe 同级 data/（便携，随包备份）；
   开发态 = local_app/data/。
2. 端口：DENTAL_PORT 显式指定就用它（非法值响亮报错）；
   未指定时 8765 空闲用 8765，被占则让系统分一个空闲端口并在控制台说清。
3. 浏览器：服务就绪（健康检查 200）才自动打开，打不开不给用户添乱。
"""
from __future__ import annotations

import http.server
import os
import socket
import threading
import unittest
from pathlib import Path
from unittest import mock

from local_app import desktop_launcher as dl


class ResolveDataDirTest(unittest.TestCase):
    def test_frozen_uses_exe_sibling_data_dir(self):
        exe = Path("C:/apps/clinic/good-dental-clinic.exe")
        self.assertEqual(
            dl.resolve_data_dir(frozen_exe=exe),
            Path("C:/apps/clinic/data"),
        )

    def test_dev_uses_local_app_data(self):
        resolved = dl.resolve_data_dir(frozen_exe=None)
        self.assertEqual(resolved, Path(dl.__file__).resolve().parent / "data")


class ApplyDesktopDefaultsTest(unittest.TestCase):
    def test_defaults_local_only_and_never_ships_a_fixed_password(self):
        """一键版只默认「只听本机」；绝不出厂固定管理员口令——
        不注入 DENTAL_ADMIN_PASSWORD，交回核心的随机口令路径。"""
        env = {}
        dl.apply_desktop_defaults(env)
        self.assertEqual(env["DENTAL_LOCAL_ONLY"], "1")
        self.assertNotIn("DENTAL_ADMIN_PASSWORD", env)

    def test_explicit_values_are_preserved(self):
        env = {"DENTAL_LOCAL_ONLY": "0", "DENTAL_ADMIN_PASSWORD": "my-own"}
        dl.apply_desktop_defaults(env)
        self.assertEqual(env["DENTAL_LOCAL_ONLY"], "0")
        self.assertEqual(env["DENTAL_ADMIN_PASSWORD"], "my-own")


class PickPortTest(unittest.TestCase):
    def test_env_port_wins(self):
        with mock.patch.dict(os.environ, {"DENTAL_PORT": "8899"}, clear=False):
            self.assertEqual(dl.pick_port(), 8899)

    def test_invalid_env_port_fails_loud(self):
        with mock.patch.dict(os.environ, {"DENTAL_PORT": "not-a-port"}, clear=False):
            with self.assertRaises(ValueError):
                dl.pick_port()

    def test_preferred_port_used_when_free(self):
        free = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            free.bind(("127.0.0.1", 0))
            free_port = free.getsockname()[1]
        finally:
            free.close()
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(dl.pick_port(preferred=free_port), free_port)

    def test_busy_default_falls_back_to_os_assigned_free_port(self):
        blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            blocker.bind(("127.0.0.1", 0))
            blocker.listen(1)
            busy_port = blocker.getsockname()[1]
            with mock.patch.dict(os.environ, {}, clear=True):
                got = dl.pick_port(preferred=busy_port)
            self.assertNotEqual(got, busy_port)
            # 分到手的端口必须真的空闲可绑
            probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                probe.bind(("127.0.0.1", got))
            finally:
                probe.close()
        finally:
            blocker.close()


class OpenBrowserWhenReadyTest(unittest.TestCase):
    def test_opens_only_after_health_ok(self):
        class Quiet(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.end_headers()

            def log_message(self, *args):
                pass

        server = http.server.HTTPServer(("127.0.0.1", 0), Quiet)
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with mock.patch.object(dl.webbrowser, "open") as opened:
                ok = dl.open_browser_when_ready(f"http://127.0.0.1:{port}/", timeout=5)
            self.assertTrue(ok)
            opened.assert_called_once_with(f"http://127.0.0.1:{port}/")
        finally:
            server.shutdown()
            server.server_close()

    def test_unreachable_returns_false_without_opening(self):
        with mock.patch.object(dl.webbrowser, "open") as opened:
            ok = dl.open_browser_when_ready("http://127.0.0.1:1/", timeout=0.3, interval=0.1)
        self.assertFalse(ok)
        opened.assert_not_called()


if __name__ == "__main__":
    unittest.main()
