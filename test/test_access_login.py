"""Contract tests for the standalone access-v3 login page renderer."""

from __future__ import annotations

import ast
import importlib
import importlib.util
import re
import unittest
from html.parser import HTMLParser
from pathlib import Path


MODULE_NAME = "local_app.access_login"
SOURCE_PATH = Path(__file__).parents[1] / "local_app" / "access_login.py"
REMEMBERED_USERNAME_KEY = "dl.rememberedUsername.v1"


class _LoginPageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.forms: list[dict[str, str | None]] = []
        self.inputs: list[dict[str, str | None]] = []
        self.buttons: list[dict[str, str | None]] = []
        self.scripts: list[str] = []
        self._script_parts: list[str] | None = None

    def handle_starttag(self, tag: str, attrs) -> None:
        attributes = dict(attrs)
        if tag == "form":
            self.forms.append(attributes)
        elif tag == "input":
            self.inputs.append(attributes)
        elif tag == "button":
            self.buttons.append(attributes)
        elif tag == "script":
            self._script_parts = []

    def handle_data(self, data: str) -> None:
        if self._script_parts is not None:
            self._script_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "script" and self._script_parts is not None:
            self.scripts.append("".join(self._script_parts))
            self._script_parts = None


class AccessLoginContractTest(unittest.TestCase):
    def _module(self):
        if importlib.util.find_spec(MODULE_NAME) is None:
            self.skipTest("access_login module is not implemented yet")
        return importlib.import_module(MODULE_NAME)

    def _render(self, clinic_name: str = "合成诊所 <A>") -> str:
        return self._module().render_access_login_html(clinic_name)

    def _parse(self, rendered: str) -> _LoginPageParser:
        parser = _LoginPageParser()
        parser.feed(rendered)
        return parser

    def test_module_exists(self) -> None:
        self.assertIsNotNone(
            importlib.util.find_spec(MODULE_NAME),
            "local_app.access_login must be added",
        )

    def test_clinic_name_requires_nonempty_builtin_utf8_string(self) -> None:
        renderer = self._module().render_access_login_html

        class StringSubclass(str):
            pass

        invalid_values = (None, 1, True, b"clinic", "", " \t\n", StringSubclass("诊所"), "\ud800")
        for value in invalid_values:
            with self.subTest(value=repr(value)):
                with self.assertRaisesRegex(ValueError, "^clinic_name_invalid$"):
                    renderer(value)

    def test_clinic_name_is_html_escaped_without_trimming(self) -> None:
        rendered = self._render(' 合成诊所 <A> & "B" ')
        self.assertIn(" 合成诊所 &lt;A&gt; &amp; &quot;B&quot; ", rendered)
        self.assertNotIn('<A> & "B"', rendered)
        self.assertEqual(rendered, self._render(' 合成诊所 <A> & "B" '))

    def test_page_uses_standard_password_manager_form_semantics(self) -> None:
        parser = self._parse(self._render())
        self.assertIn(
            {"id": "loginForm", "action": "/api/auth/login", "method": "post"},
            parser.forms,
        )

        inputs_by_id = {item.get("id"): item for item in parser.inputs}
        self.assertEqual(
            {
                "id": "username",
                "name": "username",
                "type": "text",
                "autocomplete": "username",
                "required": None,
            },
            {key: inputs_by_id["username"].get(key) for key in ("id", "name", "type", "autocomplete", "required")},
        )
        self.assertEqual(
            {
                "id": "password",
                "name": "password",
                "type": "password",
                "autocomplete": "current-password",
                "required": None,
            },
            {key: inputs_by_id["password"].get(key) for key in ("id", "name", "type", "autocomplete", "required")},
        )
        self.assertTrue(any(button.get("type") == "submit" for button in parser.buttons))

    def test_page_is_clear_chinese_gui_without_long_term_login_promises(self) -> None:
        rendered = self._render()
        for expected in ("诊所管理系统", "账号", "密码", "记住账号", "登录"):
            self.assertIn(expected, rendered)
        for forbidden in (
            "记住此设备",
            "长期免登",
            "保持登录",
            "7 天",
            "7天",
            "90 天",
            "90天",
        ):
            self.assertNotIn(forbidden, rendered)
        self.assertIn('lang="zh-CN"', rendered)
        self.assertIn('aria-live="polite"', rendered)

    def test_script_posts_only_username_and_password_then_redirects(self) -> None:
        parser = self._parse(self._render())
        self.assertEqual(len(parser.scripts), 1)
        script = parser.scripts[0]
        payload_match = re.search(r"const\s+payload\s*=\s*\{(?P<body>.*?)\};", script, re.DOTALL)
        self.assertIsNotNone(payload_match)
        payload_body = payload_match.group("body")
        self.assertEqual(
            {"username", "password"},
            set(re.findall(r"\b([A-Za-z_][A-Za-z0-9_]*)\s*:", payload_body)),
        )
        for forbidden in ("device_name", "device", "remember", "client_time", "timestamp", "time_zone"):
            self.assertNotIn(forbidden, payload_body.lower())
        self.assertIn('fetch("/api/auth/login"', script)
        self.assertRegex(script, r"method\s*:\s*[\"']POST[\"']")
        self.assertIn("JSON.stringify(payload)", script)
        self.assertIn('location.assign("/")', script)

    def test_only_fixed_username_key_is_used_for_browser_storage(self) -> None:
        parser = self._parse(self._render())
        script = parser.scripts[0]
        self.assertNotIn("sessionStorage", script)
        storage_calls = re.findall(
            r"localStorage\.(?:getItem|setItem|removeItem)\(([^,\)]*)",
            script,
        )
        self.assertTrue(storage_calls)
        self.assertTrue(all(REMEMBERED_USERNAME_KEY in argument for argument in storage_calls))
        self.assertRegex(
            script,
            r'typeof\s+rememberedUsername\s*===\s*[\"\']string[\"\']\s*&&\s*rememberedUsername\.trim\(\)\s*!==\s*[\"\'][\"\']',
        )
        self.assertNotRegex(script, r"localStorage\.setItem\([^,]+,\s*password")
        self.assertNotRegex(script, r"localStorage\.setItem\([^,]+,\s*(?:token|session)")

    def test_failure_shows_server_detail_and_keeps_inputs(self) -> None:
        script = self._parse(self._render()).scripts[0]
        self.assertIn("response.ok", script)
        self.assertRegex(script, r"data\.detail\s*\|\|")
        self.assertNotRegex(script, r"usernameInput\.value\s*=\s*[\"'][\"']")
        self.assertNotRegex(script, r"passwordInput\.value\s*=\s*[\"'][\"']")

    def test_page_has_no_unload_logout_or_auth_secret_storage(self) -> None:
        rendered = self._render()
        lowered = rendered.lower()
        for forbidden in (
            "beforeunload",
            "unload",
            "pagehide",
            "sendbeacon",
            "remember_device",
            "remember-device",
        ):
            self.assertNotIn(forbidden, lowered)
        self.assertNotRegex(lowered, r"(?:localstorage|sessionstorage).{0,80}(?:password|token|session)")

    def test_module_is_pure_and_has_no_web_or_database_imports(self) -> None:
        self._module()
        tree = ast.parse(SOURCE_PATH.read_text(encoding="utf-8"))
        imports: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imports.add(node.module or "")
        self.assertEqual({"html"}, imports)
        forbidden_calls = {"open", "connect", "urlopen"}
        called_names = {
            node.func.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }
        self.assertTrue(forbidden_calls.isdisjoint(called_names))


if __name__ == "__main__":
    unittest.main()
