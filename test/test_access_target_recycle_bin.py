"""Target access-v3 restore ownership and system-admin behavior."""

import sqlite3
import unittest
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory

from fastapi import HTTPException

from local_app import access_authorization as authorization
from local_app.access_policy import ROUTE_POLICY_BY_KEY
from local_app.routes.recycle_bin import create_recycle_bin_router


def principal(*, system_admin=False, username="restorer"):
    return authorization.AccessPrincipal(
        user_id="user-" + username,
        username=username,
        display_name=username,
        staff_id="staff-" + username,
        is_system_admin=system_admin,
        primary_role="front_desk",
        role_keys=("front_desk",),
        permissions=frozenset({"recycle.view", "recycle.restore"}),
        role_permissions=frozenset({"recycle.view", "recycle.restore"}),
    )


@contextmanager
def target_request(actor):
    active_token = authorization._access_authorizer_active.set(True)
    principal_token = authorization._current_access_principal.set(actor)
    policy_token = authorization._current_authorized_policy.set(
        ROUTE_POLICY_BY_KEY[("POST", "/api/recycle-bin/restore")]
    )
    try:
        yield
    finally:
        authorization._current_authorized_policy.reset(policy_token)
        authorization._current_access_principal.reset(principal_token)
        authorization._access_authorizer_active.reset(active_token)


def build_database(path, *, deleted_by="deleter"):
    conn = sqlite3.connect(path)
    try:
        conn.executescript(
            """
            create table return_visits(
                return_visit_id text primary key,
                is_deleted integer not null
            );
            create table audit_logs(
                audit_id integer primary key autoincrement,
                entity_type text not null,
                entity_id text not null,
                action text not null,
                old_json text,
                new_json text,
                operator text,
                created_at text not null,
                actor_user_id text
            );
            insert into return_visits(return_visit_id, is_deleted)
            values ('rv-1', 1);
            """
        )
        conn.execute(
            "insert into audit_logs(entity_type, entity_id, action, operator, created_at) "
            "values ('return_visit', 'rv-1', 'soft_delete', ?, '2026-07-16 09:00:00')",
            (deleted_by,),
        )
        conn.commit()
    finally:
        conn.close()


def restore_endpoint(db_path):
    router = create_recycle_bin_router(db_path, access_v3=True)
    return next(
        route.endpoint
        for route in router.routes
        if route.path == "/api/recycle-bin/restore"
    )


class TargetRecycleBinTest(unittest.TestCase):
    def _database(self, directory, *, deleted_by="deleter"):
        path = Path(directory) / "target.sqlite3"
        build_database(path, deleted_by=deleted_by)
        return path

    def test_system_admin_can_restore_another_users_record_and_is_audited(self):
        with TemporaryDirectory() as directory:
            db_path = self._database(directory)
            with target_request(principal(system_admin=True, username="boss")):
                result = restore_endpoint(db_path)(
                    {"entity_type": "return_visit", "entity_id": "rv-1"}
                )
            self.assertEqual(result, {"ok": True})

            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            try:
                restored = conn.execute(
                    "select is_deleted from return_visits where return_visit_id='rv-1'"
                ).fetchone()[0]
                audit = conn.execute(
                    "select operator, actor_user_id from audit_logs "
                    "where action='restore_return_visit'"
                ).fetchone()
            finally:
                conn.close()
            self.assertEqual(restored, 0)
            self.assertEqual(audit["operator"], "boss")
            self.assertEqual(audit["actor_user_id"], "user-boss")

    def test_ordinary_user_can_restore_only_own_record(self):
        with TemporaryDirectory() as directory:
            db_path = self._database(directory)
            with target_request(principal(username="other")), self.assertRaises(
                HTTPException
            ) as caught:
                restore_endpoint(db_path)(
                    {"entity_type": "return_visit", "entity_id": "rv-1"}
                )
            self.assertEqual(caught.exception.status_code, 403)

            conn = sqlite3.connect(db_path)
            try:
                self.assertEqual(
                    conn.execute(
                        "select is_deleted from return_visits where return_visit_id='rv-1'"
                    ).fetchone()[0],
                    1,
                )
                self.assertEqual(
                    conn.execute(
                        "select count(*) from audit_logs "
                        "where action='restore_return_visit'"
                    ).fetchone()[0],
                    0,
                )
            finally:
                conn.close()

        with TemporaryDirectory() as directory:
            db_path = self._database(directory, deleted_by="owner")
            with target_request(principal(username="owner")):
                self.assertEqual(
                    restore_endpoint(db_path)(
                        {"entity_type": "return_visit", "entity_id": "rv-1"}
                    ),
                    {"ok": True},
                )


if __name__ == "__main__":
    unittest.main()
