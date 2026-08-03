"""#543 回归:多个弹窗(新增预约/新增患者/预约设置/送消单/退费)在720p视口下
关键按钮(保存/确认收款/退费确认)被压出屏幕或需要滚动。`.pay-modal` 早就是
正确的「flex列+内容可滚动+头尾固定」写法，其余弹窗没跟进，属逐个遗漏。
静态检查 styles.css/workspace_tabs.js 里对应 CSS 规则是否已按同一套模式补齐。
跑：python3 -m unittest test.test_modal_viewport_543 -q
"""
import unittest
from pathlib import Path


class ModalViewport543Test(unittest.TestCase):
    def setUp(self):
        self.styles = Path("local_app/static/styles.css").read_text(encoding="utf-8")
        self.workspace_tabs = Path("local_app/static/workspace_billing.js").read_text(encoding="utf-8")   # 收费/退费markup已拆到workspace_billing.js

    def test_appt_modal_head_and_actions_pinned(self):
        # 头尾不可被压缩,只有中间内容区滚动(同 .pay-modal 已验证写法)
        self.assertIn(".appt-modal .modal-head, .appt-modal .modal-actions { flex: 0 0 auto; }", self.styles)

    def test_appt_body_scrolls_within_bounded_modal(self):
        self.assertIn(".appt-body { padding: 14px 16px; overflow: auto; display: flex; flex-direction: column; gap: 10px; flex: 1 1 auto; min-height: 0; }", self.styles)

    def test_appt_dialog_bounded_and_columns_fill_available_height(self):
        # 三栏预约弹窗(appt-modal-wide)：appt-dialog 要能收缩到剩余空间,
        # 不能按内容撑到 78vh 固定值把 modal-actions 顶出 90vh 视口外。
        self.assertIn("flex: 1 1 auto; min-height: 0; grid-template-rows: minmax(0, 1fr);", self.styles)
        self.assertNotRegex(self.styles, r"\.appt-col \{[^}]*max-height:\s*78vh")
        self.assertIn(".appt-col { padding: 12px 14px; height: 100%; overflow-y: auto; min-height: 0; display: flex; flex-direction: column; gap: 8px; }", self.styles)

    def test_refund_modal_head_actions_pinned_body_scrolls(self):
        self.assertIn(".refund-modal { background: var(--white); border-radius: 10px; width: min(880px, 94vw); max-height: 92vh; padding: 16px 20px; display: flex; flex-direction: column; overflow: hidden; }", self.styles)
        self.assertIn(".refund-modal .modal-head, .refund-modal .modal-actions { flex: 0 0 auto; }", self.styles)
        self.assertIn(".refund-scroll { flex: 1 1 auto; overflow-y: auto; min-height: 0; }", self.styles)
        self.assertIn('class="refund-scroll"', self.workspace_tabs)


if __name__ == "__main__":
    unittest.main()
