from __future__ import annotations

import json
import base64
import time
from dataclasses import dataclass


@dataclass
class TokenPayload:
    """Decoded JWT token payload."""

    user_id: int
    email: str
    issued_at: float
    expires_at: float


_SECRET_KEY = "change-me-in-production"
_TOKEN_EXPIRY_SECONDS = 3600


def sign_token(payload: dict[str, str | int]) -> str:
    """Sign a payload and return a JWT-like token string."""
    token_data = {
        **payload,
        "iat": time.time(),
        "exp": time.time() + _TOKEN_EXPIRY_SECONDS,
    }
    encoded = base64.urlsafe_b64encode(json.dumps(token_data).encode()).decode()
    return f"gt.{encoded}"


def decode_token(token: str) -> TokenPayload:
    """Decode a token string and return the payload. Raises ValueError if invalid or expired."""
    if not token.startswith("gt."):
        raise ValueError("Invalid token format")

    try:
        raw = base64.urlsafe_b64decode(token[3:].encode())
        data = json.loads(raw)
    except (json.JSONDecodeError, Exception) as exc:
        raise ValueError("Malformed token") from exc

    if data.get("exp", 0) < time.time():
        raise ValueError("Token has expired")

    return TokenPayload(
        user_id=int(data["user_id"]),
        email=str(data["email"]),
        issued_at=float(data["iat"]),
        expires_at=float(data["exp"]),
    )
