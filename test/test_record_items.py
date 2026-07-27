import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from local_app.api import create_app
from local_app.db import connect, init_db
from local_app.routes import record_items as record_items_module


def _client(tmpdir):
    db_path = Path(tmpdir) / "clinic.sqlite3"
    init_db(db_path)
    with connect(db_path) as conn:
        conn.execute(
            "insert into patients(patient_identity, display_name, updated_at, current_hash) "
            "values ('p1', '测试患者', '2026-06-07 00:00:00', 'h1')"
        )
        conn.execute(
            "insert into patients(patient_identity, display_name, updated_at, current_hash) "
            "values ('p2', '另一患者', '2026-06-07 00:00:00', 'h2')"
        )
        conn.commit()
    return db_path, TestClient(create_app(db_path))


def _create_record(client, patient="p1", visit_time="2026-06-10 09:00:00"):
    resp = client.post(
        "/api/medical-records",
        json={
            "patient_identity": patient,
            "record_type": "初诊",
            "visit_time": visit_time,
            "doctor_name": "王医生",
        },
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["medical_record"]["record_id"]


def _sample_items():
    return [
        {"tooth": "36", "item_type": "exam", "content": "深龋及髓"},
        {"tooth": "36", "item_type": "diagnosis", "content": "慢性牙髓炎"},
        {"tooth": "36", "item_type": "plan", "content": "根管治疗"},
        {"tooth": "11", "item_type": "treatment", "content": "树脂充填"},
        {"tooth": "", "item_type": "diagnosis", "content": "全口牙周炎"},
    ]


class RecordItemsCrudTest(unittest.TestCase):
    def test_put_then_get_items(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path, client = _client(tmpdir)
            record_id = _create_record(client)
            resp = client.put(
                f"/api/medical-records/{record_id}/items",
                json={"items": _sample_items()},
            )
            self.assertEqual(resp.status_code, 200)
            self.assertEqual(resp.json()["item_count"], 5)

            listed = client.get(f"/api/medical-records/{record_id}/items").json()
            self.assertEqual(listed["totalcount"], 5)
            items = listed["items"]
            # 保持提交顺序（sort_order）
            self.assertEqual(
                [(i["tooth"], i["item_type"]) for i in items],
                [("36", "exam"), ("36", "diagnosis"), ("36", "plan"),
                 ("11", "treatment"), ("", "diagnosis")],
            )
            self.assertTrue(all(i["item_id"].startswith("local-item-") for i in items))

    def test_put_replaces_previous_items(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path, client = _client(tmpdir)
            record_id = _create_record(client)
            client.put(
                f"/api/medical-records/{record_id}/items",
                json={"items": _sample_items()},
            )
            resp = client.put(
                f"/api/medical-records/{record_id}/items",
                json={"items": [
                    {"tooth": "47", "item_type": "exam", "content": "残根"},
                ]},
            )
            self.assertEqual(resp.status_code, 200)
            listed = client.get(f"/api/medical-records/{record_id}/items").json()
            self.assertEqual(listed["totalcount"], 1)
            self.assertEqual(listed["items"][0]["tooth"], "47")

    def test_blank_content_items_skipped(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path, client = _client(tmpdir)
            record_id = _create_record(client)
            resp = client.put(
                f"/api/medical-records/{record_id}/items",
                json={"items": [
                    {"tooth": "36", "item_type": "exam", "content": "深龋"},
                    {"tooth": "37", "item_type": "exam", "content": "   "},
                ]},
            )
            self.assertEqual(resp.json()["item_count"], 1)

    def test_invalid_item_type_rejected(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path, client = _client(tmpdir)
            record_id = _create_record(client)
            resp = client.put(
                f"/api/medical-records/{record_id}/items",
                json={"items": [
                    {"tooth": "36", "item_type": "随便", "content": "x"},
                ]},
            )
            self.assertEqual(resp.status_code, 400)

    def test_items_must_be_list(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path, client = _client(tmpdir)
            record_id = _create_record(client)
            resp = client.put(
                f"/api/medical-records/{record_id}/items",
                json={"items": "not-a-list"},
            )
            self.assertEqual(resp.status_code, 400)

    def test_unknown_record_404(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path, client = _client(tmpdir)
            resp = client.put(
                "/api/medical-records/no-such/items", json={"items": []}
            )
            self.assertEqual(resp.status_code, 404)
            resp = client.get("/api/medical-records/no-such/items")
            self.assertEqual(resp.status_code, 404)

    def test_put_items_writes_audit_log(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path, client = _client(tmpdir)
            record_id = _create_record(client)
            client.put(
                f"/api/medical-records/{record_id}/items",
                json={"items": _sample_items()},
            )
            with connect(db_path) as conn:
                row = conn.execute(
                    "select count(*) c from audit_logs "
                    "where entity_type='medical_record' and entity_id=? "
                    "and action='update_record_items'",
                    (record_id,),
                ).fetchone()
            self.assertEqual(row["c"], 1)


class ToothHistoryTest(unittest.TestCase):
    def test_tooth_history_across_records(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path, client = _client(tmpdir)
            r1 = _create_record(client, visit_time="2026-05-01 09:00:00")
            r2 = _create_record(client, visit_time="2026-06-10 09:00:00")
            client.put(
                f"/api/medical-records/{r1}/items",
                json={"items": [
                    {"tooth": "36", "item_type": "diagnosis", "content": "慢性牙髓炎"},
                    {"tooth": "36", "item_type": "plan", "content": "根管治疗"},
                    {"tooth": "11", "item_type": "exam", "content": "牙结石"},
                ]},
            )
            client.put(
                f"/api/medical-records/{r2}/items",
                json={"items": [
                    {"tooth": "36", "item_type": "treatment", "content": "根管充填"},
                ]},
            )
            resp = client.get("/api/patients/p1/teeth/36/history")
            self.assertEqual(resp.status_code, 200)
            body = resp.json()
            self.assertEqual(body["tooth"], "36")
            # 按就诊时间倒序分组
            self.assertEqual([v["record_id"] for v in body["visits"]], [r2, r1])
            self.assertEqual(body["visits"][0]["visit_time"], "2026-06-10 09:00:00")
            self.assertEqual(body["visits"][0]["doctor_name"], "王医生")
            self.assertEqual(
                [i["item_type"] for i in body["visits"][1]["items"]],
                ["diagnosis", "plan"],
            )
            # 11 号牙不混进来
            all_items = [i for v in body["visits"] for i in v["items"]]
            self.assertTrue(all("36" == i["tooth"] for i in all_items))

    def test_tooth_history_filter_by_type(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path, client = _client(tmpdir)
            r1 = _create_record(client)
            client.put(
                f"/api/medical-records/{r1}/items",
                json={"items": [
                    {"tooth": "36", "item_type": "diagnosis", "content": "慢性牙髓炎"},
                    {"tooth": "36", "item_type": "plan", "content": "根管治疗"},
                ]},
            )
            body = client.get(
                "/api/patients/p1/teeth/36/history?item_type=plan"
            ).json()
            items = [i for v in body["visits"] for i in v["items"]]
            self.assertEqual(len(items), 1)
            self.assertEqual(items[0]["item_type"], "plan")

    def test_tooth_history_unknown_patient_404(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path, client = _client(tmpdir)
            resp = client.get("/api/patients/nobody/teeth/36/history")
            self.assertEqual(resp.status_code, 404)

    def test_teeth_summary(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path, client = _client(tmpdir)
            r1 = _create_record(client, visit_time="2026-05-01 09:00:00")
            r2 = _create_record(client, visit_time="2026-06-10 09:00:00")
            client.put(
                f"/api/medical-records/{r1}/items",
                json={"items": [
                    {"tooth": "36", "item_type": "diagnosis", "content": "慢性牙髓炎"},
                    {"tooth": "36", "item_type": "plan", "content": "根管治疗"},
                    {"tooth": "11", "item_type": "exam", "content": "牙结石"},
                    {"tooth": "", "item_type": "diagnosis", "content": "全口牙周炎"},
                ]},
            )
            client.put(
                f"/api/medical-records/{r2}/items",
                json={"items": [
                    {"tooth": "36", "item_type": "treatment", "content": "根管充填"},
                ]},
            )
            resp = client.get("/api/patients/p1/teeth/summary")
            self.assertEqual(resp.status_code, 200)
            teeth = {t["tooth"]: t for t in resp.json()["teeth"]}
            # 空牙位（全口/记录级）不进牙图汇总
            self.assertEqual(set(teeth), {"36", "11"})
            self.assertEqual(teeth["36"]["counts"], {"diagnosis": 1, "plan": 1, "treatment": 1})
            self.assertEqual(teeth["36"]["last_visit_time"], "2026-06-10 09:00:00")
            self.assertEqual(teeth["11"]["counts"], {"exam": 1})

    def test_teeth_summary_unknown_patient_404(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path, client = _client(tmpdir)
            resp = client.get("/api/patients/nobody/teeth/summary")
            self.assertEqual(resp.status_code, 404)

    def test_tooth_history_isolated_per_patient(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path, client = _client(tmpdir)
            r1 = _create_record(client, patient="p1")
            client.put(
                f"/api/medical-records/{r1}/items",
                json={"items": [
                    {"tooth": "36", "item_type": "exam", "content": "深龋"},
                ]},
            )
            body = client.get("/api/patients/p2/teeth/36/history").json()
            self.assertEqual(body["visits"], [])


if __name__ == "__main__":
    unittest.main()


class PatientRecordItemsBatchTest(unittest.TestCase):
    def test_draft_confirmed_between_reads_does_not_break_timeline(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path, client = _client(tmpdir)
            with connect(db_path) as conn:
                conn.execute(
                    """
                    insert into medical_records(
                        record_id, patient_identity, record_type, visit_time,
                        draft_status, current_hash, created_at, updated_at
                    ) values (
                        'r-concurrent-draft', 'p1', '合成类型',
                        '2026-07-21 09:00:00', 'ai_draft', 'hash-draft',
                        '2026-07-21 09:00:00', '2026-07-21 09:00:00'
                    )
                    """
                )
                conn.execute(
                    """
                    insert into medical_record_items(
                        item_id, record_id, patient_identity, tooth, item_type,
                        content, sort_order, created_at, updated_at
                    ) values (
                        'item-concurrent', 'r-concurrent-draft', 'p1', '36',
                        'exam', '合成条目', 0,
                        '2026-07-21 09:00:00', '2026-07-21 09:00:00'
                    )
                    """
                )
                conn.commit()

            real_connect = record_items_module.connect

            class PromoteAfterTimelineFetchCursor:
                def __init__(self, cursor, promote):
                    self._cursor = cursor
                    self._promote = promote

                def fetchall(self):
                    rows = self._cursor.fetchall()
                    self._promote()
                    return rows

                def __getattr__(self, name):
                    return getattr(self._cursor, name)

            class InterleavingConnection:
                def __init__(self, path):
                    self._path = path
                    self._conn = real_connect(path)
                    self._promoted = False

                def __enter__(self):
                    self._conn.__enter__()
                    return self

                def __exit__(self, *args):
                    return self._conn.__exit__(*args)

                def __getattr__(self, name):
                    return getattr(self._conn, name)

                def execute(self, sql, params=()):
                    cursor = self._conn.execute(sql, params)
                    normalized = " ".join(sql.lower().split())
                    is_timeline = (
                        "record_type" in normalized
                        and "visit_time" in normalized
                        and "draft_status" in normalized
                        and "from medical_records" in normalized
                    )
                    if is_timeline and not self._promoted:
                        return PromoteAfterTimelineFetchCursor(cursor, self._promote)
                    return cursor

                def _promote(self):
                    self._promoted = True
                    with real_connect(self._path) as writer:
                        writer.execute(
                            "update medical_records set draft_status = '' "
                            "where record_id = 'r-concurrent-draft'"
                        )
                        writer.commit()

            with patch.object(
                record_items_module,
                "connect",
                side_effect=lambda path: InterleavingConnection(path),
            ):
                response = client.get("/api/patients/p1/record-items")

            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json(), {
                "medical_records": [], "records": {}, "totalcount": 0,
            })

    def test_formal_record_without_items_is_returned(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path, client = _client(tmpdir)
            record_id = _create_record(
                client, visit_time="2026-07-21 09:00:00"
            )

            resp = client.get("/api/patients/p1/record-items")

            self.assertEqual(resp.status_code, 200)
            body = resp.json()
            self.assertEqual(
                [row["record_id"] for row in body["medical_records"]],
                [record_id],
            )
            self.assertEqual(
                set(body["medical_records"][0]),
                {
                    "record_id",
                    "record_type",
                    "visit_time",
                    "doctor_name",
                    "created_at",
                    "updated_at",
                },
            )
            self.assertEqual(body["records"], {record_id: []})
            self.assertEqual(body["totalcount"], 0)

    def test_drafts_and_discarded_records_and_items_are_excluded(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path, client = _client(tmpdir)
            formal_id = _create_record(
                client, visit_time="2026-07-21 09:00:00"
            )
            with connect(db_path) as conn:
                conn.executemany(
                    """
                    insert into medical_records(
                        record_id, patient_identity, record_type, visit_time,
                        draft_status, current_hash, created_at, updated_at
                    ) values (?, 'p1', '合成类型', ?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            "r-ai-draft",
                            "2026-07-22 09:00:00",
                            "ai_draft",
                            "hash-draft",
                            "2026-07-22 09:00:00",
                            "2026-07-22 09:00:00",
                        ),
                        (
                            "r-discarded",
                            "2026-07-23 09:00:00",
                            "discarded",
                            "hash-discarded",
                            "2026-07-23 09:00:00",
                            "2026-07-23 09:00:00",
                        ),
                    ],
                )
                conn.executemany(
                    """
                    insert into medical_record_items(
                        item_id, record_id, patient_identity, tooth, item_type,
                        content, sort_order, created_at, updated_at
                    ) values (?, ?, 'p1', '', 'exam', ?, 0, ?, ?)
                    """,
                    [
                        (
                            "item-formal",
                            formal_id,
                            "正式条目",
                            "2026-07-21 09:00:00",
                            "2026-07-21 09:00:00",
                        ),
                        (
                            "item-draft",
                            "r-ai-draft",
                            "草稿条目",
                            "2026-07-22 09:00:00",
                            "2026-07-22 09:00:00",
                        ),
                        (
                            "item-discarded",
                            "r-discarded",
                            "丢弃条目",
                            "2026-07-23 09:00:00",
                            "2026-07-23 09:00:00",
                        ),
                    ],
                )
                conn.commit()

            body = client.get("/api/patients/p1/record-items").json()

            self.assertEqual(
                [row["record_id"] for row in body["medical_records"]],
                [formal_id],
            )
            self.assertEqual(set(body["records"]), {formal_id})
            self.assertEqual(
                [item["item_id"] for item in body["records"][formal_id]],
                ["item-formal"],
            )
            self.assertEqual(body["totalcount"], 1)

    def test_item_ownership_follows_parent_record(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path, client = _client(tmpdir)
            record_id = _create_record(client)
            with connect(db_path) as conn:
                conn.execute(
                    """
                    insert into medical_record_items(
                        item_id, record_id, patient_identity, tooth, item_type,
                        content, sort_order, created_at, updated_at
                    ) values (
                        'item-parent', ?, 'p2', '36', 'exam',
                        '父病历归属条目', 0, '2026-07-21', '2026-07-21'
                    )
                    """,
                    (record_id,),
                )
                conn.commit()

            body = client.get("/api/patients/p1/record-items").json()

            self.assertEqual(
                [item["item_id"] for item in body["records"][record_id]],
                ["item-parent"],
            )

    def test_soft_deleted_patient_returns_404(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path, client = _client(tmpdir)
            _create_record(client)
            with connect(db_path) as conn:
                conn.execute(
                    "update patients set is_deleted = 1 where patient_identity = 'p1'"
                )
                conn.commit()

            resp = client.get("/api/patients/p1/record-items")

            self.assertEqual(resp.status_code, 404)

    def test_items_grouped_by_record(self):
        import tempfile as _tf
        with _tf.TemporaryDirectory() as tmpdir:
            db_path, client = _client(tmpdir)
            r1 = _create_record(client, visit_time="2026-05-01 09:00:00")
            r2 = _create_record(client, visit_time="2026-06-10 09:00:00")
            client.put(
                f"/api/medical-records/{r1}/items",
                json={"items": [
                    {"tooth": "36", "item_type": "exam", "content": "深龋"},
                ]},
            )
            client.put(
                f"/api/medical-records/{r2}/items",
                json={"items": [
                    {"tooth": "11", "item_type": "plan", "content": "贴面"},
                    {"tooth": "12", "item_type": "plan", "content": "贴面"},
                ]},
            )
            resp = client.get("/api/patients/p1/record-items")
            self.assertEqual(resp.status_code, 200)
            body = resp.json()
            self.assertEqual(body["totalcount"], 3)
            self.assertEqual(
                [row["record_id"] for row in body["medical_records"]],
                [r2, r1],
            )
            self.assertEqual(set(body["records"]), {r1, r2})
            self.assertEqual(len(body["records"][r2]), 2)

    def test_unknown_patient_404(self):
        import tempfile as _tf
        with _tf.TemporaryDirectory() as tmpdir:
            db_path, client = _client(tmpdir)
            resp = client.get("/api/patients/nobody/record-items")
            self.assertEqual(resp.status_code, 404)
