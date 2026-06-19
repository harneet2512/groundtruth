from __future__ import annotations

from typing import Any, Callable

from src.auth.jwt import decode_token
from src.utils.errors import AppError


def auth_middleware(request: Any, next_handler: Callable[..., Any]) -> Any:
    """Middleware that validates the Authorization header and attaches user info to the request.

    Imports directly from auth.jwt (NOT from auth/__init__.py) because
    jwt functions are not re-exported from the auth package.
    """
    auth_header: str | None = getattr(request, "authorization", None)

    if auth_header is None or not auth_header.startswith("Bearer "):
        raise AppError(message="Missing or malformed Authorization header", status_code=401)

    token = auth_header[len("Bearer ") :]

    try:
        payload = decode_token(token)
    except ValueError as exc:
        raise AppError(message=str(exc), status_code=401) from exc

    # Attach decoded payload to request for downstream handlers
    request.user_id = payload.user_id  # type: ignore[attr-defined]
    request.email = payload.email  # type: ignore[attr-defined]

    return next_handler(request)
