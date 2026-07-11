"""B-32 SEAM WIRING: the seam's seal sites must commit the exact BASE observation
bytes into the TITO chain (not just the GT delta). Verifies the wiring in
gt_mini_patch._seal_lane_delivery — two seals with an identical GT block but a
DIFFERENT base observation must produce DIFFERENT chain heads.

RED before the wiring: base_output is not a parameter (TypeError). After: threaded
into seal_delivery(tool_output_bytes=...). Mutation: not passing tool_output_bytes ->
identical heads -> this test bites.
"""
import os
import sys

import pytest

_REPO = r"D:\Groundtruth"
sys.path.insert(0, os.path.join(_REPO, "artifact_deepswe"))
sys.path.insert(0, os.path.join(_REPO, "src"))

import gt_mini_patch as g  # noqa: E402


@pytest.fixture(autouse=True)
def _lane_env(monkeypatch):
    # HERMETIC: enable the lane-envelope seal only for THIS module's tests and restore
    # after (monkeypatch auto-unsets). Never set os.environ at module scope — the seam
    # reads GT_LANE_ENVELOPE live, so a leaked var pollutes later test files.
    monkeypatch.setenv("GT_LANE_ENVELOPE", "1")


def _seal_once(base_output: str, text: str = "\n<gt-contract>x</gt-contract>") -> str:
    g._reset_oracle_state()
    g._gt_gateway_chain_head = ""
    g._action_count = 1
    g._seal_lane_delivery("l3.contract", text, "a/x.py", base_output=base_output)
    return g._gt_gateway_chain_head


def test_b32_lane_seal_commits_base_observation():
    head_a = _seal_once("BASE OUTPUT A — grep result 1")
    head_b = _seal_once("BASE OUTPUT B — a totally different tool output")
    assert head_a, "seal produced no chain head (GT_LANE_ENVELOPE off or seal faulted)"
    assert head_b
    assert head_a != head_b, (
        "chain head must DIFFER when the base observation differs (B-32) — the chain "
        "commits the base tool output, not only the GT delta"
    )


def test_b32_lane_seal_deterministic():
    # same base + same delta -> identical head (no randomness/time in the chain)
    assert _seal_once("SAME BASE") == _seal_once("SAME BASE")
