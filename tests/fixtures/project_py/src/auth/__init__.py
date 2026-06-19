from __future__ import annotations

# Re-exports: login, logout, verify_token
# NOTE: jwt functions (sign_token, decode_token) are NOT re-exported here.
# Import them directly from src.auth.jwt if needed.

from src.auth.login import LoginResult, login
from src.auth.logout import logout
from src.auth.verify import verify_token

__all__ = [
    "login",
    "logout",
    "verify_token",
    "LoginResult",
]
