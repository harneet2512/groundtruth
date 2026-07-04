from __future__ import annotations

import json
from typing import Any, Callable

from src.utils.errors import AppError


def error_handler_middleware(request: Any, next_handler: Callable[..., Any]) -> Any:
    """Middleware that catches AppError exceptions and converts them to JSON responses."""
    try:
        return next_handler(request)
    except AppError as exc:
        return _make_error_response(exc.message, exc.status_code)
    except Exception:
        return _make_error_response("Internal server error", 500)


def _make_error_response(message: str, status_code: int) -> dict[str, Any]:
    """Create a structured error response."""
    return {
        "status_code": status_code,
        "body": json.dumps({"error": message}),
        "headers": {"Content-Type": "application/json"},
    }
