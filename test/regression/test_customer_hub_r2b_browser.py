"""R2B 客户通真浏览器代表集。

只使用临时合成 SQLite、临时图片目录和随机本机端口；不读取项目数据目录。
"""
from __future__ import annotations

import json
import os
import secrets
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright


REPO = Path(__file__).resolve().parents[2]
BASE_PERMISSIONS = {
    "patient.profile.view",
    "return_visit.view",
    "communication.view",
    "call_record.view",
}
PROFILES = {
    "system_admin": {"permissions": set(), "system_admin": True},
    "read_only": {"permissions": BASE_PERMISSIONS, "system_admin": False},
    "return_writer": {
        "permissions": BASE_PERMISSIONS | {"return_visit.manage", "return_visit.delete"},
        "system_admin": False,
    },
    "communication_writer": {
        "permissions": BASE_PERMISSIONS
        | {"communication.manage", "communication.image.manage", "communication.delete"},
        "system_admin": False,
    },
    "call_writer": {
        "permissions": BASE_PERMISSIONS | {"call_record.manage", "call_record.delete"},
        "system_admin": False,
    },
    "dictionary_writer": {
        "permissions": BASE_PERMISSIONS | {"return_visit.manage", "communication.manage"},
        "system_admin": False,
    },
}
VIEWPORTS = ((1440, 900), (1024, 900), (760, 900), (480, 840))
PRIVATE_MARKERS = (
    "synthetic-private-contact",
    "synthetic-private-card",
    "synthetic-private-account",
)
ALLOWED_PATIENT_PHONE_PLACEHOLDER = "synthetic-contact-placeholder"
MOJIBAKE = ("\ufffd", "\u00e6\u201c", "\u00e5\u203a", "\u00e7\u0161", "\u00e9\u201d")


def _free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _replace_access_tables(conn):
    conn.execute("pragma foreign_keys = off")
    conn.executescript(
        """
        drop table sessions;
        drop table users;
        drop table staff_members;
        drop table audit_logs;
        """
    )
    conn.executescript((REPO / "local_app/personnel_access_schema.sql").read_text("utf-8"))


class CustomerHubR2BBrowserTest(unittest.TestCase):
    maxDiff = None

    @classmethod
    def setUpClass(cls):
        from local_app.db import init_db
        from local_app.passwords import hash_password

        cls._temp = tempfile.TemporaryDirectory(prefix="customer-hub-r2b-browser-")
        cls.temp_root = Path(cls._temp.name)
        cls.app_dir = cls.temp_root / "app"
        cls.app_dir.mkdir()
        cls.images_dir = cls.app_dir / "images"
        cls.images_dir.mkdir()
        cls.db_path = cls.app_dir / "synthetic.sqlite3"
        cls.password = secrets.token_urlsafe(24)
        cls.credentials = {}
        init_db(cls.db_path)

        with sqlite3.connect(cls.db_path) as conn:
            _replace_access_tables(conn)
            now = "2026-07-21 09:00:00"
            roles = [(f"r2b_{name}", f"合成{name}", 0, index) for index, name in enumerate(PROFILES, 10)]
            roles.extend((("doctor", "医生", 1, 1), ("visitor", "回访人", 0, 2)))
            conn.executemany(
                "insert or replace into roles(role_key,name,is_system,sort) values (?,?,?,?)",
                roles,
            )
            for index, (name, profile) in enumerate(PROFILES.items(), 1):
                role_key = f"r2b_{name}"
                staff_id = f"staff-r2b-{index}"
                user_id = f"user-r2b-{index}"
                username = f"r2b-profile-{index}"
                cls.credentials[name] = (username, cls.password)
                conn.execute(
                    "insert into staff_members(staff_id,name,phone,id_card,employment_status,created_at,updated_at) "
                    "values (?,?,?,?, 'employed', ?, ?)",
                    (staff_id, f"合成画像{index}", "", "", now, now),
                )
                conn.execute(
                    "insert into staff_role_assignments(staff_id,role_key,is_primary,created_at) values (?,?,1,?)",
                    (staff_id, role_key, now),
                )
                conn.execute(
                    "insert into users(id,username,display_name,password_hash,staff_id,is_active,is_system_admin,account_kind) "
                    "values (?,?,?,?,?,1,?,'staff')",
                    (
                        user_id,
                        username,
                        f"合成用户{index}",
                        hash_password(cls.password),
                        staff_id,
                        1 if profile["system_admin"] else 0,
                    ),
                )
                conn.executemany(
                    "insert into role_permissions(role_key,perm_key) values (?,?)",
                    [(role_key, permission) for permission in sorted(profile["permissions"])],
                )

            for staff_id, name, role_key, private_contact, private_card in (
                ("staff-doctor-synthetic", "合成医生", "doctor", PRIVATE_MARKERS[0], PRIVATE_MARKERS[1]),
                ("staff-visitor-synthetic", "合成回访人", "visitor", PRIVATE_MARKERS[0], PRIVATE_MARKERS[1]),
            ):
                conn.execute(
                    "insert into staff_members(staff_id,name,phone,id_card,employment_status,created_at,updated_at) "
                    "values (?,?,?,?, 'employed', ?, ?)",
                    (staff_id, name, private_contact, private_card, now, now),
                )
                conn.execute(
                    "insert into staff_role_assignments(staff_id,role_key,is_primary,created_at) values (?,?,1,?)",
                    (staff_id, role_key, now),
                )

            conn.execute(
                "insert into patients(patient_identity,display_name,phone,responsible_doctor,current_hash,source_json,updated_at) "
                "values ('patient-r2b-synthetic','合成客户',?,'合成医生','hash-synthetic','{}',?)",
                (ALLOWED_PATIENT_PHONE_PLACEHOLDER, now),
            )
            conn.execute(
                "insert into return_visits(return_visit_id,patient_identity,due_time,item_name,status,note,visitor,channel,revision,updated_at) "
                "values ('rv-r2b-synthetic','patient-r2b-synthetic','2026-07-21 10:00:00','合成回访草稿','待回访','合成备注','合成回访人','phone',1,?)",
                (now,),
            )
            conn.execute(
                "insert into communications(communication_id,patient_identity,channel,direction,reason,contacted_at,operator,content,note,revision,created_at,updated_at) "
                "values ('comm-r2b-synthetic','patient-r2b-synthetic','wechat','','合成沟通','2026-07-21 09:30:00','合成操作人','合成内容','合成沟通草稿',1,?,?)",
                (now, now),
            )
            conn.execute(
                "insert into calls(call_id,direction,source,patient_identity,match_status,operator,started_at,linked_return_visit_id,note,revision,created_at) "
                "values ('call-r2b-synthetic','outbound','manual_import','patient-r2b-synthetic','manual','合成操作人','2026-07-21 09:40:00','','合成通话草稿',1,?)",
                (now,),
            )
            settings = {
                "clinic": {"name": "合成诊所"},
                "return_type": ["合成回访类型"],
                "visit_content": ["合成回访内容"],
                "visit_result": ["合成回访结果"],
                "comm_reason": ["合成沟通原因"],
            }
            conn.executemany(
                "insert or replace into app_settings(key,value,updated_at) values (?,?,?)",
                [(key, json.dumps(value, ensure_ascii=False), now) for key, value in settings.items()],
            )
            conn.commit()

        cls.port = _free_port()
        cls.base = f"http://127.0.0.1:{cls.port}"
        cls._server_stdout = open(cls.temp_root / "server.stdout", "wb")
        cls._server_stderr = open(cls.temp_root / "server.stderr", "wb")
        env = {
            **os.environ,
            "DENTAL_DB": str(cls.db_path),
            "DENTAL_IMAGES": str(cls.images_dir),
            "DENTAL_PORT": str(cls.port),
            "DENTAL_LOCAL_ONLY": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        cls.server = subprocess.Popen(
            [sys.executable, "-m", "local_app.run_local"],
            cwd=REPO,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=cls._server_stdout,
            stderr=cls._server_stderr,
            start_new_session=True,
        )
        deadline = time.time() + 45
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(cls.base + "/api/health", timeout=2) as response:
                    if response.status == 200:
                        break
            except OSError:
                if cls.server.poll() is not None:
                    raise RuntimeError(f"合成服务提前退出: {cls.server.returncode}")
                time.sleep(0.2)
        else:
            raise RuntimeError("合成服务 45s 内未就绪")

        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch(headless=True)

    @classmethod
    def tearDownClass(cls):
        try:
            if hasattr(cls, "browser"):
                cls.browser.close()
            if hasattr(cls, "playwright"):
                cls.playwright.stop()
        finally:
            if hasattr(cls, "server") and cls.server.poll() is None:
                os.killpg(os.getpgid(cls.server.pid), signal.SIGTERM)
                try:
                    cls.server.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    os.killpg(os.getpgid(cls.server.pid), signal.SIGKILL)
                    cls.server.wait(timeout=8)
            if hasattr(cls, "_server_stdout"):
                cls._server_stdout.close()
            if hasattr(cls, "_server_stderr"):
                cls._server_stderr.close()
            if hasattr(cls, "_temp"):
                cls._temp.cleanup()

    def setUp(self):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "update return_visits set item_name='合成回访草稿', note='合成备注', visitor='合成回访人', "
                "channel='phone', status='待回访', return_result='', revision=1, updated_at='2026-07-21 09:00:00' "
                "where return_visit_id='rv-r2b-synthetic'"
            )
            conn.execute(
                "update communications set note='合成沟通草稿', revision=1, updated_at='2026-07-21 09:00:00' "
                "where communication_id='comm-r2b-synthetic'"
            )
            conn.execute(
                "update calls set note='合成通话草稿', linked_return_visit_id='', revision=1 "
                "where call_id='call-r2b-synthetic'"
            )
            conn.execute("delete from return_visit_images")
            conn.execute("delete from communication_images")
            conn.execute("delete from attachment_file_ops")
            conn.execute("delete from role_permissions")
            for name, profile in PROFILES.items():
                conn.executemany(
                    "insert into role_permissions(role_key,perm_key) values (?,?)",
                    [(f"r2b_{name}", permission) for permission in sorted(profile["permissions"])],
                )
            dictionaries = {
                "return_type": ["合成回访类型"],
                "visit_content": ["合成回访内容"],
                "visit_result": ["合成回访结果"],
                "comm_reason": ["合成沟通原因"],
            }
            conn.executemany(
                "insert or replace into app_settings(key,value,updated_at) values (?,?,?)",
                [
                    (key, json.dumps(value, ensure_ascii=False), "2026-07-21 09:00:00")
                    for key, value in dictionaries.items()
                ],
            )
            conn.commit()

    def _page(self, profile, viewport=(1440, 900)):
        context = self.browser.new_context(viewport={"width": viewport[0], "height": viewport[1]})
        page = context.new_page()
        problems = []
        responses = []

        def route_local_only(route):
            parsed = urlparse(route.request.url)
            if parsed.hostname not in {"127.0.0.1", None} or parsed.port not in {self.port, None}:
                problems.append(f"external-request:{parsed.scheme}://{parsed.netloc}")
                route.abort()
                return
            route.continue_()

        page.route("**/*", route_local_only)
        page.on(
            "console",
            lambda message: problems.append(
                f"console:{message.text} url={message.location.get('url', '')}"
            ) if message.type == "error" else None,
        )
        page.on("pageerror", lambda error: problems.append(f"pageerror:{error}"))
        page.on(
            "response",
            lambda response: responses.append(response)
            if self._is_customer_hub_json_response(response.url) else None,
        )
        page.goto(self.base + "/", wait_until="domcontentloaded")
        page.wait_for_selector("#loginForm")
        username, password = self.credentials[profile]
        page.fill("#username", username)
        page.fill("#password", password)
        page.click("#loginButton")
        page.wait_for_selector('.nav-item[data-workspace-view="return-visits"]', timeout=15000)
        page.wait_for_timeout(300)
        return context, page, problems, responses

    def _is_customer_hub_json_response(self, url):
        path = urlparse(url).path
        if path in {
            "/api/customer-hub/today",
            "/api/return-visits",
            "/api/communications",
            "/api/calls",
            "/api/staff-members",
            "/api/settings/customer-hub-dicts",
            "/api/patients/search",
            "/api/dictionaries",
        }:
            return True
        if path.startswith("/api/return-visits/"):
            return path != "/api/return-visits/export" and not path.endswith("/file")
        if path.startswith("/api/communications/"):
            return not path.endswith("/file")
        if path.startswith("/api/calls/"):
            return not path.endswith("/recording")
        return path.startswith("/api/patients/") and not path.endswith("/file")

    def _open_hub(self, page, *, call_context=False):
        del call_context
        page.click('.nav-item[data-workspace-view="return-visits"]')
        page.wait_for_selector("#modulePanel #chvBody")
        page.wait_for_selector("#chvBody .module-loading", state="detached", timeout=15000)

    def _select_view(self, page, view):
        page.click(f'[data-chv-view="{view}"]')
        page.wait_for_function(
            """view => {
              const active = document.querySelector('[data-chv-view].on');
              return active && active.dataset.chvView === view
                && !document.querySelector('#chvBody .module-loading');
            }""",
            arg=view,
            timeout=15000,
        )

    def _assert_layout(self, page, width, label):
        dimensions = page.evaluate(
            """() => {
              const visible = element => {
                const style = getComputedStyle(element);
                const box = element.getBoundingClientRect();
                return style.display !== 'none' && style.visibility !== 'hidden'
                  && box.width > 0 && box.height > 0;
              };
              const describe = element => {
                const box = element.getBoundingClientRect();
                return {tag: element.tagName, id: element.id, cls: String(element.className || ''),
                  left: Math.round(box.left), right: Math.round(box.right),
                  top: Math.round(box.top), bottom: Math.round(box.bottom),
                  width: Math.round(box.width)};
              };
              const tracked = [document.documentElement, document.body,
                document.querySelector('#modulePanel'),
                ...document.querySelectorAll('.chv-pop')].filter(Boolean);
              const overflow = tracked.map(element => ({
                ...describe(element), scroll: element.scrollWidth, client: element.clientWidth,
              })).filter(item => item.scroll > item.client + 1);
              const tableOverflow = [...document.querySelectorAll('.chv-table')].map(table => {
                const container = table.closest('.chv-panel, .chv-pbody, .chv-mpane');
                const style = container ? getComputedStyle(container) : null;
                return {table: describe(table), container: container ? describe(container) : null,
                  needsScroll: Boolean(container && table.scrollWidth > container.clientWidth + 1),
                  overflowX: style ? style.overflowX : ''};
              }).filter(item => item.needsScroll && !['auto', 'scroll'].includes(item.overflowX));
              const intersections = [];
              document.querySelectorAll('.chv-tabs,.chv-ovbar,.chv-kpis,.chv-phead,.chv-fbar,.chv-mbody,.chv-mfoot')
                .forEach(container => {
                  if (!visible(container)) return;
                  const children = [...container.children].filter(visible);
                  for (let left = 0; left < children.length; left += 1) {
                    const a = children[left].getBoundingClientRect();
                    for (let right = left + 1; right < children.length; right += 1) {
                      const b = children[right].getBoundingClientRect();
                      if (Math.min(a.right, b.right) - Math.max(a.left, b.left) > 1
                          && Math.min(a.bottom, b.bottom) - Math.max(a.top, b.top) > 1) {
                        intersections.push({container: describe(container),
                          first: describe(children[left]), second: describe(children[right])});
                      }
                    }
                  }
                });
              const overlay = [...document.querySelectorAll('.chv-ovl')].filter(visible).at(-1);
              const actionRoot = overlay || document.querySelector('#modulePanel');
              const centerExposed = (element, x, y) => {
                for (let parent = element.parentElement; parent; parent = parent.parentElement) {
                  const style = getComputedStyle(parent);
                  if (!['auto', 'scroll', 'hidden', 'clip'].includes(style.overflowX)
                      && !['auto', 'scroll', 'hidden', 'clip'].includes(style.overflowY)) continue;
                  const box = parent.getBoundingClientRect();
                  if (x < box.left || x > box.right || y < box.top || y > box.bottom) return false;
                }
                return true;
              };
              const blocked = actionRoot ? [...actionRoot.querySelectorAll('button,.chv-btn')]
                .filter(element => visible(element) && !element.disabled)
                .map(element => {
                  const box = element.getBoundingClientRect();
                  const x = box.left + box.width / 2;
                  const y = box.top + box.height / 2;
                  if (x < 0 || x >= innerWidth || y < 0 || y >= innerHeight) return null;
                  if (!centerExposed(element, x, y)) return null;
                  const hit = document.elementFromPoint(x, y);
                  return hit && (hit === element || element.contains(hit)) ? null
                    : {button: describe(element), hit: hit ? describe(hit) : null};
                }).filter(Boolean) : [];
              return {overflow, tableOverflow, intersections, blocked,
                rootScroll: document.documentElement.scrollWidth,
                rootClient: document.documentElement.clientWidth,
                bodyScroll: document.body.scrollWidth,
                bodyClient: document.body.clientWidth};
            }"""
        )
        self.assertLessEqual(
            dimensions["rootScroll"], dimensions["rootClient"] + 1,
            f"{label}: 根节点横向溢出 {dimensions['overflow']}",
        )
        self.assertLessEqual(
            dimensions["bodyScroll"], dimensions["bodyClient"] + 1,
            f"{label}: body 横向溢出 {dimensions['overflow']}",
        )
        self.assertEqual(dimensions["overflow"], [], f"{label}: 页面/模块/弹窗横向溢出")
        self.assertEqual(dimensions["tableOverflow"], [], f"{label}: 表格被裁切且无内部横滚")
        self.assertEqual(dimensions["intersections"], [], f"{label}: 直接子项相交")
        self.assertEqual(dimensions["blocked"], [], f"{label}: 按钮中心被遮挡")
        for selector in ("#modulePanel", "#chvBody"):
            box = page.locator(selector).bounding_box()
            self.assertIsNotNone(box, f"{label}: {selector} 应可达")
            self.assertGreaterEqual(box["x"], -1, f"{label}: {selector} 左侧溢出")
            self.assertLessEqual(box["x"] + box["width"], width + 1, f"{label}: {selector} 右侧溢出")

    def _assert_approved_shell(self, page, label):
        self.assertEqual(page.locator("#workspaceTitle").inner_text().strip(), "客户通", label)
        self.assertEqual(page.locator("#modulePanel .chv-tabs").count(), 1, label)
        self.assertEqual(page.locator("#modulePanel [data-chv-view]").count(), 3, label)
        self.assertEqual(page.locator("#modulePanel [data-chv2-view], #modulePanel [data-chv2-tab]").count(), 0, label)
        duplicate_titles = page.locator("#modulePanel h1, #modulePanel h2, #modulePanel h3").evaluate_all(
            "items => items.filter(item => item.textContent.trim() === '客户通').length"
        )
        self.assertEqual(duplicate_titles, 0, f"{label}: 模块内不得出现第二个客户通标题")

    def _assert_no_runtime_problems(self, problems, label):
        self.assertEqual(problems, [], f"{label}: console/pageerror/外网请求应为空")

    def _assert_customer_hub_dom_privacy(self, page, label):
        records = page.locator("#modulePanel").evaluate(
            """root => [root, ...root.querySelectorAll('*')]
              .filter(element => !['SCRIPT', 'STYLE'].includes(element.tagName))
              .map((element, index) => ({
                index,
                tag: element.tagName,
                text: element.textContent || '',
                value: ['INPUT', 'TEXTAREA', 'SELECT'].includes(element.tagName)
                  ? String(element.value || '') : '',
                attributes: Array.from(element.attributes, attribute => ({
                  name: attribute.name,
                  value: attribute.value
                }))
              }))"""
        )
        forbidden_keys = ("phone", "mobile", "id_card", "username", "account", "credential")

        def assert_clean(value, location):
            lowered = value.lower()
            for forbidden in forbidden_keys:
                self.assertNotIn(forbidden, lowered, f"{label}: {location} 不应出现 {forbidden}")
            for marker in PRIVATE_MARKERS:
                self.assertNotIn(marker.lower(), lowered, f"{label}: {location} 泄露合成私密值")
            for broken in MOJIBAKE:
                self.assertNotIn(broken, value, f"{label}: {location} 出现乱码")

        for record in records:
            location = f"DOM#{record['index']}<{record['tag']}>"
            assert_clean(record["text"], location + ".textContent")
            assert_clean(record["value"], location + ".value")
            for attribute in record["attributes"]:
                assert_clean(attribute["name"], location + ".attribute-name")
                assert_clean(attribute["value"], location + ".attribute-value")

    def _assert_response_privacy(self, responses, label):
        staff_private_keys = {"phone", "mobile", "id_card", "username", "account", "credential"}
        global_private_keys = {"username", "account", "credential", "password", "secret", "token"}

        def walk(value, path="$", *, response_path=""):
            if isinstance(value, dict):
                for key, item in value.items():
                    normalized_key = str(key).lower()
                    if response_path == "/api/staff-members":
                        self.assertFalse(
                            any(forbidden in normalized_key for forbidden in staff_private_keys),
                            f"{label}: 员工目录 {path}.{key} 不应出现",
                        )
                    self.assertFalse(
                        any(forbidden in normalized_key for forbidden in global_private_keys),
                        f"{label}: {path}.{key} 不应出现账号或凭据字段",
                    )
                    if normalized_key == "phone" and response_path != "/api/staff-members":
                        self.assertIn(
                            item,
                            (None, "", ALLOWED_PATIENT_PHONE_PLACEHOLDER),
                            f"{label}: {path}.{key} 只允许合成患者电话占位",
                        )
                    walk(item, f"{path}.{key}", response_path=response_path)
            elif isinstance(value, list):
                for index, item in enumerate(value):
                    walk(item, f"{path}[{index}]", response_path=response_path)
            elif isinstance(value, str):
                for marker in PRIVATE_MARKERS:
                    self.assertNotIn(marker, value, f"{label}: {path} 泄露合成私密值")
                for broken in MOJIBAKE:
                    self.assertNotIn(broken, value, f"{label}: {path} 出现乱码")

        for response in responses:
            response_url = response.url
            response_status = response.status
            content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
            self.assertEqual(
                content_type,
                "application/json",
                f"{label}: JSON content-type 合同失败 url={response_url} status={response_status}",
            )
            try:
                payload = response.json()
            except (PlaywrightError, json.JSONDecodeError):
                self.fail(
                    f"{label}: JSON 解析失败 url={response_url} status={response_status}"
                )
            response_path = urlparse(response_url).path
            walk(payload, response_path, response_path=response_path)

    def _consume_expected_http_console(self, page, problems, status):
        page.wait_for_timeout(100)
        marker = f"status of {status} ("
        matches = [
            problem for problem in problems
            if problem.startswith("console:Failed to load resource:") and marker in problem
        ]
        self.assertEqual(len(matches), 1, f"应捕获且仅捕获一条预期 HTTP {status} 资源行")
        problems.remove(matches[0])

    def _assert_exact_write_count(self, writes, method, path, label):
        count = sum(
            1 for seen_method, seen_path in writes
            if (seen_method, seen_path) == (method, path)
        )
        self.assertEqual(
            count,
            1,
            f"{label}: {method} {path} 写请求应恰好 1 次，实际 {count} 次",
        )

    def _close(self, context, problems, responses, label, *, scan_responses=True):
        try:
            pages = context.pages
            if pages:
                self._assert_customer_hub_dom_privacy(pages[0], label)
            self._assert_no_runtime_problems(problems, label)
            if scan_responses:
                self._assert_response_privacy(responses, label)
        finally:
            context.close()

    def test_privacy_and_replay_guards_reject_synthetic_bypass_probes(self):
        class MalformedJsonResponse:
            url = "http://127.0.0.1/api/staff-members"
            status = 200
            headers = {"content-type": "application/json"}

            @staticmethod
            def json():
                raise json.JSONDecodeError("synthetic malformed", "{", 1)

        with self.subTest(probe="malformed-json"):
            with self.assertRaises(AssertionError):
                self._assert_response_privacy([MalformedJsonResponse()], "synthetic-malformed")

        with self.subTest(probe="duplicate-write"):
            duplicate_writes = [
                ("PUT", "/api/settings/customer-hub-dicts"),
                ("PUT", "/api/settings/customer-hub-dicts"),
            ]
            with self.assertRaises(AssertionError):
                self._assert_exact_write_count(
                    duplicate_writes,
                    "PUT",
                    "/api/settings/customer-hub-dicts",
                    "synthetic-replay",
                )

        with self.subTest(probe="hidden-dom-sensitive"):
            context, page, problems, responses = self._page("read_only")
            try:
                self._open_hub(page)
                page.locator("#modulePanel").evaluate(
                    """(root, marker) => {
                      const hidden = document.createElement('div');
                      hidden.hidden = true;
                      hidden.setAttribute('data-phone', marker);
                      hidden.setAttribute('aria-label', marker);
                      hidden.innerHTML = `<input value="${marker}"><span>${marker}</span>`;
                      root.appendChild(hidden);
                    }""",
                    PRIVATE_MARKERS[0],
                )
                with self.assertRaises(AssertionError):
                    self._assert_customer_hub_dom_privacy(page, "synthetic-hidden")
                page.locator('#modulePanel [data-phone]').evaluate("element => element.remove()")
            finally:
                self._close(context, problems, responses, "synthetic-guard-probes")

    def test_six_permission_profiles_match_visible_controls_and_backend(self):
        permission_probes = {
            "return_visit.manage": (
                "PUT", "/api/return-visits/rv-r2b-synthetic",
                {"item_name": "合成越权值", "expected_revision": 1},
            ),
            "communication.manage": (
                "PATCH", "/api/communications/comm-r2b-synthetic",
                {"note": "合成越权值", "expected_revision": 1},
            ),
            "call_record.manage": (
                "PATCH", "/api/calls/call-r2b-synthetic",
                {"note": "合成越权值", "expected_revision": 1},
            ),
        }
        for profile in PROFILES:
            with self.subTest(profile=profile):
                context, page, problems, responses = self._page(profile)
                try:
                    self._open_hub(page)
                    self._assert_approved_shell(page, profile)
                    system_admin = PROFILES[profile]["system_admin"]
                    permissions = PROFILES[profile]["permissions"]
                    can_return = system_admin or "return_visit.manage" in permissions
                    can_delete = system_admin or "return_visit.delete" in permissions
                    can_communicate = system_admin or "communication.manage" in permissions
                    can_call = system_admin or "call_record.manage" in permissions
                    self.assertEqual(page.locator("[data-chv-kpinew]").count() > 0, can_return, profile)
                    self.assertEqual(page.locator("[data-chv-addcomm]").count() > 0, can_communicate, profile)
                    self._select_view(page, "list")
                    self.assertEqual(page.locator("[data-chv-batchdel]").count() > 0, can_delete, profile)
                    page.locator(".chv-row[data-chv-open]").first.click()
                    page.wait_for_selector("#chvRvOvl .chv-pop")
                    self.assertEqual(page.locator("#chvRvOvl [data-chv-ok]").count() > 0, can_return, profile)
                    self.assertEqual(page.locator("#chvRvOvl [data-chv-del]").count() > 0, can_delete, profile)
                    self.assertEqual(page.locator("#chvRvOvl [data-chv-mic]").count() > 0, can_call, profile)
                    self.assertEqual(page.locator("#chvRvOvl [data-chv-dict]").count() > 0, can_return, profile)
                    page.locator("#chvRvOvl [data-chv-close]").first.click()
                    page.wait_for_selector("#chvRvOvl", state="detached")
                    if not PROFILES[profile]["system_admin"]:
                        for permission, (method, path, payload) in permission_probes.items():
                            if permission in PROFILES[profile]["permissions"]:
                                continue
                            response = context.request.fetch(
                                self.base + path,
                                method=method,
                                headers={"Content-Type": "application/json"},
                                data=json.dumps(payload, ensure_ascii=False),
                            )
                            self.assertEqual(
                                response.status,
                                403,
                                f"{profile}:{permission} 后端必须独立拒绝",
                            )
                    self._assert_layout(page, 1440, profile)
                finally:
                    self._close(context, problems, responses, profile)

    def test_system_admin_and_read_only_cover_four_viewports(self):
        for profile in ("system_admin", "read_only"):
            for width, height in VIEWPORTS:
                label = f"{profile}@{width}x{height}"
                with self.subTest(label=label):
                    context, page, problems, responses = self._page(profile, (width, height))
                    try:
                        self._open_hub(page)
                        self._assert_approved_shell(page, label)
                        for view in ("today", "list", "comm"):
                            self._select_view(page, view)
                            self.assertEqual(
                                page.locator("[data-chv-view].on").get_attribute("data-chv-view"),
                                view,
                                label,
                            )
                            self._assert_layout(page, width, f"{label}:{view}")
                        self._select_view(page, "list")
                        page.locator(".chv-row[data-chv-open]").first.click()
                        page.wait_for_selector("#chvRvOvl .chv-pop")
                        self._assert_layout(page, width, f"{label}:return-dialog")
                        page.locator("#chvRvOvl [data-chv-close]").first.click()
                        page.wait_for_selector("#chvRvOvl", state="detached")
                        if profile == "system_admin" and width == 1440:
                            self._select_view(page, "today")
                            page.screenshot(path="/tmp/customer-hub-v2-approved-1440.png", full_page=True)
                    finally:
                        self._assert_no_runtime_problems(problems, label)
                        context.close()

    def test_calendar_monthly_summary_and_two_staff_directories(self):
        context, page, problems, responses = self._page("system_admin")
        try:
            self._open_hub(page)
            page.click("[data-chv-cal]")
            page.wait_for_selector("#chvCalOvl .chv-pop")
            self.assertGreater(page.locator("#chvCalOvl [data-chv-day]").count(), 27)
            self.assertEqual(page.locator("#chvCalOvl .chv-pop").get_attribute("role"), "dialog")
            page.locator("#chvCalOvl [data-chv-day]").first.click()
            page.wait_for_selector("#chvCalOvl", state="detached")
            page.wait_for_selector("#chvBody .module-loading", state="detached", timeout=15000)

            page.click("[data-chv-mr]")
            page.wait_for_selector("#chvMrOvl .chv-pop")
            content = page.locator("#chvMrOvl").inner_text()
            self.assertIn("月度回顾", content)
            self.assertIn("完成率", content)
            self._assert_layout(page, 1440, "calendar-monthly")
            page.locator("#chvMrOvl [data-chv-close]").click()
            page.wait_for_selector("#chvMrOvl", state="detached")

            self._select_view(page, "list")
            doctors = page.locator('[data-chv-f="doctor"]')
            visitors = page.locator('[data-chv-f="visitor"]')
            self.assertEqual(doctors.count(), 1)
            self.assertEqual(visitors.count(), 1)
            self.assertIn("合成医生", doctors.locator("option").all_inner_texts())
            self.assertIn("合成回访人", visitors.locator("option").all_inner_texts())
            staff_responses = [response for response in responses if urlparse(response.url).path == "/api/staff-members"]
            self.assertGreaterEqual(len(staff_responses), 2)
            for response in staff_responses:
                for member in response.json()["members"]:
                    self.assertTrue({"staff_id", "name", "role", "roles"}.issubset(member))
                    self.assertTrue(
                        {"phone", "mobile", "id_card", "username", "account", "credential"}.isdisjoint(member)
                    )
        finally:
            self._close(context, problems, responses, "navigation-month-staff")

    def test_successful_return_visit_write_chains_response_revision(self):
        context, page, problems, responses = self._page("return_writer")
        seen_revisions = []
        page.on(
            "request",
            lambda request: seen_revisions.append(json.loads(request.post_data)["expected_revision"])
            if request.method == "PUT" and urlparse(request.url).path == "/api/return-visits/rv-r2b-synthetic" else None,
        )
        try:
            self._open_hub(page)
            self._select_view(page, "list")
            for note in ("合成成功一", "合成成功二"):
                page.locator(".chv-row[data-chv-open]").first.click()
                page.wait_for_selector("#chvRvOvl .chv-pop")
                field = page.locator("#chvMC")
                field.fill(note)
                with page.expect_response(
                    lambda response: response.request.method == "PUT"
                    and urlparse(response.url).path == "/api/return-visits/rv-r2b-synthetic"
                ) as response_info:
                    page.click("#chvRvOvl [data-chv-ok]")
                self.assertEqual(response_info.value.status, 200)
                page.wait_for_selector("#chvRvOvl", state="detached", timeout=15000)
                page.wait_for_selector("#chvBody .module-loading", state="detached", timeout=15000)
                self.assertIn(note, page.locator("#chvBody").inner_text())
            self.assertEqual(seen_revisions, [1, 2])
        finally:
            self._close(context, problems, responses, "success-revision")

    def test_real_403_409_and_500_keep_overlay_draft_focus_and_pending_file(self):
        scenarios = (
            (403, "PUT", "/api/return-visits/rv-r2b-synthetic"),
            (409, "PUT", "/api/return-visits/rv-r2b-synthetic"),
            (500, "POST", "/api/return-visits/rv-r2b-synthetic/images"),
        )
        for status, write_method, write_path in scenarios:
            label = f"return-visit-{status}"
            with self.subTest(label=label):
                context, page, problems, responses = self._page("return_writer")
                write_requests = []
                dialogs = []

                def capture_dialog(dialog):
                    dialogs.append(dialog.message)
                    dialog.dismiss()

                page.on("dialog", capture_dialog)
                page.on(
                    "request",
                    lambda request: write_requests.append(
                        (request.method, urlparse(request.url).path)
                    ) if request.method != "GET"
                    and self._is_customer_hub_json_response(request.url) else None,
                )
                backup = self.temp_root / f"images-backup-{status}"
                permission_removed = False
                try:
                    self._open_hub(page)
                    self._select_view(page, "list")
                    page.locator(".chv-row[data-chv-open]").first.click()
                    page.wait_for_selector("#chvRvOvl .chv-pop")
                    modal = page.locator("#chvRvOvl")
                    field = page.locator("#chvMC")
                    submit = page.locator("#chvRvOvl [data-chv-ok]")
                    draft = f"合成失败草稿-{status}"
                    field.fill(draft)

                    if status == 403:
                        with sqlite3.connect(self.db_path) as conn:
                            conn.execute(
                                "delete from role_permissions "
                                "where role_key='r2b_return_writer' and perm_key='return_visit.manage'"
                            )
                            conn.commit()
                        permission_removed = True
                    elif status == 409:
                        with sqlite3.connect(self.db_path) as conn:
                            conn.execute(
                                "update return_visits set revision=revision+1 "
                                "where return_visit_id='rv-r2b-synthetic'"
                            )
                            conn.commit()
                    else:
                        page.locator("#chvScFile").set_input_files(
                            {
                                "name": "synthetic.png",
                                "mimeType": "image/png",
                                "buffer": b"\x89PNG\r\n\x1a\nsynthetic",
                            }
                        )
                        self.assertIn("synthetic.png（待保存）", page.locator("#chvScList").inner_text())
                        self.images_dir.rename(backup)
                        self.images_dir.write_text("synthetic blocker", encoding="utf-8")

                    with page.expect_response(
                        lambda response: response.request.method == write_method
                        and urlparse(response.url).path == write_path,
                        timeout=15000,
                    ) as response_info:
                        submit.click()
                    self.assertEqual(response_info.value.status, status, label)
                    page.wait_for_function(
                        """() => {
                          const button = document.querySelector('#chvRvOvl [data-chv-ok]');
                          return button && !button.disabled && document.activeElement === button;
                        }""",
                        timeout=15000,
                    )
                    self._consume_expected_http_console(page, problems, status)
                    expected_error = {403: "权限不足", 409: "保存冲突", 500: "服务异常"}[status]
                    self.assertTrue(any(expected_error in message for message in dialogs), dialogs)
                    self.assertTrue(modal.is_visible(), label)
                    self.assertEqual(field.input_value(), draft, label)
                    self.assertTrue(submit.evaluate("button => button === document.activeElement"), label)
                    self._assert_exact_write_count(
                        write_requests, write_method, write_path, label,
                    )
                    if status == 500:
                        self.assertIn("synthetic.png（待保存）", page.locator("#chvScList").inner_text())
                finally:
                    if self.images_dir.is_file():
                        self.images_dir.unlink()
                    if backup.exists():
                        backup.rename(self.images_dir)
                    if permission_removed:
                        with sqlite3.connect(self.db_path) as conn:
                            conn.execute(
                                "insert or ignore into role_permissions(role_key,perm_key) "
                                "values ('r2b_return_writer','return_visit.manage')"
                            )
                            conn.commit()
                    self._close(context, problems, responses, label)
    def test_escape_dirty_focus_trap_pending_and_opener_restore_use_real_dom(self):
        context, page, problems, responses = self._page("return_writer")
        dialogs = []
        dialog_state = {"accept": False}
        lock_conn = None

        def handle_dialog(dialog):
            dialogs.append(dialog.message)
            if dialog_state["accept"]:
                dialog.accept()
            else:
                dialog.dismiss()

        page.on("dialog", handle_dialog)
        try:
            self._open_hub(page)
            self._select_view(page, "list")
            row = page.locator(".chv-row[data-chv-open]").first
            row.evaluate(
                """row => {
                  row.tabIndex = -1;
                  row.dataset.syntheticOpener = '1';
                  row.focus();
                  row.click();
                }"""
            )
            page.wait_for_selector("#chvRvOvl .chv-pop")
            modal = page.locator("#chvRvOvl")
            field = page.locator("#chvMC")
            modal.locator(".chv-pop").evaluate(
                """dialog => {
                  const enabled = document.createElement('div');
                  enabled.tabIndex = 0;
                  enabled.dataset.syntheticLast = '1';
                  dialog.appendChild(enabled);

                  const negative = document.createElement('div');
                  negative.tabIndex = -2;
                  negative.dataset.syntheticNegative = '1';
                  dialog.appendChild(negative);

                  const fieldset = document.createElement('fieldset');
                  fieldset.disabled = true;
                  const disabledChild = document.createElement('button');
                  disabledChild.type = 'button';
                  disabledChild.tabIndex = 0;
                  disabledChild.dataset.syntheticDisabledFieldset = '1';
                  fieldset.appendChild(disabledChild);
                  dialog.appendChild(fieldset);
                }"""
            )
            candidates = page.evaluate(
                """() => _chvDialogFocusables(document.querySelector('#chvRvOvl')).map(element =>
                  element.dataset.syntheticLast ? 'last'
                    : element.dataset.syntheticNegative ? 'negative'
                    : element.dataset.syntheticDisabledFieldset ? 'fieldset-disabled'
                    : element.id || element.tagName)"""
            )
            self.assertIn("last", candidates)
            self.assertNotIn("negative", candidates)
            self.assertNotIn("fieldset-disabled", candidates)
            last_focusable = modal.locator("[data-synthetic-last]")
            last_focusable.focus()
            page.keyboard.press("Tab")
            self.assertTrue(
                page.evaluate(
                    "() => document.activeElement === _chvDialogFocusables("
                    "document.querySelector('#chvRvOvl'))[0]"
                )
            )
            page.evaluate(
                "() => _chvDialogFocusables(document.querySelector('#chvRvOvl'))[0].focus()"
            )
            page.keyboard.press("Shift+Tab")
            self.assertTrue(last_focusable.evaluate("element => element === document.activeElement"))

            field.fill("合成未保存草稿")
            field.focus()
            page.keyboard.press("Escape")
            self.assertTrue(modal.is_visible())
            page.locator('.nav-item[data-workspace-view="patients"]').evaluate("element => element.click()")
            page.wait_for_timeout(100)
            self.assertTrue(modal.is_visible())
            self.assertTrue(page.locator("#modulePanel .chv-tabs").is_visible())
            self.assertGreaterEqual(len(dialogs), 2)

            dialog_state["accept"] = True
            field.focus()
            page.keyboard.press("Escape")
            page.wait_for_selector("#chvRvOvl", state="detached")
            self.assertTrue(
                page.evaluate(
                    "() => document.activeElement === document.querySelector('[data-synthetic-opener]')"
                )
            )

            row = page.locator("[data-synthetic-opener]")
            row.evaluate("row => { row.focus(); row.click(); }")
            page.wait_for_selector("#chvRvOvl .chv-pop")
            modal = page.locator("#chvRvOvl")
            field = page.locator("#chvMC")
            submit = page.locator("#chvRvOvl [data-chv-ok]")
            field.fill("合成等待锁草稿")
            lock_conn = sqlite3.connect(self.db_path, timeout=1)
            lock_conn.execute("begin immediate")
            submit.click()
            page.wait_for_function(
                "() => document.querySelector('#chvRvOvl [data-chv-ok]').disabled === true",
                timeout=15000,
            )
            self.assertTrue(modal.is_visible())
            self.assertTrue(field.is_disabled())
            self.assertTrue(
                modal.locator("input,select,textarea,button").evaluate_all(
                    "items => items.every(item => item.disabled)"
                )
            )
            pending_dialog_count = len(dialogs)
            page.keyboard.press("Escape")
            page.locator('.nav-item[data-workspace-view="patients"]').evaluate("element => element.click()")
            page.wait_for_timeout(100)
            self.assertTrue(modal.is_visible())
            self.assertEqual(len(dialogs), pending_dialog_count)
            self.assertTrue(page.locator("#modulePanel .chv-tabs").is_visible())

            lock_conn.rollback()
            lock_conn.close()
            lock_conn = None
            page.wait_for_selector("#chvRvOvl", state="detached", timeout=15000)
            page.wait_for_selector("#chvBody .module-loading", state="detached", timeout=15000)
        finally:
            if lock_conn is not None:
                lock_conn.rollback()
                lock_conn.close()
            self._close(context, problems, responses, "keyboard-dirty-pending")

if __name__ == "__main__":
    unittest.main()
