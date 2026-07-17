"""SS-2 feature 3 — native_render.render_ss_submit_red (the pre-submit refusal shape).

The refusal is a native pre-commit / CI hook failure that consumes the agent's OWN observed
test RED. W5 (live leak run 29594276655): "leak-safe by contract" was asserted but not
enforced — the agent's own command essentially always NAMES the failing test file, and both
live firings (haystack-8997, llama-factory-7505) delivered a hidden-test file path. The
echoed command now passes ``_final_scrub`` (test path -> ``<test>``), with a go-silent belt.
"""

from __future__ import annotations

from groundtruth.runtime.native_render import (
    contains_gt_tag,
    contains_test_identity,
    render_ss_submit_red,
)


def test_precommit_shape_and_echoes_scrubbed_agent_command():
    out = render_ss_submit_red("pytest -q tests/test_widget.py")
    assert out.startswith("pre-commit hook failed:")
    assert out.rstrip().endswith("commit aborted (exit 1)")
    # the command shape survives, but the test-file identity does NOT (W5).
    assert "pytest -q <test>" in out
    assert "tests/test_widget.py" not in out
    assert "pre-submit check" in out


def test_live_leak_witnesses_are_scrubbed():
    # The two exact live-leaked commands (run 29594276655). RED before W5: the raw
    # test path was echoed and contains_test_identity(out) was True.
    for cmd in (
        "cd $(cat /tmp/gt_root.txt) && python3 -m pytest tests/data/test_mm_plugin.py -x -v 2>&1 | tail -30",
        "cd $(cat /tmp/gt_root.txt) && python3 -m pytest test/core/pipeline/test_type_utils.py -v 2>&1 | tail -30",
    ):
        out = render_ss_submit_red(cmd)
        assert out.startswith("pre-commit hook failed:"), "signal must survive the scrub"
        assert "was last observed FAILING" in out
        assert not contains_test_identity(out)
        assert "test_mm_plugin" not in out and "test_type_utils" not in out
        assert "<test>" in out


def test_non_test_command_is_byte_identical_to_pre_w5():
    # A command with no test identity is untouched by the firewall.
    out = render_ss_submit_red("make check")
    assert "`make check`" in out


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
