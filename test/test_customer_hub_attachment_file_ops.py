"""Recoverable customer-hub attachment file operations use synthetic data only."""
from __future__ import annotations

import os
import hashlib
import io
import json
import sqlite3
import tempfile
import unittest
from unittest import mock
from pathlib import Path

from fastapi import HTTPException

from local_app.attachment_file_ops import (
    AttachmentConsistencyError,
    commit_delete,
    commit_publish,
    hidden_attachment_ids,
    publish_staged_file,
    recover_attachment_file_ops,
    require_no_pending_attachment_op,
    stage_delete,
    stage_publish,
    tombstone_staged_file,
    write_staged_bytes,
)
from local_app.db import connect, init_db
from customer_hub_access_fixture import CustomerHubAccessFixture


_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
_WAV = b"RIFF\x24\x00\x00\x00WAVEfmt " + b"\x00" * 28
_COMPLETE_HASH = hashlib.sha256(b"complete").hexdigest()


class AttachmentFileOpsFixture:
    def __init__(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db_path = self.root / "synthetic.sqlite3"
        self.images_dir = self.root / "images"
        self.images_dir.mkdir()
        init_db(self.db_path)
        with connect(self.db_path) as conn:
            conn.execute(
                "insert into patients(patient_identity, display_name, current_hash) "
                "values ('synthetic-patient', 'Synthetic patient', 'hash')"
            )
            conn.execute(
                "insert into return_visits(return_visit_id, patient_identity, "
                "due_time, item_name, revision) values "
                "('synthetic-rv', 'synthetic-patient', '2026-07-21', "
                "'Synthetic visit', 1)"
            )
            conn.execute(
                "insert into communications(communication_id, patient_identity, "
                "channel, direction, revision) values "
                "('synthetic-comm', 'synthetic-patient', 'wechat', '', 1)"
            )
            conn.execute(
                "insert into calls(call_id, patient_identity, direction, rel_path, "
                "revision) values ('synthetic-call', 'synthetic-patient', "
                "'outbound', 'calls/synthetic-call.wav', 1)"
            )
            conn.commit()

    def close(self):
        self.temp.cleanup()

    def paths(self, name="attachment.bin", *, operation_type="publish"):
        if operation_type == "publish":
            return {
                "temp_rel_path": f".attachment-staging/{name}.tmp",
                "final_rel_path": f"return-visit-images/{name}",
                "tombstone_rel_path": "",
            }
        if operation_type == "delete":
            return {
                "temp_rel_path": "",
                "final_rel_path": f"return-visit-images/{name}",
                "tombstone_rel_path": f".attachment-tombstones/{name}.deleted",
            }
        raise ValueError("unsupported synthetic operation type")

    def insert_op(
        self,
        *,
        status,
        operation_type=None,
        business=False,
        temp="missing",
        final="missing",
        tombstone="missing",
        name="attachment.bin",
    ):
        operation_type = operation_type or (
            "delete"
            if status in ("staging_delete", "delete_committed")
            else "publish"
        )
        paths = self.paths(name, operation_type=operation_type)
        with connect(self.db_path) as conn:
            conn.execute(
                "insert into attachment_file_ops(operation_id, parent_type, parent_id, "
                "attachment_id, expected_revision, operation_type, status, "
                "temp_rel_path, final_rel_path, tombstone_rel_path) "
                "values ('synthetic-op', 'return_visit', 'synthetic-rv', "
                "'synthetic-image', 1, ?, ?, ?, ?, ?)",
                (
                    operation_type,
                    status,
                    paths["temp_rel_path"],
                    paths["final_rel_path"],
                    paths["tombstone_rel_path"],
                ),
            )
            if business:
                conn.execute(
                    "insert into return_visit_images(image_id, return_visit_id, "
                    "patient_identity, rel_path, file_name, size_bytes, content_hash) "
                    "values ('synthetic-image', 'synthetic-rv', 'synthetic-patient', "
                    "?, ?, 8, ?)",
                    (paths["final_rel_path"], name, _COMPLETE_HASH),
                )
            conn.commit()

        self._make_shape(paths, temp=temp, final=final, tombstone=tombstone)
        return paths

    def _write(self, rel, data):
        path = self.images_dir / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def _make_shape(self, paths, *, temp, final, tombstone):
        temp_path = self.images_dir / paths["temp_rel_path"]
        final_path = self.images_dir / paths["final_rel_path"]
        tombstone_path = self.images_dir / paths["tombstone_rel_path"]
        if temp in ("complete", "partial", "same_as_final"):
            self._write(paths["temp_rel_path"], b"complete" if temp != "partial" else b"part")
        if tombstone == "complete":
            self._write(paths["tombstone_rel_path"], b"complete")
        if final == "same_as_temp":
            final_path.parent.mkdir(parents=True, exist_ok=True)
            os.link(temp_path, final_path)
        elif final == "same_as_tombstone":
            final_path.parent.mkdir(parents=True, exist_ok=True)
            os.link(tombstone_path, final_path)
        elif final == "different_inode":
            self._write(paths["final_rel_path"], b"preexisting")
        elif final == "complete":
            self._write(paths["final_rel_path"], b"complete")
        return temp_path, final_path, tombstone_path

    def op_count(self):
        with connect(self.db_path) as conn:
            return conn.execute(
                "select count(*) from attachment_file_ops"
            ).fetchone()[0]

    def business_count(self):
        with connect(self.db_path) as conn:
            return conn.execute(
                "select count(*) from return_visit_images "
                "where image_id='synthetic-image'"
            ).fetchone()[0]


class AttachmentRecoveryTest(unittest.TestCase):
    def setUp(self):
        self.fixture = AttachmentFileOpsFixture()

    def tearDown(self):
        self.fixture.close()

    def test_recovery_converges_all_recoverable_file_operation_states(self):
        cases = (
            ("staging_publish", False, "missing", "missing", "missing"),
            ("staging_publish", False, "complete", "missing", "missing"),
            ("staging_publish", False, "partial", "missing", "missing"),
            ("pending_publish", False, "same_as_final", "same_as_temp", "missing"),
            ("publish_committed", True, "complete", "same_as_temp", "missing"),
            ("staging_delete", True, "missing", "missing", "complete"),
            ("staging_delete", True, "missing", "complete", "missing"),
            ("staging_delete", True, "missing", "same_as_tombstone", "complete"),
            ("delete_committed", False, "missing", "missing", "complete"),
        )
        for index, (status, business, temp, final, tombstone) in enumerate(cases):
            with self.subTest(status=status, temp=temp):
                if index:
                    self.fixture.close()
                    self.fixture = AttachmentFileOpsFixture()
                paths = self.fixture.insert_op(
                    status=status,
                    business=business,
                    temp=temp,
                    final=final,
                    tombstone=tombstone,
                )
                result = recover_attachment_file_ops(
                    self.fixture.db_path, self.fixture.images_dir
                )
                self.assertEqual(result["blocked"], 0)
                self.assertEqual(self.fixture.op_count(), 0)
                final_path = self.fixture.images_dir / paths["final_rel_path"]
                self.assertEqual(final_path.exists(), business)
                with connect(self.fixture.db_path) as conn:
                    require_no_pending_attachment_op(
                        conn,
                        parent_type="return_visit",
                        parent_id="synthetic-rv",
                    )

    def test_pending_publish_preserves_different_inode_final(self):
        paths = self.fixture.insert_op(
            status="pending_publish",
            business=False,
            temp="complete",
            final="different_inode",
            tombstone="missing",
        )
        temp_path = self.fixture.images_dir / paths["temp_rel_path"]
        final_path = self.fixture.images_dir / paths["final_rel_path"]
        self.assertFalse(os.path.samefile(temp_path, final_path))
        recover_attachment_file_ops(self.fixture.db_path, self.fixture.images_dir)
        self.assertEqual(final_path.read_bytes(), b"preexisting")
        self.assertFalse(temp_path.exists())
        self.assertEqual(self.fixture.op_count(), 0)

    def test_inconsistent_committed_states_keep_log_and_block(self):
        cases = (
            ("publish_committed", True, "missing", "missing"),
            ("publish_committed", False, "complete", "missing"),
            ("delete_committed", True, "missing", "complete"),
            ("delete_committed", False, "complete", "missing"),
        )
        for index, (status, business, final, tombstone) in enumerate(cases):
            with self.subTest(status=status, business=business, final=final):
                if index:
                    self.fixture.close()
                    self.fixture = AttachmentFileOpsFixture()
                self.fixture.insert_op(
                    status=status,
                    business=business,
                    temp="missing",
                    final=final,
                    tombstone=tombstone,
                )
                with self.assertRaisesRegex(
                    AttachmentConsistencyError, "attachment_consistency_error"
                ):
                    recover_attachment_file_ops(
                        self.fixture.db_path, self.fixture.images_dir
                    )
                self.assertEqual(self.fixture.op_count(), 1)
                with connect(self.fixture.db_path) as conn:
                    with self.assertRaises(HTTPException) as raised:
                        require_no_pending_attachment_op(
                            conn,
                            parent_type="return_visit",
                            parent_id="synthetic-rv",
                        )
                self.assertEqual(raised.exception.status_code, 409)

    def test_staging_delete_inconsistent_file_shapes_keep_log_and_block(self):
        cases = (
            ("missing", "missing"),
            ("different_inode", "complete"),
        )
        for index, (final, tombstone) in enumerate(cases):
            with self.subTest(final=final, tombstone=tombstone):
                if index:
                    self.fixture.close()
                    self.fixture = AttachmentFileOpsFixture()
                self.fixture.insert_op(
                    status="staging_delete",
                    business=True,
                    final=final,
                    tombstone=tombstone,
                )
                with self.assertRaisesRegex(
                    AttachmentConsistencyError, "attachment_consistency_error"
                ):
                    recover_attachment_file_ops(
                        self.fixture.db_path, self.fixture.images_dir
                    )
                self.assertEqual(self.fixture.op_count(), 1)

    def test_publish_committed_business_path_mismatch_keeps_log_and_blocks(self):
        self.fixture.insert_op(
            status="publish_committed",
            business=True,
            temp="complete",
            final="same_as_temp",
        )
        with connect(self.fixture.db_path) as conn:
            conn.execute(
                "update return_visit_images set rel_path='return-visit-images/other.bin' "
                "where image_id='synthetic-image'"
            )
            conn.commit()
        with self.assertRaisesRegex(
            AttachmentConsistencyError, "attachment_consistency_error"
        ):
            recover_attachment_file_ops(
                self.fixture.db_path, self.fixture.images_dir
            )
        self.assertEqual(self.fixture.op_count(), 1)

    def test_publish_committed_rejects_different_inode_content(self):
        paths = self.fixture.insert_op(
            status="publish_committed",
            business=True,
            temp="complete",
            final="different_inode",
        )
        final_path = self.fixture.images_dir / paths["final_rel_path"]
        self.assertFalse(
            os.path.samefile(
                self.fixture.images_dir / paths["temp_rel_path"], final_path
            )
        )
        with self.assertRaisesRegex(
            AttachmentConsistencyError, "attachment_consistency_error"
        ):
            recover_attachment_file_ops(
                self.fixture.db_path, self.fixture.images_dir
            )
        self.assertEqual(final_path.read_bytes(), b"preexisting")
        self.assertEqual(self.fixture.op_count(), 1)

    def test_publish_committed_rejects_wrong_business_size_or_hash(self):
        mutations = (
            ("size_bytes", 999),
            ("content_hash", "0" * 64),
        )
        for index, (column, value) in enumerate(mutations):
            with self.subTest(column=column):
                if index:
                    self.fixture.close()
                    self.fixture = AttachmentFileOpsFixture()
                self.fixture.insert_op(
                    status="publish_committed",
                    business=True,
                    temp="complete",
                    final="same_as_temp",
                )
                with connect(self.fixture.db_path) as conn:
                    conn.execute(
                        f"update return_visit_images set {column}=? "
                        "where image_id='synthetic-image'",
                        (value,),
                    )
                    conn.commit()
                with self.assertRaisesRegex(
                    AttachmentConsistencyError, "attachment_consistency_error"
                ):
                    recover_attachment_file_ops(
                        self.fixture.db_path, self.fixture.images_dir
                    )
                self.assertEqual(self.fixture.op_count(), 1)

    def test_operation_type_and_status_mismatch_is_a_neutral_consistency_error(self):
        self.fixture.insert_op(
            status="staging_delete",
            operation_type="publish",
            business=True,
            temp="complete",
            final="complete",
            tombstone="missing",
        )
        with self.assertRaisesRegex(
            AttachmentConsistencyError, "attachment_consistency_error"
        ):
            recover_attachment_file_ops(
                self.fixture.db_path, self.fixture.images_dir
            )
        self.assertEqual(self.fixture.op_count(), 1)

    def test_staging_delete_is_hidden_before_file_move(self):
        self.fixture.insert_op(
            status="staging_delete", business=True, name="hidden.png"
        )
        with connect(self.fixture.db_path) as conn:
            self.assertEqual(
                hidden_attachment_ids(
                    conn,
                    parent_type="return_visit",
                    parent_id="synthetic-rv",
                ),
                frozenset({"synthetic-image"}),
            )

    def test_recovery_is_idempotent_and_closes_connection(self):
        self.fixture.insert_op(
            status="staging_publish", temp="partial", name="idempotent.bin"
        )
        first = recover_attachment_file_ops(
            self.fixture.db_path, self.fixture.images_dir
        )
        second = recover_attachment_file_ops(
            self.fixture.db_path, self.fixture.images_dir
        )
        self.assertEqual(first["recovered"], 1)
        self.assertEqual(second, {"recovered": 0, "blocked": 0, "states": {}})
        os.replace(self.fixture.db_path, self.fixture.root / "moved.sqlite3")


class AttachmentOperationHelperTest(unittest.TestCase):
    def setUp(self):
        self.fixture = AttachmentFileOpsFixture()

    def tearDown(self):
        self.fixture.close()

    def test_publish_callbacks_and_committed_state_are_one_transaction(self):
        paths = self.fixture.paths("publish.png")
        with connect(self.fixture.db_path) as conn:
            operation_id = stage_publish(
                conn,
                parent_type="return_visit",
                parent_id="synthetic-rv",
                attachment_id="synthetic-image",
                expected_revision=1,
                paths=paths,
            )
            conn.commit()
        temp_path = self.fixture._write(paths["temp_rel_path"], b"payload")
        with connect(self.fixture.db_path) as conn:
            conn.execute(
                "update attachment_file_ops set status='pending_publish' "
                "where operation_id=?",
                (operation_id,),
            )
            conn.commit()
        publish_staged_file(
            self.fixture.db_path,
            self.fixture.images_dir,
            operation_id,
        )
        with connect(self.fixture.db_path) as conn:
            conn.execute("begin immediate")
            commit_publish(
                conn,
                operation_id=operation_id,
                insert_business_row=lambda: conn.execute(
                    "insert into return_visit_images(image_id, return_visit_id, "
                    "patient_identity, rel_path, size_bytes, content_hash) values "
                    "('synthetic-image', 'synthetic-rv', 'synthetic-patient', ?, 7, ?)",
                    (
                        paths["final_rel_path"],
                        hashlib.sha256(b"payload").hexdigest(),
                    ),
                ),
                write_version=lambda: conn.execute(
                    "insert into return_visit_versions(return_visit_id, version_hash, "
                    "snapshot_json) values ('synthetic-rv', 'hash', '{}')"
                ),
                write_audit=lambda: conn.execute(
                    "insert into audit_logs(entity_type, entity_id, action, operator) "
                    "values ('return_visit', 'synthetic-rv', 'upload_image', 'synthetic')"
                ),
            )
            conn.commit()
        with connect(self.fixture.db_path) as conn:
            row = conn.execute(
                "select status from attachment_file_ops where operation_id=?",
                (operation_id,),
            ).fetchone()
            self.assertEqual(row["status"], "publish_committed")
        self.assertTrue(temp_path.exists())
        result = recover_attachment_file_ops(
            self.fixture.db_path, self.fixture.images_dir
        )
        self.assertEqual(result["recovered"], 1)
        self.assertFalse(temp_path.exists())

    def test_callback_failure_rolls_back_business_version_audit_and_state(self):
        paths = self.fixture.paths("rollback.png")
        with connect(self.fixture.db_path) as conn:
            operation_id = stage_publish(
                conn,
                parent_type="return_visit",
                parent_id="synthetic-rv",
                attachment_id="synthetic-image",
                expected_revision=1,
                paths=paths,
            )
            conn.execute(
                "update attachment_file_ops set status='pending_publish' "
                "where operation_id=?",
                (operation_id,),
            )
            conn.commit()
        self.fixture._write(paths["temp_rel_path"], b"payload")
        publish_staged_file(
            self.fixture.db_path, self.fixture.images_dir, operation_id
        )
        with self.assertRaises(RuntimeError):
            with connect(self.fixture.db_path) as conn:
                conn.execute("begin immediate")
                commit_publish(
                    conn,
                    operation_id=operation_id,
                    insert_business_row=lambda: conn.execute(
                        "insert into return_visit_images(image_id, return_visit_id, "
                        "patient_identity, rel_path) values "
                        "('synthetic-image', 'synthetic-rv', 'synthetic-patient', ?)",
                        (paths["final_rel_path"],),
                    ),
                    write_version=lambda: (_ for _ in ()).throw(
                        RuntimeError("synthetic failure")
                    ),
                    write_audit=lambda: None,
                )
        self.assertEqual(self.fixture.business_count(), 0)
        with connect(self.fixture.db_path) as conn:
            self.assertEqual(
                conn.execute(
                    "select status from attachment_file_ops"
                ).fetchone()["status"],
                "pending_publish",
            )

    def test_delete_moves_then_commits_and_recovers_cleanup(self):
        publish_paths = self.fixture.insert_op(
            status="publish_committed",
            business=True,
            final="complete",
            name="delete.png",
        )
        paths = self.fixture.paths("delete.png", operation_type="delete")
        self.assertEqual(paths["final_rel_path"], publish_paths["final_rel_path"])
        with connect(self.fixture.db_path) as conn:
            conn.execute("delete from attachment_file_ops")
            operation_id = stage_delete(
                conn,
                parent_type="return_visit",
                parent_id="synthetic-rv",
                attachment_id="synthetic-image",
                expected_revision=1,
                paths=paths,
            )
            conn.commit()
        tombstone_staged_file(
            self.fixture.db_path, self.fixture.images_dir, operation_id
        )
        with connect(self.fixture.db_path) as conn:
            conn.execute("begin immediate")
            commit_delete(
                conn,
                operation_id=operation_id,
                delete_business_row=lambda: conn.execute(
                    "delete from return_visit_images where image_id='synthetic-image'"
                ),
                write_version=lambda: None,
                write_audit=lambda: None,
            )
            conn.commit()
        recover_attachment_file_ops(self.fixture.db_path, self.fixture.images_dir)
        self.assertEqual(self.fixture.business_count(), 0)
        self.assertEqual(self.fixture.op_count(), 0)
        self.assertFalse((self.fixture.images_dir / paths["final_rel_path"]).exists())
        self.assertFalse((self.fixture.images_dir / paths["tombstone_rel_path"]).exists())

    def test_publish_is_no_clobber(self):
        paths = self.fixture.paths("no-clobber.png")
        with connect(self.fixture.db_path) as conn:
            operation_id = stage_publish(
                conn,
                parent_type="return_visit",
                parent_id="synthetic-rv",
                attachment_id="synthetic-image",
                expected_revision=1,
                paths=paths,
            )
            conn.execute(
                "update attachment_file_ops set status='pending_publish' "
                "where operation_id=?",
                (operation_id,),
            )
            conn.commit()
        self.fixture._write(paths["temp_rel_path"], b"new")
        final_path = self.fixture._write(paths["final_rel_path"], b"original")
        with self.assertRaises(FileExistsError):
            publish_staged_file(
                self.fixture.db_path, self.fixture.images_dir, operation_id
            )
        self.assertEqual(final_path.read_bytes(), b"original")

    def test_paths_reject_absolute_traversal_and_symlink_escape(self):
        outside = self.fixture.root / "outside"
        outside.mkdir()
        (self.fixture.images_dir / "escape").symlink_to(outside, target_is_directory=True)
        bad_paths = (
            {**self.fixture.paths(), "temp_rel_path": str(outside / "abs.tmp")},
            {**self.fixture.paths(), "final_rel_path": "../outside/file.bin"},
            {**self.fixture.paths(), "tombstone_rel_path": "escape/file.bin"},
        )
        for paths in bad_paths:
            with self.subTest(paths=tuple(paths)):
                with connect(self.fixture.db_path) as conn:
                    with self.assertRaises(AttachmentConsistencyError):
                        stage_publish(
                            conn,
                            parent_type="return_visit",
                            parent_id="synthetic-rv",
                            attachment_id="synthetic-image",
                            expected_revision=1,
                            paths=paths,
                            images_dir=self.fixture.images_dir,
                        )

    def test_paths_reject_symlinks_to_other_files_inside_images_root(self):
        cases = (
            (
                stage_publish,
                {
                    **self.fixture.paths("temp-link.bin"),
                    "temp_rel_path": ".attachment-staging/temp-link.bin",
                },
                "temp_rel_path",
            ),
            (
                stage_publish,
                {
                    **self.fixture.paths("final-link.bin"),
                    "final_rel_path": "return-visit-images/final-link.bin",
                },
                "final_rel_path",
            ),
            (
                stage_delete,
                {
                    **self.fixture.paths(
                        "tombstone-link.bin", operation_type="delete"
                    ),
                    "tombstone_rel_path": ".attachment-tombstones/tombstone-link.bin",
                },
                "tombstone_rel_path",
            ),
        )
        for index, (stage, paths, link_key) in enumerate(cases):
            with self.subTest(stage=stage.__name__, link_key=link_key):
                if index:
                    self.fixture.close()
                    self.fixture = AttachmentFileOpsFixture()
                victim = self.fixture._write(
                    "protected/victim.bin", b"protected"
                )
                link = self.fixture.images_dir / paths[link_key]
                link.parent.mkdir(parents=True, exist_ok=True)
                link.symlink_to(victim)
                with connect(self.fixture.db_path) as conn:
                    with self.assertRaisesRegex(
                        AttachmentConsistencyError, "attachment_consistency_error"
                    ):
                        stage(
                            conn,
                            parent_type="return_visit",
                            parent_id="synthetic-rv",
                            attachment_id="synthetic-image",
                            expected_revision=1,
                            paths=paths,
                            images_dir=self.fixture.images_dir,
                        )
                link.unlink()
                self.assertEqual(victim.read_bytes(), b"protected")

    def test_recovery_rejects_internal_temp_symlink_without_deleting_target(self):
        victim = self.fixture._write("protected/recovery-victim.bin", b"protected")
        temp_rel = ".attachment-staging/forged-link.tmp"
        temp_link = self.fixture.images_dir / temp_rel
        temp_link.parent.mkdir(parents=True, exist_ok=True)
        temp_link.symlink_to(victim)
        with connect(self.fixture.db_path) as conn:
            conn.execute(
                "insert into attachment_file_ops(operation_id, parent_type, parent_id, "
                "attachment_id, expected_revision, operation_type, status, "
                "temp_rel_path, final_rel_path, tombstone_rel_path) values "
                "('forged-symlink-op', 'return_visit', 'synthetic-rv', "
                "'synthetic-image', 1, 'publish', 'staging_publish', ?, ?, '')",
                (temp_rel, "return-visit-images/forged.bin"),
            )
            conn.commit()
        with self.assertRaisesRegex(
            AttachmentConsistencyError, "attachment_consistency_error"
        ):
            recover_attachment_file_ops(
                self.fixture.db_path, self.fixture.images_dir
            )
        self.assertEqual(victim.read_bytes(), b"protected")
        self.assertTrue(temp_link.is_symlink())
        self.assertEqual(self.fixture.op_count(), 1)

    def test_path_resolution_failures_are_neutral_consistency_errors(self):
        loop = self.fixture.images_dir / "loop"
        loop.symlink_to(loop)
        for bad_path in ("loop/file.bin", "bad\x00name.bin"):
            with self.subTest(bad_path=repr(bad_path)):
                paths = {
                    **self.fixture.paths("neutral.bin"),
                    "temp_rel_path": bad_path,
                }
                with connect(self.fixture.db_path) as conn:
                    with self.assertRaisesRegex(
                        AttachmentConsistencyError, "attachment_consistency_error"
                    ):
                        stage_publish(
                            conn,
                            parent_type="return_visit",
                            parent_id="synthetic-rv",
                            attachment_id="synthetic-image",
                            expected_revision=1,
                            paths=paths,
                            images_dir=self.fixture.images_dir,
                        )

    def test_tombstone_move_is_atomic_no_clobber_under_creation_race(self):
        paths = self.fixture.insert_op(
            status="staging_delete",
            business=True,
            final="complete",
            tombstone="missing",
            name="delete-race.bin",
        )
        final_path = self.fixture.images_dir / paths["final_rel_path"]
        tombstone_path = self.fixture.images_dir / paths["tombstone_rel_path"]
        real_link = os.link

        def create_foreign_then_link(source, target, **kwargs):
            Path(target).write_bytes(b"foreign")
            return real_link(source, target, **kwargs)

        with mock.patch(
            "local_app.attachment_file_ops.os.link",
            side_effect=create_foreign_then_link,
        ):
            with self.assertRaises(FileExistsError):
                tombstone_staged_file(
                    self.fixture.db_path,
                    self.fixture.images_dir,
                    "synthetic-op",
                )
        self.assertEqual(final_path.read_bytes(), b"complete")
        self.assertEqual(tombstone_path.read_bytes(), b"foreign")
        self.assertEqual(self.fixture.op_count(), 1)

    def test_operation_paths_reject_unrelated_nonempty_fields(self):
        cases = (
            (
                stage_publish,
                {
                    **self.fixture.paths("publish.bin"),
                    "tombstone_rel_path": ".attachment-tombstones/forged.deleted",
                },
            ),
            (
                stage_delete,
                {
                    **self.fixture.paths("delete.bin", operation_type="delete"),
                    "temp_rel_path": ".attachment-staging/forged.tmp",
                },
            ),
        )
        for stage, paths in cases:
            with self.subTest(stage=stage.__name__):
                with connect(self.fixture.db_path) as conn:
                    with self.assertRaisesRegex(
                        AttachmentConsistencyError, "attachment_consistency_error"
                    ):
                        stage(
                            conn,
                            parent_type="return_visit",
                            parent_id="synthetic-rv",
                            attachment_id="synthetic-image",
                            expected_revision=1,
                            paths=paths,
                            images_dir=self.fixture.images_dir,
                        )

    def test_staged_size_and_hash_failures_are_recoverable_without_residue(self):
        cases = (
            {"expected_size": len(_PNG) + 1, "expected_hash": hashlib.sha256(_PNG).hexdigest()},
            {"expected_size": len(_PNG), "expected_hash": "0" * 64},
        )
        for index, expectations in enumerate(cases):
            with self.subTest(expectations=expectations):
                if index:
                    self.fixture.close()
                    self.fixture = AttachmentFileOpsFixture()
                paths = self.fixture.paths(f"invalid-{index}.png")
                with connect(self.fixture.db_path) as conn:
                    operation_id = stage_publish(
                        conn,
                        parent_type="return_visit",
                        parent_id="synthetic-rv",
                        attachment_id="synthetic-image",
                        expected_revision=1,
                        paths=paths,
                        images_dir=self.fixture.images_dir,
                    )
                    conn.commit()
                with self.assertRaisesRegex(
                    AttachmentConsistencyError, "attachment_consistency_error"
                ):
                    write_staged_bytes(
                        self.fixture.db_path,
                        self.fixture.images_dir,
                        operation_id,
                        _PNG,
                        **expectations,
                    )
                recovered = recover_attachment_file_ops(
                    self.fixture.db_path, self.fixture.images_dir
                )
                self.assertEqual(recovered["recovered"], 1)
                self.assertEqual(self.fixture.op_count(), 0)
                self.assertFalse(
                    (self.fixture.images_dir / paths["temp_rel_path"]).exists()
                )

    def test_allow_operation_id_never_hides_a_second_related_parent_operation(self):
        with connect(self.fixture.db_path) as conn:
            stage_publish(
                conn,
                parent_type="return_visit",
                parent_id="synthetic-rv",
                attachment_id="synthetic-image-a",
                expected_revision=1,
                paths=self.fixture.paths("first.bin"),
            )
            stage_publish(
                conn,
                parent_type="call_create",
                parent_id="synthetic-rv",
                attachment_id="synthetic-call-a",
                expected_revision=1,
                paths=self.fixture.paths("second.bin"),
            )
            allowed_id = conn.execute(
                "select operation_id from attachment_file_ops "
                "where parent_type in ('return_visit', 'call_create') "
                "and parent_id='synthetic-rv'"
            ).fetchone()["operation_id"]
            with self.assertRaises(HTTPException) as raised:
                require_no_pending_attachment_op(
                    conn,
                    parent_type="return_visit",
                    parent_id="synthetic-rv",
                    allow_operation_id=allowed_id,
                )
        self.assertEqual(raised.exception.status_code, 409)


class AttachmentRouteFixture:
    PERMISSIONS = (
        "patient.profile.view",
        "return_visit.view",
        "return_visit.manage",
        "return_visit.delete",
        "communication.view",
        "communication.manage",
        "communication.delete",
        "communication.image.view",
        "communication.image.manage",
        "call_record.view",
        "call_record.manage",
        "call_record.delete",
    )

    def __init__(self):
        self.access = CustomerHubAccessFixture()
        with connect(self.access.db_path) as conn:
            conn.execute(
                "insert into patients(patient_identity, display_name, current_hash) "
                "values ('attachment-patient', 'Synthetic attachment patient', 'hash')"
            )
            conn.execute(
                "insert into return_visits(return_visit_id, patient_identity, due_time, "
                "item_name, revision) values ('attachment-rv', 'attachment-patient', "
                "'2026-07-21', 'Synthetic visit', 1)"
            )
            conn.execute(
                "insert into return_visit_versions(return_visit_id, version_hash, "
                "snapshot_json) values ('attachment-rv', 'initial-hash', '{}')"
            )
            conn.execute(
                "insert into communications(communication_id, patient_identity, channel, "
                "direction, revision) values ('attachment-comm', 'attachment-patient', "
                "'phone', 'outbound', 1)"
            )
            conn.execute(
                "insert into calls(call_id, patient_identity, direction, source, rel_path, "
                "file_name, revision) values ('attachment-call', 'attachment-patient', "
                "'outbound', 'manual_import', 'call-recordings/existing.wav', "
                "'existing.wav', 1)"
            )
            conn.commit()
        existing = self.access.images_dir / "call-recordings" / "existing.wav"
        existing.parent.mkdir(parents=True)
        existing.write_bytes(_WAV)
        self.client = self.access.client_with_permissions(self.PERMISSIONS)

    @property
    def db_path(self):
        return self.access.db_path

    @property
    def images_dir(self):
        return self.access.images_dir

    def close(self):
        self.access.close()

    def scalar(self, sql, params=()):
        with connect(self.db_path) as conn:
            return conn.execute(sql, params).fetchone()[0]

    def revision(self, table, id_col, entity_id):
        return self.scalar(
            f"select revision from {table} where {id_col}=?", (entity_id,)
        )

    def business_digest(self):
        with connect(self.db_path) as conn:
            rows = tuple(
                tuple(row)
                for row in conn.execute(
                    "select return_visit_id, revision, is_deleted from return_visits "
                    "union all select communication_id, revision, 0 from communications "
                    "union all select call_id, revision, 0 from calls order by 1"
                ).fetchall()
            )
            counts = tuple(
                conn.execute(f"select count(*) from {table}").fetchone()[0]
                for table in (
                    "return_visit_images",
                    "communication_images",
                    "return_visit_versions",
                    "audit_logs",
                    "attachment_file_ops",
                )
            )
        files = tuple(
            sorted(
                (
                    str(path.relative_to(self.images_dir)),
                    hashlib.sha256(path.read_bytes()).hexdigest(),
                )
                for path in self.images_dir.rglob("*")
                if path.is_file()
            )
        )
        return rows, counts, files

    def upload_communication_image(self, expected_revision=1, client=None):
        return (client or self.client).post(
            "/api/communications/attachment-comm/images",
            files={"file": ("synthetic.png", io.BytesIO(_PNG), "image/png")},
            data={"expected_revision": str(expected_revision)},
        )

    def upload_return_visit_image(self, expected_revision=1, client=None):
        return (client or self.client).post(
            "/api/return-visits/attachment-rv/images",
            files={"file": ("synthetic.png", io.BytesIO(_PNG), "image/png")},
            data={"expected_revision": str(expected_revision)},
        )

    def upload_call(self, expected_revision=1, client=None, **context):
        return (client or self.client).post(
            "/api/patients/attachment-patient/calls",
            files={"file": ("synthetic.wav", io.BytesIO(_WAV), "audio/wav")},
            data={
                "direction": "outbound",
                "source": "manual_import",
                "expected_revision": str(expected_revision),
                **context,
            },
        )


class AttachmentRouteContractTest(unittest.TestCase):
    def setUp(self):
        self.fixture = AttachmentRouteFixture()

    def tearDown(self):
        self.fixture.close()

    def _reset_fixture(self):
        self.fixture.close()
        self.fixture = AttachmentRouteFixture()

    def _attachment_case(self, case_name, *, client=None):
        from local_app.routes import calls, communications, return_visit_ops

        client = client or self.fixture.client
        if case_name == "communication_add":
            return communications, "POST", "/api/communications/attachment-comm/images", {
                "files": {"file": ("synthetic.png", io.BytesIO(_PNG), "image/png")},
                "data": {"expected_revision": "1"},
            }
        if case_name == "communication_delete":
            image_id = self.fixture.upload_communication_image().json()["image_id"]
            return communications, "DELETE", f"/api/communications/attachment-comm/images/{image_id}", {
                "json": {"expected_revision": 2},
            }
        if case_name == "return_visit_add":
            return return_visit_ops, "POST", "/api/return-visits/attachment-rv/images", {
                "files": {"file": ("synthetic.png", io.BytesIO(_PNG), "image/png")},
                "data": {"expected_revision": "1"},
            }
        if case_name == "return_visit_delete":
            image_id = self.fixture.upload_return_visit_image().json()["image_id"]
            return return_visit_ops, "DELETE", f"/api/return-visits/attachment-rv/images/{image_id}", {
                "json": {"expected_revision": 2},
            }
        if case_name == "call_add":
            return calls, "POST", "/api/patients/attachment-patient/calls", {
                "files": {"file": ("synthetic.wav", io.BytesIO(_WAV), "audio/wav")},
                "data": {
                    "direction": "outbound",
                    "linked_communication_id": "attachment-comm",
                    "expected_revision": "1",
                },
            }
        if case_name == "call_delete":
            return calls, "DELETE", "/api/calls/attachment-call", {
                "json": {"expected_revision": 1},
            }
        raise AssertionError(case_name)

    def test_communication_image_publish_delete_increments_revision_and_cleans_log(self):
        missing = self.fixture.client.post(
            "/api/communications/attachment-comm/images",
            files={"file": ("synthetic.png", io.BytesIO(_PNG), "image/png")},
        )
        self.assertEqual(missing.status_code, 400)
        uploaded = self.fixture.upload_communication_image()
        self.assertEqual(uploaded.status_code, 200, uploaded.text)
        self.assertEqual(uploaded.json()["revision"], 2)
        image_id = uploaded.json()["image_id"]
        self.assertEqual(self.fixture.revision("communications", "communication_id", "attachment-comm"), 2)
        self.assertEqual(self.fixture.scalar("select count(*) from attachment_file_ops"), 0)
        deleted = self.fixture.client.request(
            "DELETE",
            f"/api/communications/attachment-comm/images/{image_id}",
            json={"expected_revision": 2},
        )
        self.assertEqual(deleted.status_code, 200, deleted.text)
        self.assertEqual(deleted.json()["revision"], 3)
        self.assertEqual(self.fixture.scalar("select count(*) from communication_images"), 0)
        self.assertEqual(self.fixture.scalar("select count(*) from attachment_file_ops"), 0)

    def test_return_visit_image_publish_delete_writes_final_versions(self):
        before = self.fixture.scalar(
            "select count(*) from return_visit_versions where return_visit_id='attachment-rv'"
        )
        uploaded = self.fixture.upload_return_visit_image()
        self.assertEqual(uploaded.status_code, 200, uploaded.text)
        self.assertEqual(uploaded.json()["revision"], 2)
        image_id = uploaded.json()["image_id"]
        self.assertEqual(
            self.fixture.scalar(
                "select count(*) from return_visit_versions where return_visit_id='attachment-rv'"
            ),
            before + 1,
        )
        deleted = self.fixture.client.request(
            "DELETE",
            f"/api/return-visits/attachment-rv/images/{image_id}",
            json={"expected_revision": 2},
        )
        self.assertEqual(deleted.status_code, 200, deleted.text)
        self.assertEqual(deleted.json()["revision"], 3)
        self.assertEqual(
            self.fixture.scalar(
                "select count(*) from return_visit_versions where return_visit_id='attachment-rv'"
            ),
            before + 2,
        )

    def test_manual_call_requires_exactly_one_revisioned_context(self):
        self.assertEqual(self.fixture.upload_call().status_code, 400)
        self.assertEqual(
            self.fixture.upload_call(linked_consult_id="legacy-consult").status_code,
            400,
        )
        self.assertEqual(
            self.fixture.upload_call(
                linked_communication_id="attachment-comm",
                linked_return_visit_id="attachment-rv",
            ).status_code,
            400,
        )
        created = self.fixture.upload_call(
            linked_communication_id="attachment-comm"
        )
        self.assertEqual(created.status_code, 200, created.text)
        self.assertEqual(created.json()["revision"], 1)
        self.assertEqual(created.json()["context_revision"], 2)
        self.assertEqual(
            self.fixture.revision(
                "communications", "communication_id", "attachment-comm"
            ),
            2,
        )
        self.assertEqual(self.fixture.scalar("select count(*) from attachment_file_ops"), 0)

    def test_stale_revision_and_second_lock_change_leave_no_orphans(self):
        stale = self.fixture.upload_communication_image(expected_revision=2)
        self.assertEqual(stale.status_code, 409)
        self._reset_fixture()
        cases = (
            ("communication_add", "communications", "communication_id", "attachment-comm"),
            ("communication_delete", "communications", "communication_id", "attachment-comm"),
            ("return_visit_add", "return_visits", "return_visit_id", "attachment-rv"),
            ("return_visit_delete", "return_visits", "return_visit_id", "attachment-rv"),
            ("call_add", "communications", "communication_id", "attachment-comm"),
            ("call_delete", "calls", "call_id", "attachment-call"),
        )
        for index, (case_name, table, id_column, parent_id) in enumerate(cases):
            with self.subTest(case=case_name):
                if index:
                    self._reset_fixture()
                module, method, path, kwargs = self._attachment_case(case_name)
                before_non_revision = self.fixture.business_digest()[1:]
                before_revision = self.fixture.revision(table, id_column, parent_id)
                real_begin = module.begin_immediate
                lock_count = 0

                def mutate_before_second_lock(conn):
                    nonlocal lock_count
                    lock_count += 1
                    if lock_count == 2:
                        with connect(self.fixture.db_path) as competing:
                            competing.execute(
                                f"update {table} set revision=revision+1 "
                                f"where {id_column}=?",
                                (parent_id,),
                            )
                            competing.commit()
                    return real_begin(conn)

                with mock.patch.object(
                    module, "begin_immediate", side_effect=mutate_before_second_lock
                ):
                    raced = self.fixture.client.request(method, path, **kwargs)
                self.assertGreaterEqual(lock_count, 2)
                self.assertEqual(raced.status_code, 409, raced.text)
                self.assertEqual(
                    self.fixture.revision(table, id_column, parent_id),
                    before_revision + 1,
                )
                self.assertEqual(
                    self.fixture.business_digest()[1:], before_non_revision
                )

    def test_pending_operation_hides_attachment_and_blocks_parent_writes(self):
        uploaded = self.fixture.upload_communication_image()
        image_id = uploaded.json()["image_id"]
        with connect(self.fixture.db_path) as conn:
            row = conn.execute(
                "select rel_path from communication_images where image_id=?", (image_id,)
            ).fetchone()
            stage_delete(
                conn,
                parent_type="communication",
                parent_id="attachment-comm",
                attachment_id=image_id,
                expected_revision=2,
                paths={
                    "temp_rel_path": "",
                    "final_rel_path": row["rel_path"],
                    "tombstone_rel_path": ".attachment-tombstones/hidden.deleted",
                },
                images_dir=self.fixture.images_dir,
            )
            conn.commit()
        listed = self.fixture.client.get(
            "/api/patients/attachment-patient/communications"
        )
        self.assertEqual(listed.status_code, 200)
        self.assertEqual(listed.json()["communications"][0]["images"], [])
        self.assertEqual(
            self.fixture.client.get(
                f"/api/communications/attachment-comm/images/{image_id}/file"
            ).status_code,
            404,
        )
        blocked = self.fixture.client.patch(
            "/api/communications/attachment-comm",
            json={"expected_revision": 2, "note": "blocked"},
        )
        self.assertEqual(blocked.status_code, 409)

    def test_staging_delete_hides_all_three_attachment_types_and_blocks_edits_and_deletes(self):
        cases = ("return_visit", "communication", "call")
        for index, parent_type in enumerate(cases):
            with self.subTest(parent_type=parent_type):
                if index:
                    self._reset_fixture()
                if parent_type == "return_visit":
                    uploaded = self.fixture.upload_return_visit_image().json()
                    attachment_id = uploaded["image_id"]
                    parent_id = "attachment-rv"
                    expected_revision = 2
                    table = "return_visit_images"
                    id_column = "image_id"
                elif parent_type == "communication":
                    uploaded = self.fixture.upload_communication_image().json()
                    attachment_id = uploaded["image_id"]
                    parent_id = "attachment-comm"
                    expected_revision = 2
                    table = "communication_images"
                    id_column = "image_id"
                else:
                    attachment_id = "attachment-call"
                    parent_id = "attachment-call"
                    expected_revision = 1
                    table = "calls"
                    id_column = "call_id"
                    with connect(self.fixture.db_path) as conn:
                        conn.execute(
                            "update calls set linked_return_visit_id='attachment-rv', "
                            "started_at='2026-07-20 10:00:00' where call_id=?",
                            (attachment_id,),
                        )
                        conn.commit()
                with connect(self.fixture.db_path) as conn:
                    rel_path = conn.execute(
                        f"select rel_path from {table} where {id_column}=?",
                        (attachment_id,),
                    ).fetchone()["rel_path"]
                    stage_delete(
                        conn,
                        parent_type=parent_type,
                        parent_id=parent_id,
                        attachment_id=attachment_id,
                        expected_revision=expected_revision,
                        paths={
                            "temp_rel_path": "",
                            "final_rel_path": rel_path,
                            "tombstone_rel_path": (
                                f".attachment-tombstones/{parent_type}.deleted"
                            ),
                        },
                        images_dir=self.fixture.images_dir,
                    )
                    conn.commit()

                if parent_type == "return_visit":
                    detail = self.fixture.client.get(
                        "/api/return-visits/attachment-rv"
                    )
                    self.assertEqual(detail.status_code, 200)
                    self.assertEqual(detail.json()["images"], [])
                    ledger = self.fixture.client.get(
                        "/api/return-visits?date=2026-07-21&status=all"
                    ).json()
                    row = next(
                        item for item in ledger["list"]
                        if item["return_visit_id"] == "attachment-rv"
                    )
                    self.assertEqual(row["has_screenshot"], 0)
                    self.assertEqual(
                        self.fixture.client.get(
                            "/api/return-visits?date=2026-07-21&status=all&att=screenshot"
                        ).json()["totalcount"],
                        0,
                    )
                    reads = (
                        self.fixture.client.get(
                            f"/api/return-visits/attachment-rv/images/{attachment_id}/file"
                        ),
                    )
                    writes = (
                        self.fixture.client.put(
                            "/api/return-visits/attachment-rv",
                            json={"expected_revision": 2, "note": "blocked"},
                        ),
                        self.fixture.client.request(
                            "DELETE",
                            "/api/return-visits/attachment-rv",
                            json={"expected_revision": 2},
                        ),
                    )
                elif parent_type == "communication":
                    detail = self.fixture.client.get(
                        "/api/patients/attachment-patient/communications"
                    )
                    self.assertEqual(detail.status_code, 200)
                    self.assertEqual(detail.json()["communications"][0]["images"], [])
                    reads = (
                        self.fixture.client.get(
                            f"/api/communications/attachment-comm/images/{attachment_id}/file"
                        ),
                    )
                    writes = (
                        self.fixture.client.patch(
                            "/api/communications/attachment-comm",
                            json={"expected_revision": 2, "note": "blocked"},
                        ),
                        self.fixture.client.request(
                            "DELETE",
                            "/api/communications/attachment-comm",
                            json={"expected_revision": 2},
                        ),
                    )
                else:
                    detail = self.fixture.client.get(
                        "/api/patients/attachment-patient/calls"
                    )
                    self.assertEqual(detail.status_code, 200)
                    self.assertNotIn(
                        attachment_id,
                        [row["call_id"] for row in detail.json()["calls"]],
                    )
                    ledger = self.fixture.client.get(
                        "/api/return-visits?date=2026-07-21&status=all"
                    ).json()
                    return_visit = next(
                        item for item in ledger["list"]
                        if item["return_visit_id"] == "attachment-rv"
                    )
                    self.assertEqual(return_visit["has_recording"], 0)
                    self.assertEqual(
                        self.fixture.client.get(
                            "/api/return-visits?date=2026-07-21&status=all&att=recording"
                        ).json()["totalcount"],
                        0,
                    )
                    today = self.fixture.client.get(
                        "/api/customer-hub/today?date=2026-07-20"
                    )
                    self.assertEqual(today.status_code, 200)
                    self.assertNotIn(
                        attachment_id,
                        [row["call_id"] for row in today.json()["calls"]],
                    )
                    reads = (
                        self.fixture.client.get(
                            "/api/calls/attachment-call/recording"
                        ),
                    )
                    writes = (
                        self.fixture.client.patch(
                            "/api/calls/attachment-call",
                            json={"expected_revision": 1, "note": "blocked"},
                        ),
                        self.fixture.client.request(
                            "DELETE",
                            "/api/calls/attachment-call",
                            json={"expected_revision": 1},
                        ),
                    )
                self.assertEqual([response.status_code for response in reads], [404])
                self.assertEqual(
                    [response.status_code for response in writes],
                    [409, 409],
                )

    def test_every_attachment_write_rechecks_revocation_before_first_lock(self):
        cases = (
            ("communication_add", "communication.image.manage"),
            ("communication_delete", "communication.image.manage"),
            ("return_visit_add", "return_visit.manage"),
            ("return_visit_delete", "return_visit.manage"),
            ("call_add", "call_record.manage"),
            ("call_delete", "call_record.delete"),
        )
        for index, (case_name, permission) in enumerate(cases):
            with self.subTest(case=case_name):
                if index:
                    self.fixture.close()
                    self.fixture = AttachmentRouteFixture()
                module, method, path, kwargs = self._attachment_case(case_name)
                before = (
                    self.fixture.scalar("select count(*) from attachment_file_ops"),
                    self.fixture.scalar("select count(*) from audit_logs"),
                )
                response = self.fixture.access.request_with_revocation_before_begin(
                    self.fixture.client,
                    method,
                    path,
                    permission,
                    begin_module=module,
                    **kwargs,
                )
                self.assertTrue(self.fixture.access.revocation_callback_hit)
                self.assertEqual(response.status_code, 403, response.text)
                self.assertEqual(
                    (
                        self.fixture.scalar("select count(*) from attachment_file_ops"),
                        self.fixture.scalar("select count(*) from audit_logs"),
                    ),
                    before,
                )

    def test_every_attachment_write_accepts_only_its_exact_permission(self):
        cases = (
            ("communication_add", "communication.image.manage"),
            ("communication_delete", "communication.image.manage"),
            ("return_visit_add", "return_visit.manage"),
            ("return_visit_delete", "return_visit.manage"),
            ("call_add", "call_record.manage"),
            ("call_delete", "call_record.delete"),
        )
        for index, (case_name, permission) in enumerate(cases):
            with self.subTest(case=case_name, permission=permission):
                if index:
                    self._reset_fixture()
                module, method, path, kwargs = self._attachment_case(case_name)
                exact_client = self.fixture.access.client_with_permissions((permission,))
                response = exact_client.request(method, path, **kwargs)
                self.assertEqual(response.status_code, 200, response.text)

    def test_adjacent_attachment_permissions_never_substitute_for_exact_permission(self):
        cases = (
            ("return_visit_add", "return_visit.delete"),
            ("return_visit_delete", "return_visit.delete"),
            ("communication_add", "communication.manage"),
            ("communication_add", "communication.delete"),
            ("communication_delete", "communication.manage"),
            ("communication_delete", "communication.delete"),
            ("call_add", "call_record.delete"),
            ("call_delete", "call_record.manage"),
        )
        for index, (case_name, wrong_permission) in enumerate(cases):
            with self.subTest(case=case_name, permission=wrong_permission):
                if index:
                    self._reset_fixture()
                module, method, path, kwargs = self._attachment_case(case_name)
                before = self.fixture.business_digest()
                wrong_client = self.fixture.access.client_with_permissions(
                    (wrong_permission,)
                )
                response = wrong_client.request(method, path, **kwargs)
                self.assertEqual(response.status_code, 403, response.text)
                after = self.fixture.business_digest()
                self.assertEqual(after[0], before[0])
                self.assertEqual(after[2], before[2])
                for count_index in (0, 1, 2, 4):
                    self.assertEqual(after[1][count_index], before[1][count_index])

    def test_every_attachment_write_rechecks_actor_state_without_side_effects(self):
        cases = (
            "communication_add",
            "communication_delete",
            "return_visit_add",
            "return_visit_delete",
            "call_add",
            "call_delete",
        )
        mutations = (
            (
                "inactive",
                "update users set is_active=0 where id=?",
                self.fixture.access.USER_ID,
            ),
            (
                "left",
                "update staff_members set employment_status='left' where staff_id=?",
                self.fixture.access.STAFF_ID,
            ),
        )
        case_index = 0
        for case_name in cases:
            for mutation_name, sql, value in mutations:
                with self.subTest(case=case_name, mutation=mutation_name):
                    if case_index:
                        self._reset_fixture()
                    case_index += 1
                    module, method, path, kwargs = self._attachment_case(case_name)
                    before = self.fixture.business_digest()
                    real_begin = module.begin_immediate
                    mutated = False

                    def mutate_then_begin(conn):
                        nonlocal mutated
                        if not mutated:
                            with connect(self.fixture.db_path) as mutation:
                                mutation.execute(sql, (value,))
                                mutation.commit()
                            mutated = True
                        return real_begin(conn)

                    with mock.patch.object(
                        module, "begin_immediate", side_effect=mutate_then_begin
                    ):
                        response = self.fixture.client.request(method, path, **kwargs)
                    self.assertTrue(mutated)
                    self.assertEqual(response.status_code, 403, response.text)
                    self.assertEqual(self.fixture.business_digest(), before)

    def test_every_attachment_write_rechecks_patient_soft_delete_at_both_locks(self):
        cases = (
            "communication_add",
            "communication_delete",
            "return_visit_add",
            "return_visit_delete",
            "call_add",
            "call_delete",
        )
        case_index = 0
        for mutation_lock in (1, 2):
            for case_name in cases:
                with self.subTest(case=case_name, mutation_lock=mutation_lock):
                    if case_index:
                        self._reset_fixture()
                    case_index += 1
                    module, method, path, kwargs = self._attachment_case(case_name)
                    before = self.fixture.business_digest()
                    real_begin = module.begin_immediate
                    lock_count = 0

                    def delete_patient_then_begin(conn):
                        nonlocal lock_count
                        lock_count += 1
                        if lock_count == mutation_lock:
                            with connect(self.fixture.db_path) as mutation:
                                mutation.execute(
                                    "update patients set is_deleted=1 "
                                    "where patient_identity='attachment-patient'"
                                )
                                mutation.commit()
                        return real_begin(conn)

                    with mock.patch.object(
                        module, "begin_immediate", side_effect=delete_patient_then_begin
                    ):
                        response = self.fixture.client.request(method, path, **kwargs)
                    self.assertGreaterEqual(lock_count, mutation_lock)
                    self.assertEqual(response.status_code, 404, response.text)
                    self.assertEqual(self.fixture.business_digest(), before)

    def test_invalid_attachment_signatures_leave_no_operations_or_files(self):
        cases = (
            (
                "communication",
                "POST",
                "/api/communications/attachment-comm/images",
                {
                    "files": {"file": ("synthetic.png", io.BytesIO(_WAV), "image/png")},
                    "data": {"expected_revision": "1"},
                },
            ),
            (
                "return_visit",
                "POST",
                "/api/return-visits/attachment-rv/images",
                {
                    "files": {"file": ("synthetic.png", io.BytesIO(_WAV), "image/png")},
                    "data": {"expected_revision": "1"},
                },
            ),
            (
                "call",
                "POST",
                "/api/patients/attachment-patient/calls",
                {
                    "files": {"file": ("synthetic.wav", io.BytesIO(_PNG), "audio/wav")},
                    "data": {
                        "direction": "outbound",
                        "source": "manual_import",
                        "linked_communication_id": "attachment-comm",
                        "expected_revision": "1",
                    },
                },
            ),
        )
        for index, (kind, method, path, kwargs) in enumerate(cases):
            with self.subTest(kind=kind):
                if index:
                    self._reset_fixture()
                before = self.fixture.business_digest()
                response = self.fixture.client.request(method, path, **kwargs)
                self.assertEqual(response.status_code, 400, response.text)
                self.assertEqual(self.fixture.business_digest(), before)

    def test_disk_hash_failure_is_immediately_recovered_for_all_publish_routes(self):
        cases = ("communication_add", "return_visit_add", "call_add")
        for index, case_name in enumerate(cases):
            with self.subTest(case=case_name):
                if index:
                    self._reset_fixture()
                module, method, path, kwargs = self._attachment_case(case_name)
                before = self.fixture.business_digest()
                real_write = module.write_staged_bytes

                def write_with_wrong_hash(*args, **write_kwargs):
                    write_kwargs["expected_hash"] = "0" * 64
                    return real_write(*args, **write_kwargs)

                with mock.patch.object(
                    module, "write_staged_bytes", side_effect=write_with_wrong_hash
                ):
                    response = self.fixture.client.request(method, path, **kwargs)
                self.assertEqual(response.status_code, 500, response.text)
                self.assertEqual(self.fixture.business_digest(), before)

    def test_publish_routes_preserve_racing_final_file_and_clean_staging(self):
        cases = ("communication_add", "return_visit_add", "call_add")
        for index, case_name in enumerate(cases):
            with self.subTest(case=case_name):
                if index:
                    self._reset_fixture()
                module, method, path, kwargs = self._attachment_case(case_name)
                before = self.fixture.business_digest()
                real_write = module.write_staged_bytes
                final_holder = {}

                def write_then_create_final(*args, **write_kwargs):
                    result = real_write(*args, **write_kwargs)
                    operation_id = args[2]
                    with connect(self.fixture.db_path) as conn:
                        rel_path = conn.execute(
                            "select final_rel_path from attachment_file_ops "
                            "where operation_id=?",
                            (operation_id,),
                        ).fetchone()["final_rel_path"]
                    final_path = self.fixture.images_dir / rel_path
                    final_path.parent.mkdir(parents=True, exist_ok=True)
                    final_path.write_bytes(b"foreign")
                    final_holder["path"] = final_path
                    return result

                with mock.patch.object(
                    module,
                    "write_staged_bytes",
                    side_effect=write_then_create_final,
                ):
                    response = self.fixture.client.request(method, path, **kwargs)
                self.assertEqual(response.status_code, 409, response.text)
                self.assertEqual(final_holder["path"].read_bytes(), b"foreign")
                after = self.fixture.business_digest()
                self.assertEqual(after[0], before[0])
                self.assertEqual(after[1], before[1])
                staging_files = [
                    item
                    for item in (self.fixture.images_dir / ".attachment-staging").glob("*")
                    if item.is_file()
                ]
                self.assertEqual(staging_files, [])

    def test_call_recording_delete_keeps_full_patient_audit_tombstone(self):
        response = self.fixture.client.request(
            "DELETE",
            "/api/calls/attachment-call",
            json={"expected_revision": 1},
        )
        self.assertEqual(response.status_code, 200, response.text)
        with connect(self.fixture.db_path) as conn:
            audit = conn.execute(
                "select old_json, new_json from audit_logs "
                "where entity_type='call' and entity_id='attachment-call' "
                "and action='delete_call' order by audit_id desc limit 1"
            ).fetchone()
        self.assertIsNotNone(audit)
        old_value = json.loads(audit["old_json"])
        new_value = json.loads(audit["new_json"])
        self.assertEqual(old_value["patient_identity"], "attachment-patient")
        self.assertEqual(old_value["revision"], 1)
        self.assertEqual(
            new_value,
            {"call_id": "attachment-call", "deleted": True, "revision": 2},
        )

    def test_second_lock_revocation_recovers_files_and_business_state(self):
        cases = (
            ("communication_add", "communication.image.manage"),
            ("communication_delete", "communication.image.manage"),
            ("return_visit_add", "return_visit.manage"),
            ("return_visit_delete", "return_visit.manage"),
            ("call_add", "call_record.manage"),
            ("call_delete", "call_record.delete"),
        )
        for index, (case_name, permission) in enumerate(cases):
            with self.subTest(case=case_name):
                if index:
                    self._reset_fixture()
                module, method, path, kwargs = self._attachment_case(case_name)
                before = self.fixture.business_digest()
                real_begin = module.begin_immediate
                lock_count = 0

                def revoke_before_second_lock(conn):
                    nonlocal lock_count
                    lock_count += 1
                    if lock_count == 2:
                        self.fixture.access.revoke(permission)
                    return real_begin(conn)

                with mock.patch.object(
                    module, "begin_immediate", side_effect=revoke_before_second_lock
                ):
                    response = self.fixture.client.request(method, path, **kwargs)
                self.assertGreaterEqual(lock_count, 2)
                self.assertEqual(response.status_code, 403, response.text)
                self.assertEqual(self.fixture.business_digest(), before)

    def test_audit_failure_rolls_back_each_attachment_saga(self):
        cases = (
            "communication_add",
            "communication_delete",
            "return_visit_add",
            "return_visit_delete",
            "call_add",
            "call_delete",
        )
        for index, case_name in enumerate(cases):
            with self.subTest(case=case_name):
                if index:
                    self._reset_fixture()
                _, method, path, kwargs = self._attachment_case(case_name)
                before = self.fixture.business_digest()
                with connect(self.fixture.db_path) as conn:
                    conn.execute(
                        "create trigger fail_attachment_audit before insert on audit_logs "
                        "begin select raise(abort, 'synthetic attachment audit failure'); end"
                    )
                    conn.commit()
                response = self.fixture.client.request(method, path, **kwargs)
                self.assertEqual(response.status_code, 500, response.text)
                self.assertEqual(self.fixture.business_digest(), before)


if __name__ == "__main__":
    unittest.main()
