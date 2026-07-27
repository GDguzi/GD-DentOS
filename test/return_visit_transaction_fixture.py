"""回访写事务的共享合成 Fixture（纯核心依赖，不 import 迁移扩展）。

本模块被多个回访测试模块共享；共享 Fixture 必须独立存在且不依赖可选扩展，
核心测试才能照常运行。
"""
from __future__ import annotations

import sqlite3
from unittest import mock

from fastapi.testclient import TestClient

from customer_hub_access_fixture import CustomerHubAccessFixture


WRITE_PERMISSIONS = (
    "return_visit.view",
    "return_visit.manage",
    "return_visit.delete",
)


class ReturnVisitTransactionFixture:
    def __init__(self):
        self.access = CustomerHubAccessFixture()
        self._seed()
        self.client = self.access.client_with_permissions(WRITE_PERMISSIONS)
        self.safe_client = TestClient(
            self.access.app, raise_server_exceptions=False
        )
        response = self.safe_client.post(
            "/api/auth/login",
            json={
                "username": self.access.USERNAME,
                "password": self.access.PASSWORD,
            },
        )
        if response.status_code != 200:
            raise AssertionError("synthetic Access V3 login failed")
        self.access.clients.append(self.safe_client)

    @property
    def db_path(self):
        return self.access.db_path

    def _seed(self):
        conn = sqlite3.connect(self.access.db_path)
        try:
            conn.executemany(
                "insert into patients(patient_identity, display_name, is_deleted, "
                "current_hash, source_json) values (?, ?, ?, 'hash', '{}')",
                (
                    ("patient-active", "Synthetic active patient", 0),
                    ("patient-deleted", "Synthetic deleted patient", 1),
                ),
            )
            rows = (
                ("rv-edit", "patient-active", "待回访", "", 1, 0, ""),
                ("rv-delete", "patient-active", "待回访", "", 1, 0, ""),
                ("rv-batch-a", "patient-active", "待回访", "", 1, 0, ""),
                ("rv-batch-b", "patient-active", "待回访", "", 1, 0, ""),
                ("rv-deleted-parent", "patient-deleted", "待回访", "", 1, 0, ""),
                ("rv-done", "patient-active", "已回访", "2026-07-19", 4, 0, ""),
            )
            conn.executemany(
                "insert into return_visits(return_visit_id, patient_identity, due_time, "
                "item_name, status, actual_date, revision, is_deleted, channel) "
                "values (?, ?, '2026-07-30', 'Synthetic follow-up', ?, ?, ?, ?, ?)",
                rows,
            )
            conn.commit()
        finally:
            conn.close()

    def close(self):
        self.access.close()

    def digest(self):
        conn = sqlite3.connect(self.db_path)
        try:
            visits = conn.execute(
                "select return_visit_id, status, note, actual_date, revision, "
                "is_deleted from return_visits order by return_visit_id"
            ).fetchall()
            versions = conn.execute(
                "select return_visit_id, version_hash, snapshot_json "
                "from return_visit_versions order by version_id"
            ).fetchall()
            audits = conn.execute(
                "select entity_id, action, old_json, new_json, operator, actor_user_id "
                "from audit_logs where entity_type = 'return_visit' order by audit_id"
            ).fetchall()
            return visits, versions, audits
        finally:
            conn.close()

    def row(self, return_visit_id):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            return conn.execute(
                "select * from return_visits where return_visit_id = ?",
                (return_visit_id,),
            ).fetchone()
        finally:
            conn.close()

    def versions(self, return_visit_id):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            return conn.execute(
                "select * from return_visit_versions where return_visit_id = ? "
                "order by version_id",
                (return_visit_id,),
            ).fetchall()
        finally:
            conn.close()

    def audits(self, return_visit_id):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            return conn.execute(
                "select * from audit_logs where entity_type = 'return_visit' "
                "and entity_id = ? order by audit_id",
                (return_visit_id,),
            ).fetchall()
        finally:
            conn.close()

    def request_with_revocation_before_begin(
        self, client, method, path, permission, **kwargs
    ):
        from local_app import return_visit_transactions as begin_module
        real_begin = begin_module.begin_immediate
        callback_hit = False

        def revoke_then_begin(conn):
            nonlocal callback_hit
            callback_hit = True
            self.access.revoke(permission)
            return real_begin(conn)

        with mock.patch.object(
            begin_module, "begin_immediate", side_effect=revoke_then_begin
        ):
            response = client.request(method, path, **kwargs)
        return response, callback_hit
