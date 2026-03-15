from __future__ import annotations


class AppError(Exception):
    """Base application error with status code and message."""

    def __init__(self, message: str, status_code: int = 500) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code

    def to_dict(self) -> dict[str, str | int]:
        return {"error": self.message, "status_code": self.status_code}


class NotFoundError(AppError):
    """Raised when a requested resource does not exist."""

    def __init__(self, resource: str, identifier: str | int) -> None:
        self.resource = resource
        self.identifier = identifier
        super().__init__(
            message=f"{resource} with id '{identifier}' not found",
            status_code=404,
        )


class ValidationError(AppError):
    """Raised when input validation fails."""

    def __init__(self, field: str, reason: str) -> None:
        self.field = field
        self.reason = reason
        super().__init__(
            message=f"Validation failed for '{field}': {reason}",
            status_code=422,
        )
