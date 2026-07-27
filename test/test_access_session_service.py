import ast
import hashlib
import inspect
import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from local_app import access_session_service as service
from local_app.passwords import hash_password


UTC = timezone.utc
BASE_TIME = datetime(2026, 7, 16, 1, 2, 3, 987654, tzinfo=UTC)
RAW_TOKEN = "a" * 64
SESSION_ID = "b" * 32
REQUEST_ID = "c" * 16
CURRENT_IP = "10.0.0.9"
CURRENT_UA = "current-request-agent"
_DUMMY_PASSWORD = "__access_v3_dummy_password__"


def compact_json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class SyntheticSessionDatabase(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "synthetic.sqlite"
        schema_path = Path(__file__).parents[1] / "local_app" / "personnel_access_schema.sql"
        conn = sqlite3.connect(self.db_path)
        conn.execute("pragma foreign_keys = on")
        conn.execute("create table roles(role_key text primary key)")
        conn.executescript(schema_path.read_text(encoding="utf-8"))
        conn.executemany(
            "insert into staff_members(staff_id, name, employment_status) values (?, ?, ?)",
            [
                ("staff-employed", "Employed", "employed"),
                ("staff-left", "Left", "left"),
                ("staff-inactive", "Inactive", "employed"),
            ],
        )
        conn.executemany(
            "insert into users(id, username, display_name, password_hash, staff_id, "
            "is_active, is_system_admin, account_kind) values (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                ("user-employed", "employed", "Employed", hash_password("pw"),
                 "staff-employed", 1, 0, "staff"),
                ("user-left", "left", "Left", hash_password("pw"),
                 "staff-left", 1, 0, "staff"),
                ("user-inactive", "inactive", "Inactive", hash_password("pw"),
                 "staff-inactive", 0, 0, "staff"),
                ("user-admin", "admin", "Admin", hash_password("pw"),
                 None, 1, 1, "independent_admin"),
            ],
        )
        conn.commit()
        conn.close()

    def tearDown(self):
        self.temp_dir.cleanup()

    def connect(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def scalar(self, sql, params=()):
        conn = self.connect()
        try:
            return conn.execute(sql, params).fetchone()[0]
        finally:
            conn.close()

    def audits(self):
        conn = self.connect()
        try:
            return conn.execute("select * from audit_logs order by audit_id").fetchall()
        finally:
            conn.close()

    def error_code(self, call):
        with self.assertRaises(service.AccessSessionError) as caught:
            call()
        return caught.exception.code

    def authenticate(
        self,
        username="employed",
        password="pw",
        device_name="front desk",
        ip_address="127.0.0.1",
        user_agent="stored-agent",
        request_id=REQUEST_ID,
        now=BASE_TIME,
    ):
        return service.authenticate_and_create_session(
            self.db_path,
            username=username,
            password=password,
            device_name=device_name,
            user_agent=user_agent,
            ip_address=ip_address,
            request_id=request_id,
            now=now,
        )


class PublicContractTest(unittest.TestCase):
    def test_public_signatures_match_the_http_handoff(self):
        expected = {
            "authenticate_and_create_session": (
                "(db_path, *, username, password, device_name, user_agent, "
                "ip_address, request_id, now=None)"
            ),
            "resolve_session": "(db_path, raw_token, *, now=None, renew_activity=True)",
            "logout_session": (
                "(db_path, *, raw_token, request_id, ip_address, user_agent, now=None)"
            ),
            "reauthenticate_session": (
                "(db_path, *, session_id, password, request_id, ip_address, "
                "user_agent, now=None)"
            ),
            "is_reauth_fresh": "(reauthenticated_at, *, created_at, now=None)",
            "normalize_server_now": "(value)",
            "hash_token": "(raw_token)",
        }
        for name, signature in expected.items():
            with self.subTest(name=name):
                self.assertTrue(hasattr(service, name))
                self.assertEqual(str(inspect.signature(getattr(service, name))), signature)

    def test_hash_token_requires_exact_lowercase_64_hex(self):
        self.assertEqual(
            service.hash_token(RAW_TOKEN),
            hashlib.sha256(RAW_TOKEN.encode("utf-8")).hexdigest(),
        )
        for bad in (None, 1, "", "a" * 63, "a" * 65, "A" * 64, "g" * 64):
            with self.subTest(bad=bad):
                with self.assertRaises(service.AccessSessionError) as caught:
                    service.hash_token(bad)
                self.assertEqual(caught.exception.code, "token_invalid")

    def test_normalize_server_now_is_strict_utc_at_second_precision(self):
        normalized = service.normalize_server_now(BASE_TIME)
        self.assertIs(type(normalized), datetime)
        self.assertEqual(normalized, datetime(2026, 7, 16, 1, 2, 3, tzinfo=UTC))
        for bad in (None, "now", datetime(2026, 7, 16, 1, 2, 3)):
            with self.subTest(bad=bad):
                with self.assertRaises(service.AccessSessionError) as caught:
                    service.normalize_server_now(bad)
                self.assertEqual(caught.exception.code, "now_invalid")

    def test_is_reauth_fresh_uses_created_floor_and_five_minute_seconds(self):
        created = "2026-07-16T01:00:00Z"
        reauthed = "2026-07-16T01:02:03Z"
        self.assertTrue(
            service.is_reauth_fresh(
                reauthed, created_at=created, now=datetime(2026, 7, 16, 1, 7, 3, tzinfo=UTC)
            )
        )
        self.assertFalse(
            service.is_reauth_fresh(
                reauthed, created_at=created, now=datetime(2026, 7, 16, 1, 7, 4, tzinfo=UTC)
            )
        )
        for bad in (None, "broken", "2026-07-16T00:59:59Z", "2026-07-16T01:08:00Z"):
            with self.subTest(bad=bad):
                self.assertFalse(
                    service.is_reauth_fresh(
                        bad,
                        created_at=created,
                        now=datetime(2026, 7, 16, 1, 7, 3, tzinfo=UTC),
                    )
                )


class SlimSourceContractTest(unittest.TestCase):
    def test_old_complexity_is_absent_and_one_write_owner_remains(self):
        source = inspect.getsource(service)
        forbidden = (
            "MappingProxyType", "set_authorizer", "statement_not_allowed",
            "SYSTEM_ADMIN_REAUTH_STATEMENT_IDS", "CLOCK_ROLLBACK_TOLERANCE",
            "ACTIVITY_BATCH_GRACE", "request_superseded", "session_state_busy",
            "_acquire_session_gate", "record_session_activity", "absolute_expires_at",
            "list_sessions", "revoke_session",
        )
        for term in forbidden:
            self.assertNotIn(term, source)
        self.assertTrue(hasattr(service, "_run_immediate"))
        self.assertEqual(source.lower().count("begin immediate"), 1)
        self.assertNotIn("local_app.auth", source)

    def test_execute_calls_do_not_build_sql_dynamically(self):
        tree = ast.parse(inspect.getsource(service))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and node.args
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in {"execute", "executemany"}
            ):
                self.assertNotIsInstance(node.args[0], (ast.JoinedStr, ast.BinOp))


class AuthenticationTest(SyntheticSessionDatabase):
    def test_dummy_hash_is_fixed_without_storing_dummy_plaintext(self):
        salt = "f4bfaa87cd6f471fa65ec1a404fe63e4"
        self.assertEqual(hash_password(_DUMMY_PASSWORD, salt), service._DUMMY_PASSWORD_HASH)
        self.assertNotIn(_DUMMY_PASSWORD, inspect.getsource(service))

    def test_success_returns_nested_context_and_exact_safe_audit(self):
        def token_hex(n):
            if n == 16:
                return SESSION_ID
            if n == 32:
                return RAW_TOKEN
            raise AssertionError(n)

        with patch.object(service.secrets, "token_hex", side_effect=token_hex):
            raw_token, context = self.authenticate()

        self.assertEqual(raw_token, RAW_TOKEN)
        self.assertEqual(set(context), {"user", "session"})
        self.assertEqual(
            set(context["user"]),
            {"id", "username", "display_name", "staff_id", "account_kind", "is_system_admin"},
        )
        self.assertEqual(
            set(context["session"]),
            {"session_id", "user_id", "device_name", "user_agent", "ip_address",
             "created_at", "last_seen_at", "idle_expires_at", "reauthenticated_at"},
        )
        self.assertEqual(context["session"]["last_seen_at"], "2026-07-16T01:02:03Z")
        self.assertEqual(context["session"]["idle_expires_at"], "2026-07-16T07:02:03Z")

        conn = self.connect()
        try:
            session = conn.execute("select * from sessions").fetchone()
        finally:
            conn.close()
        self.assertEqual(session["token_hash"], hashlib.sha256(RAW_TOKEN.encode()).hexdigest())
        self.assertNotIn(RAW_TOKEN.encode(), self.db_path.read_bytes())

        audit = self.audits()[0]
        self.assertEqual(audit["entity_type"], "session")
        self.assertEqual(audit["entity_id"], SESSION_ID)
        self.assertEqual(audit["action"], "auth.login")
        self.assertEqual(audit["operator"], "employed")
        self.assertEqual(audit["actor_user_id"], "user-employed")
        self.assertEqual(audit["result"], "success")
        self.assertIsNone(audit["reason"])
        self.assertEqual(audit["risk_level"], "low")
        self.assertIsNone(audit["old_json"])
        self.assertEqual(
            audit["new_json"],
            compact_json({"session_id": SESSION_ID, "user_id": "user-employed",
                          "device_name": "front desk"}),
        )
        self.assertEqual(
            (audit["request_id"], audit["ip_address"], audit["user_agent"]),
            (REQUEST_ID, "127.0.0.1", "stored-agent"),
        )

    def test_username_and_metadata_strip_then_truncate(self):
        long_username = "x" * 128
        conn = self.connect()
        conn.execute(
            "insert into users(id, username, display_name, password_hash, staff_id, "
            "is_active, is_system_admin, account_kind) values (?, ?, ?, ?, ?, ?, ?, ?)",
            ("user-long", long_username, "Long", hash_password("pw"), None,
             1, 1, "independent_admin"),
        )
        conn.commit()
        conn.close()

        _token, context = self.authenticate(
            username="  " + ("x" * 140) + "  ",
            device_name="  " + ("d" * 70) + "  ",
            user_agent="  " + ("u" * 300) + "  ",
            ip_address="  " + ("i" * 70) + "  ",
        )
        self.assertEqual(context["user"]["username"], long_username)
        self.assertEqual(context["session"]["device_name"], "d" * 64)
        self.assertEqual(context["session"]["user_agent"], "u" * 256)
        self.assertEqual(context["session"]["ip_address"], "i" * 64)
        audit = self.audits()[-1]
        self.assertEqual(audit["user_agent"], "u" * 256)
        self.assertEqual(audit["ip_address"], "i" * 64)

    def test_all_credential_denials_commit_identical_nonsecret_audits(self):
        attempts = (
            ("missing-user", "unknown-secret"),
            ("employed", "wrong-secret"),
            ("inactive", "pw"),
            ("left", "pw"),
        )
        for username, password in attempts:
            with self.subTest(username=username):
                self.assertEqual(
                    self.error_code(lambda username=username, password=password:
                                    self.authenticate(username=username, password=password)),
                    "invalid_credentials",
                )
        self.assertEqual(self.scalar("select count(*) from sessions"), 0)
        audits = self.audits()
        self.assertEqual(len(audits), 4)
        frozen = []
        for row in audits:
            frozen.append((
                row["entity_type"], row["entity_id"], row["action"], row["old_json"],
                row["new_json"], row["operator"], row["actor_user_id"], row["result"],
                row["reason"], row["risk_level"], row["request_id"], row["ip_address"],
                row["user_agent"],
            ))
        self.assertEqual(len(set(frozen)), 1)
        self.assertEqual(
            frozen[0],
            ("session", "login", "auth.login", None, None, "anonymous", None,
             "denied", None, "medium", REQUEST_ID, "127.0.0.1", "stored-agent"),
        )
        rendered = repr([dict(row) for row in audits])
        for secret in ("missing-user", "unknown-secret", "employed", "wrong-secret", "pw"):
            self.assertNotIn(secret, rendered)

    def test_login_audit_failure_rolls_back_session(self):
        conn = self.connect()
        conn.execute(
            "create trigger fail_login_audit before insert on audit_logs "
            "when new.action = 'auth.login' "
            "begin select raise(abort, 'synthetic audit failure'); end"
        )
        conn.commit()
        conn.close()
        self.assertEqual(self.error_code(self.authenticate), "audit_failed")
        self.assertEqual(self.scalar("select count(*) from sessions"), 0)


class ResolveAndRenewTest(SyntheticSessionDatabase):
    def test_resolve_is_nested_and_refreshes_only_at_300_seconds(self):
        token, _ = self.authenticate()
        before = service.resolve_session(
            self.db_path, token, now=BASE_TIME + timedelta(seconds=299)
        )
        self.assertEqual(before["session"]["last_seen_at"], "2026-07-16T01:02:03Z")
        renewed = service.resolve_session(
            self.db_path, token, now=BASE_TIME + timedelta(seconds=300)
        )
        self.assertEqual(renewed["session"]["last_seen_at"], "2026-07-16T01:07:03Z")
        self.assertEqual(renewed["session"]["idle_expires_at"], "2026-07-16T07:07:03Z")
        self.assertEqual(len(self.audits()), 1)

    def test_polling_resolve_does_not_renew(self):
        token, _ = self.authenticate()
        context = service.resolve_session(
            self.db_path, token, now=BASE_TIME + timedelta(hours=1), renew_activity=False
        )
        self.assertEqual(context["session"]["last_seen_at"], "2026-07-16T01:02:03Z")

    def test_invalid_unknown_and_expired_tokens_return_none(self):
        self.assertIsNone(service.resolve_session(self.db_path, "bad", now=BASE_TIME))
        token, _ = self.authenticate()
        conn = self.connect()
        conn.execute(
            "create trigger fail_future_audit before insert on audit_logs "
            "begin select raise(abort, 'audit must not run'); end"
        )
        conn.commit()
        conn.close()
        self.assertIsNone(
            service.resolve_session(self.db_path, token, now=BASE_TIME + timedelta(hours=6))
        )
        self.assertEqual(self.scalar("select count(*) from sessions"), 0)
        self.assertEqual(len(self.audits()), 1)

    def test_account_invalidation_deletes_without_audit(self):
        token, _ = self.authenticate()
        conn = self.connect()
        conn.execute("update users set is_active = 0 where id = 'user-employed'")
        conn.execute(
            "create trigger fail_future_audit before insert on audit_logs "
            "begin select raise(abort, 'audit must not run'); end"
        )
        conn.commit()
        conn.close()
        self.assertIsNone(service.resolve_session(self.db_path, token, now=BASE_TIME))
        self.assertEqual(self.scalar("select count(*) from sessions"), 0)
        self.assertEqual(len(self.audits()), 1)

    def test_employee_marked_left_is_immediately_logged_out_without_audit(self):
        token, _ = self.authenticate()
        conn = self.connect()
        conn.execute(
            "update staff_members set employment_status = 'left' "
            "where staff_id = 'staff-employed'"
        )
        conn.execute(
            "create trigger fail_future_audit before insert on audit_logs "
            "begin select raise(abort, 'audit must not run'); end"
        )
        conn.commit()
        conn.close()
        self.assertIsNone(
            service.resolve_session(
                self.db_path, token, now=BASE_TIME + timedelta(seconds=1)
            )
        )
        self.assertEqual(self.scalar("select count(*) from sessions"), 0)
        self.assertEqual(len(self.audits()), 1)

    def test_malformed_reauthentication_time_is_none_without_kicking_session(self):
        token, _ = self.authenticate()
        conn = self.connect()
        conn.execute("update sessions set reauthenticated_at = 'broken'")
        conn.commit()
        conn.close()
        context = service.resolve_session(
            self.db_path, token, now=BASE_TIME + timedelta(seconds=1)
        )
        self.assertIsNone(context["session"]["reauthenticated_at"])
        self.assertEqual(self.scalar("select count(*) from sessions"), 1)
        self.assertFalse(
            service.is_reauth_fresh(
                context["session"]["reauthenticated_at"],
                created_at=context["session"]["created_at"],
                now=BASE_TIME + timedelta(seconds=1),
            )
        )


class LogoutTest(SyntheticSessionDatabase):
    def test_logout_uses_current_metadata_and_safe_old_json(self):
        token, context = self.authenticate()
        self.assertTrue(
            service.logout_session(
                self.db_path,
                raw_token=token,
                request_id="d" * 16,
                ip_address=CURRENT_IP,
                user_agent=CURRENT_UA,
                now=BASE_TIME + timedelta(minutes=1),
            )
        )
        self.assertEqual(self.scalar("select count(*) from sessions"), 0)
        audit = self.audits()[-1]
        self.assertEqual(
            (audit["action"], audit["result"], audit["reason"], audit["risk_level"]),
            ("auth.logout", "success", "self_logout", "low"),
        )
        self.assertEqual(
            (audit["request_id"], audit["ip_address"], audit["user_agent"]),
            ("d" * 16, CURRENT_IP, CURRENT_UA),
        )
        safe = {key: context["session"][key] for key in (
            "session_id", "device_name", "user_agent", "ip_address", "created_at",
            "last_seen_at", "idle_expires_at",
        )}
        self.assertEqual(audit["old_json"], compact_json(safe))
        self.assertIsNone(audit["new_json"])
        for forbidden in ("password", "token", "hash"):
            self.assertNotIn(forbidden, audit["old_json"].lower())

    def test_unknown_or_invalid_logout_is_false_without_noise_audit(self):
        before = len(self.audits())
        for token in ("bad", "f" * 64):
            with self.subTest(token=token):
                self.assertFalse(
                    service.logout_session(
                        self.db_path, raw_token=token, request_id="d" * 16,
                        ip_address=CURRENT_IP, user_agent=CURRENT_UA, now=BASE_TIME,
                    )
                )
        self.assertEqual(len(self.audits()), before)

    def test_logout_audit_failure_rolls_back_delete(self):
        token, _ = self.authenticate()
        conn = self.connect()
        conn.execute(
            "create trigger fail_logout_audit before insert on audit_logs "
            "when new.action = 'auth.logout' "
            "begin select raise(abort, 'synthetic audit failure'); end"
        )
        conn.commit()
        conn.close()
        self.assertEqual(
            self.error_code(lambda: service.logout_session(
                self.db_path, raw_token=token, request_id="d" * 16,
                ip_address=CURRENT_IP, user_agent=CURRENT_UA, now=BASE_TIME,
            )),
            "audit_failed",
        )
        self.assertEqual(self.scalar("select count(*) from sessions"), 1)


class ReauthenticationTest(SyntheticSessionDatabase):
    def test_success_only_changes_reauth_and_writes_current_metadata(self):
        _token, context = self.authenticate()
        session_id = context["session"]["session_id"]
        self.assertTrue(
            service.reauthenticate_session(
                self.db_path, session_id=session_id, password="pw", request_id="e" * 16,
                ip_address=CURRENT_IP, user_agent=CURRENT_UA,
                now=BASE_TIME + timedelta(minutes=10),
            )
        )
        conn = self.connect()
        try:
            row = conn.execute("select * from sessions where session_id = ?", (session_id,)).fetchone()
        finally:
            conn.close()
        self.assertEqual(row["reauthenticated_at"], "2026-07-16T01:12:03Z")
        self.assertEqual(row["last_seen_at"], "2026-07-16T01:02:03Z")
        audit = self.audits()[-1]
        self.assertEqual(
            (audit["action"], audit["result"], audit["reason"], audit["risk_level"]),
            ("auth.reauth", "success", None, "medium"),
        )
        self.assertEqual(
            (audit["request_id"], audit["ip_address"], audit["user_agent"]),
            ("e" * 16, CURRENT_IP, CURRENT_UA),
        )
        self.assertIsNone(audit["old_json"])
        self.assertIsNone(audit["new_json"])

    def test_wrong_password_returns_false_and_commits_denied_audit(self):
        _token, context = self.authenticate()
        session_id = context["session"]["session_id"]
        before = self.scalar("select reauthenticated_at from sessions")
        self.assertFalse(
            service.reauthenticate_session(
                self.db_path, session_id=session_id, password="wrong-secret",
                request_id="e" * 16, ip_address=CURRENT_IP, user_agent=CURRENT_UA,
                now=BASE_TIME + timedelta(minutes=10),
            )
        )
        self.assertEqual(self.scalar("select reauthenticated_at from sessions"), before)
        audit = self.audits()[-1]
        self.assertEqual((audit["action"], audit["result"]), ("auth.reauth", "denied"))
        self.assertNotIn("wrong-secret", repr(dict(audit)))

    def test_reauth_audit_failure_rolls_back_timestamp(self):
        _token, context = self.authenticate()
        session_id = context["session"]["session_id"]
        before = self.scalar("select reauthenticated_at from sessions")
        conn = self.connect()
        conn.execute(
            "create trigger fail_reauth_audit before insert on audit_logs "
            "when new.action = 'auth.reauth' "
            "begin select raise(abort, 'synthetic audit failure'); end"
        )
        conn.commit()
        conn.close()
        self.assertEqual(
            self.error_code(lambda: service.reauthenticate_session(
                self.db_path, session_id=session_id, password="pw", request_id="e" * 16,
                ip_address=CURRENT_IP, user_agent=CURRENT_UA,
                now=BASE_TIME + timedelta(minutes=10),
            )),
            "audit_failed",
        )
        self.assertEqual(self.scalar("select reauthenticated_at from sessions"), before)


if __name__ == "__main__":
    unittest.main()
