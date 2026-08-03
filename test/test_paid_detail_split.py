import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from local_app.api import create_app
from local_app.paid_detail import load_pay_method_aliases, split_paid_detail


class TestSplitPaidDetail(unittest.TestCase):
    """#525:paiddetail 拆分——单一真相源,解析不出回退整笔计。"""

    def test_multi_method(self):
        self.assertEqual(split_paid_detail("扫码付:1233||现金:800"),
                         [("扫码付", 1233.0), ("现金", 800.0)])

    def test_configured_alias_applies(self):
        """上游写法与本地收款选项写法不一致时靠 DENTAL_PAY_METHOD_ALIASES 归一，
        口径必须与 paytype_from_detail 一致；没配就按原文落，不猜。"""
        with mock.patch.dict(os.environ, {"DENTAL_PAY_METHOD_ALIASES": "扫码Pay=扫码付"}):
            self.assertEqual(split_paid_detail("扫码Pay:1233||现金:800"),
                             [("扫码付", 1233.0), ("现金", 800.0)])
        with mock.patch.dict(os.environ, {"DENTAL_PAY_METHOD_ALIASES": ""}):
            self.assertEqual(split_paid_detail("扫码Pay:1233"), [("扫码Pay", 1233.0)])

    def test_malformed_alias_config_raises(self):
        """配置写错必须当场报错，不许跳过那条继续跑（禁止兜底）。

        静默忽略一条写坏的映射，表现是服务照常起、报表里同一种付款方式悄悄裂成两组，
        钱对不上还查不出原因——比直接起不来危险得多。
        """
        for bad in ("坏配置", "扫码Pay=扫码付,漏了等号", "=空来源", "空目标=",
                    "扫码Pay=扫码付=多余", "a=b=c"):   # 多个等号也不许：partition 只切第一个，会静默错映射
            with self.subTest(bad=bad), mock.patch.dict(
                os.environ, {"DENTAL_PAY_METHOD_ALIASES": bad}
            ):
                with self.assertRaises(ValueError):
                    load_pay_method_aliases()

    def test_run_local_validates_before_any_side_effect(self):
        """配置校验必须排在建库/迁移/种子管理员之前——写错配置不该已经把生产库改了。"""
        import local_app.run_local as run_local
        with mock.patch.dict(os.environ, {"DENTAL_PAY_METHOD_ALIASES": "漏了等号"}), \
                mock.patch.object(run_local, "init_db") as init_db:
            with self.assertRaises(ValueError):
                run_local.main()
            init_db.assert_not_called()   # 一行库都不许建

    def test_app_refuses_to_start_on_malformed_alias_config(self):
        """写错配置要在启动时就炸，而不是等到出报表——那时钱已经按错的方式名分完组了。"""
        with mock.patch.dict(os.environ, {"DENTAL_PAY_METHOD_ALIASES": "漏了等号"}):
            with tempfile.TemporaryDirectory() as tmp:
                with self.assertRaises(ValueError):
                    create_app(Path(tmp) / "clinic.sqlite3")

    def test_single_method(self):
        self.assertEqual(split_paid_detail("微信:450"), [("微信", 450.0)])

    def test_thousand_separator(self):
        self.assertEqual(split_paid_detail("银行卡:3,653.00"), [("银行卡", 3653.0)])

    def test_garbage_returns_empty(self):
        self.assertEqual(split_paid_detail("现金:三百"), [])   # 金额非数
        self.assertEqual(split_paid_detail(":300"), [])        # 缺方式名
        self.assertEqual(split_paid_detail(""), [])
        self.assertEqual(split_paid_detail(None), [])


if __name__ == "__main__":
    unittest.main()
