from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class PearlGlassHomeUiTests(unittest.TestCase):
    def setUp(self):
        self.index = (ROOT / "local_app/static/index.html").read_text(encoding="utf-8")
        self.styles = (ROOT / "local_app/static/styles.css").read_text(encoding="utf-8")
        self.today = (ROOT / "local_app/static/today_work.js").read_text(encoding="utf-8")

    def test_pearl_theme_shell_and_tooth_asset_are_wired(self):
        motif = ROOT / "local_app/static/tooth-motif.svg"
        self.assertIn('<body class="theme-pearl">', self.index)
        self.assertIn('class="brand-mark"', self.index)
        self.assertIn('/tooth-motif.svg', self.index)
        self.assertTrue(motif.is_file())
        svg = motif.read_text(encoding="utf-8")
        self.assertIn('viewBox="0 0 120 140"', svg)
        self.assertNotIn("https://", svg)
        self.assertNotIn("<image", svg)
        self.assertNotIn("href", svg)
        for token in [
            "--pearl-bg",
            "--glass-surface",
            "--glass-border",
            "--glass-shadow",
            "--pearl-motion",
        ]:
            with self.subTest(token=token):
                self.assertIn(token, self.styles)
        self.assertIn("body.theme-pearl .side-nav", self.styles)
        self.assertIn("body.theme-pearl .topbar", self.styles)

    def test_today_home_uses_spatial_overview_without_breaking_callbacks(self):
        for marker in [
            'class="today-overview-grid"',
            'class="today-date-copy"',
            'class="today-kpi-strip"',
            '/tooth-motif.svg',
        ]:
            with self.subTest(marker=marker):
                self.assertIn(marker, self.today)
        for callback in [
            'changeWorkDate(-1)',
            'openWorkDatePicker()',
            'changeWorkDate(1)',
            'changeWorkDate(0)',
            'openNewPatient()',
            'refreshTodayWorkbench()',
        ]:
            with self.subTest(callback=callback):
                self.assertIn(callback, self.today)
        self.assertIn('${todayWorkKpiStrip(summary)}', self.today)
        self.assertIn('${renderTodayQueue(data.appointments || [])}', self.today)
        self.assertIn('todayMoneyOrMask(summary.today_unpaid_amount)', self.today)
        self.assertIn(
            'workDate.value === localDateValue() ? "今天，一切就绪。" : "当日工作总览"',
            self.today,
        )
        # 日期横幅文案随所选日期是否为今天而切换，两种文案都必须在源码中出现——
        # 锁住条件分支不被回退成写死的"今天，一切就绪。"
        self.assertIn('今天，一切就绪。', self.today)
        self.assertIn('当日工作总览', self.today)

    def test_pearl_theme_has_fallback_focus_reduced_motion_and_breakpoints(self):
        for contract in [
            "@supports not ((backdrop-filter: blur(1px))",
            "body.theme-pearl :focus-visible",
            "@media (prefers-reduced-motion: reduce)",
            "@media (max-width: 1100px)",
            "@media (max-width: 840px)",
            "body.theme-pearl .today-overview-grid",
            "body.theme-pearl #todayQueueListWrap",
        ]:
            with self.subTest(contract=contract):
                self.assertIn(contract, self.styles)
        self.assertIn("overflow-x: auto", self.styles)


if __name__ == "__main__":
    unittest.main()
