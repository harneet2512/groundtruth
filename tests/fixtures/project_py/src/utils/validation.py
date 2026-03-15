from __future__ import annotations

import re

from src.utils.errors import ValidationError


_EMAIL_REGEX = re.compile(r"^[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+$")
_MIN_PASSWORD_LENGTH = 8


def validate_email(email: str) -> str:
    """Validate an email address format. Returns the normalized email or raises ValidationError."""
    email = email.strip().lower()
    if not _EMAIL_REGEX.match(email):
        raise ValidationError(field="email", reason="Invalid email format")
    return email


def validate_password(password: str) -> str:
    """Validate password strength. Returns the password or raises ValidationError."""
    if len(password) < _MIN_PASSWORD_LENGTH:
        raise ValidationError(
            field="password",
            reason=f"Password must be at least {_MIN_PASSWORD_LENGTH} characters",
        )
    if not re.search(r"[A-Z]", password):
        raise ValidationError(field="password", reason="Password must contain an uppercase letter")
    if not re.search(r"[0-9]", password):
        raise ValidationError(field="password", reason="Password must contain a digit")
    return password


def sanitize_input(value: str) -> str:
    """Strip dangerous characters from user input."""
    sanitized = re.sub(r"[<>&\"']", "", value)
    return sanitized.strip()
