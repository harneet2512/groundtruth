"""Shared validation for exception-like names extracted from code evidence."""

from __future__ import annotations

_KNOWN_EXCEPTIONS = frozenset({
    "ValueError", "TypeError", "KeyError", "IndexError", "AttributeError",
    "RuntimeError", "IOError", "OSError", "FileNotFoundError", "ImportError",
    "NameError", "ZeroDivisionError", "OverflowError", "StopIteration",
    "NotImplementedError", "PermissionError", "TimeoutError", "ConnectionError",
    "AssertionError", "LookupError", "UnicodeError", "UnicodeDecodeError",
    "UnicodeEncodeError", "RecursionError", "MemoryError", "SystemError",
    "NullPointerException", "IllegalArgumentException", "IllegalStateException",
    "IOException", "ClassNotFoundException", "ArrayIndexOutOfBoundsException",
    "NumberFormatException", "UnsupportedOperationException",
    "RangeError", "ReferenceError", "SyntaxError", "URIError",
    "PanicError",
    "ValidationError", "NotFoundError", "AuthenticationError",
    "AuthorizationError", "ConfigurationError", "SerializationError",
    "DeserializationError", "ParseError", "HTTPError", "NetworkError",
})

_INVALID_EXCEPTION_LIKE_NAMES = frozenset({
    "ErrorType", "ErrorClass", "ExceptionType", "ExceptionClass",
})


def is_valid_exception_name(name: str) -> bool:
    """Return True if the value looks like a real exception class name."""
    if not name or not name[0].isupper():
        return False

    if name in _KNOWN_EXCEPTIONS:
        return True

    if name in _INVALID_EXCEPTION_LIKE_NAMES:
        return False

    return "Error" in name or "Exception" in name


def normalize_exception_name(name: str) -> str:
    """Return a validated exception name, or empty string if invalid."""
    candidate = name.strip().rstrip(",;:()[]")
    return candidate if is_valid_exception_name(candidate) else ""
