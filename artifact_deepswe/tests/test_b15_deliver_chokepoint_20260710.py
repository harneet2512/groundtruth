"""B-15: `_gt_deliver_append` is the ONE model-facing delivery chokepoint for the
direct-append (legacy GT_ORACLE_ROUTE=0) lanes — it LEAK-VALIDATES every byte so the
leak law governs ALL delivery, not just the oracle route. A test-identity payload is
DROPPED (0 bytes); a clean payload appends byte-identically.

Mutation: neuter the `contains_test_identity` guard -> the leak test bites.
"""
import os
import sys

sys.path.insert(0, os.path.join(r"D:\Groundtruth", "artifact_deepswe"))
sys.path.insert(0, os.path.join(r"D:\Groundtruth", "src"))

import gt_mini_patch as g  # noqa: E402


def test_b15_appends_clean_payload():
    out = {"output": "BASE OUTPUT"}
    ok = g._gt_deliver_append(out, "\n<gt-contract>[SIGNATURE] def f(x)</gt-contract>")
    assert ok is True
    assert out["output"].startswith("BASE OUTPUT")  # prefix never rewritten
    assert "<gt-contract>" in out["output"]


def test_b15_drops_test_identity_leak():
    # a test-file nodeid (the ABI §5 leak class contains_test_identity guards) must be
    # DROPPED by the chokepoint — the same leak law the gateway route already enforces.
    out = {"output": "BASE OUTPUT"}
    ok = g._gt_deliver_append(out, "\nfailing at tests/unit/test_widget.py::test_render\n")
    assert ok is False
    assert out["output"] == "BASE OUTPUT"  # byte-identical: 0 GT bytes appended


def test_b15_empty_is_noop():
    out = {"output": "X"}
    assert g._gt_deliver_append(out, "") is False
    assert out["output"] == "X"
