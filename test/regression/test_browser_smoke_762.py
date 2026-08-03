"""#762 传统界面导航真实浏览器冒烟:合成演示库起真服务,Playwright 真点击。

覆盖:登录 → 逐点顶导(10) → 各视图内逐点 .module-subtab 二级导航 → 患者列表点行
开工作台 → 逐点 13 患者 tab。每步断言:active 唯一、对应面板可见、内容不停留在
"载入中"、永不出现"载入失败/加载失败"、console 无 ERROR/pageerror。

依赖(可选):pip install playwright && playwright install chromium。未装则整类跳过,
不影响无浏览器环境跑其余套件。真机断言全走 DOM 实况,不做源码字符串断言。
"""
import json
import os
import contextlib
import gc
import secrets
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent

try:
    from playwright.sync_api import sync_playwright
    _HAS_PW = True
except ImportError:
    _HAS_PW = False

LOADING_WORDS = ("载入中", "加载中")
FAIL_WORDS = ("载入失败", "加载失败", "搜索失败")
# 主视图 → 承载面板(app.js switchWorkspaceView 的显隐逻辑)
VIEW_PANEL = {
    "today": "#todayWorkPanel", "inspection": "#todayWorkPanel", "ai-drafts": "#aiDraftsPanel",
    "patients": "#modulePanel", "appointments": "#modulePanel", "return-visits": "#modulePanel",
    "sterilization": "#modulePanel", "warehouse": "#modulePanel",
    "data-reports": "#modulePanel", "config": "#modulePanel",
}


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@unittest.skipUnless(_HAS_PW, "playwright 未安装(可选依赖):pip install playwright && playwright install chromium")
class BrowserSmokeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from local_app import access_policy, personnel_access_migration
        from local_app.auth import create_user
        from local_app.db import connect
        from local_app.demo import seed_demo
        from local_app.migrations import apply_migrations
        from local_app.passwords import hash_password

        cls._tmp = tempfile.TemporaryDirectory()
        data_dir = Path(cls._tmp.name)
        images_dir = data_dir / "images"
        legacy_db = data_dir / "demo_clinic_legacy.sqlite3"
        db = data_dir / "demo_clinic_v3.sqlite3"
        # run_local 已固定启用 access_v3。冒烟测试先按真实升级路径把纯合成旧库
        # 转成 V3 副本，不能再把旧 schema 直接交给 V3 登录栈。
        # seed_demo 默认直接产 v3 混合形态库(供演示首启);此处要演练升级路径,
        # 显式要旧形状库(v3_ready=False)。
        seed_demo.seed(legacy_db, images_dir=images_dir, v3_ready=False)
        apply_migrations(legacy_db)
        cls.username = "smoke-admin"
        cls.password = secrets.token_urlsafe(24)
        # sqlite3 的 with 只包事务不关连接;不关闭会让下面收口 WAL 的连接撞 database is locked
        with contextlib.closing(connect(legacy_db)) as conn:
            staff_row = conn.execute(
                "select s.staff_id from staff_members s "
                "left join users u on u.staff_id = s.staff_id "
                "where u.id is null order by s.staff_id limit 1"
            ).fetchone()
            if staff_row is None:
                raise RuntimeError("合成演示库缺少可绑定的人员")
            admin_id = create_user(
                conn,
                cls.username,
                "合成系统管理员",
                cls.password,
                role="admin",
                staff_id=staff_row["staff_id"],
            )
            conn.commit()

        # 迁移器的只读临时副本要求完整单文件；合成源库在最后一次写入后收口 WAL。
        # apply_migrations 沿途的连接躺在引用循环里等 GC,不先回收会撞 database is locked。
        gc.collect()
        with contextlib.closing(sqlite3.connect(legacy_db)) as conn:
            conn.execute("pragma wal_checkpoint(truncate)")
            journal_mode = conn.execute("pragma journal_mode=delete").fetchone()[0]
        if str(journal_mode).lower() != "delete":
            raise RuntimeError("合成演示库无法收口为单文件")

        personnel_access_migration.build_migrated_copy(
            legacy_db,
            db,
            {
                "regular_system_admin_user_ids": [admin_id],
                "break_glass_account": {
                    "id": "smoke-break-glass-user",
                    "username": "smoke-break-glass",
                    "display_name": "合成应急管理员",
                    "password_hash": hash_password(secrets.token_urlsafe(24)),
                },
                "staff_role_map": {
                    "医生": "doctor",
                    "护士": "nurse",
                    "咨询师": "consultant",
                },
                "acknowledged_legacy_user_role_user_ids": [admin_id],
                "status_conflict_resolutions": {},
            },
        )

        # 本测试覆盖 V3 UI/导航，临时合成库需先满足 V3 权限依赖合同。
        # 旧演示库的角色授权可能只有子权限；仅在这个一次性副本补齐递归依赖。
        with connect(db) as conn:
            # 响应式用例需同时进入“人员”与“角色与权限”，
            # 并能打开人员状态确认弹窗（只验布局，不执行业务请求）。
            # 系统管理员总钥匙不隐式授予人员业务权限，因此只在
            # 一次性合成副本中给其现有岗位补必需权限。
            for permission in ("staff.view", "staff.employment"):
                conn.execute(
                    "insert or ignore into role_permissions(role_key, perm_key) "
                    "select sra.role_key, ? from staff_role_assignments sra "
                    "join users u on u.staff_id = sra.staff_id where u.id = ?",
                    (permission, admin_id),
                )
            granted = list(conn.execute(
                "select role_key, perm_key from role_permissions"
            ))
            for row in granted:
                pending = list(access_policy.PERMISSION_DEPENDENCIES.get(
                    row["perm_key"], (),
                ))
                seen = set()
                while pending:
                    dependency = pending.pop()
                    if dependency in seen:
                        continue
                    seen.add(dependency)
                    conn.execute(
                        "insert or ignore into role_permissions(role_key, perm_key) "
                        "values (?, ?)",
                        (row["role_key"], dependency),
                    )
                    pending.extend(
                        access_policy.PERMISSION_DEPENDENCIES.get(dependency, ())
                    )
            conn.commit()

        cls.port = _free_port()
        env = {**os.environ, "DENTAL_DB": str(db), "DENTAL_IMAGES": str(images_dir),
               "DENTAL_PORT": str(cls.port), "DENTAL_LOCAL_ONLY": "1"}
        cls.server = subprocess.Popen([sys.executable, "-m", "local_app.run_local"],
                                      cwd=str(REPO), env=env,
                                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                      start_new_session=True)
        cls.base = f"http://127.0.0.1:{cls.port}"
        deadline = time.time() + 45
        while time.time() < deadline:
            try:
                urllib.request.urlopen(cls.base + "/", timeout=2)
                break
            except OSError:
                if cls.server.poll() is not None:
                    raise RuntimeError("演示服务进程提前退出")
                time.sleep(0.4)
        else:
            raise RuntimeError("演示服务 45s 内未就绪")

    @classmethod
    def tearDownClass(cls):
        import signal
        if cls.server.poll() is None:
            os.killpg(os.getpgid(cls.server.pid), signal.SIGKILL)
        cls.server.wait(timeout=10)
        cls._tmp.cleanup()

    # ---- 断言小工具 ----
    def _panel_settled(self, page, selector):
        """面板可见、失败字样零容忍、载入字样限时消失。"""
        el = page.locator(selector)
        self.assertTrue(el.is_visible(), f"{selector} 应可见")
        deadline = time.time() + 15
        while time.time() < deadline:
            text = el.inner_text()
            for w in FAIL_WORDS:
                self.assertNotIn(w, text, f"{selector} 出现失败态: {w}")
            if not any(w in text for w in LOADING_WORDS):
                return
            page.wait_for_timeout(200)
        self.fail(f"{selector} 15s 后仍停留在载入中")

    def _unique_active(self, page, group_selector, expect_key=None, key_attr=None):
        actives = page.locator(f"{group_selector}.active")
        self.assertEqual(actives.count(), 1, f"{group_selector} active 应唯一")
        if expect_key:
            self.assertEqual(actives.first.get_attribute(key_attr), expect_key,
                             f"active 应是 {expect_key}")

    def test_access_center_responsive_desktop_widths(self):
        console_errors = []
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            page = browser.new_page(viewport={"width": 1440, "height": 900})
            page.on(
                "console",
                lambda message: console_errors.append(f"console:{message.text}")
                if message.type == "error" else None,
            )
            page.on("pageerror", lambda error: console_errors.append(f"pageerror:{error}"))

            page.goto(self.base + "/")
            page.wait_for_selector("#loginForm")
            page.fill("#username", self.username)
            page.fill("#password", self.password)
            page.click("#loginButton")
            page.wait_for_selector(".nav-item[data-workspace-view]", timeout=15000)
            page.wait_for_timeout(800)
            first_run = page.locator(".modal-backdrop:visible", has_text="首次使用")
            if first_run.count():
                first_run.locator("input").first.fill("冒烟测试诊所")
                first_run.locator("button:has-text('确定')").click()

            page.click('.nav-item[data-workspace-view="config"]')
            page.wait_for_selector('[data-cfg-tab="access"]', timeout=15000)
            access_tab = page.locator('[data-cfg-tab="access"]')
            if not access_tab.evaluate("el => el.classList.contains('active')"):
                access_tab.click()
            page.wait_for_selector(
                '[data-access-center-tab="staff"]', state="attached", timeout=15000,
            )

            def assert_document_fits(width, page_name):
                dimensions = page.evaluate("""() => ({
                    scrollWidth: document.documentElement.scrollWidth,
                    clientWidth: document.documentElement.clientWidth,
                })""")
                self.assertLessEqual(
                    dimensions["scrollWidth"], dimensions["clientWidth"],
                    f"{width}px {page_name}不得产生整页横向滚动: {dimensions}",
                )

            for width in (1024, 1440, 1920):
                with self.subTest(width=width, page="staff"):
                    page.set_viewport_size({"width": width, "height": 900})
                    staff_tab = page.locator('[data-access-center-tab="staff"]')
                    if not staff_tab.evaluate("el => el.classList.contains('active')"):
                        staff_tab.click()
                    page.wait_for_selector(
                        ".access-staff-list-scroll", state="visible", timeout=15000,
                    )
                    assert_document_fits(width, "人员页")

                with self.subTest(width=width, page="roles"):
                    role_tab = page.locator('[data-access-center-tab="roles"]')
                    self.assertTrue(role_tab.is_visible(), "合成 V3 管理员应可见角色页")
                    if not role_tab.evaluate("el => el.classList.contains('active')"):
                        role_tab.click()
                    page.wait_for_selector(
                        ".access-role-layout", state="visible", timeout=15000,
                    )
                    save_button = page.locator("[data-access-save]")
                    self.assertEqual(save_button.count(), 1)
                    self.assertTrue(save_button.is_visible(), "窄屏下保存按钮仍应在文档流中可见")
                    rect = save_button.bounding_box()
                    self.assertIsNotNone(rect)
                    client_width = page.evaluate("document.documentElement.clientWidth")
                    self.assertGreaterEqual(rect["x"], -0.5)
                    self.assertLessEqual(rect["x"] + rect["width"], client_width + 0.5)
                    assert_document_fits(width, "角色与权限页")

            browser.close()
        self.assertEqual(console_errors, [], "响应式检查不应产生 console ERROR/pageerror")

    def test_access_staff_action_modal_controls_reachable_small_viewport(self):
        console_errors = []
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            page = browser.new_page(viewport={"width": 1024, "height": 900})
            page.on(
                "console",
                lambda message: console_errors.append(f"console:{message.text}")
                if message.type == "error" else None,
            )
            page.on("pageerror", lambda error: console_errors.append(f"pageerror:{error}"))

            page.goto(self.base + "/")
            page.wait_for_selector("#loginForm")
            page.fill("#username", self.username)
            page.fill("#password", self.password)
            page.click("#loginButton")
            page.wait_for_selector(".nav-item[data-workspace-view]", timeout=15000)
            page.wait_for_timeout(800)
            first_run = page.locator(".modal-backdrop:visible", has_text="首次使用")
            if first_run.count():
                first_run.locator("input").first.fill("冒烟测试诊所")
                first_run.locator("button:has-text('确定')").click()

            page.click('.nav-item[data-workspace-view="config"]')
            page.wait_for_selector('[data-cfg-tab="access"]', timeout=15000)
            access_tab = page.locator('[data-cfg-tab="access"]')
            if not access_tab.evaluate("el => el.classList.contains('active')"):
                access_tab.click()
            page.wait_for_selector(
                ".access-staff-row:not(.is-left) .access-staff-row-main",
                state="visible", timeout=15000,
            )
            page.locator(
                ".access-staff-row:not(.is-left) .access-staff-row-main"
            ).first.click()
            sheet = page.locator(".access-sheet-panel")
            sheet.wait_for(state="visible", timeout=5000)
            departure = sheet.locator("button", has_text="离职")
            self.assertGreater(departure.count(), 0, "合成管理员应可打开离职确认")
            departure.first.click()
            dialog = page.locator(".access-center-modal")
            dialog.wait_for(state="visible", timeout=5000)

            page.set_viewport_size({"width": 320, "height": 400})
            dialog.evaluate("el => { el.style.fontSize = '24px'; }")
            body = dialog.locator(".access-center-modal-body")
            self.assertEqual(body.count(), 1, "动作弹窗应有独立可滚动中段")
            geometry = dialog.evaluate("""dialog => {
                const body = dialog.querySelector('.access-center-modal-body');
                const footer = dialog.querySelector('.access-center-modal-actions');
                const confirm = dialog.querySelector('#accessStaffActionConfirm');
                const rect = element => {
                    const value = element.getBoundingClientRect();
                    return {top: value.top, right: value.right,
                            bottom: value.bottom, left: value.left};
                };
                return {
                    viewport: {width: innerWidth, height: innerHeight},
                    dialog: rect(dialog), body: rect(body), footer: rect(footer),
                    confirm: rect(confirm), overflowY: getComputedStyle(body).overflowY,
                    bodyScrollHeight: body.scrollHeight,
                    bodyClientHeight: body.clientHeight,
                };
            }""")
            self.assertIn(geometry["overflowY"], {"auto", "scroll"})
            self.assertGreater(
                geometry["bodyScrollHeight"], geometry["bodyClientHeight"],
                f"放大文字后动作中段应实际可滚动: {geometry}",
            )
            self.assertGreaterEqual(geometry["dialog"]["top"], -0.5)
            self.assertLessEqual(
                geometry["dialog"]["bottom"], geometry["viewport"]["height"] + 0.5,
            )
            self.assertLessEqual(
                geometry["footer"]["bottom"], geometry["dialog"]["bottom"] + 0.5,
                f"确认 footer 不得被 modal 裁掉: {geometry}",
            )
            self.assertLessEqual(
                geometry["confirm"]["bottom"], geometry["footer"]["bottom"] + 0.5,
            )
            self.assertGreaterEqual(geometry["confirm"]["left"], -0.5)
            self.assertLessEqual(
                geometry["confirm"]["right"], geometry["viewport"]["width"] + 0.5,
            )

            browser.close()
        self.assertEqual(console_errors, [], "动作弹窗检查不应产生 console ERROR/pageerror")

    def test_access_role_layout_breakpoint_topology(self):
        console_errors = []
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            page = browser.new_page(viewport={"width": 1181, "height": 900})
            page.on(
                "console",
                lambda message: console_errors.append(f"console:{message.text}")
                if message.type == "error" else None,
            )
            page.on("pageerror", lambda error: console_errors.append(f"pageerror:{error}"))

            page.goto(self.base + "/")
            page.wait_for_selector("#loginForm")
            page.fill("#username", self.username)
            page.fill("#password", self.password)
            page.click("#loginButton")
            page.wait_for_selector(".nav-item[data-workspace-view]", timeout=15000)
            page.wait_for_timeout(800)
            first_run = page.locator(".modal-backdrop:visible", has_text="首次使用")
            if first_run.count():
                first_run.locator("input").first.fill("冒烟测试诊所")
                first_run.locator("button:has-text('确定')").click()

            page.click('.nav-item[data-workspace-view="config"]')
            page.wait_for_selector('[data-cfg-tab="access"]', timeout=15000)
            access_tab = page.locator('[data-cfg-tab="access"]')
            if not access_tab.evaluate("el => el.classList.contains('active')"):
                access_tab.click()
            page.wait_for_selector(
                '[data-access-center-tab="roles"]', state="attached", timeout=15000,
            )
            role_tab = page.locator('[data-access-center-tab="roles"]')
            if not role_tab.evaluate("el => el.classList.contains('active')"):
                role_tab.click()
            page.wait_for_selector(".access-role-layout", state="visible", timeout=15000)

            def layout_geometry(width):
                page.set_viewport_size({"width": width, "height": 900})
                return page.evaluate("""() => {
                    const rect = selector => {
                        const value = document.querySelector(selector).getBoundingClientRect();
                        return {top: value.top, right: value.right,
                                bottom: value.bottom, left: value.left};
                    };
                    return {
                        roles: rect('.access-role-roles'),
                        main: rect('.access-role-main'),
                        impact: rect('.access-role-impact'),
                    };
                }""")

            three_columns = layout_geometry(1181)
            self.assertAlmostEqual(
                three_columns["roles"]["top"], three_columns["main"]["top"], delta=1,
            )
            self.assertAlmostEqual(
                three_columns["main"]["top"], three_columns["impact"]["top"], delta=1,
            )
            self.assertLess(three_columns["roles"]["left"], three_columns["main"]["left"])
            self.assertLess(three_columns["main"]["left"], three_columns["impact"]["left"])

            two_columns = layout_geometry(1180)
            self.assertAlmostEqual(
                two_columns["roles"]["top"], two_columns["main"]["top"], delta=1,
            )
            self.assertGreaterEqual(
                two_columns["impact"]["top"],
                max(two_columns["roles"]["bottom"], two_columns["main"]["bottom"]) - 1,
            )
            self.assertAlmostEqual(
                two_columns["impact"]["left"], two_columns["roles"]["left"], delta=1,
            )
            self.assertAlmostEqual(
                two_columns["impact"]["right"], two_columns["main"]["right"], delta=1,
            )

            one_column = layout_geometry(760)
            self.assertGreaterEqual(
                one_column["main"]["top"], one_column["roles"]["bottom"] - 1,
            )
            self.assertGreaterEqual(
                one_column["impact"]["top"], one_column["main"]["bottom"] - 1,
            )
            for region in ("main", "impact"):
                self.assertAlmostEqual(
                    one_column[region]["left"], one_column["roles"]["left"], delta=1,
                )
                self.assertAlmostEqual(
                    one_column[region]["right"], one_column["roles"]["right"], delta=1,
                )

            browser.close()
        self.assertEqual(console_errors, [], "角色断点几何检查不应产生 console ERROR/pageerror")

    def test_full_navigation(self):
        console_errors = []
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            page = browser.new_page()
            page.on("console", lambda m: console_errors.append(f"console:{m.text}") if m.type == "error" else None)
            page.on("pageerror", lambda e: console_errors.append(f"pageerror:{e}"))

            # 登录(require_login=True,与生产同构)
            page.goto(self.base + "/")
            page.wait_for_selector("#loginForm")
            page.fill("#username", self.username)
            page.fill("#password", self.password)
            page.click("#loginButton")
            page.wait_for_selector(".nav-item[data-workspace-view]", timeout=15000)

            # 全新库首启会弹"请填写诊所名称"(真实首启流程),照真实用户填掉;
            # 除此之外的任何可见弹窗不做豁免——挡住导航就该让测试红
            page.wait_for_timeout(1500)
            first_run = page.locator(".modal-backdrop:visible", has_text="首次使用")
            if first_run.count():
                first_run.locator("input").first.fill("冒烟测试诊所")
                first_run.locator("button:has-text('确定')").click()
                page.wait_for_timeout(500)

            # 顶导逐点(admin 全权限,10 个入口都应可见可点)
            nav_keys = page.eval_on_selector_all(
                ".nav-item[data-workspace-view]", "els => els.map(e => e.dataset.workspaceView)")
            self.assertGreaterEqual(len(nav_keys), 10, f"顶导入口不足10个: {nav_keys}")
            for key in nav_keys:
                with self.subTest(nav=key):
                    page.click(f'.nav-item[data-workspace-view="{key}"]')
                    self._unique_active(page, ".nav-item[data-workspace-view]", key, "data-workspace-view")
                    self._panel_settled(page, VIEW_PANEL[key])
                    # 视图内的二级导航逐点:只点当前可见面板里的(切走的视图会把旧按钮留在
                    # 隐藏面板中,全局选择器会点到不可见元素),纯导航按钮,不碰动作按钮
                    # 只遍历当前视图的二级导航。配置中心的“人员与权限”内部还有
                    # 独立三级页签；两级各有一个 active 是合法状态，不能混在同组计数。
                    sub_group = f"{VIEW_PANEL[key]} > .module-subtabs"
                    sub_sel = f"{sub_group} > .module-subtab:visible"
                    sub_count = page.locator(sub_sel).count()
                    if key in {"config", "warehouse"}:
                        self.assertGreater(sub_count, 0, f"{key} 应有可见直属二级页签")
                    for i in range(sub_count):
                        sub = page.locator(sub_sel).nth(i)
                        label = sub.inner_text()
                        with self.subTest(nav=key, subtab=label):
                            sub.click()
                            self._panel_settled(page, VIEW_PANEL[key])
                            # active 唯一性查本面板内(不能把 :visible 拼进 .active 选择器)
                            self._unique_active(page, f"{sub_group} > .module-subtab")

            # 患者管理 → 真点第一行开工作台
            page.click('.nav-item[data-workspace-view="patients"]')
            self._panel_settled(page, "#modulePanel")
            # 现行主动线:表格点行选中患者 → 右栏(#pcDetail)内嵌完整档案(克隆 workspace 布局)
            page.wait_for_selector(".pt-row[data-pc-pick]", timeout=15000)
            page.locator(".pt-row[data-pc-pick]").first.click()
            page.wait_for_selector("#pcDetail .workspace-tab[data-workspace-tab]", timeout=15000)

            # 13 个患者 tab 在内嵌档案里逐点
            scope = "#pcDetail"
            tab_keys = page.eval_on_selector_all(
                f"{scope} .workspace-tab[data-workspace-tab]", "els => els.map(e => e.dataset.workspaceTab)")
            self.assertEqual(len(tab_keys), 13, f"患者 tab 应为13个: {tab_keys}")
            # #763 几何回归(用户拍板=单行横向滚动):13个tab必须同一行,无孤儿掉行
            tops = page.eval_on_selector_all(
                f"{scope} .workspace-tab[data-workspace-tab]", "els => els.map(e => e.offsetTop)")
            self.assertEqual(len(set(tops)), 1, f"13个患者tab必须同一行(offsetTop应一致): {tops}")
            for key in tab_keys:
                with self.subTest(tab=key):
                    page.click(f'{scope} .workspace-tab[data-workspace-tab="{key}"]')
                    self._unique_active(page, f"{scope} .workspace-tab[data-workspace-tab]", key, "data-workspace-tab")
                    panel = page.locator(f'{scope} [data-workspace-panel="{key}"]')
                    self.assertTrue(panel.is_visible(), f"tab {key} 面板应可见")
                    hidden_others = page.eval_on_selector_all(
                        f"{scope} [data-workspace-panel]",
                        f"els => els.filter(e => e.dataset.workspacePanel !== '{key}' && !e.hidden).length")
                    self.assertEqual(hidden_others, 0, f"tab {key} 激活时其它面板必须隐藏")
                    self._panel_settled(page, f'{scope} [data-workspace-panel="{key}"]')

            browser.close()
        self.assertEqual(console_errors, [], "浏览器 console 不得有 ERROR/pageerror")

    def test_role_permissions_top_navigation_guard(self):
        console_errors = []
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            page = browser.new_page()
            page.on("console", lambda m: console_errors.append(f"console:{m.text}") if m.type == "error" else None)
            page.on("pageerror", lambda e: console_errors.append(f"pageerror:{e}"))

            page.goto(self.base + "/")
            page.wait_for_selector("#loginForm")
            page.fill("#username", self.username)
            page.fill("#password", self.password)
            page.click("#loginButton")
            page.wait_for_selector(".nav-item[data-workspace-view]", timeout=15000)
            page.wait_for_timeout(800)
            first_run = page.locator(".modal-backdrop:visible", has_text="首次使用")
            if first_run.count():
                first_run.locator("input").first.fill("冒烟测试诊所")
                first_run.locator("button:has-text('确定')").click()

            permission_selector = 'input[data-rp-permission-key]'

            def open_role_permissions():
                page.click('.nav-item[data-workspace-view="config"]')
                page.wait_for_selector('[data-cfg-tab="access"]', timeout=15000)
                access_tab = page.locator('[data-cfg-tab="access"]')
                if not access_tab.evaluate("el => el.classList.contains('active')"):
                    access_tab.click()
                page.wait_for_selector(
                    '[data-access-center-tab="roles"]', state="attached", timeout=15000,
                )
                role_tab = page.locator('[data-access-center-tab="roles"]')
                if role_tab.is_visible() and not role_tab.evaluate("el => el.classList.contains('active')"):
                    role_tab.click()
                page.wait_for_function("""selector => {
                    const box = document.getElementById('cfgRolePerms');
                    return Boolean(document.querySelector(selector))
                        || Boolean(box && /\u5931\u8d25|\u65e0\u6743/.test(box.textContent));
                }""", arg=permission_selector, timeout=15000)
                self.assertGreater(
                    page.locator(permission_selector).count(),
                    0,
                    "V3 权限树未渲染："
                    + page.locator("#cfgRolePerms").inner_text()
                    + f"; console={console_errors}",
                )

            def navigation_snapshot():
                return page.evaluate("""() => {
                    const active = document.querySelector('.nav-item.active[data-workspace-view]');
                    const checkbox = document.querySelector('input[data-rp-permission-key]');
                    const role = document.querySelector('[data-rp-select-role][aria-pressed="true"]');
                    const panel = document.getElementById('rpV3Confirm');
                    const passwordStep = document.getElementById('rpPasswordStep');
                    const reasonStep = document.getElementById('rpReasonStep');
                    const password = document.getElementById('rpCurrentPassword');
                    const reason = document.getElementById('rpReason');
                    return {
                        hash: location.hash,
                        active: active ? active.dataset.workspaceView : null,
                        moduleHtml: document.getElementById('modulePanel').innerHTML,
                        moduleHidden: document.getElementById('modulePanel').hidden,
                        patientPageHidden: document.getElementById('patientWorkspacePage').hidden,
                        patientListHidden: document.getElementById('patientWorkspace').hidden,
                        todayHidden: document.getElementById('todayWorkPanel').hidden,
                        permissionKey: checkbox ? checkbox.dataset.rpPermissionKey : null,
                        checkboxChecked: checkbox ? checkbox.checked : null,
                        roleKey: role ? role.dataset.rpSelectRole : null,
                        confirmHidden: panel ? panel.hidden : null,
                        passwordStepHidden: passwordStep ? passwordStep.hidden : null,
                        reasonStepHidden: reasonStep ? reasonStep.hidden : null,
                        passwordLength: password ? password.value.length : 0,
                        reasonValue: reason ? reason.value : '',
                        roleMessage: document.getElementById('rpMsg')?.textContent || '',
                    };
                }""")

            def expand_first_object():
                # 三级权限树默认仅第一模块展开、对象折叠；点击叶子前先展开首个对象。
                first_object = page.locator(
                    '[data-rp-section-toggle][data-rp-level="object"]'
                ).first
                if first_object.get_attribute("aria-expanded") != "true":
                    first_object.click()

            open_role_permissions()
            expand_first_object()
            checkbox = page.locator(permission_selector).first
            original_checked = checkbox.is_checked()
            checkbox.set_checked(not original_checked)
            patient_identity = page.evaluate("""async () => {
                const response = await fetch('/api/patients?pagesize=1');
                const payload = await response.json();
                return payload.list && payload.list[0] && payload.list[0].patient_identity;
            }""")
            self.assertTrue(patient_identity, "合成库应至少有一位患者")
            patient_before = navigation_snapshot()

            page.evaluate("""identity => {
                window.__patientOpenOutcome = 'pending';
                Promise.resolve(openPatientWorkspace(identity, 'profile')).then(
                    result => { window.__patientOpenOutcome = result; },
                );
            }""", patient_identity)
            leave_modal = page.locator(".modal-backdrop:visible", has_text="离开角色与权限")
            leave_modal.wait_for(timeout=5000)
            self.assertEqual(navigation_snapshot(), patient_before)
            leave_modal.locator('[data-rp-leave="cancel"]').click()
            page.wait_for_function("window.__patientOpenOutcome === false", timeout=5000)
            self.assertEqual(navigation_snapshot(), patient_before)

            page.evaluate("""identity => {
                window.__patientOpenOutcome = 'pending';
                Promise.resolve(openPatientWorkspace(identity, 'profile')).then(
                    result => { window.__patientOpenOutcome = result; },
                );
            }""", patient_identity)
            leave_modal = page.locator(".modal-backdrop:visible", has_text="离开角色与权限")
            leave_modal.wait_for(timeout=5000)
            self.assertEqual(navigation_snapshot(), patient_before)
            leave_modal.locator('[data-rp-leave="confirm"]').click()
            page.wait_for_function("""window.__patientOpenOutcome === true
                && location.hash.startsWith('#patient/')
                && !document.getElementById('patientWorkspacePage').hidden""", timeout=10000)
            self._unique_active(page, ".nav-item[data-workspace-view]", "config", "data-workspace-view")
            self.assertTrue(page.locator("#modulePanel").is_hidden())
            self.assertTrue(page.locator("#patientWorkspacePage").is_visible())
            self.assertEqual(checkbox.is_checked(), original_checked)

            # 浏览器 Back 已先移动历史索引；取消后再次点应用内返回不得重复 back 越界或卡死。
            page.eval_on_selector(
                permission_selector,
                """el => {
                    el.checked = !el.checked;
                    el.dispatchEvent(new Event('change', {bubbles: true}));
                }""",
            )
            patient_hash = page.evaluate("location.hash")
            page.go_back()
            leave_modal = page.locator(".modal-backdrop:visible", has_text="离开角色与权限")
            leave_modal.wait_for(timeout=5000)
            self.assertEqual(page.evaluate("location.hash"), patient_hash)
            self.assertTrue(page.locator("#patientWorkspacePage").is_visible())
            self.assertTrue(page.locator("#modulePanel").is_hidden())
            leave_modal.locator('[data-rp-leave="cancel"]').click()
            page.wait_for_timeout(100)
            self.assertEqual(page.evaluate("location.hash"), patient_hash)
            self.assertTrue(page.locator("#patientWorkspacePage").is_visible())

            page.evaluate("""() => {
                window.__patientCloseOutcome = 'pending';
                Promise.resolve(closePatientWorkspace()).then(
                    result => { window.__patientCloseOutcome = result; },
                );
            }""")
            leave_modal = page.locator(".modal-backdrop:visible", has_text="离开角色与权限")
            leave_modal.wait_for(timeout=5000)
            self.assertEqual(page.evaluate("location.hash"), patient_hash)
            self.assertTrue(page.locator("#patientWorkspacePage").is_visible())
            leave_modal.locator('[data-rp-leave="confirm"]').click()
            page.wait_for_function("""window.__patientCloseOutcome === true
                && location.hash === ''
                && document.getElementById('patientWorkspacePage').hidden""", timeout=10000)
            self._unique_active(page, ".nav-item[data-workspace-view]", "config", "data-workspace-view")
            self.assertTrue(page.locator("#modulePanel").is_visible())

            # 嵌套患者 P1→P2：Back 到 P1 后取消，随后关闭 P2 必须仍回 P1，不能丢父 hash 掉到根页。
            open_role_permissions()
            page.evaluate("""identity => {
                window.__nestedOpenOutcome = 'pending';
                Promise.resolve(openPatientWorkspace(identity, 'profile')).then(
                    result => { window.__nestedOpenOutcome = result; },
                );
            }""", patient_identity)
            page.wait_for_function("""window.__nestedOpenOutcome === true
                && location.hash.endsWith('/profile')
                && !document.getElementById('patientWorkspacePage').hidden""", timeout=10000)
            parent_patient_hash = page.evaluate("location.hash")
            page.evaluate("""identity => {
                window.__nestedOpenOutcome = 'pending';
                Promise.resolve(openPatientWorkspace(identity, 'billing')).then(
                    result => { window.__nestedOpenOutcome = result; },
                );
            }""", patient_identity)
            page.wait_for_function("""window.__nestedOpenOutcome === true
                && location.hash.endsWith('/billing')""", timeout=10000)
            child_patient_hash = page.evaluate("location.hash")
            page.eval_on_selector(
                permission_selector,
                """el => {
                    el.checked = !el.checked;
                    el.dispatchEvent(new Event('change', {bubbles: true}));
                }""",
            )
            page.go_back()
            leave_modal = page.locator(".modal-backdrop:visible", has_text="离开角色与权限")
            leave_modal.wait_for(timeout=5000)
            self.assertEqual(page.evaluate("location.hash"), child_patient_hash)
            leave_modal.locator('[data-rp-leave="cancel"]').click()
            page.wait_for_timeout(100)
            self.assertEqual(page.evaluate("location.hash"), child_patient_hash)

            page.evaluate("""() => {
                window.__nestedCloseOutcome = 'pending';
                Promise.resolve(closePatientWorkspace()).then(
                    result => { window.__nestedCloseOutcome = result; },
                );
            }""")
            leave_modal = page.locator(".modal-backdrop:visible", has_text="离开角色与权限")
            leave_modal.wait_for(timeout=5000)
            leave_modal.locator('[data-rp-leave="confirm"]').click()
            page.wait_for_function(
                "expected => window.__nestedCloseOutcome === true && location.hash === expected",
                arg=parent_patient_hash,
                timeout=10000,
            )
            self.assertTrue(page.locator("#patientWorkspacePage").is_visible())
            self.assertTrue(page.locator("#modulePanel").is_hidden())

            page.evaluate("""() => {
                window.__nestedParentCloseOutcome = 'pending';
                Promise.resolve(closePatientWorkspace()).then(
                    result => { window.__nestedParentCloseOutcome = result; },
                );
            }""")
            page.wait_for_function("""window.__nestedParentCloseOutcome === true
                && location.hash === ''
                && document.getElementById('patientWorkspacePage').hidden""", timeout=10000)

            open_role_permissions()
            expand_first_object()
            checkbox = page.locator(permission_selector).first
            original_checked = checkbox.is_checked()
            checkbox.set_checked(not original_checked)
            config_dom = page.locator("#modulePanel").inner_html()
            original_hash = page.evaluate("location.hash")

            page.click('.nav-item[data-workspace-view="warehouse"]')
            leave_modal = page.locator(".modal-backdrop:visible", has_text="离开角色与权限")
            leave_modal.wait_for(timeout=5000)
            self._unique_active(page, ".nav-item[data-workspace-view]", "config", "data-workspace-view")
            self.assertEqual(page.locator("#modulePanel").inner_html(), config_dom)
            self.assertEqual(page.evaluate("location.hash"), original_hash)
            leave_modal.locator('[data-rp-leave="cancel"]').click()
            page.wait_for_timeout(100)
            self._unique_active(page, ".nav-item[data-workspace-view]", "config", "data-workspace-view")
            self.assertEqual(page.locator("#modulePanel").inner_html(), config_dom)
            self.assertEqual(checkbox.is_checked(), not original_checked)

            page.click('.nav-item[data-workspace-view="warehouse"]')
            leave_modal = page.locator(".modal-backdrop:visible", has_text="离开角色与权限")
            leave_modal.wait_for(timeout=5000)
            leave_modal.locator('[data-rp-leave="confirm"]').click()
            page.wait_for_function(
                "document.querySelector('.nav-item.active[data-workspace-view]')?.dataset.workspaceView === 'warehouse'",
                timeout=10000,
            )
            self._panel_settled(page, "#modulePanel")

            open_role_permissions()
            def wait_for_pending(pending, label):
                deadline = time.time() + 5
                while not pending and time.time() < deadline:
                    page.wait_for_timeout(50)
                self.assertTrue(pending, label)

            def enable_next_permission():
                candidate = page.locator(f"{permission_selector}:not(:checked)").first
                self.assertGreater(candidate.count(), 0, "应至少有一项未启用权限供冒烟测试")
                permission_key = candidate.get_attribute("data-rp-permission-key")
                page.evaluate("""key => {
                    const input = Array.from(
                        document.querySelectorAll('input[data-rp-permission-key]')
                    ).find(item => item.dataset.rpPermissionKey === key);
                    input.checked = true;
                    input.dispatchEvent(new Event('change', {bubbles: true}));
                }""", permission_key)
                page.wait_for_function("""key => Array.from(
                    document.querySelectorAll('input[data-rp-permission-key]')
                ).some(input => input.dataset.rpPermissionKey === key && input.checked)""",
                    arg=permission_key, timeout=5000)
                return permission_key

            def assert_password_stable():
                self.assertTrue(page.evaluate("""() => {
                    const current = document.getElementById('rpCurrentPassword');
                    return current === window.__rpPasswordNode
                        && current.isConnected
                        && current.value.length > 0
                        && !document.getElementById('rpV3Confirm').hidden
                        && !document.getElementById('rpPasswordStep').hidden
                        && document.getElementById('rpReasonStep').hidden;
                }"""), "密码只能保留在当前稳定输入节点")

            reason_text = "冒烟测试保留修改原因"

            def assert_reason_stable():
                self.assertTrue(page.evaluate("""expected => {
                    const current = document.getElementById('rpReason');
                    return current === window.__rpReasonNode
                        && current.isConnected
                        && current.value === expected
                        && !document.getElementById('rpV3Confirm').hidden
                        && document.getElementById('rpPasswordStep').hidden
                        && !document.getElementById('rpReasonStep').hidden;
                }""", reason_text), "原因节点、阶段和内容应跨重绘保留")

            def set_background_search(value):
                page.evaluate("value => window.setRolePermissionSearch(value)", value)

            def set_background_view(view):
                self.assertTrue(page.evaluate(
                    "view => window.setRolePermissionView(view)", view,
                ))

            def toggle_background_section():
                page.evaluate("""() => {
                    const button = document.querySelector('[data-rp-section-toggle]');
                    if (!button) throw new Error('permission section unavailable');
                    window.toggleRoleTreeSection(
                        button.dataset.rpLevel,
                        button.dataset.rpModule,
                        button.dataset.rpObject || ''
                    );
                }""")

            role_baseline = page.eval_on_selector_all(
                f"{permission_selector}:checked",
                "els => els.map(item => item.dataset.rpPermissionKey).sort()",
            )
            affected_staff_count = page.evaluate("""() => {
                const selected = document.querySelector(
                    '[data-rp-select-role][aria-pressed="true"]'
                );
                const match = selected && selected.textContent.match(/已分配\s*(\d+)\s*人/);
                return match ? Number(match[1]) : null;
            }""")
            self.assertIsInstance(affected_staff_count, int)

            first_changed_key = enable_next_permission()

            # 打开 modal 前用真实点击验证背景搜索、视图和折叠交互。
            search = page.locator('[data-rp-search]')
            search.click()
            prefix = first_changed_key[:4]
            for index, character in enumerate(prefix, start=1):
                page.keyboard.insert_text(character)
                expected = prefix[:index]
                page.wait_for_function("""expected => {
                    const input = document.querySelector('[data-rp-search]');
                    return input && input.value === expected
                        && document.activeElement === input
                        && input.selectionStart === expected.length
                        && input.selectionEnd === expected.length;
                }""", arg=expected, timeout=5000)

            # composition 期间不替换节点；compositionend 后再重绘并精确命中权限键。
            page.evaluate("""key => {
                const input = document.querySelector('[data-rp-search]');
                window.__rpComposingSearchNode = input;
                input.dispatchEvent(new CompositionEvent('compositionstart', {
                    bubbles: true, data: key,
                }));
                input.value = key;
                input.setSelectionRange(key.length, key.length);
                input.dispatchEvent(new InputEvent('input', {
                    bubbles: true,
                    data: key,
                    inputType: 'insertCompositionText',
                    isComposing: true,
                }));
            }""", first_changed_key)
            self.assertTrue(page.evaluate("""() =>
                document.querySelector('[data-rp-search]') === window.__rpComposingSearchNode
            """), "composition 期间不应替换搜索节点")
            page.evaluate("""key => {
                const input = document.querySelector('[data-rp-search]');
                input.dispatchEvent(new CompositionEvent('compositionend', {
                    bubbles: true, data: key,
                }));
                input.dispatchEvent(new InputEvent('input', {
                    bubbles: true,
                    data: key,
                    inputType: 'insertText',
                    isComposing: false,
                }));
            }""", first_changed_key)
            page.wait_for_function("""key => {
                const input = document.querySelector('[data-rp-search]');
                const matches = Array.from(
                    document.querySelectorAll('input[data-rp-permission-key]')
                );
                return input && input.value === key
                    && document.activeElement === input
                    && input.selectionStart === key.length
                    && matches.length === 1
                    && matches[0].dataset.rpPermissionKey === key;
            }""", arg=first_changed_key, timeout=5000)

            page.fill('[data-rp-search]', '')
            page.click('[data-rp-view="sensitive"]')
            self.assertEqual(
                page.locator('[data-rp-view="sensitive"]').get_attribute("aria-pressed"),
                "true",
            )
            page.click('[data-rp-view="all"]')
            section_toggle = page.locator('[data-rp-section-toggle]').first
            expanded_before = section_toggle.get_attribute("aria-expanded")
            section_toggle.click()
            self.assertNotEqual(
                page.locator('[data-rp-section-toggle]').first.get_attribute("aria-expanded"),
                expanded_before,
            )
            page.locator('[data-rp-section-toggle]').first.click()
            self.assertEqual(
                page.locator('[data-rp-section-toggle]').first.get_attribute("aria-expanded"),
                expanded_before,
            )

            page.click('[data-rp-save-selected]')
            save_modal = page.locator('#rpV3Confirm.modal-backdrop:visible')
            save_modal.wait_for(timeout=5000)
            dialog = save_modal.locator('[role="dialog"][aria-modal="true"]')
            self.assertEqual(dialog.count(), 1)
            self.assertTrue(dialog.is_visible())
            self.assertEqual(dialog.get_attribute("aria-hidden"), "false")
            modal_state = page.evaluate("""() => {
                const background = document.querySelector('[data-rp-modal-background]');
                const modal = document.getElementById('rpV3Confirm');
                const cancel = modal.querySelector('[data-rp-cancel-save]');
                return {
                    siblings: background.parentElement === modal.parentElement,
                    modalOutsideBackground: !background.contains(modal),
                    inert: background.inert && background.hasAttribute('inert'),
                    ariaHidden: background.getAttribute('aria-hidden'),
                    cancelVisible: Boolean(cancel.offsetWidth || cancel.offsetHeight),
                };
            }""")
            self.assertTrue(modal_state["siblings"])
            self.assertTrue(modal_state["modalOutsideBackground"])
            self.assertTrue(modal_state["inert"])
            self.assertEqual(modal_state["ariaHidden"], "true")
            self.assertTrue(modal_state["cancelVisible"])
            page.wait_for_function(
                "document.activeElement?.id === 'rpCurrentPassword'", timeout=1000,
            )

            page.locator('[data-rp-cancel-save]').focus()
            page.keyboard.press("Tab")
            self.assertEqual(page.evaluate("document.activeElement.id"), "rpCurrentPassword")
            page.keyboard.press("Shift+Tab")
            self.assertTrue(page.evaluate(
                "document.activeElement.hasAttribute('data-rp-cancel-save')"
            ))
            page.keyboard.press("Escape")
            save_modal.wait_for(state="hidden", timeout=5000)
            self.assertTrue(page.evaluate("""() => {
                const background = document.querySelector('[data-rp-modal-background]');
                return document.activeElement?.hasAttribute('data-rp-save-selected')
                    && !background.inert
                    && !background.hasAttribute('inert')
                    && !background.hasAttribute('aria-hidden');
            }"""))

            page.click('[data-rp-save-selected]')
            save_modal.wait_for(timeout=5000)
            page.wait_for_function(
                "document.activeElement?.id === 'rpCurrentPassword'", timeout=1000,
            )
            # 保存 modal 上再叠离开确认时，只有顶层接管焦点与键盘。
            page.evaluate("""() => {
                window.__rpPasswordLeaveOutcome = 'pending';
                Promise.resolve(requestLeaveRolePermissions()).then(result => {
                    window.__rpPasswordLeaveOutcome = result;
                });
            }""")
            stacked_leave = page.locator(
                ".modal-backdrop:visible", has_text="离开角色与权限",
            )
            stacked_leave.wait_for(timeout=5000)
            page.wait_for_function(
                "document.activeElement?.dataset.rpLeave === 'cancel'", timeout=1000,
            )
            stacked_state = page.evaluate("""() => {
                const dialog = document.getElementById('rpSaveDialog');
                return {
                    inert: dialog.inert && dialog.hasAttribute('inert'),
                    ariaHidden: dialog.getAttribute('aria-hidden'),
                };
            }""")
            self.assertTrue(stacked_state["inert"])
            self.assertEqual(stacked_state["ariaHidden"], "true")
            page.keyboard.press("Shift+Tab")
            self.assertEqual(
                page.evaluate("document.activeElement?.dataset.rpLeave"), "confirm",
            )
            page.keyboard.press("Tab")
            self.assertEqual(
                page.evaluate("document.activeElement?.dataset.rpLeave"), "cancel",
            )
            page.keyboard.press("Escape")
            page.wait_for_function("""() => {
                const dialog = document.getElementById('rpSaveDialog');
                return window.__rpPasswordLeaveOutcome === false
                    && document.getElementById('rpV3Confirm')?.hidden === false
                    && !dialog.inert
                    && !dialog.hasAttribute('inert')
                    && dialog.getAttribute('aria-hidden') === 'false'
                    && document.activeElement?.id === 'rpCurrentPassword';
            }""", timeout=5000)
            stacked_leave.wait_for(state="hidden", timeout=5000)
            foreground = page.evaluate("""() => {
                const search = document.querySelector('[data-rp-search]');
                const modal = document.getElementById('rpV3Confirm');
                const rect = search.getBoundingClientRect();
                const hit = document.elementFromPoint(
                    rect.left + rect.width / 2,
                    rect.top + rect.height / 2
                );
                return {
                    searchVisible: rect.width > 0 && rect.height > 0,
                    hitSearch: hit === search || search.contains(hit),
                    hitModal: hit === modal || modal.contains(hit),
                };
            }""")
            self.assertTrue(foreground["searchVisible"])
            self.assertFalse(foreground["hitSearch"])
            self.assertTrue(foreground["hitModal"])

            page.fill("#rpCurrentPassword", self.password)
            page.evaluate("window.__rpPasswordNode = document.getElementById('rpCurrentPassword')")
            assert_password_stable()

            # 验密 4xx：请求挂起后再重绘，错误必须写入当前消息节点。
            pending_reauth_4xx = []

            def hold_reauth_4xx(route, request):
                pending_reauth_4xx.append(route)

            page.route("**/api/auth/reauth", hold_reauth_4xx)
            page.click("#rpPasswordStep button:has-text('确认密码')")
            wait_for_pending(pending_reauth_4xx, "验密请求应进入挂起状态")
            set_background_view("sensitive")
            assert_password_stable()
            pending_reauth_4xx[0].fulfill(
                status=401, content_type="application/json", body='{"detail":"synthetic"}',
            )
            page.wait_for_function(
                "document.getElementById('rpMsg')?.textContent.includes('密码确认失败：401')",
                timeout=5000,
            )
            page.unroute("**/api/auth/reauth", hold_reauth_4xx)
            assert_password_stable()

            # 验密断网：仍留在密码阶段，不得显示假成功。
            pending_reauth_network = []

            def hold_reauth_network(route, request):
                pending_reauth_network.append(route)

            page.route("**/api/auth/reauth", hold_reauth_network)
            page.click("#rpPasswordStep button:has-text('确认密码')")
            wait_for_pending(pending_reauth_network, "第二次验密请求应进入挂起状态")
            set_background_search(first_changed_key)
            assert_password_stable()
            pending_reauth_network[0].abort("connectionfailed")
            page.wait_for_function(
                "document.getElementById('rpMsg')?.textContent.includes('密码确认失败（网络错误）')",
                timeout=5000,
            )
            page.unroute("**/api/auth/reauth", hold_reauth_network)
            assert_password_stable()

            # 真实验密成功后，原因输入在勾选/搜索/视图/折叠重绘中保持。
            set_background_search("")
            set_background_view("all")
            page.click("#rpPasswordStep button:has-text('确认密码')")
            page.wait_for_selector("#rpReasonStep:not([hidden])", timeout=10000)
            page.fill("#rpReason", reason_text)
            page.evaluate("window.__rpReasonNode = document.getElementById('rpReason')")
            page.evaluate("""() => {
                window.__rpReasonLeaveOutcome = 'pending';
                Promise.resolve(requestLeaveRolePermissions()).then(result => {
                    window.__rpReasonLeaveOutcome = result;
                });
            }""")
            stacked_leave.wait_for(timeout=5000)
            stacked_leave.locator('[data-rp-leave="cancel"]').click()
            page.wait_for_function("""() =>
                window.__rpReasonLeaveOutcome === false
                    && document.activeElement?.id === 'rpReason'
                    && document.getElementById('rpV3Confirm')?.hidden === false
            """, timeout=5000)
            enable_next_permission()
            assert_reason_stable()
            set_background_search(first_changed_key[:5])
            assert_reason_stable()
            set_background_search("")
            set_background_view("sensitive")
            assert_reason_stable()
            set_background_view("all")
            toggle_background_section()
            assert_reason_stable()
            toggle_background_section()
            assert_reason_stable()

            # 过期验密必须先显示密码阶段，再在真实浏览器中聚焦密码框。
            pending_put_reauth = []

            def hold_put_reauth(route, request):
                if request.method == "PUT":
                    pending_put_reauth.append(route)
                else:
                    route.continue_()

            page.route("**/api/role-permissions", hold_put_reauth)
            page.click("#rpReasonStep button:has-text('确认保存')")
            wait_for_pending(pending_put_reauth, "过期验密 PUT 应进入挂起状态")
            pending_put_reauth[0].fulfill(
                status=403,
                content_type="application/json",
                body='{"detail":"recent_reauthentication_required"}',
            )
            page.wait_for_function("""() => {
                const password = document.getElementById('rpCurrentPassword');
                return document.getElementById('rpMsg')?.textContent.includes('当前密码确认已过期')
                    && !document.getElementById('rpPasswordStep').hidden
                    && document.getElementById('rpReasonStep').hidden
                    && document.activeElement === password;
            }""", timeout=5000)
            page.unroute("**/api/role-permissions", hold_put_reauth)
            page.fill("#rpCurrentPassword", self.password)
            page.click("#rpPasswordStep button:has-text('确认密码')")
            page.wait_for_selector("#rpReasonStep:not([hidden])", timeout=10000)
            page.fill("#rpReason", reason_text)
            page.evaluate("window.__rpReasonNode = document.getElementById('rpReason')")
            assert_reason_stable()

            # PUT 4xx 与断网都必须保留草稿、原因和可重试阶段。
            pending_put_4xx = []

            def hold_put_4xx(route, request):
                if request.method == "PUT":
                    pending_put_4xx.append(route)
                else:
                    route.continue_()

            page.route("**/api/role-permissions", hold_put_4xx)
            page.click("#rpReasonStep button:has-text('确认保存')")
            wait_for_pending(pending_put_4xx, "PUT 4xx 用例应进入挂起状态")
            enable_next_permission()
            pending_put_4xx[0].fulfill(
                status=409, content_type="application/json", body='{"detail":"synthetic"}',
            )
            page.wait_for_function(
                "document.getElementById('rpMsg')?.textContent.includes('保存失败：409')",
                timeout=5000,
            )
            page.unroute("**/api/role-permissions", hold_put_4xx)
            assert_reason_stable()
            self.assertEqual(page.locator('[data-rp-region="save-status"]').inner_text(), "未保存")

            pending_put_network = []

            def hold_put_network(route, request):
                if request.method == "PUT":
                    pending_put_network.append(route)
                else:
                    route.continue_()

            page.route("**/api/role-permissions", hold_put_network)
            page.click("#rpReasonStep button:has-text('确认保存')")
            wait_for_pending(pending_put_network, "PUT 断网用例应进入挂起状态")
            enable_next_permission()
            pending_put_network[0].abort("connectionfailed")
            page.wait_for_function(
                "document.getElementById('rpMsg')?.textContent.includes('保存失败（网络错误）')",
                timeout=5000,
            )
            page.unroute("**/api/role-permissions", hold_put_network)
            assert_reason_stable()
            self.assertEqual(page.locator('[data-rp-region="save-status"]').inner_text(), "未保存")

            pending_put = []

            def hold_role_permission_put(route, request):
                if request.method == "PUT":
                    pending_put.append((route, request))
                else:
                    route.continue_()

            page.route("**/api/role-permissions", hold_role_permission_put)
            page.click("#rpReasonStep button:has-text('确认保存')")
            wait_for_pending(pending_put, "角色权限 PUT 应进入挂起状态")
            self.assertTrue(page.locator('[data-rp-cancel-save]').is_disabled())
            page.keyboard.press("Escape")
            self.assertTrue(save_modal.is_visible())
            enable_next_permission()

            saving_before = navigation_snapshot()
            page.evaluate("""identity => {
                window.__patientOpenOutcome = 'pending';
                Promise.resolve(openPatientWorkspace(identity, 'profile')).then(
                    result => { window.__patientOpenOutcome = result; },
                );
            }""", patient_identity)
            page.wait_for_function("window.__patientOpenOutcome === false", timeout=5000)
            self.assertEqual(navigation_snapshot(), saving_before)
            self.assertTrue(save_modal.is_visible())
            self.assertEqual(
                page.locator(".modal-backdrop:visible", has_text="离开角色与权限").count(),
                0,
            )

            page.evaluate("""() => {
                document.querySelector(
                    '.nav-item[data-workspace-view="warehouse"]'
                ).click();
            }""")
            page.wait_for_timeout(150)
            self._unique_active(page, ".nav-item[data-workspace-view]", "config", "data-workspace-view")
            self.assertTrue(save_modal.is_visible())
            self.assertEqual(
                page.locator(".modal-backdrop:visible", has_text="离开角色与权限").count(),
                0,
            )
            self.assertEqual(navigation_snapshot(), saving_before)

            save_route, save_request = pending_put[0]
            request_payload = save_request.post_data_json
            self.assertEqual(
                set(request_payload),
                {"role_key", "permissions", "expected_permissions", "reason"},
            )
            self.assertTrue(request_payload["reason"].strip())
            self.assertEqual(request_payload["expected_permissions"], role_baseline)
            submitted_permissions = set(request_payload["permissions"])
            baseline_permissions = set(role_baseline)
            added = sorted(submitted_permissions - baseline_permissions)
            removed = sorted(baseline_permissions - submitted_permissions)
            save_route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps({
                    "ok": True,
                    "role_key": request_payload["role_key"],
                    "old": sorted(baseline_permissions),
                    "new": sorted(submitted_permissions),
                    "added": added,
                    "removed": removed,
                    "affected_staff_count": affected_staff_count,
                    "changed": bool(added or removed),
                }, ensure_ascii=False),
            )
            page.wait_for_function(
                "document.getElementById('rpMsg')?.textContent.includes('本次提交已保存，另有未保存修改')",
                timeout=10000,
            )
            self.assertTrue(save_modal.is_hidden())
            self.assertEqual(page.locator('[data-rp-region="save-status"]').inner_text(), "未保存")
            page.wait_for_function("""() => {
                const preview = document.querySelector('[data-rp-save-selected]');
                return preview && !preview.disabled && document.activeElement === preview;
            }""", timeout=5000)
            page.unroute("**/api/role-permissions", hold_role_permission_put)

            # 把等待期草稿完整保存后，disabled 预览不可接焦点，回退到当前岗位。
            page.click('[data-rp-save-selected]')
            page.fill("#rpCurrentPassword", self.password)
            page.click("#rpPasswordStep button:has-text('确认密码')")
            page.wait_for_selector("#rpReasonStep:not([hidden])", timeout=10000)
            page.fill("#rpReason", "冒烟测试干净保存后的稳定焦点")
            pending_clean_put = []

            def hold_clean_put(route, request):
                if request.method == "PUT":
                    pending_clean_put.append((route, request))
                else:
                    route.continue_()

            page.route("**/api/role-permissions", hold_clean_put)
            page.click("#rpReasonStep button:has-text('确认保存')")
            wait_for_pending(pending_clean_put, "干净保存 PUT 应进入挂起状态")
            clean_route, clean_request = pending_clean_put[0]
            clean_payload = clean_request.post_data_json
            clean_permissions = set(clean_payload["permissions"])
            clean_added = sorted(clean_permissions - submitted_permissions)
            clean_removed = sorted(submitted_permissions - clean_permissions)
            clean_route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps({
                    "ok": True,
                    "role_key": clean_payload["role_key"],
                    "old": sorted(submitted_permissions),
                    "new": sorted(clean_permissions),
                    "added": clean_added,
                    "removed": clean_removed,
                    "affected_staff_count": affected_staff_count,
                    "changed": bool(clean_added or clean_removed),
                }, ensure_ascii=False),
            )
            page.wait_for_function("""() => {
                const preview = document.querySelector('[data-rp-save-selected]');
                const selectedRole = document.querySelector(
                    '[data-rp-select-role][aria-pressed="true"]'
                );
                return document.getElementById('rpV3Confirm').hidden
                    && preview?.disabled
                    && document.activeElement === selectedRole;
            }""", timeout=10000)
            page.unroute("**/api/role-permissions", hold_clean_put)

            # 确认离开同时清理顶层与底层，不再恢复底层焦点陷阱。
            enable_next_permission()
            page.click('[data-rp-save-selected]')
            save_modal.wait_for(timeout=5000)
            page.evaluate("""() => {
                window.__rpConfirmLeaveOutcome = 'pending';
                Promise.resolve(requestLeaveRolePermissions()).then(result => {
                    window.__rpConfirmLeaveOutcome = result;
                });
            }""")
            stacked_leave.wait_for(timeout=5000)
            stacked_leave.locator('[data-rp-leave="confirm"]').click()
            page.wait_for_function("""() => {
                const panel = document.getElementById('rpV3Confirm');
                const dialog = document.getElementById('rpSaveDialog');
                const background = document.querySelector('[data-rp-modal-background]');
                const diff = rolePermissionDiff();
                return window.__rpConfirmLeaveOutcome === true
                    && panel.hidden
                    && dialog.hidden
                    && !dialog.inert
                    && dialog.getAttribute('aria-hidden') === 'true'
                    && !background.inert
                    && !background.hasAttribute('inert')
                    && !background.hasAttribute('aria-hidden')
                    && !dialog.contains(document.activeElement)
                    && diff.added.length === 0
                    && diff.removed.length === 0;
            }""", timeout=5000)
            stacked_leave.wait_for(state="hidden", timeout=5000)
            browser.close()

        expected_console_errors = {
            "console:Failed to load resource: net::ERR_CONNECTION_FAILED": 2,
            "console:Failed to load resource: the server responded with a status of 401 (Unauthorized)": 1,
            "console:Failed to load resource: the server responded with a status of 403 (Forbidden)": 1,
            "console:Failed to load resource: the server responded with a status of 409 (Conflict)": 1,
        }
        for message, count in expected_console_errors.items():
            self.assertEqual(
                console_errors.count(message), count,
                f"主动模拟失败应产生精确 console 记录: {console_errors}",
            )
        self.assertEqual(
            [item for item in console_errors if item not in expected_console_errors],
            [],
            "除精确模拟的 401、409 和两次断网外，console 不得有 ERROR/pageerror",
        )


if __name__ == "__main__":
    unittest.main()
