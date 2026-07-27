import datetime as dt
import os
import unittest

import local_app  # noqa: F401  导入包即触发入口处的北京时区设置


class TestBeijingTimezone(unittest.TestCase):
    """诊所业务时间独立于宿主机系统时区，一律北京时间(Asia/Shanghai)。"""

    def test_tz_env_pinned(self):
        self.assertEqual(os.environ.get("TZ"), "Asia/Shanghai")

    def test_naive_now_is_beijing(self):
        beijing = dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).replace(tzinfo=None)
        local = dt.datetime.now()
        self.assertLess(abs((local - beijing).total_seconds()), 5)


if __name__ == "__main__":
    unittest.main()
