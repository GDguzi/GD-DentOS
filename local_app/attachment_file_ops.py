"""Recoverable file-operation saga for customer-hub attachments.

SQLite and the filesystem cannot share one transaction.  This module keeps a
small, content-free operation log so an interrupted publish/delete can be
converged before the application accepts requests.
"""
from __future__ import annotations

import os
import hashlib
import stat
import uuid
from collections import Counter
from contextlib import closing
from pathlib import Path

from fastapi import HTTPException

from local_app.db import begin_immediate, connect
from local_app.timeutil import now_str


_PARENT_TYPES = frozenset(("return_visit", "communication", "call", "call_create"))
_PATH_KEYS = ("temp_rel_path", "final_rel_path", "tombstone_rel_path")


class AttachmentConsistencyError(RuntimeError):
    """A logged operation cannot be safely converged without guessing."""

    def __init__(self, message="attachment_consistency_error"):
        super().__init__(message)


def attachment_content_matches(data, extension):
    """Small signature check; callers still enforce their explicit allow-list."""
    if type(data) is not bytes or type(extension) is not str:
        return False
    extension = extension.lower()
    if extension == ".png":
        return data.startswith(b"\x89PNG\r\n\x1a\n")
    if extension in (".jpg", ".jpeg"):
        return data.startswith(b"\xff\xd8\xff")
    if extension == ".webp":
        return len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP"
    if extension == ".wav":
        return len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WAVE"
    if extension == ".mp3":
        return data.startswith(b"ID3") or (
            len(data) >= 2 and data[0] == 0xFF and data[1] & 0xE0 == 0xE0
        )
    if extension == ".ogg":
        return data.startswith(b"OggS")
    if extension == ".m4a":
        return len(data) >= 12 and data[4:8] == b"ftyp"
    if extension == ".webm":
        return data.startswith(b"\x1a\x45\xdf\xa3")
    if extension == ".amr":
        return data.startswith((b"#!AMR\n", b"#!AMR-WB\n"))
    return False


def _consistency_error():
    return AttachmentConsistencyError()


def _validate_identity(value):
    if type(value) is not str or not value or not _utf8(value):
        raise _consistency_error()
    return value


def _utf8(value):
    try:
        value.encode("utf-8")
    except (AttributeError, UnicodeEncodeError):
        return False
    return True


def _relative_path(images_dir, rel_path, *, required):
    if type(rel_path) is not str or not _utf8(rel_path):
        raise _consistency_error()
    if not rel_path:
        if required:
            raise _consistency_error()
        return None
    try:
        root = Path(images_dir).resolve()
        relative = Path(rel_path)
    except (OSError, RuntimeError, ValueError):
        raise _consistency_error() from None
    if relative.is_absolute() or ".." in relative.parts:
        raise _consistency_error()
    candidate = root / relative
    current = root
    try:
        for part in relative.parts:
            current = current / part
            try:
                mode = os.lstat(current).st_mode
            except FileNotFoundError:
                continue
            if stat.S_ISLNK(mode):
                raise _consistency_error()
    except AttachmentConsistencyError:
        raise
    except (OSError, RuntimeError, ValueError):
        raise _consistency_error() from None
    return candidate


def validate_operation_paths(images_dir, paths, *, operation_type):
    if type(paths) is not dict or set(paths) != set(_PATH_KEYS):
        raise _consistency_error()
    if operation_type == "publish":
        required = {"temp_rel_path", "final_rel_path"}
        if paths["tombstone_rel_path"]:
            raise _consistency_error()
    elif operation_type == "delete":
        required = {"final_rel_path", "tombstone_rel_path"}
        if paths["temp_rel_path"]:
            raise _consistency_error()
    else:
        raise _consistency_error()
    resolved = {
        key: _relative_path(images_dir, paths[key], required=key in required)
        for key in _PATH_KEYS
    }
    nonempty = [path for path in resolved.values() if path is not None]
    if len(nonempty) != len(set(nonempty)):
        raise _consistency_error()
    return resolved


def _stage(
    conn,
    *,
    parent_type,
    parent_id,
    attachment_id,
    expected_revision,
    paths,
    operation_type,
    images_dir=None,
):
    if parent_type not in _PARENT_TYPES:
        raise _consistency_error()
    _validate_identity(parent_id)
    _validate_identity(attachment_id)
    if type(expected_revision) is not int or expected_revision < 1:
        raise HTTPException(status_code=400, detail="invalid_expected_revision")
    if type(paths) is not dict or set(paths) != set(_PATH_KEYS):
        raise _consistency_error()
    for value in paths.values():
        if type(value) is not str or not _utf8(value):
            raise _consistency_error()
    if images_dir is not None:
        validate_operation_paths(images_dir, paths, operation_type=operation_type)
    operation_id = "attachment-op-" + uuid.uuid4().hex
    now = now_str()
    conn.execute(
        "insert into attachment_file_ops(operation_id, parent_type, parent_id, "
        "attachment_id, expected_revision, operation_type, status, temp_rel_path, "
        "final_rel_path, tombstone_rel_path, created_at, updated_at) "
        "values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            operation_id,
            parent_type,
            parent_id,
            attachment_id,
            expected_revision,
            operation_type,
            "staging_publish" if operation_type == "publish" else "staging_delete",
            paths["temp_rel_path"],
            paths["final_rel_path"],
            paths["tombstone_rel_path"],
            now,
            now,
        ),
    )
    return operation_id


def stage_publish(
    conn,
    *,
    parent_type,
    parent_id,
    attachment_id,
    expected_revision,
    paths,
    images_dir=None,
):
    return _stage(
        conn,
        parent_type=parent_type,
        parent_id=parent_id,
        attachment_id=attachment_id,
        expected_revision=expected_revision,
        paths=paths,
        operation_type="publish",
        images_dir=images_dir,
    )


def stage_delete(
    conn,
    *,
    parent_type,
    parent_id,
    attachment_id,
    expected_revision,
    paths,
    images_dir=None,
):
    return _stage(
        conn,
        parent_type=parent_type,
        parent_id=parent_id,
        attachment_id=attachment_id,
        expected_revision=expected_revision,
        paths=paths,
        operation_type="delete",
        images_dir=images_dir,
    )


def require_no_pending_attachment_op(
    conn, *, parent_type, parent_id, allow_operation_id=None
):
    if parent_type in ("return_visit", "communication"):
        parent_types = (parent_type, "call_create")
    elif parent_type == "call_create":
        parent_types = ("call_create", "return_visit", "communication")
    else:
        parent_types = (parent_type,)
    placeholders = ",".join("?" for _ in parent_types)
    row = conn.execute(
        "select 1 from attachment_file_ops "
        f"where parent_type in ({placeholders}) and parent_id = ? "
        "and (? is null or operation_id != ?) limit 1",
        (*parent_types, parent_id, allow_operation_id, allow_operation_id),
    ).fetchone()
    if row is not None:
        raise HTTPException(status_code=409, detail="attachment_operation_pending")


def hidden_attachment_ids(conn, *, parent_type, parent_id):
    rows = conn.execute(
        "select attachment_id from attachment_file_ops "
        "where parent_type = ? and parent_id = ? and status = 'staging_delete'",
        (parent_type, parent_id),
    ).fetchall()
    return frozenset(row["attachment_id"] for row in rows)


def _operation(conn, operation_id, *, status=None):
    row = conn.execute(
        "select * from attachment_file_ops where operation_id = ?",
        (operation_id,),
    ).fetchone()
    if row is None or (status is not None and row["status"] != status):
        raise HTTPException(status_code=409, detail="attachment_operation_conflict")
    return row


def mark_publish_pending(conn, *, operation_id):
    row = _operation(conn, operation_id, status="staging_publish")
    require_no_pending_attachment_op(
        conn,
        parent_type=row["parent_type"],
        parent_id=row["parent_id"],
        allow_operation_id=operation_id,
    )
    conn.execute(
        "update attachment_file_ops set status='pending_publish', updated_at=? "
        "where operation_id=?",
        (now_str(), operation_id),
    )


def _run_callback(callback):
    if callback is not None:
        callback()


def commit_publish(
    conn, *, operation_id, insert_business_row, write_version, write_audit
):
    row = _operation(conn, operation_id, status="pending_publish")
    require_no_pending_attachment_op(
        conn,
        parent_type=row["parent_type"],
        parent_id=row["parent_id"],
        allow_operation_id=operation_id,
    )
    _run_callback(insert_business_row)
    _run_callback(write_version)
    _run_callback(write_audit)
    conn.execute(
        "update attachment_file_ops set status='publish_committed', updated_at=? "
        "where operation_id=?",
        (now_str(), operation_id),
    )


def commit_delete(
    conn, *, operation_id, delete_business_row, write_version, write_audit
):
    row = _operation(conn, operation_id, status="staging_delete")
    require_no_pending_attachment_op(
        conn,
        parent_type=row["parent_type"],
        parent_id=row["parent_id"],
        allow_operation_id=operation_id,
    )
    _run_callback(delete_business_row)
    _run_callback(write_version)
    _run_callback(write_audit)
    conn.execute(
        "update attachment_file_ops set status='delete_committed', updated_at=? "
        "where operation_id=?",
        (now_str(), operation_id),
    )


def _row_paths(row):
    return {key: row[key] for key in _PATH_KEYS}


def publish_staged_file(db_path, images_dir, operation_id):
    with closing(connect(Path(db_path))) as conn:
        row = _operation(conn, operation_id, status="pending_publish")
        paths = validate_operation_paths(
            images_dir, _row_paths(row), operation_type="publish"
        )
    temp_path = paths["temp_rel_path"]
    final_path = paths["final_rel_path"]
    if not temp_path.is_file():
        raise _consistency_error()
    final_path.parent.mkdir(parents=True, exist_ok=True)
    os.link(temp_path, final_path)


def write_staged_bytes(
    db_path,
    images_dir,
    operation_id,
    data,
    *,
    expected_size,
    expected_hash,
):
    """Create the logged temp file exclusively and verify its bytes on disk."""
    if type(data) is not bytes or len(data) != expected_size:
        raise _consistency_error()
    with closing(connect(Path(db_path))) as conn:
        row = _operation(conn, operation_id, status="staging_publish")
        paths = validate_operation_paths(
            images_dir, _row_paths(row), operation_type="publish"
        )
    temp_path = paths["temp_rel_path"]
    temp_path.parent.mkdir(parents=True, exist_ok=True)
    with temp_path.open("xb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    digest = hashlib.sha256()
    size = 0
    with temp_path.open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            size += len(chunk)
            digest.update(chunk)
    if size != expected_size or digest.hexdigest() != expected_hash:
        raise _consistency_error()


def tombstone_staged_file(db_path, images_dir, operation_id):
    with closing(connect(Path(db_path))) as conn:
        row = _operation(conn, operation_id, status="staging_delete")
        paths = validate_operation_paths(
            images_dir, _row_paths(row), operation_type="delete"
        )
    final_path = paths["final_rel_path"]
    tombstone_path = paths["tombstone_rel_path"]
    if not final_path.is_file():
        raise _consistency_error()
    tombstone_path.parent.mkdir(parents=True, exist_ok=True)
    os.link(final_path, tombstone_path, follow_symlinks=False)
    final_path.unlink()


def _business_row(conn, row):
    if row["parent_type"] == "return_visit":
        return conn.execute(
            "select rel_path, size_bytes, content_hash from return_visit_images "
            "where image_id = ? and return_visit_id = ?",
            (row["attachment_id"], row["parent_id"]),
        ).fetchone()
    if row["parent_type"] == "communication":
        return conn.execute(
            "select rel_path, size_bytes, content_hash from communication_images "
            "where image_id = ? and communication_id = ?",
            (row["attachment_id"], row["parent_id"]),
        ).fetchone()
    return conn.execute(
        "select rel_path, size_bytes, content_hash from calls where call_id = ?",
        (row["attachment_id"],),
    ).fetchone()


def _file_matches_business(path, business):
    expected_size = business["size_bytes"]
    expected_hash = business["content_hash"]
    if (
        type(expected_size) is not int
        or expected_size < 0
        or type(expected_hash) is not str
        or len(expected_hash) != 64
    ):
        return False
    digest = hashlib.sha256()
    size = 0
    try:
        with path.open("rb") as handle:
            while True:
                chunk = handle.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                digest.update(chunk)
    except (OSError, RuntimeError, ValueError):
        return False
    return size == expected_size and digest.hexdigest() == expected_hash


def _same_file(left, right):
    return left is not None and right is not None and left.exists() and right.exists() and os.path.samefile(left, right)


def _unlink(path):
    if path is not None and path.exists():
        if not path.is_file():
            raise _consistency_error()
        path.unlink()


def _restore_tombstone(tombstone_path, final_path):
    if final_path.exists():
        if not _same_file(tombstone_path, final_path):
            raise _consistency_error()
        _unlink(tombstone_path)
        return
    final_path.parent.mkdir(parents=True, exist_ok=True)
    os.link(tombstone_path, final_path)
    _unlink(tombstone_path)


def _recover_locked(conn, images_dir, row):
    operation_type = row["operation_type"]
    status = row["status"]
    expected_operation_type = (
        "publish" if status in (
            "staging_publish", "pending_publish", "publish_committed"
        ) else "delete" if status in (
            "staging_delete", "delete_committed"
        ) else None
    )
    if operation_type != expected_operation_type:
        raise _consistency_error()
    paths = validate_operation_paths(
        images_dir, _row_paths(row), operation_type=operation_type
    )
    temp_path = paths["temp_rel_path"]
    final_path = paths["final_rel_path"]
    tombstone_path = paths["tombstone_rel_path"]
    business = _business_row(conn, row)
    if status in ("staging_publish", "pending_publish"):
        if business is not None:
            raise _consistency_error()
        if _same_file(temp_path, final_path):
            _unlink(final_path)
        _unlink(temp_path)
    elif status == "publish_committed":
        if (
            business is None
            or business["rel_path"] != row["final_rel_path"]
            or not final_path.is_file()
            or (temp_path.exists() and not _same_file(temp_path, final_path))
            or not _file_matches_business(final_path, business)
        ):
            raise _consistency_error()
        _unlink(temp_path)
    elif status == "staging_delete":
        if (
            business is None
            or business["rel_path"] != row["final_rel_path"]
        ):
            raise _consistency_error()
        if tombstone_path.exists():
            if not tombstone_path.is_file():
                raise _consistency_error()
            _restore_tombstone(tombstone_path, final_path)
        elif not final_path.is_file():
            raise _consistency_error()
    elif status == "delete_committed":
        if business is not None or final_path.exists():
            raise _consistency_error()
        _unlink(tombstone_path)
    else:
        raise _consistency_error()

    conn.execute(
        "delete from attachment_file_ops where operation_id = ?",
        (row["operation_id"],),
    )
    return status


def recover_attachment_file_operation(db_path, images_dir, operation_id):
    with closing(connect(Path(db_path))) as conn:
        begin_immediate(conn)
        row = conn.execute(
            "select * from attachment_file_ops where operation_id = ?",
            (operation_id,),
        ).fetchone()
        if row is None:
            conn.commit()
            return {"recovered": 0, "blocked": 0, "states": {}}
        try:
            status = _recover_locked(conn, images_dir, row)
            conn.commit()
        except AttachmentConsistencyError:
            conn.rollback()
            raise
        except OSError:
            conn.rollback()
            raise _consistency_error() from None
        except BaseException:
            conn.rollback()
            raise
    return {"recovered": 1, "blocked": 0, "states": {status: 1}}


def recover_attachment_file_ops(db_path, images_dir):
    with closing(connect(Path(db_path))) as conn:
        operation_ids = [
            row["operation_id"]
            for row in conn.execute(
                "select operation_id from attachment_file_ops order by created_at, operation_id"
            ).fetchall()
        ]
    states = Counter()
    recovered = 0
    blocked = 0
    for operation_id in operation_ids:
        try:
            result = recover_attachment_file_operation(
                db_path, images_dir, operation_id
            )
        except AttachmentConsistencyError:
            blocked += 1
            continue
        recovered += result["recovered"]
        states.update(result["states"])
    if blocked:
        raise _consistency_error()
    return {
        "recovered": recovered,
        "blocked": 0,
        "states": dict(states),
    }
