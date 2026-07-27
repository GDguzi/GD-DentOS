"""Atomic customer-hub communication write transactions."""
from __future__ import annotations

from .customer_hub_transaction_core import (
    HTTPException,
    Path,
    _delete_tombstone,
    _locked_actor,
    _payload_dict,
    _request_text,
    _require_active_communication,
    _require_active_patient,
    _require_only_fields,
    _row_json,
    _stable_entity_write_error,
    _write_entity_audit,
    begin_immediate,
    closing,
    connect,
    increment_attachment_parent,
    legacy_audit_operator,
    lock_attachment_parent,
    new_id,
    request_write_actor,
    require_expected_revision,
    require_no_pending_attachment_op,
    row_to_dict,
    timeutil,
    write_attachment_audit,
)


_COMMUNICATION_CREATE_FIELDS = frozenset(
    ("channel", "direction", "reason", "content", "deal_status", "note")
)
_COMMUNICATION_EDIT_FIELDS = _COMMUNICATION_CREATE_FIELDS


def _communication_direction(channel, direction):
    channel = channel.strip()
    direction = direction.strip()
    if channel not in ("phone", "wechat"):
        raise HTTPException(status_code=400, detail="invalid_communication_channel")
    if channel == "phone":
        if direction not in ("inbound", "outbound"):
            raise HTTPException(
                status_code=400, detail="invalid_communication_direction"
            )
        return channel, direction
    if direction:
        raise HTTPException(
            status_code=400, detail="invalid_communication_direction"
        )
    return channel, ""


def _communication_operator(locked_actor):
    if locked_actor is None:
        return legacy_audit_operator()
    return locked_actor.display_name


def create_communication(db_path, patient_identity, payload, actor):
    def write():
        with closing(connect(Path(db_path))) as conn:
            begin_immediate(conn)
            locked_actor = _locked_actor(
                conn, actor, ("communication.manage",)
            )
            _require_active_patient(conn, patient_identity)
            checked_payload = _payload_dict(payload)
            if "operator" in checked_payload or "contacted_at" in checked_payload:
                raise HTTPException(
                    status_code=400, detail="communication_server_managed_field"
                )
            _require_only_fields(
                checked_payload, _COMMUNICATION_CREATE_FIELDS
            )
            channel = _request_text(
                checked_payload, "channel", default=""
            )
            direction = _request_text(
                checked_payload, "direction", default=""
            )
            channel, direction = _communication_direction(channel, direction)
            values = {
                field: _request_text(checked_payload, field, default="")
                for field in ("reason", "content", "deal_status", "note")
            }
            communication_id = new_id("local-comm")
            now = timeutil.now_str()
            conn.execute(
                "insert into communications("
                "communication_id, patient_identity, channel, direction, reason, "
                "contacted_at, operator, content, deal_status, note, revision, "
                "created_at, updated_at) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)",
                (
                    communication_id,
                    patient_identity,
                    channel,
                    direction,
                    values["reason"],
                    now,
                    _communication_operator(locked_actor),
                    values["content"],
                    values["deal_status"],
                    values["note"],
                    now,
                    now,
                ),
            )
            final_row = conn.execute(
                "select * from communications where communication_id = ?",
                (communication_id,),
            ).fetchone()
            _write_entity_audit(
                conn,
                locked_actor,
                "communication",
                communication_id,
                "create_communication",
                new_json=_row_json(final_row),
                created_at=now,
            )
            conn.commit()
            return {
                "ok": True,
                "communication_id": communication_id,
                "revision": final_row["revision"],
            }

    return _stable_entity_write_error(write, "communication_write_failed")


def update_communication(db_path, communication_id, payload, actor):
    def write():
        with closing(connect(Path(db_path))) as conn:
            begin_immediate(conn)
            locked_actor = _locked_actor(
                conn, actor, ("communication.manage",)
            )
            current = _require_active_communication(conn, communication_id)
            require_no_pending_attachment_op(
                conn,
                parent_type="communication",
                parent_id=communication_id,
            )
            checked_payload = _payload_dict(payload)
            if "operator" in checked_payload or "contacted_at" in checked_payload:
                raise HTTPException(
                    status_code=400, detail="communication_server_managed_field"
                )
            _require_only_fields(
                checked_payload,
                _COMMUNICATION_EDIT_FIELDS | {"expected_revision"},
            )
            expected_revision = require_expected_revision(
                checked_payload.get("expected_revision")
            )
            if current["revision"] != expected_revision:
                raise HTTPException(status_code=409, detail="communication_stale")
            updates = {
                field: _request_text(checked_payload, field)
                for field in _COMMUNICATION_EDIT_FIELDS
                if field in checked_payload
            }
            if not updates:
                raise HTTPException(status_code=400, detail="no_editable_fields")
            if "channel" in updates or "direction" in updates:
                channel, direction = _communication_direction(
                    updates.get("channel", current["channel"]),
                    updates.get("direction", current["direction"]),
                )
                updates["channel"] = channel
                updates["direction"] = direction
                linked_directions = [
                    row["direction"]
                    for row in conn.execute(
                        "select direction from calls "
                        "where linked_communication_id = ?",
                        (communication_id,),
                    ).fetchall()
                ]
                if linked_directions and (
                    channel != "phone"
                    or any(value != direction for value in linked_directions)
                ):
                    raise HTTPException(
                        status_code=409, detail="communication_direction_conflict"
                    )
            changed = {
                field: value
                for field, value in updates.items()
                if value != current[field]
            }
            if not changed:
                return {"communication": row_to_dict(current)}
            old_json = _row_json(current)
            now = timeutil.now_str()
            assignments = [f"{field} = ?" for field in changed]
            assignments.extend(("revision = revision + 1", "updated_at = ?"))
            conn.execute(
                f"update communications set {', '.join(assignments)} "
                "where communication_id = ?",
                (*changed.values(), now, communication_id),
            )
            final_row = conn.execute(
                "select * from communications where communication_id = ?",
                (communication_id,),
            ).fetchone()
            _write_entity_audit(
                conn,
                locked_actor,
                "communication",
                communication_id,
                "update_communication",
                old_json=old_json,
                new_json=_row_json(final_row),
                created_at=now,
            )
            conn.commit()
            return {"communication": row_to_dict(final_row)}

    return _stable_entity_write_error(write, "communication_write_failed")


def delete_communication(db_path, communication_id, payload, actor):
    def write():
        with closing(connect(Path(db_path))) as conn:
            begin_immediate(conn)
            locked_actor = _locked_actor(
                conn, actor, ("communication.delete",)
            )
            current = _require_active_communication(conn, communication_id)
            require_no_pending_attachment_op(
                conn,
                parent_type="communication",
                parent_id=communication_id,
            )
            checked_payload = _payload_dict(payload)
            _require_only_fields(checked_payload, {"expected_revision"})
            expected_revision = require_expected_revision(
                checked_payload.get("expected_revision")
            )
            if current["revision"] != expected_revision:
                raise HTTPException(status_code=409, detail="communication_stale")
            if conn.execute(
                "select 1 from calls where linked_communication_id = ? limit 1",
                (communication_id,),
            ).fetchone() is not None:
                raise HTTPException(
                    status_code=409, detail="communication_has_linked_calls"
                )
            if conn.execute(
                "select 1 from communication_images where communication_id = ? limit 1",
                (communication_id,),
            ).fetchone() is not None:
                raise HTTPException(
                    status_code=409, detail="communication_has_images"
                )
            rel_paths = [
                row["rel_path"]
                for row in conn.execute(
                    "select rel_path from communication_images "
                    "where communication_id = ?",
                    (communication_id,),
                ).fetchall()
            ]
            now = timeutil.now_str()
            old_json = _row_json(current)
            next_revision = current["revision"] + 1
            conn.execute(
                "update communications set revision = ?, updated_at = ? "
                "where communication_id = ?",
                (next_revision, now, communication_id),
            )
            deletion_row = conn.execute(
                "select communication_id, revision from communications "
                "where communication_id = ?",
                (communication_id,),
            ).fetchone()
            if (
                deletion_row is None
                or deletion_row["revision"] != next_revision
            ):
                raise RuntimeError("communication delete revision failed")
            tombstone_json = _delete_tombstone(
                "communication_id", communication_id, next_revision
            )
            conn.execute(
                "delete from communication_images where communication_id = ?",
                (communication_id,),
            )
            conn.execute(
                "delete from communications where communication_id = ?",
                (communication_id,),
            )
            if conn.execute(
                "select 1 from communications where communication_id = ?",
                (communication_id,),
            ).fetchone() is not None:
                raise RuntimeError("communication delete failed")
            _write_entity_audit(
                conn,
                locked_actor,
                "communication",
                communication_id,
                "delete_communication",
                old_json=old_json,
                new_json=tombstone_json,
                created_at=now,
            )
            conn.commit()
            return {
                "ok": True,
                "communication_id": communication_id,
                "revision": next_revision,
                "_patient_identity": current["patient_identity"],
                "_rel_paths": rel_paths,
            }

    return _stable_entity_write_error(write, "communication_write_failed")
