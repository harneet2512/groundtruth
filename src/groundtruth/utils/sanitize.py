"""Sanitize user-supplied text before inserting into AI prompts."""

from __future__ import annotations

import re

# Control chars except \n (0x0A) and \t (0x09)
_CONTROL_CHAR_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")


def sanitize_for_prompt(text: str, max_length: int = 500) -> str:
    """Strip control characters and truncate.

    - Removes U+0000-001F (except \\n and \\t) and U+007F-009F.
    - Truncates to max_length characters.
    - Returns the cleaned string.
    """
    cleaned = _CONTROL_CHAR_RE.sub("", text)
    if len(cleaned) > max_length:
        return cleaned[:max_length]
    return cleaned
