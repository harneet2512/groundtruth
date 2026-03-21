"""Content hashing for incremental indexing.

Computes a SHA-256 digest of file contents. Used to detect real changes
(as opposed to mtime-only changes from touch/checkout).
"""

from __future__ import annotations

import hashlib


def content_hash(file_path: str) -> str | None:
    """Compute SHA-256 hex digest of file contents.

    Returns None if the file cannot be read.
    """
    try:
        with open(file_path, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()
    except OSError:
        return None
