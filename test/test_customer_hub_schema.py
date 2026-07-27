import sqlite3
import tempfile
import unittest
from pathlib import Path

from local_app.db import connect, init_db


class CustomerHubSchemaTest(unittest.TestCase):
    @staticmethod
    def _columns(conn, table):
        return {row[1]: row for row in conn.execute(f'pragma table_info("{table}")')}

    @staticmethod
    def _index_columns(conn, index):
        return tuple(row[2] for row in conn.execute(f'pragma index_info("{index}")'))

    @staticmethod
    def _drop_column_indexes(conn, table, column):
        for index_row in conn.execute(f'pragma index_list("{table}")').fetchall():
            index = index_row[1]
            if column in CustomerHubSchemaTest._index_columns(conn, index):
                quoted = index.replace('"', '""')
                conn.execute(f'drop index "{quoted}"')

    def test_fresh_schema_contract(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "clinic.sqlite3"
            init_db(db)
            with connect(db) as conn:
                tables = {row[0] for row in conn.execute(
                    "select name from sqlite_master where type='table'")}
                self.assertTrue({
                    "call_transcripts", "attachment_file_ops", "customer_hub_tasks",
                }.issubset(tables))

                return_visit_columns = self._columns(conn, "return_visits")
                communication_columns = self._columns(conn, "communications")
                call_columns = self._columns(conn, "calls")
                self.assertEqual(
                    (return_visit_columns["channel"][2].lower(),
                     return_visit_columns["channel"][3],
                     return_visit_columns["channel"][4]),
                    ("text", 1, "''"),
                )
                for columns in (return_visit_columns, communication_columns, call_columns):
                    self.assertEqual(
                        (columns["revision"][2].lower(), columns["revision"][3],
                         columns["revision"][4]),
                        ("integer", 1, "1"),
                    )
                for name in ("direction", "reason"):
                    column = communication_columns[name]
                    self.assertEqual((column[2].lower(), column[3], column[4]),
                                     ("text", 1, "''"))

                sessions_expires_at = self._columns(conn, "sessions")["expires_at"]
                self.assertEqual(sessions_expires_at[3], 0)

                transcript_columns = set(self._columns(conn, "call_transcripts"))
                self.assertEqual(transcript_columns, {
                    "call_id", "status", "text", "segments_json", "model", "error",
                    "duration_sec", "summary_status", "summary_json", "summary_model",
                    "summary_error", "created_at", "updated_at",
                })
                transcript_fks = [tuple(row) for row in conn.execute(
                    'pragma foreign_key_list("call_transcripts")')]
                self.assertTrue(any(
                    row[2] == "calls" and row[3] == "call_id" and row[4] == "call_id"
                    and row[6].upper() == "CASCADE"
                    for row in transcript_fks
                ))

                task_columns = self._columns(conn, "customer_hub_tasks")
                self.assertEqual(task_columns["task_seq"][5], 1)
                self.assertEqual(task_columns["task_id"][3], 1)
                task_id_unique = any(
                    row[2] == 1 and self._index_columns(conn, row[1]) == ("task_id",)
                    for row in conn.execute('pragma index_list("customer_hub_tasks")')
                )
                self.assertTrue(task_id_unique)

                attachment_indexes = {
                    row[1]: row for row in conn.execute(
                        'pragma index_list("attachment_file_ops")')
                }
                parent_index = attachment_indexes["idx_attachment_file_ops_parent"]
                self.assertEqual(parent_index[2], 1)
                self.assertEqual(
                    self._index_columns(conn, "idx_attachment_file_ops_parent"),
                    ("parent_type", "parent_id"),
                )
                self.assertEqual(
                    self._index_columns(conn, "idx_calls_linked_rv"),
                    ("linked_return_visit_id",),
                )
                self.assertEqual(
                    self._index_columns(conn, "idx_calls_linked_communication"),
                    ("linked_communication_id",),
                )

                conflict_columns = self._columns(conn, "import_conflicts")
                self.assertEqual(
                    (
                        conflict_columns["conflict_key"][2].lower(),
                        conflict_columns["conflict_key"][3],
                        conflict_columns["conflict_key"][4],
                    ),
                    ("text", 1, "''"),
                )
                self.assertEqual(
                    (
                        conflict_columns["reason_code"][2].lower(),
                        conflict_columns["reason_code"][3],
                        conflict_columns["reason_code"][4],
                    ),
                    ("text", 1, "'value_conflict'"),
                )
                conflict_index = {
                    row[1]: row
                    for row in conn.execute('pragma index_list("import_conflicts")')
                }["idx_import_conflicts_conflict_key"]
                self.assertEqual(conflict_index[2], 1)
                self.assertEqual(conflict_index[4], 1)
                self.assertEqual(
                    self._index_columns(conn, "idx_import_conflicts_conflict_key"),
                    ("conflict_key",),
                )
                index_sql = conn.execute(
                    "select sql from sqlite_master where type='index' and name=?",
                    ("idx_import_conflicts_conflict_key",),
                ).fetchone()[0]
                self.assertIn(
                    "where conflict_key <> ''",
                    " ".join(index_sql.lower().split()),
                )

    def test_attachment_file_ops_rejects_second_operation_for_parent(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "clinic.sqlite3"
            init_db(db)
            with connect(db) as conn:
                conn.execute(
                    "insert into attachment_file_ops "
                    "(operation_id,parent_type,parent_id,attachment_id,expected_revision,"
                    "operation_type,status) values (?,?,?,?,?,?,?)",
                    ("synthetic-op-1", "call", "synthetic-parent", "synthetic-attachment-1",
                     1, "publish", "staging_publish"),
                )
                with self.assertRaises(sqlite3.IntegrityError):
                    conn.execute(
                        "insert into attachment_file_ops "
                        "(operation_id,parent_type,parent_id,attachment_id,expected_revision,"
                        "operation_type,status) values (?,?,?,?,?,?,?)",
                        ("synthetic-op-2", "call", "synthetic-parent",
                         "synthetic-attachment-2", 1, "delete", "staging_delete"),
                    )

    def test_customer_hub_task_sequence_is_not_reused_after_delete(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "clinic.sqlite3"
            init_db(db)
            with connect(db) as conn:
                task_values = ("transcription", "call", "synthetic-call", 1,
                               "service", "queued")
                conn.execute(
                    "insert into customer_hub_tasks "
                    "(task_id,task_type,target_type,target_id,target_revision,actor_type,status) "
                    "values (?,?,?,?,?,?,?)",
                    ("synthetic-task-a", *task_values),
                )
                task_a_seq = conn.execute(
                    "select task_seq from customer_hub_tasks where task_id='synthetic-task-a'"
                ).fetchone()[0]
                conn.execute(
                    "delete from customer_hub_tasks where task_id='synthetic-task-a'"
                )
                conn.execute(
                    "insert into customer_hub_tasks "
                    "(task_id,task_type,target_type,target_id,target_revision,actor_type,status) "
                    "values (?,?,?,?,?,?,?)",
                    ("synthetic-task-b", *task_values),
                )
                task_b_seq = conn.execute(
                    "select task_seq from customer_hub_tasks where task_id='synthetic-task-b'"
                ).fetchone()[0]
                self.assertGreater(task_b_seq, task_a_seq)

    def test_legacy_schema_upgrade_is_idempotent_and_preserves_rows(self):
        drops = (
            ("return_visits", "channel"),
            ("return_visits", "revision"),
            ("communications", "direction"),
            ("communications", "reason"),
            ("communications", "revision"),
            ("calls", "revision"),
        )
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "clinic.sqlite3"
            init_db(db)
            with connect(db) as conn:
                for table, column in drops:
                    self._drop_column_indexes(conn, table, column)
                    conn.execute(f'alter table "{table}" drop column "{column}"')
                for table in ("call_transcripts", "attachment_file_ops", "customer_hub_tasks"):
                    conn.execute(f'drop table "{table}"')
                conn.execute(
                    "insert into patients (patient_identity,current_hash) values (?,?)",
                    ("synthetic-patient", "synthetic-hash"),
                )
                conn.execute(
                    "insert into return_visits (return_visit_id,patient_identity) values (?,?)",
                    ("synthetic-rv", "synthetic-patient"),
                )
                conn.execute(
                    "insert into communications (communication_id,patient_identity) values (?,?)",
                    ("synthetic-comm", "synthetic-patient"),
                )
                conn.execute(
                    "insert into calls (call_id,patient_identity) values (?,?)",
                    ("synthetic-call", "synthetic-patient"),
                )
                conn.commit()

            init_db(db)
            init_db(db)

            with connect(db) as conn:
                tables = {row[0] for row in conn.execute(
                    "select name from sqlite_master where type='table'")}
                self.assertTrue({
                    "call_transcripts", "attachment_file_ops", "customer_hub_tasks",
                }.issubset(tables))
                self.assertEqual(conn.execute(
                    "select revision from return_visits where return_visit_id='synthetic-rv'"
                ).fetchone()[0], 1)
                self.assertEqual(conn.execute(
                    "select revision from communications "
                    "where communication_id='synthetic-comm'"
                ).fetchone()[0], 1)
                self.assertEqual(conn.execute(
                    "select revision from calls where call_id='synthetic-call'"
                ).fetchone()[0], 1)
                self.assertEqual(conn.execute("select count(*) from patients").fetchone()[0], 1)
                self.assertEqual(conn.execute("select count(*) from return_visits").fetchone()[0], 1)

    def test_legacy_schema_adds_linked_communication_index_idempotently(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "clinic.sqlite3"
            init_db(db)
            with connect(db) as conn:
                sessions_expires_at = self._columns(conn, "sessions")["expires_at"]
                conn.execute("drop index if exists idx_calls_linked_communication")
                conn.execute(
                    "insert into patients(patient_identity,current_hash) values (?,?)",
                    ("synthetic-index-patient", "synthetic-index-hash"),
                )
                conn.execute(
                    "insert into communications(communication_id,patient_identity) "
                    "values (?,?)",
                    ("synthetic-index-comm", "synthetic-index-patient"),
                )
                conn.execute(
                    "insert into calls(call_id,patient_identity,linked_communication_id) "
                    "values (?,?,?)",
                    (
                        "synthetic-index-call",
                        "synthetic-index-patient",
                        "synthetic-index-comm",
                    ),
                )
                conn.commit()

            init_db(db)
            init_db(db)

            with connect(db) as conn:
                self.assertEqual(
                    self._index_columns(conn, "idx_calls_linked_communication"),
                    ("linked_communication_id",),
                )
                self.assertEqual(
                    conn.execute(
                        "select linked_communication_id from calls "
                        "where call_id = 'synthetic-index-call'"
                    ).fetchone()[0],
                    "synthetic-index-comm",
                )
                self.assertEqual(
                    self._columns(conn, "sessions")["expires_at"],
                    sessions_expires_at,
                )


if __name__ == "__main__":
    unittest.main()
