"""合成演示库播种器冒烟测试：seed 到临时库,断言数据齐全且能驱动关键功能。
纯合成数据,不碰真实库。也兼作 简表 + 支付方式分组在生成数据上的回归。"""
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from local_app.api import create_app
from local_app.demo.seed_demo import seed


class DemoSeedSmallSetTest(unittest.TestCase):
    def test_small_patient_set_seeds_without_sample_overflow(self):
        """小演示集(n<抽样桶)也要能完整生成:各桶抽样按 min(桶,患者数) 封顶。"""
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "demo.sqlite3"
            counts = seed(db, images_dir=Path(tmp) / "images", n_patients=5, seed=42)
            self.assertEqual(counts["patients"], 5)
            for t in ("medical_records", "appointments", "bills", "payments",
                      "treatment_orders", "return_visits"):
                with self.subTest(table=t):
                    self.assertGreater(counts[t], 0, f"{t} 应有合成数据")


class DemoSeedTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "demo_clinic.sqlite3"
        self.imgs = Path(self.tmp.name) / "images"
        self.counts = seed(self.db, images_dir=self.imgs, n_patients=100, seed=42)
        self.client = TestClient(create_app(self.db, images_dir=self.imgs, require_login=False))

    def tearDown(self):
        self.tmp.cleanup()

    def test_counts_populated(self):
        self.assertEqual(self.counts["patients"], 100)
        for t in ("medical_records", "appointments", "bills", "payments",
                  "treatment_orders", "treatment_items", "return_visits"):
            with self.subTest(table=t):
                self.assertGreater(self.counts[t], 0, f"{t} 应有合成数据")

    def test_patient_group_column_seeded(self):
        # 分组真相源=patient_group 列(迁列),演示库必须落列,分组筛选/统计才读得到
        import sqlite3
        with sqlite3.connect(self.db) as conn:
            n = conn.execute("select count(*) from patients where coalesce(patient_group,'') != ''").fetchone()[0]
        self.assertGreater(n, 0, "演示患者 patient_group 列不得全空")
        groups = self.client.get("/api/patient-groups").json()["groups"]
        self.assertTrue(groups, "演示库分组统计应有分组")

    def test_no_real_pii_markers(self):
        # 合成器只产假值:患者标识/单号都带 demo_ 前缀,姓名来自固定假名池
        import sqlite3
        conn = sqlite3.connect(self.db)
        ids = [r[0] for r in conn.execute("select patient_identity from patients").fetchall()]
        conn.close()
        self.assertTrue(all(i.startswith("demo_p_") for i in ids))

    def test_today_work_simple_lists_have_content(self):
        # 六个左入口简表都应有内容,证明实机能逐个点开测
        d = self.client.get("/api/today-work").json()
        s = d["summary"]
        self.assertGreater(s["appointments_today"], 0)
        self.assertGreater(s["tomorrow_appointments"], 0)
        self.assertGreater(s["today_settlements"], 0)
        self.assertGreater(s["birthdays_today"], 0)
        self.assertGreater(len(d["tomorrow_appointments"]), 0)
        self.assertGreater(len(d["today_settlements"]), 0)
        self.assertGreater(len(d["birthdays"]), 0)
        self.assertGreater(len(d["return_visits"]), 0)

    def test_finance_groups_by_pay_method(self):
        # 今日实收按支付方式分组应出多种方式(现金/微信/支付宝/银行卡)
        tc = self.client.get("/api/reports/today-collection").json()
        methods = {m["method"] for m in tc["by_method"]}
        self.assertTrue(methods & {"现金", "微信", "支付宝", "银行卡"},
                        f"应按支付方式分组,得到 {methods}")
        self.assertGreater(tc["collected"], 0)

    def test_medical_record_save_path(self):
        # 新建病历保存路径可用(POST 返回新 record_id)
        r = self.client.post("/api/medical-records", json={
            "patient_identity": "demo_p_0001", "record_type": "复诊",
            "visit_time": "2026-06-28", "content_json": {"PC": "演示"}, "tooth_json": {}})
        self.assertEqual(r.status_code, 200)
        self.assertTrue((r.json().get("medical_record") or {}).get("record_id"))

    def test_images_replicate_real_types_and_serve(self):
        # 复刻真实影像:有 patient_images 记录(真实 category) + 同格式占位 JPG 能被服务
        self.assertGreater(self.counts["patient_images"], 0)
        import sqlite3
        conn = sqlite3.connect(self.db)
        pid, img_id, cat = conn.execute(
            "select patient_identity, image_id, category from patient_images limit 1").fetchone()
        cats = {r[0] for r in conn.execute("select distinct category from patient_images")}
        conn.close()
        self.assertTrue(cats & {"口内照", "全景片", "根尖片", "正畸组图"}, f"影像类型应复刻真实,得到 {cats}")
        f = self.client.get(f"/api/patients/{pid}/images/{img_id}/file")
        self.assertEqual(f.status_code, 200)
        self.assertEqual(f.headers.get("content-type"), "image/jpeg")
        self.assertEqual(f.content[:3], b"\xff\xd8\xff", "应为有效 JPEG")

    def test_handle_items_picker_not_empty(self):
        # 处置选择器数据源 handle_items 必须有内容,按 fee_type 分组
        self.assertGreater(self.counts["handle_items"], 0)
        d = self.client.get("/api/handle-items").json()
        self.assertGreater(len(d["groups"]), 0, "处置选择器应有分组")
        names = [i["name"] for g in d["groups"] for i in g["items"]]
        self.assertTrue(any("根管" in n for n in names), "应能搜到根管类项目")

    def test_staff_seeded_active(self):
        # 演示医生须是在职 staff_members,否则处置医生下拉显示「已停用」
        r = self.client.get("/api/staff-members?role=医生").json()
        self.assertGreater(len(r["members"]), 0, "应有在职医生")

    def test_source_json_replicates_import_keys(self):
        # 复刻真实导入:source_json 带应用真正会读的 外部字段键(否则相关功能在 demo 上读不到)
        import json
        import sqlite3
        conn = sqlite3.connect(self.db)

        def keys(table):
            return set(json.loads(conn.execute(f"select source_json from {table} limit 1").fetchone()[0]))
        self.assertTrue({"patientid", "groupname", "origin"} <= keys("patients"))
        self.assertIn("ScheduleStartTime", keys("appointments"))
        self.assertTrue({"DiscountFee", "BillDoct"} <= keys("bills"))
        self.assertTrue({"pay_method", "CancelMark"} <= keys("payments"))
        conn.close()


if __name__ == "__main__":
    unittest.main()
