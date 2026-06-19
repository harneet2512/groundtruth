"""Module 5 — Brief renderer with rationale tags.

Takes ranked candidates plus the anchors / frames metadata that produced
them, and emits a ``<gt-task-brief>`` block. The renderer is purely
formatting — all selection / scoring decisions happen upstream.

Provenance: each Candidate carries a set of tags (``issue-symbol``,
``stack-trace-frame``, ``graph-neighbor``, ``test-of-affected-class``,
``recent-edit``) recorded by whichever module surfaced it. The renderer
turns those into the ``[tag: detail]`` annotations spec'd in §5 of
arch_update.md.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from groundtruth.pretask.anchors import IssueAnchors
from groundtruth.pretask.traces import StackFrame


@dataclass
class Candidate:
    """One file in the candidate set passed to the renderer.

    Attributes:
        file: Repo-relative file path. The renderer does not normalize.
        score: Final ranked score (PPR + recency boost). Higher is better.
        tags: Provenance tags assigned by upstream modules. Each tag is a
            ``(tag, detail)`` tuple — ``detail`` may be empty.
        is_test: Whether this file is a test file (drives the
            ``test-of-affected-class`` tag's emit logic).
    """

    file: str
    score: float
    tags: list[tuple[str, str]] = field(default_factory=list)
    is_test: bool = False


_HEADER_SUCCESS = (
    "<gt-task-brief>\n"
    "GT pre-task localization (deterministic, multi-signal).\n"
)
_FOOTER_SUCCESS = (
    "\n"
    "Editing files outside this list is not blocked, but should be justified.\n"
    "Files were selected from: issue text mentions, stack-trace frames, "
    "graph links, lexical matches, and repository memory.\n"
    "</gt-task-brief>"
)
_ABSTAIN_BRIEF = (
    "<gt-task-brief>\n"
    "GT could not deterministically localize this issue.\n"
    "Recommend exploring from issue text directly.\n"
    "</gt-task-brief>"
)


def _format_tag(tag: str, detail: str) -> str:
    """Render one provenance tag in the spec's bracket format."""
    if detail:
        return f"[{tag}: {detail}]"
    return f"[{tag}]"


def _format_candidate_line(cand: Candidate) -> str:
    """Render a single ``  - path [tag] [tag]`` brief line."""
    if cand.tags:
        tag_str = " ".join(_format_tag(t, d) for t, d in cand.tags)
        return f"  - {cand.file} {tag_str}"
    return f"  - {cand.file}"


def render_brief(
    candidates: list[Candidate],
    anchors: IssueAnchors | None = None,
    frames: list[StackFrame] | None = None,
    max_files: int = 5,
) -> str:
    """Emit the brief XML block.

    Args:
        candidates: Pre-ranked, pre-filtered candidate list. The first
            ``max_files`` are emitted in order.
        anchors: Anchors record from Module 1. Reserved for future use
            by the renderer (e.g. rendering an "anchors found" footer);
            currently unused but kept in the signature so the spec
            matches arch_update.md §2 verbatim.
        frames: Stack frames from Module 2, also reserved.
        max_files: Cap on candidate lines emitted.

    Returns:
        The ``<gt-task-brief>`` block as a string. When ``candidates`` is
        empty, the abstain template is emitted instead.
    """
    del anchors, frames  # not yet used by the renderer surface

    if not candidates:
        return _ABSTAIN_BRIEF

    # Filter empty / pathless entries, then truncate.
    cleaned = [c for c in candidates if c.file]
    if not cleaned:
        return _ABSTAIN_BRIEF
    cleaned = cleaned[:max_files]

    lines = [_format_candidate_line(c) for c in cleaned]
    body = "\n".join(lines)
    return _HEADER_SUCCESS + body + _FOOTER_SUCCESS


def collect_rationale_tags(candidates: list[Candidate]) -> list[str]:
    """Distinct tag names across all rendered candidates (telemetry helper)."""
    seen: list[str] = []
    seen_set: set[str] = set()
    for cand in candidates:
        for tag, _detail in cand.tags:
            if tag not in seen_set:
                seen_set.add(tag)
                seen.append(tag)
    return seen
