"""HTTP server entry point. Wires url parsing into the request handler."""
from __future__ import annotations

from .url_utils import parse_url, normalize_url
from .validators import validate_request_url


def handle_request(raw_url: str) -> dict:
    """Top-level request handler: parses and validates an incoming URL."""
    parsed = parse_url(raw_url)
    canonical = normalize_url(raw_url)
    ok = validate_request_url(raw_url)
    return {
        "scheme": parsed.scheme,
        "host": parsed.netloc,
        "canonical": canonical,
        "allowed": ok,
    }


def serve_forever(port: int = 8080) -> None:
    """Stub event loop. Real implementation would bind a socket."""
    print(f"server: listening on :{port}")
