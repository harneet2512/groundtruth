"""Regression for build_pred.dewrap — repairs terminal-wrapped diffs, never harms clean ones.

Origin: Live-Lite run 28922955627 — mini-swe-agent's ~80-col PTY split a long diff line so the
continuation lost its +/-/space prefix (`+... x has ` / `non-numeric values."`), git apply rejected
the patch, the official SWE-bench-Live eval produced no report. dewrap rejoins provable wrap artifacts.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "scripts", "swebench"))
from build_pred import dewrap  # noqa: E402


def _in_hunk_unprefixed(patch):
    """Count lines inside a hunk that lack a +/-/space/\\ prefix (= malformed)."""
    n, inb = 0, False
    for ln in patch.split("\n"):
        if ln.startswith("@@"):
            inb = True
            continue
        if ln.startswith(("diff ", "index ", "--- ", "+++ ", "new file", "deleted", "rename", "Binary")):
            inb = False
            continue
        if inb and ln and ln[0] not in "+- \\":
            n += 1
    return n


WRAPPED = (
    "diff --git a/sh.py b/sh.py\n"
    "--- a/sh.py\n+++ b/sh.py\n"
    "@@ -889,4 +889,6 @@ class C:\n"
    "     def m(self):\n"
    "+        # comment that is long enough to wrap __str__ -> \n"
    "stdout -> wait()\n"
    "+        return self\n"
    "     x = 1\n"
)

CLEAN = (
    "diff --git a/f.py b/f.py\n"
    "--- a/f.py\n+++ b/f.py\n"
    "@@ -1,3 +1,4 @@\n"
    " import os\n"
    "+import sys\n"
    " def f():\n"
    "     return 1\n"
)


def test_wrapped_is_repaired():
    assert _in_hunk_unprefixed(WRAPPED) == 1          # malformed as received
    fixed = dewrap(WRAPPED)
    assert _in_hunk_unprefixed(fixed) == 0            # repaired
    assert "__str__ -> stdout -> wait()" in fixed     # rejoined without a separator


def test_clean_patch_unchanged():
    # the critical safety property: a well-formed patch must pass through byte-identical
    assert dewrap(CLEAN) == CLEAN


def test_blank_context_line_preserved():
    # a diff with a blank context line (" ") must not trip the rejoin
    p = "@@ -1,2 +1,2 @@\n context\n \n-old\n+new\n"
    assert dewrap(p) == p


def test_no_newline_marker_preserved():
    p = "@@ -1 +1 @@\n-a\n+b\n\\ No newline at end of file\n"
    assert dewrap(p) == p
