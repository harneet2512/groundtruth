"""W5-sweep — every submit/syntax renderer ENFORCES the identity firewall itself.

Sweep finding (2026-07-17, post-W5): render_submit_rejection and
render_syntax_error_native were the two remaining renderers whose leak-safety was
asserted of callers instead of enforced in the renderer — the exact architectural
shape that leaked live in run 29594276655 (render_ss_submit_red). Both now run
``_final_scrub``; these canaries pin that enforcement plus byte-identity on
non-test inputs.
"""
from __future__ import annotations

from groundtruth.runtime.native_render import (
    contains_gt_tag,
    contains_test_identity,
    render_submit_rejection,
    render_syntax_error_native,
)


def test_submit_rejection_scrubs_test_paths_in_hygiene_detail():
    # The gate_verdict diff-hygiene shape: detail embeds agent-diff paths.
    out = render_submit_rejection(
        "refusing to commit binary artifact(s)",
        "large added file(s): tests/test_new.py (+300)\nbinary: tests/fixtures/blob.bin",
    )
    assert out.startswith("pre-commit hook failed:")
    assert not contains_test_identity(out)
    assert "test_new.py" not in out
    assert "<test>" in out


def test_submit_rejection_non_test_detail_byte_identical():
    out = render_submit_rejection("working tree dirty", "unstaged: src/mod.py (+3 -1)")
    assert "working tree dirty" in out
    assert "unstaged: src/mod.py (+3 -1)" in out


def test_submit_rejection_scrubs_injected_gt_tag():
    out = render_submit_rejection("<gt-fact>reason</gt-fact>", "")
    assert not contains_gt_tag(out)


def test_syntax_renderer_scrubs_test_file_diagnostic():
    # Both live callers guard test-path edits; the renderer must not depend on that.
    out = render_syntax_error_native({
        "verdict": "syntax_error",
        "diagnostic": 'File "tests/test_widget.py", line 5\n    def f(\n        ^\nSyntaxError: never closed',
    })
    assert out, "signal must survive"
    assert not contains_test_identity(out)
    assert "test_widget" not in out


def test_syntax_renderer_non_test_diagnostic_byte_identical():
    diag = 'File "src/mod.py", line 5\n    def f(\n        ^\nSyntaxError: never closed'
    out = render_syntax_error_native({"verdict": "syntax_error", "diagnostic": diag})
    assert out == diag
