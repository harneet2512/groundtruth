from __future__ import annotations

import hashlib
import os


def generate_salt(length: int = 16) -> bytes:
    """Generate a cryptographically secure random salt."""
    return os.urandom(length)


def hash_password(password: str, salt: bytes | None = None) -> tuple[str, bytes]:
    """Hash a password with the given salt, or generate a new salt.

    Returns a tuple of (hashed_password, salt).
    """
    if salt is None:
        salt = generate_salt()
    hashed = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 100_000)
    return hashed.hex(), salt


def compare_password(password: str, hashed: str, salt: bytes) -> bool:
    """Compare a plaintext password against a hashed password."""
    candidate, _ = hash_password(password, salt)
    return candidate == hashed
