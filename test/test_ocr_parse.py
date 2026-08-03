import threading
import unittest

from local_app.ocr import TextBox, id_card_valid, parse_patient_card


def B(text, x, y, w=0.3, h=0.05, conf=0.9):
    """构造一个 OCR 文字块。坐标是 Vision 原生归一化坐标：原点左下，y 向上。"""
    return TextBox(text=text, conf=conf, x=x, y=y, w=w, h=h)


# 版式 A：标签和值粘成一块（"姓名陈明远"）。真证 OCR 就是这么返回的，坐标与分块方式照实测复刻，
# 但姓名/号码/地址全部是编的合成数据——真实证件信息一律不入仓（本仓要开源）。
GLUED_LAYOUT = [
    B("姓名陈明远", 0.09, 0.78),
    B("性别男民族汉", 0.09, 0.66),
    B("出生", 0.09, 0.57),
    B("1990", 0.21, 0.56),
    B("年3月7日", 0.30, 0.56),
    B("住 址江苏省南京市玄武区中山", 0.09, 0.43),
    B("路88号民主里3栋201", 0.20, 0.35),
    B("公民身份号码", 0.09, 0.15),
    B("y回THNXS", 0.68, 0.20, conf=0.3),   # 「中国CHINA」全息防伪膜识别出的乱码
    B("990101199003070015", 0.35, 0.12),
]

# 另一种版式：标签和值分成两块（"姓名" | "张伟民"）
SPLIT_LAYOUT = [
    B("姓名", 0.07, 0.84, w=0.10),
    B("张伟民", 0.23, 0.84),
    B("性别", 0.07, 0.71, w=0.10),
    B("男", 0.23, 0.71, w=0.04),
    B("民 族", 0.42, 0.71, w=0.10),
    B("汉", 0.57, 0.71, w=0.04),
    B("住址", 0.07, 0.45, w=0.10),
    B("浙江省杭州市西湖区文三路", 0.22, 0.45),
    B("398号紫金公寓3幢2单元501室", 0.22, 0.38),
    B("公民身份号码", 0.07, 0.21),
    B("990102198503120017", 0.32, 0.20),
]


class TestIdCardValid(unittest.TestCase):
    def test_valid_number_passes(self):
        self.assertTrue(id_card_valid("990101199003070015"))

    def test_check_digit_x(self):
        self.assertTrue(id_card_valid("99010419491231006X"))

    def test_lowercase_x_ok(self):
        self.assertTrue(id_card_valid("99010419491231006x"))

    def test_wrong_check_digit_fails(self):
        self.assertFalse(id_card_valid("990101199003070010"))

    def test_bad_shape_fails(self):
        for bad in ("", "123", "99010119900307001", "99010119900307001A"):
            self.assertFalse(id_card_valid(bad))


class TestGluedLayout(unittest.TestCase):
    def setUp(self):
        self.r = parse_patient_card(GLUED_LAYOUT)

    def test_id_card(self):
        self.assertEqual(self.r["fields"]["id_card"], "990101199003070015")

    def test_birthday_is_derived_from_id(self):
        self.assertEqual(self.r["fields"]["birthday"], "1990-03-07")

    def test_sex_is_derived_from_id(self):
        self.assertEqual(self.r["fields"]["sex"], "男")

    def test_name(self):
        self.assertEqual(self.r["fields"]["display_name"], "陈明远")

    def test_address_excludes_hologram_garbage(self):
        # 全息防伪膜的乱码在证件右侧，绝不能拼进地址
        self.assertEqual(self.r["fields"]["address"], "江苏省南京市玄武区中山路88号民主里3栋201")

    def test_derived_fields_are_confident(self):
        self.assertEqual(set(self.r["confident"]), {"id_card", "birthday", "sex"})

    def test_kind(self):
        self.assertEqual(self.r["kind"], "id_card")

    def test_no_warnings(self):
        self.assertEqual(self.r["warnings"], [])


class TestSplitLayout(unittest.TestCase):
    def setUp(self):
        self.r = parse_patient_card(SPLIT_LAYOUT)

    def test_name_from_right_of_label(self):
        self.assertEqual(self.r["fields"]["display_name"], "张伟民")

    def test_address_joins_both_lines_and_excludes_id_number(self):
        # 身份证号块与「公民身份号码」标签块不同排，下边界取错会把号码拼进地址
        self.assertEqual(
            self.r["fields"]["address"],
            "浙江省杭州市西湖区文三路398号紫金公寓3幢2单元501室",
        )

    def test_sex_derived_not_ocr(self):
        self.assertEqual(self.r["fields"]["sex"], "男")


class TestBadCheckDigit(unittest.TestCase):
    def test_field_returned_but_flagged(self):
        boxes = [B("990101199003070010", 0.35, 0.12)]
        r = parse_patient_card(boxes)
        self.assertEqual(r["fields"]["id_card"], "990101199003070010")
        self.assertNotIn("id_card", r["confident"])   # 认错了/是假号，不能标"可信"
        self.assertTrue(any("校验" in w for w in r["warnings"]))

    def test_derived_fields_absent_when_check_fails(self):
        # 号码本身就不可信，从它推出来的生日性别更不能填
        r = parse_patient_card([B("990101199003070010", 0.35, 0.12)])
        self.assertNotIn("birthday", r["fields"])
        self.assertNotIn("sex", r["fields"])


class TestImpossibleBirthday(unittest.TestCase):
    """校验位只管最后一位算得对不对，不管日期本身成不成立。13 月 40 日照样能过校验，
    绝不能把这种生日当"可信"填进患者档案。"""

    def test_month_13_day_40_passes_checksum_but_is_rejected(self):
        num = "990103199013400017"
        self.assertTrue(id_card_valid(num))              # 校验位确实是对的
        r = parse_patient_card([B(num, 0.35, 0.12)])
        self.assertEqual(r["fields"]["id_card"], num)    # 号码本身照填，让人核对
        self.assertNotIn("birthday", r["fields"])        # 但 1990-13-40 绝不能填
        self.assertNotIn("sex", r["fields"])
        self.assertNotIn("id_card", r["confident"])
        self.assertTrue(any("出生日期" in w for w in r["warnings"]))

    def test_future_birthday_rejected(self):
        r = parse_patient_card([B("99010521991231003X", 0.35, 0.12)])   # 2199 年出生
        self.assertNotIn("birthday", r["fields"])


class TestPhone(unittest.TestCase):
    def test_extracted_from_noise(self):
        boxes = [B("联系电话：13800138000（本人）", 0.1, 0.5)]
        r = parse_patient_card(boxes)
        self.assertEqual(r["fields"]["phone"], "13800138000")

    def test_not_confused_by_id_card_digits(self):
        # 身份证号里含 11 位连续数字，不能被当成手机号
        r = parse_patient_card(GLUED_LAYOUT)
        self.assertNotIn("phone", r["fields"])

    def test_landline_ignored(self):
        r = parse_patient_card([B("0755-88886666", 0.1, 0.5)])
        self.assertNotIn("phone", r["fields"])

    def test_partial_id_number_yields_no_phone(self):
        """身份证号少认一位校验位 → 17 位数字。从中任取 11 位很像手机号，
        而电话是建档必填项——抠错了就是把假号码存进患者档案。"""
        r = parse_patient_card([B("99010119900307001", 0.35, 0.12)])   # 17 位，掉了校验位
        self.assertNotIn("phone", r["fields"])

    def test_phone_not_glued_across_lines(self):
        """两行的数字拼在一起可能凑出个"手机号"。逐行找才不会串。"""
        r = parse_patient_card([B("卡号6228481", 0.1, 0.6), B("3800138000元", 0.1, 0.5)])
        self.assertNotIn("phone", r["fields"])


class TestUnknownCard(unittest.TestCase):
    def test_wechat_screenshot_gets_phone_and_lines(self):
        boxes = [B("我叫王小明", 0.1, 0.8), B("电话 13912345678", 0.1, 0.7)]
        r = parse_patient_card(boxes)
        self.assertEqual(r["kind"], "unknown")
        self.assertEqual(r["fields"]["phone"], "13912345678")
        self.assertEqual(r["lines"], ["我叫王小明", "电话 13912345678"])   # 兜底：原始行给人点选

    def test_empty_input(self):
        r = parse_patient_card([])
        self.assertEqual(r["fields"], {})
        self.assertEqual(r["lines"], [])
        self.assertEqual(r["kind"], "unknown")


class TestPixelBomb(unittest.TestCase):
    def test_huge_dimensions_rejected_before_decoding(self):
        """8MB 限的是压缩后的字节。一张几十 KB 的纯色 PNG 能解压成几千万像素撑爆内存，
        必须在解码前按尺寸拒掉。"""
        import io

        from PIL import Image

        from local_app import ocr as ocr_mod
        if not ocr_mod.available():
            self.skipTest("本机无识别引擎")

        buf = io.BytesIO()
        Image.new("L", (8000, 8000), 255).save(buf, format="PNG")   # 6400 万像素，文件才几十 KB
        payload = buf.getvalue()
        self.assertLess(len(payload), 1024 * 1024)                  # 体积确实很小，限流拦不住它
        with self.assertRaises(ValueError):
            ocr_mod.recognize(payload)


class TestAutoreleasePool(unittest.TestCase):
    def test_recognize_wrapped_in_autorelease_pool(self):
        """识别跑在线程池的工作线程里，那儿没有 Cocoa 自动回收池，原生对象只堆不放。
        实测：200 次识别，无回收池涨 14.2MB，有回收池只涨 2.7MB。服务常年不重启，必须有。"""
        import inspect

        from local_app import ocr as ocr_mod
        self.assertIn("autorelease_pool", inspect.getsource(ocr_mod.recognize))


class TestRuntimeBreaker(unittest.TestCase):
    """框架能 import 不代表 Vision 真能跑。首次运行失败要熔断，之后 available() 转 False，
    前端隐藏入口——否则前台会一直点一个每次都失败的按钮。"""

    def setUp(self):
        from local_app import ocr as ocr_mod
        self.ocr = ocr_mod
        self._saved_broken = ocr_mod._RUNTIME_BROKEN
        self._saved_fails = ocr_mod._CONSECUTIVE_FAILURES
        ocr_mod._RUNTIME_BROKEN = False
        ocr_mod._CONSECUTIVE_FAILURES = 0
        if not ocr_mod._VISION_OK:
            self.skipTest("本机无 pyobjc")

    def tearDown(self):
        self.ocr._RUNTIME_BROKEN = self._saved_broken   # 别把熔断状态漏给别的测试
        self.ocr._CONSECUTIVE_FAILURES = self._saved_fails

    def _make_engine_fail(self):
        self.ocr._recognize = lambda b: (_ for _ in ()).throw(RuntimeError("Vision 起不来"))

    def test_single_failure_does_not_trip_breaker(self):
        # 一张问题图让 Vision 出错，不该把整个引擎判死、搞停全院 OCR
        original = self.ocr._recognize
        self._make_engine_fail()
        try:
            with self.assertRaises(self.ocr.OcrUnavailable):
                self.ocr.recognize(b"\xff\xd8\xff whatever")
            self.assertTrue(self.ocr.available())         # 单次失败，引擎仍可用
        finally:
            self.ocr._recognize = original

    def test_consecutive_failures_trip_breaker(self):
        original = self.ocr._recognize
        self._make_engine_fail()
        try:
            for _ in range(self.ocr._BREAKER_THRESHOLD):
                with self.assertRaises(self.ocr.OcrUnavailable):
                    self.ocr.recognize(b"\xff\xd8\xff whatever")
            self.assertFalse(self.ocr.available())        # 连续多次全挂 → 熔断
            self.assertIsNone(self.ocr.engine_name())
        finally:
            self.ocr._recognize = original

    def test_success_resets_failure_streak(self):
        # 失败几次但没到阈值，中间成功一次就清零，不会攒着攒着突然熔断
        original = self.ocr._recognize
        try:
            self._make_engine_fail()
            for _ in range(self.ocr._BREAKER_THRESHOLD - 1):
                with self.assertRaises(self.ocr.OcrUnavailable):
                    self.ocr.recognize(b"\xff\xd8\xff x")
            self.ocr._recognize = lambda b: []            # 成功一次
            self.ocr.recognize(b"\xff\xd8\xff x")
            self.assertEqual(self.ocr._CONSECUTIVE_FAILURES, 0)
            self.assertTrue(self.ocr.available())
        finally:
            self.ocr._recognize = original

    def test_image_error_does_not_count_as_engine_failure(self):
        # 图片本身坏 ≠ 引擎坏：走 ValueError，不计入连续失败
        with self.assertRaises(ValueError):
            self.ocr.recognize(b"\xff\xd8\xff not a real jpeg body")
        self.assertEqual(self.ocr._CONSECUTIVE_FAILURES, 0)
        self.assertTrue(self.ocr.available())

    def test_open_breaker_fails_before_native_call(self):
        native_calls = 0
        original = self.ocr._recognize

        def native(_payload):
            nonlocal native_calls
            native_calls += 1
            return []

        self.ocr._RUNTIME_BROKEN = True
        self.ocr._recognize = native
        try:
            with self.assertRaises(self.ocr.OcrUnavailable):
                self.ocr.recognize(b"blocked")
            self.assertEqual(native_calls, 0, "熔断后必须在实际调用 Vision 前 fail-fast")
        finally:
            self.ocr._recognize = original

    def test_concurrent_success_and_failure_follow_completion_order(self):
        original = self.ocr._recognize

        def run_order(first):
            self.ocr._CONSECUTIVE_FAILURES = self.ocr._BREAKER_THRESHOLD - 1
            self.ocr._RUNTIME_BROKEN = False
            both_inside_native = threading.Barrier(2)
            release = {"success": threading.Event(), "failure": threading.Event()}
            entered = {"success": threading.Event(), "failure": threading.Event()}
            completed = {"success": threading.Event(), "failure": threading.Event()}
            outcomes = {}

            def native(payload):
                kind = payload.decode()
                entered[kind].set()
                both_inside_native.wait(timeout=2)
                release[kind].wait(timeout=2)
                if kind == "failure":
                    raise RuntimeError("Vision 起不来")
                return []

            def invoke(kind):
                try:
                    self.ocr.recognize(kind.encode())
                    outcomes[kind] = "ok"
                except self.ocr.OcrUnavailable:
                    outcomes[kind] = "unavailable"
                finally:
                    completed[kind].set()

            self.ocr._recognize = native
            threads = [threading.Thread(target=invoke, args=(kind,)) for kind in ("success", "failure")]
            for thread in threads:
                thread.start()
            self.assertTrue(entered["success"].wait(timeout=2))
            self.assertTrue(entered["failure"].wait(timeout=2))
            second = "failure" if first == "success" else "success"
            release[first].set()
            self.assertTrue(completed[first].wait(timeout=2))
            release[second].set()
            for thread in threads:
                thread.join(timeout=2)
                self.assertFalse(thread.is_alive())
            self.assertEqual(outcomes, {"success": "ok", "failure": "unavailable"})
            return self.ocr._CONSECUTIVE_FAILURES, self.ocr._RUNTIME_BROKEN

        try:
            self.assertEqual(run_order("failure"), (0, False), "后完成的成功必须原子清空失败数和熔断")
            self.assertEqual(run_order("success"), (1, False), "后完成的失败必须从最近成功后的零开始计数")
        finally:
            self.ocr._recognize = original


class TestEngineAvailability(unittest.TestCase):
    def test_available_is_bool(self):
        from local_app import ocr
        self.assertIsInstance(ocr.available(), bool)

    def test_engine_name_matches_availability(self):
        from local_app import ocr
        self.assertEqual(ocr.engine_name(), "macos-vision" if ocr.available() else None)

    def test_recognize_raises_when_unavailable(self):
        # 没有 Vision 的机器（Windows/Linux 开源用户）必须干净报错，不能崩在 import 上
        from local_app import ocr
        original = ocr._VISION_OK
        ocr._VISION_OK = False
        try:
            with self.assertRaises(ocr.OcrUnavailable):
                ocr.recognize(b"whatever")
        finally:
            ocr._VISION_OK = original


if __name__ == "__main__":
    unittest.main()
