from __future__ import annotations

from src.auth.jwt import decode_token

# In-memory token blacklist (stub — a real app would use Redis or DB)
_blacklisted_tokens: set[str] = set()


def logout(token: str) -> None:
    """Invalidate a token by adding it to the blacklist.

    Raises ValueError if the token is already invalid.
    """
    # Verify the token is valid before blacklisting
    _ = decode_token(token)
    _blacklisted_tokens.add(token)


def is_token_blacklisted(token: str) -> bool:
    """Check whether a token has been blacklisted."""
    return token in _blacklisted_tokens
