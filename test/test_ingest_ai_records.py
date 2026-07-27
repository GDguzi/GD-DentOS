import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from local_app.db import init_db
from local_app.ingest_ai_records import ingest_ai_records
from local_app.snapshots import medical_record_snapshot
from local_app.versioning import stable_hash


RECORD_MATCH = """姓名: {NAME_001}
性别: 男
年龄: 40
就诊时间: 2026-06-07 09:30
主诊医生: {NAME_006}
诊室: 诊室1

【主诉】 46┐冷热刺激痛。

【检查所见】
46┐深龋

【诊断】
46┐ 深龋

【治疗明细】
46┐树脂充填，已收费200元。

【收费明细】
树脂充填 × 1 = 200元

【医嘱】
勿用患牙咀嚼。
"""

RECORD_UNMATCHED = """姓名: {NAME_404}
性别: 女
年龄: 30
就诊时间: 2026-06-07 10:00
主诊医生: {NAME_053}
诊室: 诊室2

【主诉】 洁牙。
"""

RECORD_DUP = """姓名: {NAME_DUP}
性别: 男
年龄: 50
就诊时间: 2026-06-07 11:00
主诊医生: {NAME_006}
诊室: 诊室1

【主诉】 复查。
"""


def _setup(tmpdir):
    root = Path(tmpdir)
    db_path = root / "clinic.sqlite3"
    init_db(db_path)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "insert into patients(patient_identity, display_name, updated_at, current_hash) "
            "values ('p1', '张三', '2026-06-07 00:00:00', 'h1')"
        )
        # 重名两条
        conn.execute(
            "insert into patients(patient_identity, display_name, updated_at, current_hash) "
            "values ('p2', '李四', '2026-06-07 00:00:00', 'h2')"
        )
        conn.execute(
            "insert into patients(patient_identity, display_name, updated_at, current_hash) "
            "values ('p3', '李四', '2026-06-07 00:00:00', 'h3')"
        )
        conn.commit()

    records_dir = root / "records"
    day_dir = records_dir / "2026-06-07"
    day_dir.mkdir(parents=True)
    (day_dir / "rec_match.md").write_text(RECORD_MATCH, encoding="utf-8")
    (day_dir / "rec_unmatched.md").write_text(RECORD_UNMATCHED, encoding="utf-8")
    (day_dir / "rec_dup.md").write_text(RECORD_DUP, encoding="utf-8")

    mapping_path = root / "mapping.json"
    mapping_path.write_text(
        json.dumps(
            {"{NAME_001}": "张三", "{NAME_DUP}": "李四"},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return db_path, records_dir, mapping_path


class IngestAiRecordsTest(unittest.TestCase):
    def test_match_unclaimed_split_and_report_counts_only(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path, records_dir, mapping_path = _setup(tmpdir)

            report = ingest_ai_records(
                db_path=db_path,
                records_dir=records_dir,
                date="2026-06-07",
                mapping_path=mapping_path,
            )

            with sqlite3.connect(db_path) as conn:
                med_count = conn.execute(
                    "select count(*) from medical_records"
                ).fetchone()[0]
                unclaimed_count = conn.execute(
                    "select count(*) from ai_unclaimed_drafts"
                ).fetchone()[0]
                med = conn.execute(
                    "select patient_identity, draft_status, origin, doctor_name, "
                    "content_json, tooth_json, ai_meta_json from medical_records"
                ).fetchone()

        # 计数报告，无姓名/正文
        self.assertEqual(report["files_scanned"], 3)
        self.assertEqual(report["parsed"], 3)
        self.assertEqual(report["matched"], 1)
        self.assertEqual(report["unclaimed"], 2)  # 无映射(404) + 重名(李四)
        self.assertEqual(report["skipped_dup"], 0)
        self.assertEqual(report["errors"], 0)
        report_blob = json.dumps(report, ensure_ascii=False)
        self.assertNotIn("张三", report_blob)
        self.assertNotIn("李四", report_blob)
        self.assertNotIn("洁牙", report_blob)

        self.assertEqual(med_count, 1)
        self.assertEqual(unclaimed_count, 2)

        # 匹配记录落库正确
        self.assertEqual(med[0], "p1")
        self.assertEqual(med[1], "ai_draft")
        self.assertEqual(med[2], "ai_voice")
        self.assertEqual(med[3], "{NAME_006}")
        content = json.loads(med[4])
        self.assertIn("46┐冷热刺激痛", content["PC"])
        self.assertIn("深龋", content["DG"])
        tooth = json.loads(med[5])
        self.assertIn("46┐", tooth["positions"])
        ai_meta = json.loads(med[6])
        self.assertEqual(ai_meta["room"], "诊室1")
        self.assertEqual(ai_meta["code_name"], "{NAME_001}")
        self.assertIn("收费明细", ai_meta["extras"])

    def test_current_hash_matches_canonical_snapshot_570(self):
        # AI摄入写的 current_hash 必须跟 api.py 更新病历用的 medical_record_snapshot()
        # 口径一致，否则首次本地编辑会拿两套不同公式的哈希比较，恒判"有变化"，写出不可校验版本行。
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path, records_dir, mapping_path = _setup(tmpdir)
            ingest_ai_records(db_path=db_path, records_dir=records_dir,
                               date="2026-06-07", mapping_path=mapping_path)
            with sqlite3.connect(db_path) as conn:
                conn.row_factory = sqlite3.Row
                row = conn.execute("select * from medical_records").fetchone()
            self.assertEqual(row["current_hash"], stable_hash(medical_record_snapshot(row)))

    def test_rerun_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path, records_dir, mapping_path = _setup(tmpdir)

            ingest_ai_records(
                db_path=db_path,
                records_dir=records_dir,
                date="2026-06-07",
                mapping_path=mapping_path,
            )
            report2 = ingest_ai_records(
                db_path=db_path,
                records_dir=records_dir,
                date="2026-06-07",
                mapping_path=mapping_path,
            )

            with sqlite3.connect(db_path) as conn:
                med_count = conn.execute(
                    "select count(*) from medical_records"
                ).fetchone()[0]
                unclaimed_count = conn.execute(
                    "select count(*) from ai_unclaimed_drafts"
                ).fetchone()[0]

        # 重跑：全部跳过，不重复落库
        self.assertEqual(report2["skipped_dup"], 3)
        self.assertEqual(report2["matched"], 0)
        self.assertEqual(report2["unclaimed"], 0)
        self.assertEqual(med_count, 1)
        self.assertEqual(unclaimed_count, 2)

    def test_no_mapping_routes_all_to_unclaimed(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path, records_dir, _ = _setup(tmpdir)

            report = ingest_ai_records(
                db_path=db_path,
                records_dir=records_dir,
                date="2026-06-07",
                mapping_path=None,
            )

            with sqlite3.connect(db_path) as conn:
                med_count = conn.execute(
                    "select count(*) from medical_records"
                ).fetchone()[0]
                unclaimed_count = conn.execute(
                    "select count(*) from ai_unclaimed_drafts"
                ).fetchone()[0]

        self.assertEqual(report["matched"], 0)
        self.assertEqual(report["unclaimed"], 3)
        self.assertEqual(med_count, 0)
        self.assertEqual(unclaimed_count, 3)

    def test_real_name_directly_matches_without_mapping(self):
        # 真实语音管线输出：「姓名」字段直填真名（非脱敏代号），无需映射即应命中。
        # 这是 matched=0 线上 bug 的回归保护：旧逻辑只走映射，真名直填永不命中。
        real_name_record = (
            "姓名: 张三\n性别: 男\n年龄: 40\n就诊时间: 2026-06-07 09:30\n"
            "主诊医生: 王医生\n诊室: 诊室1\n\n【主诉】 牙痛。\n"
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path, records_dir, _ = _setup(tmpdir)
            (records_dir / "2026-06-07" / "rec_realname.md").write_text(
                real_name_record, encoding="utf-8"
            )

            report = ingest_ai_records(
                db_path=db_path,
                records_dir=records_dir,
                date="2026-06-07",
                mapping_path=None,  # 没有映射也要命中
            )

            with sqlite3.connect(db_path) as conn:
                matched_patient = conn.execute(
                    "select patient_identity from medical_records "
                    "where draft_status = 'ai_draft'"
                ).fetchone()

        # 4 份：原 3 份（无映射全 unclaimed）+ 这份真名直配命中 1
        self.assertEqual(report["matched"], 1)
        self.assertEqual(matched_patient[0], "p1")

    def test_missing_date_dir_yields_zero_scanned(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path, records_dir, mapping_path = _setup(tmpdir)

            report = ingest_ai_records(
                db_path=db_path,
                records_dir=records_dir,
                date="2099-01-01",
                mapping_path=mapping_path,
            )

        self.assertEqual(report["files_scanned"], 0)
        self.assertEqual(report["matched"], 0)

    # --- K1: mapping 非字符串值 不崩、不中断整批、不回滚其他文件 ---
    def test_non_string_mapping_value_does_not_break_batch(self):
        for bad_value in ({"x": 1}, ["a", "b"], 123):
            with self.subTest(bad_value=bad_value), \
                    tempfile.TemporaryDirectory() as tmpdir:
                db_path, records_dir, _ = _setup(tmpdir)
                # 映射里 {NAME_001} 是非字符串值；{NAME_DUP} 正常映射到重名
                mapping_path = Path(tmpdir) / "bad_mapping.json"
                mapping_path.write_text(
                    json.dumps(
                        {"{NAME_001}": bad_value, "{NAME_DUP}": "李四"},
                        ensure_ascii=False,
                    ),
                    encoding="utf-8",
                )

                report = ingest_ai_records(
                    db_path=db_path,
                    records_dir=records_dir,
                    date="2026-06-07",
                    mapping_path=mapping_path,
                )

                with sqlite3.connect(db_path) as conn:
                    med_count = conn.execute(
                        "select count(*) from medical_records"
                    ).fetchone()[0]
                    unclaimed_count = conn.execute(
                        "select count(*) from ai_unclaimed_drafts"
                    ).fetchone()[0]

                # 三个文件都被处理（非字符串映射 -> unclaimed），不崩、不中断
                self.assertEqual(report["files_scanned"], 3)
                self.assertEqual(report["errors"], 0)
                self.assertEqual(report["matched"], 0)
                self.assertEqual(report["unclaimed"], 3)
                self.assertEqual(med_count, 0)
                self.assertEqual(unclaimed_count, 3)

    # --- K1: 单文件写库异常 计入 errors、不中断、不回滚其他文件 ---
    def test_write_error_on_one_file_counts_error_and_keeps_others(self):
        from unittest import mock
        import local_app.ingest_ai_records as mod

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path, records_dir, mapping_path = _setup(tmpdir)

            real_write = mod._write_medical_record

            def boom(conn, patient_identity, parsed, source_file, batch_id):
                raise RuntimeError("write failed")

            with mock.patch.object(mod, "_write_medical_record", boom):
                report = ingest_ai_records(
                    db_path=db_path,
                    records_dir=records_dir,
                    date="2026-06-07",
                    mapping_path=mapping_path,
                )

            with sqlite3.connect(db_path) as conn:
                med_count = conn.execute(
                    "select count(*) from medical_records"
                ).fetchone()[0]
                unclaimed_count = conn.execute(
                    "select count(*) from ai_unclaimed_drafts"
                ).fetchone()[0]

        # 匹配文件写库失败 -> errors+1，其余两条 unclaimed 仍落库
        self.assertEqual(report["files_scanned"], 3)
        self.assertEqual(report["errors"], 1)
        self.assertEqual(report["matched"], 0)
        self.assertEqual(report["unclaimed"], 2)
        self.assertEqual(med_count, 0)
        self.assertEqual(unclaimed_count, 2)

    # --- 错误留痕不得把真名文件名/异常message泄进report(CLI打印)或日志 ---
    def test_error_files_redacts_source_names(self):
        from unittest import mock
        import local_app.ingest_ai_records as mod

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path, records_dir, mapping_path = _setup(tmpdir)

            def boom(conn, patient_identity, parsed, source_file, batch_id):
                raise RuntimeError("write failed")

            with mock.patch.object(mod, "_write_medical_record", boom):
                report = ingest_ai_records(
                    db_path=db_path,
                    records_dir=records_dir,
                    date="2026-06-07",
                    mapping_path=mapping_path,
                )

            self.assertEqual(report["errors"], 1)
            entry = report["error_files"][0]
            self.assertNotIn("rec_match", entry)      # 文件名(生产环境含患者真名)不进报告
            self.assertNotIn("write failed", entry)   # 异常message不进报告
            self.assertIn("RuntimeError", entry)      # 异常类型可见,足够分流排障
            self.assertIn("hash=", entry)             # 短hash供人工比对定位
            # 明文映射只落 db 同目录(gitignored data区)诊断文件,人工定位用
            diag = Path(db_path).parent / "ingest_errors.log"
            self.assertTrue(diag.exists())
            self.assertIn("rec_match", diag.read_text())

    # --- Z2: 跨日期同名文件 各自落库，不互相 skip ---
    def test_same_name_across_dates_both_persisted(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path, records_dir, _ = _setup(tmpdir)
            # 在第二个日期目录放同名文件 rec_match.md
            day2 = Path(records_dir) / "2026-06-08"
            day2.mkdir(parents=True)
            (day2 / "rec_match.md").write_text(RECORD_MATCH, encoding="utf-8")

            mapping_path = Path(tmpdir) / "m.json"
            mapping_path.write_text(
                json.dumps({"{NAME_001}": "张三"}, ensure_ascii=False),
                encoding="utf-8",
            )

            r1 = ingest_ai_records(
                db_path=db_path, records_dir=records_dir,
                date="2026-06-07", mapping_path=mapping_path,
            )
            r2 = ingest_ai_records(
                db_path=db_path, records_dir=records_dir,
                date="2026-06-08", mapping_path=mapping_path,
            )

            with sqlite3.connect(db_path) as conn:
                med_count = conn.execute(
                    "select count(*) from medical_records"
                ).fetchone()[0]
                source_files = {
                    json.loads(m or "{}").get("source_file")
                    for (m,) in conn.execute(
                        "select ai_meta_json from medical_records"
                    )
                }

        # 两个日期下的 rec_match.md 都各自落库，不被去重
        self.assertEqual(r1["matched"], 1)
        self.assertEqual(r1["skipped_dup"], 0)
        self.assertEqual(r2["matched"], 1)
        self.assertEqual(r2["skipped_dup"], 0)
        self.assertEqual(med_count, 2)
        self.assertIn("2026-06-07/rec_match.md", source_files)
        self.assertIn("2026-06-08/rec_match.md", source_files)

    # --- Z1: 统一去重键（两表都用含日期相对路径），medical_records 侧也去重 ---
    def test_idempotent_unified_key_covers_medical_records(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path, records_dir, mapping_path = _setup(tmpdir)

            ingest_ai_records(
                db_path=db_path, records_dir=records_dir,
                date="2026-06-07", mapping_path=mapping_path,
            )
            report2 = ingest_ai_records(
                db_path=db_path, records_dir=records_dir,
                date="2026-06-07", mapping_path=mapping_path,
            )

            with sqlite3.connect(db_path) as conn:
                med_count = conn.execute(
                    "select count(*) from medical_records"
                ).fetchone()[0]
                unclaimed_count = conn.execute(
                    "select count(*) from ai_unclaimed_drafts"
                ).fetchone()[0]
                # 匹配记录的 source_file 含日期相对路径
                med_sf = conn.execute(
                    "select ai_meta_json from medical_records"
                ).fetchone()[0]

        self.assertEqual(report2["skipped_dup"], 3)
        self.assertEqual(report2["matched"], 0)
        self.assertEqual(report2["unclaimed"], 0)
        self.assertEqual(med_count, 1)
        self.assertEqual(unclaimed_count, 2)
        self.assertEqual(
            json.loads(med_sf)["source_file"], "2026-06-07/rec_match.md"
        )

    # --- C5: 坏 md（parser 抛异常）计入 errors、不中断其他文件 ---
    def test_bad_markdown_counts_error_keeps_others(self):
        from unittest import mock
        import local_app.ingest_ai_records as mod

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path, records_dir, mapping_path = _setup(tmpdir)

            real_parse = mod.parse_ai_record_markdown

            def maybe_boom(text):
                if "{NAME_404}" in text:
                    raise ValueError("bad markdown")
                return real_parse(text)

            with mock.patch.object(mod, "parse_ai_record_markdown", maybe_boom):
                report = ingest_ai_records(
                    db_path=db_path, records_dir=records_dir,
                    date="2026-06-07", mapping_path=mapping_path,
                )

            with sqlite3.connect(db_path) as conn:
                med_count = conn.execute(
                    "select count(*) from medical_records"
                ).fetchone()[0]
                unclaimed_count = conn.execute(
                    "select count(*) from ai_unclaimed_drafts"
                ).fetchone()[0]

        # 坏文件 errors+1，其余正常：1 matched + 1 unclaimed
        self.assertEqual(report["files_scanned"], 3)
        self.assertEqual(report["errors"], 1)
        self.assertEqual(report["parsed"], 2)
        self.assertEqual(report["matched"], 1)
        self.assertEqual(report["unclaimed"], 1)
        self.assertEqual(med_count, 1)
        self.assertEqual(unclaimed_count, 1)

    # --- C5: mapping 文件不存在 -> 友好报错或归为无映射，不整批崩 ---
    def test_missing_mapping_file_does_not_crash(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path, records_dir, _ = _setup(tmpdir)
            missing = Path(tmpdir) / "nope.json"  # 不存在

            report = ingest_ai_records(
                db_path=db_path, records_dir=records_dir,
                date="2026-06-07", mapping_path=missing,
            )

            with sqlite3.connect(db_path) as conn:
                med_count = conn.execute(
                    "select count(*) from medical_records"
                ).fetchone()[0]
                unclaimed_count = conn.execute(
                    "select count(*) from ai_unclaimed_drafts"
                ).fetchone()[0]

        # 映射文件缺失 -> 视为无映射，全部归 unclaimed，不崩
        self.assertEqual(report["files_scanned"], 3)
        self.assertEqual(report["matched"], 0)
        self.assertEqual(report["unclaimed"], 3)
        self.assertEqual(med_count, 0)
        self.assertEqual(unclaimed_count, 3)


if __name__ == "__main__":
    unittest.main()


class CliMissingDirTest(unittest.TestCase):
    def test_all_with_missing_records_dir_exits_zero_with_hint(self):
        # 干净安装无 data/ai_records 时,--all 必须友好空态退出,不得 FileNotFoundError
        import subprocess
        import sys
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "clinic.sqlite3"
            init_db(db)
            r = subprocess.run(
                [sys.executable, "-m", "local_app.ingest_ai_records",
                 "--records-dir", str(Path(tmp) / "不存在的目录"), "--all", "--db", str(db)],
                capture_output=True, text=True, timeout=60,
                cwd=str(Path(__file__).resolve().parent.parent))
            self.assertEqual(r.returncode, 0, f"应按0文件正常退出: {r.stderr[-300:]}")
            self.assertNotIn("Traceback", r.stderr, "不得抛栈")
            self.assertIn("草稿目录不存在", r.stdout, "应给清晰空态提示")
