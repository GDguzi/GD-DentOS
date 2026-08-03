"""快速挂号端点回归。

全部用例使用临时 SQLite 库和合成患者，不读取任何真实业务数据。
"""
from __future__ import annotations

import concurrent.futures
import json
import sqlite3
import tempfile
import threading
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

from local_app.api import create_app
from local_app.db import begin_immediate, connect, init_db
from local_app.passwords import hash_password
from local_app.routes import appointments as appointments_module


TODAY = "2026-08-02"
NOW = "2026-08-02 08:30:00"


@contextmanager
def _frozen_clock():
    with mock.patch.object(appointments_module, "today_str", return_value=TODAY):
        with mock.patch.object(appointments_module, "now_str", return_value=NOW):
            yield


def _add_patient(conn, patient_identity, display_name="演示患者", *, deleted=0):
    conn.execute(
        "insert into patients(patient_identity, display_name, is_deleted, "
        "updated_at, current_hash) values (?, ?, ?, ?, ?)",
        (patient_identity, display_name, deleted, "2026-08-01 00:00:00", f"h-{patient_identity}"),
    )


_APPOINTMENT_FIELDS = (
    "appointment_id",
    "patient_identity",
    "study_identity",
    "start_time",
    "end_time",
    "doctor_name",
    "item_name",
    "status",
    "cancel_reason",
    "room",
    "arrived_at",
    "finished_at",
    "visit_type",
    "register_type",
    "last_batch_id",
    "suspect_cancelled",
    "suspect_reason",
    "schedule_remark",
    "source_json",
    "created_at",
    "updated_at",
)


def _add_appointment(conn, appointment_id, patient_identity="p1", **overrides):
    values = {
        "appointment_id": appointment_id,
        "patient_identity": patient_identity,
        "study_identity": "",
        "start_time": f"{TODAY} 09:00:00",
        "end_time": f"{TODAY} 10:00:00",
        "doctor_name": "演示医生",
        "item_name": "演示项目",
        "status": "已预约",
        "cancel_reason": "",
        "room": "A",
        "arrived_at": "",
        "finished_at": "",
        "visit_type": "",
        "register_type": "预约",
        "last_batch_id": "",
        "suspect_cancelled": 0,
        "suspect_reason": "",
        "schedule_remark": "",
        "source_json": '{"origin":"fixture"}',
        "created_at": "2026-08-01 10:00:00",
        "updated_at": "2026-08-01 10:00:00",
    }
    unknown = set(overrides) - set(values)
    if unknown:
        raise AssertionError(f"unknown appointment fixture fields: {sorted(unknown)}")
    values.update(overrides)
    placeholders = ", ".join("?" for _ in _APPOINTMENT_FIELDS)
    conn.execute(
        f"insert into appointments({', '.join(_APPOINTMENT_FIELDS)}) values ({placeholders})",
        tuple(values[field] for field in _APPOINTMENT_FIELDS),
    )


def _setup_db(tmp, patient_ids=("p1",)):
    db_path = Path(tmp) / "clinic.sqlite3"
    init_db(db_path)
    with connect(db_path) as conn:
        for patient_identity in patient_ids:
            _add_patient(conn, patient_identity)
        conn.commit()
    return db_path


def _setup_v3_db(tmp):
    db_path = _setup_db(tmp)
    target_schema = (
        Path(__file__).parents[1] / "local_app" / "personnel_access_schema.sql"
    ).read_text(encoding="utf-8")
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("pragma foreign_keys = off")
        conn.executescript(
            "drop table sessions; drop table users; drop table staff_members; "
            "drop table audit_logs;"
        )
        conn.executescript(target_schema)
        conn.execute(
            "insert into roles(role_key, name, is_system, sort) "
            "values ('check_in_role', '合成挂号角色', 0, 10)"
        )
        conn.execute(
            "insert into staff_members(staff_id, name, employment_status) "
            "values ('staff-check-in', '合成挂号人员', 'employed')"
        )
        conn.execute(
            "insert into staff_role_assignments(staff_id, role_key, is_primary, created_at) "
            "values ('staff-check-in', 'check_in_role', 1, '2026-08-02 08:00:00')"
        )
        conn.execute(
            "insert into users(id, username, display_name, password_hash, staff_id, "
            "is_active, is_system_admin, account_kind) values (?, ?, ?, ?, ?, 1, 0, 'staff')",
            (
                "user-check-in",
                "check-in-user",
                "合成挂号用户",
                hash_password("synthetic-password"),
                "staff-check-in",
            ),
        )
        conn.commit()
    finally:
        conn.close()
    return db_path


def _set_v3_permissions(db_path, permissions):
    with sqlite3.connect(db_path) as conn:
        conn.execute("delete from role_permissions where role_key='check_in_role'")
        conn.executemany(
            "insert into role_permissions(role_key, perm_key) values ('check_in_role', ?)",
            [(permission,) for permission in permissions],
        )
        conn.commit()


def _v3_client(db_path):
    client = TestClient(create_app(db_path, access_v3=True))
    login = client.post(
        "/api/auth/login",
        json={"username": "check-in-user", "password": "synthetic-password"},
    )
    if login.status_code != 200:
        raise AssertionError(f"synthetic v3 login failed: {login.status_code} {login.text}")
    return client


def _post(client, patient_identity="p1", payload=None):
    with _frozen_clock():
        return client.post(
            f"/api/patients/{patient_identity}/check-in",
            json={} if payload is None else payload,
        )


class QuickCheckInTest(unittest.TestCase):
    def test_without_today_appointment_creates_complete_arrived_walkin(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = _setup_db(tmp)
            client = TestClient(create_app(db_path))

            response = _post(client)

            self.assertEqual(response.status_code, 200)
            body = response.json()
            self.assertIs(body["created"], True)
            self.assertIs(body["already_arrived"], False)
            appointment = body["appointment"]
            self.assertEqual(appointment["patient_identity"], "p1")
            self.assertEqual(appointment["display_name"], "演示患者")
            self.assertEqual(appointment["start_time"], NOW)
            self.assertEqual(appointment["end_time"], "")
            self.assertEqual(appointment["doctor_name"], "")
            self.assertEqual(appointment["item_name"], "")
            self.assertEqual(appointment["room"], "")
            self.assertEqual(appointment["status"], "已到诊")
            self.assertEqual(appointment["register_type"], "到店")
            self.assertEqual(appointment["visit_type"], "初诊")
            self.assertEqual(appointment["arrived_at"], NOW)
            self.assertEqual(appointment["finished_at"], "")
            self.assertEqual(appointment["created_at"], NOW)
            self.assertEqual(appointment["updated_at"], NOW)
            with connect(db_path) as conn:
                stored = conn.execute(
                    "select source_json from appointments where appointment_id = ?",
                    (appointment["appointment_id"],),
                ).fetchone()
                actions = [
                    row["action"]
                    for row in conn.execute(
                        "select action from audit_logs where entity_type='appointment' "
                        "and entity_id=? order by audit_id",
                        (appointment["appointment_id"],),
                    ).fetchall()
                ]
            self.assertEqual(json.loads(stored["source_json"]), {"origin": "local"})
            self.assertEqual(actions, ["create_appointment"])

    def test_unique_pending_appointment_is_advanced_and_business_fields_are_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = _setup_db(tmp)
            with connect(db_path) as conn:
                _add_appointment(
                    conn,
                    "a-preserve",
                    status="已确认",
                    doctor_name="原医生",
                    item_name="原项目",
                    register_type="预约",
                    visit_type="点诊",
                    room="B",
                    finished_at="2026-08-02 07:00:00",
                    source_json='{"origin":"remote","keep":1}',
                )
                conn.commit()

            response = _post(TestClient(create_app(db_path)))

            self.assertEqual(response.status_code, 200)
            body = response.json()
            self.assertIs(body["created"], False)
            self.assertIs(body["already_arrived"], False)
            appointment = body["appointment"]
            self.assertEqual(appointment["appointment_id"], "a-preserve")
            self.assertEqual(appointment["status"], "已到诊")
            self.assertEqual(appointment["arrived_at"], NOW)
            self.assertEqual(appointment["finished_at"], "")
            self.assertEqual(appointment["doctor_name"], "原医生")
            self.assertEqual(appointment["item_name"], "原项目")
            self.assertEqual(appointment["register_type"], "预约")
            self.assertEqual(appointment["visit_type"], "点诊")
            self.assertEqual(appointment["room"], "B")
            # source_json 不在挂号响应白名单里，直接查库确认 SaaS 镜像原样保留
            with connect(db_path) as conn:
                stored = conn.execute(
                    "select source_json from appointments where appointment_id='a-preserve'"
                ).fetchone()
            self.assertEqual(json.loads(stored["source_json"]), {"origin": "remote", "keep": 1})

    def test_stage_two_through_five_never_regress_and_missing_arrival_is_stamped(self):
        statuses = ("已到诊", "已分诊", "已完成", "已离开")
        with tempfile.TemporaryDirectory() as tmp:
            db_path = _setup_db(tmp)
            with connect(db_path) as conn:
                for index, status in enumerate(statuses, start=2):
                    _add_appointment(
                        conn,
                        f"a-stage-{index}",
                        start_time=f"{TODAY} {index + 7:02d}:00:00",
                        status=status,
                        arrived_at="",
                        finished_at="2026-08-02 08:00:00",
                        visit_type="复诊",
                    )
                conn.commit()
            client = TestClient(create_app(db_path))

            for index, expected_status in enumerate(statuses, start=2):
                with self.subTest(status=expected_status):
                    response = _post(client, payload={"appointment_id": f"a-stage-{index}"})
                    self.assertEqual(response.status_code, 200)
                    body = response.json()
                    self.assertIs(body["already_arrived"], True)
                    self.assertEqual(body["appointment"]["status"], expected_status)
                    self.assertEqual(body["appointment"]["arrived_at"], NOW)
                    self.assertEqual(
                        body["appointment"]["finished_at"],
                        "2026-08-02 08:00:00",
                    )

    def test_multiple_pending_candidates_return_only_safe_structured_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = _setup_db(tmp)
            with connect(db_path) as conn:
                _add_appointment(conn, "a-first", start_time=f"{TODAY} 09:00:00")
                _add_appointment(
                    conn,
                    "a-suspect",
                    start_time=f"{TODAY} 10:00:00",
                    doctor_name="备选医生",
                    item_name="备选项目",
                    suspect_cancelled=1,
                    suspect_reason="batch-fixture",
                )
                conn.commit()

            response = _post(TestClient(create_app(db_path)))

            self.assertEqual(response.status_code, 409)
            detail = response.json()["detail"]
            self.assertEqual(detail["code"], "multiple_check_in_candidates")
            self.assertEqual(len(detail["candidates"]), 2)
            candidates = {row["appointment_id"]: row for row in detail["candidates"]}
            self.assertEqual(
                set(candidates["a-first"]),
                {
                    "appointment_id",
                    "start_time",
                    "doctor_name",
                    "item_name",
                    "suspect_cancelled",
                },
            )
            self.assertIs(candidates["a-first"]["suspect_cancelled"], False)
            self.assertIs(candidates["a-suspect"]["suspect_cancelled"], True)
            with connect(db_path) as conn:
                rows = conn.execute(
                    "select status, arrived_at, suspect_cancelled, suspect_reason "
                    "from appointments order by appointment_id"
                ).fetchall()
                audit_count = conn.execute("select count(*) from audit_logs").fetchone()[0]
            self.assertTrue(all(row["status"] == "已预约" for row in rows))
            self.assertTrue(all(row["arrived_at"] == "" for row in rows))
            self.assertEqual(audit_count, 0)

    def test_explicit_candidate_advances_and_clears_only_the_selected_suspect(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = _setup_db(tmp)
            with connect(db_path) as conn:
                _add_appointment(
                    conn,
                    "a-selected",
                    suspect_cancelled=1,
                    suspect_reason="selected-reason",
                )
                _add_appointment(
                    conn,
                    "a-untouched",
                    start_time=f"{TODAY} 10:00:00",
                    suspect_cancelled=1,
                    suspect_reason="untouched-reason",
                )
                conn.commit()

            response = _post(
                TestClient(create_app(db_path)),
                payload={"appointment_id": "a-selected"},
            )

            self.assertEqual(response.status_code, 200)
            selected = response.json()["appointment"]
            self.assertEqual(selected["status"], "已到诊")
            # suspect_* 不在挂号响应白名单里，直接查库确认只清了被选中那条
            with connect(db_path) as conn:
                picked = conn.execute(
                    "select suspect_cancelled, suspect_reason "
                    "from appointments where appointment_id='a-selected'"
                ).fetchone()
                untouched = conn.execute(
                    "select status, arrived_at, suspect_cancelled, suspect_reason "
                    "from appointments where appointment_id='a-untouched'"
                ).fetchone()
            self.assertEqual(picked["suspect_cancelled"], 0)
            self.assertEqual(picked["suspect_reason"], "")
            self.assertEqual(untouched["status"], "已预约")
            self.assertEqual(untouched["arrived_at"], "")
            self.assertEqual(untouched["suspect_cancelled"], 1)
            self.assertEqual(untouched["suspect_reason"], "untouched-reason")

    def test_stale_explicit_candidate_returns_409_without_auto_selecting_another(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = _setup_db(tmp)
            with connect(db_path) as conn:
                _add_appointment(conn, "a-stale")
                _add_appointment(conn, "a-other", start_time=f"{TODAY} 10:00:00")
                conn.commit()
            client = TestClient(create_app(db_path))
            initial = _post(client)
            self.assertEqual(initial.status_code, 409)
            with connect(db_path) as conn:
                conn.execute(
                    "update appointments set status='已取消' where appointment_id='a-stale'"
                )
                conn.commit()

            response = _post(client, payload={"appointment_id": "a-stale"})

            self.assertEqual(response.status_code, 409)
            self.assertEqual(response.json()["detail"]["code"], "check_in_candidate_stale")
            with connect(db_path) as conn:
                other = conn.execute(
                    "select status, arrived_at from appointments where appointment_id='a-other'"
                ).fetchone()
                counts = (
                    conn.execute("select count(*) from appointment_versions").fetchone()[0],
                    conn.execute("select count(*) from audit_logs").fetchone()[0],
                )
            self.assertEqual((other["status"], other["arrived_at"]), ("已预约", ""))
            self.assertEqual(counts, (0, 0))

    def test_formal_past_record_defaults_new_walkin_to_follow_up(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = _setup_db(tmp)
            with connect(db_path) as conn:
                conn.execute(
                    "insert into medical_records(record_id, patient_identity, visit_time, "
                    "draft_status, created_at, current_hash) values (?, ?, ?, ?, ?, ?)",
                    ("r1", "p1", "", "", "2026-08-01 09:00:00", "hr1"),
                )
                conn.commit()

            response = _post(TestClient(create_app(db_path)))

            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["appointment"]["visit_type"], "复诊")

    def test_only_draft_or_unarrived_history_defaults_new_walkin_to_first_visit(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = _setup_db(tmp)
            with connect(db_path) as conn:
                conn.execute(
                    "insert into medical_records(record_id, patient_identity, visit_time, "
                    "draft_status, created_at, current_hash) values (?, ?, ?, ?, ?, ?)",
                    ("draft-r", "p1", "2026-08-01 09:00:00", "ai_draft", "", "hdr"),
                )
                _add_appointment(
                    conn,
                    "past-pending",
                    start_time="2026-08-01 09:00:00",
                    status="已确认",
                    arrived_at="",
                )
                conn.commit()

            response = _post(TestClient(create_app(db_path)))

            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["appointment"]["visit_type"], "初诊")

    def test_past_arrival_or_high_stage_defaults_new_walkin_to_follow_up(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = _setup_db(tmp, patient_ids=("p-arrived", "p-finished"))
            with connect(db_path) as conn:
                _add_appointment(
                    conn,
                    "past-arrived",
                    patient_identity="p-arrived",
                    start_time="2026-08-01 09:00:00",
                    status="已预约",
                    arrived_at="2026-08-01 09:10:00",
                )
                _add_appointment(
                    conn,
                    "past-finished",
                    patient_identity="p-finished",
                    start_time="2026-08-01 10:00:00",
                    status="已完成",
                    arrived_at="",
                )
                conn.commit()
            client = TestClient(create_app(db_path))

            arrived = _post(client, patient_identity="p-arrived")
            finished = _post(client, patient_identity="p-finished")

            self.assertEqual(arrived.status_code, 200)
            self.assertEqual(finished.status_code, 200)
            self.assertEqual(arrived.json()["appointment"]["visit_type"], "复诊")
            self.assertEqual(finished.json()["appointment"]["visit_type"], "复诊")

    def test_arrived_candidate_selection_orders_by_arrival_start_and_id_descending(self):
        with tempfile.TemporaryDirectory() as tmp:
            patients = ("p-arrival", "p-start", "p-id")
            db_path = _setup_db(tmp, patient_ids=patients)
            with connect(db_path) as conn:
                for appointment_id, arrived_at in (
                    ("arrival-old", "2026-08-02 08:50:00"),
                    ("arrival-new", "2026-08-02 09:10:00"),
                ):
                    _add_appointment(
                        conn,
                        appointment_id,
                        patient_identity="p-arrival",
                        status="已到诊",
                        arrived_at=arrived_at,
                        visit_type="初诊",
                    )
                for appointment_id, start_time in (
                    ("start-old", f"{TODAY} 09:00:00"),
                    ("start-new", f"{TODAY} 10:00:00"),
                ):
                    _add_appointment(
                        conn,
                        appointment_id,
                        patient_identity="p-start",
                        start_time=start_time,
                        status="已到诊",
                        arrived_at="2026-08-02 08:50:00",
                        visit_type="初诊",
                    )
                for appointment_id in ("id-a", "id-z"):
                    _add_appointment(
                        conn,
                        appointment_id,
                        patient_identity="p-id",
                        status="已到诊",
                        arrived_at="2026-08-02 08:50:00",
                        visit_type="初诊",
                    )
                conn.commit()
            client = TestClient(create_app(db_path))

            selected = {}
            for patient_identity in patients:
                response = _post(client, patient_identity=patient_identity)
                self.assertEqual(response.status_code, 200)
                selected[patient_identity] = response.json()["appointment"]["appointment_id"]

            self.assertEqual(
                selected,
                {"p-arrival": "arrival-new", "p-start": "start-new", "p-id": "id-z"},
            )

    def test_real_update_writes_version_and_update_action_but_repeat_is_noop(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = _setup_db(tmp)
            with connect(db_path) as conn:
                _add_appointment(
                    conn,
                    "a-version",
                    status="已预约",
                    arrived_at="",
                    visit_type="",
                    register_type="预约",
                    suspect_cancelled=1,
                    suspect_reason="fixture-reason",
                )
                conn.commit()
            client = TestClient(create_app(db_path))

            first = _post(client)
            self.assertEqual(first.status_code, 200)
            with connect(db_path) as conn:
                first_versions = conn.execute(
                    "select snapshot_json from appointment_versions where appointment_id='a-version'"
                ).fetchall()
                first_actions = [
                    row["action"]
                    for row in conn.execute(
                        "select action from audit_logs where entity_type='appointment' "
                        "and entity_id='a-version' order by audit_id"
                    ).fetchall()
                ]
            self.assertEqual(len(first_versions), 1)
            old_snapshot = json.loads(first_versions[0]["snapshot_json"])
            self.assertEqual(old_snapshot["register_type"], "预约")
            self.assertEqual(old_snapshot["suspect_cancelled"], 1)
            self.assertEqual(old_snapshot["suspect_reason"], "fixture-reason")
            self.assertEqual(first_actions, ["update_appointment"])

            second = _post(client)

            self.assertEqual(second.status_code, 200)
            self.assertIs(second.json()["already_arrived"], True)
            with connect(db_path) as conn:
                final_counts = (
                    conn.execute(
                        "select count(*) from appointment_versions where appointment_id='a-version'"
                    ).fetchone()[0],
                    conn.execute(
                        "select count(*) from audit_logs where entity_type='appointment' "
                        "and entity_id='a-version'"
                    ).fetchone()[0],
                )
            self.assertEqual(final_counts, (1, 1))

    def test_two_concurrent_requests_create_only_one_today_appointment(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = _setup_db(tmp)
            clients = (TestClient(create_app(db_path)), TestClient(create_app(db_path)))
            barrier = threading.Barrier(2)

            def synchronized_begin(conn):
                barrier.wait(timeout=5)
                return begin_immediate(conn)

            def check_in(index):
                return clients[index].post("/api/patients/p1/check-in", json={})

            with _frozen_clock():
                with mock.patch.object(
                    appointments_module,
                    "begin_immediate",
                    side_effect=synchronized_begin,
                ):
                    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
                        responses = list(executor.map(check_in, range(2)))

            self.assertEqual([response.status_code for response in responses], [200, 200])
            appointment_ids = {
                response.json()["appointment"]["appointment_id"] for response in responses
            }
            self.assertEqual(len(appointment_ids), 1)
            self.assertCountEqual(
                [response.json()["created"] for response in responses],
                [True, False],
            )
            self.assertCountEqual(
                [response.json()["already_arrived"] for response in responses],
                [False, True],
            )
            with connect(db_path) as conn:
                counts = (
                    conn.execute(
                        "select count(*) from appointments where patient_identity='p1' "
                        "and substr(start_time,1,10)=?",
                        (TODAY,),
                    ).fetchone()[0],
                    conn.execute(
                        "select count(*) from audit_logs where action='create_appointment'"
                    ).fetchone()[0],
                    conn.execute("select count(*) from appointment_versions").fetchone()[0],
                )
            self.assertEqual(counts, (1, 1, 0))

    def test_now_is_sampled_only_after_write_lock_is_acquired(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = _setup_db(tmp)
            client = TestClient(create_app(db_path))
            events = []

            def locking_begin(conn):
                events.append("begin")
                return begin_immediate(conn)

            def sample_now():
                events.append("now")
                return NOW

            def sample_today():
                events.append("today")
                return TODAY

            with mock.patch.object(
                appointments_module,
                "begin_immediate",
                side_effect=locking_begin,
            ):
                with mock.patch.object(
                    appointments_module,
                    "now_str",
                    side_effect=sample_now,
                ):
                    with mock.patch.object(
                        appointments_module,
                        "today_str",
                        side_effect=sample_today,
                    ):
                        response = client.post("/api/patients/p1/check-in", json={})

            self.assertEqual(response.status_code, 200)
            self.assertEqual(events, ["begin", "now"])

    def test_locked_now_date_prevents_cross_midnight_duplicate_creation(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = _setup_db(tmp)
            client = TestClient(create_app(db_path))
            with mock.patch.object(
                appointments_module,
                "now_str",
                return_value="2026-08-02 23:59:59",
            ):
                with mock.patch.object(
                    appointments_module,
                    "today_str",
                    return_value="2026-08-03",
                ):
                    first = client.post("/api/patients/p1/check-in", json={})
                    second = client.post("/api/patients/p1/check-in", json={})

            self.assertEqual(first.status_code, 200)
            self.assertEqual(second.status_code, 200)
            self.assertEqual(
                first.json()["appointment"]["appointment_id"],
                second.json()["appointment"]["appointment_id"],
            )
            with connect(db_path) as conn:
                count = conn.execute(
                    "select count(*) from appointments where patient_identity='p1'"
                ).fetchone()[0]
            self.assertEqual(count, 1)

    def test_legacy_route_bridge_requires_patient_edit(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = _setup_db(tmp)
            client = TestClient(create_app(db_path))

            with mock.patch.object(appointments_module, "require_perm") as require_perm:
                response = _post(client)

            self.assertEqual(response.status_code, 200)
            require_perm.assert_called_once_with("patient.edit")

    def test_v3_requires_exactly_all_three_appointment_permissions(self):
        required = {
            "appointment.create",
            "appointment.status",
            "appointment.edit",
        }
        self.assertNotIn("patient.profile.edit", required)
        with tempfile.TemporaryDirectory() as tmp:
            db_path = _setup_v3_db(tmp)
            _set_v3_permissions(db_path, required)
            allowed_client = _v3_client(db_path)

            allowed = _post(allowed_client)

            self.assertEqual(allowed.status_code, 200, allowed.text)
            for missing in sorted(required):
                with self.subTest(missing=missing):
                    _set_v3_permissions(db_path, required - {missing})
                    denied_client = _v3_client(db_path)
                    denied = _post(
                        denied_client,
                        payload={
                            "appointment_id": allowed.json()["appointment"]["appointment_id"]
                        },
                    )
                    self.assertEqual(denied.status_code, 403, denied.text)
                    denied_client.close()
            allowed_client.close()

    def test_body_rejects_fields_other_than_optional_appointment_id_without_writes(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = _setup_db(tmp)
            client = TestClient(create_app(db_path))

            extra = _post(client, payload={"doctor_name": "不应接受"})
            wrong_type = _post(client, payload={"appointment_id": 123})

            self.assertEqual(extra.status_code, 400)
            self.assertEqual(wrong_type.status_code, 400)
            with connect(db_path) as conn:
                self.assertEqual(conn.execute("select count(*) from appointments").fetchone()[0], 0)
                self.assertEqual(conn.execute("select count(*) from audit_logs").fetchone()[0], 0)

    def test_soft_deleted_patient_is_not_check_in_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "clinic.sqlite3"
            init_db(db_path)
            with connect(db_path) as conn:
                _add_patient(conn, "p-deleted", deleted=1)
                conn.commit()

            response = _post(
                TestClient(create_app(db_path)),
                patient_identity="p-deleted",
            )

            self.assertEqual(response.status_code, 404)
            with connect(db_path) as conn:
                self.assertEqual(conn.execute("select count(*) from appointments").fetchone()[0], 0)

    def test_check_in_response_exposes_only_whitelisted_fields(self):
        """挂号响应是明确白名单：不得漏出 source_json / last_batch_id 等 SaaS 同步层内部字段。"""
        with tempfile.TemporaryDirectory() as tmp:
            db_path = _setup_db(tmp)
            client = TestClient(create_app(db_path))

            body = _post(client).json()

            self.assertEqual(
                set(body["appointment"]),
                {
                    "appointment_id", "patient_identity", "start_time", "end_time",
                    "doctor_name", "item_name", "status", "visit_type", "register_type",
                    "room", "arrived_at", "finished_at", "created_at", "updated_at",
                    "display_name", "phone",
                },
            )
    def test_same_day_earlier_visit_counts_as_follow_up(self):
        """同一天上午已看过诊，下午再挂号是复诊——判定按完整时间戳比，不是按日期比。"""
        with tempfile.TemporaryDirectory() as tmp:
            db_path = _setup_db(tmp)
            with connect(db_path) as conn:
                conn.execute(
                    "insert into medical_records(record_id, patient_identity, visit_time, "
                    "draft_status, created_at, current_hash) values (?, ?, ?, ?, ?, ?)",
                    # 必须早于 NOW(08:30)，否则不构成"本次之前看过"
                    ("r-am", "p1", f"{TODAY} 07:40:00", "", f"{TODAY} 07:40:00", "hram"),
                )
                conn.commit()

            body = _post(TestClient(create_app(db_path))).json()

            self.assertEqual(body["appointment"]["visit_type"], "复诊")

    def _morning_done_afternoon_pending(self, tmp):
        """上午那条已看完 + 下午另有一条待到店——同日两次到店的真实场景。"""
        db_path = _setup_db(tmp)
        with connect(db_path) as conn:
            _add_appointment(
                conn,
                "a-am",
                start_time=f"{TODAY} 07:30:00",
                status="已完成",
                arrived_at=f"{TODAY} 07:35:00",
                finished_at=f"{TODAY} 08:00:00",
            )
            _add_appointment(conn, "a-pm", start_time=f"{TODAY} 14:00:00", status="已预约")
            conn.commit()
        return db_path

    def test_same_day_earlier_arrival_counts_as_follow_up(self):
        """上午那条已到诊，下午这条挂号是复诊；下午这条本身不能当"更早就诊"的依据。"""
        with tempfile.TemporaryDirectory() as tmp:
            db_path = self._morning_done_afternoon_pending(tmp)

            body = _post(
                TestClient(create_app(db_path)),
                payload={"appointment_id": "a-pm"},
            ).json()

            self.assertEqual(body["appointment"]["appointment_id"], "a-pm")
            self.assertEqual(body["appointment"]["visit_type"], "复诊")

    def test_ambiguous_second_check_in_only_offers_pending(self):
        """已完成 1 条 + 待到店 2 条：409 让前台挑时只能给待到店那两条。

        混进已完成那条，前台一选又复用回旧记录；而候选列表不显示状态，看不出哪条是上午那条。
        """
        with tempfile.TemporaryDirectory() as tmp:
            db_path = self._morning_done_afternoon_pending(tmp)
            with connect(db_path) as conn:
                _add_appointment(conn, "a-pm2", start_time=f"{TODAY} 16:00:00", status="已预约")
                conn.commit()

            r = _post(TestClient(create_app(db_path)))

            self.assertEqual(r.status_code, 409)
            detail = r.json()["detail"]
            self.assertEqual(detail["code"], "multiple_check_in_candidates")
            self.assertEqual(
                [c["appointment_id"] for c in detail["candidates"]], ["a-pm", "a-pm2"]
            )

    def test_second_check_in_picks_pending_not_finished_appointment(self):
        """同日第二次到店：不传 id 时要挑还没到诊的那条，不能复用上午已完成的。"""
        with tempfile.TemporaryDirectory() as tmp:
            db_path = self._morning_done_afternoon_pending(tmp)

            body = _post(TestClient(create_app(db_path))).json()

            self.assertEqual(body["appointment"]["appointment_id"], "a-pm")
            self.assertEqual(body["appointment"]["status"], "已到诊")
            self.assertEqual(body["appointment"]["arrived_at"], NOW)
            with connect(db_path) as conn:
                morning = conn.execute(
                    "select status, arrived_at from appointments where appointment_id = 'a-am'"
                ).fetchone()
            self.assertEqual(morning["status"], "已完成")          # 上午那条不许被翻回「已到诊」
            self.assertEqual(morning["arrived_at"], f"{TODAY} 07:35:00")

if __name__ == "__main__":
    unittest.main()
