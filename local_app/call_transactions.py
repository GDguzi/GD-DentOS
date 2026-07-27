"""Atomic customer-hub call write transactions."""
from __future__ import annotations

from .customer_hub_transaction_core import (
    HTTPException,
    Path,
    _delete_tombstone,
    _locked_actor,
    _payload_dict,
    _request_text,
    _require_active_call,
    _require_active_patient,
    _require_call_create_parents,
    _require_only_fields,
    _row_json,
    _stable_entity_write_error,
    _write_entity_audit,
    begin_immediate,
    closing,
    connect,
    increment_attachment_parent,
    insert_attachment_return_visit_version,
    legacy_audit_operator,
    lock_attachment_parent,
    request_write_actor,
    require_expected_revision,
    require_no_pending_attachment_op,
    row_to_dict,
    timeutil,
    write_attachment_audit,
    write_attachment_delete_audit,
)


_CALL_EDIT_FIELDS = frozenset(("direction", "deal_status", "region", "note"))


def insert_uploaded_call(conn, *, values, locked_actor):
    operator = (
        legacy_audit_operator() if locked_actor is None else locked_actor.username
    )
    conn.execute(
        "insert into calls("
        "call_id, direction, source, caller_raw, callee_raw, peer_norm, "
        "patient_identity, match_status, operator, started_at, duration_sec, "
        "deal_status, rel_path, file_name, size_bytes, content_hash, "
        "linked_consult_id, linked_return_visit_id, linked_communication_id, "
        "note, revision, created_at) values "
        "(?, ?, ?, ?, ?, ?, ?, 'manual', ?, ?, ?, ?, ?, ?, ?, ?, '', ?, ?, ?, 1, ?)",
        (
            values["call_id"],
            values["direction"],
            values["source"],
            values["caller_raw"],
            values["callee_raw"],
            values["peer_norm"],
            values["patient_identity"],
            operator,
            values["started_at"],
            values["duration_sec"],
            values["deal_status"],
            values["rel_path"],
            values["file_name"],
            values["size_bytes"],
            values["content_hash"],
            values["linked_return_visit_id"],
            values["linked_communication_id"],
            values["note"],
            values["created_at"],
        ),
    )
    return conn.execute(
        "select * from calls where call_id=?", (values["call_id"],)
    ).fetchone()


def create_call(db_path, values, actor):
    """Insert one uploaded call after revalidating actor and parents in the write lock."""
    def write():
        with closing(connect(Path(db_path))) as conn:
            begin_immediate(conn)
            locked_actor = _locked_actor(
                conn, actor, ("call_record.manage",)
            )
            patient_identity = values["patient_identity"]
            direction = values["direction"]
            _require_active_patient(conn, patient_identity)
            _require_call_create_parents(
                conn,
                patient_identity=patient_identity,
                direction=direction,
                linked_communication_id=values["linked_communication_id"],
                linked_return_visit_id=values["linked_return_visit_id"],
                linked_consult_id=values["linked_consult_id"],
            )
            operator = (
                legacy_audit_operator()
                if locked_actor is None
                else locked_actor.username
            )
            conn.execute(
                "insert into calls("
                "call_id, direction, source, caller_raw, callee_raw, peer_norm, "
                "patient_identity, match_status, operator, started_at, duration_sec, "
                "deal_status, rel_path, file_name, size_bytes, content_hash, "
                "linked_consult_id, linked_return_visit_id, linked_communication_id, "
                "note, created_at) values "
                "(?, ?, ?, ?, ?, ?, ?, 'manual', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    values["call_id"],
                    direction,
                    values["source"],
                    values["caller_raw"],
                    values["callee_raw"],
                    values["peer_norm"],
                    patient_identity,
                    operator,
                    values["started_at"],
                    values["duration_sec"],
                    values["deal_status"],
                    values["rel_path"],
                    values["file_name"],
                    values["size_bytes"],
                    values["content_hash"],
                    values["linked_consult_id"],
                    values["linked_return_visit_id"],
                    values["linked_communication_id"],
                    values["note"],
                    values["created_at"],
                ),
            )
            final_row = conn.execute(
                "select * from calls where call_id = ?",
                (values["call_id"],),
            ).fetchone()
            _write_entity_audit(
                conn,
                locked_actor,
                "call",
                values["call_id"],
                "upload_patient_call",
                new_json=_row_json(final_row),
                created_at=values["created_at"],
            )
            conn.commit()
            return {
                "ok": True,
                "call_id": values["call_id"],
                "revision": final_row["revision"],
            }

    return _stable_entity_write_error(write, "call_write_failed")


def update_call(db_path, call_id, payload, actor):
    def write():
        with closing(connect(Path(db_path))) as conn:
            begin_immediate(conn)
            locked_actor = _locked_actor(
                conn, actor, ("call_record.manage",)
            )
            current = _require_active_call(conn, call_id)
            require_no_pending_attachment_op(
                conn, parent_type="call", parent_id=call_id
            )
            checked_payload = _payload_dict(payload)
            _require_only_fields(
                checked_payload, _CALL_EDIT_FIELDS | {"expected_revision"}
            )
            expected_revision = require_expected_revision(
                checked_payload.get("expected_revision")
            )
            if current["revision"] != expected_revision:
                raise HTTPException(status_code=409, detail="call_stale")
            updates = {
                field: _request_text(checked_payload, field)
                for field in _CALL_EDIT_FIELDS
                if field in checked_payload
            }
            if not updates:
                raise HTTPException(status_code=400, detail="no_editable_fields")
            if "direction" in updates:
                direction = updates["direction"].strip()
                if direction not in ("inbound", "outbound"):
                    raise HTTPException(status_code=400, detail="invalid_call_direction")
                updates["direction"] = direction
                if current["linked_communication_id"]:
                    parent = conn.execute(
                        "select channel, direction from communications "
                        "where communication_id = ?",
                        (current["linked_communication_id"],),
                    ).fetchone()
                    if (
                        parent is None
                        or parent["channel"] != "phone"
                        or parent["direction"] != direction
                    ):
                        raise HTTPException(
                            status_code=409, detail="call_direction_conflict"
                        )
            changed = {
                field: value
                for field, value in updates.items()
                if value != current[field]
            }
            if not changed:
                return {"call": row_to_dict(current)}
            old_json = _row_json(current)
            now = timeutil.now_str()
            assignments = [f"{field} = ?" for field in changed]
            assignments.append("revision = revision + 1")
            conn.execute(
                f"update calls set {', '.join(assignments)} where call_id = ?",
                (*changed.values(), call_id),
            )
            final_row = conn.execute(
                "select * from calls where call_id = ?", (call_id,)
            ).fetchone()
            _write_entity_audit(
                conn,
                locked_actor,
                "call",
                call_id,
                "update_call",
                old_json=old_json,
                new_json=_row_json(final_row),
                created_at=now,
            )
            conn.commit()
            return {"call": row_to_dict(final_row)}

    return _stable_entity_write_error(write, "call_write_failed")


def delete_call(db_path, call_id, payload, actor):
    def write():
        with closing(connect(Path(db_path))) as conn:
            begin_immediate(conn)
            locked_actor = _locked_actor(
                conn, actor, ("call_record.delete",)
            )
            current = _require_active_call(conn, call_id)
            require_no_pending_attachment_op(
                conn, parent_type="call", parent_id=call_id
            )
            checked_payload = _payload_dict(payload)
            _require_only_fields(checked_payload, {"expected_revision"})
            expected_revision = require_expected_revision(
                checked_payload.get("expected_revision")
            )
            if current["revision"] != expected_revision:
                raise HTTPException(status_code=409, detail="call_stale")
            now = timeutil.now_str()
            old_json = _row_json(current)
            next_revision = current["revision"] + 1
            conn.execute(
                "update calls set revision = ? where call_id = ?",
                (next_revision, call_id),
            )
            deletion_row = conn.execute(
                "select call_id, revision from calls where call_id = ?",
                (call_id,),
            ).fetchone()
            if (
                deletion_row is None
                or deletion_row["revision"] != next_revision
            ):
                raise RuntimeError("call delete revision failed")
            tombstone_json = _delete_tombstone(
                "call_id", call_id, next_revision
            )
            conn.execute("delete from calls where call_id = ?", (call_id,))
            if conn.execute(
                "select 1 from calls where call_id = ?", (call_id,)
            ).fetchone() is not None:
                raise RuntimeError("call delete failed")
            _write_entity_audit(
                conn,
                locked_actor,
                "call",
                call_id,
                "delete_call",
                old_json=old_json,
                new_json=tombstone_json,
                created_at=now,
            )
            conn.commit()
            return {
                "ok": True,
                "call_id": call_id,
                "revision": next_revision,
                "_patient_identity": current["patient_identity"],
                "_rel_path": current["rel_path"],
            }

    return _stable_entity_write_error(write, "call_write_failed")
