"""Pure password helpers extracted from the legacy auth module."""
import ast
import inspect
import subprocess
import sys
import unittest
from pathlib import Path

from local_app import passwords


ROOT = Path(__file__).resolve().parents[1]


class PasswordModuleTest(unittest.TestCase):
    def test_import_is_pure_in_a_clean_process(self):
        code = r'''
import sys
import local_app.passwords as passwords
for name in ("fastapi", "starlette", "local_app.db", "local_app.auth"):
    assert name not in sys.modules, (name, sorted(sys.modules))
assert not hasattr(passwords, "DEFAULT_DB_PATH")
'''
        completed = subprocess.run(
            [sys.executable, "-c", code],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_fixed_password_and_salt_preserve_existing_pbkdf2_result(self):
        salt = "0123456789abcdef0123456789abcdef"
        expected = (
            salt
            + "$9906b7404f995937502d8ea93a24824b53032d7f2c5e234e4ef8baff7176916e"
        )
        self.assertEqual(passwords.hash_password("synthetic-pass-1", salt), expected)
        self.assertEqual(passwords._PBKDF2_ROUNDS, 200_000)
        self.assertEqual(
            tuple(inspect.signature(passwords.hash_password).parameters),
            ("pw", "salt"),
        )

    def test_password_text_rule_is_exact_and_does_not_trim(self):
        class Text(str):
            pass

        for bad in (None, b"x", 1, True, "", " \t\r\n", Text("x"), "\ud800"):
            with self.subTest(bad=repr(bad)):
                self.assertFalse(passwords.password_is_nonblank(bad))
        for good in ("x", "\u5bc6\u7801", " x ", "\U0001f512"):
            with self.subTest(good=good):
                self.assertTrue(passwords.password_is_nonblank(good))

        spaced_hash = passwords.hash_password(" x ", "a" * 32)
        self.assertTrue(passwords.verify_password(" x ", spaced_hash))
        self.assertFalse(passwords.verify_password("x", spaced_hash))

    def test_verify_fails_closed_for_bad_candidates_and_storage(self):
        class Text(str):
            pass

        stored = passwords.hash_password("\u5bc6\u7801", "b" * 32)
        self.assertTrue(passwords.verify_password("\u5bc6\u7801", stored))
        for candidate in (
            None,
            b"\xe5\xaf\x86\xe7\xa0\x81",
            Text("\u5bc6\u7801"),
            "\ud800",
        ):
            with self.subTest(candidate=repr(candidate)):
                self.assertFalse(passwords.verify_password(candidate, stored))
        for malformed in (None, b"bad", Text(stored), "", "nodollar", "\ud800"):
            with self.subTest(malformed=repr(malformed)):
                self.assertFalse(passwords.verify_password("\u5bc6\u7801", malformed))

    def test_verify_rejects_utf8_encodable_noncanonical_storage(self):
        malformed_values = (
            "\u5bc6$" + ("a" * 64),
            ("a" * 32) + "$\u5bc6",
            ("a" * 32) + "$" + ("\u5bc6" * 64),
            ("A" * 32) + "$" + ("a" * 64),
            ("a" * 32) + "$" + ("A" * 64),
            ("a" * 31) + "$" + ("a" * 64),
            ("a" * 32) + "$" + ("a" * 63),
            ("a" * 32) + "$" + ("a" * 64) + "$extra",
        )
        for malformed in malformed_values:
            with self.subTest(malformed=repr(malformed)):
                self.assertFalse(passwords.verify_password("\u5bc6\u7801", malformed))

    def test_auth_reexports_the_same_objects_and_has_no_private_copy(self):
        from local_app import auth

        self.assertIs(auth.hash_password, passwords.hash_password)
        self.assertIs(auth.verify_password, passwords.verify_password)
        self.assertIs(auth.password_is_nonblank, passwords.password_is_nonblank)
        self.assertFalse(hasattr(auth, "_utf8_encodable"))

        password_tree = ast.parse(inspect.getsource(passwords))
        auth_tree = ast.parse(inspect.getsource(auth))
        password_defs = [
            node
            for node in ast.walk(password_tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == "_utf8_encodable"
        ]
        auth_defs = [
            node
            for node in ast.walk(auth_tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == "_utf8_encodable"
        ]
        self.assertEqual(len(password_defs), 1)
        self.assertEqual(auth_defs, [])

    def test_source_has_only_the_two_allowed_standard_library_imports(self):
        tree = ast.parse(inspect.getsource(passwords))
        imports = []
        for node in tree.body:
            if isinstance(node, ast.Import):
                imports.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imports.append(node.module)
        self.assertEqual(imports, ["hashlib", "secrets"])
        source = inspect.getsource(passwords)
        for forbidden in ("open(", "sqlite3", "DEFAULT_DB_PATH", "fastapi", "starlette"):
            self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
