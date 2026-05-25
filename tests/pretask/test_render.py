"""Stage A unit tests for Module 5 (render)."""

from __future__ import annotations

from groundtruth.pretask.render import Candidate, render_brief


def test_render_abstains_when_empty() -> None:
    """No candidates → abstain message."""
    out = render_brief(candidates=[], anchors=None, frames=None)
    assert "could not deterministically localize" in out
    assert "<gt-task-brief>" in out
    assert "</gt-task-brief>" in out


def test_render_includes_rationale_tags() -> None:
    """Every rendered file must show at least one [tag] annotation."""
    cands = [
        Candidate(
            file="patroni/watchdog.py",
            score=0.5,
            tags=[("issue-symbol", "SafeWatchdog")],
        ),
        Candidate(
            file="patroni/postmaster.py",
            score=0.3,
            tags=[("stack-trace-frame", "line 89")],
        ),
        Candidate(
            file="tests/test_watchdog.py",
            score=0.2,
            tags=[("test-of-affected-class", "")],
        ),
    ]
    out = render_brief(cands, anchors=None, frames=None)
    for line in out.splitlines():
        if line.startswith("  - "):
            assert "[" in line and "]" in line, line


def test_render_truncates_to_max_files() -> None:
    """Beyond ``max_files`` candidates are dropped."""
    cands = [
        Candidate(file=f"f{i}.py", score=1.0 - i * 0.1, tags=[("issue-symbol", "X")])
        for i in range(8)
    ]
    out = render_brief(cands, max_files=3)
    file_lines = [ln for ln in out.splitlines() if ln.startswith("  - ")]
    assert len(file_lines) == 3
    assert "f0.py" in file_lines[0]


def test_render_handles_untagged_candidate() -> None:
    """A candidate with no tags renders without crashing."""
    cands = [Candidate(file="orphan.py", score=0.1, tags=[])]
    out = render_brief(cands)
    assert "orphan.py" in out
