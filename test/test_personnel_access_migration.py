import hashlib
import inspect
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from local_app import personnel_access_migration as pam

SECRET_MARKER = "SECRETMARK"

FIXED_TABLES = (
    "staff_members",
    "users",
    "sessions",
    "roles",
    "role_permissions",
    "audit_logs",
)

REQUIRED_COLUMNS = {
    "staff_members": ["staff_id", "name", "role", "roles", "active"],
    "users": ["id", "username", "password_hash", "role", "is_active", "staff_id"],
    "sessions": ["token", "user_id", "created_at", "expires_at"],
    "roles": ["role_key", "name"],
    "role_permissions": ["role_key", "perm_key"],
    "audit_logs": [
        "audit_id",
        "entity_type",
        "entity_id",
        "action",
        "old_json",
        "new_json",
        "operator",
        "created_at",
    ],
}

AUDIT_EXTENDED_FIELDS = [
    "actor_user_id",
    "result",
    "reason",
    "risk_level",
    "request_id",
    "ip_address",
    "user_agent",
]


def _file_sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _build_full_db(path):
    con = sqlite3.connect(path)
    try:
        con.executescript(
            """
            CREATE TABLE staff_members (
                staff_id INTEGER PRIMARY KEY,
                name TEXT,
                role TEXT,
                roles TEXT,
                active INTEGER,
                phone TEXT,
                id_card TEXT,
                emp_no TEXT
            );
            CREATE TABLE users (
                id INTEGER PRIMARY KEY,
                username TEXT,
                password_hash TEXT,
                role TEXT,
                is_active INTEGER,
                staff_id INTEGER
            );
            CREATE TABLE sessions (
                token TEXT,
                user_id INTEGER,
                created_at TEXT,
                expires_at TEXT
            );
            CREATE TABLE roles (
                role_key TEXT PRIMARY KEY,
                name TEXT
            );
            CREATE TABLE role_permissions (
                role_key TEXT,
                perm_key TEXT
            );
            CREATE TABLE audit_logs (
                audit_id INTEGER PRIMARY KEY,
                entity_type TEXT,
                entity_id TEXT,
                action TEXT,
                old_json TEXT,
                new_json TEXT,
                operator TEXT,
                created_at TEXT
            );
            """
        )

        con.execute(
            "INSERT INTO roles(role_key, name) VALUES (?, ?)", ("admin", "管理员")
        )
        con.execute(
            "INSERT INTO roles(role_key, name) VALUES (?, ?)", ("doctor", "医生")
        )

        # staff members
        staff_rows = [
            # (staff_id, name, role, roles, active, phone, id_card, emp_no)
            (1, f"NAME_{SECRET_MARKER}_1", "医生", "医生", 1, f"PHONE_{SECRET_MARKER}_1", f"IDCARD_{SECRET_MARKER}_1", f"EMP_{SECRET_MARKER}_1"),
            (2, f"NAME_{SECRET_MARKER}_2", "护士", "护士,前台", 1, f"PHONE_{SECRET_MARKER}_2", f"IDCARD_{SECRET_MARKER}_2", f"EMP_{SECRET_MARKER}_2"),
            (3, f"NAME_{SECRET_MARKER}_3", f"UNKNOWNROLE_{SECRET_MARKER}", f"UNKNOWNROLE_{SECRET_MARKER}", 0, f"PHONE_{SECRET_MARKER}_3", f"IDCARD_{SECRET_MARKER}_3", f"EMP_{SECRET_MARKER}_3"),
            (4, f"NAME_{SECRET_MARKER}_4", "医生", "医生,, ,咨询师", 1, f"PHONE_{SECRET_MARKER}_4", f"IDCARD_{SECRET_MARKER}_4", f"EMP_{SECRET_MARKER}_4"),
        ]
        con.executemany(
            "INSERT INTO staff_members(staff_id, name, role, roles, active, phone, id_card, emp_no) VALUES (?,?,?,?,?,?,?,?)",
            staff_rows,
        )

        # users
        user_rows = [
            # (id, username, password_hash, role, is_active, staff_id)
            (1, f"USER_{SECRET_MARKER}_1", f"HASH_{SECRET_MARKER}_1", "admin", 1, 1),
            (2, f"USER_{SECRET_MARKER}_2", f"HASH_{SECRET_MARKER}_2", "doctor", 1, 2),
            (3, f"USER_{SECRET_MARKER}_2", f"HASH_{SECRET_MARKER}_3", "doctor", 1, 3),  # duplicate username
            (4, f"USER_{SECRET_MARKER}_4", f"HASH_{SECRET_MARKER}_4", f"UNKNOWNROLE_{SECRET_MARKER}", 1, None),  # independent, unknown role
            (5, f"USER_{SECRET_MARKER}_5", f"HASH_{SECRET_MARKER}_5", "doctor", 0, 1),  # duplicate staff link with user1, inactive user active staff
            (6, f"USER_{SECRET_MARKER}_6", f"HASH_{SECRET_MARKER}_6", "doctor", 1, 3),  # active user inactive staff(3), duplicate staff link with user3
            (7, f"USER_{SECRET_MARKER}_7", f"HASH_{SECRET_MARKER}_7", "doctor", 1, 999),  # orphan staff link
        ]
        con.executemany(
            "INSERT INTO users(id, username, password_hash, role, is_active, staff_id) VALUES (?,?,?,?,?,?)",
            user_rows,
        )

        # sessions - plaintext token structure (token column, no hash)
        con.executemany(
            "INSERT INTO sessions(token, user_id, created_at, expires_at) VALUES (?,?,?,?)",
            [
                (f"TOKEN_{SECRET_MARKER}_1", 1, "2026-01-01", "2026-01-02"),
                (f"TOKEN_{SECRET_MARKER}_2", 2, "2026-01-01", "2026-01-02"),
            ],
        )

        con.commit()
    finally:
        con.close()


def _build_partial_db(path):
    con = sqlite3.connect(path)
    try:
        con.executescript(
            """
            CREATE TABLE staff_members (
                staff_id INTEGER PRIMARY KEY,
                name TEXT
            );
            CREATE TABLE users (
                id INTEGER PRIMARY KEY,
                username TEXT
            );
            """
        )
        con.commit()
    finally:
        con.close()


class PersonnelPreflightTests(unittest.TestCase):
    def test_signature_has_no_defaults(self):
        sig = inspect.signature(pam.preflight)
        params = list(sig.parameters.values())
        self.assertEqual(len(params), 1)
        self.assertEqual(params[0].name, "source_db_path")
        self.assertIs(params[0].default, inspect.Parameter.empty)

    def test_full_synthetic_db_counts_and_conflicts(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "synthetic.sqlite3")
            _build_full_db(db_path)

            before_sha = _file_sha256(db_path)
            before_size = os.path.getsize(db_path)
            con = sqlite3.connect(db_path)
            before_schema = con.execute(
                "SELECT sql FROM sqlite_master ORDER BY name"
            ).fetchall()
            before_counts = {
                t: con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                for t in FIXED_TABLES
            }
            before_data_version = con.execute("PRAGMA data_version").fetchone()[0]
            con.close()
            before_files = set(os.listdir(tmpdir))

            report = pam.preflight(db_path)

            after_sha = _file_sha256(db_path)
            after_size = os.path.getsize(db_path)
            con = sqlite3.connect(db_path)
            after_schema = con.execute(
                "SELECT sql FROM sqlite_master ORDER BY name"
            ).fetchall()
            after_counts = {
                t: con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                for t in FIXED_TABLES
            }
            after_data_version = con.execute("PRAGMA data_version").fetchone()[0]
            con.close()
            after_files = set(os.listdir(tmpdir))

            self.assertEqual(before_sha, after_sha)
            self.assertEqual(before_size, after_size)
            self.assertEqual(before_schema, after_schema)
            self.assertEqual(before_counts, after_counts)
            self.assertEqual(before_data_version, after_data_version)
            self.assertEqual(before_files, after_files)
            for fname in after_files:
                self.assertNotRegex(fname, r"-(wal|shm|journal)$")

            self.assertEqual(report["version"], 1)
            for t in FIXED_TABLES:
                self.assertTrue(report["schema"]["tables_present"][t])
                self.assertEqual(report["schema"]["missing_columns"][t], [])

            counts = report["counts"]
            self.assertEqual(counts["staff_total"], 4)
            self.assertEqual(counts["staff_active"], 3)
            self.assertEqual(counts["staff_inactive"], 1)
            self.assertEqual(counts["users_total"], 7)
            self.assertEqual(counts["users_active"], 6)
            self.assertEqual(counts["users_inactive"], 1)
            self.assertEqual(counts["users_linked"], 6)
            self.assertEqual(counts["users_independent"], 1)
            self.assertEqual(counts["legacy_admin_role_users"], 1)
            self.assertEqual(counts["sessions_total"], 2)
            # staff rows with >1 non-empty roles token: staff 2 (护士,前台), staff 4 (医生,咨询师)
            self.assertEqual(counts["staff_multi_role_rows"], 2)

            conflicts = report["conflicts"]
            self.assertEqual(conflicts["duplicate_username_groups"], 1)
            self.assertEqual(conflicts["duplicate_username_rows"], 2)
            self.assertEqual(conflicts["duplicate_staff_link_groups"], 2)
            self.assertEqual(conflicts["duplicate_staff_link_rows"], 4)
            self.assertEqual(conflicts["orphan_staff_links"], 1)
            self.assertEqual(conflicts["active_user_inactive_staff"], 2)
            self.assertEqual(conflicts["inactive_user_active_staff"], 1)
            self.assertEqual(conflicts["unknown_user_roles"], 1)
            self.assertEqual(conflicts["unknown_staff_role_tokens"], 1)

            security = report["security"]
            self.assertFalse(security["users_has_is_system_admin"])
            self.assertFalse(security["users_has_account_kind"])
            self.assertTrue(security["sessions_has_plaintext_token"])
            self.assertFalse(security["sessions_has_token_hash"])
            self.assertFalse(security["audit_has_extended_fields"])

    def test_report_contains_no_secret_markers(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "synthetic.sqlite3")
            _build_full_db(db_path)
            report = pam.preflight(db_path)
            dumped = json.dumps(report, ensure_ascii=False)
            self.assertNotIn(SECRET_MARKER, dumped)

    def test_missing_table_and_columns_report_fixed_and_zero(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "partial.sqlite3")
            _build_partial_db(db_path)
            report = pam.preflight(db_path)

            self.assertTrue(report["schema"]["tables_present"]["staff_members"])
            self.assertTrue(report["schema"]["tables_present"]["users"])
            self.assertFalse(report["schema"]["tables_present"]["sessions"])
            self.assertFalse(report["schema"]["tables_present"]["roles"])
            self.assertFalse(report["schema"]["tables_present"]["role_permissions"])
            self.assertFalse(report["schema"]["tables_present"]["audit_logs"])

            self.assertEqual(
                sorted(report["schema"]["missing_columns"]["staff_members"]),
                sorted(["role", "roles", "active"]),
            )
            self.assertEqual(
                sorted(report["schema"]["missing_columns"]["users"]),
                sorted(["password_hash", "role", "is_active", "staff_id"]),
            )
            self.assertEqual(
                report["schema"]["missing_columns"]["sessions"],
                REQUIRED_COLUMNS["sessions"],
            )

            for key, value in report["counts"].items():
                self.assertEqual(value, 0, key)
            for key, value in report["conflicts"].items():
                self.assertEqual(value, 0, key)

            con = sqlite3.connect(db_path)
            tables = {
                r[0]
                for r in con.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            con.close()
            self.assertEqual(tables, {"staff_members", "users"})

    def test_rejects_nonexistent_directory_and_corrupt_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            missing_path = os.path.join(tmpdir, "does_not_exist.sqlite3")
            with self.assertRaises(pam.PreflightError):
                pam.preflight(missing_path)

            dir_path = os.path.join(tmpdir, "a_directory")
            os.mkdir(dir_path)
            with self.assertRaises(pam.PreflightError):
                pam.preflight(dir_path)

            corrupt_path = os.path.join(tmpdir, "corrupt.sqlite3")
            with open(corrupt_path, "wb") as f:
                f.write(b"not a real sqlite file" * 10)
            with self.assertRaises(pam.PreflightError):
                pam.preflight(corrupt_path)

            for bad_path in (missing_path, dir_path, corrupt_path):
                try:
                    pam.preflight(bad_path)
                except pam.PreflightError as exc:
                    msg = str(exc)
                    self.assertNotIn(bad_path, msg)
                    self.assertNotIn(tmpdir, msg)

    def test_rejects_default_db_path_and_symlink(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            fake_default = os.path.join(tmpdir, "default.sqlite3")
            _build_full_db(fake_default)

            with mock.patch.object(
                pam, "DEFAULT_DB_PATH", Path(fake_default).resolve()
            ):
                with self.assertRaises(pam.PreflightError):
                    pam.preflight(fake_default)

                symlink_path = os.path.join(tmpdir, "link.sqlite3")
                os.symlink(fake_default, symlink_path)
                with self.assertRaises(pam.PreflightError):
                    pam.preflight(symlink_path)

    def test_authorizer_denies_writes_allows_reads(self):
        allow = sqlite3.SQLITE_OK
        deny = sqlite3.SQLITE_DENY

        deny_actions = [
            sqlite3.SQLITE_INSERT,
            sqlite3.SQLITE_UPDATE,
            sqlite3.SQLITE_DELETE,
            sqlite3.SQLITE_CREATE_TABLE,
            sqlite3.SQLITE_DROP_TABLE,
            sqlite3.SQLITE_ALTER_TABLE,
            sqlite3.SQLITE_ATTACH,
            sqlite3.SQLITE_DETACH,
        ]
        for action in deny_actions:
            result = pam._readonly_authorizer(action, None, None, None, None)
            self.assertEqual(result, deny, action)

        allow_actions = [
            sqlite3.SQLITE_SELECT,
            sqlite3.SQLITE_READ,
            sqlite3.SQLITE_FUNCTION,
        ]
        for action in allow_actions:
            result = pam._readonly_authorizer(action, None, None, None, None)
            self.assertEqual(result, allow, action)

    def test_import_does_not_touch_sqlite3_connect(self):
        with mock.patch("sqlite3.connect") as mock_connect:
            import importlib

            importlib.reload(pam)
            mock_connect.assert_not_called()

    def test_report_shape_is_fixed_and_json_safe(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "synthetic.sqlite3")
            _build_full_db(db_path)
            report = pam.preflight(db_path)

            allowed_list_values = set()
            for cols in REQUIRED_COLUMNS.values():
                allowed_list_values.update(cols)

            def walk(value):
                if isinstance(value, dict):
                    for v in value.values():
                        walk(v)
                elif isinstance(value, list):
                    for v in value:
                        self.assertIsInstance(v, str)
                        self.assertIn(v, allowed_list_values)
                elif isinstance(value, bool):
                    pass
                elif isinstance(value, int):
                    pass
                elif isinstance(value, str):
                    pass
                else:
                    self.fail(f"unexpected type: {type(value)}")

            walk(report)

            self.assertEqual(
                set(report.keys()), {"version", "schema", "counts", "conflicts", "security"}
            )
            self.assertEqual(
                set(report["schema"].keys()), {"tables_present", "missing_columns"}
            )
            self.assertEqual(set(report["schema"]["tables_present"].keys()), set(FIXED_TABLES))
            self.assertEqual(set(report["schema"]["missing_columns"].keys()), set(FIXED_TABLES))
            self.assertEqual(
                set(report["counts"].keys()),
                {
                    "staff_total",
                    "staff_active",
                    "staff_inactive",
                    "users_total",
                    "users_active",
                    "users_inactive",
                    "users_linked",
                    "users_independent",
                    "legacy_admin_role_users",
                    "sessions_total",
                    "staff_multi_role_rows",
                },
            )
            self.assertEqual(
                set(report["conflicts"].keys()),
                {
                    "duplicate_username_groups",
                    "duplicate_username_rows",
                    "duplicate_staff_link_groups",
                    "duplicate_staff_link_rows",
                    "orphan_staff_links",
                    "active_user_inactive_staff",
                    "inactive_user_active_staff",
                    "unknown_user_roles",
                    "unknown_staff_role_tokens",
                },
            )
            self.assertEqual(
                set(report["security"].keys()),
                {
                    "users_has_is_system_admin",
                    "users_has_account_kind",
                    "sessions_has_plaintext_token",
                    "sessions_has_token_hash",
                    "audit_has_extended_fields",
                },
            )


class PersonnelPreflightHardeningTests(unittest.TestCase):
    """人员权限迁移回归测试。"""

    # --- percent-encoded file URIs for special-character paths ---

    def test_special_character_filenames_are_read_correctly_and_stay_readonly(self):
        special_names = [
            "edge?.sqlite3",
            "edge#.sqlite3",
            "edge%.sqlite3",
            "edge space.sqlite3",
            "edge中文.sqlite3",
        ]
        for name in special_names:
            with self.subTest(name=name):
                with tempfile.TemporaryDirectory() as tmpdir:
                    db_path = os.path.join(tmpdir, name)
                    _build_full_db(db_path)

                    before_sha = _file_sha256(db_path)
                    before_files = set(os.listdir(tmpdir))

                    report = pam.preflight(db_path)

                    after_sha = _file_sha256(db_path)
                    after_files = set(os.listdir(tmpdir))

                    # Must read the *correct* database, not silently open an
                    # empty/truncated file at a mangled path.
                    self.assertTrue(report["schema"]["tables_present"]["staff_members"])
                    self.assertEqual(report["counts"]["staff_total"], 4)
                    self.assertEqual(report["counts"]["users_total"], 7)

                    self.assertEqual(before_sha, after_sha)
                    self.assertEqual(before_files, after_files)
                    for fname in after_files:
                        self.assertNotRegex(fname, r"-(wal|shm|journal)$")

    # --- real empty-string staff_id linkage semantics ---

    def _build_text_staff_id_db(self, path):
        con = sqlite3.connect(path)
        try:
            con.executescript(
                """
                CREATE TABLE staff_members (
                    staff_id text primary key,
                    name text,
                    role text,
                    roles text,
                    active integer
                );
                CREATE TABLE users (
                    id text primary key,
                    username text,
                    password_hash text,
                    role text,
                    is_active integer,
                    staff_id text not null default ''
                );
                """
            )
            con.execute(
                "INSERT INTO staff_members VALUES (?,?,?,?,?)",
                (f"STAFFID_{SECRET_MARKER}_1", f"NAME_{SECRET_MARKER}_1", "医生", "医生", 1),
            )
            con.execute(
                "INSERT INTO staff_members VALUES (?,?,?,?,?)",
                (f"STAFFID_{SECRET_MARKER}_2", f"NAME_{SECRET_MARKER}_2", "护士", "护士", 0),
            )
            # Two independent accounts: one empty string, one whitespace-only.
            con.execute(
                "INSERT INTO users VALUES (?,?,?,?,?,?)",
                (f"U_{SECRET_MARKER}_1", f"USER_{SECRET_MARKER}_1", f"HASH_{SECRET_MARKER}_1", "doctor", 1, ""),
            )
            con.execute(
                "INSERT INTO users VALUES (?,?,?,?,?,?)",
                (f"U_{SECRET_MARKER}_2", f"USER_{SECRET_MARKER}_2", f"HASH_{SECRET_MARKER}_2", "doctor", 1, "   "),
            )
            # One genuinely linked account.
            con.execute(
                "INSERT INTO users VALUES (?,?,?,?,?,?)",
                (f"U_{SECRET_MARKER}_3", f"USER_{SECRET_MARKER}_3", f"HASH_{SECRET_MARKER}_3", "doctor", 0, f"STAFFID_{SECRET_MARKER}_1"),
            )
            con.commit()
        finally:
            con.close()

    def test_empty_string_staff_id_counts_as_independent_not_linked(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "text_staff_id.sqlite3")
            self._build_text_staff_id_db(db_path)

            report = pam.preflight(db_path)
            counts = report["counts"]
            conflicts = report["conflicts"]

            self.assertEqual(counts["users_total"], 3)
            self.assertEqual(counts["users_linked"], 1)
            self.assertEqual(counts["users_independent"], 2)
            self.assertEqual(conflicts["duplicate_staff_link_groups"], 0)
            self.assertEqual(conflicts["duplicate_staff_link_rows"], 0)
            self.assertEqual(conflicts["orphan_staff_links"], 0)
            self.assertEqual(conflicts["active_user_inactive_staff"], 0)
            # user3 is inactive while its linked staff (S1) is active.
            self.assertEqual(conflicts["inactive_user_active_staff"], 1)

            dumped = json.dumps(report, ensure_ascii=False)
            self.assertNotIn(SECRET_MARKER, dumped)

    def test_active_status_mapping_matches_sql_equals_one_semantics(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "active_map.sqlite3")
            con = sqlite3.connect(db_path)
            try:
                con.executescript(
                    """
                    CREATE TABLE staff_members (
                        staff_id text primary key,
                        name text,
                        role text,
                        roles text,
                        active integer
                    );
                    CREATE TABLE users (
                        id text primary key,
                        username text,
                        password_hash text,
                        role text,
                        is_active integer,
                        staff_id text not null default ''
                    );
                    """
                )
                # active stored as 2 (truthy under bool()) must NOT count as active.
                con.execute(
                    "INSERT INTO staff_members VALUES ('S1','n','医生','医生',2)"
                )
                con.execute(
                    "INSERT INTO users VALUES ('U1','u1','h','doctor',1,'S1')"
                )
                con.commit()
            finally:
                con.close()

            report = pam.preflight(db_path)
            self.assertEqual(report["counts"]["staff_active"], 0)
            self.assertEqual(report["counts"]["staff_inactive"], 1)
            self.assertEqual(report["conflicts"]["active_user_inactive_staff"], 1)

    # --- primary role column + token-column-agnostic session count ---

    def test_primary_role_column_is_checked_even_when_roles_column_exists(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "primary_role.sqlite3")
            con = sqlite3.connect(db_path)
            try:
                con.executescript(
                    """
                    CREATE TABLE staff_members (
                        staff_id text primary key,
                        name text,
                        role text,
                        roles text,
                        active integer
                    );
                    """
                )
                con.execute(
                    "INSERT INTO staff_members VALUES ('S1','n',?,'',1)",
                    (f"UNKNOWNROLE_{SECRET_MARKER}",),
                )
                con.commit()
            finally:
                con.close()

            report = pam.preflight(db_path)
            self.assertEqual(report["conflicts"]["unknown_staff_role_tokens"], 1)

    def test_duplicate_token_across_role_and_roles_columns_counts_once(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "dup_token.sqlite3")
            con = sqlite3.connect(db_path)
            try:
                con.executescript(
                    """
                    CREATE TABLE staff_members (
                        staff_id text primary key,
                        name text,
                        role text,
                        roles text,
                        active integer
                    );
                    """
                )
                con.execute(
                    "INSERT INTO staff_members VALUES ('S1','n',?,?,1)",
                    (f"UNKNOWNROLE_{SECRET_MARKER}", f"UNKNOWNROLE_{SECRET_MARKER}"),
                )
                con.commit()
            finally:
                con.close()

            report = pam.preflight(db_path)
            self.assertEqual(report["conflicts"]["unknown_staff_role_tokens"], 1)

    def test_sessions_total_counts_rows_regardless_of_token_column_shape(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "hashed_sessions.sqlite3")
            con = sqlite3.connect(db_path)
            try:
                con.execute(
                    "CREATE TABLE sessions (token_hash TEXT, user_id TEXT)"
                )
                con.execute(
                    "INSERT INTO sessions VALUES (?, ?)",
                    (f"HASH_{SECRET_MARKER}", f"U_{SECRET_MARKER}"),
                )
                con.commit()
            finally:
                con.close()

            report = pam.preflight(db_path)
            self.assertEqual(report["counts"]["sessions_total"], 1)
            self.assertFalse(report["security"]["sessions_has_plaintext_token"])
            self.assertTrue(report["security"]["sessions_has_token_hash"])

    # --- authorizer only allows the fixed read-only table_info pragma ---

    def test_authorizer_denies_assignment_style_pragmas(self):
        deny_cases = [
            (sqlite3.SQLITE_PRAGMA, "journal_mode", "WAL"),
            (sqlite3.SQLITE_PRAGMA, "user_version", "1"),
            (sqlite3.SQLITE_PRAGMA, "application_id", "1"),
            (sqlite3.SQLITE_PRAGMA, "writable_schema", "ON"),
        ]
        for action, arg1, arg2 in deny_cases:
            with self.subTest(pragma=arg1):
                result = pam._readonly_authorizer(action, arg1, arg2, None, None)
                self.assertEqual(result, sqlite3.SQLITE_DENY)

    def test_authorizer_allows_only_table_info_pragma(self):
        result = pam._readonly_authorizer(
            sqlite3.SQLITE_PRAGMA, "table_info", "staff_members", None, None
        )
        self.assertEqual(result, sqlite3.SQLITE_OK)

    def test_authorizer_still_allows_reads_after_pragma_hardening(self):
        allow_actions = [
            sqlite3.SQLITE_SELECT,
            sqlite3.SQLITE_READ,
            sqlite3.SQLITE_FUNCTION,
        ]
        for action in allow_actions:
            result = pam._readonly_authorizer(action, None, None, None, None)
            self.assertEqual(result, sqlite3.SQLITE_OK, action)


TARGET_SCHEMA_TABLES = (
    "staff_members",
    "staff_role_assignments",
    "users",
    "sessions",
    "role_data_scopes",
    "audit_logs",
)

# 局域网精简模式取消登录限速，这两张表不再属于目标模型，
# 只作为旧合成源可能残留、必须被清理的废表出现在测试里。
OBSOLETE_THROTTLE_TABLES = ("login_throttle_accounts", "login_throttle_sources")

SESSION_CLOCK_TAIL = """    created_at text not null,
    last_seen_at text not null,
    idle_expires_at text not null,
    reauthenticated_at text not null
);"""


def _schema_without_reauthenticated_at():
    sql_text = pam.load_personnel_access_schema_sql()
    assert sql_text.count(SESSION_CLOCK_TAIL) == 1
    return sql_text.replace(
        SESSION_CLOCK_TAIL,
        """    created_at text not null,
    last_seen_at text not null,
    idle_expires_at text not null
);""",
    )


def _schema_with_absolute_expires_at():
    sql_text = pam.load_personnel_access_schema_sql()
    assert sql_text.count(SESSION_CLOCK_TAIL) == 1
    return sql_text.replace(
        SESSION_CLOCK_TAIL,
        """    created_at text not null,
    last_seen_at text not null,
    idle_expires_at text not null,
    reauthenticated_at text not null,
    absolute_expires_at text not null
);""",
    )

TARGET_SCHEMA_COLUMNS = {
    "staff_members": {
        "staff_id", "name", "note", "job_no", "phone", "sex", "id_card",
        "title", "license_no", "department", "employment_status",
        "left_at", "left_reason", "created_at", "updated_at",
    },
    "staff_role_assignments": {"staff_id", "role_key", "is_primary", "created_at"},
    "users": {
        "id", "username", "display_name", "password_hash", "staff_id",
        "is_active", "is_system_admin", "account_kind",
        "created_at", "updated_at",
    },
    "sessions": {
        "session_id", "token_hash", "user_id", "device_name", "user_agent",
        "ip_address", "created_at", "last_seen_at", "idle_expires_at",
        "reauthenticated_at",
    },
    "role_data_scopes": {"role_key", "scope_key", "scope_value"},
    "audit_logs": {
        "audit_id", "entity_type", "entity_id", "action", "old_json",
        "new_json", "operator", "created_at", "actor_user_id", "result",
        "reason", "risk_level", "request_id", "ip_address", "user_agent",
    },
}


def _table_columns(con, table):
    return {row[1] for row in con.execute(f"PRAGMA table_info({table})").fetchall()}


def _build_target_schema_db():
    con = sqlite3.connect(":memory:")
    con.execute("PRAGMA foreign_keys = ON")
    con.executescript(
        """
        CREATE TABLE roles (
            role_key text primary key,
            name text not null,
            is_system integer not null default 0,
            sort integer not null default 0,
            created_at text,
            updated_at text
        );
        """
    )
    con.execute("INSERT INTO roles(role_key, name) VALUES ('doctor', '医生')")
    con.execute("INSERT INTO roles(role_key, name) VALUES ('nurse', '护士')")
    con.executescript(pam.load_personnel_access_schema_sql())
    return con


class PersonnelAccessTargetSchemaTests(unittest.TestCase):
    def test_schema_sql_has_no_pragma_transaction_or_if_not_exists(self):
        sql_text = pam.load_personnel_access_schema_sql()
        lowered = sql_text.lower()
        self.assertNotIn("pragma", lowered)
        self.assertNotIn("begin", lowered)
        self.assertNotIn("commit", lowered)
        self.assertNotIn("if not exists", lowered)

    def test_loading_schema_sql_does_not_touch_sqlite3_connect(self):
        with mock.patch("sqlite3.connect") as mock_connect:
            pam.load_personnel_access_schema_sql()
            mock_connect.assert_not_called()

    def test_target_schema_creates_all_tables_with_exact_columns(self):
        con = _build_target_schema_db()
        try:
            for table in TARGET_SCHEMA_TABLES:
                with self.subTest(table=table):
                    self.assertEqual(
                        _table_columns(con, table), TARGET_SCHEMA_COLUMNS[table]
                    )
        finally:
            con.close()

    def test_staff_members_drops_legacy_fields_and_rejects_bad_status(self):
        con = _build_target_schema_db()
        try:
            cols = _table_columns(con, "staff_members")
            self.assertNotIn("role", cols)
            self.assertNotIn("roles", cols)
            self.assertNotIn("active", cols)

            con.execute(
                "INSERT INTO staff_members(staff_id, name, employment_status) "
                "VALUES ('S1', 'n', 'employed')"
            )
            with self.assertRaises(sqlite3.IntegrityError):
                con.execute(
                    "INSERT INTO staff_members(staff_id, name, employment_status) "
                    "VALUES ('S2', 'n', 'retired')"
                )
        finally:
            con.close()

    def test_staff_role_assignments_composite_key_and_single_primary(self):
        con = _build_target_schema_db()
        try:
            con.execute(
                "INSERT INTO staff_members(staff_id, name) VALUES ('S1', 'n')"
            )
            con.execute(
                "INSERT INTO staff_role_assignments(staff_id, role_key, is_primary) "
                "VALUES ('S1', 'doctor', 1)"
            )
            with self.assertRaises(sqlite3.IntegrityError):
                con.execute(
                    "INSERT INTO staff_role_assignments(staff_id, role_key, is_primary) "
                    "VALUES ('S1', 'doctor', 0)"
                )
            with self.assertRaises(sqlite3.IntegrityError):
                con.execute(
                    "INSERT INTO staff_role_assignments(staff_id, role_key, is_primary) "
                    "VALUES ('S1', 'nurse', 1)"
                )
            con.execute(
                "INSERT INTO staff_role_assignments(staff_id, role_key, is_primary) "
                "VALUES ('S1', 'nurse', 0)"
            )
        finally:
            con.close()

    def test_users_nullable_staff_id_and_constraints(self):
        con = _build_target_schema_db()
        try:
            cols = _table_columns(con, "users")
            self.assertNotIn("role", cols)

            con.execute(
                "INSERT INTO users(id, username, password_hash, staff_id, account_kind, is_system_admin) "
                "VALUES ('U1', 'u1', 'h', NULL, 'independent_admin', 1)"
            )
            con.execute(
                "INSERT INTO users(id, username, password_hash, staff_id, account_kind, is_system_admin) "
                "VALUES ('U2', 'u2', 'h', NULL, 'break_glass', 1)"
            )

            con.execute("INSERT INTO staff_members(staff_id, name) VALUES ('S1', 'n')")
            con.execute(
                "INSERT INTO users(id, username, password_hash, staff_id) "
                "VALUES ('U3', 'u3', 'h', 'S1')"
            )
            with self.assertRaises(sqlite3.IntegrityError):
                con.execute(
                    "INSERT INTO users(id, username, password_hash, staff_id) "
                    "VALUES ('U4', 'u4', 'h', 'S1')"
                )

            with self.assertRaises(sqlite3.IntegrityError):
                con.execute(
                    "INSERT INTO users(id, username, password_hash, account_kind) "
                    "VALUES ('U5', 'u5', 'h', 'ghost')"
                )
        finally:
            con.close()

    def test_sessions_token_hash_unique_and_no_token_column(self):
        con = _build_target_schema_db()
        try:
            cols = _table_columns(con, "sessions")
            self.assertNotIn("token", cols)

            con.execute("INSERT INTO staff_members(staff_id, name) VALUES ('S1', 'n')")
            con.execute(
                "INSERT INTO users(id, username, password_hash, staff_id) "
                "VALUES ('U1', 'u1', 'h', 'S1')"
            )
            con.execute(
                "INSERT INTO sessions("
                "session_id, token_hash, user_id, created_at, last_seen_at, "
                "idle_expires_at, reauthenticated_at"
                ") VALUES ("
                "'SESS1', 'HASH1', 'U1', "
                "'2026-01-01T00:00:00.000000Z', "
                "'2026-01-01T00:01:00.000000Z', "
                "'2026-01-01T00:31:00.000000Z', "
                "'2026-01-01T00:00:30.000000Z')"
            )
            with self.assertRaises(sqlite3.IntegrityError):
                con.execute(
                    "INSERT INTO sessions("
                    "session_id, token_hash, user_id, created_at, last_seen_at, "
                    "idle_expires_at, reauthenticated_at"
                    ") VALUES ("
                    "'SESS2', 'HASH1', 'U1', "
                    "'2026-01-01T00:00:00.000000Z', "
                    "'2026-01-01T00:01:00.000000Z', "
                    "'2026-01-01T00:31:00.000000Z', "
                    "'2026-01-01T00:00:30.000000Z')"
                )
        finally:
            con.close()

    def test_sessions_activity_and_reauth_columns_are_required(self):
        con = _build_target_schema_db()
        try:
            columns = {
                row[1]: row
                for row in con.execute("PRAGMA table_info(sessions)").fetchall()
            }
            for column in (
                "created_at",
                "last_seen_at",
                "idle_expires_at",
                "reauthenticated_at",
            ):
                with self.subTest(column=column):
                    self.assertIn(column, columns)
                    self.assertEqual(columns[column][3], 1)
                    self.assertIsNone(columns[column][4])
            self.assertNotIn("absolute_expires_at", columns)
        finally:
            con.close()

    def test_target_schema_creates_no_login_throttle_tables(self):
        con = _build_target_schema_db()
        try:
            existing = {
                row[0]
                for row in con.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            for table in OBSOLETE_THROTTLE_TABLES:
                self.assertNotIn(table, existing)
        finally:
            con.close()

    def test_users_has_no_password_policy_columns_but_keeps_nonblank_hash(self):
        con = _build_target_schema_db()
        try:
            cols = _table_columns(con, "users")
            self.assertNotIn("must_change_password", cols)
            self.assertNotIn("password_changed_at", cols)

            con.execute("INSERT INTO staff_members(staff_id, name) VALUES ('S1', 'n')")
            for bad_hash in (None, "", "   "):
                with self.subTest(password_hash=repr(bad_hash)):
                    with self.assertRaises(sqlite3.IntegrityError):
                        con.execute(
                            "INSERT INTO users(id, username, password_hash, staff_id) "
                            "VALUES ('U1', 'u1', ?, 'S1')",
                            (bad_hash,),
                        )
        finally:
            con.close()

    def test_role_data_scopes_composite_unique(self):
        con = _build_target_schema_db()
        try:
            con.execute(
                "INSERT INTO role_data_scopes(role_key, scope_key, scope_value) "
                "VALUES ('doctor', 'dept', 'ortho')"
            )
            with self.assertRaises(sqlite3.IntegrityError):
                con.execute(
                    "INSERT INTO role_data_scopes(role_key, scope_key, scope_value) "
                    "VALUES ('doctor', 'dept', 'other')"
                )
        finally:
            con.close()

    def test_audit_logs_result_values_and_extended_columns(self):
        con = _build_target_schema_db()
        try:
            for value in ("success", "denied", "failed", None):
                con.execute(
                    "INSERT INTO audit_logs(entity_type, entity_id, action, result) "
                    "VALUES ('staff', 'S1', 'update', ?)",
                    (value,),
                )
            with self.assertRaises(sqlite3.IntegrityError):
                con.execute(
                    "INSERT INTO audit_logs(entity_type, entity_id, action, result) "
                    "VALUES ('staff', 'S1', 'update', 'weird')"
                )

            fk_rows = con.execute("PRAGMA foreign_key_list(audit_logs)").fetchall()
            self.assertEqual(fk_rows, [])
        finally:
            con.close()

    def test_users_account_kind_staff_id_invariant(self):
        con = _build_target_schema_db()
        try:
            with self.assertRaises(sqlite3.IntegrityError):
                con.execute(
                    "INSERT INTO users(id, username, password_hash, staff_id, account_kind) "
                    "VALUES ('U1', 'u1', 'h', NULL, 'staff')"
                )

            con.execute("INSERT INTO staff_members(staff_id, name) VALUES ('S1', 'n')")
            with self.assertRaises(sqlite3.IntegrityError):
                con.execute(
                    "INSERT INTO users(id, username, password_hash, staff_id, account_kind, is_system_admin) "
                    "VALUES ('U2', 'u2', 'h', 'S1', 'independent_admin', 1)"
                )
            with self.assertRaises(sqlite3.IntegrityError):
                con.execute(
                    "INSERT INTO users(id, username, password_hash, staff_id, account_kind, is_system_admin) "
                    "VALUES ('U3', 'u3', 'h', 'S1', 'break_glass', 1)"
                )
            with self.assertRaises(sqlite3.IntegrityError):
                con.execute(
                    "INSERT INTO users(id, username, password_hash, staff_id, account_kind, is_system_admin) "
                    "VALUES ('U4', 'u4', 'h', NULL, 'independent_admin', 0)"
                )
            with self.assertRaises(sqlite3.IntegrityError):
                con.execute(
                    "INSERT INTO users(id, username, password_hash, staff_id, account_kind, is_system_admin) "
                    "VALUES ('U5', 'u5', 'h', NULL, 'break_glass', 0)"
                )

            con.execute(
                "INSERT INTO users(id, username, password_hash, staff_id, account_kind, is_system_admin) "
                "VALUES ('U6', 'u6', 'h', NULL, 'independent_admin', 1)"
            )
            con.execute(
                "INSERT INTO users(id, username, password_hash, staff_id, account_kind, is_system_admin) "
                "VALUES ('U7', 'u7', 'h', NULL, 'break_glass', 1)"
            )

            con.execute(
                "INSERT INTO users(id, username, password_hash, staff_id, account_kind, is_system_admin) "
                "VALUES ('U8', 'u8', 'h', 'S1', 'staff', 1)"
            )
            self.assertEqual(
                con.execute("SELECT COUNT(*) FROM users").fetchone()[0], 3
            )
        finally:
            con.close()

    def test_users_boolean_columns_reject_non_bool_values(self):
        con = _build_target_schema_db()
        try:
            con.execute("INSERT INTO staff_members(staff_id, name) VALUES ('S1', 'n')")
            con.execute("INSERT INTO staff_members(staff_id, name) VALUES ('S2', 'n')")

            with self.assertRaises(sqlite3.IntegrityError):
                con.execute(
                    "INSERT INTO users(id, username, password_hash, staff_id, account_kind, is_active) "
                    "VALUES ('U1', 'u1', 'h', 'S1', 'staff', 2)"
                )
            with self.assertRaises(sqlite3.IntegrityError):
                con.execute(
                    "INSERT INTO users(id, username, password_hash, staff_id, account_kind, is_system_admin) "
                    "VALUES ('U2', 'u2', 'h', 'S2', 'staff', 2)"
                )
        finally:
            con.close()

    def test_target_schema_index_columns(self):
        con = _build_target_schema_db()
        try:
            expected = {
                "sessions": {"idx_sessions_user": ["user_id"]},
                "audit_logs": {
                    "idx_audit_logs_entity": ["entity_type", "entity_id"],
                    "idx_audit_logs_created": ["created_at"],
                },
                "staff_members": {
                    "idx_staff_members_employment_status": ["employment_status"],
                },
                "staff_role_assignments": {
                    "idx_staff_role_assignments_role": ["role_key"],
                },
            }
            for table, indexes in expected.items():
                index_names = {
                    row[1] for row in con.execute(f"PRAGMA index_list({table})").fetchall()
                }
                for index_name, cols in indexes.items():
                    self.assertIn(index_name, index_names)
                    index_info = con.execute(
                        f"PRAGMA index_info({index_name})"
                    ).fetchall()
                    actual_cols = [row[2] for row in sorted(index_info, key=lambda r: r[0])]
                    self.assertEqual(actual_cols, cols)
        finally:
            con.close()

    def test_staff_role_assignments_two_non_primary_roles_for_same_staff(self):
        con = _build_target_schema_db()
        try:
            con.execute("INSERT INTO roles(role_key, name) VALUES ('consultant', '咨询师')")
            con.execute("INSERT INTO staff_members(staff_id, name) VALUES ('S1', 'n')")
            con.execute(
                "INSERT INTO staff_role_assignments(staff_id, role_key, is_primary) "
                "VALUES ('S1', 'nurse', 0)"
            )
            con.execute(
                "INSERT INTO staff_role_assignments(staff_id, role_key, is_primary) "
                "VALUES ('S1', 'consultant', 0)"
            )
            rows = con.execute(
                "SELECT role_key FROM staff_role_assignments "
                "WHERE staff_id = 'S1' AND is_primary = 0 ORDER BY role_key"
            ).fetchall()
            self.assertEqual([r[0] for r in rows], ["consultant", "nurse"])
        finally:
            con.close()

    def test_sessions_cascade_delete_on_user_removal(self):
        con = _build_target_schema_db()
        try:
            con.execute(
                "INSERT INTO users(id, username, password_hash, staff_id, account_kind, is_system_admin) "
                "VALUES ('U1', 'u1', 'h', NULL, 'independent_admin', 1)"
            )
            con.execute(
                "INSERT INTO sessions("
                "session_id, token_hash, user_id, created_at, last_seen_at, "
                "idle_expires_at, reauthenticated_at"
                ") VALUES ("
                "'SESS1', 'HASH1', 'U1', "
                "'2026-01-01T00:00:00.000000Z', "
                "'2026-01-01T00:01:00.000000Z', "
                "'2026-01-01T00:31:00.000000Z', "
                "'2026-01-01T00:00:30.000000Z')"
            )
            con.execute("DELETE FROM users WHERE id = 'U1'")
            self.assertEqual(
                con.execute("SELECT COUNT(*) FROM sessions").fetchone()[0], 0
            )
        finally:
            con.close()

    def test_staff_and_role_deletion_restricted_when_referenced(self):
        con = _build_target_schema_db()
        try:
            con.execute("INSERT INTO staff_members(staff_id, name) VALUES ('S1', 'n')")
            con.execute(
                "INSERT INTO staff_role_assignments(staff_id, role_key, is_primary) "
                "VALUES ('S1', 'doctor', 1)"
            )
            with self.assertRaises(sqlite3.IntegrityError):
                con.execute("DELETE FROM staff_members WHERE staff_id = 'S1'")

            con.execute(
                "INSERT INTO role_data_scopes(role_key, scope_key, scope_value) "
                "VALUES ('nurse', 'dept', 'ortho')"
            )
            with self.assertRaises(sqlite3.IntegrityError):
                con.execute("DELETE FROM roles WHERE role_key = 'nurse'")

            con.execute(
                "INSERT INTO users(id, username, password_hash, staff_id) "
                "VALUES ('U1', 'u1', 'h', 'S1')"
            )
            with self.assertRaises(sqlite3.IntegrityError):
                con.execute("DELETE FROM staff_members WHERE staff_id = 'S1'")
        finally:
            con.close()

    def test_schema_metadata_report_excludes_secret_marker(self):
        con = _build_target_schema_db()
        try:
            con.execute("INSERT INTO staff_members(staff_id, name) VALUES (?, ?)",
                        (f"STAFF_{SECRET_MARKER}", f"NAME_{SECRET_MARKER}"))
            con.execute(
                "INSERT INTO users(id, username, password_hash, staff_id) VALUES (?, ?, 'h', ?)",
                (f"USER_{SECRET_MARKER}", f"UNAME_{SECRET_MARKER}", f"STAFF_{SECRET_MARKER}"),
            )
            con.execute(
                "INSERT INTO sessions("
                "session_id, token_hash, user_id, created_at, last_seen_at, "
                "idle_expires_at, reauthenticated_at"
                ") VALUES ("
                "?, ?, ?, '2026-01-01T00:00:00.000000Z', "
                "'2026-01-01T00:01:00.000000Z', "
                "'2026-01-01T00:31:00.000000Z', "
                "'2026-01-01T00:00:30.000000Z')",
                (f"SESS_{SECRET_MARKER}", f"HASH_{SECRET_MARKER}", f"USER_{SECRET_MARKER}"),
            )

            metadata = []
            for table in TARGET_SCHEMA_TABLES:
                metadata.append(sorted(_table_columns(con, table)))
                metadata.extend(
                    row[1] for row in con.execute(f"PRAGMA index_list({table})").fetchall()
                )
            dumped = json.dumps(metadata, ensure_ascii=False)
            self.assertNotIn(SECRET_MARKER, dumped)
        finally:
            con.close()

    NOT_NULL_REJECTION_CASES = (
        ("staff_members", "staff_id", None),
        ("staff_members", "staff_id", ""),
        ("staff_members", "staff_id", "   "),
        ("users", "id", None),
        ("users", "id", ""),
        ("users", "id", "   "),
        ("users", "username", None),
        ("users", "username", ""),
        ("users", "username", "   "),
        ("users", "password_hash", None),
        ("users", "password_hash", ""),
        ("users", "password_hash", "   "),
        ("sessions", "session_id", None),
        ("sessions", "session_id", ""),
        ("sessions", "session_id", "   "),
        ("sessions", "token_hash", None),
        ("sessions", "token_hash", ""),
        ("sessions", "token_hash", "   "),
        ("sessions", "user_id", None),
        ("sessions", "user_id", ""),
        ("sessions", "user_id", "   "),
    )

    ROW_TEMPLATES = {
        "staff_members": {"staff_id": "S1", "name": "张三"},
        "users": {
            "id": "U1", "username": "u1", "password_hash": "h1", "staff_id": "S1",
        },
        "sessions": {
            "session_id": "SE1", "token_hash": "T1", "user_id": "U1",
            "created_at": "2026-01-01T00:00:00.000000Z",
            "last_seen_at": "2026-01-01T00:01:00.000000Z",
            "idle_expires_at": "2026-01-01T00:31:00.000000Z",
            "reauthenticated_at": "2026-01-01T00:00:30.000000Z",
        },
    }

    def _insert(self, con, table, row):
        cols = list(row.keys())
        placeholders = ",".join("?" for _ in cols)
        con.execute(
            f"INSERT INTO {table}({','.join(cols)}) VALUES ({placeholders})",
            [row[c] for c in cols],
        )

    def test_key_columns_reject_null_empty_and_blank(self):
        for table, column, bad_value in self.NOT_NULL_REJECTION_CASES:
            with self.subTest(table=table, column=column, bad_value=repr(bad_value)):
                con = _build_target_schema_db()
                try:
                    if table == "users":
                        self._insert(con, "staff_members", self.ROW_TEMPLATES["staff_members"])
                    if table == "sessions":
                        self._insert(con, "staff_members", self.ROW_TEMPLATES["staff_members"])
                        self._insert(con, "users", self.ROW_TEMPLATES["users"])
                    row = dict(self.ROW_TEMPLATES[table])
                    row[column] = bad_value
                    with self.assertRaises(sqlite3.IntegrityError):
                        self._insert(con, table, row)
                finally:
                    con.close()

    def test_users_staff_id_null_or_blank_only_valid_for_independent_admin(self):
        con = _build_target_schema_db()
        try:
            con.execute(
                "INSERT INTO users(id, username, password_hash, staff_id, "
                "is_system_admin, account_kind) VALUES "
                "('ADMIN1', 'admin1', 'h', NULL, 1, 'independent_admin')"
            )
            self.assertEqual(
                con.execute("SELECT staff_id FROM users WHERE id='ADMIN1'").fetchone()[0],
                None,
            )
            with self.assertRaises(sqlite3.IntegrityError):
                con.execute(
                    "INSERT INTO users(id, username, password_hash, staff_id, "
                    "is_system_admin, account_kind) VALUES "
                    "('ADMIN2', 'admin2', 'h', '   ', 1, 'independent_admin')"
                )
        finally:
            con.close()


def _valid_decisions():
    return {
        "regular_system_admin_user_ids": [1, "U2"],
        "break_glass_account": {
            "id": "BG1",
            "username": f"bg_{SECRET_MARKER}",
            "display_name": f"应急_{SECRET_MARKER}",
            "password_hash": f"HASH_{SECRET_MARKER}",
        },
        "staff_role_map": {f"legacy_{SECRET_MARKER}": "doctor"},
        "acknowledged_legacy_user_role_user_ids": [3],
        "status_conflict_resolutions": {1: "disable_user", "U2": "keep_disabled"},
    }


class MigrationDecisionEnvelopeTests(unittest.TestCase):
    def test_valid_envelope_returns_new_normalized_dict(self):
        decisions = _valid_decisions()
        result = pam._validate_decision_envelope(decisions)
        self.assertEqual(result, decisions)
        self.assertIsNot(result, decisions)
        self.assertIsNot(
            result["break_glass_account"], decisions["break_glass_account"]
        )
        self.assertIsNot(result["staff_role_map"], decisions["staff_role_map"])

    def _assert_rejects(self, decisions, expected_code):
        with self.assertRaises(pam.MigrationDecisionError) as ctx:
            pam._validate_decision_envelope(decisions)
        exc = ctx.exception
        self.assertEqual(exc.code, expected_code)
        self.assertNotIn(SECRET_MARKER, str(exc))
        self.assertNotIn(SECRET_MARKER, repr(exc))

    def test_top_level_shape_rejections(self):
        cases = [
            ("not a dict", []),
            ("missing key", {k: v for k, v in _valid_decisions().items() if k != "break_glass_account"}),
            ("extra key", {**_valid_decisions(), f"extra_{SECRET_MARKER}": 1}),
        ]
        for label, decisions in cases:
            with self.subTest(label=label):
                self._assert_rejects(decisions, "decisions_shape")

    def test_admin_ids_rejections(self):
        cases = [
            "not a list",
            [],
            [True],
            [""],
            ["   "],
            [1, 1],
            [1.5],
        ]
        for bad in cases:
            with self.subTest(bad=repr(bad)):
                decisions = _valid_decisions()
                decisions["regular_system_admin_user_ids"] = bad
                self._assert_rejects(decisions, "admin_ids")

    def test_break_glass_rejections(self):
        base = _valid_decisions()["break_glass_account"]
        cases = [
            ("not dict", []),
            ("missing key", {k: v for k, v in base.items() if k != "id"}),
            ("extra key", {**base, f"password_{SECRET_MARKER}": "plaintext"}),
            ("blank value", {**base, "username": "   "}),
            ("non str value", {**base, "id": 1}),
        ]
        for label, bad in cases:
            with self.subTest(label=label):
                decisions = _valid_decisions()
                decisions["break_glass_account"] = bad
                self._assert_rejects(decisions, "break_glass")

    def test_staff_role_map_rejections(self):
        cases = [
            "not a dict",
            {f"key_{SECRET_MARKER}": ""},
            {f"key_{SECRET_MARKER}": "   "},
            {"": "doctor"},
            {"   ": "doctor"},
        ]
        for bad in cases:
            with self.subTest(bad=repr(bad)):
                decisions = _valid_decisions()
                decisions["staff_role_map"] = bad
                self._assert_rejects(decisions, "staff_role_map")

    def test_staff_role_map_allows_empty_dict(self):
        decisions = _valid_decisions()
        decisions["staff_role_map"] = {}
        result = pam._validate_decision_envelope(decisions)
        self.assertEqual(result["staff_role_map"], {})

    def test_acknowledged_user_roles_rejections(self):
        cases = [
            "not a list",
            [True],
            [""],
            [1, 1],
        ]
        for bad in cases:
            with self.subTest(bad=repr(bad)):
                decisions = _valid_decisions()
                decisions["acknowledged_legacy_user_role_user_ids"] = bad
                self._assert_rejects(decisions, "acknowledged_user_roles")

    def test_acknowledged_user_roles_allows_empty_list(self):
        decisions = _valid_decisions()
        decisions["acknowledged_legacy_user_role_user_ids"] = []
        result = pam._validate_decision_envelope(decisions)
        self.assertEqual(result["acknowledged_legacy_user_role_user_ids"], [])

    def test_status_conflict_resolutions_rejections(self):
        cases = [
            "not a dict",
            {1: f"unknown_{SECRET_MARKER}"},
            {True: "disable_user"},
            {"": "disable_user"},
        ]
        for bad in cases:
            with self.subTest(bad=repr(bad)):
                decisions = _valid_decisions()
                decisions["status_conflict_resolutions"] = bad
                self._assert_rejects(decisions, "status_resolutions")

    def test_status_conflict_resolutions_allows_empty_dict(self):
        decisions = _valid_decisions()
        decisions["status_conflict_resolutions"] = {}
        result = pam._validate_decision_envelope(decisions)
        self.assertEqual(result["status_conflict_resolutions"], {})


MIGRATION_REQUIRED_COLUMNS = {
    "staff_members": [
        "staff_id", "name", "note", "job_no", "phone", "sex", "id_card",
        "title", "license_no", "department", "role", "roles", "active",
        "created_at", "updated_at",
    ],
    "users": [
        "id", "username", "display_name", "password_hash", "role",
        "is_active", "staff_id", "created_at", "updated_at",
    ],
    "sessions": ["token", "user_id", "created_at", "expires_at"],
    "roles": ["role_key", "name", "is_system", "sort", "created_at", "updated_at"],
    "role_permissions": ["role_key", "perm_key"],
    "audit_logs": [
        "audit_id", "entity_type", "entity_id", "action", "old_json",
        "new_json", "operator", "created_at",
    ],
}


def _build_migration_source_db(path, extra_table=None, extra_columns=None):
    con = sqlite3.connect(path)
    try:
        con.executescript(
            """
            CREATE TABLE staff_members (
                staff_id INTEGER PRIMARY KEY,
                name TEXT, note TEXT, job_no TEXT, phone TEXT, sex TEXT,
                id_card TEXT, title TEXT, license_no TEXT, department TEXT,
                role TEXT, roles TEXT, active INTEGER,
                created_at TEXT, updated_at TEXT
            );
            CREATE TABLE users (
                id INTEGER PRIMARY KEY,
                username TEXT, display_name TEXT, password_hash TEXT,
                role TEXT, is_active INTEGER, staff_id INTEGER,
                created_at TEXT, updated_at TEXT
            );
            CREATE TABLE sessions (
                token TEXT, user_id INTEGER, created_at TEXT, expires_at TEXT
            );
            CREATE TABLE roles (
                role_key TEXT PRIMARY KEY, name TEXT, is_system INTEGER,
                sort INTEGER, created_at TEXT, updated_at TEXT
            );
            CREATE TABLE role_permissions (
                role_key TEXT, perm_key TEXT
            );
            CREATE TABLE audit_logs (
                audit_id INTEGER PRIMARY KEY, entity_type TEXT, entity_id TEXT,
                action TEXT, old_json TEXT, new_json TEXT, operator TEXT,
                created_at TEXT
            );
            """
        )
        con.execute(
            "INSERT INTO roles(role_key, name, is_system, sort, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?)",
            ("admin", f"ROLE_{SECRET_MARKER}_admin", 1, 0, "2026-01-01", "2026-01-01"),
        )
        con.execute(
            "INSERT INTO staff_members(staff_id, name, note, job_no, phone, sex, id_card, "
            "title, license_no, department, role, roles, active, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                1, f"NAME_{SECRET_MARKER}", f"NOTE_{SECRET_MARKER}", f"JOB_{SECRET_MARKER}",
                f"PHONE_{SECRET_MARKER}", f"SEX_{SECRET_MARKER}", f"ID_{SECRET_MARKER}",
                f"TITLE_{SECRET_MARKER}", f"LIC_{SECRET_MARKER}", f"DEPT_{SECRET_MARKER}",
                "admin", "admin", 1, "2026-01-01", "2026-01-01",
            ),
        )
        con.execute(
            "INSERT INTO users(id, username, display_name, password_hash, role, is_active, "
            "staff_id, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (
                1, f"USER_{SECRET_MARKER}", f"DISP_{SECRET_MARKER}", f"HASH_{SECRET_MARKER}",
                "admin", 1, 1, "2026-01-01", "2026-01-01",
            ),
        )
        con.execute(
            "INSERT INTO sessions(token, user_id, created_at, expires_at) VALUES (?,?,?,?)",
            (f"TOKEN_{SECRET_MARKER}", 1, "2026-01-01", "2026-01-02"),
        )
        con.execute(
            "INSERT INTO audit_logs(entity_type, entity_id, action, old_json, new_json, "
            "operator, created_at) VALUES (?,?,?,?,?,?,?)",
            (
                f"ENTITY_{SECRET_MARKER}", "1", "update", f"OLD_{SECRET_MARKER}",
                f"NEW_{SECRET_MARKER}", f"OP_{SECRET_MARKER}", "2026-01-01",
            ),
        )
        if extra_table:
            con.execute(f"CREATE TABLE {extra_table} (id INTEGER PRIMARY KEY)")
        if extra_columns:
            table, cols = extra_columns
            for col in cols:
                con.execute(f"ALTER TABLE {table} ADD COLUMN {col} TEXT")
        con.commit()
    finally:
        con.close()


class LegacySourceSnapshotTests(unittest.TestCase):
    def test_success_returns_fixed_shape_without_secrets_or_paths(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "legacy.sqlite3")
            _build_migration_source_db(db_path)
            before_hash = _file_sha256(db_path)
            before_names = set(os.listdir(tmpdir))

            snapshot = pam._load_legacy_source_snapshot(db_path)

            self.assertEqual(
                set(snapshot.keys()),
                {
                    "staff_rows", "user_rows", "roles",
                    "sessions_to_invalidate", "audit_rows_preserved",
                },
            )
            self.assertEqual(len(snapshot["staff_rows"]), 1)
            self.assertEqual(
                len(snapshot["staff_rows"][0]),
                len(MIGRATION_REQUIRED_COLUMNS["staff_members"]),
            )
            self.assertEqual(len(snapshot["user_rows"]), 1)
            self.assertEqual(
                len(snapshot["user_rows"][0]),
                len(MIGRATION_REQUIRED_COLUMNS["users"]),
            )
            self.assertEqual(snapshot["roles"], [("admin", f"ROLE_{SECRET_MARKER}_admin")])
            self.assertEqual(snapshot["sessions_to_invalidate"], 1)
            self.assertEqual(snapshot["audit_rows_preserved"], 1)

            dumped = json.dumps(snapshot, default=str)
            self.assertNotIn("token", dumped.lower().replace("token_hash", ""))
            self.assertNotIn(tmpdir, dumped)
            self.assertNotIn(db_path, dumped)

            self.assertEqual(_file_sha256(db_path), before_hash)
            self.assertEqual(set(os.listdir(tmpdir)), before_names)

    def test_missing_table_raises_source_schema_without_leaking(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "legacy.sqlite3")
            _build_partial_db(db_path)
            with self.assertRaises(pam.MigrationDecisionError) as ctx:
                pam._load_legacy_source_snapshot(db_path)
            self.assertEqual(ctx.exception.code, "source_schema")
            self.assertNotIn(tmpdir, str(ctx.exception))
            self.assertNotIn(db_path, str(ctx.exception))
            self.assertNotIn(tmpdir, repr(ctx.exception))

    def test_missing_required_column_raises_source_schema(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "legacy.sqlite3")
            _build_migration_source_db(db_path)
            con = sqlite3.connect(db_path)
            con.execute("DROP TABLE role_permissions")
            con.execute("CREATE TABLE role_permissions (role_key TEXT)")
            con.commit()
            con.close()
            with self.assertRaises(pam.MigrationDecisionError) as ctx:
                pam._load_legacy_source_snapshot(db_path)
            self.assertEqual(ctx.exception.code, "source_schema")

    def test_extra_business_column_is_allowed(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "legacy.sqlite3")
            _build_migration_source_db(
                db_path, extra_columns=("staff_members", [f"extra_{SECRET_MARKER}"])
            )
            snapshot = pam._load_legacy_source_snapshot(db_path)
            self.assertEqual(len(snapshot["staff_rows"]), 1)

    def test_target_extra_table_raises_source_already_target(self):
        for extra_table in (
            "staff_role_assignments",
            "role_data_scopes",
        ):
            with self.subTest(extra_table=extra_table):
                with tempfile.TemporaryDirectory() as tmpdir:
                    db_path = os.path.join(tmpdir, "legacy.sqlite3")
                    _build_migration_source_db(db_path, extra_table=extra_table)
                    with self.assertRaises(pam.MigrationDecisionError) as ctx:
                        pam._load_legacy_source_snapshot(db_path)
                    self.assertEqual(ctx.exception.code, "source_already_target")

    def test_target_marker_column_raises_source_already_target(self):
        cases = [
            ("users", "is_system_admin"),
            ("users", "account_kind"),
            ("sessions", "token_hash"),
            ("sessions", "session_id"),
            ("staff_members", "employment_status"),
        ]
        for table, col in cases:
            with self.subTest(table=table, col=col):
                with tempfile.TemporaryDirectory() as tmpdir:
                    db_path = os.path.join(tmpdir, "legacy.sqlite3")
                    _build_migration_source_db(db_path, extra_columns=(table, [col]))
                    with self.assertRaises(pam.MigrationDecisionError) as ctx:
                        pam._load_legacy_source_snapshot(db_path)
                    self.assertEqual(ctx.exception.code, "source_already_target")

    def test_rejects_default_db_path_and_symlink(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            fake_default = os.path.join(tmpdir, "default.sqlite3")
            _build_migration_source_db(fake_default)

            with mock.patch.object(
                pam, "DEFAULT_DB_PATH", Path(fake_default).resolve()
            ):
                with self.assertRaises(pam.MigrationDecisionError) as ctx:
                    pam._load_legacy_source_snapshot(fake_default)
                self.assertEqual(ctx.exception.code, "source_path")

                symlink_path = os.path.join(tmpdir, "link.sqlite3")
                os.symlink(fake_default, symlink_path)
                with self.assertRaises(pam.MigrationDecisionError) as ctx2:
                    pam._load_legacy_source_snapshot(symlink_path)
                self.assertEqual(ctx2.exception.code, "source_path")

    def test_special_char_filename_is_readable(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "legacy 备份 (1)#测试.sqlite3")
            _build_migration_source_db(db_path)
            snapshot = pam._load_legacy_source_snapshot(db_path)
            self.assertEqual(len(snapshot["staff_rows"]), 1)

    def test_no_wal_shm_journal_files_left_behind(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "legacy.sqlite3")
            _build_migration_source_db(db_path)
            pam._load_legacy_source_snapshot(db_path)
            names = os.listdir(tmpdir)
            self.assertFalse(any(n.endswith(("-wal", "-shm", "-journal")) for n in names))


def _staff_row(**overrides):
    row = {
        "staff_id": 1, "name": f"NAME_{SECRET_MARKER}", "note": None, "job_no": None,
        "phone": None, "sex": None, "id_card": None, "title": None, "license_no": None,
        "department": None, "role": "admin", "roles": "admin", "active": 1,
        "created_at": "2026-01-01", "updated_at": "2026-01-01",
    }
    row.update(overrides)
    return tuple(row[c] for c in pam._MIGRATION_REQUIRED_COLUMNS["staff_members"])


def _user_row(**overrides):
    row = {
        "id": 1, "username": f"USER_{SECRET_MARKER}", "display_name": None,
        "password_hash": f"HASH_{SECRET_MARKER}", "role": "admin", "is_active": 1,
        "staff_id": 1, "created_at": "2026-01-01", "updated_at": "2026-01-01",
    }
    row.update(overrides)
    return tuple(row[c] for c in pam._MIGRATION_REQUIRED_COLUMNS["users"])


def _base_snapshot(**overrides):
    snapshot = {
        "staff_rows": [_staff_row()],
        "user_rows": [_user_row()],
        "roles": [("admin", f"ROLE_{SECRET_MARKER}_admin")],
        "sessions_to_invalidate": 1,
        "audit_rows_preserved": 1,
    }
    snapshot.update(overrides)
    return snapshot


class NormalizeLegacySourceRowsTests(unittest.TestCase):
    def test_success_shape_and_no_sharing_with_snapshot(self):
        snapshot = _base_snapshot()
        result = pam._normalize_legacy_source_rows(snapshot)
        self.assertEqual(
            set(result.keys()),
            {"staff", "users", "roles", "sessions_to_invalidate", "audit_rows_preserved"},
        )
        self.assertEqual(result["staff"]["1"]["staff_id"], "1")
        self.assertEqual(result["users"]["1"]["staff_id"], "1")
        self.assertEqual(result["roles"], {"admin": f"ROLE_{SECRET_MARKER}_admin"})
        result["staff"]["1"]["name"] = "MUTATED"
        self.assertEqual(snapshot["staff_rows"][0][1], f"NAME_{SECRET_MARKER}")

    def test_string_id_kept_with_whitespace(self):
        snapshot = _base_snapshot(
            staff_rows=[_staff_row(staff_id=" S1 ")],
            user_rows=[_user_row(staff_id=" S1 ")],
        )
        result = pam._normalize_legacy_source_rows(snapshot)
        self.assertIn(" S1 ", result["staff"])
        self.assertEqual(result["users"]["1"]["staff_id"], " S1 ")

    def test_blank_user_staff_id_normalized_to_none(self):
        for blank in ("", "   "):
            with self.subTest(blank=repr(blank)):
                snapshot = _base_snapshot(user_rows=[_user_row(staff_id=blank)])
                result = pam._normalize_legacy_source_rows(snapshot)
                self.assertIsNone(result["users"]["1"]["staff_id"])

    def test_error_cases_raise_stable_codes(self):
        cases = {
            "invalid_staff_id": (
                _base_snapshot(staff_rows=[_staff_row(staff_id=True)]), "source_rows",
            ),
            "duplicate_staff_id_after_normalize": (
                _base_snapshot(staff_rows=[_staff_row(staff_id=1), _staff_row(staff_id="1")]),
                "source_rows",
            ),
            "bool_user_id": (
                _base_snapshot(user_rows=[_user_row(id=True)]), "source_rows",
            ),
            "blank_username": (
                _base_snapshot(user_rows=[_user_row(username="  ")]), "source_rows",
            ),
            "blank_password_hash": (
                _base_snapshot(user_rows=[_user_row(password_hash="")]), "source_rows",
            ),
            "duplicate_username": (
                _base_snapshot(
                    staff_rows=[_staff_row(staff_id=1), _staff_row(staff_id=2)],
                    user_rows=[
                        _user_row(id=1, staff_id=1),
                        _user_row(id=2, staff_id=2, username=f"USER_{SECRET_MARKER}"),
                    ],
                ),
                "duplicate_username",
            ),
            "staff_active_bool": (
                _base_snapshot(staff_rows=[_staff_row(active=True)]), "source_rows",
            ),
            "staff_active_string": (
                _base_snapshot(staff_rows=[_staff_row(active="1")]), "source_rows",
            ),
            "staff_active_out_of_range": (
                _base_snapshot(staff_rows=[_staff_row(active=2)]), "source_rows",
            ),
            "staff_active_null": (
                _base_snapshot(staff_rows=[_staff_row(active=None)]), "source_rows",
            ),
            "user_is_active_bool": (
                _base_snapshot(user_rows=[_user_row(is_active=False)]), "source_rows",
            ),
            "user_is_active_out_of_range": (
                _base_snapshot(user_rows=[_user_row(is_active=5)]), "source_rows",
            ),
            "orphan_staff_link": (
                _base_snapshot(staff_rows=[_staff_row(staff_id=9)]), "orphan_staff_link",
            ),
            "duplicate_staff_account": (
                _base_snapshot(
                    user_rows=[
                        _user_row(id=1, staff_id=1),
                        _user_row(id=2, staff_id=1, username=f"USER2_{SECRET_MARKER}"),
                    ],
                ),
                "duplicate_staff_account",
            ),
            "role_key_bool": (
                _base_snapshot(roles=[(True, "x")]), "source_rows",
            ),
            "role_key_duplicate": (
                _base_snapshot(roles=[(1, "a"), ("1", "b")]), "source_rows",
            ),
            "sessions_negative": (
                _base_snapshot(sessions_to_invalidate=-1), "source_rows",
            ),
            "sessions_bool": (
                _base_snapshot(sessions_to_invalidate=True), "source_rows",
            ),
            "audit_negative": (
                _base_snapshot(audit_rows_preserved=-1), "source_rows",
            ),
        }
        for name, (snapshot, expected_code) in cases.items():
            with self.subTest(name=name):
                with self.assertRaises(pam.MigrationDecisionError) as ctx:
                    pam._normalize_legacy_source_rows(snapshot)
                self.assertEqual(ctx.exception.code, expected_code)
                self.assertNotIn(SECRET_MARKER, str(ctx.exception))
                self.assertNotIn(SECRET_MARKER, repr(ctx.exception))


class LegacySourceSnapshotSqliteErrorTests(unittest.TestCase):
    def test_sqlite_error_during_read_becomes_source_schema_and_closes_connection(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "legacy.sqlite3")
            _build_migration_source_db(db_path)

            class FailingConnection(sqlite3.Connection):
                def execute(self, sql, *a, **kw):
                    if "FROM staff_members" in sql:
                        raise sqlite3.OperationalError(f"boom_{SECRET_MARKER}")
                    return super().execute(sql, *a, **kw)

            real_connect = sqlite3.connect

            def fake_connect(*args, **kwargs):
                kwargs["factory"] = FailingConnection
                return real_connect(*args, **kwargs)

            with mock.patch.object(sqlite3, "connect", side_effect=fake_connect):
                with self.assertRaises(pam.MigrationDecisionError) as ctx:
                    pam._load_legacy_source_snapshot(db_path)
                self.assertEqual(ctx.exception.code, "source_schema")
                self.assertNotIn(SECRET_MARKER, str(ctx.exception))

            snapshot = pam._load_legacy_source_snapshot(db_path)
            self.assertEqual(len(snapshot["staff_rows"]), 1)
            names = os.listdir(tmpdir)
            self.assertFalse(any(n.endswith(("-wal", "-shm", "-journal")) for n in names))

    def test_close_failure_after_successful_read_becomes_source_schema(self):
        """Regression for a close() failure after an otherwise
        successful read must not leak a native sqlite3.Error and must not
        be reported as success."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "legacy.sqlite3")
            _build_migration_source_db(db_path)
            source_hash_before = _file_sha256(db_path)

            class FailCloseConnection(sqlite3.Connection):
                def close(self):
                    super().close()
                    raise sqlite3.Error(f"boom_{SECRET_MARKER}")

            real_connect = sqlite3.connect

            def fake_connect(*args, **kwargs):
                kwargs["factory"] = FailCloseConnection
                return real_connect(*args, **kwargs)

            with mock.patch.object(sqlite3, "connect", side_effect=fake_connect):
                with self.assertRaises(pam.MigrationDecisionError) as ctx:
                    pam._load_legacy_source_snapshot(db_path)
                self.assertEqual(ctx.exception.code, "source_schema")
                self.assertNotIn(SECRET_MARKER, str(ctx.exception))
                self.assertNotIn(SECRET_MARKER, repr(ctx.exception))

            self.assertEqual(_file_sha256(db_path), source_hash_before)
            names = os.listdir(tmpdir)
            self.assertFalse(any(n.endswith(("-wal", "-shm", "-journal")) for n in names))


class ConnectionCloseFailureTests(unittest.TestCase):
    """Regression tests for sqlite3 connection.close() failures must
    resolve to the existing stable per-phase error, never leak a native
    sqlite3.Error, and never crash on an unassigned local connection."""

    def test_preflight_close_failure_raises_generic_preflight_error(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "source.sqlite3")
            _build_full_db(db_path)
            source_hash_before = _file_sha256(db_path)
            files_before = set(os.listdir(tmpdir))

            class FailCloseConnection(sqlite3.Connection):
                def close(self):
                    super().close()
                    raise sqlite3.Error(f"boom_{SECRET_MARKER}")

            real_connect = sqlite3.connect

            def fake_connect(*args, **kwargs):
                kwargs["factory"] = FailCloseConnection
                return real_connect(*args, **kwargs)

            with mock.patch.object(sqlite3, "connect", side_effect=fake_connect):
                with self.assertRaises(pam.PreflightError) as ctx:
                    pam.preflight(db_path)
                self.assertEqual(str(ctx.exception), pam._GENERIC_ERROR)
                self.assertNotIn(SECRET_MARKER, str(ctx.exception))
                self.assertNotIn(SECRET_MARKER, repr(ctx.exception))
                self.assertNotIn(db_path, str(ctx.exception))

            self.assertEqual(_file_sha256(db_path), source_hash_before)
            self.assertEqual(set(os.listdir(tmpdir)), files_before)

    def test_open_readonly_connect_failure_raises_preflight_error_without_unbound_local(self):
        """Regression for if sqlite3.connect itself raises, `con` is
        never assigned; the except branch must not crash with
        UnboundLocalError while trying to close it."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "source.sqlite3")
            _build_full_db(db_path)
            resolved = pam._resolve_and_guard_path(db_path)

            with mock.patch.object(
                sqlite3, "connect", side_effect=sqlite3.OperationalError(f"boom_{SECRET_MARKER}")
            ):
                with self.assertRaises(pam.PreflightError) as ctx:
                    pam._open_readonly(resolved)
                self.assertEqual(str(ctx.exception), pam._GENERIC_ERROR)
                self.assertNotIn(SECRET_MARKER, str(ctx.exception))


def _staff_record(staff_id, role, roles=None):
    return {"staff_id": staff_id, "role": role, "roles": roles}


def _norm_source(staff, roles):
    return {"staff": staff, "roles": roles}


class PrepareStaffRoleAssignmentsTests(unittest.TestCase):
    def test_zero_staff_and_empty_map_succeeds(self):
        source = _norm_source({}, {"admin": "x"})
        decisions = {"staff_role_map": {}}
        result = pam._prepare_staff_role_assignments(source, decisions)
        self.assertEqual(result["assignments"], [])
        self.assertEqual(result["staff_role_keys"], {})
        self.assertEqual(result["source_token_map"], {})

    def test_multi_role_dedup_and_single_primary(self):
        staff = {
            "1": _staff_record("1", f" 护士_{SECRET_MARKER} ", f" 护士_{SECRET_MARKER} , ,前台_{SECRET_MARKER} , 前台_{SECRET_MARKER} "),
        }
        roles = {"nurse": "护士角色", "front": "前台角色"}
        decisions = {
            "staff_role_map": {
                f"护士_{SECRET_MARKER}": "nurse",
                f"前台_{SECRET_MARKER}": "front",
            }
        }
        result = pam._prepare_staff_role_assignments(_norm_source(staff, roles), decisions)
        self.assertEqual(
            result["assignments"],
            [("1", "nurse", 1, None), ("1", "front", 0, None)],
        )
        self.assertEqual(result["staff_role_keys"], {"1": frozenset({"nurse", "front"})})
        self.assertNotIn(SECRET_MARKER, str(result["assignments"]))

    def test_primary_included_even_if_absent_from_roles_field(self):
        staff = {"1": _staff_record("1", "doctor", "")}
        roles = {"doctor_key": "医生"}
        decisions = {"staff_role_map": {"doctor": "doctor_key"}}
        result = pam._prepare_staff_role_assignments(_norm_source(staff, roles), decisions)
        self.assertEqual(result["assignments"], [("1", "doctor_key", 1, None)])

    def test_multiple_tokens_mapping_to_same_target_dedup_primary_wins(self):
        staff = {"1": _staff_record("1", "doctor", "consultant")}
        roles = {"shared": "共享岗位"}
        decisions = {"staff_role_map": {"doctor": "shared", "consultant": "shared"}}
        result = pam._prepare_staff_role_assignments(_norm_source(staff, roles), decisions)
        self.assertEqual(result["assignments"], [("1", "shared", 1, None)])
        self.assertEqual(result["staff_role_keys"], {"1": frozenset({"shared"})})

    def test_staff_sorted_and_roles_original_order_preserved(self):
        staff = {
            "2": _staff_record("2", "b", "c,a"),
            "1": _staff_record("1", "a"),
        }
        roles = {"ra": "A", "rb": "B", "rc": "C"}
        decisions = {"staff_role_map": {"a": "ra", "b": "rb", "c": "rc"}}
        result = pam._prepare_staff_role_assignments(_norm_source(staff, roles), decisions)
        self.assertEqual(
            result["assignments"],
            [
                ("1", "ra", 1, None),
                ("2", "rb", 1, None),
                ("2", "rc", 0, None),
                ("2", "ra", 0, None),
            ],
        )

    def test_map_key_set_mismatch_rejected(self):
        staff = {"1": _staff_record("1", "a")}
        roles = {"ra": "A"}
        cases = {
            "missing_key": {},
            "extra_key": {"a": "ra", "extra": "ra"},
        }
        for name, staff_role_map in cases.items():
            with self.subTest(name=name):
                with self.assertRaises(pam.MigrationDecisionError) as ctx:
                    pam._prepare_staff_role_assignments(
                        _norm_source(staff, roles), {"staff_role_map": staff_role_map}
                    )
                self.assertEqual(ctx.exception.code, "staff_role_map")

    def test_unknown_or_admin_target_rejected(self):
        staff = {"1": _staff_record("1", "a")}
        roles = {"ra": "A"}
        for target in ("unknown_role", "admin"):
            with self.subTest(target=target):
                with self.assertRaises(pam.MigrationDecisionError) as ctx:
                    pam._prepare_staff_role_assignments(
                        _norm_source(staff, roles), {"staff_role_map": {"a": target}}
                    )
                self.assertEqual(ctx.exception.code, "staff_role_map")

    def test_role_blank_or_non_str_rejected(self):
        roles = {"ra": "A"}
        for bad_role in (None, "  ", 123):
            with self.subTest(bad_role=repr(bad_role)):
                staff = {"1": _staff_record("1", bad_role)}
                with self.assertRaises(pam.MigrationDecisionError) as ctx:
                    pam._prepare_staff_role_assignments(
                        _norm_source(staff, roles), {"staff_role_map": {}}
                    )
                self.assertEqual(ctx.exception.code, "staff_role_map")

    def test_roles_field_non_str_rejected(self):
        staff = {"1": _staff_record("1", "a", roles=123)}
        roles = {"ra": "A"}
        with self.assertRaises(pam.MigrationDecisionError) as ctx:
            pam._prepare_staff_role_assignments(
                _norm_source(staff, roles), {"staff_role_map": {"a": "ra"}}
            )
        self.assertEqual(ctx.exception.code, "staff_role_map")

    def test_does_not_mutate_inputs_and_no_secret_leak_in_exception(self):
        staff = {"1": _staff_record("1", f"a_{SECRET_MARKER}", f"b_{SECRET_MARKER}")}
        roles = {"ra": "A"}
        source = _norm_source(staff, roles)
        decisions = {"staff_role_map": {}}
        with self.assertRaises(pam.MigrationDecisionError) as ctx:
            pam._prepare_staff_role_assignments(source, decisions)
        self.assertNotIn(SECRET_MARKER, str(ctx.exception))
        self.assertNotIn(SECRET_MARKER, repr(ctx.exception))
        self.assertEqual(source, _norm_source(staff, roles))
        self.assertEqual(decisions, {"staff_role_map": {}})


def _user(id, staff_id=None, is_active=1, username=None):
    return {
        "id": id,
        "staff_id": staff_id,
        "is_active": is_active,
        "username": username or f"user_{id}_{SECRET_MARKER}",
    }


def _staff(active=1):
    return {"active": active}


def _account_source(users, staff=None):
    return {"users": users, "staff": staff or {}}


def _bg(id="bg1", username=f"bg_{SECRET_MARKER}"):
    return {
        "id": id,
        "username": username,
        "display_name": f"应急_{SECRET_MARKER}",
        "password_hash": f"HASH_{SECRET_MARKER}",
    }


def _account_decisions(admin_ids=("1",), break_glass=None, resolutions=None):
    return {
        "regular_system_admin_user_ids": list(admin_ids),
        "break_glass_account": break_glass or _bg(),
        "status_conflict_resolutions": resolutions or {},
    }


class PrepareAccountDecisionsTests(unittest.TestCase):
    def test_linked_and_unlinked_explicit_admin_succeeds(self):
        users = {
            "1": _user("1", staff_id="s1"),
            "2": _user("2", staff_id=None),
        }
        source = _account_source(users, {"s1": _staff()})
        decisions = _account_decisions(admin_ids=("1", "2"))
        result = pam._prepare_account_decisions(source, decisions)
        self.assertEqual(result["regular_admin_ids"], frozenset({"1", "2"}))
        self.assertEqual(result["account_kinds"], {"1": "staff", "2": "independent_admin"})
        self.assertEqual(result["final_user_active"], {"1": 1, "2": 1})
        self.assertEqual(result["users_to_disable"], 0)

    def test_old_admin_role_not_auto_authorized(self):
        users = {"1": _user("1", staff_id=None)}
        source = _account_source(users)
        decisions = _account_decisions(admin_ids=())
        with self.assertRaises(pam.MigrationDecisionError) as ctx:
            pam._prepare_account_decisions(source, decisions)
        self.assertEqual(ctx.exception.code, "unlinked_account")

    def test_unknown_admin_id_rejected(self):
        users = {"1": _user("1", staff_id=None)}
        source = _account_source(users)
        decisions = _account_decisions(admin_ids=("1", "999"))
        with self.assertRaises(pam.MigrationDecisionError) as ctx:
            pam._prepare_account_decisions(source, decisions)
        self.assertEqual(ctx.exception.code, "admin_ids")

    def test_int_str_duplicate_admin_id_rejected(self):
        users = {"1": _user("1", staff_id=None)}
        source = _account_source(users)
        decisions = _account_decisions(admin_ids=(1, "1"))
        with self.assertRaises(pam.MigrationDecisionError) as ctx:
            pam._prepare_account_decisions(source, decisions)
        self.assertEqual(ctx.exception.code, "admin_ids")

    def test_unlinked_non_admin_rejected(self):
        users = {
            "1": _user("1", staff_id=None),
            "2": _user("2", staff_id=None),
        }
        source = _account_source(users)
        decisions = _account_decisions(admin_ids=("1",))
        with self.assertRaises(pam.MigrationDecisionError) as ctx:
            pam._prepare_account_decisions(source, decisions)
        self.assertEqual(ctx.exception.code, "unlinked_account")

    def test_break_glass_id_collision_rejected(self):
        users = {"1": _user("1", staff_id=None)}
        source = _account_source(users)
        decisions = _account_decisions(admin_ids=("1",), break_glass=_bg(id=1))
        with self.assertRaises(pam.MigrationDecisionError) as ctx:
            pam._prepare_account_decisions(source, decisions)
        self.assertEqual(ctx.exception.code, "break_glass")

    def test_break_glass_username_collision_rejected(self):
        users = {"1": _user("1", staff_id=None, username="clash")}
        source = _account_source(users)
        decisions = _account_decisions(admin_ids=("1",), break_glass=_bg(username="clash"))
        with self.assertRaises(pam.MigrationDecisionError) as ctx:
            pam._prepare_account_decisions(source, decisions)
        self.assertEqual(ctx.exception.code, "break_glass")

    def test_break_glass_not_counted_toward_usable_admin(self):
        users = {"1": _user("1", staff_id="s1", is_active=0)}
        source = _account_source(users, {"s1": _staff(active=1)})
        decisions = _account_decisions(admin_ids=("1",), resolutions={"1": "keep_disabled"})
        with self.assertRaises(pam.MigrationDecisionError) as ctx:
            pam._prepare_account_decisions(source, decisions)
        self.assertEqual(ctx.exception.code, "no_usable_admin")

    def test_active_user_inactive_staff_missing_resolution_rejected(self):
        users = {"1": _user("1", staff_id="s1", is_active=1)}
        source = _account_source(users, {"s1": _staff(active=0)})
        decisions = _account_decisions(admin_ids=("1",), resolutions={})
        with self.assertRaises(pam.MigrationDecisionError) as ctx:
            pam._prepare_account_decisions(source, decisions)
        self.assertEqual(ctx.exception.code, "status_resolutions")

    def test_active_user_inactive_staff_disable_user_resolution(self):
        users = {
            "1": _user("1", staff_id="s1", is_active=1),
            "2": _user("2", staff_id=None),
        }
        source = _account_source(users, {"s1": _staff(active=0)})
        decisions = _account_decisions(
            admin_ids=("1", "2"), resolutions={"1": "disable_user"}
        )
        result = pam._prepare_account_decisions(source, decisions)
        self.assertEqual(result["final_user_active"]["1"], 0)
        self.assertEqual(result["users_to_disable"], 1)

    def test_inactive_user_active_staff_keep_disabled_resolution(self):
        users = {
            "1": _user("1", staff_id="s1", is_active=0),
            "2": _user("2", staff_id=None),
        }
        source = _account_source(users, {"s1": _staff(active=1)})
        decisions = _account_decisions(
            admin_ids=("1", "2"), resolutions={"1": "keep_disabled"}
        )
        result = pam._prepare_account_decisions(source, decisions)
        self.assertEqual(result["final_user_active"]["1"], 0)
        self.assertEqual(result["users_to_disable"], 0)

    def test_missing_extra_and_wrong_value_resolutions_rejected(self):
        users = {"1": _user("1", staff_id="s1", is_active=1)}
        source = _account_source(users, {"s1": _staff(active=0)})
        cases = {
            "missing": {},
            "extra": {"1": "disable_user", "2": "keep_disabled"},
            "wrong_value": {"1": "keep_disabled"},
        }
        for name, resolutions in cases.items():
            with self.subTest(name=name):
                decisions = _account_decisions(admin_ids=("1",), resolutions=resolutions)
                with self.assertRaises(pam.MigrationDecisionError) as ctx:
                    pam._prepare_account_decisions(source, decisions)
                self.assertEqual(ctx.exception.code, "status_resolutions")

    def test_normalized_duplicate_resolution_key_rejected(self):
        users = {"1": _user("1", staff_id=None)}
        source = _account_source(users)
        decisions = _account_decisions(admin_ids=("1",))
        decisions["status_conflict_resolutions"] = {1: "disable_user", "1": "disable_user"}
        with self.assertRaises(pam.MigrationDecisionError) as ctx:
            pam._prepare_account_decisions(source, decisions)
        self.assertEqual(ctx.exception.code, "status_resolutions")

    def test_at_least_one_active_admin_among_mixed_succeeds(self):
        users = {
            "1": _user("1", staff_id=None, is_active=1),
            "2": _user("2", staff_id="s2", is_active=0),
        }
        source = _account_source(users, {"s2": _staff(active=0)})
        decisions = _account_decisions(admin_ids=("1", "2"), resolutions={})
        result = pam._prepare_account_decisions(source, decisions)
        self.assertEqual(result["final_user_active"], {"1": 1, "2": 0})

    def test_only_disabled_admins_rejected(self):
        users = {"1": _user("1", staff_id=None, is_active=0)}
        source = _account_source(users)
        decisions = _account_decisions(admin_ids=("1",))
        with self.assertRaises(pam.MigrationDecisionError) as ctx:
            pam._prepare_account_decisions(source, decisions)
        self.assertEqual(ctx.exception.code, "no_usable_admin")

    def test_does_not_mutate_inputs_and_no_secret_leak(self):
        users = {"1": _user("1", staff_id=None)}
        source = _account_source(users)
        decisions = _account_decisions(admin_ids=("1",))
        source_copy = json.loads(json.dumps(source))
        decisions_copy = json.loads(json.dumps(decisions))
        pam._prepare_account_decisions(source, decisions)
        self.assertEqual(json.loads(json.dumps(source)), source_copy)
        self.assertEqual(json.loads(json.dumps(decisions)), decisions_copy)

        bad_users = {"1": _user("1", staff_id=None, username=f"clash_{SECRET_MARKER}")}
        bad_source = _account_source(bad_users)
        bad_decisions = _account_decisions(
            admin_ids=("1",), break_glass=_bg(username=f"clash_{SECRET_MARKER}")
        )
        with self.assertRaises(pam.MigrationDecisionError) as ctx:
            pam._prepare_account_decisions(bad_source, bad_decisions)
        self.assertNotIn(SECRET_MARKER, str(ctx.exception))
        self.assertNotIn(SECRET_MARKER, repr(ctx.exception))


def _ack_user(id, staff_id, role):
    return {"id": id, "staff_id": staff_id, "role": role}


def _ack_source(users, roles=("nurse", "doctor")):
    return {"users": users, "roles": {r: r for r in roles}}


def _role_plan(staff_role_keys):
    return {"staff_role_keys": staff_role_keys}


def _ack_decisions(acknowledged=()):
    return {"acknowledged_legacy_user_role_user_ids": list(acknowledged)}


class PrepareLegacyUserRoleAcknowledgmentsTests(unittest.TestCase):
    def test_matching_role_needs_no_acknowledgment(self):
        source = _ack_source({"1": _ack_user("1", "s1", "nurse")})
        role_plan = _role_plan({"s1": frozenset({"nurse"})})
        result = pam._prepare_legacy_user_role_acknowledgments(
            source, _ack_decisions(), role_plan, {}
        )
        self.assertEqual(result["acknowledged_user_ids"], frozenset())
        self.assertEqual(result["legacy_user_roles_acknowledged"], 0)

    def test_unlinked_account_never_required_even_if_independent_admin(self):
        source = _ack_source({"1": _ack_user("1", None, "admin")})
        role_plan = _role_plan({})
        account_plan = {"account_kinds": {"1": "independent_admin"}}
        result = pam._prepare_legacy_user_role_acknowledgments(
            source, _ack_decisions(), role_plan, account_plan
        )
        self.assertEqual(result["acknowledged_user_ids"], frozenset())

    def test_blank_non_str_unknown_admin_mismatch_all_require_ack(self):
        cases = {
            "blank": "   ",
            "non_str": 5,
            "unknown": "wizard",
            "admin": "admin",
            "mismatch": "doctor",
        }
        for name, role in cases.items():
            with self.subTest(name=name):
                source = _ack_source({"1": _ack_user("1", "s1", role)})
                role_plan = _role_plan({"s1": frozenset({"nurse"})})
                decisions = _ack_decisions(acknowledged=["1"])
                result = pam._prepare_legacy_user_role_acknowledgments(
                    source, decisions, role_plan, {}
                )
                self.assertEqual(result["acknowledged_user_ids"], frozenset({"1"}))
                self.assertEqual(result["legacy_user_roles_acknowledged"], 1)

    def test_missing_extra_unknown_unlinked_and_consistent_ack_rejected(self):
        source = _ack_source(
            {
                "1": _ack_user("1", "s1", "wizard"),
                "2": _ack_user("2", "s2", "nurse"),
                "3": _ack_user("3", None, "admin"),
            }
        )
        role_plan = _role_plan({"s1": frozenset({"nurse"}), "s2": frozenset({"nurse"})})
        cases = {
            "missing": [],
            "extra": ["1", "999"],
            "unknown_user": ["999"],
            "unlinked_user": ["1", "3"],
            "consistent_user": ["1", "2"],
        }
        for name, acknowledged in cases.items():
            with self.subTest(name=name):
                decisions = _ack_decisions(acknowledged=acknowledged)
                with self.assertRaises(pam.MigrationDecisionError) as ctx:
                    pam._prepare_legacy_user_role_acknowledgments(
                        source, decisions, role_plan, {}
                    )
                self.assertEqual(ctx.exception.code, "acknowledged_user_roles")

    def test_int_str_id_normalization_and_duplicate_rejected(self):
        source = _ack_source({"1": _ack_user("1", "s1", "wizard")})
        role_plan = _role_plan({"s1": frozenset({"nurse"})})

        result = pam._prepare_legacy_user_role_acknowledgments(
            source, _ack_decisions(acknowledged=[1]), role_plan, {}
        )
        self.assertEqual(result["acknowledged_user_ids"], frozenset({"1"}))

        with self.assertRaises(pam.MigrationDecisionError) as ctx:
            pam._prepare_legacy_user_role_acknowledgments(
                source, _ack_decisions(acknowledged=[1, "1"]), role_plan, {}
            )
        self.assertEqual(ctx.exception.code, "acknowledged_user_roles")

    def test_unlinked_account_rejected_when_account_plan_missing_empty_or_wrong_kind(self):
        source = _ack_source({"1": _ack_user("1", None, "admin")})
        role_plan = _role_plan({})
        cases = {
            "empty_plan": {},
            "missing_user": {"account_kinds": {"999": "independent_admin"}},
            "wrong_kind": {"account_kinds": {"1": "staff"}},
        }
        for name, account_plan in cases.items():
            with self.subTest(name=name):
                with self.assertRaises(pam.MigrationDecisionError) as ctx:
                    pam._prepare_legacy_user_role_acknowledgments(
                        source, _ack_decisions(), role_plan, account_plan
                    )
                self.assertEqual(ctx.exception.code, "acknowledged_user_roles")

    def test_does_not_mutate_inputs_and_no_id_or_role_leak(self):
        source = _ack_source({f"{SECRET_MARKER}1": _ack_user(f"{SECRET_MARKER}1", "s1", "wizard")})
        role_plan = _role_plan({"s1": frozenset({"nurse"})})
        decisions = _ack_decisions(acknowledged=[])
        source_copy = json.loads(json.dumps(source))
        decisions_copy = json.loads(json.dumps(decisions))

        with self.assertRaises(pam.MigrationDecisionError) as ctx:
            pam._prepare_legacy_user_role_acknowledgments(source, decisions, role_plan, {})

        self.assertEqual(json.loads(json.dumps(source)), source_copy)
        self.assertEqual(json.loads(json.dumps(decisions)), decisions_copy)
        self.assertNotIn(SECRET_MARKER, str(ctx.exception))
        self.assertNotIn(SECRET_MARKER, repr(ctx.exception))


_PLAN_TOKEN_DOCTOR = f"TOKDOC_{SECRET_MARKER}"
_PLAN_TOKEN_NURSE = f"TOKNUR_{SECRET_MARKER}"


def _build_plan_source_db(path):
    con = sqlite3.connect(path)
    try:
        con.executescript(
            """
            CREATE TABLE staff_members (
                staff_id INTEGER PRIMARY KEY,
                name TEXT, note TEXT, job_no TEXT, phone TEXT, sex TEXT,
                id_card TEXT, title TEXT, license_no TEXT, department TEXT,
                role TEXT, roles TEXT, active INTEGER,
                created_at TEXT, updated_at TEXT
            );
            CREATE TABLE users (
                id INTEGER PRIMARY KEY,
                username TEXT, display_name TEXT, password_hash TEXT,
                role TEXT, is_active INTEGER, staff_id INTEGER,
                created_at TEXT, updated_at TEXT
            );
            CREATE TABLE sessions (
                token TEXT, user_id INTEGER, created_at TEXT, expires_at TEXT
            );
            CREATE TABLE roles (
                role_key TEXT PRIMARY KEY, name TEXT, is_system INTEGER,
                sort INTEGER, created_at TEXT, updated_at TEXT
            );
            CREATE TABLE role_permissions (
                role_key TEXT, perm_key TEXT
            );
            CREATE TABLE audit_logs (
                audit_id INTEGER PRIMARY KEY, entity_type TEXT, entity_id TEXT,
                action TEXT, old_json TEXT, new_json TEXT, operator TEXT,
                created_at TEXT
            );
            """
        )
        for role_key in ("admin", "doctor", "nurse"):
            con.execute(
                "INSERT INTO roles(role_key, name, is_system, sort, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?)",
                (role_key, f"ROLE_{SECRET_MARKER}_{role_key}", 1, 0, "2026-01-01", "2026-01-01"),
            )
        con.execute(
            "INSERT INTO staff_members(staff_id, name, note, job_no, phone, sex, id_card, "
            "title, license_no, department, role, roles, active, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                1, f"NAME_{SECRET_MARKER}_1", f"NOTE_{SECRET_MARKER}", f"JOB_{SECRET_MARKER}",
                f"PHONE_{SECRET_MARKER}_1", f"SEX_{SECRET_MARKER}", f"ID_{SECRET_MARKER}_1",
                f"TITLE_{SECRET_MARKER}", f"LIC_{SECRET_MARKER}", f"DEPT_{SECRET_MARKER}",
                _PLAN_TOKEN_DOCTOR, _PLAN_TOKEN_DOCTOR, 1, "2026-01-01", "2026-01-01",
            ),
        )
        con.execute(
            "INSERT INTO staff_members(staff_id, name, note, job_no, phone, sex, id_card, "
            "title, license_no, department, role, roles, active, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                2, f"NAME_{SECRET_MARKER}_2", f"NOTE_{SECRET_MARKER}", f"JOB_{SECRET_MARKER}",
                f"PHONE_{SECRET_MARKER}_2", f"SEX_{SECRET_MARKER}", f"ID_{SECRET_MARKER}_2",
                f"TITLE_{SECRET_MARKER}", f"LIC_{SECRET_MARKER}", f"DEPT_{SECRET_MARKER}",
                _PLAN_TOKEN_NURSE, f"{_PLAN_TOKEN_NURSE},{_PLAN_TOKEN_DOCTOR}", 0,
                "2026-01-01", "2026-01-01",
            ),
        )
        # user 1: linked to staff1 (doctor), regular admin, legacy role "admin" needs ack
        con.execute(
            "INSERT INTO users(id, username, display_name, password_hash, role, is_active, "
            "staff_id, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (
                1, f"USER_{SECRET_MARKER}_1", f"DISP_{SECRET_MARKER}_1", f"HASH_{SECRET_MARKER}_1",
                "admin", 1, 1, "2026-01-01", "2026-01-01",
            ),
        )
        # user 2: linked to staff2 (nurse, inactive) -> active user / inactive staff conflict
        con.execute(
            "INSERT INTO users(id, username, display_name, password_hash, role, is_active, "
            "staff_id, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (
                2, f"USER_{SECRET_MARKER}_2", f"DISP_{SECRET_MARKER}_2", f"HASH_{SECRET_MARKER}_2",
                "nurse", 1, 2, "2026-01-01", "2026-01-01",
            ),
        )
        # user 3: independent admin, no staff link
        con.execute(
            "INSERT INTO users(id, username, display_name, password_hash, role, is_active, "
            "staff_id, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (
                3, f"USER_{SECRET_MARKER}_3", f"DISP_{SECRET_MARKER}_3", f"HASH_{SECRET_MARKER}_3",
                "admin", 1, None, "2026-01-01", "2026-01-01",
            ),
        )
        for token, user_id in ((f"TOKEN_{SECRET_MARKER}_1", 1), (f"TOKEN_{SECRET_MARKER}_2", 2)):
            con.execute(
                "INSERT INTO sessions(token, user_id, created_at, expires_at) VALUES (?,?,?,?)",
                (token, user_id, "2026-01-01", "2026-01-02"),
            )
        for i in range(3):
            con.execute(
                "INSERT INTO audit_logs(entity_type, entity_id, action, old_json, new_json, "
                "operator, created_at) VALUES (?,?,?,?,?,?,?)",
                (
                    f"ENTITY_{SECRET_MARKER}", str(i), "update", f"OLD_{SECRET_MARKER}_{i}",
                    f"NEW_{SECRET_MARKER}_{i}", f"OP_{SECRET_MARKER}_{i}", "2026-01-01",
                ),
            )
        con.commit()
    finally:
        con.close()


def _plan_decisions():
    return {
        "regular_system_admin_user_ids": [1, 3],
        "break_glass_account": {
            "id": "BG1",
            "username": f"bg_{SECRET_MARKER}",
            "display_name": f"应急_{SECRET_MARKER}",
            "password_hash": f"HASH_{SECRET_MARKER}_bg",
        },
        "staff_role_map": {
            _PLAN_TOKEN_DOCTOR: "doctor",
            _PLAN_TOKEN_NURSE: "nurse",
        },
        "acknowledged_legacy_user_role_user_ids": [1],
        "status_conflict_resolutions": {2: "disable_user"},
    }


class PlanMigrationTests(unittest.TestCase):
    def test_success_returns_exact_summary_and_is_readonly(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "legacy.sqlite3")
            _build_plan_source_db(db_path)
            before_hash = _file_sha256(db_path)
            before_names = set(os.listdir(tmpdir))
            con = sqlite3.connect(db_path)
            try:
                before_tables = {
                    row[0] for row in con.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    ).fetchall()
                }
                before_row_counts = {
                    table: con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                    for table in before_tables
                }
            finally:
                con.close()

            summary = pam.plan_migration(db_path, _plan_decisions())

            self.assertEqual(
                summary,
                {
                    "version": 1,
                    "staff_migrated": 2,
                    "role_assignments_created": 3,
                    "users_migrated": 4,
                    "regular_system_admins": 2,
                    "break_glass_accounts": 1,
                    "sessions_to_invalidate": 2,
                    "audit_rows_preserved": 3,
                    "users_to_disable": 1,
                    "legacy_user_roles_acknowledged": 1,
                },
            )
            for value in summary.values():
                self.assertIs(type(value), int)

            dumped = json.dumps(summary, ensure_ascii=False)
            self.assertNotIn(SECRET_MARKER, dumped)
            self.assertNotIn(db_path, dumped)
            self.assertNotIn("username", dumped)
            self.assertNotIn("token", dumped)

            self.assertEqual(_file_sha256(db_path), before_hash)
            self.assertEqual(set(os.listdir(tmpdir)), before_names)
            con = sqlite3.connect(db_path)
            try:
                after_tables = {
                    row[0] for row in con.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    ).fetchall()
                }
                after_row_counts = {
                    table: con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                    for table in after_tables
                }
            finally:
                con.close()
            self.assertEqual(after_tables, before_tables)
            self.assertEqual(after_row_counts, before_row_counts)

    def test_public_result_is_new_dict_without_private_keys(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "legacy.sqlite3")
            _build_plan_source_db(db_path)

            plan = pam._prepare_migration_plan(db_path, _plan_decisions())
            summary = pam.plan_migration(db_path, _plan_decisions())

            self.assertEqual(summary, plan["summary"])
            self.assertIsNot(summary, plan["summary"])
            for private_key in (
                "source", "decisions", "role_plan", "account_plan", "role_ack_plan",
            ):
                self.assertNotIn(private_key, summary)

    def test_invalid_decisions_fail_before_touching_database(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "legacy.sqlite3")
            _build_plan_source_db(db_path)

            with mock.patch("sqlite3.connect") as mock_connect:
                with self.assertRaises(pam.MigrationDecisionError):
                    pam.plan_migration(db_path, {"bad": "shape"})
                mock_connect.assert_not_called()

    def test_rejects_default_db_path_and_symlink(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            fake_default = os.path.join(tmpdir, "default.sqlite3")
            _build_plan_source_db(fake_default)

            with mock.patch.object(
                pam, "DEFAULT_DB_PATH", Path(fake_default).resolve()
            ):
                with self.assertRaises(pam.MigrationDecisionError) as ctx:
                    pam.plan_migration(fake_default, _plan_decisions())
                self.assertEqual(ctx.exception.code, "source_path")

                symlink_path = os.path.join(tmpdir, "link.sqlite3")
                os.symlink(fake_default, symlink_path)
                with self.assertRaises(pam.MigrationDecisionError) as ctx:
                    pam.plan_migration(symlink_path, _plan_decisions())
                self.assertEqual(ctx.exception.code, "source_path")

    def test_signature_has_exactly_two_required_params(self):
        sig = inspect.signature(pam.plan_migration)
        params = list(sig.parameters.values())
        self.assertEqual(len(params), 2)
        for param in params:
            self.assertIs(param.default, inspect.Parameter.empty)

    def test_malformed_source_path_raises_stable_source_path_error(self):
        bad_paths = (None, 123, "\x00bad")
        for bad_path in bad_paths:
            with self.subTest(bad_path=bad_path):
                with mock.patch("sqlite3.connect") as mock_connect:
                    with self.assertRaises(pam.MigrationDecisionError) as ctx:
                        pam.plan_migration(bad_path, _plan_decisions())
                    self.assertEqual(ctx.exception.code, "source_path")
                    message = str(ctx.exception)
                    self.assertNotIn(repr(bad_path), message)
                    self.assertNotIn("NoneType", message)
                    self.assertNotIn("int", message)
                    self.assertNotIn("\x00", message)
                    mock_connect.assert_not_called()

    def test_nonexistent_source_path_raises_stable_source_path_error(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            missing_path = os.path.join(tmpdir, "does_not_exist.sqlite3")
            with mock.patch("sqlite3.connect") as mock_connect:
                with self.assertRaises(pam.MigrationDecisionError) as ctx:
                    pam.plan_migration(missing_path, _plan_decisions())
                self.assertEqual(ctx.exception.code, "source_path")
                mock_connect.assert_not_called()


class PreflightMalformedPathTests(unittest.TestCase):
    def test_malformed_source_path_raises_generic_preflight_error(self):
        bad_paths = (None, 123, "\x00bad")
        for bad_path in bad_paths:
            with self.subTest(bad_path=bad_path):
                with mock.patch("sqlite3.connect") as mock_connect:
                    with self.assertRaises(pam.PreflightError) as ctx:
                        pam.preflight(bad_path)
                    message = str(ctx.exception)
                    self.assertEqual(message, pam._GENERIC_ERROR)
                    self.assertNotIn(repr(bad_path), message)
                    mock_connect.assert_not_called()

    def test_nonexistent_source_path_raises_generic_preflight_error(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            missing_path = os.path.join(tmpdir, "does_not_exist.sqlite3")
            with mock.patch("sqlite3.connect") as mock_connect:
                with self.assertRaises(pam.PreflightError) as ctx:
                    pam.preflight(missing_path)
                self.assertEqual(str(ctx.exception), pam._GENERIC_ERROR)
                mock_connect.assert_not_called()


class GuardedBackupTests(unittest.TestCase):
    def test_successful_backup_copies_all_data_and_leaves_source_untouched(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            source_path = os.path.join(tmpdir, "source.sqlite3")
            _build_full_db(source_path)
            source_hash_before = _file_sha256(source_path)
            files_before = set(os.listdir(tmpdir))

            target_path = os.path.join(tmpdir, "目标 库 (1).sqlite3")
            resolved_source, resolved_target = pam._guard_copy_paths(
                source_path, target_path
            )
            temp_path = pam._backup_source_to_temp(resolved_source, resolved_target)

            try:
                self.assertFalse(resolved_target.exists())
                self.assertEqual(temp_path.parent, resolved_target.parent)
                self.assertNotEqual(temp_path, resolved_target)
                mode = os.stat(temp_path).st_mode & 0o777
                self.assertEqual(mode & ~0o600, 0)

                src_con = sqlite3.connect(source_path)
                dst_con = sqlite3.connect(str(temp_path))
                try:
                    for table in FIXED_TABLES:
                        src_rows = src_con.execute(
                            f"SELECT * FROM {table} ORDER BY rowid"
                        ).fetchall()
                        dst_rows = dst_con.execute(
                            f"SELECT * FROM {table} ORDER BY rowid"
                        ).fetchall()
                        self.assertEqual(src_rows, dst_rows)
                finally:
                    src_con.close()
                    dst_con.close()

                self.assertEqual(_file_sha256(source_path), source_hash_before)
                self.assertEqual(set(os.listdir(tmpdir)) - files_before, {temp_path.name})
            finally:
                pam._remove_sqlite_artifacts(temp_path)

    def test_source_equals_target_raises_source_target_same(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            source_path = os.path.join(tmpdir, "same.sqlite3")
            _build_full_db(source_path)
            with self.assertRaises(pam.MigrationDecisionError) as ctx:
                pam._guard_copy_paths(source_path, source_path)
            self.assertEqual(ctx.exception.code, "source_target_same")
            self.assertNotIn(source_path, str(ctx.exception))
            self.assertNotIn(SECRET_MARKER, str(ctx.exception))

    def test_source_equals_target_after_normalization_raises_source_target_same(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            source_path = os.path.join(tmpdir, "same.sqlite3")
            _build_full_db(source_path)
            aliased = os.path.join(tmpdir, ".", "same.sqlite3")
            with self.assertRaises(pam.MigrationDecisionError) as ctx:
                pam._guard_copy_paths(source_path, aliased)
            self.assertEqual(ctx.exception.code, "source_target_same")

    def test_target_existing_plain_file_raises_target_exists(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            source_path = os.path.join(tmpdir, "source.sqlite3")
            _build_full_db(source_path)
            target_path = os.path.join(tmpdir, "existing.sqlite3")
            Path(target_path).write_text("x")
            with self.assertRaises(pam.MigrationDecisionError) as ctx:
                pam._guard_copy_paths(source_path, target_path)
            self.assertEqual(ctx.exception.code, "target_exists")

    def test_target_existing_directory_raises_target_exists(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            source_path = os.path.join(tmpdir, "source.sqlite3")
            _build_full_db(source_path)
            target_path = os.path.join(tmpdir, "existing_dir")
            os.mkdir(target_path)
            with self.assertRaises(pam.MigrationDecisionError) as ctx:
                pam._guard_copy_paths(source_path, target_path)
            self.assertEqual(ctx.exception.code, "target_exists")

    def test_target_existing_symlink_raises_target_exists(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            source_path = os.path.join(tmpdir, "source.sqlite3")
            _build_full_db(source_path)
            link_target = os.path.join(tmpdir, "link_target.sqlite3")
            Path(link_target).write_text("x")
            target_path = os.path.join(tmpdir, "link.sqlite3")
            os.symlink(link_target, target_path)
            with self.assertRaises(pam.MigrationDecisionError) as ctx:
                pam._guard_copy_paths(source_path, target_path)
            self.assertEqual(ctx.exception.code, "target_exists")

    def test_target_broken_symlink_raises_target_exists(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            source_path = os.path.join(tmpdir, "source.sqlite3")
            _build_full_db(source_path)
            target_path = os.path.join(tmpdir, "broken_link.sqlite3")
            os.symlink(os.path.join(tmpdir, "does_not_exist.sqlite3"), target_path)
            with self.assertRaises(pam.MigrationDecisionError) as ctx:
                pam._guard_copy_paths(source_path, target_path)
            self.assertEqual(ctx.exception.code, "target_exists")

    def test_default_source_and_default_target_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            source_path = os.path.join(tmpdir, "source.sqlite3")
            _build_full_db(source_path)
            default_db = os.path.join(tmpdir, "default.sqlite3")
            _build_full_db(default_db)
            with mock.patch.object(pam, "DEFAULT_DB_PATH", Path(default_db)):
                with self.assertRaises(pam.MigrationDecisionError) as ctx:
                    pam._guard_copy_paths(source_path, default_db)
                self.assertEqual(ctx.exception.code, "target_path")

            with mock.patch.object(pam, "DEFAULT_DB_PATH", Path(default_db)):
                target_path = os.path.join(tmpdir, "target.sqlite3")
                with self.assertRaises(pam.MigrationDecisionError) as ctx:
                    pam._guard_copy_paths(default_db, target_path)
                self.assertEqual(ctx.exception.code, "source_path")

    def test_target_parent_missing_or_not_directory_raises_target_path(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            source_path = os.path.join(tmpdir, "source.sqlite3")
            _build_full_db(source_path)

            missing_parent_target = os.path.join(tmpdir, "no_such_dir", "t.sqlite3")
            with self.assertRaises(pam.MigrationDecisionError) as ctx:
                pam._guard_copy_paths(source_path, missing_parent_target)
            self.assertEqual(ctx.exception.code, "target_path")

            file_as_parent = os.path.join(tmpdir, "not_a_dir")
            Path(file_as_parent).write_text("x")
            bad_parent_target = os.path.join(file_as_parent, "t.sqlite3")
            with self.assertRaises(pam.MigrationDecisionError) as ctx:
                pam._guard_copy_paths(source_path, bad_parent_target)
            self.assertEqual(ctx.exception.code, "target_path")

    def test_malformed_target_paths_raise_target_path_without_leaking(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            source_path = os.path.join(tmpdir, "source.sqlite3")
            _build_full_db(source_path)
            for bad_target in (None, 123, f"\x00bad_{SECRET_MARKER}"):
                with self.subTest(bad_target=bad_target):
                    with self.assertRaises(pam.MigrationDecisionError) as ctx:
                        pam._guard_copy_paths(source_path, bad_target)
                    self.assertEqual(ctx.exception.code, "target_path")
                    self.assertNotIn(SECRET_MARKER, str(ctx.exception))

    def test_nonexistent_or_corrupt_source_maps_to_source_path(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            missing_source = os.path.join(tmpdir, "missing.sqlite3")
            target_path = os.path.join(tmpdir, "t.sqlite3")
            with self.assertRaises(pam.MigrationDecisionError) as ctx:
                pam._guard_copy_paths(missing_source, target_path)
            self.assertEqual(ctx.exception.code, "source_path")

    def test_backup_failure_leaves_no_temp_file_and_source_untouched(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            source_path = os.path.join(tmpdir, "source.sqlite3")
            _build_full_db(source_path)
            source_hash_before = _file_sha256(source_path)
            target_path = os.path.join(tmpdir, "target.sqlite3")

            resolved_source, resolved_target = pam._guard_copy_paths(
                source_path, target_path
            )
            files_before = set(os.listdir(tmpdir))

            real_connect = sqlite3.connect
            source_uri_prefix = resolved_source.as_uri()

            def _connect_then_close_dest(path, *args, **kwargs):
                con = real_connect(path, *args, **kwargs)
                if not str(path).startswith(source_uri_prefix):
                    con.close()
                return con

            with mock.patch("sqlite3.connect", side_effect=_connect_then_close_dest):
                with self.assertRaises(pam.MigrationDecisionError) as ctx:
                    pam._backup_source_to_temp(resolved_source, resolved_target)
                self.assertEqual(ctx.exception.code, "backup_failed")

            self.assertEqual(set(os.listdir(tmpdir)), files_before)
            self.assertFalse(resolved_target.exists())
            self.assertEqual(_file_sha256(source_path), source_hash_before)

    def test_backup_connect_failure_leaves_no_temp_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            source_path = os.path.join(tmpdir, "source.sqlite3")
            _build_full_db(source_path)
            source_hash_before = _file_sha256(source_path)
            target_path = os.path.join(tmpdir, "target.sqlite3")

            resolved_source, resolved_target = pam._guard_copy_paths(
                source_path, target_path
            )
            files_before = set(os.listdir(tmpdir))

            real_connect = sqlite3.connect

            def _flaky_connect(path, *args, **kwargs):
                if str(path) not in (str(resolved_source), f"{resolved_source.as_uri()}?mode=ro"):
                    raise sqlite3.Error("boom")
                return real_connect(path, *args, **kwargs)

            with mock.patch("sqlite3.connect", side_effect=_flaky_connect):
                with self.assertRaises(pam.MigrationDecisionError) as ctx:
                    pam._backup_source_to_temp(resolved_source, resolved_target)
                self.assertEqual(ctx.exception.code, "backup_failed")

            self.assertEqual(set(os.listdir(tmpdir)), files_before)
            self.assertFalse(resolved_target.exists())
            self.assertEqual(_file_sha256(source_path), source_hash_before)

    def test_backup_failure_closes_dest_then_source_before_cleanup(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            source_path = os.path.join(tmpdir, "source.sqlite3")
            _build_full_db(source_path)
            target_path = os.path.join(tmpdir, "target.sqlite3")

            resolved_source, resolved_target = pam._guard_copy_paths(
                source_path, target_path
            )

            events = []
            real_connect = sqlite3.connect
            source_uri_prefix = resolved_source.as_uri()

            class _TrackingConnection(sqlite3.Connection):
                def close(self):
                    is_source = str(self._label).startswith(source_uri_prefix)
                    events.append("source_close" if is_source else "dest_close")
                    super().close()

                def backup(self, target, *args, **kwargs):
                    raise sqlite3.Error("boom")

            def _tracking_connect(path, *args, **kwargs):
                con = real_connect(
                    path, *args, factory=_TrackingConnection, **kwargs
                )
                con._label = str(path)
                return con

            with mock.patch("sqlite3.connect", side_effect=_tracking_connect):
                with self.assertRaises(pam.MigrationDecisionError) as ctx:
                    pam._backup_source_to_temp(resolved_source, resolved_target)
                self.assertEqual(ctx.exception.code, "backup_failed")

            events.append("remove")
            self.assertEqual(events, ["dest_close", "source_close", "remove"])
            self.assertFalse(resolved_target.exists())

    def test_chmod_failure_after_mkstemp_leaves_no_temp_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            source_path = os.path.join(tmpdir, "source.sqlite3")
            _build_full_db(source_path)
            source_hash_before = _file_sha256(source_path)
            target_path = os.path.join(tmpdir, "target.sqlite3")

            resolved_source, resolved_target = pam._guard_copy_paths(
                source_path, target_path
            )
            files_before = set(os.listdir(tmpdir))

            with mock.patch("os.chmod", side_effect=OSError("boom")):
                with self.assertRaises(pam.MigrationDecisionError) as ctx:
                    pam._backup_source_to_temp(resolved_source, resolved_target)
                self.assertEqual(ctx.exception.code, "backup_failed")

            self.assertEqual(set(os.listdir(tmpdir)), files_before)
            self.assertFalse(resolved_target.exists())
            self.assertEqual(_file_sha256(source_path), source_hash_before)

    def test_remove_sqlite_artifacts_is_idempotent_on_missing_files(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            missing = Path(tmpdir) / "does_not_exist.sqlite3"
            pam._remove_sqlite_artifacts(missing)

    def test_transient_migrating_file_unlink_retries_after_backup_failure(self):
        """Dynamic regression for SQLite backup fails AND the first
        cleanup unlink of the `.migrating-*` temp main file hits a transient
        OSError; the retry must clear it, leaving no residue behind."""
        with tempfile.TemporaryDirectory() as tmpdir:
            source_path = os.path.join(tmpdir, "source.sqlite3")
            _build_full_db(source_path)
            source_hash_before = _file_sha256(source_path)
            target_path = os.path.join(tmpdir, "target.sqlite3")

            resolved_source, resolved_target = pam._guard_copy_paths(
                source_path, target_path
            )
            files_before = set(os.listdir(tmpdir))

            real_connect = sqlite3.connect
            real_unlink = Path.unlink
            temp_holder = {}
            attempts = {"count": 0}

            class _FailingBackupConnection(sqlite3.Connection):
                def backup(self, target, *args, **kwargs):
                    raise sqlite3.Error(f"boom_{SECRET_MARKER}")

            def _tracking_connect(path, *args, **kwargs):
                kwargs["factory"] = _FailingBackupConnection
                con = real_connect(path, *args, **kwargs)
                if not str(path).startswith(resolved_source.as_uri()):
                    temp_holder["path"] = Path(str(path))
                return con

            def _flaky_unlink(self_path, *args, **kwargs):
                if (
                    temp_holder.get("path") is not None
                    and self_path == temp_holder["path"]
                    and attempts["count"] == 0
                ):
                    attempts["count"] += 1
                    raise OSError("transient: resource temporarily busy")
                return real_unlink(self_path, *args, **kwargs)

            with mock.patch("sqlite3.connect", side_effect=_tracking_connect):
                with mock.patch.object(Path, "unlink", _flaky_unlink):
                    with self.assertRaises(pam.MigrationDecisionError) as ctx:
                        pam._backup_source_to_temp(resolved_source, resolved_target)
                    self.assertEqual(ctx.exception.code, "backup_failed")

            self.assertEqual(set(os.listdir(tmpdir)), files_before)
            self.assertFalse(resolved_target.exists())
            self.assertEqual(_file_sha256(source_path), source_hash_before)
            self.assertEqual(attempts["count"], 1)

    def test_mkstemp_failure_leaves_no_temp_file(self):
        """Dynamic regression for tempfile.mkstemp itself failing must
        surface as backup_failed without creating a target or temp file."""
        with tempfile.TemporaryDirectory() as tmpdir:
            source_path = os.path.join(tmpdir, "source.sqlite3")
            _build_full_db(source_path)
            source_hash_before = _file_sha256(source_path)
            target_path = os.path.join(tmpdir, "target.sqlite3")

            resolved_source, resolved_target = pam._guard_copy_paths(
                source_path, target_path
            )
            files_before = set(os.listdir(tmpdir))

            with mock.patch("tempfile.mkstemp", side_effect=OSError("boom")):
                with self.assertRaises(pam.MigrationDecisionError) as ctx:
                    pam._backup_source_to_temp(resolved_source, resolved_target)
                self.assertEqual(ctx.exception.code, "backup_failed")

            self.assertEqual(set(os.listdir(tmpdir)), files_before)
            self.assertFalse(resolved_target.exists())
            self.assertEqual(_file_sha256(source_path), source_hash_before)

    def test_dest_close_failure_after_successful_backup_raises_backup_failed(self):
        """Regression for a close() failure on either backup
        connection must be treated as backup_failed even when the backup
        body itself succeeded, cleaning up the temp file and leaving the
        source untouched."""
        with tempfile.TemporaryDirectory() as tmpdir:
            source_path = os.path.join(tmpdir, "source.sqlite3")
            _build_full_db(source_path)
            source_hash_before = _file_sha256(source_path)
            target_path = os.path.join(tmpdir, "target.sqlite3")

            resolved_source, resolved_target = pam._guard_copy_paths(
                source_path, target_path
            )
            files_before = set(os.listdir(tmpdir))

            real_connect = sqlite3.connect
            source_uri_prefix = resolved_source.as_uri()

            class _FailCloseConnection(sqlite3.Connection):
                def close(self):
                    super().close()
                    raise sqlite3.Error(f"boom_{SECRET_MARKER}")

            def _fake_connect(path, *args, **kwargs):
                if str(path).startswith(source_uri_prefix):
                    return real_connect(path, *args, **kwargs)
                kwargs["factory"] = _FailCloseConnection
                return real_connect(path, *args, **kwargs)

            with mock.patch("sqlite3.connect", side_effect=_fake_connect):
                with self.assertRaises(pam.MigrationDecisionError) as ctx:
                    pam._backup_source_to_temp(resolved_source, resolved_target)
                self.assertEqual(ctx.exception.code, "backup_failed")
                self.assertNotIn(SECRET_MARKER, str(ctx.exception))

            self.assertEqual(set(os.listdir(tmpdir)), files_before)
            self.assertFalse(resolved_target.exists())
            self.assertEqual(_file_sha256(source_path), source_hash_before)


_XFORM_TOKEN_DOCTOR = f"ROLE_{SECRET_MARKER}_doctor_tok"
_XFORM_TOKEN_NURSE = f"ROLE_{SECRET_MARKER}_nurse_tok"


def _build_transform_source_db(path):
    con = sqlite3.connect(path)
    try:
        con.executescript(
            """
            CREATE TABLE staff_members (
                staff_id INTEGER PRIMARY KEY,
                name TEXT, note TEXT, job_no TEXT, phone TEXT, sex TEXT,
                id_card TEXT, title TEXT, license_no TEXT, department TEXT,
                role TEXT, roles TEXT, active INTEGER,
                created_at TEXT, updated_at TEXT
            );
            CREATE TABLE users (
                id INTEGER PRIMARY KEY,
                username TEXT, display_name TEXT, password_hash TEXT,
                role TEXT, is_active INTEGER, staff_id INTEGER,
                created_at TEXT, updated_at TEXT
            );
            CREATE TABLE sessions (
                token TEXT, user_id INTEGER, created_at TEXT, expires_at TEXT
            );
            CREATE TABLE roles (
                role_key TEXT PRIMARY KEY, name TEXT, is_system INTEGER,
                sort INTEGER, created_at TEXT, updated_at TEXT
            );
            CREATE TABLE role_permissions (
                role_key TEXT, perm_key TEXT
            );
            CREATE TABLE audit_logs (
                audit_id INTEGER PRIMARY KEY, entity_type TEXT, entity_id TEXT,
                action TEXT, old_json TEXT, new_json TEXT, operator TEXT,
                created_at TEXT
            );
            CREATE TABLE sentinel_business_table (
                sentinel_id INTEGER PRIMARY KEY, value TEXT
            );
            """
        )
        for role_key in ("admin", "doctor", "nurse", "reception"):
            con.execute(
                "INSERT INTO roles(role_key, name, is_system, sort, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?)",
                (role_key, f"ROLE_{SECRET_MARKER}_{role_key}", 1, 0, "2026-01-01", "2026-01-01"),
            )
        for role_key, perm_key in (
            ("admin", "perm_all"),
            ("doctor", "perm_visit"),
            ("reception", "perm_front"),
        ):
            con.execute(
                "INSERT INTO role_permissions(role_key, perm_key) VALUES (?,?)",
                (role_key, perm_key),
            )
        staff_rows = [
            (1, f"NAME_{SECRET_MARKER}_1", "", "", "", "", "", "", "", "",
             _XFORM_TOKEN_DOCTOR, _XFORM_TOKEN_DOCTOR, 1, "2026-01-01", "2026-01-01"),
            (2, f"NAME_{SECRET_MARKER}_2", "", "", "", "", "", "", "", "",
             _XFORM_TOKEN_NURSE, _XFORM_TOKEN_NURSE, 0, "2026-01-01", "2026-01-01"),
            (3, f"NAME_{SECRET_MARKER}_3", "", "", "", "", "", "", "", "",
             _XFORM_TOKEN_DOCTOR, _XFORM_TOKEN_DOCTOR, 1, "2026-01-01", "2026-01-01"),
        ]
        for row in staff_rows:
            con.execute(
                "INSERT INTO staff_members(staff_id, name, note, job_no, phone, sex, id_card, "
                "title, license_no, department, role, roles, active, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                row,
            )
        user_rows = [
            # user 1: linked to staff1 (doctor, active), legacy role 'admin' (stale, needs ack),
            # IS in chosen regular-admin id set
            (1, f"USER_{SECRET_MARKER}_1", f"DISP_{SECRET_MARKER}_1", f"HASH_{SECRET_MARKER}_1",
             "admin", 1, 1, "2026-01-01", "2026-01-01"),
            # user 2: linked to staff2 (nurse, inactive) -> active user / inactive staff conflict
            (2, f"USER_{SECRET_MARKER}_2", f"DISP_{SECRET_MARKER}_2", f"HASH_{SECRET_MARKER}_2",
             "nurse", 1, 2, "2026-01-01", "2026-01-01"),
            # user 3: independent admin, no staff link, chosen regular admin
            (3, f"USER_{SECRET_MARKER}_3", f"DISP_{SECRET_MARKER}_3", f"HASH_{SECRET_MARKER}_3",
             "admin", 1, None, "2026-01-01", "2026-01-01"),
            # user 5: linked to staff3 (doctor, active), legacy role 'admin' (stale, needs ack),
            # NOT in chosen regular-admin id set -> must not be auto-elevated
            (5, f"USER_{SECRET_MARKER}_5", f"DISP_{SECRET_MARKER}_5", f"HASH_{SECRET_MARKER}_5",
             "admin", 1, 3, "2026-01-01", "2026-01-01"),
        ]
        for row in user_rows:
            con.execute(
                "INSERT INTO users(id, username, display_name, password_hash, role, is_active, "
                "staff_id, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                row,
            )
        con.execute(
            "INSERT INTO sessions(token, user_id, created_at, expires_at) VALUES (?,?,?,?)",
            (f"TOKEN_{SECRET_MARKER}_1", 1, "2026-01-01", "2026-01-02"),
        )
        for i in range(3):
            con.execute(
                "INSERT INTO audit_logs(entity_type, entity_id, action, old_json, new_json, "
                "operator, created_at) VALUES (?,?,?,?,?,?,?)",
                (
                    f"ENTITY_{SECRET_MARKER}", str(i), "update", f"OLD_{SECRET_MARKER}_{i}",
                    f"NEW_{SECRET_MARKER}_{i}", f"OP_{SECRET_MARKER}_{i}", "2026-01-01",
                ),
            )
        con.execute(
            "INSERT INTO sentinel_business_table(sentinel_id, value) VALUES (1, ?)",
            (f"SENTINEL_{SECRET_MARKER}",),
        )
        con.commit()
    finally:
        con.close()


def _transform_decisions():
    return {
        "regular_system_admin_user_ids": [1, 3],
        "break_glass_account": {
            "id": "BG1",
            "username": f"bg_{SECRET_MARKER}",
            "display_name": f"应急_{SECRET_MARKER}",
            "password_hash": f"HASH_{SECRET_MARKER}_bg",
        },
        "staff_role_map": {
            _XFORM_TOKEN_DOCTOR: "doctor",
            _XFORM_TOKEN_NURSE: "nurse",
        },
        "acknowledged_legacy_user_role_user_ids": [1, 5],
        "status_conflict_resolutions": {2: "disable_user"},
    }


def _make_transformed_temp_copy(tmpdir, decisions=None):
    """Build a synthetic legacy DB, back it up to a temp copy and prepare a plan on it."""
    source_path = os.path.join(tmpdir, "legacy.sqlite3")
    _build_transform_source_db(source_path)
    target_path = os.path.join(tmpdir, "final_target.sqlite3")
    resolved_source, resolved_target = pam._guard_copy_paths(source_path, target_path)
    temp_path = pam._backup_source_to_temp(resolved_source, resolved_target)
    plan = pam._prepare_migration_plan(str(temp_path), decisions or _transform_decisions())
    return source_path, resolved_target, temp_path, plan


class TransformTemporaryCopyTests(unittest.TestCase):
    def _assert_bad_session_schema_fails_closed(self, bad_schema):
        with tempfile.TemporaryDirectory() as tmpdir:
            source_path, resolved_target, temp_path, plan = _make_transformed_temp_copy(
                tmpdir
            )
            source_hash_before = _file_sha256(source_path)
            temp_artifacts = (
                temp_path,
                *(Path(str(temp_path) + suffix) for suffix in ("-wal", "-shm", "-journal")),
            )

            try:
                with mock.patch.object(
                    pam,
                    "_validate_transformed_copy",
                    wraps=pam._validate_transformed_copy,
                ) as validate_transformed_copy:
                    with mock.patch.object(
                        pam, "load_personnel_access_schema_sql", return_value=bad_schema
                    ):
                        with self.assertRaises(pam.MigrationDecisionError) as ctx:
                            pam._transform_temporary_copy(temp_path, plan)
                    validate_transformed_copy.assert_called_once()
                self.assertEqual(ctx.exception.code, "transform_failed")
                self.assertFalse(resolved_target.exists())
                self.assertEqual(_file_sha256(source_path), source_hash_before)
            finally:
                pam._remove_sqlite_artifacts(temp_path)
                for artifact in temp_artifacts:
                    self.assertFalse(artifact.exists())

    def test_missing_reauthenticated_at_schema_fails_closed(self):
        self._assert_bad_session_schema_fails_closed(
            _schema_without_reauthenticated_at()
        )

    def test_extra_absolute_expires_at_schema_fails_closed(self):
        self._assert_bad_session_schema_fails_closed(
            _schema_with_absolute_expires_at()
        )

    def test_success_migrates_data_and_preserves_untouched_tables(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            source_path, resolved_target, temp_path, plan = _make_transformed_temp_copy(tmpdir)
            source_hash_before = _file_sha256(source_path)
            source_dir_before = set(os.listdir(tmpdir))

            try:
                result = pam._transform_temporary_copy(temp_path, plan)

                self.assertEqual(
                    result,
                    {
                        "version": 1,
                        "staff_migrated": 3,
                        "role_assignments_created": 3,
                        "users_migrated": 5,
                        "regular_system_admins": 2,
                        "break_glass_accounts": 1,
                        "sessions_invalidated": 1,
                        "audit_rows_preserved": 3,
                    },
                )
                for value in result.values():
                    self.assertIs(type(value), int)
                dumped = json.dumps(result, ensure_ascii=False)
                self.assertNotIn(SECRET_MARKER, dumped)

                con = sqlite3.connect(str(temp_path))
                try:
                    # staff status mapping
                    rows = {
                        r[0]: r
                        for r in con.execute(
                            "SELECT staff_id, employment_status, left_at, left_reason "
                            "FROM staff_members"
                        ).fetchall()
                    }
                    self.assertEqual(rows["1"][1], "employed")
                    self.assertIsNone(rows["1"][2])
                    self.assertEqual(rows["1"][3], "")
                    self.assertEqual(rows["2"][1], "left")
                    self.assertIsNone(rows["2"][2])
                    self.assertEqual(rows["2"][3], "")

                    # role assignments
                    assignments = set(
                        con.execute(
                            "SELECT staff_id, role_key, is_primary FROM staff_role_assignments"
                        ).fetchall()
                    )
                    self.assertEqual(
                        assignments,
                        {("1", "doctor", 1), ("2", "nurse", 1), ("3", "doctor", 1)},
                    )

                    # users mapping
                    users_by_id = {
                        r[0]: r
                        for r in con.execute(
                            "SELECT id, is_active, is_system_admin, account_kind "
                            "FROM users"
                        ).fetchall()
                    }
                    self.assertEqual(users_by_id["1"][1], 1)  # still active
                    self.assertEqual(users_by_id["1"][2], 1)  # user1 chosen admin -> is_system_admin
                    self.assertEqual(users_by_id["1"][3], "staff")
                    self.assertEqual(users_by_id["2"][1], 0)  # disabled by conflict resolution
                    self.assertEqual(users_by_id["3"][2], 1)  # independent admin
                    self.assertEqual(users_by_id["3"][3], "independent_admin")
                    # user5: stale legacy role='admin' but NOT in chosen admin id set
                    self.assertEqual(users_by_id["5"][2], 0)
                    self.assertEqual(users_by_id["5"][3], "staff")

                    # break-glass account
                    bg_rows = con.execute(
                        "SELECT id, staff_id, is_active, is_system_admin, account_kind, "
                        "password_hash FROM users WHERE account_kind='break_glass'"
                    ).fetchall()
                    self.assertEqual(len(bg_rows), 1)
                    bg = bg_rows[0]
                    self.assertIsNone(bg[1])
                    self.assertEqual(bg[2], 1)
                    self.assertEqual(bg[3], 1)
                    self.assertEqual(bg[5], f"HASH_{SECRET_MARKER}_bg")

                    # sessions empty, no token column
                    self.assertEqual(
                        con.execute("SELECT COUNT(*) FROM sessions").fetchone()[0], 0
                    )
                    session_cols = _table_columns(con, "sessions")
                    self.assertNotIn("token", session_cols)
                    self.assertIn("token_hash", session_cols)

                    # audit logs: old 8 fields preserved, new cols NULL
                    audit_rows = con.execute(
                        "SELECT entity_type, entity_id, action, old_json, new_json, operator, "
                        "created_at, actor_user_id, result, reason, risk_level, request_id, "
                        "ip_address, user_agent FROM audit_logs ORDER BY entity_id"
                    ).fetchall()
                    self.assertEqual(len(audit_rows), 3)
                    for i, row in enumerate(audit_rows):
                        self.assertEqual(row[0], f"ENTITY_{SECRET_MARKER}")
                        self.assertEqual(row[1], str(i))
                        self.assertEqual(row[3], f"OLD_{SECRET_MARKER}_{i}")
                        self.assertEqual(row[4], f"NEW_{SECRET_MARKER}_{i}")
                        self.assertEqual(row[5], f"OP_{SECRET_MARKER}_{i}")
                        for new_field in row[7:]:
                            self.assertIsNone(new_field)

                    # admin role + its permissions gone
                    self.assertEqual(
                        con.execute(
                            "SELECT COUNT(*) FROM roles WHERE role_key='admin'"
                        ).fetchone()[0],
                        0,
                    )
                    self.assertEqual(
                        con.execute(
                            "SELECT COUNT(*) FROM role_permissions WHERE role_key='admin'"
                        ).fetchone()[0],
                        0,
                    )

                    # non-admin roles/permissions survive untouched
                    remaining_roles = set(
                        con.execute("SELECT role_key, name FROM roles").fetchall()
                    )
                    self.assertEqual(
                        remaining_roles,
                        {
                            ("doctor", f"ROLE_{SECRET_MARKER}_doctor"),
                            ("nurse", f"ROLE_{SECRET_MARKER}_nurse"),
                            ("reception", f"ROLE_{SECRET_MARKER}_reception"),
                        },
                    )
                    remaining_perms = set(
                        con.execute(
                            "SELECT role_key, perm_key FROM role_permissions"
                        ).fetchall()
                    )
                    self.assertEqual(
                        remaining_perms,
                        {("doctor", "perm_visit"), ("reception", "perm_front")},
                    )

                    # sentinel table untouched
                    sentinel_rows = con.execute(
                        "SELECT sentinel_id, value FROM sentinel_business_table"
                    ).fetchall()
                    self.assertEqual(sentinel_rows, [(1, f"SENTINEL_{SECRET_MARKER}")])
                finally:
                    con.close()

                # final target path never created by this slice
                self.assertFalse(resolved_target.exists())

                # source file untouched
                self.assertEqual(_file_sha256(source_path), source_hash_before)
                self.assertEqual(set(os.listdir(tmpdir)), source_dir_before)
            finally:
                pam._remove_sqlite_artifacts(temp_path)

    def test_ddl_failure_rolls_back_and_leaves_legacy_tables_intact(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            _, _, temp_path, plan = _make_transformed_temp_copy(tmpdir)
            try:
                with mock.patch.object(
                    pam, "load_personnel_access_schema_sql",
                    return_value="CREATE TABLE broken_ddl_missing_paren(id text",
                ):
                    with self.assertRaises(pam.MigrationDecisionError) as ctx:
                        pam._transform_temporary_copy(temp_path, plan)
                    self.assertEqual(ctx.exception.code, "transform_failed")

                con = sqlite3.connect(str(temp_path))
                try:
                    cols = _table_columns(con, "staff_members")
                    self.assertIn("role", cols)
                    self.assertIn("roles", cols)
                    self.assertIn("active", cols)
                    self.assertEqual(
                        con.execute("SELECT COUNT(*) FROM staff_members").fetchone()[0], 3
                    )
                    self.assertEqual(
                        con.execute(
                            "SELECT COUNT(*) FROM sentinel_business_table"
                        ).fetchone()[0],
                        1,
                    )
                    self.assertEqual(
                        con.execute(
                            "SELECT COUNT(*) FROM roles WHERE role_key='admin'"
                        ).fetchone()[0],
                        1,
                    )
                finally:
                    con.close()
            finally:
                pam._remove_sqlite_artifacts(temp_path)

    def test_bad_insert_null_name_rolls_back(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            _, _, temp_path, plan = _make_transformed_temp_copy(tmpdir)
            try:
                plan["source"]["staff"]["1"]["name"] = None
                with self.assertRaises(pam.MigrationDecisionError) as ctx:
                    pam._transform_temporary_copy(temp_path, plan)
                self.assertEqual(ctx.exception.code, "transform_failed")

                con = sqlite3.connect(str(temp_path))
                try:
                    cols = _table_columns(con, "users")
                    self.assertIn("role", cols)
                    self.assertEqual(
                        con.execute("SELECT COUNT(*) FROM users").fetchone()[0], 4
                    )
                    self.assertEqual(
                        con.execute(
                            "SELECT COUNT(*) FROM sentinel_business_table"
                        ).fetchone()[0],
                        1,
                    )
                finally:
                    con.close()
            finally:
                pam._remove_sqlite_artifacts(temp_path)

    def test_validation_trap_no_active_admin_rolls_back(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            _, _, temp_path, plan = _make_transformed_temp_copy(tmpdir)
            try:
                # corrupt the plan so no user ends up flagged as a regular system admin
                plan["account_plan"]["regular_admin_ids"] = frozenset()
                with self.assertRaises(pam.MigrationDecisionError) as ctx:
                    pam._transform_temporary_copy(temp_path, plan)
                self.assertEqual(ctx.exception.code, "transform_failed")

                con = sqlite3.connect(str(temp_path))
                try:
                    self.assertIn("active", _table_columns(con, "staff_members"))
                    self.assertEqual(
                        con.execute(
                            "SELECT COUNT(*) FROM roles WHERE role_key='admin'"
                        ).fetchone()[0],
                        1,
                    )
                finally:
                    con.close()
            finally:
                pam._remove_sqlite_artifacts(temp_path)

    def test_connect_failure_raises_transform_failed_without_leaking_error(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            _, _, temp_path, plan = _make_transformed_temp_copy(tmpdir)
            try:
                with mock.patch.object(
                    pam.sqlite3, "connect",
                    side_effect=sqlite3.OperationalError(f"boom_{SECRET_MARKER}"),
                ):
                    with self.assertRaises(pam.MigrationDecisionError) as ctx:
                        pam._transform_temporary_copy(temp_path, plan)
                self.assertEqual(ctx.exception.code, "transform_failed")
                self.assertNotIn(SECRET_MARKER, str(ctx.exception))
                self.assertNotIn(SECRET_MARKER, repr(ctx.exception))
                self.assertNotIn(str(temp_path), str(ctx.exception))
            finally:
                pam._remove_sqlite_artifacts(temp_path)

    def test_legacy_audit_read_failure_raises_transform_failed_and_closes(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            _, _, temp_path, plan = _make_transformed_temp_copy(tmpdir)
            try:
                def make_connection(trigger_prefix):
                    class TrappedConnection(sqlite3.Connection):
                        def execute(self, sql, *args, **kwargs):
                            if sql.strip().upper().startswith(trigger_prefix):
                                raise sqlite3.OperationalError(f"boom_{SECRET_MARKER}")
                            return super().execute(sql, *args, **kwargs)

                    return sqlite3.connect(
                        str(temp_path), isolation_level=None, factory=TrappedConnection
                    )

                real_con = make_connection("SELECT")

                with mock.patch.object(pam.sqlite3, "connect", return_value=real_con):
                    with self.assertRaises(pam.MigrationDecisionError) as ctx:
                        pam._transform_temporary_copy(temp_path, plan)
                self.assertEqual(ctx.exception.code, "transform_failed")
                self.assertNotIn(SECRET_MARKER, str(ctx.exception))
                self.assertNotIn(SECRET_MARKER, repr(ctx.exception))
                self.assertNotIn(str(temp_path), str(ctx.exception))

                with self.assertRaises(sqlite3.ProgrammingError):
                    sqlite3.Connection.execute(real_con, "SELECT 1")
            finally:
                pam._remove_sqlite_artifacts(temp_path)

    def test_vacuum_failure_after_commit_raises_transform_failed_and_closes(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            _, _, temp_path, plan = _make_transformed_temp_copy(tmpdir)
            try:
                class TrappedConnection(sqlite3.Connection):
                    def execute(self, sql, *args, **kwargs):
                        if sql.strip().upper().startswith("VACUUM"):
                            raise sqlite3.OperationalError(f"boom_{SECRET_MARKER}")
                        return super().execute(sql, *args, **kwargs)

                real_con = sqlite3.connect(
                    str(temp_path), isolation_level=None, factory=TrappedConnection
                )

                with mock.patch.object(pam.sqlite3, "connect", return_value=real_con):
                    with self.assertRaises(pam.MigrationDecisionError) as ctx:
                        pam._transform_temporary_copy(temp_path, plan)
                self.assertEqual(ctx.exception.code, "transform_failed")
                self.assertNotIn(SECRET_MARKER, str(ctx.exception))
                self.assertNotIn(SECRET_MARKER, repr(ctx.exception))
                self.assertNotIn(str(temp_path), str(ctx.exception))

                with self.assertRaises(sqlite3.ProgrammingError):
                    sqlite3.Connection.execute(real_con, "SELECT 1")
            finally:
                pam._remove_sqlite_artifacts(temp_path)

    def test_close_failure_after_successful_transform_raises_transform_failed(self):
        """Regression for a close() failure on the transform
        connection after an otherwise fully successful transform must still
        surface as transform_failed, not be reported as success."""
        with tempfile.TemporaryDirectory() as tmpdir:
            source_path, _, temp_path, plan = _make_transformed_temp_copy(tmpdir)
            source_hash_before = _file_sha256(source_path)
            try:
                class FailCloseConnection(sqlite3.Connection):
                    def close(self):
                        super().close()
                        raise sqlite3.Error(f"boom_{SECRET_MARKER}")

                real_con = sqlite3.connect(
                    str(temp_path), isolation_level=None, factory=FailCloseConnection
                )

                with mock.patch.object(pam.sqlite3, "connect", return_value=real_con):
                    with self.assertRaises(pam.MigrationDecisionError) as ctx:
                        pam._transform_temporary_copy(temp_path, plan)
                self.assertEqual(ctx.exception.code, "transform_failed")
                self.assertNotIn(SECRET_MARKER, str(ctx.exception))
                self.assertNotIn(SECRET_MARKER, repr(ctx.exception))

                self.assertEqual(_file_sha256(source_path), source_hash_before)
            finally:
                pam._remove_sqlite_artifacts(temp_path)

    def test_execute_schema_ddl_does_not_implicitly_commit(self):
        con = sqlite3.connect(":memory:", isolation_level=None)
        try:
            con.execute("BEGIN")
            pam._execute_schema_ddl(con, "CREATE TABLE scratch_probe(id text);")
            self.assertIn(
                "scratch_probe",
                {
                    r[0] for r in con.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    ).fetchall()
                },
            )
            con.execute("ROLLBACK")
            self.assertNotIn(
                "scratch_probe",
                {
                    r[0] for r in con.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    ).fetchall()
                },
            )
        finally:
            con.close()

    def test_execute_schema_ddl_raises_on_trailing_incomplete_statement(self):
        con = sqlite3.connect(":memory:")
        try:
            with self.assertRaises(pam.MigrationDecisionError) as ctx:
                pam._execute_schema_ddl(con, "CREATE TABLE ok(id text); CREATE TABLE bad(id")
            self.assertEqual(ctx.exception.code, "transform_failed")
        finally:
            con.close()


class BuildMigratedCopySessionSchemaValidationTests(unittest.TestCase):
    def _assert_bad_session_schema_fails_closed(self, bad_schema):
        with tempfile.TemporaryDirectory() as tmpdir:
            source_path = os.path.join(tmpdir, "legacy.sqlite3")
            _build_transform_source_db(source_path)
            source_hash_before = _file_sha256(source_path)
            target_path = os.path.join(tmpdir, "final.sqlite3")
            files_before = set(os.listdir(tmpdir))

            with mock.patch.object(
                pam,
                "_validate_transformed_copy",
                wraps=pam._validate_transformed_copy,
            ) as validate_transformed_copy:
                with mock.patch.object(
                    pam, "load_personnel_access_schema_sql", return_value=bad_schema
                ):
                    with self.assertRaises(pam.MigrationDecisionError) as ctx:
                        pam.build_migrated_copy(
                            source_path, target_path, _transform_decisions()
                        )
                validate_transformed_copy.assert_called_once()

            self.assertEqual(ctx.exception.code, "transform_failed")
            self.assertFalse(os.path.exists(target_path))
            self.assertEqual(_file_sha256(source_path), source_hash_before)
            self.assertEqual(set(os.listdir(tmpdir)), files_before)

    def test_missing_reauthenticated_at_schema_fails_closed(self):
        self._assert_bad_session_schema_fails_closed(
            _schema_without_reauthenticated_at()
        )

    def test_extra_absolute_expires_at_schema_fails_closed(self):
        self._assert_bad_session_schema_fails_closed(
            _schema_with_absolute_expires_at()
        )


class BuildMigratedCopyTests(unittest.TestCase):
    def test_signature_has_three_required_params_no_defaults(self):
        sig = inspect.signature(pam.build_migrated_copy)
        params = list(sig.parameters.values())
        self.assertEqual([p.name for p in params], ["source_db_path", "target_db_path", "decisions"])
        for p in params:
            self.assertIs(p.default, inspect.Parameter.empty)

    def test_success_publishes_atomically_with_exact_summary(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            source_path = os.path.join(tmpdir, "legacy.sqlite3")
            _build_transform_source_db(source_path)
            source_hash_before = _file_sha256(source_path)
            target_path = os.path.join(tmpdir, "final.sqlite3")
            files_before = set(os.listdir(tmpdir))

            result = pam.build_migrated_copy(source_path, target_path, _transform_decisions())

            self.assertEqual(
                result,
                {
                    "version": 1,
                    "staff_migrated": 3,
                    "role_assignments_created": 3,
                    "users_migrated": 5,
                    "regular_system_admins": 2,
                    "break_glass_accounts": 1,
                    "sessions_invalidated": 1,
                    "audit_rows_preserved": 3,
                },
            )
            dumped = json.dumps(result, ensure_ascii=False)
            self.assertNotIn(SECRET_MARKER, dumped)
            for key in ("path", "id", "username", "token", "hash"):
                self.assertNotIn(key, result)

            self.assertTrue(os.path.exists(target_path))
            mode = os.stat(target_path).st_mode & 0o777
            self.assertEqual(mode & ~0o600, 0)

            files_after = set(os.listdir(tmpdir))
            self.assertEqual(files_after - files_before, {"final.sqlite3"})
            for name in files_after:
                self.assertNotIn(".migrating-", name)

            self.assertEqual(_file_sha256(source_path), source_hash_before)
            src_con = sqlite3.connect(source_path)
            try:
                self.assertEqual(
                    src_con.execute("SELECT COUNT(*) FROM staff_members").fetchone()[0], 3
                )
                self.assertEqual(
                    src_con.execute("SELECT COUNT(*) FROM users").fetchone()[0], 4
                )
            finally:
                src_con.close()

            con = sqlite3.connect(target_path)
            try:
                self.assertEqual(
                    con.execute(
                        "SELECT value FROM sentinel_business_table WHERE sentinel_id=1"
                    ).fetchone()[0],
                    f"SENTINEL_{SECRET_MARKER}",
                )
                self.assertEqual(con.execute("SELECT COUNT(*) FROM sessions").fetchone()[0], 0)
                self.assertEqual(
                    con.execute(
                        "SELECT COUNT(*) FROM users WHERE account_kind='break_glass' "
                        "AND is_active=1 AND is_system_admin=1"
                    ).fetchone()[0],
                    1,
                )
                self.assertEqual(
                    con.execute(
                        "SELECT COUNT(*) FROM users WHERE is_system_admin=1 AND account_kind != 'break_glass'"
                    ).fetchone()[0],
                    2,
                )
                self.assertEqual(
                    con.execute("SELECT COUNT(*) FROM staff_role_assignments").fetchone()[0], 3
                )
                self.assertEqual(con.execute("SELECT COUNT(*) FROM audit_logs").fetchone()[0], 3)
                self.assertEqual(
                    con.execute("SELECT COUNT(*) FROM roles WHERE role_key='admin'").fetchone()[0],
                    0,
                )
            finally:
                con.close()

    def test_legacy_throttle_tables_are_not_target_markers_and_get_dropped(self):
        """A legacy source carrying login_throttle_* must still migrate, and the
        published target must not inherit those tables via the SQLite backup."""
        with tempfile.TemporaryDirectory() as tmpdir:
            source_path = os.path.join(tmpdir, "legacy.sqlite3")
            _build_transform_source_db(source_path)

            con = sqlite3.connect(source_path)
            try:
                con.execute(
                    "CREATE TABLE login_throttle_accounts ("
                    "username_normalized TEXT PRIMARY KEY, failed_count INTEGER)"
                )
                con.execute(
                    "CREATE TABLE login_throttle_sources ("
                    "source_hash TEXT PRIMARY KEY, failed_count INTEGER)"
                )
                con.execute(
                    "INSERT INTO login_throttle_accounts VALUES (?, 3)",
                    (f"THROTTLEUSER_{SECRET_MARKER}",),
                )
                con.execute(
                    "INSERT INTO login_throttle_sources VALUES (?, 5)",
                    (f"THROTTLEHASH_{SECRET_MARKER}",),
                )
                con.commit()
            finally:
                con.close()

            source_hash_before = _file_sha256(source_path)
            target_path = os.path.join(tmpdir, "final.sqlite3")

            # Must NOT be rejected as source_already_target: throttle tables are
            # obsolete leftovers, not part of the target model any more.
            result = pam.build_migrated_copy(
                source_path, target_path, _transform_decisions()
            )
            self.assertEqual(result["version"], 1)

            self.assertEqual(_file_sha256(source_path), source_hash_before)
            src_con = sqlite3.connect(source_path)
            try:
                self.assertEqual(
                    src_con.execute(
                        "SELECT COUNT(*) FROM login_throttle_accounts"
                    ).fetchone()[0],
                    1,
                )
            finally:
                src_con.close()

            con = sqlite3.connect(target_path)
            try:
                existing = {
                    row[0]
                    for row in con.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    ).fetchall()
                }
                for table in OBSOLETE_THROTTLE_TABLES:
                    self.assertNotIn(table, existing)
                # unrelated business data still rides along untouched
                self.assertEqual(
                    con.execute(
                        "SELECT value FROM sentinel_business_table WHERE sentinel_id=1"
                    ).fetchone()[0],
                    f"SENTINEL_{SECRET_MARKER}",
                )
            finally:
                con.close()

            with open(target_path, "rb") as f:
                target_bytes = f.read()
            self.assertNotIn(
                f"THROTTLEUSER_{SECRET_MARKER}".encode("utf-8"), target_bytes
            )
            self.assertNotIn(
                f"THROTTLEHASH_{SECRET_MARKER}".encode("utf-8"), target_bytes
            )

    def test_plaintext_session_tokens_purged_from_target_raw_file_bytes(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            source_path = os.path.join(tmpdir, "legacy.sqlite3")
            _build_transform_source_db(source_path)

            token_a = "RAWPURGE_TOKEN_A_" + ("a1b2c3d4" * 8)
            token_b = "RAWPURGE_TOKEN_B_" + ("e5f6a7b8" * 8)
            con = sqlite3.connect(source_path)
            try:
                con.execute(
                    "INSERT INTO sessions(token, user_id, created_at, expires_at) "
                    "VALUES (?,?,?,?)",
                    (token_a, 1, "2026-01-01", "2026-01-02"),
                )
                con.execute(
                    "INSERT INTO sessions(token, user_id, created_at, expires_at) "
                    "VALUES (?,?,?,?)",
                    (token_b, 2, "2026-01-01", "2026-01-02"),
                )
                con.commit()
            finally:
                con.close()

            with open(source_path, "rb") as f:
                source_bytes = f.read()
            self.assertIn(token_a.encode("utf-8"), source_bytes)
            self.assertIn(token_b.encode("utf-8"), source_bytes)

            source_hash_before = _file_sha256(source_path)
            target_path = os.path.join(tmpdir, "final.sqlite3")

            pam.build_migrated_copy(source_path, target_path, _transform_decisions())

            with open(target_path, "rb") as f:
                target_bytes = f.read()
            self.assertNotIn(token_a.encode("utf-8"), target_bytes)
            self.assertNotIn(token_b.encode("utf-8"), target_bytes)

            self.assertEqual(_file_sha256(source_path), source_hash_before)

            names = os.listdir(tmpdir)
            for name in names:
                self.assertNotRegex(name, r"\.migrating-|-(wal|shm|journal)$")

            con = sqlite3.connect(target_path)
            try:
                self.assertEqual(con.execute("SELECT COUNT(*) FROM sessions").fetchone()[0], 0)
                cols = {r[1] for r in con.execute("PRAGMA table_info(sessions)").fetchall()}
                self.assertNotIn("token", cols)
                self.assertIn("token_hash", cols)
                self.assertEqual(con.execute("PRAGMA quick_check").fetchone()[0], "ok")
            finally:
                con.close()

    def test_no_chmod_on_final_target_after_publish(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            source_path = os.path.join(tmpdir, "legacy.sqlite3")
            _build_transform_source_db(source_path)
            target_path = os.path.join(tmpdir, "final.sqlite3")
            resolved_target = str(Path(target_path).resolve(strict=False))

            real_chmod = os.chmod
            calls = []

            def _spy_chmod(path, mode):
                calls.append(str(path))
                if str(Path(path).resolve(strict=False)) == resolved_target:
                    raise OSError("boom: chmod on final target")
                return real_chmod(path, mode)

            with mock.patch("os.chmod", side_effect=_spy_chmod):
                result = pam.build_migrated_copy(
                    source_path, target_path, _transform_decisions()
                )

            self.assertTrue(os.path.exists(target_path))
            mode = os.stat(target_path).st_mode & 0o777
            self.assertEqual(mode & ~0o600, 0)
            self.assertTrue(calls)
            self.assertTrue(all(".migrating-" in c for c in calls))
            self.assertEqual(result["staff_migrated"], 3)

    def test_transform_runs_before_target_exists_then_publish_link_creates_it(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            source_path = os.path.join(tmpdir, "legacy.sqlite3")
            _build_transform_source_db(source_path)
            target_path = os.path.join(tmpdir, "final.sqlite3")

            real_transform = pam._transform_temporary_copy
            real_link = os.link
            events = []

            def _spy_transform(temp_path, plan):
                events.append("transform_start")
                self.assertFalse(os.path.exists(target_path))
                result = real_transform(temp_path, plan)
                events.append("transform_end")
                self.assertFalse(os.path.exists(target_path))
                return result

            def _spy_link(src, dst):
                events.append("publish")
                self.assertIn("transform_end", events)
                return real_link(src, dst)

            with mock.patch.object(pam, "_transform_temporary_copy", side_effect=_spy_transform):
                with mock.patch("os.link", side_effect=_spy_link):
                    pam.build_migrated_copy(source_path, target_path, _transform_decisions())

            self.assertEqual(events, ["transform_start", "transform_end", "publish"])
            self.assertTrue(os.path.exists(target_path))

    def test_invalid_decisions_fail_before_touching_filesystem(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            source_path = os.path.join(tmpdir, "legacy.sqlite3")
            _build_transform_source_db(source_path)
            target_path = os.path.join(tmpdir, "final.sqlite3")

            with mock.patch("sqlite3.connect") as mock_connect, \
                 mock.patch("tempfile.mkstemp") as mock_mkstemp:
                with self.assertRaises(pam.MigrationDecisionError) as ctx:
                    pam.build_migrated_copy(source_path, target_path, {"bad": True})
                self.assertEqual(ctx.exception.code, "decisions_shape")
                mock_connect.assert_not_called()
                mock_mkstemp.assert_not_called()

            self.assertFalse(os.path.exists(target_path))

    def test_source_equals_target_fails_with_stable_code(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            source_path = os.path.join(tmpdir, "legacy.sqlite3")
            _build_transform_source_db(source_path)
            with self.assertRaises(pam.MigrationDecisionError) as ctx:
                pam.build_migrated_copy(source_path, source_path, _transform_decisions())
            self.assertEqual(ctx.exception.code, "source_target_same")

    def test_target_already_exists_fails_without_touching_it(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            source_path = os.path.join(tmpdir, "legacy.sqlite3")
            _build_transform_source_db(source_path)
            target_path = os.path.join(tmpdir, "final.sqlite3")
            Path(target_path).write_text("preexisting")

            with self.assertRaises(pam.MigrationDecisionError) as ctx:
                pam.build_migrated_copy(source_path, target_path, _transform_decisions())
            self.assertEqual(ctx.exception.code, "target_exists")
            self.assertEqual(Path(target_path).read_text(), "preexisting")

    def test_default_target_path_fails_with_stable_code(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            source_path = os.path.join(tmpdir, "legacy.sqlite3")
            _build_transform_source_db(source_path)
            default_db = os.path.join(tmpdir, "default.sqlite3")
            _build_transform_source_db(default_db)
            with mock.patch.object(pam, "DEFAULT_DB_PATH", Path(default_db)):
                with self.assertRaises(pam.MigrationDecisionError) as ctx:
                    pam.build_migrated_copy(source_path, default_db, _transform_decisions())
                self.assertEqual(ctx.exception.code, "target_path")

    def test_bad_parent_directory_fails_with_stable_code(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            source_path = os.path.join(tmpdir, "legacy.sqlite3")
            _build_transform_source_db(source_path)
            bad_target = os.path.join(tmpdir, "no_such_dir", "final.sqlite3")
            with self.assertRaises(pam.MigrationDecisionError) as ctx:
                pam.build_migrated_copy(source_path, bad_target, _transform_decisions())
            self.assertEqual(ctx.exception.code, "target_path")

    def test_transform_failure_leaves_no_target_no_temp_source_unchanged(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            source_path = os.path.join(tmpdir, "legacy.sqlite3")
            _build_transform_source_db(source_path)
            source_hash_before = _file_sha256(source_path)
            target_path = os.path.join(tmpdir, "final.sqlite3")
            files_before = set(os.listdir(tmpdir))

            with mock.patch.object(
                pam,
                "_transform_temporary_copy",
                side_effect=pam.MigrationDecisionError("transform_failed"),
            ):
                with self.assertRaises(pam.MigrationDecisionError) as ctx:
                    pam.build_migrated_copy(source_path, target_path, _transform_decisions())
                self.assertEqual(ctx.exception.code, "transform_failed")

            self.assertFalse(os.path.exists(target_path))
            self.assertEqual(set(os.listdir(tmpdir)), files_before)
            self.assertEqual(_file_sha256(source_path), source_hash_before)

    def test_prepare_plan_failure_leaves_no_target_no_temp_source_unchanged(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            source_path = os.path.join(tmpdir, "legacy.sqlite3")
            _build_transform_source_db(source_path)
            source_hash_before = _file_sha256(source_path)
            target_path = os.path.join(tmpdir, "final.sqlite3")
            files_before = set(os.listdir(tmpdir))

            with mock.patch.object(
                pam,
                "_prepare_migration_plan",
                side_effect=pam.MigrationDecisionError("source_schema"),
            ):
                with self.assertRaises(pam.MigrationDecisionError) as ctx:
                    pam.build_migrated_copy(source_path, target_path, _transform_decisions())
                self.assertEqual(ctx.exception.code, "source_schema")

            self.assertFalse(os.path.exists(target_path))
            self.assertEqual(set(os.listdir(tmpdir)), files_before)
            self.assertEqual(_file_sha256(source_path), source_hash_before)

    def test_publish_failure_leaves_no_target_no_temp_source_unchanged(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            source_path = os.path.join(tmpdir, "legacy.sqlite3")
            _build_transform_source_db(source_path)
            source_hash_before = _file_sha256(source_path)
            target_path = os.path.join(tmpdir, "final.sqlite3")
            files_before = set(os.listdir(tmpdir))

            with mock.patch("os.link", side_effect=OSError("disk full")):
                with self.assertRaises(pam.MigrationDecisionError) as ctx:
                    pam.build_migrated_copy(source_path, target_path, _transform_decisions())
                self.assertEqual(ctx.exception.code, "publish_failed")

            self.assertFalse(os.path.exists(target_path))
            self.assertEqual(set(os.listdir(tmpdir)), files_before)
            self.assertEqual(_file_sha256(source_path), source_hash_before)

    def test_external_target_created_after_transform_wins_race_and_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            source_path = os.path.join(tmpdir, "legacy.sqlite3")
            _build_transform_source_db(source_path)
            source_hash_before = _file_sha256(source_path)
            target_path = os.path.join(tmpdir, "final.sqlite3")

            real_transform = pam._transform_temporary_copy

            def _racing_transform(temp_path, plan):
                result = real_transform(temp_path, plan)
                Path(target_path).write_text("external_race_winner")
                return result

            with mock.patch.object(pam, "_transform_temporary_copy", side_effect=_racing_transform):
                with self.assertRaises(pam.MigrationDecisionError) as ctx:
                    pam.build_migrated_copy(source_path, target_path, _transform_decisions())
                self.assertEqual(ctx.exception.code, "target_exists")

            self.assertEqual(Path(target_path).read_text(), "external_race_winner")
            self.assertFalse(
                any(".migrating-" in name for name in os.listdir(tmpdir))
            )
            self.assertEqual(_file_sha256(source_path), source_hash_before)

    def test_external_target_created_in_publish_syscall_window_wins_race(self):
        """Dynamic regression for an external target appears *inside* the
        publish primitive's own call (the narrowest possible window between the
        no-target check and the act of creating it), not merely after transform
        ends. A check-then-os.replace publish would silently clobber this file;
        the atomic no-clobber primitive must still refuse and leave it intact.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            source_path = os.path.join(tmpdir, "legacy.sqlite3")
            _build_transform_source_db(source_path)
            source_hash_before = _file_sha256(source_path)
            target_path = os.path.join(tmpdir, "final.sqlite3")

            real_link = os.link

            def _racing_link(src, dst):
                Path(dst).write_bytes(b"external_race_winner")
                return real_link(src, dst)

            with mock.patch("os.link", side_effect=_racing_link):
                with self.assertRaises(pam.MigrationDecisionError) as ctx:
                    pam.build_migrated_copy(source_path, target_path, _transform_decisions())
                self.assertEqual(ctx.exception.code, "target_exists")

            self.assertEqual(Path(target_path).read_bytes(), b"external_race_winner")
            names = os.listdir(tmpdir)
            self.assertFalse(any(".migrating-" in name for name in names))
            self.assertFalse(any(n.endswith(("-wal", "-shm", "-journal")) for n in names))
            self.assertEqual(_file_sha256(source_path), source_hash_before)

    def test_transient_sidecar_cleanup_failure_retries_then_publishes(self):
        """Dynamic regression for a transient OSError on the first
        pre-publish sidecar unlink attempt must be retried, not leaked as a
        raw OSError, and cleanup must finish before the target is published.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            source_path = os.path.join(tmpdir, "legacy.sqlite3")
            _build_transform_source_db(source_path)
            source_hash_before = _file_sha256(source_path)
            target_path = os.path.join(tmpdir, "final.sqlite3")

            real_transform = pam._transform_temporary_copy
            real_unlink = Path.unlink
            wal_holder = {}
            attempts = {"count": 0}

            def _spy_transform(temp_path, plan):
                result = real_transform(temp_path, plan)
                wal_path = Path(str(temp_path) + "-wal")
                wal_path.write_bytes(b"synthetic-wal")
                wal_holder["path"] = wal_path
                return result

            def _flaky_unlink(self_path, *args, **kwargs):
                if (
                    wal_holder.get("path") is not None
                    and self_path == wal_holder["path"]
                    and attempts["count"] == 0
                ):
                    attempts["count"] += 1
                    raise OSError("transient: resource temporarily busy")
                return real_unlink(self_path, *args, **kwargs)

            with mock.patch.object(pam, "_transform_temporary_copy", side_effect=_spy_transform):
                with mock.patch.object(Path, "unlink", _flaky_unlink):
                    result = pam.build_migrated_copy(
                        source_path, target_path, _transform_decisions()
                    )

            self.assertEqual(result["staff_migrated"], 3)
            self.assertTrue(os.path.exists(target_path))
            names = os.listdir(tmpdir)
            self.assertFalse(any(n.endswith(("-wal", "-shm", "-journal")) for n in names))
            self.assertFalse(any(".migrating-" in n for n in names))
            self.assertEqual(_file_sha256(source_path), source_hash_before)
            self.assertEqual(attempts["count"], 1)

    def test_persistent_sidecar_cleanup_failure_raises_cleanup_failed_without_publishing(self):
        """Dynamic regression for an unrecoverable pre-publish sidecar
        cleanup failure must raise a stable non-leaking cleanup_failed error and
        must never leave a published target behind.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            source_path = os.path.join(tmpdir, "legacy.sqlite3")
            _build_transform_source_db(source_path)
            source_hash_before = _file_sha256(source_path)
            target_path = os.path.join(tmpdir, "final.sqlite3")

            real_transform = pam._transform_temporary_copy
            real_unlink = Path.unlink
            wal_holder = {}

            def _spy_transform(temp_path, plan):
                result = real_transform(temp_path, plan)
                wal_path = Path(str(temp_path) + "-wal")
                wal_path.write_bytes(b"synthetic-wal-" + SECRET_MARKER.encode())
                wal_holder["path"] = wal_path
                return result

            def _always_fail_unlink(self_path, *args, **kwargs):
                if wal_holder.get("path") is not None and self_path == wal_holder["path"]:
                    raise OSError(f"permanent: device busy at {self_path}")
                return real_unlink(self_path, *args, **kwargs)

            with mock.patch.object(pam, "_transform_temporary_copy", side_effect=_spy_transform):
                with mock.patch.object(Path, "unlink", _always_fail_unlink):
                    with self.assertRaises(pam.MigrationDecisionError) as ctx:
                        pam.build_migrated_copy(source_path, target_path, _transform_decisions())
                    self.assertEqual(ctx.exception.code, "cleanup_failed")
                    self.assertNotIn(tmpdir, str(ctx.exception))
                    self.assertNotIn(tmpdir, repr(ctx.exception))
                    self.assertNotIn(SECRET_MARKER, str(ctx.exception))
                    self.assertNotIn(SECRET_MARKER, repr(ctx.exception))

            self.assertFalse(os.path.exists(target_path))
            self.assertEqual(_file_sha256(source_path), source_hash_before)

    def test_transient_main_file_unlink_failure_retries_then_publishes(self):
        """A transient OSError on the first post-link temp-file unlink must be
        retried, not swallowed by the finally-block cleanup, and must not
        block a successful publish."""
        with tempfile.TemporaryDirectory() as tmpdir:
            source_path = os.path.join(tmpdir, "legacy.sqlite3")
            _build_transform_source_db(source_path)
            source_hash_before = _file_sha256(source_path)
            target_path = os.path.join(tmpdir, "final.sqlite3")

            real_unlink = Path.unlink
            temp_holder = {}
            attempts = {"count": 0}

            real_link = os.link

            def _spy_link(src, dst, *args, **kwargs):
                temp_holder["path"] = Path(src)
                return real_link(src, dst, *args, **kwargs)

            def _flaky_unlink(self_path, *args, **kwargs):
                if (
                    temp_holder.get("path") is not None
                    and self_path == temp_holder["path"]
                    and attempts["count"] == 0
                ):
                    attempts["count"] += 1
                    raise OSError("transient: resource temporarily busy")
                return real_unlink(self_path, *args, **kwargs)

            with mock.patch("os.link", side_effect=_spy_link):
                with mock.patch.object(Path, "unlink", _flaky_unlink):
                    result = pam.build_migrated_copy(
                        source_path, target_path, _transform_decisions()
                    )

            self.assertEqual(result["staff_migrated"], 3)
            self.assertTrue(os.path.exists(target_path))
            names = os.listdir(tmpdir)
            self.assertFalse(any(".migrating-" in n for n in names))
            self.assertEqual(_file_sha256(source_path), source_hash_before)
            self.assertEqual(attempts["count"], 1)

    def test_persistent_main_file_unlink_failure_rolls_back_target_and_raises_cleanup_failed(self):
        """When the temp main file can never be unlinked after publish, the
        just-created target must be rolled back (verified same inode) and a
        stable non-leaking cleanup_failed must be raised; the source must be
        untouched."""
        with tempfile.TemporaryDirectory() as tmpdir:
            source_path = os.path.join(tmpdir, "legacy.sqlite3")
            _build_transform_source_db(source_path)
            source_hash_before = _file_sha256(source_path)
            target_path = os.path.join(tmpdir, "final.sqlite3")

            real_unlink = Path.unlink
            temp_holder = {}

            real_link = os.link

            def _spy_link(src, dst, *args, **kwargs):
                temp_holder["path"] = Path(src)
                return real_link(src, dst, *args, **kwargs)

            def _always_fail_unlink(self_path, *args, **kwargs):
                if temp_holder.get("path") is not None and self_path == temp_holder["path"]:
                    raise OSError(f"permanent: device busy at {self_path}")
                return real_unlink(self_path, *args, **kwargs)

            with mock.patch("os.link", side_effect=_spy_link):
                with mock.patch.object(Path, "unlink", _always_fail_unlink):
                    with self.assertRaises(pam.MigrationDecisionError) as ctx:
                        pam.build_migrated_copy(source_path, target_path, _transform_decisions())
                    self.assertEqual(ctx.exception.code, "cleanup_failed")
                    self.assertNotIn(tmpdir, str(ctx.exception))
                    self.assertNotIn(tmpdir, repr(ctx.exception))
                    self.assertNotIn(SECRET_MARKER, str(ctx.exception))
                    self.assertNotIn(SECRET_MARKER, repr(ctx.exception))

            self.assertFalse(os.path.exists(target_path))
            self.assertEqual(_file_sha256(source_path), source_hash_before)

    def test_persistent_main_file_unlink_failure_does_not_delete_external_replacement(self):
        """If an external process replaces the target's inode between our
        publish and the rollback attempt, the rollback must detect the inode
        mismatch and leave the external file untouched."""
        with tempfile.TemporaryDirectory() as tmpdir:
            source_path = os.path.join(tmpdir, "legacy.sqlite3")
            _build_transform_source_db(source_path)
            source_hash_before = _file_sha256(source_path)
            target_path = os.path.join(tmpdir, "final.sqlite3")

            real_unlink = Path.unlink
            temp_holder = {}

            real_link = os.link

            def _spy_link(src, dst, *args, **kwargs):
                temp_holder["path"] = Path(src)
                return real_link(src, dst, *args, **kwargs)

            def _always_fail_unlink(self_path, *args, **kwargs):
                if temp_holder.get("path") is not None and self_path == temp_holder["path"]:
                    if os.path.exists(target_path):
                        os.unlink(target_path)
                        with open(target_path, "wb") as fh:
                            fh.write(b"external-replacement")
                    raise OSError(f"permanent: device busy at {self_path}")
                return real_unlink(self_path, *args, **kwargs)

            with mock.patch("os.link", side_effect=_spy_link):
                with mock.patch.object(Path, "unlink", _always_fail_unlink):
                    with self.assertRaises(pam.MigrationDecisionError) as ctx:
                        pam.build_migrated_copy(source_path, target_path, _transform_decisions())
                    self.assertEqual(ctx.exception.code, "cleanup_failed")

            self.assertTrue(os.path.exists(target_path))
            with open(target_path, "rb") as fh:
                self.assertEqual(fh.read(), b"external-replacement")
            self.assertEqual(_file_sha256(source_path), source_hash_before)


if __name__ == "__main__":
    unittest.main()
