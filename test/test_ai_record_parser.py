import unittest

from local_app.ai_record_parser import parse_ai_record_markdown


FULL_RECORD = """═══════════════════════════════════
  口腔门诊病历
═══════════════════════════════════
姓名: {NAME_042}
性别: 男
年龄: 35
就诊时间: 2026-06-07 09:30
主诊医生: {NAME_006}
诊室: 诊室1

【主诉】 46┐冷热刺激痛三天。

【现病史】
- 三天前出现冷热刺激痛
- 无自发痛

【检查所见】
- 46┐深龋及牙本质深层
- 牙周：└27B 牙石II度

【诊断】
46┐ 深龋

【治疗明细】
46┐去腐净达牙本质深层，3M Z350 树脂充填，调合抛光。已收费200元。

【收费明细】
树脂充填 × 1 = 200元

【医嘱】
1. 24小时内勿用患牙咀嚼
2. 如有不适及时复诊

【复诊计划】
一周后复诊检查。
"""

MISSING_BLOCKS = """姓名: {NAME_099}
性别: 女
年龄: 28
就诊时间: 2026-06-08 14:00
主诊医生: {NAME_053}
诊室: 诊室2

【主诉】 牙龈出血。

【诊断】
慢性牙龈炎

【医嘱】
注意口腔卫生。
"""


class ParseAiRecordTest(unittest.TestCase):
    def test_header_fields_parsed(self):
        result = parse_ai_record_markdown(FULL_RECORD)
        header = result["header"]
        self.assertEqual(header["name_code"], "{NAME_042}")
        self.assertEqual(header["sex"], "男")
        self.assertEqual(header["age"], "35")
        self.assertEqual(header["visit_time"], "2026-06-07 09:30")
        self.assertEqual(header["doctor"], "{NAME_006}")
        self.assertEqual(header["room"], "诊室1")

    def test_content_blocks_mapped_to_subjects(self):
        result = parse_ai_record_markdown(FULL_RECORD)
        content = result["content_json"]
        self.assertIn("46┐冷热刺激痛三天", content["PC"])
        self.assertIn("三天前出现冷热刺激痛", content["HPI"])
        self.assertIn("深龋及牙本质深层", content["Exam"])
        self.assertIn("深龋", content["DG"])
        self.assertIn("树脂充填", content["TR"])
        self.assertIn("24小时内勿用患牙", content["DA"])
        self.assertIn("一周后复诊", content["Plan"])

    def test_charge_detail_goes_to_extras_not_subjects(self):
        result = parse_ai_record_markdown(FULL_RECORD)
        self.assertIn("树脂充填 × 1 = 200元", result["extras"]["收费明细"])
        # 收费明细不进任何科目键
        for value in result["content_json"].values():
            self.assertNotIn("收费明细", value)

    def test_tooth_positions_extracted(self):
        result = parse_ai_record_markdown(FULL_RECORD)
        teeth = result["tooth_json"]["positions"]
        self.assertIn("46┐", teeth)
        self.assertIn("└27B", teeth)

    def test_missing_blocks_do_not_error(self):
        result = parse_ai_record_markdown(MISSING_BLOCKS)
        content = result["content_json"]
        self.assertIn("牙龈出血", content["PC"])
        self.assertIn("慢性牙龈炎", content["DG"])
        # 缺失的科目键不出现
        self.assertNotIn("HPI", content)
        self.assertNotIn("Exam", content)
        self.assertNotIn("TR", content)

    def test_name_code_preserved_verbatim(self):
        result = parse_ai_record_markdown(MISSING_BLOCKS)
        self.assertEqual(result["header"]["name_code"], "{NAME_099}")

    def test_no_teeth_yields_empty_tooth_json(self):
        text = "姓名: {NAME_001}\n\n【主诉】 洁牙咨询。\n"
        result = parse_ai_record_markdown(text)
        self.assertEqual(result["tooth_json"], {})

    def test_empty_input_does_not_crash(self):
        result = parse_ai_record_markdown("")
        self.assertEqual(result["header"]["name_code"], "")
        self.assertEqual(result["content_json"], {})
        self.assertEqual(result["tooth_json"], {})


if __name__ == "__main__":
    unittest.main()
