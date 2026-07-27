import unittest

from local_app.paid_detail import split_paid_detail


class TestSplitPaidDetail(unittest.TestCase):
    """paiddetail 拆分——单一真相源,解析不出回退整笔计。"""

    def test_multi_method(self):
        self.assertEqual(split_paid_detail("云闪付:1233||现金:800"),
                         [("云闪付", 1233.0), ("现金", 800.0)])

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
