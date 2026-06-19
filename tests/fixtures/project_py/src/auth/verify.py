from __future__ import annotations

from src.auth.jwt import TokenPayload, decode_token


def verify_token(token: str) -> TokenPayload:
    """Verify and decode a token. Raises ValueError if the token is invalid or expired."""
    return decode_token(token)
