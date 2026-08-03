import concurrent.futures
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

from local_app import auth
from local_app.api import create_app
from local_app.db import connect, init_db


VALID_ITEM = {
    "name": "本地洁牙",
    "code": "CLEAN-LOCAL",
    "unit": "次",
    "price": "200.00",
    "fee_type": "治疗费",
}

SNAPSHOT_FIELDS = {
    "handle_id",
    "code",
    "name",
    "unit",
    "price",
    "handle_type",
    "fee_type",
    "name_py",
    "stopped",
    "source_json",
    "last_batch_id",
    "created_at",
    "updated_at",
}


class _FetchedOne:
    def __init__(self, row):
        self._row = row

    def fetchone(self):
        row, self._row = self._row, None
        return row


class _BarrierConnection:
    """让旧实现的「查后写」稳定同时通过查询；写事务内的查询不停顿。"""

    def __init__(self, raw, barrier):
        self._raw = raw
        self._barrier = barrier

    @property
    def isolation_level(self):
        return self._raw.isolation_level

    @isolation_level.setter
    def isolation_level(self, value):
        self._raw.isolation_level = value

    def execute(self, sql, *args, **kwargs):
        cursor = self._raw.execute(sql, *args, **kwargs)
        normalized = " ".join(sql.lower().split())
        guarded_read = (
            normalized.startswith("select handle_id from handle_items where stopped = 0")
            or normalized.startswith("select * from handle_items where handle_id = ?")
        )
        if guarded_read and not self._raw.in_transaction:
            row = cursor.fetchone()
            self._barrier.wait(timeout=5)
            return _FetchedOne(row)
        return cursor

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return self._raw.__exit__(exc_type, exc_value, traceback)

    def __getattr__(self, name):
        return getattr(self._raw, name)


def _client(tmpdir):
    db_path = Path(tmpdir) / "clinic.sqlite3"
    init_db(db_path)
    with connect(db_path) as conn:
        conn.execute(
            "insert into handle_items(handle_id, name, price, fee_type, stopped) "
            "values ('saas-001', '同步项目', 100, '治疗费', 0)"
        )
        conn.execute(
            "insert into handle_items(handle_id, name, price, fee_type, stopped, last_batch_id, source_json) "
            "values ('local-saas-001', '撞名同步项', 100, '治疗费', 0, 'batch-1', '{\"x\":1}')"
        )
        conn.commit()
    return db_path, TestClient(create_app(db_path))


class HandleItemAdminCase(unittest.TestCase):
    def setUp(self):
        perm_patcher = mock.patch(
            "local_app.routes.master_data.require_perm",
            return_value={"id": "synthetic-manager"},
        )
        self.require_perm = perm_patcher.start()
        self.addCleanup(perm_patcher.stop)

    def _create(self, client, **changes):
        payload = {**VALID_ITEM, **changes}
        response = client.post("/api/handle-items", json=payload)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["handle_id"]


class HandleItemCreateTest(HandleItemAdminCase):
    def test_create_local_item_appears_in_list(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path, client = _client(tmpdir)
            handle_id = self._create(client)
            self.assertTrue(handle_id.startswith("local-handle-"))

            groups = client.get("/api/handle-items").json()["groups"]
            items = {
                item["handle_id"]: item
                for group in groups
                for item in group["items"]
            }
            self.assertEqual(items[handle_id]["name"], "本地洁牙")
            self.assertEqual(items[handle_id]["price"], 200)
            with connect(db_path) as conn:
                row = conn.execute(
                    "select fee_type, handle_type from handle_items where handle_id=?",
                    (handle_id,),
                ).fetchone()
            self.assertEqual(tuple(row), ("治疗费", "治疗费"))

    def test_zero_price_item_with_category_is_visible(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            _, client = _client(tmpdir)
            handle_id = self._create(client, name="免费复查", code="FREE", price=0)
            groups = client.get("/api/handle-items").json()["groups"]
            ids = [item["handle_id"] for group in groups for item in group["items"]]
            self.assertIn(handle_id, ids)


class HandleItemManagementTest(HandleItemAdminCase):
    def test_manage_list_includes_active_and_stopped_with_origin(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            _, client = _client(tmpdir)
            local_id = self._create(client)
            self.assertEqual(client.delete(f"/api/handle-items/{local_id}").status_code, 200)

            response = client.get("/api/handle-items/manage", params={"status": "all"})
            self.assertEqual(response.status_code, 200, response.text)
            body = response.json()
            items = {item["handle_id"]: item for item in body["items"]}
            self.assertEqual(body["counts"], {"active": 2, "stopped": 1})
            self.assertEqual(items[local_id]["status"], "stopped")
            self.assertEqual(items[local_id]["restore_issue"], "")
            self.assertEqual(items[local_id]["origin"], "local")
            self.assertEqual(items["saas-001"]["origin"], "imported")
            self.assertEqual(items["local-saas-001"]["origin"], "imported")
            self.assertEqual(
                set(items[local_id]),
                {
                    "handle_id",
                    "code",
                    "name",
                    "unit",
                    "price",
                    "fee_type",
                    "status",
                    "restore_issue",
                    "origin",
                    "created_at",
                    "updated_at",
                },
            )

    def test_manage_status_filters_and_rejects_invalid_status(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            _, client = _client(tmpdir)
            handle_id = self._create(client)
            client.delete(f"/api/handle-items/{handle_id}")

            active = client.get("/api/handle-items/manage", params={"status": "active"})
            stopped = client.get("/api/handle-items/manage", params={"status": "stopped"})
            self.assertTrue(all(item["status"] == "active" for item in active.json()["items"]))
            self.assertEqual([item["handle_id"] for item in stopped.json()["items"]], [handle_id])
            self.assertEqual(
                client.get("/api/handle-items/manage", params={"status": "bad"}).status_code,
                400,
            )

    def test_imported_item_can_be_updated_stopped_and_restored(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path, client = _client(tmpdir)
            payload = {
                "name": "同步项目改名",
                "code": "SYNC-EDITED",
                "unit": "次",
                "price": "120.00",
                "fee_type": "治疗费",
            }
            self.assertEqual(client.put("/api/handle-items/saas-001", json=payload).status_code, 200)
            self.assertEqual(client.delete("/api/handle-items/saas-001").status_code, 200)
            self.assertEqual(client.post("/api/handle-items/saas-001/restore").status_code, 200)
            with connect(db_path) as conn:
                row = conn.execute(
                    "select name, code, unit, price, fee_type, handle_type, stopped "
                    "from handle_items where handle_id='saas-001'"
                ).fetchone()
            self.assertEqual(
                tuple(row),
                ("同步项目改名", "SYNC-EDITED", "次", 120, "治疗费", "治疗费", 0),
            )

    def test_duplicate_state_transitions_are_conflicts(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            _, client = _client(tmpdir)
            handle_id = self._create(client)
            self.assertEqual(client.delete(f"/api/handle-items/{handle_id}").status_code, 200)
            self.assertEqual(client.delete(f"/api/handle-items/{handle_id}").status_code, 409)
            self.assertEqual(client.put(f"/api/handle-items/{handle_id}", json=VALID_ITEM).status_code, 409)
            self.assertEqual(client.post(f"/api/handle-items/{handle_id}/restore").status_code, 200)
            self.assertEqual(client.post(f"/api/handle-items/{handle_id}/restore").status_code, 409)

    def test_missing_update_stop_and_restore_are_not_found(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            _, client = _client(tmpdir)
            missing = "missing-handle"
            self.assertEqual(client.put(f"/api/handle-items/{missing}", json=VALID_ITEM).status_code, 404)
            self.assertEqual(client.delete(f"/api/handle-items/{missing}").status_code, 404)
            self.assertEqual(client.post(f"/api/handle-items/{missing}/restore").status_code, 404)

    def test_missing_update_precedes_payload_validation(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            _, client = _client(tmpdir)
            response = client.put(
                "/api/handle-items/missing-handle",
                json={"name": ["非法类型"]},
            )
            self.assertEqual(response.status_code, 404, response.text)

    def test_stopped_update_precedes_payload_validation(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            _, client = _client(tmpdir)
            handle_id = self._create(client)
            self.assertEqual(client.delete(f"/api/handle-items/{handle_id}").status_code, 200)
            response = client.put(
                f"/api/handle-items/{handle_id}",
                json={"name": ["非法类型"]},
            )
            self.assertEqual(response.status_code, 409, response.text)

    def test_restore_rejects_dirty_legacy_items_without_writes(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path, client = _client(tmpdir)
            dirty_items = (
                ("dirty-name", "  ", 100, "治疗费"),
                ("dirty-fee", "项目", 100, "\t"),
                ("dirty-null-price", "项目", None, "治疗费"),
                ("dirty-bad-price", "项目", "bad", "治疗费"),
            )
            with connect(db_path) as conn:
                conn.executemany(
                    "insert into handle_items(handle_id, name, price, fee_type, stopped) "
                    "values (?, ?, ?, ?, 1)",
                    dirty_items,
                )
                conn.commit()

            for handle_id, *_ in dirty_items:
                response = client.post(f"/api/handle-items/{handle_id}/restore")
                self.assertEqual(response.status_code, 400, (handle_id, response.text))

            with connect(db_path) as conn:
                stopped_count = conn.execute(
                    "select count(*) from handle_items "
                    "where handle_id like 'dirty-%' and stopped = 1"
                ).fetchone()[0]
                restore_audits = conn.execute(
                    "select count(*) from audit_logs "
                    "where entity_id like 'dirty-%' and action = 'restore_handle_item'"
                ).fetchone()[0]
            self.assertEqual(stopped_count, len(dirty_items))
            self.assertEqual(restore_audits, 0)

    def test_dirty_legacy_item_can_be_completed_and_restored_atomically(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path, client = _client(tmpdir)
            with connect(db_path) as conn:
                conn.execute(
                    "insert into handle_items(handle_id, code, name, unit, price, fee_type, stopped) "
                    "values ('dirty-repair', ' OLD ', '历史项目', '', null, '', 1)"
                )
                conn.commit()

            managed = client.get(
                "/api/handle-items/manage", params={"status": "stopped"}
            )
            self.assertEqual(managed.status_code, 200, managed.text)
            item = next(
                row for row in managed.json()["items"]
                if row["handle_id"] == "dirty-repair"
            )
            self.assertEqual(item["restore_issue"], "费用分类不能为空")

            repaired = client.post(
                "/api/handle-items/dirty-repair/restore",
                json={
                    "name": "历史项目补全",
                    "code": "REPAIRED",
                    "unit": "次",
                    "price": "88.00",
                    "fee_type": "检查费",
                },
            )
            self.assertEqual(repaired.status_code, 200, repaired.text)
            with connect(db_path) as conn:
                row = conn.execute(
                    "select name, code, unit, price, fee_type, handle_type, stopped "
                    "from handle_items where handle_id = 'dirty-repair'"
                ).fetchone()
                audit = conn.execute(
                    "select old_json, new_json from audit_logs "
                    "where entity_id = 'dirty-repair' and action = 'restore_handle_item'"
                ).fetchone()
            self.assertEqual(
                tuple(row),
                ("历史项目补全", "REPAIRED", "次", 88, "检查费", "检查费", 0),
            )
            self.assertIsNone(json.loads(audit["old_json"])["price"])
            self.assertEqual(json.loads(audit["new_json"])["price"], 88)
            self.assertEqual(json.loads(audit["new_json"])["stopped"], 0)

    def test_manage_permission_guard_is_used_by_manage_and_all_mutations(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            _, client = _client(tmpdir)
            calls = self.require_perm.call_count
            self.assertEqual(client.get("/api/handle-items/manage").status_code, 200)
            self.assertEqual(self.require_perm.call_count, calls + 1)

            calls = self.require_perm.call_count
            handle_id = self._create(client)
            self.assertEqual(self.require_perm.call_count, calls + 1)

            calls = self.require_perm.call_count
            self.assertEqual(
                client.put(
                    f"/api/handle-items/{handle_id}",
                    json={**VALID_ITEM, "price": 210},
                ).status_code,
                200,
            )
            self.assertEqual(self.require_perm.call_count, calls + 1)

            calls = self.require_perm.call_count
            self.assertEqual(client.delete(f"/api/handle-items/{handle_id}").status_code, 200)
            self.assertEqual(self.require_perm.call_count, calls + 1)

            calls = self.require_perm.call_count
            self.assertEqual(client.post(f"/api/handle-items/{handle_id}/restore").status_code, 200)
            self.assertEqual(self.require_perm.call_count, calls + 1)

    def test_concurrent_create_with_same_code_has_one_winner(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path, _ = _client(tmpdir)
            attempts = 8
            barrier = threading.Barrier(attempts)

            def wrapped_connect(path):
                return _BarrierConnection(connect(path), barrier)

            parallel_clients = [TestClient(create_app(db_path)) for _ in range(attempts)]

            def create(index):
                return parallel_clients[index].post(
                    "/api/handle-items",
                    json={**VALID_ITEM, "name": f"并发项目{index}", "code": "RACE-CODE"},
                ).status_code

            with mock.patch("local_app.routes.master_data.connect", side_effect=wrapped_connect):
                with concurrent.futures.ThreadPoolExecutor(max_workers=attempts) as executor:
                    statuses = list(executor.map(create, range(attempts)))

            self.assertEqual(statuses.count(200), 1, statuses)
            self.assertEqual(statuses.count(409), attempts - 1, statuses)
            with connect(db_path) as conn:
                create_audits = conn.execute(
                    "select count(*) from audit_logs a "
                    "join handle_items h on h.handle_id = a.entity_id "
                    "where a.action = 'create_handle_item' and lower(trim(h.code)) = 'race-code'"
                ).fetchone()[0]
            self.assertEqual(create_audits, 1)

    def test_concurrent_stop_and_restore_each_have_one_winner(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path, client = _client(tmpdir)
            handle_id = self._create(client)
            attempts = 8

            def run_parallel(method, url):
                barrier = threading.Barrier(attempts)

                def wrapped_connect(path):
                    return _BarrierConnection(connect(path), barrier)

                parallel_clients = [TestClient(create_app(db_path)) for _ in range(attempts)]

                def request(index):
                    return getattr(parallel_clients[index], method)(url).status_code

                with mock.patch("local_app.routes.master_data.connect", side_effect=wrapped_connect):
                    with concurrent.futures.ThreadPoolExecutor(max_workers=attempts) as executor:
                        return list(executor.map(request, range(attempts)))

            stop_statuses = run_parallel("delete", f"/api/handle-items/{handle_id}")
            self.assertEqual(stop_statuses.count(200), 1, stop_statuses)
            self.assertEqual(stop_statuses.count(409), attempts - 1, stop_statuses)

            restore_statuses = run_parallel("post", f"/api/handle-items/{handle_id}/restore")
            self.assertEqual(restore_statuses.count(200), 1, restore_statuses)
            self.assertEqual(restore_statuses.count(409), attempts - 1, restore_statuses)
            with connect(db_path) as conn:
                action_counts = {
                    row["action"]: row["count"]
                    for row in conn.execute(
                        "select action, count(*) as count from audit_logs "
                        "where entity_id = ? and action in ('stop_handle_item', 'restore_handle_item') "
                        "group by action",
                        (handle_id,),
                    )
                }
            self.assertEqual(action_counts.get("stop_handle_item"), 1)
            self.assertEqual(action_counts.get("restore_handle_item"), 1)


class HandleItemValidationAuditTest(HandleItemAdminCase):
    def test_name_price_and_category_are_required(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            _, client = _client(tmpdir)
            for payload in (
                {**VALID_ITEM, "name": ""},
                {**VALID_ITEM, "price": ""},
                {key: value for key, value in VALID_ITEM.items() if key != "price"},
                {**VALID_ITEM, "fee_type": ""},
                {key: value for key, value in VALID_ITEM.items() if key != "fee_type"},
            ):
                response = client.post("/api/handle-items", json=payload)
                self.assertEqual(response.status_code, 400, (payload, response.text))

    def test_price_must_be_finite_nonnegative_and_at_most_two_decimals(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            _, client = _client(tmpdir)
            for bad_price in ("abc", "nan", "inf", "-inf", -5, "1.001"):
                response = client.post(
                    "/api/handle-items",
                    json={**VALID_ITEM, "price": bad_price, "code": ""},
                )
                self.assertEqual(response.status_code, 400, (bad_price, response.text))

    def test_text_fields_reject_non_string_json_values(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            _, client = _client(tmpdir)
            for field in ("name", "code", "unit", "fee_type"):
                for bad_value in (True, 1, ["x"], {"x": 1}):
                    response = client.post(
                        "/api/handle-items",
                        json={**VALID_ITEM, field: bad_value},
                    )
                    self.assertEqual(
                        response.status_code,
                        400,
                        (field, bad_value, response.text),
                    )

    def test_optional_code_and_unit_accept_missing_or_null(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path, client = _client(tmpdir)
            missing_payload = {
                key: value for key, value in VALID_ITEM.items() if key not in {"code", "unit"}
            }
            first_response = client.post("/api/handle-items", json=missing_payload)
            self.assertEqual(first_response.status_code, 200, first_response.text)
            first_id = first_response.json()["handle_id"]
            second_response = client.post(
                "/api/handle-items",
                json={**VALID_ITEM, "name": "空编码单位", "code": None, "unit": None},
            )
            self.assertEqual(second_response.status_code, 200, second_response.text)
            second_id = second_response.json()["handle_id"]
            with connect(db_path) as conn:
                rows = conn.execute(
                    "select code, unit from handle_items where handle_id in (?, ?) order by handle_id",
                    (first_id, second_id),
                ).fetchall()
            self.assertEqual([tuple(row) for row in rows], [("", ""), ("", "")])

    def test_historical_tabbed_casefolded_code_blocks_create(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path, client = _client(tmpdir)
            with connect(db_path) as conn:
                conn.execute(
                    "insert into handle_items(handle_id, code, name, price, fee_type, stopped) "
                    "values ('legacy-tab-code', '\tStraße\n', '历史编码项', 10, '治疗费', 0)"
                )
                conn.commit()
            response = client.post(
                "/api/handle-items",
                json={**VALID_ITEM, "name": "冲突项目", "code": "STRASSE"},
            )
            self.assertEqual(response.status_code, 409, response.text)

    def test_existing_duplicate_code_can_update_when_normalized_code_is_unchanged(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path, client = _client(tmpdir)
            with connect(db_path) as conn:
                conn.execute(
                    "insert into handle_items(handle_id, code, name, unit, price, fee_type, stopped) "
                    "values ('legacy-dup-a', '\tDUP\n', '存量重复A', '次', 10, '治疗费', 0)"
                )
                conn.execute(
                    "insert into handle_items(handle_id, code, name, unit, price, fee_type, stopped) "
                    "values ('legacy-dup-b', 'dup', '存量重复B', '次', 20, '治疗费', 0)"
                )
                conn.commit()

            response = client.put(
                "/api/handle-items/legacy-dup-a",
                json={
                    "name": "存量重复A改名",
                    "code": "\tDUP\n",
                    "unit": "次",
                    "price": "30.00",
                    "fee_type": "治疗费",
                },
            )
            self.assertEqual(response.status_code, 200, response.text)
            with connect(db_path) as conn:
                row = conn.execute(
                    "select name, code, price from handle_items where handle_id = 'legacy-dup-a'"
                ).fetchone()
            self.assertEqual(tuple(row), ("存量重复A改名", "DUP", 30))

    def test_active_code_conflicts_are_case_insensitive_and_trimmed(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            _, client = _client(tmpdir)
            first_id = self._create(client)
            second_id = self._create(client, name="无编码项目", code="")

            duplicate = client.post(
                "/api/handle-items",
                json={**VALID_ITEM, "name": "另一项目", "code": " clean-local "},
            )
            self.assertEqual(duplicate.status_code, 409)
            update = client.put(
                f"/api/handle-items/{second_id}",
                json={**VALID_ITEM, "name": "无编码项目", "code": "cLeAn-LoCaL"},
            )
            self.assertEqual(update.status_code, 409)

            self.assertEqual(client.delete(f"/api/handle-items/{first_id}").status_code, 200)
            replacement_id = self._create(client, name="替代项目", code=" clean-local ")
            self.assertNotEqual(replacement_id, first_id)
            self.assertEqual(client.post(f"/api/handle-items/{first_id}/restore").status_code, 409)

    def test_all_mutations_write_full_json_snapshots(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path, client = _client(tmpdir)
            handle_id = self._create(client)
            client.put(
                f"/api/handle-items/{handle_id}",
                json={**VALID_ITEM, "name": "本地洁牙改", "price": "230.00"},
            )
            client.delete(f"/api/handle-items/{handle_id}")
            client.post(f"/api/handle-items/{handle_id}/restore")

            with connect(db_path) as conn:
                rows = conn.execute(
                    "select action, old_json, new_json from audit_logs "
                    "where entity_type='handle_item' and entity_id=? order by rowid",
                    (handle_id,),
                ).fetchall()
            self.assertEqual(
                [row["action"] for row in rows],
                [
                    "create_handle_item",
                    "update_handle_item",
                    "stop_handle_item",
                    "restore_handle_item",
                ],
            )
            snapshots = []
            for index, row in enumerate(rows):
                old_value = json.loads(row["old_json"]) if row["old_json"] else None
                new_value = json.loads(row["new_json"])
                if index == 0:
                    self.assertIsNone(old_value)
                else:
                    self.assertEqual(set(old_value), SNAPSHOT_FIELDS)
                    self.assertEqual(old_value["handle_id"], handle_id)
                self.assertEqual(set(new_value), SNAPSHOT_FIELDS)
                self.assertEqual(new_value["handle_id"], handle_id)
                snapshots.append((old_value, new_value))

            self.assertEqual(snapshots[0][1]["stopped"], 0)
            self.assertEqual(snapshots[1][1]["name"], "本地洁牙改")
            self.assertEqual(snapshots[2][1]["stopped"], 1)
            self.assertEqual(snapshots[3][1]["stopped"], 0)

    def test_audit_failure_rolls_back_item_mutation(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path, client = _client(tmpdir)
            handle_id = self._create(client)
            with mock.patch(
                "local_app.routes.master_data.audit_write",
                side_effect=RuntimeError("synthetic audit failure"),
            ):
                with self.assertRaisesRegex(RuntimeError, "synthetic audit failure"):
                    client.put(
                        f"/api/handle-items/{handle_id}",
                        json={**VALID_ITEM, "name": "不应落库"},
                    )
            with connect(db_path) as conn:
                row = conn.execute(
                    "select name, price, stopped from handle_items where handle_id=?",
                    (handle_id,),
                ).fetchone()
            self.assertEqual(tuple(row), ("本地洁牙", 200, 0))

    def test_master_item_changes_do_not_rewrite_treatment_snapshot(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path, client = _client(tmpdir)
            handle_id = self._create(client)
            with connect(db_path) as conn:
                conn.execute(
                    "insert into patients(patient_identity, display_name, updated_at, current_hash) "
                    "values ('p-snapshot', '合成患者', 'x', 'hash')"
                )
                conn.execute(
                    "insert into treatment_items(treatment_item_id, patient_identity, item_name, item_code, "
                    "unit_price, quantity, total_fee, fee_type) values "
                    "('ti-snapshot', 'p-snapshot', '本地洁牙', 'CLEAN-LOCAL', 200, 1, 200, '治疗费')"
                )
                conn.commit()

            update_response = client.put(
                f"/api/handle-items/{handle_id}",
                json={**VALID_ITEM, "name": "洁牙新名", "code": "CLEAN-NEW", "price": "260.00"},
            )
            self.assertEqual(update_response.status_code, 200, update_response.text)
            stop_response = client.delete(f"/api/handle-items/{handle_id}")
            self.assertEqual(stop_response.status_code, 200, stop_response.text)
            with connect(db_path) as conn:
                master_row = conn.execute(
                    "select name, code, price, stopped from handle_items where handle_id=?",
                    (handle_id,),
                ).fetchone()
                treatment_row = conn.execute(
                    "select item_name, item_code, unit_price, total_fee, fee_type "
                    "from treatment_items where treatment_item_id='ti-snapshot'"
                ).fetchone()
            self.assertEqual(tuple(master_row), ("洁牙新名", "CLEAN-NEW", 260, 1))
            self.assertEqual(
                tuple(treatment_row),
                ("本地洁牙", "CLEAN-LOCAL", 200, 200, "治疗费"),
            )


class HandleItemLegacyAdminIntegrationTest(unittest.TestCase):
    def test_legacy_admin_can_manage_while_regular_role_is_denied(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "clinic.sqlite3"
            init_db(db_path)
            with connect(db_path) as conn:
                auth.create_user(conn, "legacy-admin", "合成管理员", "admin-pw", role="admin")
                auth.create_user(conn, "legacy-desk", "合成前台", "desk-pw", role="reception")
                conn.commit()

            app = create_app(db_path, require_login=True, access_v3=False)
            admin = TestClient(app)
            self.assertEqual(
                admin.post(
                    "/api/auth/login",
                    json={"username": "legacy-admin", "password": "admin-pw"},
                ).status_code,
                200,
            )
            created = admin.post("/api/handle-items", json=VALID_ITEM)
            self.assertEqual(created.status_code, 200, created.text)
            handle_id = created.json()["handle_id"]
            self.assertEqual(admin.get("/api/handle-items/manage").status_code, 200)
            self.assertEqual(
                admin.put(
                    f"/api/handle-items/{handle_id}",
                    json={**VALID_ITEM, "price": "210.00"},
                ).status_code,
                200,
            )
            self.assertEqual(admin.delete(f"/api/handle-items/{handle_id}").status_code, 200)
            self.assertEqual(admin.post(f"/api/handle-items/{handle_id}/restore").status_code, 200)

            regular = TestClient(app)
            self.assertEqual(
                regular.post(
                    "/api/auth/login",
                    json={"username": "legacy-desk", "password": "desk-pw"},
                ).status_code,
                200,
            )
            self.assertEqual(regular.get("/api/handle-items/manage").status_code, 403)
            self.assertEqual(regular.post("/api/handle-items", json=VALID_ITEM).status_code, 403)


if __name__ == "__main__":
    unittest.main()
