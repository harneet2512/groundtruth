"""AUDIT 2026-07-24 — the substrate closure block must survive shell quoting.

gt_substrate_image.yml verifies the published image by running a long script inside
`docker run ... bash -lc '...'` — a SINGLE-quoted shell string. Any apostrophe inside it closes that
string early, and every subsequent line then runs on the GITHUB RUNNER instead of inside the image.

That is not hypothetical: build 30139930574 died with a bare `exit 127` because a comment containing
the word "eval'd" terminated the block, so the closure check ran on the host, where
/opt/gt/python/bin/python3 does not exist. The failure looks like a missing interpreter and is
actually a quoting bug — expensive to diagnose (a full image build per attempt) and trivial to
prevent.

Worse, a truncated block can FALSELY PASS: the remaining host-side commands may exit 0, so the image
publishes while the checks that were supposed to gate it never ran in the image at all.
"""
from __future__ import annotations
import os

import pytest

_WF = os.path.join(os.path.dirname(__file__), "..", "..",
                   ".github", "workflows", "gt_substrate_image.yml")


def _block_bounds(lines):
    start = None
    for i, line in enumerate(lines):
        if "bash -lc '" in line:
            start = i
        elif start is not None and line.rstrip().endswith("'"):
            return start, i
    return start, None


@pytest.mark.skipif(not os.path.exists(_WF), reason="substrate workflow not present")
def test_no_apostrophe_inside_the_docker_run_block():
    lines = open(os.path.abspath(_WF), encoding="utf-8").read().splitlines()
    start, end = _block_bounds(lines)
    assert start is not None and end is not None, "could not locate the bash -lc block"
    offenders = [
        (i + 1, lines[i].strip())
        for i in range(start + 1, end)          # exclude the opening and closing quote lines
        if "'" in lines[i]
    ]
    assert not offenders, (
        "apostrophe inside the single-quoted docker-run block — it terminates the shell string and "
        "the remaining commands run on the RUNNER, not in the image:\n"
        + "\n".join(f"  line {n}: {t}" for n, t in offenders)
    )


@pytest.mark.skipif(not os.path.exists(_WF), reason="substrate workflow not present")
def test_the_closure_still_asserts_the_at_edit_checkers():
    """The two checkers that were silently dark must stay inside the gate."""
    text = open(os.path.abspath(_WF), encoding="utf-8").read()
    assert "import pyflakes" in text, "pyflakes dropped from the substrate closure"
    assert "check_edit_syntax" in text, \
        "the TS at-edit CAPABILITY assertion was dropped (a path check is not equivalent)"
    assert "/opt/gt/tsmod/node_modules/typescript" in text, \
        "the deterministic typescript module path is no longer verified"
