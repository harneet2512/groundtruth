from __future__ import annotations

from dataclasses import dataclass

from src.auth.jwt import sign_token
from src.db.client import db
from src.utils.crypto import compare_password
from src.utils.errors import AppError
from src.utils.validation import validate_email


@dataclass
class LoginResult:
    """Result of a successful login."""

    token: str
    user_id: int
    email: str


def login(email: str, password: str) -> LoginResult:
    """Authenticate a user by email and password. Returns a LoginResult with a signed token.

    Raises AppError if credentials are invalid.
    """
    email = validate_email(email)
    result = db.query("SELECT * FROM users WHERE email = ?", (email,))

    if not result.rows:
        raise AppError(message="Invalid credentials", status_code=401)

    user = result.rows[0]
    if not compare_password(password, user["hashed_password"], bytes.fromhex(user["salt"])):
        raise AppError(message="Invalid credentials", status_code=401)

    token = sign_token({"user_id": user["id"], "email": user["email"]})
    return LoginResult(token=token, user_id=user["id"], email=user["email"])
