"""SS-2 feature 3 — native_render.render_ss_submit_red (the pre-submit refusal shape).

The refusal is a native pre-commit / CI hook failure that consumes the agent's OWN observed
test RED. Leak-safe by CONTRACT: only the agent's own command reaches the renderer.
"""

from __future__ import annotations

from groundtruth.runtime.native_render import (
    contains_gt_tag,
    render_ss_submit_red,
)


def test_precommit_shape_and_echoes_agent_command():
    out = render_ss_submit_red("pytest -q tests/test_widget.py")
    assert out.startswith("pre-commit hook failed:")
    assert out.rstrip().endswith("commit aborted (exit 1)")
    # the agent's OWN command is quoted back (leak-safe: it is the agent's own string).
    assert "pytest -q tests/test_widget.py" in out
    assert "pre-submit check" in out


def test_no_gt_tag():
    out = render_ss_submit_red("pytest -q")
    assert not contains_gt_tag(out)
    assert "<gt-" not in out


def test_empty_command_is_quiet():
    assert render_ss_submit_red("") == ""
    assert render_ss_submit_red("   ") == ""
    assert render_ss_submit_red(None) == ""  # type: ignore[arg-type]


def test_long_command_is_bounded():
    huge = "pytest " + "x" * 5000
    out = render_ss_submit_red(huge)
    # bounded (the renderer tails the command) but still a valid refusal.
    assert out.startswith("pre-commit hook failed:")
    assert len(out) < 1000
