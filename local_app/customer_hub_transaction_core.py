"""Shared primitives for atomic customer-hub write transactions."""
from __future__ import annotations

from contextlib import closing
from pathlib import Path

from fastapi import HTTPException

from local_app import timeutil
from local_app.attachment_file_ops import require_no_pending_attachment_op
from local_app.access_authorization import access_authorizer_active
from local_app.auth import (
    audit_operator as legacy_audit_operator,
    audit_write as legacy_audit_write,
)
from local_app.customer_hub_security import (
    audit_actor_id,
    request_human_actor,
    require_locked_human_actor,
)
from local_app.db import (
    audit_write as data_audit_write,
    begin_immediate,
    connect,
    new_id,
)
from local_app.rv_query import DONE_COND
from local_app.snapshots import return_visit_snapshot, row_to_dict
from local_app.validation import valid_time_field
from local_app.versioning import stable_hash, stable_json


_MISSING = object()


def request_write_actor():
    """Capture the authenticated actor before waiting for the SQLite write lock."""
    if access_authorizer_active():
        return request_human_actor()
    return None


def require_expected_revision(value):
    if type(value) is not int or value < 1:
        raise HTTPException(status_code=400, detail="invalid_expected_revision")
    return value


def _payload_dict(payload):
    if type(payload) is not dict:
        raise HTTPException(status_code=400, detail="invalid_request_body")
    return payload


def _require_only_fields(payload, allowed):
    unknown = set(payload) - set(allowed)
    if unknown:
        raise HTTPException(status_code=400, detail="unknown_request_field")


def _request_text(payload, field, *, default=_MISSING):
    if field not in payload:
        if default is _MISSING:
            raise KeyError(field)
        return default
    value = payload[field]
    if type(value) is not str:
        raise HTTPException(status_code=400, detail="invalid_text_field")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise HTTPException(
            status_code=400, detail="invalid_text_field"
        ) from None
    return value


def _locked_actor(conn, actor, permissions):
    if actor is None:
        if access_authorizer_active():
            raise HTTPException(
                status_code=401, detail="authentication_required"
            )
        return None
    return require_locked_human_actor(conn, actor, permissions)


def _require_active_patient(conn, patient_identity):
    row = conn.execute(
        "select patient_identity from patients where patient_identity = ? "
        "and coalesce(is_deleted, 0) = 0",
        (patient_identity,),
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="patient_not_found")
    return row


def _require_active_visit(conn, return_visit_id):
    row = conn.execute(
        "select * from return_visits where return_visit_id = ? "
        "and coalesce(is_deleted, 0) = 0",
        (return_visit_id,),
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="return_visit_not_found")
    _require_active_patient(conn, row["patient_identity"])
    return row


def _done_for_values(conn, *, status, item_name, note):
    row = conn.execute(
        f"select case when ({DONE_COND}) then 1 else 0 end as done "
        "from (select ? as status, ? as item_name, ? as note) r",
        (status, item_name, note),
    ).fetchone()
    return row["done"] == 1


def _insert_final_version(conn, row):
    snapshot = return_visit_snapshot(row)
    snapshot_json = stable_json(snapshot)
    conn.execute(
        "insert into return_visit_versions"
        "(return_visit_id, version_hash, snapshot_json, batch_id) "
        "values (?, ?, ?, ?)",
        (
            row["return_visit_id"],
            stable_hash(snapshot),
            snapshot_json,
            None,
        ),
    )
    return snapshot_json


def _write_audit(
    conn,
    locked_actor,
    entity_id,
    action,
    *,
    old_json=None,
    new_json=None,
    created_at,
):
    if locked_actor is None:
        legacy_audit_write(
            conn,
            "return_visit",
            entity_id,
            action,
            old_json=old_json,
            new_json=new_json,
            created_at=created_at,
        )
        return
    data_audit_write(
        conn,
        "return_visit",
        entity_id,
        action,
        old_json=old_json,
        new_json=new_json,
        operator=locked_actor.username,
        created_at=created_at,
        actor_user_id=audit_actor_id(locked_actor),
    )


def _write_entity_audit(
    conn,
    locked_actor,
    entity_type,
    entity_id,
    action,
    *,
    old_json=None,
    new_json=None,
    created_at,
):
    if locked_actor is None:
        legacy_audit_write(
            conn,
            entity_type,
            entity_id,
            action,
            old_json=old_json,
            new_json=new_json,
            created_at=created_at,
        )
        return
    data_audit_write(
        conn,
        entity_type,
        entity_id,
        action,
        old_json=old_json,
        new_json=new_json,
        operator=locked_actor.username,
        created_at=created_at,
        actor_user_id=audit_actor_id(locked_actor),
    )


def _stable_write_error(call):
    try:
        return call()
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(
            status_code=500, detail="return_visit_write_failed"
        ) from None


def _stable_entity_write_error(call, detail):
    try:
        return call()
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=500, detail=detail) from None


def _require_active_communication(conn, communication_id):
    row = conn.execute(
        "select * from communications where communication_id = ?",
        (communication_id,),
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="communication_not_found")
    _require_active_patient(conn, row["patient_identity"])
    return row


def _require_call_parent(conn, table, id_column, parent_id, patient_identity):
    if not parent_id:
        return None
    active_clause = " and coalesce(is_deleted, 0) = 0" if table == "return_visits" else ""
    row = conn.execute(
        f"select * from {table} where {id_column} = ?{active_clause}",
        (parent_id,),
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="call_parent_not_found")
    _require_active_patient(conn, row["patient_identity"])
    if row["patient_identity"] != patient_identity:
        raise HTTPException(status_code=400, detail="call_parent_mismatch")
    return row


def _require_active_call(conn, call_id):
    row = conn.execute(
        "select * from calls where call_id = ?", (call_id,)
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="call_not_found")
    _require_active_patient(conn, row["patient_identity"])
    communication = _require_call_parent(
        conn,
        "communications",
        "communication_id",
        row["linked_communication_id"],
        row["patient_identity"],
    )
    _require_call_parent(
        conn,
        "return_visits",
        "return_visit_id",
        row["linked_return_visit_id"],
        row["patient_identity"],
    )
    _require_call_parent(
        conn,
        "consults",
        "consult_id",
        row["linked_consult_id"],
        row["patient_identity"],
    )
    if communication is not None:
        if communication["channel"] != "phone":
            raise HTTPException(status_code=400, detail="call_parent_not_phone")
        if communication["direction"] != row["direction"]:
            raise HTTPException(status_code=409, detail="call_direction_conflict")
    return row


def lock_attachment_parent(
    conn,
    *,
    actor,
    permissions,
    parent_type,
    parent_id,
    expected_revision,
    patient_identity=None,
    direction=None,
    allow_operation_id=None,
):
    """Revalidate one attachment parent and actor inside the current write lock."""
    locked_actor = _locked_actor(conn, actor, permissions)
    expected_revision = require_expected_revision(expected_revision)
    if patient_identity is not None:
        _require_active_patient(conn, patient_identity)
    if parent_type == "return_visit":
        row = _require_active_visit(conn, parent_id)
        stale_detail = "return_visit_stale"
    elif parent_type == "communication":
        row = _require_active_communication(conn, parent_id)
        stale_detail = "communication_stale"
        if direction is not None:
            if row["channel"] != "phone":
                raise HTTPException(status_code=400, detail="call_parent_not_phone")
            if row["direction"] not in ("inbound", "outbound"):
                raise HTTPException(
                    status_code=409, detail="call_parent_direction_required"
                )
            if row["direction"] != direction:
                raise HTTPException(status_code=409, detail="call_direction_conflict")
    elif parent_type == "call":
        row = _require_active_call(conn, parent_id)
        stale_detail = "call_stale"
    else:
        raise HTTPException(status_code=400, detail="invalid_attachment_parent")
    if patient_identity is not None and row["patient_identity"] != patient_identity:
        raise HTTPException(status_code=400, detail="attachment_parent_mismatch")
    require_no_pending_attachment_op(
        conn,
        parent_type=parent_type,
        parent_id=parent_id,
        allow_operation_id=allow_operation_id,
    )
    if row["revision"] != expected_revision:
        raise HTTPException(status_code=409, detail=stale_detail)
    return locked_actor, row


def increment_attachment_parent(conn, *, parent_type, parent_id, now):
    if parent_type == "return_visit":
        conn.execute(
            "update return_visits set revision=revision+1, updated_at=? "
            "where return_visit_id=?",
            (now, parent_id),
        )
        return _require_active_visit(conn, parent_id)
    if parent_type == "communication":
        conn.execute(
            "update communications set revision=revision+1, updated_at=? "
            "where communication_id=?",
            (now, parent_id),
        )
        return _require_active_communication(conn, parent_id)
    if parent_type == "call":
        conn.execute(
            "update calls set revision=revision+1 where call_id=?", (parent_id,)
        )
        return _require_active_call(conn, parent_id)
    raise HTTPException(status_code=400, detail="invalid_attachment_parent")


def insert_attachment_return_visit_version(conn, row):
    return _insert_final_version(conn, row)


def write_attachment_audit(
    conn,
    *,
    locked_actor,
    entity_type,
    entity_id,
    action,
    attachment_id,
    revision,
    created_at,
):
    _write_entity_audit(
        conn,
        locked_actor,
        entity_type,
        entity_id,
        action,
        new_json=stable_json(
            {"attachment_id": attachment_id, "revision": revision}
        ),
        created_at=created_at,
    )


def write_attachment_delete_audit(
    conn,
    *,
    locked_actor,
    entity_type,
    entity_id,
    id_field,
    old_row,
    revision,
    created_at,
):
    _write_entity_audit(
        conn,
        locked_actor,
        entity_type,
        entity_id,
        "delete_call",
        old_json=_row_json(old_row),
        new_json=_delete_tombstone(id_field, entity_id, revision),
        created_at=created_at,
    )


def _require_call_create_parents(
    conn,
    *,
    patient_identity,
    direction,
    linked_communication_id,
    linked_return_visit_id,
    linked_consult_id,
):
    communication = _require_call_parent(
        conn,
        "communications",
        "communication_id",
        linked_communication_id,
        patient_identity,
    )
    _require_call_parent(
        conn,
        "return_visits",
        "return_visit_id",
        linked_return_visit_id,
        patient_identity,
    )
    _require_call_parent(
        conn,
        "consults",
        "consult_id",
        linked_consult_id,
        patient_identity,
    )
    if communication is None:
        return
    if communication["channel"] != "phone":
        raise HTTPException(status_code=400, detail="call_parent_not_phone")
    if communication["direction"] not in ("inbound", "outbound"):
        raise HTTPException(
            status_code=409, detail="call_parent_direction_required"
        )
    if communication["direction"] != direction:
        raise HTTPException(status_code=409, detail="call_direction_conflict")


def _row_json(row):
    return stable_json(row_to_dict(row))


def _delete_tombstone(id_field, entity_id, revision):
    return stable_json(
        {id_field: entity_id, "deleted": True, "revision": revision}
    )
