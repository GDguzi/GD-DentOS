"""#420二批守卫:手填人员字段统一挂 staff 选人控件(data-staff-role datalist,可搜可自由填)。
覆盖:退费医生/治疗计划医生/预约历史编辑医生/口腔检查医生/库房经手人+领用人/收银查询医生+收银员筛选;
staffByRole("*")=全员候选;各渲染点渲染后调 bindStaffInputs。源码级断言,防后续 UI 调整丢控件。"""
import unittest
from pathlib import Path

S = Path(__file__).resolve().parent.parent.parent / "local_app" / "static"


def _t(name):
    return (S / name).read_text(encoding="utf-8")


class StaffPickers420Test(unittest.TestCase):
    def test_star_role_returns_all_staff(self):
        self.assertIn('if (role === "*") return staffCache || []', _t("staff_manager.js"))

    def test_refund_doctor_picker(self):
        t = _t("workspace_billing.js")
        self.assertIn('data-rf="doctor" data-staff-role="医生"', t)
        self.assertIn("bindStaffInputs(ov)", t)

    def test_plan_doctor_picker(self):
        t = _t("treatment_plan_editor.js")
        self.assertIn('data-meta="doctor_name" data-staff-role="医生"', t)
        self.assertIn("bindStaffInputs(editor)", t)

    def test_appointment_edit_doctor_picker(self):
        t = _t("workspace_tabs.js")
        self.assertIn('data-appointment-field="doctor_name" data-staff-role="医生"', t)
        self.assertIn("bindStaffInputs(panel)", t)

    def test_oral_exam_doctor_picker(self):
        t = _t("oral_exam_tab.js")
        self.assertIn('data-meta="exam_doctor" data-staff-role="医生"', t)
        self.assertIn("bindStaffInputs(panel)", t)
        self.assertIn("bindStaffInputs(f)", t)

    def test_warehouse_operator_receiver_picker(self):
        t = _t("warehouse.js")
        self.assertIn('data-f="operator" data-staff-role="*"', t)
        self.assertIn('data-f="receiver" data-staff-role="*"', t)
        self.assertIn("bindStaffInputs(wrap)", t)

    def test_cashier_filter_pickers(self):
        t = _t("reports.js")
        self.assertIn('data-cash-doctor data-staff-role="医生"', t)
        self.assertIn('data-cash-operator data-staff-role="*"', t)
        self.assertIn("bindStaffInputs(body)", t)


    def test_pay_cashier_picker_666(self):
        # #666 收款弹窗收银员必须接人员库(用户拍板:只放前台岗),弹窗打开时绑定
        t = _t("index.html")
        self.assertIn('id="payCashier" class="pay-input" data-staff-role="前台"', t)
        b = _t("workspace_billing.js")
        self.assertIn("bindStaffInputs(m)", b)


if __name__ == "__main__":
    unittest.main()
