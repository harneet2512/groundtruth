"""GT pre-task brief public entry point.

The live package-level shim routes to ``v1r_brief.generate_v1r_brief``.
"""

from __future__ import annotations

__all__ = ["generate_brief"]


def generate_brief(*args, **kwargs):
    """Lazy re-export to avoid import cycles."""
    from groundtruth.pretask.v1r_brief import generate_v1r_brief as _gen

    return _gen(*args, **kwargs)
