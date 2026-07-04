"""Input validators that build on URL parsing."""
from __future__ import annotations

from .url_utils import parse_url, is_https


ALLOWED_HOSTS = {"example.com", "api.example.com"}


def validate_request_url(raw: str) -> bool:
    """Validate that a request URL points at an allowed host over https."""
    parsed = parse_url(raw)
    if not is_https(raw):
        return False
    return parsed.netloc.lower() in ALLOWED_HOSTS


def validate_callback(raw: str) -> bool:
    """Looser callback validation: any https URL on a non-empty host."""
    parsed = parse_url(raw)
    return is_https(raw) and bool(parsed.netloc)
