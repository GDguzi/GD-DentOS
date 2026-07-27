"""独立系统管理员守卫 require_system_admin() 的正反例（Task 2A）。

只用合成 dict 驱动 contextvar，不接触任何数据库/真实数据。

本文件同时收纳 Task 2B.1：access_service.grant_system_admin()/revoke_system_admin()
的单事务正反例，只用 TemporaryDirectory 合成 SQLite 文件库，不接触真实库/患者/员工数据。
"""
import sqlite3
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone, tzinfo
from decimal import Decimal
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

from fastapi import HTTPException

from local_app import access_service, auth
from local_app import personnel_access_migration as pam
from local_app.timeutil import BJ


class _FlipTzinfo(tzinfo):
    """第一次报 UTC+0、第二次就抛异常的敌意 tzinfo：用来证明归一化只问一次 offset。"""

    def __init__(self):
        self.calls = 0

    def utcoffset(self, dt):
        self.calls += 1
        if self.calls == 1:
            return timedelta(0)
        raise RuntimeError("second-utcoffset-secret")

    def tzname(self, dt):
        return None

    def dst(self, dt):
        return None


class _EqualsOne:
    def __eq__(self, other):
        return other == 1

    def __hash__(self):
        return hash(1)


class RequireSystemAdminTests(unittest.TestCase):
    def _set_user(self, user):
        token = auth._current_user.set(user)
        self.addCleanup(auth._current_user.reset, token)

    def test_unauthenticated_raises_401(self):
        self._set_user(None)
        with self.assertRaises(HTTPException) as ctx:
            auth.require_system_admin()
        self.assertEqual(ctx.exception.status_code, 401)

    def test_is_system_admin_true_int_1_passes(self):
        self._set_user({"id": "u1", "is_system_admin": 1})
        result = auth.require_system_admin()
        self.assertEqual(result["id"], "u1")

    def test_is_system_admin_bool_true_passes(self):
        self._set_user({"id": "u1", "is_system_admin": True})
        result = auth.require_system_admin()
        self.assertEqual(result["id"], "u1")

    def test_missing_flag_raises_403(self):
        self._set_user({"id": "u1"})
        with self.assertRaises(HTTPException) as ctx:
            auth.require_system_admin()
        self.assertEqual(ctx.exception.status_code, 403)

    def test_is_system_admin_0_raises_403(self):
        self._set_user({"id": "u1", "is_system_admin": 0})
        with self.assertRaises(HTTPException) as ctx:
            auth.require_system_admin()
        self.assertEqual(ctx.exception.status_code, 403)

    def test_float_1_0_does_not_pass(self):
        self._set_user({"id": "u1", "is_system_admin": 1.0})
        with self.assertRaises(HTTPException) as ctx:
            auth.require_system_admin()
        self.assertEqual(ctx.exception.status_code, 403)

    def test_decimal_1_does_not_pass(self):
        self._set_user({"id": "u1", "is_system_admin": Decimal(1)})
        with self.assertRaises(HTTPException) as ctx:
            auth.require_system_admin()
        self.assertEqual(ctx.exception.status_code, 403)

    def test_custom_equals_one_object_does_not_pass(self):
        self._set_user({"id": "u1", "is_system_admin": _EqualsOne()})
        with self.assertRaises(HTTPException) as ctx:
            auth.require_system_admin()
        self.assertEqual(ctx.exception.status_code, 403)

    def test_string_1_does_not_pass(self):
        self._set_user({"id": "u1", "is_system_admin": "1"})
        with self.assertRaises(HTTPException) as ctx:
            auth.require_system_admin()
        self.assertEqual(ctx.exception.status_code, 403)

    def test_role_admin_does_not_substitute(self):
        self._set_user({"id": "u1", "role": "admin", "is_system_admin": 0})
        with self.assertRaises(HTTPException) as ctx:
            auth.require_system_admin()
        self.assertEqual(ctx.exception.status_code, 403)

    def test_wildcard_permissions_does_not_substitute(self):
        self._set_user({"id": "u1", "permissions": ["*"], "is_system_admin": 0})
        with self.assertRaises(HTTPException) as ctx:
            auth.require_system_admin()
        self.assertEqual(ctx.exception.status_code, 403)

    def test_display_name_admin_does_not_substitute(self):
        self._set_user({"id": "u1", "display_name": "管理员", "is_system_admin": 0})
        with self.assertRaises(HTTPException) as ctx:
            auth.require_system_admin()
        self.assertEqual(ctx.exception.status_code, 403)

    def test_independent_admin_account_kind_passes(self):
        self._set_user({
            "id": "u2", "account_kind": "independent_admin", "is_system_admin": 1,
        })
        result = auth.require_system_admin()
        self.assertEqual(result["id"], "u2")

    def test_break_glass_account_kind_passes(self):
        self._set_user({
            "id": "u3", "account_kind": "break_glass", "is_system_admin": 1,
        })
        result = auth.require_system_admin()
        self.assertEqual(result["id"], "u3")


NOW = datetime(2026, 7, 15, 10, 0, 0, tzinfo=BJ)
VALID_REAUTH = NOW - timedelta(seconds=10)


def _build_db(db_path):
    conn = sqlite3.connect(str(db_path))
    conn.execute("pragma foreign_keys = on")
    conn.executescript(pam.load_personnel_access_schema_sql())
    conn.commit()
    conn.close()


def _insert_staff(conn, staff_id):
    conn.execute(
        "insert into staff_members(staff_id, name, employment_status) values (?, ?, 'employed')",
        (staff_id, staff_id),
    )


def _insert_user(conn, user_id, *, staff_id=None, is_active=1, is_system_admin=0,
                  account_kind="staff"):
    conn.execute(
        "insert into users(id, username, password_hash, staff_id, is_active, "
        "is_system_admin, account_kind) values (?, ?, 'x$y', ?, ?, ?, ?)",
        (user_id, user_id, staff_id, is_active, is_system_admin, account_kind),
    )


def _audit_rows(conn):
    return conn.execute(
        "select * from audit_logs where entity_type = 'user' order by audit_id"
    ).fetchall()


def _admin_flag(conn, user_id):
    return conn.execute(
        "select is_system_admin from users where id = ?", (user_id,)
    ).fetchone()["is_system_admin"]


class SystemAdminChangeTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db_path = Path(self._tmp.name) / "synthetic.db"
        _build_db(self.db_path)

    def _seed(self, rows, db_path=None):
        conn = sqlite3.connect(str(db_path or self.db_path))
        conn.row_factory = sqlite3.Row
        conn.execute("pragma foreign_keys = on")
        for kind, args, kwargs in rows:
            if kind == "staff":
                _insert_staff(conn, *args, **kwargs)
            else:
                _insert_user(conn, *args, **kwargs)
        conn.commit()
        conn.close()

    def _conn(self, db_path=None):
        conn = sqlite3.connect(str(db_path or self.db_path))
        conn.row_factory = sqlite3.Row
        return conn

    # --- actor 资格 ---

    def test_plain_staff_actor_without_flag_rejected(self):
        self._seed([
            ("staff", ("s1",), {}),
            ("user", ("u1",), {"staff_id": "s1", "is_system_admin": 0}),
            ("staff", ("s2",), {}),
            ("user", ("u2",), {"staff_id": "s2", "is_system_admin": 0}),
        ])
        with self.assertRaises(access_service.AccessServiceError) as ctx:
            access_service.grant_system_admin(
                self.db_path, actor_user_id="u1", target_user_id="u2",
                reason="test", reauthenticated_at=VALID_REAUTH, now=NOW,
            )
        self.assertEqual(ctx.exception.code, "actor_not_authorized")
        conn = self._conn()
        self.assertEqual(_admin_flag(conn, "u2"), 0)
        self.assertEqual(len(_audit_rows(conn)), 0)
        conn.close()

    def test_staff_independent_break_glass_admin_actor_can_grant(self):
        for actor_kind, actor_id in (
            ("staff", "a_staff"), ("independent_admin", "a_indep"), ("break_glass", "a_bg"),
        ):
            with self.subTest(actor_kind=actor_kind):
                with tempfile.TemporaryDirectory() as tmp_dir:
                    db_path = Path(tmp_dir) / "synthetic.db"
                    _build_db(db_path)
                    rows = [("staff", ("s1",), {}), ("user", ("target",), {"staff_id": "s1"})]
                    if actor_kind == "staff":
                        rows += [
                            ("staff", ("sa",), {}),
                            ("user", (actor_id,), {"staff_id": "sa", "is_system_admin": 1}),
                        ]
                    else:
                        rows.append(
                            ("user", (actor_id,), {"account_kind": actor_kind, "is_system_admin": 1}),
                        )
                    self._seed(rows, db_path=db_path)
                    access_service.grant_system_admin(
                        db_path, actor_user_id=actor_id, target_user_id="target",
                        reason="test", reauthenticated_at=VALID_REAUTH, now=NOW,
                    )
                    conn = self._conn(db_path=db_path)
                    self.assertEqual(_admin_flag(conn, "target"), 1)
                    conn.close()

    # --- reason / reauth 校验，零变化 ---

    def _admin_actor(self):
        self._seed([
            ("staff", ("sa",), {}),
            ("user", ("actor",), {"staff_id": "sa", "is_system_admin": 1}),
            ("staff", ("st",), {}),
            ("user", ("target",), {"staff_id": "st", "is_system_admin": 0}),
        ])

    def test_reason_blank_rejected(self):
        self._admin_actor()
        for bad_reason in ("", "   "):
            with self.subTest(reason=bad_reason):
                with self.assertRaises(access_service.AccessServiceError) as ctx:
                    access_service.grant_system_admin(
                        self.db_path, actor_user_id="actor", target_user_id="target",
                        reason=bad_reason, reauthenticated_at=VALID_REAUTH, now=NOW,
                    )
                self.assertEqual(ctx.exception.code, "reason_required")
        conn = self._conn()
        self.assertEqual(_admin_flag(conn, "target"), 0)
        self.assertEqual(len(_audit_rows(conn)), 0)
        conn.close()

    def test_reason_non_string_rejected(self):
        self._admin_actor()
        for bad_reason in (None, 123, ["test"]):
            with self.subTest(reason=bad_reason):
                with self.assertRaises(access_service.AccessServiceError) as ctx:
                    access_service.grant_system_admin(
                        self.db_path, actor_user_id="actor", target_user_id="target",
                        reason=bad_reason, reauthenticated_at=VALID_REAUTH, now=NOW,
                    )
                self.assertEqual(ctx.exception.code, "reason_invalid")
        conn = self._conn()
        self.assertEqual(_admin_flag(conn, "target"), 0)
        self.assertEqual(len(_audit_rows(conn)), 0)
        conn.close()

    def test_reauth_none_naive_expired_future_rejected(self):
        self._admin_actor()
        bad_values = {
            "reauth_invalid": [None, datetime(2026, 7, 15, 9, 59, 50), "2026-07-15T09:59:50+08:00"],
            "reauth_expired": [NOW - timedelta(seconds=301), NOW + timedelta(seconds=5)],
        }
        for expected_code, values in bad_values.items():
            for bad_reauth in values:
                with self.subTest(bad_reauth=bad_reauth):
                    with self.assertRaises(access_service.AccessServiceError) as ctx:
                        access_service.grant_system_admin(
                            self.db_path, actor_user_id="actor", target_user_id="target",
                            reason="test", reauthenticated_at=bad_reauth, now=NOW,
                        )
                    self.assertEqual(ctx.exception.code, expected_code)
        conn = self._conn()
        self.assertEqual(_admin_flag(conn, "target"), 0)
        self.assertEqual(len(_audit_rows(conn)), 0)
        conn.close()

    def test_now_naive_or_non_datetime_rejected(self):
        self._admin_actor()
        for bad_now in (datetime(2026, 7, 15, 10, 0, 0), "2026-07-15T10:00:00+08:00"):
            with self.subTest(bad_now=bad_now):
                with self.assertRaises(access_service.AccessServiceError) as ctx:
                    access_service.grant_system_admin(
                        self.db_path, actor_user_id="actor", target_user_id="target",
                        reason="test", reauthenticated_at=VALID_REAUTH, now=bad_now,
                    )
                self.assertEqual(ctx.exception.code, "now_invalid")
        conn = self._conn()
        self.assertEqual(_admin_flag(conn, "target"), 0)
        self.assertEqual(len(_audit_rows(conn)), 0)
        conn.close()

    def test_hostile_tzinfo_utcoffset_error_does_not_leak_and_maps_now_invalid(self):
        self._admin_actor()

        class _HostileTzinfo(tzinfo):
            def utcoffset(self, dt):
                raise RuntimeError("synthetic-tz-secret")

            def tzname(self, dt):
                return None

            def dst(self, dt):
                return None

        bad_now = datetime(2026, 7, 15, 10, 0, 0, tzinfo=_HostileTzinfo())
        with self.assertRaises(access_service.AccessServiceError) as ctx:
            access_service.grant_system_admin(
                self.db_path, actor_user_id="actor", target_user_id="target",
                reason="test", reauthenticated_at=VALID_REAUTH, now=bad_now,
            )
        self.assertEqual(ctx.exception.code, "now_invalid")
        self.assertIsNone(ctx.exception.__context__)
        self.assertIsNone(ctx.exception.__cause__)
        self.assertNotIn("secret", str(ctx.exception))
        self.assertNotIn("secret", repr(ctx.exception))

    def test_hostile_tzinfo_utcoffset_error_on_reauth_maps_reauth_invalid(self):
        self._admin_actor()

        class _HostileTzinfo(tzinfo):
            def utcoffset(self, dt):
                raise RuntimeError("synthetic-tz-secret")

            def tzname(self, dt):
                return None

            def dst(self, dt):
                return None

        bad_reauth = datetime(2026, 7, 15, 9, 59, 50, tzinfo=_HostileTzinfo())
        with self.assertRaises(access_service.AccessServiceError) as ctx:
            access_service.grant_system_admin(
                self.db_path, actor_user_id="actor", target_user_id="target",
                reason="test", reauthenticated_at=bad_reauth, now=NOW,
            )
        self.assertEqual(ctx.exception.code, "reauth_invalid")
        self.assertIsNone(ctx.exception.__context__)
        self.assertIsNone(ctx.exception.__cause__)
        self.assertNotIn("secret", str(ctx.exception))
        self.assertNotIn("secret", repr(ctx.exception))

    def test_flip_tzinfo_now_offset_read_once_and_frozen(self):
        self._admin_actor()

        flip = _FlipTzinfo()
        bad_now = datetime(2026, 7, 15, 10, 0, 0, tzinfo=flip)
        with self.assertRaises(access_service.AccessServiceError) as ctx:
            access_service.grant_system_admin(
                self.db_path, actor_user_id="actor", target_user_id="target",
                reason="test", reauthenticated_at=VALID_REAUTH, now=bad_now,
            )
        # 只问一次 offset：第二次才抛的敌意 tzinfo 根本没机会被问到，now 按冻结的
        # UTC+0 归一化成 10:00 UTC，VALID_REAUTH 相对它已过期。
        self.assertEqual(flip.calls, 1)
        self.assertEqual(ctx.exception.code, "reauth_expired")
        self.assertIsNone(ctx.exception.__context__)
        self.assertIsNone(ctx.exception.__cause__)
        self.assertNotIn("secret", str(ctx.exception))
        self.assertNotIn("secret", repr(ctx.exception))
        conn = self._conn()
        self.assertEqual(_admin_flag(conn, "target"), 0)
        self.assertEqual(len(_audit_rows(conn)), 0)
        conn.close()

    def test_flip_tzinfo_reauth_offset_read_once_and_frozen(self):
        self._admin_actor()

        flip = _FlipTzinfo()
        bad_reauth = datetime(2026, 7, 15, 9, 59, 50, tzinfo=flip)
        with self.assertRaises(access_service.AccessServiceError) as ctx:
            access_service.grant_system_admin(
                self.db_path, actor_user_id="actor", target_user_id="target",
                reason="test", reauthenticated_at=bad_reauth, now=NOW,
            )
        # 同上：reauth 按冻结的 UTC+0 = 09:59:50 UTC，早于 NOW(02:00 UTC) 八小时，
        # 稳定判过期，敌意 tzinfo 的第二个答案永远不参与判定。
        self.assertEqual(flip.calls, 1)
        self.assertEqual(ctx.exception.code, "reauth_expired")
        self.assertIsNone(ctx.exception.__context__)
        self.assertIsNone(ctx.exception.__cause__)
        self.assertNotIn("secret", str(ctx.exception))
        self.assertNotIn("secret", repr(ctx.exception))
        conn = self._conn()
        self.assertEqual(_admin_flag(conn, "target"), 0)
        self.assertEqual(len(_audit_rows(conn)), 0)
        conn.close()

    def test_reauth_dst_fold_uses_real_utc_difference(self):
        self._admin_actor()
        la = ZoneInfo("America/Los_Angeles")
        # 2026-11-01 美国太平洋时区秋季回拨：01:58(fold=0,PDT) 到 01:59(fold=1,PST)
        # 墙钟只差 60 秒，真实 UTC 时间差 3660 秒，必须按真实时间差判定过期。
        reauth = datetime(2026, 11, 1, 1, 58, 0, tzinfo=la, fold=0)
        now = datetime(2026, 11, 1, 1, 59, 0, tzinfo=la, fold=1)
        with self.assertRaises(access_service.AccessServiceError) as ctx:
            access_service.grant_system_admin(
                self.db_path, actor_user_id="actor", target_user_id="target",
                reason="test", reauthenticated_at=reauth, now=now,
            )
        self.assertEqual(ctx.exception.code, "reauth_expired")

    def test_reauth_cross_timezone_real_time_diff_within_window_passes(self):
        self._admin_actor()
        now_bj = datetime(2026, 7, 15, 10, 0, 10, tzinfo=BJ)
        reauth_utc = datetime(2026, 7, 15, 2, 0, 0, tzinfo=timezone.utc)  # 北京 10:00:00，早 10 秒
        access_service.grant_system_admin(
            self.db_path, actor_user_id="actor", target_user_id="target",
            reason="test", reauthenticated_at=reauth_utc, now=now_bj,
        )
        conn = self._conn()
        self.assertEqual(_admin_flag(conn, "target"), 1)
        conn.close()

    def test_reauth_exactly_300_seconds_passes_over_rejected(self):
        self._admin_actor()
        self._seed([("staff", ("st2",), {}), ("user", ("target2",), {"staff_id": "st2"})])
        access_service.grant_system_admin(
            self.db_path, actor_user_id="actor", target_user_id="target",
            reason="test", reauthenticated_at=NOW - timedelta(seconds=300), now=NOW,
        )
        conn = self._conn()
        self.assertEqual(_admin_flag(conn, "target"), 1)
        conn.close()

        with self.assertRaises(access_service.AccessServiceError) as ctx:
            access_service.grant_system_admin(
                self.db_path, actor_user_id="actor", target_user_id="target2",
                reason="test",
                reauthenticated_at=NOW - timedelta(seconds=300, microseconds=1),
                now=NOW,
            )
        self.assertEqual(ctx.exception.code, "reauth_expired")

    # --- grant ---

    def test_grant_success_then_duplicate_rejected(self):
        self._admin_actor()
        access_service.grant_system_admin(
            self.db_path, actor_user_id="actor", target_user_id="target",
            reason=" 新聘管理员 ", reauthenticated_at=VALID_REAUTH, now=NOW,
        )
        conn = self._conn()
        self.assertEqual(_admin_flag(conn, "target"), 1)
        rows = _audit_rows(conn)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["action"], "system_admin.grant")
        self.assertEqual(row["actor_user_id"], "actor")
        self.assertEqual(row["reason"], "新聘管理员")
        self.assertEqual(row["risk_level"], "high")
        self.assertEqual(row["result"], "success")
        self.assertIn('"is_system_admin": false', row["old_json"])
        self.assertIn('"is_system_admin": true', row["new_json"])
        conn.close()

        with self.assertRaises(access_service.AccessServiceError) as ctx:
            access_service.grant_system_admin(
                self.db_path, actor_user_id="actor", target_user_id="target",
                reason="test", reauthenticated_at=VALID_REAUTH, now=NOW,
            )
        self.assertEqual(ctx.exception.code, "target_already_admin")
        conn = self._conn()
        self.assertEqual(len(_audit_rows(conn)), 1)
        conn.close()

    def test_grant_inactive_target_rejected(self):
        self._seed([
            ("staff", ("sa",), {}),
            ("user", ("actor",), {"staff_id": "sa", "is_system_admin": 1}),
            ("staff", ("st",), {}),
            ("user", ("target",), {"staff_id": "st", "is_active": 0}),
        ])
        with self.assertRaises(access_service.AccessServiceError) as ctx:
            access_service.grant_system_admin(
                self.db_path, actor_user_id="actor", target_user_id="target",
                reason="test", reauthenticated_at=VALID_REAUTH, now=NOW,
            )
        self.assertEqual(ctx.exception.code, "target_inactive")

    def test_grant_non_staff_target_rejected(self):
        self._seed([
            ("staff", ("sa",), {}),
            ("user", ("actor",), {"staff_id": "sa", "is_system_admin": 1}),
            ("user", ("target",), {"account_kind": "independent_admin", "is_system_admin": 1}),
        ])
        with self.assertRaises(access_service.AccessServiceError) as ctx:
            access_service.grant_system_admin(
                self.db_path, actor_user_id="actor", target_user_id="target",
                reason="test", reauthenticated_at=VALID_REAUTH, now=NOW,
            )
        self.assertEqual(ctx.exception.code, "target_not_eligible")

    # --- revoke ---

    def test_revoke_self_rejected(self):
        self._seed([
            ("staff", ("sa",), {}),
            ("user", ("actor",), {"staff_id": "sa", "is_system_admin": 1}),
        ])
        with self.assertRaises(access_service.AccessServiceError) as ctx:
            access_service.revoke_system_admin(
                self.db_path, actor_user_id="actor", target_user_id="actor",
                reason="test", reauthenticated_at=VALID_REAUTH, now=NOW,
            )
        self.assertEqual(ctx.exception.code, "cannot_revoke_self")
        conn = self._conn()
        self.assertEqual(_admin_flag(conn, "actor"), 1)
        conn.close()

    def test_revoke_last_regular_admin_rejected_break_glass_not_counted(self):
        self._seed([
            ("user", ("bg",), {"account_kind": "break_glass", "is_system_admin": 1}),
            ("staff", ("st",), {}),
            ("user", ("only_admin",), {"staff_id": "st", "is_system_admin": 1}),
        ])
        with self.assertRaises(access_service.AccessServiceError) as ctx:
            access_service.revoke_system_admin(
                self.db_path, actor_user_id="bg", target_user_id="only_admin",
                reason="test", reauthenticated_at=VALID_REAUTH, now=NOW,
            )
        self.assertEqual(ctx.exception.code, "admin_floor_violation")
        conn = self._conn()
        self.assertEqual(_admin_flag(conn, "only_admin"), 1)
        self.assertEqual(len(_audit_rows(conn)), 0)
        conn.close()

    def test_revoke_success_with_two_regular_admins(self):
        self._seed([
            ("staff", ("s1",), {}),
            ("user", ("admin1",), {"staff_id": "s1", "is_system_admin": 1}),
            ("staff", ("s2",), {}),
            ("user", ("admin2",), {"staff_id": "s2", "is_system_admin": 1}),
        ])
        access_service.revoke_system_admin(
            self.db_path, actor_user_id="admin1", target_user_id="admin2",
            reason="离职撤权", reauthenticated_at=VALID_REAUTH, now=NOW,
        )
        conn = self._conn()
        self.assertEqual(_admin_flag(conn, "admin2"), 0)
        rows = _audit_rows(conn)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["action"], "system_admin.revoke")
        self.assertEqual(row["actor_user_id"], "admin1")
        self.assertEqual(row["entity_id"], "admin2")
        self.assertEqual(row["reason"], "离职撤权")
        self.assertIn('"is_system_admin": true', row["old_json"])
        self.assertIn('"is_system_admin": false', row["new_json"])
        conn.close()

    # --- 审计事务失败整体回滚 ---

    def test_audit_trigger_failure_rolls_back_admin_change(self):
        self._admin_actor()
        conn = sqlite3.connect(str(self.db_path))
        conn.execute(
            "create trigger block_audit before insert on audit_logs "
            "begin select raise(abort, 'synthetic secret trigger text'); end"
        )
        conn.commit()
        conn.close()

        with self.assertRaises(access_service.AccessServiceError) as ctx:
            access_service.grant_system_admin(
                self.db_path, actor_user_id="actor", target_user_id="target",
                reason="test", reauthenticated_at=VALID_REAUTH, now=NOW,
            )
        self.assertEqual(ctx.exception.code, "transaction_failed")
        self.assertNotIn("secret", str(ctx.exception))
        self.assertNotIn("secret", repr(ctx.exception))
        conn = self._conn()
        self.assertEqual(_admin_flag(conn, "target"), 0)
        self.assertEqual(len(_audit_rows(conn)), 0)
        conn.close()

    # --- db_path 必须已存在，绝不隐式建库 ---

    def test_missing_db_path_rejected_without_creating_file(self):
        missing_path = Path(self._tmp.name) / "does_not_exist.db"
        with self.assertRaises(access_service.AccessServiceError) as ctx:
            access_service.grant_system_admin(
                missing_path, actor_user_id="actor", target_user_id="target",
                reason="test", reauthenticated_at=VALID_REAUTH, now=NOW,
            )
        self.assertEqual(ctx.exception.code, "database_unavailable")
        self.assertFalse(missing_path.exists())

    def test_illegal_db_path_types_rejected_as_database_unavailable(self):
        for bad_path in (None, 123, object()):
            with self.subTest(bad_path=bad_path):
                with self.assertRaises(access_service.AccessServiceError) as ctx:
                    access_service.grant_system_admin(
                        bad_path, actor_user_id="actor", target_user_id="target",
                        reason="test", reauthenticated_at=VALID_REAUTH, now=NOW,
                    )
                self.assertEqual(ctx.exception.code, "database_unavailable")

    def test_pathlike_fspath_oserror_does_not_leak_into_exception_chain(self):
        class _HostileDbPath:
            def __fspath__(self):
                raise OSError("synthetic-path-secret")

        with self.assertRaises(access_service.AccessServiceError) as ctx:
            access_service.grant_system_admin(
                _HostileDbPath(), actor_user_id="actor", target_user_id="target",
                reason="test", reauthenticated_at=VALID_REAUTH, now=NOW,
            )
        self.assertEqual(ctx.exception.code, "database_unavailable")
        self.assertIsNone(ctx.exception.__context__)
        self.assertIsNone(ctx.exception.__cause__)
        self.assertNotIn("secret", str(ctx.exception))
        self.assertNotIn("secret", repr(ctx.exception))

    def test_pathlike_fspath_runtimeerror_does_not_leak_into_exception_chain(self):
        class _HostileDbPath:
            def __fspath__(self):
                raise RuntimeError("runtime-path-secret")

        with self.assertRaises(access_service.AccessServiceError) as ctx:
            access_service.grant_system_admin(
                _HostileDbPath(), actor_user_id="actor", target_user_id="target",
                reason="test", reauthenticated_at=VALID_REAUTH, now=NOW,
            )
        self.assertEqual(ctx.exception.code, "database_unavailable")
        self.assertIsNone(ctx.exception.__context__)
        self.assertIsNone(ctx.exception.__cause__)
        self.assertNotIn("secret", str(ctx.exception))
        self.assertNotIn("secret", repr(ctx.exception))

    # --- actor 必须活跃且已授权 ---

    def test_inactive_system_admin_actor_rejected(self):
        self._seed([
            ("staff", ("sa",), {}),
            ("user", ("actor",), {"staff_id": "sa", "is_active": 0, "is_system_admin": 1}),
            ("staff", ("st",), {}),
            ("user", ("target",), {"staff_id": "st"}),
        ])
        with self.assertRaises(access_service.AccessServiceError) as ctx:
            access_service.grant_system_admin(
                self.db_path, actor_user_id="actor", target_user_id="target",
                reason="test", reauthenticated_at=VALID_REAUTH, now=NOW,
            )
        self.assertEqual(ctx.exception.code, "actor_not_authorized")
        conn = self._conn()
        self.assertEqual(_admin_flag(conn, "target"), 0)
        self.assertEqual(len(_audit_rows(conn)), 0)
        conn.close()

    # --- BEGIN / COMMIT / close 层的 sqlite3.Error 全部稳定 transaction_failed ---

    def _patched_connect(self, connection_cls):
        """把 sqlite3.connect(...) 换成用给定子类打开连接，其余参数原样透传。"""
        original_connect = sqlite3.connect

        def fake_connect(*args, **kwargs):
            kwargs["factory"] = connection_cls
            return original_connect(*args, **kwargs)

        return mock.patch("local_app.access_service.sqlite3.connect", fake_connect)

    def test_begin_failure_reports_transaction_failed(self):
        self._admin_actor()

        class _BeginFailConnection(sqlite3.Connection):
            def execute(self, sql, *args, **kwargs):
                if sql == "begin immediate":
                    raise sqlite3.OperationalError("synthetic begin failure")
                return super().execute(sql, *args, **kwargs)

        with self._patched_connect(_BeginFailConnection):
            with self.assertRaises(access_service.AccessServiceError) as ctx:
                access_service.grant_system_admin(
                    self.db_path, actor_user_id="actor", target_user_id="target",
                    reason="test", reauthenticated_at=VALID_REAUTH, now=NOW,
                )
            self.assertEqual(ctx.exception.code, "transaction_failed")
        conn = self._conn()
        self.assertEqual(_admin_flag(conn, "target"), 0)
        self.assertEqual(len(_audit_rows(conn)), 0)
        conn.close()

    def test_commit_failure_rolls_back_admin_change_and_audit(self):
        self._admin_actor()

        class _CommitFailConnection(sqlite3.Connection):
            def execute(self, sql, *args, **kwargs):
                if sql == "commit":
                    raise sqlite3.OperationalError("synthetic commit failure")
                return super().execute(sql, *args, **kwargs)

        with self._patched_connect(_CommitFailConnection):
            with self.assertRaises(access_service.AccessServiceError) as ctx:
                access_service.grant_system_admin(
                    self.db_path, actor_user_id="actor", target_user_id="target",
                    reason="test", reauthenticated_at=VALID_REAUTH, now=NOW,
                )
            self.assertEqual(ctx.exception.code, "transaction_failed")
        conn = self._conn()
        self.assertEqual(_admin_flag(conn, "target"), 0)
        self.assertEqual(len(_audit_rows(conn)), 0)
        conn.close()

    def test_audit_trigger_and_rollback_failure_still_transaction_failed(self):
        self._admin_actor()
        conn = sqlite3.connect(str(self.db_path))
        conn.execute(
            "create trigger block_audit before insert on audit_logs "
            "begin select raise(abort, 'synthetic secret trigger text'); end"
        )
        conn.commit()
        conn.close()

        class _RollbackFailConnection(sqlite3.Connection):
            def execute(self, sql, *args, **kwargs):
                if sql == "rollback":
                    raise sqlite3.OperationalError("synthetic rollback failure")
                return super().execute(sql, *args, **kwargs)

        with self._patched_connect(_RollbackFailConnection):
            with self.assertRaises(access_service.AccessServiceError) as ctx:
                access_service.grant_system_admin(
                    self.db_path, actor_user_id="actor", target_user_id="target",
                    reason="test", reauthenticated_at=VALID_REAUTH, now=NOW,
                )
            self.assertEqual(ctx.exception.code, "transaction_failed")
        conn = self._conn()
        self.assertEqual(_admin_flag(conn, "target"), 0)
        self.assertEqual(len(_audit_rows(conn)), 0)
        conn.close()

    def test_close_failure_after_success_does_not_leak(self):
        self._admin_actor()

        class _CloseFailConnection(sqlite3.Connection):
            def close(self):
                super().close()
                raise sqlite3.OperationalError("synthetic close failure")

        with self._patched_connect(_CloseFailConnection):
            access_service.grant_system_admin(
                self.db_path, actor_user_id="actor", target_user_id="target",
                reason="test", reauthenticated_at=VALID_REAUTH, now=NOW,
            )
        conn = self._conn()
        self.assertEqual(_admin_flag(conn, "target"), 1)
        conn.close()

    def test_connect_failure_reports_transaction_failed(self):
        self._admin_actor()

        def fake_connect(*args, **kwargs):
            raise sqlite3.OperationalError("synthetic connect failure")

        with mock.patch("local_app.access_service.sqlite3.connect", fake_connect):
            with self.assertRaises(access_service.AccessServiceError) as ctx:
                access_service.grant_system_admin(
                    self.db_path, actor_user_id="actor", target_user_id="target",
                    reason="test", reauthenticated_at=VALID_REAUTH, now=NOW,
                )
            self.assertEqual(ctx.exception.code, "transaction_failed")
        conn = self._conn()
        self.assertEqual(_admin_flag(conn, "target"), 0)
        self.assertEqual(len(_audit_rows(conn)), 0)
        conn.close()

    def test_pragma_init_failure_reports_transaction_failed(self):
        self._admin_actor()

        class _PragmaFailConnection(sqlite3.Connection):
            def execute(self, sql, *args, **kwargs):
                if sql.startswith("pragma"):
                    raise sqlite3.OperationalError("synthetic pragma failure")
                return super().execute(sql, *args, **kwargs)

        with self._patched_connect(_PragmaFailConnection):
            with self.assertRaises(access_service.AccessServiceError) as ctx:
                access_service.grant_system_admin(
                    self.db_path, actor_user_id="actor", target_user_id="target",
                    reason="test", reauthenticated_at=VALID_REAUTH, now=NOW,
                )
            self.assertEqual(ctx.exception.code, "transaction_failed")
        conn = self._conn()
        self.assertEqual(_admin_flag(conn, "target"), 0)
        self.assertEqual(len(_audit_rows(conn)), 0)
        conn.close()

    def test_actor_or_target_select_or_update_failure_reports_transaction_failed(self):
        self._admin_actor()

        def make_fail_connection(match):
            class _FailConnection(sqlite3.Connection):
                def execute(self, sql, *args, **kwargs):
                    if match in sql:
                        raise sqlite3.OperationalError("synthetic %s failure" % match)
                    return super().execute(sql, *args, **kwargs)
            return _FailConnection

        cases = [
            ("select id, username, is_active, is_system_admin from users", "actor SELECT"),
            ("select id, is_active, is_system_admin, account_kind from users", "target SELECT"),
            ("update users set is_system_admin", "UPDATE"),
        ]
        for match, label in cases:
            with self.subTest(step=label):
                with self._patched_connect(make_fail_connection(match)):
                    with self.assertRaises(access_service.AccessServiceError) as ctx:
                        access_service.grant_system_admin(
                            self.db_path, actor_user_id="actor", target_user_id="target",
                            reason="test", reauthenticated_at=VALID_REAUTH, now=NOW,
                        )
                    self.assertEqual(ctx.exception.code, "transaction_failed")
                conn = self._conn()
                self.assertEqual(_admin_flag(conn, "target"), 0)
                self.assertEqual(len(_audit_rows(conn)), 0)
                conn.close()

    def test_admin_floor_select_failure_on_revoke_reports_transaction_failed(self):
        self._seed([
            ("staff", ("s1",), {}),
            ("user", ("admin1",), {"staff_id": "s1", "is_system_admin": 1}),
            ("staff", ("s2",), {}),
            ("user", ("admin2",), {"staff_id": "s2", "is_system_admin": 1}),
        ])

        class _FloorSelectFailConnection(sqlite3.Connection):
            def execute(self, sql, *args, **kwargs):
                if "select count(*) as n from users" in sql:
                    raise sqlite3.OperationalError("synthetic admin-floor failure")
                return super().execute(sql, *args, **kwargs)

        with self._patched_connect(_FloorSelectFailConnection):
            with self.assertRaises(access_service.AccessServiceError) as ctx:
                access_service.revoke_system_admin(
                    self.db_path, actor_user_id="admin1", target_user_id="admin2",
                    reason="test", reauthenticated_at=VALID_REAUTH, now=NOW,
                )
            self.assertEqual(ctx.exception.code, "transaction_failed")
        conn = self._conn()
        self.assertEqual(_admin_flag(conn, "admin2"), 1)
        self.assertEqual(len(_audit_rows(conn)), 0)
        conn.close()

    def test_transaction_failed_error_has_no_exception_context(self):
        self._admin_actor()

        class _CommitFailConnection(sqlite3.Connection):
            def execute(self, sql, *args, **kwargs):
                if sql == "commit":
                    raise sqlite3.OperationalError("synthetic commit failure")
                return super().execute(sql, *args, **kwargs)

        with self._patched_connect(_CommitFailConnection):
            try:
                access_service.grant_system_admin(
                    self.db_path, actor_user_id="actor", target_user_id="target",
                    reason="test", reauthenticated_at=VALID_REAUTH, now=NOW,
                )
                self.fail("expected AccessServiceError")
            except access_service.AccessServiceError as err:
                self.assertIsNone(err.__context__)
                self.assertIsNone(err.__cause__)

    def test_utc_now_written_as_beijing_time_in_audit(self):
        self._admin_actor()
        now_utc = datetime(2026, 7, 15, 2, 0, 0, tzinfo=timezone.utc)  # 北京时间 10:00:00
        access_service.grant_system_admin(
            self.db_path, actor_user_id="actor", target_user_id="target",
            reason="test", reauthenticated_at=VALID_REAUTH, now=now_utc,
        )
        conn = self._conn()
        row = _audit_rows(conn)[0]
        self.assertEqual(row["created_at"], "2026-07-15 10:00:00")
        conn.close()

    # --- 错误码不泄露；不产生库外文件 ---

    def test_error_str_repr_do_not_leak_reason_or_paths(self):
        self._admin_actor()
        secret_reason = "secret-patient-name-张三"
        try:
            access_service.grant_system_admin(
                self.db_path, actor_user_id="missing", target_user_id="target",
                reason=secret_reason, reauthenticated_at=VALID_REAUTH, now=NOW,
            )
            self.fail("expected AccessServiceError")
        except access_service.AccessServiceError as err:
            self.assertNotIn(secret_reason, str(err))
            self.assertNotIn(secret_reason, repr(err))
            self.assertNotIn(str(self.db_path), str(err))
            self.assertNotIn(str(self.db_path), repr(err))

    def test_no_files_outside_synthetic_db(self):
        self._admin_actor()
        access_service.grant_system_admin(
            self.db_path, actor_user_id="actor", target_user_id="target",
            reason="test", reauthenticated_at=VALID_REAUTH, now=NOW,
        )
        names = {p.name for p in Path(self._tmp.name).iterdir()}
        self.assertTrue(names.issubset({"synthetic.db", "synthetic.db-journal"}))


# === 并发系统管理员撤权不变量（Task 2B.2）===
# 用真实两条 sqlite3.Connection + BEGIN IMMEDIATE 制造确定性写锁争用：
# 第一个连接拿到写锁后暂停，直到第二个连接已进入/尝试 BEGIN IMMEDIATE 才放行第一个提交。
# 不 mock 业务逻辑，只替换 sqlite3.connect 注入的 Connection 子类，底层仍是真实文件库。

_SYNC_WAIT_TIMEOUT = 5
_CONCURRENCY_ROUNDS = 6


def _make_ordered_connection_class(timeout=_SYNC_WAIT_TIMEOUT, expected_busy_timeout=5000):
    """返回一个只在本轮生效的 Connection 子类：谁先到达 BEGIN IMMEDIATE 谁当
    first（真正拿锁后暂停等待 release_first），另一个当 second——second 先查询
    该连接真实的 pragma busy_timeout 原值并断言等于生产合同 5000ms，再临时
    置 0 对同一把写锁做一次零等待真实探针，必须拿到 SQLite 的 locked/busy
    错误，才能证明它是在 first 持锁期间真撞锁；无论探针/合同断言是否通过，
    finally 都恢复"查询得到的原值"并 set release_first 唤醒 first 提交，避免
    first 等满超时；探针成功后 second 自己发起正常等待版 BEGIN IMMEDIATE
    完成串行化。"""
    state = {"first_claimed": False}
    state_lock = threading.Lock()
    first_ready = threading.Event()
    lock_collision_observed = threading.Event()
    release_first = threading.Event()

    class _OrderedConnection(sqlite3.Connection):
        def execute(self, sql, *args, **kwargs):
            if sql != "begin immediate":
                return super().execute(sql, *args, **kwargs)
            with state_lock:
                is_first = not state["first_claimed"]
                state["first_claimed"] = True
            if is_first:
                result = super().execute(sql, *args, **kwargs)
                first_ready.set()
                if not release_first.wait(timeout=timeout):
                    raise AssertionError("second 线程超时未证明真实撞锁")
                return result
            if not first_ready.wait(timeout=timeout):
                raise AssertionError("first 线程超时未拿到 BEGIN IMMEDIATE 写锁")

            original_timeout = super().execute("pragma busy_timeout").fetchone()[0]
            try:
                if original_timeout != expected_busy_timeout:
                    raise AssertionError(
                        "生产 busy_timeout 合同被打破：期望 %r，实际 %r"
                        % (expected_busy_timeout, original_timeout)
                    )
                super().execute("pragma busy_timeout=0")
                try:
                    super().execute(sql, *args, **kwargs)
                except sqlite3.OperationalError as probe_err:
                    msg = str(probe_err).lower()
                    if "locked" not in msg and "busy" not in msg:
                        raise AssertionError(
                            f"零等待探针得到非预期错误：{probe_err!r}"
                        ) from probe_err
                else:
                    try:
                        super().rollback()
                    except sqlite3.OperationalError:
                        pass
                    raise AssertionError("零等待探针意外成功，未证明真实撞锁")
                lock_collision_observed.set()
            finally:
                super().execute("pragma busy_timeout=%d" % original_timeout)
                release_first.set()

            return super().execute(sql, *args, **kwargs)

    _OrderedConnection.first_ready = first_ready
    _OrderedConnection.lock_collision_observed = lock_collision_observed
    _OrderedConnection.release_first = release_first
    return _OrderedConnection


class ConcurrentSystemAdminTests(unittest.TestCase):
    def _patched_connect(self, connection_cls):
        original_connect = sqlite3.connect

        def fake_connect(*args, **kwargs):
            kwargs["factory"] = connection_cls
            return original_connect(*args, **kwargs)

        return mock.patch("local_app.access_service.sqlite3.connect", fake_connect)

    def _seed_into(self, db_path, rows):
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        conn.execute("pragma foreign_keys = on")
        for kind, args, kwargs in rows:
            if kind == "staff":
                _insert_staff(conn, *args, **kwargs)
            else:
                _insert_user(conn, *args, **kwargs)
        conn.commit()
        conn.close()

    def _run_thread(self, func, results, idx):
        def target():
            try:
                func()
                results[idx] = ("ok", None)
            except access_service.AccessServiceError as err:
                results[idx] = ("err", err.code)
            except Exception as exc:  # 捕获意外异常，让断言暴露而不是静默挂起
                results[idx] = ("exc", repr(exc))

        thread = threading.Thread(target=target)
        thread.daemon = True
        return thread

    def _run_concurrent_revokes(self, call_a, call_b, first_idx):
        conn_cls = _make_ordered_connection_class()
        results = [None, None]
        funcs = [call_a, call_b]
        second_idx = 1 - first_idx
        t_first = self._run_thread(funcs[first_idx], results, first_idx)
        t_second = self._run_thread(funcs[second_idx], results, second_idx)
        got_lock = False
        try:
            with self._patched_connect(conn_cls):
                t_first.start()
                got_lock = conn_cls.first_ready.wait(timeout=_SYNC_WAIT_TIMEOUT)
                t_second.start()
                t_first.join(timeout=_SYNC_WAIT_TIMEOUT * 2)
                t_second.join(timeout=_SYNC_WAIT_TIMEOUT * 2)
        finally:
            conn_cls.release_first.set()
            t_first.join(timeout=_SYNC_WAIT_TIMEOUT)
            t_second.join(timeout=_SYNC_WAIT_TIMEOUT)
        self.assertTrue(got_lock, "first 线程超时未拿到 BEGIN IMMEDIATE 写锁")
        self.assertTrue(
            conn_cls.lock_collision_observed.is_set(),
            "second 线程未证明真实撞上 SQLite 写锁",
        )
        self.assertFalse(t_first.is_alive(), "first 线程未退出，测试可能死锁")
        self.assertFalse(t_second.is_alive(), "second 线程未退出，测试可能死锁")
        self.assertIsNotNone(results[0], results)
        self.assertIsNotNone(results[1], results)
        return results

    def test_concurrent_mutual_revoke_exactly_one_succeeds(self):
        for round_idx in range(_CONCURRENCY_ROUNDS):
            with self.subTest(round=round_idx):
                with tempfile.TemporaryDirectory() as tmp_dir:
                    db_path = Path(tmp_dir) / "synthetic.db"
                    _build_db(db_path)
                    self._seed_into(db_path, [
                        ("staff", ("s1",), {}),
                        ("user", ("A",), {"staff_id": "s1", "is_system_admin": 1}),
                        ("staff", ("s2",), {}),
                        ("user", ("B",), {"staff_id": "s2", "is_system_admin": 1}),
                    ])

                    def call_a():
                        access_service.revoke_system_admin(
                            db_path, actor_user_id="A", target_user_id="B",
                            reason="并发互相撤权测试", reauthenticated_at=VALID_REAUTH, now=NOW,
                        )

                    def call_b():
                        access_service.revoke_system_admin(
                            db_path, actor_user_id="B", target_user_id="A",
                            reason="并发互相撤权测试", reauthenticated_at=VALID_REAUTH, now=NOW,
                        )

                    first_idx = round_idx % 2
                    first_actor, first_target = ("A", "B") if first_idx == 0 else ("B", "A")
                    results = self._run_concurrent_revokes(
                        call_a, call_b, first_idx=first_idx,
                    )

                    self.assertEqual(results[first_idx], ("ok", None), results)
                    self.assertEqual(
                        results[1 - first_idx], ("err", "actor_not_authorized"), results,
                    )

                    conn = sqlite3.connect(str(db_path))
                    conn.row_factory = sqlite3.Row
                    self.assertEqual(
                        conn.execute("pragma quick_check").fetchone()[0], "ok",
                    )
                    active_regular = conn.execute(
                        "select id from users where is_active = 1 and is_system_admin = 1 "
                        "and account_kind != 'break_glass'"
                    ).fetchall()
                    self.assertEqual(len(active_regular), 1, [dict(r) for r in active_regular])
                    winner = active_regular[0]["id"]
                    loser = "B" if winner == "A" else "A"
                    self.assertEqual(winner, first_actor, results)
                    self.assertEqual(loser, first_target, results)
                    self.assertEqual(_admin_flag(conn, winner), 1)
                    self.assertEqual(_admin_flag(conn, loser), 0)

                    rows = _audit_rows(conn)
                    self.assertEqual(len(rows), 1, [dict(r) for r in rows])
                    row = rows[0]
                    self.assertEqual(row["action"], "system_admin.revoke")
                    self.assertEqual(row["actor_user_id"], winner)
                    self.assertEqual(row["entity_id"], loser)
                    self.assertIn('"is_system_admin": true', row["old_json"])
                    self.assertIn('"is_system_admin": false', row["new_json"])
                    conn.close()

                    names = {p.name for p in Path(tmp_dir).iterdir()}
                    self.assertTrue(names.issubset({"synthetic.db", "synthetic.db-journal"}), names)

    def test_concurrent_break_glass_revoke_of_last_two_admins(self):
        for round_idx in range(_CONCURRENCY_ROUNDS):
            with self.subTest(round=round_idx):
                with tempfile.TemporaryDirectory() as tmp_dir:
                    db_path = Path(tmp_dir) / "synthetic.db"
                    _build_db(db_path)
                    self._seed_into(db_path, [
                        ("user", ("bg",), {"account_kind": "break_glass", "is_system_admin": 1}),
                        ("staff", ("s1",), {}),
                        ("user", ("A",), {"staff_id": "s1", "is_system_admin": 1}),
                        ("staff", ("s2",), {}),
                        ("user", ("B",), {"staff_id": "s2", "is_system_admin": 1}),
                    ])

                    def call_a():
                        access_service.revoke_system_admin(
                            db_path, actor_user_id="bg", target_user_id="A",
                            reason="break_glass 批量撤权测试", reauthenticated_at=VALID_REAUTH, now=NOW,
                        )

                    def call_b():
                        access_service.revoke_system_admin(
                            db_path, actor_user_id="bg", target_user_id="B",
                            reason="break_glass 批量撤权测试", reauthenticated_at=VALID_REAUTH, now=NOW,
                        )

                    first_idx = round_idx % 2
                    first_target = "A" if first_idx == 0 else "B"
                    second_target = "B" if first_idx == 0 else "A"
                    results = self._run_concurrent_revokes(
                        call_a, call_b, first_idx=first_idx,
                    )

                    self.assertEqual(results[first_idx], ("ok", None), results)
                    self.assertEqual(
                        results[1 - first_idx], ("err", "admin_floor_violation"), results,
                    )

                    conn = sqlite3.connect(str(db_path))
                    conn.row_factory = sqlite3.Row
                    self.assertEqual(
                        conn.execute("pragma quick_check").fetchone()[0], "ok",
                    )
                    active_regular = conn.execute(
                        "select id from users where is_active = 1 and is_system_admin = 1 "
                        "and account_kind != 'break_glass'"
                    ).fetchall()
                    self.assertEqual(len(active_regular), 1, [dict(r) for r in active_regular])
                    remaining_admin = active_regular[0]["id"]
                    self.assertEqual(remaining_admin, second_target, results)
                    self.assertEqual(_admin_flag(conn, remaining_admin), 1)
                    self.assertEqual(_admin_flag(conn, first_target), 0)

                    rows = _audit_rows(conn)
                    self.assertEqual(len(rows), 1, [dict(r) for r in rows])
                    row = rows[0]
                    self.assertEqual(row["action"], "system_admin.revoke")
                    self.assertEqual(row["actor_user_id"], "bg")
                    self.assertEqual(row["entity_id"], first_target)
                    conn.close()

                    names = {p.name for p in Path(tmp_dir).iterdir()}
                    self.assertTrue(names.issubset({"synthetic.db", "synthetic.db-journal"}), names)

    # --- 反事实回归：生产 busy_timeout 配置若被悄悄改坏，探针必须报错，
    # 不能被夹具的"写回正确值"掩盖成假绿 ---

    def test_counterfactual_broken_production_busy_timeout_fails_probe(self):
        original_open_existing = access_service._open_existing

        def broken_open_existing(db_path):
            conn = original_open_existing(db_path)
            conn.execute("pragma busy_timeout = 0")
            return conn

        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = Path(tmp_dir) / "synthetic.db"
            _build_db(db_path)
            self._seed_into(db_path, [
                ("staff", ("s1",), {}),
                ("user", ("A",), {"staff_id": "s1", "is_system_admin": 1}),
                ("staff", ("s2",), {}),
                ("user", ("B",), {"staff_id": "s2", "is_system_admin": 1}),
            ])

            def call_a():
                access_service.revoke_system_admin(
                    db_path, actor_user_id="A", target_user_id="B",
                    reason="反事实busy_timeout回归测试", reauthenticated_at=VALID_REAUTH, now=NOW,
                )

            def call_b():
                access_service.revoke_system_admin(
                    db_path, actor_user_id="B", target_user_id="A",
                    reason="反事实busy_timeout回归测试", reauthenticated_at=VALID_REAUTH, now=NOW,
                )

            conn_cls = _make_ordered_connection_class()
            results = [None, None]
            t_first = self._run_thread(call_a, results, 0)
            t_second = self._run_thread(call_b, results, 1)
            try:
                with mock.patch(
                    "local_app.access_service._open_existing", broken_open_existing,
                ), self._patched_connect(conn_cls):
                    t_first.start()
                    conn_cls.first_ready.wait(timeout=_SYNC_WAIT_TIMEOUT)
                    t_second.start()
                    t_first.join(timeout=_SYNC_WAIT_TIMEOUT * 2)
                    t_second.join(timeout=_SYNC_WAIT_TIMEOUT * 2)
            finally:
                conn_cls.release_first.set()
                t_first.join(timeout=_SYNC_WAIT_TIMEOUT)
                t_second.join(timeout=_SYNC_WAIT_TIMEOUT)

            self.assertFalse(t_first.is_alive(), "first 线程未退出，测试可能死锁")
            self.assertFalse(t_second.is_alive(), "second 线程未退出，测试可能死锁")
            # 生产 busy_timeout 被悄悄改成 0（不等于合同 5000），探针必须报错，
            # 绝不能因为夹具事后"写回正确值"而误判为真实撞锁。
            self.assertFalse(conn_cls.lock_collision_observed.is_set())
            self.assertEqual(results[0], ("ok", None), results)
            self.assertEqual(results[1][0], "exc", results)
            self.assertIn("busy_timeout", results[1][1])


if __name__ == "__main__":
    unittest.main()
