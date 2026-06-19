from __future__ import annotations

from src.middleware.auth import auth_middleware
from src.middleware.error_handler import error_handler_middleware

__all__ = ["auth_middleware", "error_handler_middleware"]
