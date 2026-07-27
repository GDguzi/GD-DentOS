"""Atomic customer-hub return-visit write transactions."""
from __future__ import annotations

import json
import math
from collections.abc import Mapping
from dataclasses import dataclass

from .customer_hub_transaction_core import (
    HTTPException,
    Path,
    _done_for_values,
    _insert_final_version,
    _locked_actor,
    _payload_dict,
    _request_text,
    _require_active_patient,
    _require_active_visit,
    _stable_write_error,
    _write_audit,
    begin_immediate,
    closing,
    connect,
    increment_attachment_parent,
    insert_attachment_return_visit_version,
    lock_attachment_parent,
    new_id,
    request_write_actor,
    require_expected_revision,
    require_no_pending_attachment_op,
    return_visit_snapshot,
    row_to_dict,
    stable_json,
    timeutil,
    valid_time_field,
    write_attachment_audit,
)
from .db import audit_write
from .versioning import (
    framed_utf8_sha256,
    locally_edited_since_last_import,
    stable_hash,
)


_CREATE_FIELDS = (
    "status",
    "note",
    "visitor",
    "return_doctor",
    "return_type",
    "channel",
    "return_result",
    "intent_level",
    "satisfaction",
)
_EDITABLE_FIELDS = (
    "due_time",
    "item_name",
    "status",
    "note",
    "visitor",
    "return_doctor",
    "return_type",
    "channel",
    "return_result",
    "intent_level",
    "satisfaction",
)
MAX_BATCH_DELETE_ITEMS = 200
_MIGRATION_MERGE_FIELDS = (
    "return_doctor",
    "visitor",
    "return_type",
    "return_result",
    "actual_date",
)
_MIGRATION_BASE_FIELDS = (
    "due_time",
    "item_name",
    "status",
    "note",
    "created_at",
    "updated_at",
)
_MIGRATION_PROTECTED_BASE_FIELDS = (
    "due_time",
    "item_name",
    "status",
    "note",
)


@dataclass(frozen=True)
class MigrationWriteContext:
    actor_id: str
    allowed_action: str


MIGRATION_IMPORT_CONTEXT = MigrationWriteContext(
    actor_id="migration_import",
    allowed_action="return_visit.merge",
)


def _migration_text(value):
    if type(value) is not str or not value.strip():
        raise ValueError("invalid_migration_text")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise ValueError("invalid_migration_text") from None
    return value


def validate_migration_batch_id(batch_id):
    """Validate an importer-owned batch id before the batch row is written."""
    return _migration_text(batch_id)


def _validated_normalized_fields(incoming):
    try:
        values = incoming.values
        errors = incoming.errors
    except AttributeError:
        raise ValueError("invalid_normalized_return_visit_fields") from None
    if not isinstance(values, Mapping) or not isinstance(errors, Mapping):
        raise ValueError("invalid_normalized_return_visit_fields")
    if set(values) - set(_MIGRATION_MERGE_FIELDS):
        raise ValueError("unknown_normalized_return_visit_field")
    if set(errors) - {"actual_date"}:
        raise ValueError("unknown_normalized_return_visit_error")
    if set(values) & set(errors):
        raise ValueError("invalid_normalized_return_visit_fields")
    checked_values = {}
    for field, value in values.items():
        checked = _migration_text(value)
        if checked != checked.strip():
            raise ValueError("invalid_normalized_return_visit_fields")
        checked_values[field] = checked
    checked_errors = dict(errors)
    if checked_errors and checked_errors != {"actual_date": "invalid_source"}:
        raise ValueError("unknown_normalized_return_visit_error")
    return checked_values, checked_errors


def _reject_json_constant(_value):
    raise ValueError


def _validate_portable_json(value):
    if type(value) is float and not math.isfinite(value):
        raise ValueError
    if type(value) is str:
        value.encode("utf-8")
    elif type(value) is list:
        for item in value:
            _validate_portable_json(item)
    elif type(value) is dict:
        for key, item in value.items():
            key.encode("utf-8")
            _validate_portable_json(item)


def canonical_migration_source_json(source_json):
    if type(source_json) is not str or not source_json:
        raise ValueError("invalid_migration_source_json")
    try:
        source_json.encode("utf-8")
        parsed = json.loads(
            source_json,
            parse_constant=_reject_json_constant,
        )
        if type(parsed) is not dict:
            raise ValueError
        _validate_portable_json(parsed)
        canonical = stable_json(parsed)
        canonical.encode("utf-8")
    except (UnicodeEncodeError, json.JSONDecodeError, ValueError):
        raise ValueError("invalid_migration_source_json") from None
    return canonical


_canonical_migration_source_json = canonical_migration_source_json


def _validated_import_base_fields(base_fields):
    if not isinstance(base_fields, Mapping):
        raise ValueError("invalid_migration_base_fields")
    if set(base_fields) != set(_MIGRATION_BASE_FIELDS):
        raise ValueError("invalid_migration_base_fields")
    checked = {}
    for field_name in _MIGRATION_BASE_FIELDS:
        value = base_fields[field_name]
        if value is None and field_name in ("due_time", "item_name", "updated_at"):
            checked[field_name] = None
            continue
        if type(value) is not str:
            raise ValueError("invalid_migration_base_fields")
        try:
            value.encode("utf-8")
        except UnicodeEncodeError:
            raise ValueError("invalid_migration_base_fields") from None
        if value != value.strip():
            raise ValueError("invalid_migration_base_fields")
        checked[field_name] = value
    return checked


def _require_migration_batch(conn, batch_id):
    if conn.execute(
        "select 1 from sync_batches where batch_id = ?",
        (batch_id,),
    ).fetchone() is None:
        raise ValueError("migration_batch_not_found")


def _insert_migration_final_version(conn, row, batch_id):
    snapshot = return_visit_snapshot(row)
    snapshot_json = stable_json(snapshot)
    conn.execute(
        "insert into return_visit_versions("
        "return_visit_id,version_hash,snapshot_json,batch_id) values (?,?,?,?)",
        (
            row["return_visit_id"],
            stable_hash(snapshot),
            snapshot_json,
            batch_id,
        ),
    )
    return snapshot_json


def _insert_migration_conflict(
    conn,
    *,
    return_visit_id,
    field_name,
    local_value,
    incoming_value,
    batch_id,
    reason_code,
):
    conflict_key = framed_utf8_sha256(
        return_visit_id,
        field_name,
        local_value,
        incoming_value,
    )
    cursor = conn.execute(
        "insert or ignore into import_conflicts("
        "entity_type,entity_id,field_name,local_value,incoming_value,batch_id,"
        "status,conflict_key,reason_code) "
        "values ('return_visit',?,?,null,null,?,'open',?,?)",
        (
            return_visit_id,
            field_name,
            batch_id,
            conflict_key,
            reason_code,
        ),
    )
    return cursor.rowcount == 1


def _migration_merge_result(
    *,
    revision,
    filled_fields=(),
    conflict_fields=(),
    noop_fields=(),
    status=None,
    reason="",
    conflict_count=None,
):
    result = {
        "filled_count": len(filled_fields),
        "filled_fields": list(filled_fields),
        "conflict_count": (
            len(conflict_fields) if conflict_count is None else conflict_count
        ),
        "conflict_fields": list(conflict_fields),
        "noop_count": len(noop_fields),
        "noop_fields": list(noop_fields),
        "revision": revision,
    }
    if status is not None:
        result.update({"status": status, "reason": reason})
    return result


def _resolve_import_target(conn, return_visit_id, patient_identity, batch_id):
    batch_id = validate_migration_batch_id(batch_id)
    _require_migration_batch(conn, batch_id)
    current = conn.execute(
        "select * from return_visits where return_visit_id = ?",
        (return_visit_id,),
    ).fetchone()
    patient = conn.execute(
        "select is_deleted from patients where patient_identity = ?",
        (patient_identity,),
    ).fetchone()
    revision = current["revision"] if current is not None else None
    if patient is None or patient["is_deleted"]:
        return batch_id, current, _migration_merge_result(
            status="skipped",
            reason="inactive_patient",
            revision=revision,
        )
    if current is not None and current["patient_identity"] != patient_identity:
        return batch_id, current, _migration_merge_result(
            status="conflict",
            reason="patient_mismatch",
            revision=revision,
            conflict_count=1,
        )
    if current is not None and current["is_deleted"]:
        return batch_id, current, _migration_merge_result(
            status="skipped",
            reason="inactive_return_visit",
            revision=revision,
        )
    if current is not None:
        require_no_pending_attachment_op(
            conn,
            parent_type="return_visit",
            parent_id=return_visit_id,
        )
    return batch_id, current, None


def _create_imported_return_visit(
    conn,
    *,
    return_visit_id,
    patient_identity,
    checked_base,
    values,
    errors,
    source_json,
    batch_id,
):
    conn.execute(
        "insert into return_visits("
        "return_visit_id,patient_identity,due_time,item_name,status,note,"
        "return_doctor,visitor,return_type,return_result,actual_date,"
        "source_json,created_at,updated_at,last_batch_id,revision) "
        "values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1)",
        (
            return_visit_id,
            patient_identity,
            checked_base["due_time"],
            checked_base["item_name"],
            checked_base["status"],
            checked_base["note"],
            values.get("return_doctor", ""),
            values.get("visitor", ""),
            values.get("return_type", ""),
            values.get("return_result", ""),
            values.get("actual_date", ""),
            source_json,
            checked_base["created_at"],
            checked_base["updated_at"],
            batch_id,
        ),
    )
    conflict_fields = []
    for field_name in errors:
        if _insert_migration_conflict(
            conn,
            return_visit_id=return_visit_id,
            field_name=field_name,
            local_value="",
            incoming_value="invalid_source",
            batch_id=batch_id,
            reason_code="invalid_source",
        ):
            conflict_fields.append(field_name)
    final_row = conn.execute(
        "select * from return_visits where return_visit_id = ?",
        (return_visit_id,),
    ).fetchone()
    final_json = _insert_migration_final_version(conn, final_row, batch_id)
    audit_write(
        conn,
        "return_visit",
        return_visit_id,
        "import_return_visit",
        old_json=None,
        new_json=final_json,
        operator=MIGRATION_IMPORT_CONTEXT.actor_id,
    )
    return _migration_merge_result(
        status="created",
        revision=1,
        filled_fields=values,
        conflict_fields=conflict_fields,
    )


def merge_imported_return_visit(
    conn,
    *,
    context,
    return_visit_id,
    patient_identity,
    incoming,
    source_json,
    batch_id,
    base_fields=None,
):
    """Merge one imported return visit inside the caller's transaction.

    Existing direct callers omit ``base_fields`` and retain the five-field-only
    contract.  The chunk importer supplies the complete base field mapping,
    which also enables create-if-missing and neutral row outcomes.
    """
    if context is not MIGRATION_IMPORT_CONTEXT:
        raise PermissionError("invalid_migration_write_context")
    if not conn.in_transaction:
        raise RuntimeError("migration_write_transaction_required")

    return_visit_id = _migration_text(return_visit_id)
    patient_identity = _migration_text(patient_identity)
    import_mode = base_fields is not None

    if import_mode:
        batch_id, current, neutral_result = _resolve_import_target(
            conn, return_visit_id, patient_identity, batch_id
        )
        if neutral_result is not None:
            return neutral_result
        checked_base = _validated_import_base_fields(base_fields)
        values, errors = _validated_normalized_fields(incoming)
        try:
            source_json = canonical_migration_source_json(source_json)
        except ValueError as exc:
            if str(exc) != "invalid_migration_source_json":
                raise
            return _migration_merge_result(
                status="conflict",
                reason="invalid_source",
                revision=current["revision"] if current is not None else None,
                conflict_count=1,
            )
    else:
        _require_active_patient(conn, patient_identity)
        current = _require_active_visit(conn, return_visit_id)
        if current["patient_identity"] != patient_identity:
            raise HTTPException(
                status_code=409,
                detail="return_visit_patient_mismatch",
            )
        require_no_pending_attachment_op(
            conn,
            parent_type="return_visit",
            parent_id=return_visit_id,
        )
        values, errors = _validated_normalized_fields(incoming)
        source_json = canonical_migration_source_json(source_json)
        batch_id = validate_migration_batch_id(batch_id)
        _require_migration_batch(conn, batch_id)

    if current is None:
        return _create_imported_return_visit(
            conn,
            return_visit_id=return_visit_id,
            patient_identity=patient_identity,
            checked_base=checked_base,
            values=values,
            errors=errors,
            source_json=source_json,
            batch_id=batch_id,
        )

    fills = {}
    filled_fields = []
    conflict_fields = []
    noop_fields = []

    protected_updates = {}
    metadata_created_at = None
    protected_conflict = False
    if import_mode:
        protected_updates = {
            field_name: checked_base[field_name]
            for field_name in _MIGRATION_PROTECTED_BASE_FIELDS
            if checked_base[field_name] != current[field_name]
        }
        if not current["created_at"] and checked_base["created_at"]:
            metadata_created_at = checked_base["created_at"]
        if protected_updates and locally_edited_since_last_import(
            conn,
            "return_visit",
            return_visit_id,
            ("update_return_visit", "soft_delete_return_visit"),
            current["last_batch_id"],
        ):
            if _insert_migration_conflict(
                conn,
                return_visit_id=return_visit_id,
                field_name="row",
                local_value=stable_json({
                    field_name: current[field_name]
                    for field_name in _MIGRATION_PROTECTED_BASE_FIELDS
                }),
                incoming_value=stable_json({
                    field_name: checked_base[field_name]
                    for field_name in _MIGRATION_PROTECTED_BASE_FIELDS
                }),
                batch_id=batch_id,
                reason_code="value_conflict",
            ):
                conflict_fields.append("row")
            protected_updates = {}
            protected_conflict = True

    for field_name in _MIGRATION_MERGE_FIELDS:
        local_value = current[field_name]
        comparable_local = str(local_value or "").strip()
        if field_name in errors:
            if _insert_migration_conflict(
                conn,
                return_visit_id=return_visit_id,
                field_name=field_name,
                local_value=comparable_local,
                incoming_value="invalid_source",
                batch_id=batch_id,
                reason_code="invalid_source",
            ):
                conflict_fields.append(field_name)
            continue
        if field_name not in values:
            continue
        incoming_value = values[field_name]
        if not comparable_local:
            fills[field_name] = incoming_value
            filled_fields.append(field_name)
        elif comparable_local == incoming_value:
            noop_fields.append(field_name)
        elif _insert_migration_conflict(
            conn,
            return_visit_id=return_visit_id,
            field_name=field_name,
            local_value=comparable_local,
            incoming_value=incoming_value,
            batch_id=batch_id,
            reason_code="value_conflict",
        ):
            conflict_fields.append(field_name)

    changes = {**protected_updates, **fills}
    if changes and metadata_created_at is not None and not protected_conflict:
        changes["created_at"] = metadata_created_at

    revision = current["revision"]
    status = "noop"
    if changes:
        old_json = stable_json(return_visit_snapshot(current))
        changed_at = (
            checked_base["updated_at"] if import_mode else timeutil.now_str()
        )
        assignments = [f"{field_name} = ?" for field_name in changes]
        assignments.extend(
            (
                "source_json = ?",
                "last_batch_id = ?",
                "updated_at = ?",
                "revision = revision + 1",
            )
        )
        conn.execute(
            f"update return_visits set {', '.join(assignments)} "
            "where return_visit_id = ?",
            (
                *changes.values(),
                source_json,
                current["last_batch_id"] if protected_conflict else batch_id,
                changed_at,
                return_visit_id,
            ),
        )
        final_row = conn.execute(
            "select * from return_visits where return_visit_id = ?",
            (return_visit_id,),
        ).fetchone()
        final_json = _insert_migration_final_version(conn, final_row, batch_id)
        audit_write(
            conn,
            "return_visit",
            return_visit_id,
            "import_return_visit" if import_mode else "merge_imported_return_visit",
            old_json=old_json,
            new_json=final_json,
            operator=MIGRATION_IMPORT_CONTEXT.actor_id,
            created_at=None if import_mode else changed_at,
        )
        revision = final_row["revision"]
        status = "updated"
    elif import_mode and not protected_conflict:
        conn.execute(
            "update return_visits set "
            "created_at=case when coalesce(created_at,'')='' then ? else created_at end,"
            "source_json=?,last_batch_id=?,updated_at=? where return_visit_id=?",
            (
                checked_base["created_at"],
                source_json,
                batch_id,
                checked_base["updated_at"],
                return_visit_id,
            ),
        )

    return _migration_merge_result(
        status=status if import_mode else None,
        revision=revision,
        filled_fields=filled_fields,
        conflict_fields=conflict_fields,
        noop_fields=noop_fields,
    )


def create_return_visit(db_path, patient_identity, payload, actor):
    def write():
        with closing(connect(Path(db_path))) as conn:
            begin_immediate(conn)
            locked_actor = _locked_actor(
                conn, actor, ("return_visit.manage",)
            )
            checked_payload = _payload_dict(payload)
            if "actual_date" in checked_payload:
                raise HTTPException(
                    status_code=400, detail="actual_date_server_managed"
                )
            due_time = _request_text(
                checked_payload, "due_time", default=""
            )
            if not due_time.strip():
                raise HTTPException(
                    status_code=400, detail="return_visit_due_required"
                )
            due_time = valid_time_field(due_time, "due_time")
            item_name = _request_text(
                checked_payload, "item_name", default=""
            )
            if not item_name.strip():
                raise HTTPException(
                    status_code=400, detail="return_visit_item_required"
                )
            values = {
                field: _request_text(checked_payload, field, default="")
                for field in _CREATE_FIELDS
            }
            _require_active_patient(conn, patient_identity)
            return_visit_id = new_id("local-rv")
            now = timeutil.now_str()
            actual_date = (
                timeutil.today_str()
                if _done_for_values(
                    conn,
                    status=values["status"],
                    item_name=item_name,
                    note=values["note"],
                )
                else ""
            )
            conn.execute(
                "insert into return_visits("
                "return_visit_id, patient_identity, due_time, item_name, status, "
                "note, visitor, return_doctor, return_type, channel, return_result, "
                "actual_date, intent_level, satisfaction, is_deleted, origin, "
                "created_at, revision, updated_at) "
                "values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 'local', ?, 1, ?)",
                (
                    return_visit_id,
                    patient_identity,
                    due_time,
                    item_name,
                    values["status"],
                    values["note"],
                    values["visitor"],
                    values["return_doctor"],
                    values["return_type"],
                    values["channel"],
                    values["return_result"],
                    actual_date,
                    values["intent_level"],
                    values["satisfaction"],
                    now,
                    now,
                ),
            )
            final_row = conn.execute(
                "select * from return_visits where return_visit_id = ?",
                (return_visit_id,),
            ).fetchone()
            final_json = _insert_final_version(conn, final_row)
            _write_audit(
                conn,
                locked_actor,
                return_visit_id,
                "create_return_visit",
                new_json=final_json,
                created_at=now,
            )
            conn.commit()
            return {
                "return_visit_id": return_visit_id,
                "revision": final_row["revision"],
            }

    return _stable_write_error(write)


def update_return_visit(db_path, return_visit_id, payload, actor):
    def write():
        with closing(connect(Path(db_path))) as conn:
            begin_immediate(conn)
            locked_actor = _locked_actor(
                conn, actor, ("return_visit.manage",)
            )
            checked_payload = _payload_dict(payload)
            if "actual_date" in checked_payload:
                raise HTTPException(
                    status_code=400, detail="actual_date_server_managed"
                )
            updates = {
                field: _request_text(checked_payload, field)
                for field in _EDITABLE_FIELDS
                if field in checked_payload
            }
            if "due_time" in updates:
                updates["due_time"] = valid_time_field(
                    updates["due_time"], "due_time"
                )
            if not updates:
                raise HTTPException(
                    status_code=400, detail="no_editable_fields"
                )
            current = _require_active_visit(conn, return_visit_id)
            require_no_pending_attachment_op(
                conn, parent_type="return_visit", parent_id=return_visit_id
            )
            expected_revision = require_expected_revision(
                checked_payload.get("expected_revision")
            )
            if current["revision"] != expected_revision:
                raise HTTPException(
                    status_code=409, detail="return_visit_stale"
                )
            changed = {
                field: value
                for field, value in updates.items()
                if value != current[field]
            }
            if not changed:
                return {"return_visit": row_to_dict(current)}

            old_snapshot_json = stable_json(return_visit_snapshot(current))
            proposed = {
                field: changed.get(field, current[field])
                for field in ("status", "item_name", "note")
            }
            old_done = _done_for_values(
                conn,
                status=current["status"],
                item_name=current["item_name"],
                note=current["note"],
            )
            new_done = _done_for_values(conn, **proposed)
            if not old_done and new_done:
                actual_date = timeutil.today_str()
            elif old_done and not new_done:
                actual_date = ""
            else:
                actual_date = current["actual_date"]
            now = timeutil.now_str()
            assignments = [f"{field} = ?" for field in changed]
            assignments.extend(
                ("actual_date = ?", "revision = revision + 1", "updated_at = ?")
            )
            conn.execute(
                f"update return_visits set {', '.join(assignments)} "
                "where return_visit_id = ?",
                (*changed.values(), actual_date, now, return_visit_id),
            )
            final_row = conn.execute(
                "select * from return_visits where return_visit_id = ?",
                (return_visit_id,),
            ).fetchone()
            final_json = _insert_final_version(conn, final_row)
            _write_audit(
                conn,
                locked_actor,
                return_visit_id,
                "update_return_visit",
                old_json=old_snapshot_json,
                new_json=final_json,
                created_at=now,
            )
            conn.commit()
            return {"return_visit": row_to_dict(final_row)}

    return _stable_write_error(write)


def _delete_one_locked(conn, locked_actor, row, now):
    return_visit_id = row["return_visit_id"]
    old_json = stable_json(return_visit_snapshot(row))
    conn.execute(
        "update return_visits set is_deleted = 1, revision = revision + 1, "
        "updated_at = ? where return_visit_id = ?",
        (now, return_visit_id),
    )
    final_row = conn.execute(
        "select * from return_visits where return_visit_id = ?",
        (return_visit_id,),
    ).fetchone()
    final_json = _insert_final_version(conn, final_row)
    _write_audit(
        conn,
        locked_actor,
        return_visit_id,
        "soft_delete_return_visit",
        old_json=old_json,
        new_json=final_json,
        created_at=now,
    )
    return final_row


def delete_return_visit(db_path, return_visit_id, payload, actor):
    def write():
        with closing(connect(Path(db_path))) as conn:
            begin_immediate(conn)
            locked_actor = _locked_actor(
                conn, actor, ("return_visit.delete",)
            )
            checked_payload = _payload_dict(payload)
            current = _require_active_visit(conn, return_visit_id)
            require_no_pending_attachment_op(
                conn, parent_type="return_visit", parent_id=return_visit_id
            )
            expected_revision = require_expected_revision(
                checked_payload.get("expected_revision")
            )
            if current["revision"] != expected_revision:
                raise HTTPException(
                    status_code=409, detail="return_visit_stale"
                )
            final_row = _delete_one_locked(
                conn, locked_actor, current, timeutil.now_str()
            )
            conn.commit()
            return {
                "return_visit_id": return_visit_id,
                "is_deleted": 1,
                "revision": final_row["revision"],
            }

    return _stable_write_error(write)


def _batch_items(payload):
    payload = _payload_dict(payload)
    items = payload.get("items")
    if type(items) is not list or not items:
        raise HTTPException(status_code=400, detail="invalid_batch_items")
    if len(items) > MAX_BATCH_DELETE_ITEMS:
        raise HTTPException(status_code=400, detail="batch_limit_exceeded")
    parsed = []
    seen = set()
    for item in items:
        if type(item) is not dict:
            raise HTTPException(status_code=400, detail="invalid_batch_items")
        return_visit_id = item.get("id")
        if type(return_visit_id) is not str or not return_visit_id.strip():
            raise HTTPException(status_code=400, detail="invalid_batch_items")
        return_visit_id = return_visit_id.strip()
        if return_visit_id in seen:
            raise HTTPException(status_code=400, detail="duplicate_return_visit")
        seen.add(return_visit_id)
        parsed.append(
            (
                return_visit_id,
                item.get("expected_revision"),
            )
        )
    return parsed


def batch_delete_return_visits(db_path, payload, actor):
    def write():
        with closing(connect(Path(db_path))) as conn:
            begin_immediate(conn)
            locked_actor = _locked_actor(
                conn, actor, ("return_visit.delete",)
            )
            items = _batch_items(payload)
            validated = []
            for return_visit_id, expected_revision in items:
                row = _require_active_visit(conn, return_visit_id)
                require_no_pending_attachment_op(
                    conn,
                    parent_type="return_visit",
                    parent_id=return_visit_id,
                )
                validated.append((row, expected_revision))
            checked = []
            for row, expected_revision in validated:
                expected_revision = require_expected_revision(
                    expected_revision
                )
                if row["revision"] != expected_revision:
                    raise HTTPException(
                        status_code=409, detail="return_visit_stale"
                    )
                checked.append(row)
            now = timeutil.now_str()
            revisions = []
            for row in checked:
                final_row = _delete_one_locked(
                    conn, locked_actor, row, now
                )
                revisions.append(
                    {
                        "id": final_row["return_visit_id"],
                        "revision": final_row["revision"],
                    }
                )
            conn.commit()
            return {"deleted": revisions, "count": len(revisions)}

    return _stable_write_error(write)
