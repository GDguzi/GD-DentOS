import asyncio
import csv
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from fastapi.responses import StreamingResponse
from starlette.requests import ClientDisconnect

from local_app.api import create_app
from local_app.db import connect, init_db
from customer_hub_access_fixture import CustomerHubAccessFixture
from local_app.routes import return_visit_ops


class ExportTest(unittest.TestCase):
    def _db(self, tmp):
        db = Path(tmp) / "clinic.sqlite3"
        init_db(db)
        with connect(db) as conn:
            conn.execute("insert into patients(patient_identity,display_name,responsible_doctor,updated_at,current_hash) values('p1','患者甲','主治甲','x','h1')")
            conn.execute("insert into patients(patient_identity,display_name,is_deleted,updated_at,current_hash) values('pd','停用患者',1,'x','hd')")
            conn.execute("insert into return_visits(return_visit_id,patient_identity,due_time,item_name,status,visitor,channel,return_result,revision) values('rv1','p1','2026-06-10 09:00','=1+1','待回访','回访人甲','phone','@SUM(A1)',3)")
            conn.execute("insert into return_visits(return_visit_id,patient_identity,due_time,item_name,status,visitor,channel,return_result) values('rv2','p1','2026-06-11 09:00','其他事项','待回访','回访人乙','wechat','普通结果')")
            conn.execute("insert into return_visits(return_visit_id,patient_identity,due_time,item_name,status) values('hidden','pd','2026-06-12','隐藏事项','待回访')")
            conn.commit()
        return db

    def _client(self, db):
        return TestClient(create_app(db))

    def _export_response(self, db, tmp):
        router = return_visit_ops.create_return_visit_ops_router(db, Path(tmp) / "images")
        endpoint = next(
            route.endpoint for route in router.routes
            if route.path == "/api/return-visits/export" and "GET" in route.methods
        )
        return endpoint(
            date_from="2026-06-01", date_to="2026-06-30", status="all",
        )

    async def _serve_asgi24(self, response, *, disconnect=False):
        messages = []

        async def receive():
            return {"type": "http.disconnect"}

        async def send(message):
            if disconnect and message["type"] == "http.response.body" and message.get("body"):
                raise OSError("synthetic client disconnect")
            messages.append(message)

        scope = {"type": "http", "asgi": {"spec_version": "2.4"}}
        await response(scope, receive, send)
        return messages

    def test_export_reuses_filter_and_escapes_formulas(self):
        with tempfile.TemporaryDirectory() as tmp:
            text = self._client(self._db(tmp)).get(
                "/api/return-visits/export?date_from=2026-06-01&date_to=2026-06-30&status=all&visitor=回访人甲&channel=phone"
            ).content.decode("utf-8-sig")
            self.assertIn("'=1+1", text)
            self.assertIn("'@SUM(A1)", text)
            self.assertNotIn("其他事项", text)
            self.assertNotIn("隐藏事项", text)

    def test_export_is_not_paginated_and_has_stable_download_contract(self):
        with tempfile.TemporaryDirectory() as tmp:
            response = self._client(self._db(tmp)).get(
                "/api/return-visits/export?date_from=2026-06-01&date_to=2026-06-30&status=all&pageno=99&pagesize=1"
            )
            self.assertEqual(response.status_code, 200)
            self.assertIn("text/csv", response.headers["content-type"])
            self.assertIn("attachment", response.headers["content-disposition"])
            text = response.content.decode("utf-8-sig")
            self.assertIn("其他事项", text)
            self.assertIn("'=1+1", text)

    def test_export_query_and_close_stay_in_the_route_worker(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = self._db(tmp)
            events = []
            real_connect = return_visit_ops.connect

            class TrackingCursor:
                def __init__(self, cursor):
                    self.cursor = cursor

                def fetchmany(self, size):
                    events.append(("fetchmany", threading.get_ident()))
                    return self.cursor.fetchmany(size)

            class TrackingConnection:
                def __init__(self, conn):
                    self.conn = conn

                def execute(self, *args, **kwargs):
                    events.append(("execute", threading.get_ident()))
                    return TrackingCursor(self.conn.execute(*args, **kwargs))

                def close(self):
                    events.append(("close", threading.get_ident()))
                    self.conn.close()

            def tracked_connect(path):
                events.append(("connect", threading.get_ident()))
                return TrackingConnection(real_connect(path))

            def build_response():
                return threading.get_ident(), self._export_response(db, tmp)

            with patch.object(return_visit_ops, "connect", new=tracked_connect):
                with ThreadPoolExecutor(max_workers=1) as executor:
                    route_thread, response = executor.submit(build_response).result()

            self.assertIsInstance(response, StreamingResponse)
            self.assertEqual({thread_id for _event, thread_id in events}, {route_thread})
            self.assertEqual(events[0][0], "connect")
            self.assertIn("fetchmany", [event for event, _thread_id in events])
            self.assertEqual(events[-1][0], "close")
            self.assertNotEqual(route_thread, threading.get_ident())
            asyncio.run(self._serve_asgi24(response))

    def test_streaming_response_closes_spool_after_normal_exhaustion(self):
        with tempfile.TemporaryDirectory() as tmp:
            response = self._export_response(self._db(tmp), tmp)
            self.assertIsInstance(response, StreamingResponse)
            self.assertIsNotNone(response.background)
            spool_file = response.background.func.__self__

            async def exercise():
                messages = await self._serve_asgi24(response)
                self.assertTrue(spool_file.closed)
                return b"".join(
                    message.get("body", b"")
                    for message in messages if message["type"] == "http.response.body"
                ).decode("utf-8")

            text = asyncio.run(exercise())
            self.assertIn("'=1+1", text)
            self.assertIn("'@SUM(A1)", text)
            self.assertIn("其他事项", text)

    def test_streaming_response_closes_spool_immediately_on_asgi24_disconnect(self):
        with tempfile.TemporaryDirectory() as tmp:
            response = self._export_response(self._db(tmp), tmp)
            self.assertIsInstance(response, StreamingResponse)
            self.assertIsNotNone(response.background)
            spool_file = response.background.func.__self__

            async def exercise():
                with self.assertRaises(ClientDisconnect):
                    await self._serve_asgi24(response, disconnect=True)
                self.assertTrue(spool_file.closed)
                await asyncio.sleep(0.05)
                self.assertTrue(spool_file.closed)

            asyncio.run(exercise())

    def test_export_policy_is_all_of_profile_export_and_return_visit_view(self):
        from local_app.access_policy import ROUTE_POLICY_BY_KEY

        policy = ROUTE_POLICY_BY_KEY[("GET", "/api/return-visits/export")]
        self.assertEqual(policy.policy_type, "ALL_OF")
        self.assertEqual(policy.permissions, ("patient.profile.export", "return_visit.view"))

    def test_export_all_of_is_enforced_by_access_v3(self):
        fixture = CustomerHubAccessFixture()
        try:
            with connect(fixture.db_path) as conn:
                conn.execute(
                    "insert into patients(patient_identity,display_name,updated_at,current_hash) "
                    "values('export-patient','合成患者','x','export-hash')"
                )
                conn.execute(
                    "insert into return_visits(return_visit_id,patient_identity,due_time,item_name,status) "
                    "values('export-rv','export-patient','2026-06-10 09:00','合成事项','待回访')"
                )
                conn.commit()
            path = "/api/return-visits/export?date_from=2026-06-01&date_to=2026-06-30&status=all"
            only_export = fixture.client_with_permissions(("patient.profile.export",))
            self.assertEqual(only_export.get(path).status_code, 403)
            only_view = fixture.client_with_permissions(("return_visit.view",))
            self.assertEqual(only_view.get(path).status_code, 403)
            both = fixture.client_with_permissions(("patient.profile.export", "return_visit.view"))
            self.assertEqual(both.get(path).status_code, 200)
        finally:
            fixture.close()

    def test_export_attachment_filters_require_asset_permissions(self):
        fixture = CustomerHubAccessFixture()
        try:
            base = ("patient.profile.export", "return_visit.view")
            path = "/api/return-visits/export?date_from=2026-06-01&date_to=2026-06-30&status=all"

            without_recording = fixture.client_with_permissions(base)
            self.assertEqual(without_recording.get(path + "&att=recording").status_code, 403)
            with_recording = fixture.client_with_permissions(base + ("call_record.view",))
            self.assertEqual(with_recording.get(path + "&att=recording").status_code, 200)

            without_image = fixture.client_with_permissions(base)
            self.assertEqual(without_image.get(path + "&att=image").status_code, 403)
            with_image = fixture.client_with_permissions(base + ("patient.image.view",))
            self.assertEqual(with_image.get(path + "&att=image").status_code, 200)

            screenshot = fixture.client_with_permissions(base)
            self.assertEqual(screenshot.get(path + "&att=screenshot").status_code, 200)
        finally:
            fixture.close()

    def test_streaming_csv_fetches_bounded_batches_without_fetchall(self):
        self.assertTrue(hasattr(return_visit_ops, "stream_return_visit_csv"))
        self.assertTrue(hasattr(return_visit_ops, "CSV_BATCH_SIZE"))
        batch_size = return_visit_ops.CSV_BATCH_SIZE
        total = batch_size * 2 + 3
        rows = []
        for i in range(total):
            rows.append({
                "due_time": "2026-06-10 09:00", "display_name": f"合成患者{i}", "phone": "",
                "item_name": "=1+1" if i == 0 else f"事项{i}", "note": "", "channel": "phone",
                "return_type": "", "return_result": "@SUM(A1)" if i == 0 else "", "responsible_doctor": "",
                "return_doctor": "", "visitor": "", "status": "待回访", "actual_date": "", "revision": 1,
            })

        class FakeCursor:
            def __init__(self, values):
                self.values = values
                self.offset = 0
                self.fetch_sizes = []

            def fetchall(self):
                raise AssertionError("streaming export must not call fetchall")

            def fetchmany(self, size):
                self.fetch_sizes.append(size)
                batch = self.values[self.offset:self.offset + size]
                self.offset += len(batch)
                return batch

        cursor = FakeCursor(rows)
        with tempfile.SpooledTemporaryFile(
            max_size=64, mode="w+", encoding="utf-8", newline="",
        ) as output:
            return_visit_ops.write_return_visit_csv(cursor, output)
            output.flush()
            output.seek(0)
            text = output.read()
            output.seek(0)
            self.assertEqual(output.read(1), "\ufeff")
            parsed = list(csv.reader(output))
        self.assertEqual(text.count("\ufeff"), 1)
        self.assertEqual(text.count("回访时间"), 1)
        self.assertEqual(len(parsed), total + 1)
        self.assertTrue(all(size == batch_size for size in cursor.fetch_sizes))
        self.assertGreaterEqual(len(cursor.fetch_sizes), 4)
        self.assertIn("'=1+1", text)
        self.assertIn("'@SUM(A1)", text)


if __name__ == "__main__":
    unittest.main()
