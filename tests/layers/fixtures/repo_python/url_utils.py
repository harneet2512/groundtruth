"""URL parsing and validation utilities."""
from __future__ import annotations

import re
from urllib.parse import urlparse, ParseResult


_SCHEME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+\-.]*$")


def parse_url(raw: str) -> ParseResult:
    """Parse a raw URL string into its components.

    Performs validation on the scheme and host before returning.
    Raises ValueError when validation fails so callers can surface
    a meaningful message to the end user.
    """
    if not raw or not isinstance(raw, str):
        raise ValueError("parse_url: empty or non-string input")
    parsed = urlparse(raw)
    if not parsed.scheme or not _SCHEME_RE.match(parsed.scheme):
        raise ValueError(f"parse_url: invalid scheme {parsed.scheme!r}")
    if not parsed.netloc:
        raise ValueError("parse_url: missing host")
    return parsed


def normalize_url(raw: str) -> str:
    """Normalize a URL to its canonical form (lowercase scheme/host)."""
    parsed = parse_url(raw)
    scheme = parsed.scheme.lower()
    host = parsed.netloc.lower()
    return f"{scheme}://{host}{parsed.path or ''}"


def is_https(raw: str) -> bool:
    """Cheap predicate: is this URL https?"""
    parsed = parse_url(raw)
    return parsed.scheme.lower() == "https"
