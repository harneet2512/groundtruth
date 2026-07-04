"""Single-source path normalization for the kernel and adapters.

Replaces ad-hoc ``_norm`` helpers across the codebase. The buggy
``v7_brief._norm`` calls ``lstrip("./")`` which is a CHARSET strip, not a
prefix strip -- it turns ``..foo.py`` into ``foo.py`` and mangles any path
with leading dots. This module forbids that pattern and pins the rule to a
single regex.

Rule:
1. Backslashes become forward slashes (Windows safety).
2. Exactly one leading ``/`` is removed (absolute-path tolerance).
3. The literal prefix ``workspace/`` or ``testbed/`` is removed iff it is a
   full path component at position 0 (regex anchored). ``workspaces/x.py``
   is NOT a match -- the boundary is the trailing slash.
4. Nothing else is stripped. Leading dots in filenames are preserved.
"""

from __future__ import annotations

import re

_PREFIX_RE = re.compile(r"^(?:workspace|testbed)/")


def normalize(path: str) -> str:
    """Return the canonical kernel-side form of ``path``.

    Pure function. No filesystem access.
    """
    text = path.replace("\\", "/")
    if text.startswith("/"):
        text = text[1:]
    text = _PREFIX_RE.sub("", text, count=1)
    return text


__all__ = ["normalize"]
