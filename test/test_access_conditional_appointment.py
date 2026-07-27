"""预约更新的 CONDITIONAL 权限按实际提交字段判定。"""

import unittest
from unittest.mock import Mock, patch

from fastapi import HTTPException

from local_app.routes import appointments


class DatabaseReached(RuntimeError):
    """测试哨兵：权限检查完成后，路由才允许走到 connect。"""


def _endpoint():
    router = appointments.create_appointments_router("unused.sqlite3")
    return next(
        route.endpoint
        for route in router.routes
        if route.path == "/api/appointments/{appointment_id}"
        and "PUT" in route.methods
    )


class ConditionalAppointmentPermissionTest(unittest.TestCase):
    def _permissions_before_connect(self, payload):
        required = []
        with (
            patch.object(
                appointments, "require_access", side_effect=required.append, create=True
            ),
            patch.object(
                appointments, "connect", side_effect=DatabaseReached("connect")
            ),
        ):
            with self.assertRaises(DatabaseReached):
                _endpoint()("a1", payload)
        return required

    def test_each_edit_field_requires_appointment_edit(self):
        cases = {
            "start_time": "2026-07-16 09:00",
            "end_time": "2026-07-16 10:00",
            "doctor_name": "王医生",
            "item_name": "复诊",
            "room": "诊室一",
            "visit_type": "复诊",
            "register_type": "预约",
            "appointment_type": "待确定预约",
            "consultant": "小李",
            "patient_source": "老带新",
            "booked_by": "前台甲",
            "remark": "怕疼",
        }
        for field, value in cases.items():
            with self.subTest(field=field):
                self.assertEqual(
                    self._permissions_before_connect({field: value}),
                    ["appointment.edit"],
                )

    def test_non_cancel_status_requires_appointment_status(self):
        for value in ("1", "已到诊", 2):
            with self.subTest(value=value):
                self.assertEqual(
                    self._permissions_before_connect({"status": value}),
                    ["appointment.status"],
                )

    def test_cancel_reason_requires_appointment_cancel(self):
        self.assertEqual(
            self._permissions_before_connect({"cancel_reason": "患者临时有事"}),
            ["appointment.cancel"],
        )

    def test_numeric_and_chinese_cancel_statuses_only_require_cancel(self):
        for value in (3, "3", 9, "9", "已取消", "已爽约", "爽约"):
            with self.subTest(value=value):
                self.assertEqual(
                    self._permissions_before_connect({"status": value}),
                    ["appointment.cancel"],
                )

    def test_mixed_payload_requires_union_before_connect(self):
        self.assertEqual(
            self._permissions_before_connect(
                {
                    "room": "诊室二",
                    "status": "已到诊",
                    "cancel_reason": "撤销旧取消原因",
                    "remark": "患者提前到",
                }
            ),
            ["appointment.edit", "appointment.status", "appointment.cancel"],
        )

    def test_missing_required_permission_never_connects(self):
        cases = (
            ({"room": "诊室二"}, "appointment.edit"),
            ({"status": "已到诊"}, "appointment.status"),
            ({"status": "已取消"}, "appointment.cancel"),
            (
                {
                    "room": "诊室二",
                    "status": "已到诊",
                    "cancel_reason": "撤销旧取消原因",
                },
                "appointment.status",
            ),
        )
        for payload, denied in cases:
            with self.subTest(payload=payload, denied=denied):
                db_connect = Mock()

                def require(permission):
                    if permission == denied:
                        raise HTTPException(status_code=403, detail="access_denied")

                with (
                    patch.object(
                        appointments, "require_access", side_effect=require, create=True
                    ),
                    patch.object(appointments, "connect", db_connect),
                ):
                    with self.assertRaises(HTTPException) as caught:
                        _endpoint()("a1", payload)

                self.assertEqual(caught.exception.status_code, 403)
                db_connect.assert_not_called()

    def test_no_editable_fields_stays_400_without_connecting(self):
        require = Mock()
        db_connect = Mock()
        with (
            patch.object(appointments, "require_access", require, create=True),
            patch.object(appointments, "connect", db_connect),
        ):
            with self.assertRaises(HTTPException) as caught:
                _endpoint()("a1", {"patient_identity": "p2"})

        self.assertEqual(caught.exception.status_code, 400)
        require.assert_not_called()
        db_connect.assert_not_called()


if __name__ == "__main__":
    unittest.main()
