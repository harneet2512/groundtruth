"""Result type for error handling without exceptions."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Generic, TypeVar, Union

T = TypeVar("T")
E = TypeVar("E")


@dataclass(frozen=True)
class Ok(Generic[T]):
    """Successful result."""

    value: T

    def is_ok(self) -> bool:
        return True

    def is_err(self) -> bool:
        return False


@dataclass(frozen=True)
class Err(Generic[E]):
    """Error result."""

    error: E

    def is_ok(self) -> bool:
        return False

    def is_err(self) -> bool:
        return True


Result = Union[Ok[T], Err[E]]


@dataclass(frozen=True)
class GroundTruthError:
    """Standard error type for GroundTruth operations."""

    code: str
    message: str
    details: dict[str, object] | None = None
