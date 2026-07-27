"""Startup must recover attachment operations before constructing the server."""
from __future__ import annotations

import unittest
from unittest import mock

from local_app import run_local
from local_app.attachment_file_ops import AttachmentConsistencyError


class CustomerHubStartupRecoveryTest(unittest.TestCase):
    def test_startup_orders_schema_migrations_recovery_then_server(self):
        events = []
        with mock.patch.object(
            run_local, "init_db", side_effect=lambda *_: events.append("schema")
        ), mock.patch(
            "local_app.v3_bootstrap.ensure_v3_personnel_schema",
            side_effect=lambda *_: events.append("v3boot") or False,
        ), mock.patch(
            "local_app.migrations.apply_migrations",
            side_effect=lambda *_: events.append("migrations") or [],
        ), mock.patch(
            "local_app.attachment_file_ops.recover_attachment_file_ops",
            side_effect=lambda *_: events.append("recovery") or {
                "recovered": 0,
                "blocked": 0,
                "states": {},
            },
        ), mock.patch(
            "local_app.auth.ensure_seed_admin",
            side_effect=lambda *_: events.append("seed") or None,
        ), mock.patch.object(
            run_local, "create_app", side_effect=lambda *_args, **_kwargs: events.append("app") or object()
        ), mock.patch.object(
            run_local.uvicorn, "run", side_effect=lambda *_args, **_kwargs: events.append("server")
        ), mock.patch.dict(
            run_local.os.environ,
            {"DENTAL_LOCAL_ONLY": "1", "DENTAL_IMAGES": "/tmp/synthetic-images"},
            clear=False,
        ):
            run_local.main()
        self.assertLess(events.index("schema"), events.index("v3boot"))
        self.assertLess(events.index("v3boot"), events.index("migrations"))
        self.assertLess(events.index("migrations"), events.index("recovery"))
        self.assertLess(events.index("recovery"), events.index("app"))
        self.assertLess(events.index("app"), events.index("server"))

    def test_inconsistent_recovery_prevents_app_and_server_start(self):
        with mock.patch.object(run_local, "init_db"), mock.patch(
            "local_app.v3_bootstrap.ensure_v3_personnel_schema", return_value=False
        ), mock.patch(
            "local_app.migrations.apply_migrations", return_value=[]
        ), mock.patch(
            "local_app.attachment_file_ops.recover_attachment_file_ops",
            side_effect=AttachmentConsistencyError("attachment_consistency_error"),
        ), mock.patch.object(run_local, "create_app") as create_app, mock.patch.object(
            run_local.uvicorn, "run"
        ) as run:
            with self.assertRaisesRegex(
                AttachmentConsistencyError, "attachment_consistency_error"
            ):
                run_local.main()
        create_app.assert_not_called()
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
