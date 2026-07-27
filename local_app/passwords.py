"""Pure password hashing and validation helpers."""
import hashlib
import secrets


_PBKDF2_ROUNDS = 200_000


def hash_password(pw, salt=None):
    salt = salt or secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac(
        "sha256",
        (pw or "").encode("utf-8"),
        salt.encode("utf-8"),
        _PBKDF2_ROUNDS,
    )
    return f"{salt}${dk.hex()}"


def _utf8_encodable(s):
    """Return whether *s* can be represented as UTF-8 text."""
    try:
        s.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def password_is_nonblank(pw):
    """Apply the shared password text rule without trimming the password."""
    return type(pw) is str and bool(pw.strip()) and _utf8_encodable(pw)


def _is_lower_hex(value, length):
    return (
        len(value) == length
        and all(char in "0123456789abcdef" for char in value)
    )


def verify_password(pw, stored):
    if type(pw) is not str or type(stored) is not str:
        return False
    if not _utf8_encodable(pw) or not _utf8_encodable(stored):
        return False
    parts = stored.split("$")
    if len(parts) != 2:
        return False
    salt, digest = parts
    if not _is_lower_hex(salt, 32) or not _is_lower_hex(digest, 64):
        return False
    return secrets.compare_digest(hash_password(pw, salt), stored)
