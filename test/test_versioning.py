import sqlite3
import tempfile
import unittest
from pathlib import Path

from local_app.db import init_db
from local_app.models import PatientSnapshot
from local_app.versioning import stable_hash, upsert_patient_with_version


class PatientVersioningTest(unittest.TestCase):
    def test_stable_hash_ignores_key_order(self):
        left = {"name": "测试患者", "phone": "10086"}
        right = {"phone": "10086", "name": "测试患者"}

        self.assertEqual(stable_hash(left), stable_hash(right))

    def test_upsert_patient_saves_previous_snapshot_on_change(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "clinic.sqlite3"
            init_db(db_path)

            with sqlite3.connect(db_path) as conn:
                conn.execute(
                    "insert into sync_batches(batch_id, source, status, started_at) "
                    "values (?, ?, ?, ?)",
                    ("batch-1", "unit", "done", "2026-06-07 10:00:00"),
                )
                conn.execute(
                    "insert into sync_batches(batch_id, source, status, started_at) "
                    "values (?, ?, ?, ?)",
                    ("batch-2", "unit", "done", "2026-06-07 11:00:00"),
                )

                upsert_patient_with_version(
                    conn,
                    PatientSnapshot(
                        patient_identity="p1",
                        source_customer_id="c1",
                        display_name="测试患者",
                        phone="10086",
                        source={"patient_identity": "p1", "phone": "10086"},
                        updated_at="2026-06-07 10:00:00",
                    ),
                    batch_id="batch-1",
                )
                upsert_patient_with_version(
                    conn,
                    PatientSnapshot(
                        patient_identity="p1",
                        source_customer_id="c1",
                        display_name="测试患者",
                        phone="10010",
                        source={"patient_identity": "p1", "phone": "10010"},
                        updated_at="2026-06-07 11:00:00",
                    ),
                    batch_id="batch-2",
                )

                phone = conn.execute(
                    "select phone from patients where patient_identity = ?",
                    ("p1",),
                ).fetchone()[0]
                versions = conn.execute(
                    "select count(*) from patient_versions where patient_identity = ?",
                    ("p1",),
                ).fetchone()[0]

        self.assertEqual(phone, "10010")
        self.assertEqual(versions, 1)

    def test_archived_version_hash_consistent_with_source_json(self):
        # 归档到 patient_versions 的 version_hash 必须与被归档的 source_json 自洽,
        # 不能直接抄可能已被本地编辑/合并偏离的 current_hash
        import json
        from local_app.versioning import stable_json
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "clinic.sqlite3"
            init_db(db_path)
            with sqlite3.connect(db_path) as conn:
                conn.execute("insert into sync_batches(batch_id,source,status,started_at) "
                             "values('b1','unit','done','2026-01-01 00:00:00')")
                conn.execute("insert into sync_batches(batch_id,source,status,started_at) "
                             "values('b2','unit','running','2026-02-01 00:00:00')")
                # 一行本地编辑过的患者:current_hash 与 source_json 已偏离
                conn.execute(
                    "insert into patients(patient_identity,display_name,phone,updated_at,"
                    "current_hash,source_json,last_batch_id) "
                    "values('p1','张三','100','2026-01-01','LIVE_EDITED_HASH',?, 'b1')",
                    (stable_json({"y": 2}),))
                # 重跑导入:source 变了(进归档分支),5个档案字段与现有一致(diffs空→跳冲突→归档)
                upsert_patient_with_version(conn, PatientSnapshot(
                    patient_identity="p1", source_customer_id="c1", display_name="张三",
                    phone="100", source={"x": 1}, updated_at="2026-02-01",
                ), batch_id="b2")
                v = conn.execute("select version_hash, source_json from patient_versions "
                                 "where patient_identity='p1'").fetchone()
            self.assertIsNotNone(v)
            self.assertEqual(v[0], stable_hash(json.loads(v[1])))  # 归档行自洽
            self.assertNotEqual(v[0], "LIVE_EDITED_HASH")          # 不再抄偏离的 current_hash

    def test_upsert_empty_customerid_keeps_existing_binding(self):
        # 重导入带空 customerid 不能静默清空已绑定的 source_customer_id
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "clinic.sqlite3"
            init_db(db_path)
            with sqlite3.connect(db_path) as conn:
                conn.execute("insert into sync_batches(batch_id, source, status, started_at) "
                             "values ('b1','unit','done','2026-06-07 10:00:00')")
                conn.execute("insert into sync_batches(batch_id, source, status, started_at) "
                             "values ('b2','unit','done','2026-06-07 11:00:00')")
                upsert_patient_with_version(conn, PatientSnapshot(
                    patient_identity="p1", source_customer_id="c1", display_name="测试患者",
                    phone="10086", source={"patient_identity": "p1"}, updated_at="2026-06-07 10:00:00",
                ), batch_id="b1")
                # 再次导入同一患者,但 customerid 为空(外部系统 该字段缺失)
                upsert_patient_with_version(conn, PatientSnapshot(
                    patient_identity="p1", source_customer_id="", display_name="测试患者改",
                    phone="10086", source={"patient_identity": "p1", "x": 1}, updated_at="2026-06-07 11:00:00",
                ), batch_id="b2")
                cid = conn.execute("select source_customer_id from patients where patient_identity='p1'").fetchone()[0]
                name = conn.execute("select display_name from patients where patient_identity='p1'").fetchone()[0]
        self.assertEqual(cid, "c1", "空 customerid 不应清空已绑定的 c1")
        self.assertEqual(name, "测试患者改", "其它字段仍正常更新")


if __name__ == "__main__":
    unittest.main()
