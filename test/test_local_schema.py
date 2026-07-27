import unittest
import sqlite3
import tempfile
from pathlib import Path

from local_app.db import connect, init_db


class PrivacyBoundaryTest(unittest.TestCase):
    def test_gitignore_keeps_local_patient_data_out_of_git(self):
        gitignore = Path(".gitignore").read_text(encoding="utf-8")

        required_patterns = [
            "data/raw/",
            "data/masked/",
            "*.db",
            "*.sqlite",
            "*.sqlite3",
            "local_app/data/",
            "local_app/backups/",
            "local_app/logs/",
        ]

        for pattern in required_patterns:
            with self.subTest(pattern=pattern):
                self.assertIn(pattern, gitignore)


class LocalSchemaTest(unittest.TestCase):
    def test_init_db_creates_core_tables(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "clinic.sqlite3"
            init_db(db_path)

            with sqlite3.connect(db_path) as conn:
                tables = {
                    row[0]
                    for row in conn.execute(
                        "select name from sqlite_master where type='table'"
                    )
                }

            self.assertTrue(
                {
                    "sync_batches",
                    "patients",
                    "patient_versions",
                    "medical_records",
                    "appointments",
                    "return_visits",
                    "bills",
                    "payments",
                    "treatment_items",
                    "image_download_tasks",
                    "local_notes",
                    "audit_logs",
                    "import_conflicts",
                }.issubset(tables)
            )

    def test_image_download_tasks_table_tracks_local_archive_jobs(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "clinic.sqlite3"
            init_db(db_path)

            with sqlite3.connect(db_path) as conn:
                columns = {
                    row[1]
                    for row in conn.execute("pragma table_info(image_download_tasks)")
                }

            self.assertTrue(
                {
                    "task_id",
                    "patient_identity",
                    "source_url",
                    "upload_date",
                    "image_type",
                    "archive_path",
                    "status",
                    "error_text",
                }.issubset(columns)
            )

    def test_treatment_items_table_has_billdetail_fields_and_indexes(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "clinic.sqlite3"
            init_db(db_path)

            with sqlite3.connect(db_path) as conn:
                columns = {
                    row[1]
                    for row in conn.execute("pragma table_info(treatment_items)")
                }
                indexes = {
                    row[0]
                    for row in conn.execute(
                        "select name from sqlite_master where type='index'"
                    )
                }

            self.assertTrue(
                {
                    "treatment_item_id",
                    "patient_identity",
                    "bill_id",
                    "item_name",
                    "item_code",
                    "handleset_identity",
                    "doctor_name",
                    "unit_price",
                    "quantity",
                    "total_fee",
                    "fee_type",
                    "is_deleted",
                    "is_refund",
                    "source_json",
                    "updated_at",
                    "last_batch_id",
                }.issubset(columns)
            )
            self.assertTrue(
                {
                    "idx_treatment_items_patient",
                    "idx_treatment_items_bill",
                }.issubset(indexes)
            )

    def test_patient_images_table_has_local_image_fields_and_indexes(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "clinic.sqlite3"
            init_db(db_path)

            with sqlite3.connect(db_path) as conn:
                columns = {
                    row[1]
                    for row in conn.execute("pragma table_info(patient_images)")
                }
                indexes = {
                    row[0]
                    for row in conn.execute(
                        "select name from sqlite_master where type='index'"
                    )
                }
                unique_indexes = {
                    row[1]
                    for row in conn.execute("pragma index_list(patient_images)")
                    if row[2] == 1
                }

            self.assertTrue(
                {
                    "image_id",
                    "patient_identity",
                    "image_date",
                    "seq",
                    "category",
                    "file_name",
                    "rel_path",
                    "size_bytes",
                    "content_hash",
                    "source_folder",
                    "last_batch_id",
                    "created_at",
                }.issubset(columns)
            )
            self.assertTrue(
                {
                    "idx_patient_images_rel_path",
                    "idx_patient_images_patient",
                }.issubset(indexes)
            )
            self.assertIn("idx_patient_images_rel_path", unique_indexes)

    def test_patient_table_has_identity_and_contact_fields(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "clinic.sqlite3"
            init_db(db_path)

            with sqlite3.connect(db_path) as conn:
                columns = {
                    row[1]
                    for row in conn.execute("pragma table_info(patients)")
                }

            self.assertTrue(
                {
                    "patient_identity",
                    "source_customer_id",
                    "display_name",
                    "phone",
                    "updated_at",
                    "current_hash",
                }.issubset(columns)
            )

    def test_medical_records_has_ai_draft_columns(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "clinic.sqlite3"
            init_db(db_path)

            with sqlite3.connect(db_path) as conn:
                columns = {
                    row[1]
                    for row in conn.execute("pragma table_info(medical_records)")
                }

            self.assertTrue(
                {"draft_status", "origin", "ai_meta_json"}.issubset(columns)
            )

    def test_column_migration_adds_missing_columns_to_legacy_db(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "clinic.sqlite3"
            # 模拟存量库：没有新三列的 medical_records 表
            with sqlite3.connect(db_path) as conn:
                conn.execute(
                    """
                    create table medical_records (
                        record_id text primary key,
                        patient_identity text not null,
                        study_identity text,
                        record_type text,
                        visit_time text,
                        doctor_name text,
                        tooth_json text not null default '{}',
                        content_json text not null default '{}',
                        created_at text,
                        updated_at text,
                        current_hash text not null,
                        last_batch_id text
                    )
                    """
                )
                conn.commit()

            init_db(db_path)

            with sqlite3.connect(db_path) as conn:
                columns = {
                    row[1]
                    for row in conn.execute("pragma table_info(medical_records)")
                }

            self.assertTrue(
                {"draft_status", "origin", "ai_meta_json"}.issubset(columns)
            )

    def test_ai_unclaimed_drafts_table_and_indexes(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "clinic.sqlite3"
            init_db(db_path)

            with sqlite3.connect(db_path) as conn:
                tables = {
                    row[0]
                    for row in conn.execute(
                        "select name from sqlite_master where type='table'"
                    )
                }
                columns = {
                    row[1]
                    for row in conn.execute("pragma table_info(ai_unclaimed_drafts)")
                }
                indexes = {
                    row[0]
                    for row in conn.execute(
                        "select name from sqlite_master where type='index'"
                    )
                }
                unique_indexes = {
                    row[1]
                    for row in conn.execute("pragma index_list(ai_unclaimed_drafts)")
                    if row[2] == 1
                }

            self.assertIn("ai_unclaimed_drafts", tables)
            self.assertTrue(
                {
                    "unclaimed_id",
                    "code_name",
                    "visit_time",
                    "room",
                    "doctor_name",
                    "content_json",
                    "tooth_json",
                    "ai_meta_json",
                    "source_file",
                    "status",
                    "created_at",
                }.issubset(columns)
            )
            self.assertTrue(
                {
                    "idx_ai_unclaimed_drafts_source_file",
                    "idx_ai_unclaimed_drafts_status",
                }.issubset(indexes)
            )
            self.assertIn("idx_ai_unclaimed_drafts_source_file", unique_indexes)

    def test_connect_returns_row_objects(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "clinic.sqlite3"
            init_db(db_path)

            with connect(db_path) as conn:
                conn.execute(
                    """
                    insert into sync_batches(batch_id, source, status, started_at)
                    values ('b1', 'unit', 'done', '2026-06-07 00:00:00')
                    """
                )
                row = conn.execute(
                    "select source from sync_batches where batch_id = 'b1'"
                ).fetchone()

            self.assertEqual(row["source"], "unit")


if __name__ == "__main__":
    unittest.main()
