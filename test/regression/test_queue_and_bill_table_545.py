"""#545 回归(剩余部分)：候诊队列操作按钮32px太窄挤字；收费表格14列在1280宽度下
"打印/详情"操作列默认落在屏幕外，只能横向滚到底才看得到，无任何提示/固定。
静态检查对应 CSS/HTML 是否已按更宽按钮+粘性操作列的方式修复。
跑：python3 -m unittest test.test_queue_and_bill_table_545 -q
"""
import unittest
from pathlib import Path


class QueueAndBillTable545Test(unittest.TestCase):
    def setUp(self):
        self.styles = Path("local_app/static/styles.css").read_text(encoding="utf-8")
        self.workspace_tabs = Path("local_app/static/workspace_billing.js").read_text(encoding="utf-8")   # 收费/退费markup已拆到workspace_billing.js

    def test_queue_step_button_widened(self):
        self.assertIn(".tq-step { flex: 0 0 36px;", self.styles)
        self.assertNotIn("flex: 0 0 32px", self.styles)
        self.assertIn(".tq-lbl { font-size: 9.5px; line-height: 1.1; }", self.styles)

    def test_queue_row_grid_column_matches_widened_buttons(self):
        # 12键*36px=432px+边框,列宽跟着按钮宽度同步调整,不然会被 overflow:hidden 裁切
        self.assertIn("grid-template-columns: 450px minmax(150px, 1.1fr) minmax(110px, 1.1fr) minmax(120px, 1.3fr) 78px;", self.styles)

    def test_bill_table_ops_column_sticky_to_stay_reachable(self):
        self.assertIn('class="bt-ops-h"', self.workspace_tabs)
        self.assertIn(".bill-table .bt-ops, .bill-table th.bt-ops-h { position: sticky; right: 0;", self.styles)


if __name__ == "__main__":
    unittest.main()

    def test_plan_row_wraps_instead_of_overflow(self):
        # #545 尾巴:治疗计划行禁换行+行内滚动把备注/删除键推出可视区 → 改 wrap;
        # 且 .plan-handle 选择器与类名 plan-handle-btn 不符,宽度约束需真实生效
        import re
        block = re.search(r"\.plan-row \{[^}]+\}", self.styles).group(0)
        self.assertIn("flex-wrap: wrap", block)
        self.assertNotIn("overflow-x: auto", block)
        self.assertIn(".plan-handle-btn { flex: 0 0 150px;", self.styles)
