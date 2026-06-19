from __future__ import annotations

from src.auth import login, logout, verify_token
from src.db.client import db
from src.middleware.auth import auth_middleware
from src.middleware.error_handler import error_handler_middleware
from src.users.queries import create_user, get_user_by_id


def create_app() -> dict[str, object]:
    """Initialize and return the application configuration.

    In a real Flask app this would return a Flask instance.
    Here it wires together the components for indexing purposes.
    """
    db.connect()

    return {
        "db": db,
        "middleware": [auth_middleware, error_handler_middleware],
        "routes": {
            "login": login,
            "logout": logout,
            "verify": verify_token,
            "get_user": get_user_by_id,
            "create_user": create_user,
        },
    }
