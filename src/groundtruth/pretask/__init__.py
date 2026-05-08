"""GT pre-task brief v5 — deterministic localization pipeline.

Five modules + telemetry + render + orchestration. No LLM calls anywhere.

Public entry point: ``brief_v5.generate_brief(issue_text, repo_root, graph_db)``.
"""

from __future__ import annotations

__all__ = ["generate_brief"]


def generate_brief(*args, **kwargs):
    """Lazy re-export to avoid import cycles."""
    from groundtruth.pretask.brief_v5 import generate_brief as _gen

    return _gen(*args, **kwargs)
