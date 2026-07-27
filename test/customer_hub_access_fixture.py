"""Synthetic Access V3 fixture shared by customer-hub integration tests."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from fastapi.testclient import TestClient

from local_app.access_authorization import load_access_principal
from local_app.api import create_app
from local_app.db import init_db
from local_app.passwords import hash_password


class CustomerHubAccessFixture:
    USER_ID = "user-customer-hub"
    USERNAME = "customer-hub-user"
    PASSWORD = "synthetic-password"
    ROLE_KEY = "customer_hub_role"
    STAFF_ID = "staff-customer-hub"
    DICT_KEYS = ("return_type", "visit_content", "visit_result", "comm_reason")

    def __init__(self):
        self.temp_dir = TemporaryDirectory()
        self.temp_root = Path(self.temp_dir.name)
        self.images_dir = self.temp_root / "images"
        self.images_dir.mkdir()
        self.db_path = self._build_target_database(self.temp_root)
        self.app = create_app(
            self.db_path,
            images_dir=self.images_dir,
            access_v3=True,
        )
        self.clients = []
        self.revocation_callback_hit = False

    def _build_target_database(self, root):
        db_path = root / "customer-hub.sqlite3"
        init_db(db_path)
        target_schema = (
            Path(__file__).parents[1]
            / "local_app"
            / "personnel_access_schema.sql"
        ).read_text(encoding="utf-8")
        conn = sqlite3.connect(db_path)
        try:
            conn.execute("pragma foreign_keys = off")
            conn.executescript(
                """
                drop table sessions;
                drop table users;
                drop table staff_members;
                drop table audit_logs;
                """
            )
            conn.executescript(target_schema)
            conn.execute(
                "insert into roles(role_key, name, is_system, sort) "
                "values (?, '合成客户通角色', 0, 10)",
                (self.ROLE_KEY,),
            )
            conn.execute(
                "insert into staff_members(staff_id, name, employment_status) "
                "values (?, '合成在职人员', 'employed')",
                (self.STAFF_ID,),
            )
            conn.execute(
                "insert into staff_role_assignments"
                "(staff_id, role_key, is_primary, created_at) values (?, ?, 1, ?)",
                (self.STAFF_ID, self.ROLE_KEY, "2026-07-20 09:00:00"),
            )
            conn.execute(
                "insert into users"
                "(id, username, display_name, password_hash, staff_id, is_active, "
                "is_system_admin, account_kind) values (?, ?, ?, ?, ?, 1, 0, 'staff')",
                (
                    self.USER_ID,
                    self.USERNAME,
                    "合成客户通用户",
                    hash_password(self.PASSWORD),
                    self.STAFF_ID,
                ),
            )
            initial = {
                "return_type": ["合成回访类型-原值"],
                "visit_content": ["合成回访内容-原值"],
                "visit_result": ["合成回访结果-原值"],
                "comm_reason": ["合成沟通原因-原值"],
            }
            conn.executemany(
                "insert or replace into app_settings(key, value, updated_at) "
                "values (?, ?, '2026-07-20 09:00:00')",
                [
                    (key, json.dumps(value, ensure_ascii=False))
                    for key, value in initial.items()
                ],
            )
            conn.commit()
        finally:
            conn.close()
        return db_path

    def principal(self):
        return load_access_principal(self.db_path, self.USER_ID)

    def client_with_permissions(self, permissions):
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                "delete from role_permissions where role_key = ?",
                (self.ROLE_KEY,),
            )
            conn.executemany(
                "insert into role_permissions(role_key, perm_key) values (?, ?)",
                [(self.ROLE_KEY, permission) for permission in permissions],
            )
            conn.commit()
        finally:
            conn.close()
        client = TestClient(self.app)
        response = client.post(
            "/api/auth/login",
            json={"username": self.USERNAME, "password": self.PASSWORD},
        )
        if response.status_code != 200:
            client.close()
            raise AssertionError(
                f"synthetic Access V3 login failed: {response.status_code}"
            )
        self.clients.append(client)
        return client

    def revoke(self, permission):
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                "delete from role_permissions where role_key = ? and perm_key = ?",
                (self.ROLE_KEY, permission),
            )
            conn.commit()
        finally:
            conn.close()

    def read_customer_hub_dicts(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                "select key, value from app_settings where key in (?, ?, ?, ?)",
                self.DICT_KEYS,
            ).fetchall()
        finally:
            conn.close()
        return {row["key"]: json.loads(row["value"]) for row in rows}

    def count_audits(self):
        conn = sqlite3.connect(self.db_path)
        try:
            return conn.execute(
                "select count(*) from audit_logs where entity_type = ?",
                ("customer_hub_dicts",),
            ).fetchone()[0]
        finally:
            conn.close()

    def request_with_revocation_before_begin(
        self,
        client,
        method,
        path,
        permission,
        *,
        begin_module=None,
        **request_kwargs,
    ):
        if begin_module is None:
            from local_app.routes import app_settings as begin_module

        real_begin = begin_module.begin_immediate
        revoked = False

        def revoke_then_begin(conn):
            nonlocal revoked
            if not revoked:
                revoked = True
                self.revocation_callback_hit = True
                self.revoke(permission)
            return real_begin(conn)

        self.revocation_callback_hit = False
        with mock.patch.object(
            begin_module, "begin_immediate", side_effect=revoke_then_begin
        ):
            return client.request(method, path, **request_kwargs)

    def close(self):
        for client in self.clients:
            client.close()
        self.clients.clear()
        self.temp_dir.cleanup()
