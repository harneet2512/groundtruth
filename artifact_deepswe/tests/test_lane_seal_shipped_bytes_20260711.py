r"""Fable-LIPI round-2 (2026-07-11), seam Finding F-SEAM-1 (MEDIUM, audit-integrity): the lane
envelope seal must hash the bytes that ACTUALLY ship — the boundary-joined delta — not the raw
block text.

The lane delivers via ``_join_lane_output(base, text)`` (which inserts ONE ``\n`` boundary when the
base observation is not ``\n``-terminated). The pre-fix ``_seal_lane_delivery`` sealed
``text.encode()``, so ``rendered_bytes_hash`` differed from the real shipped delta by exactly the
boundary ``\n`` — corrupting the TITO law-6 replay check and feeding the wrong bytes into the law-5
chain commitment. The gateway path already sealed the joined delta (Seam-F6); this brings the lane
path to parity. GT_RL_PROFILE=1 co-activates GT_STEER_NATIVE (native steers do NOT open with ``\n``)
+ GT_LANE_ENVELOPE, so the boundary case was live on the paid path.

Audit/replay-integrity pin, NOT a leak pin: ``rendered_bytes_hash`` is host-side metadata (leak
law 9), never a model-facing byte.
"""
from __future__ import annotations

import hashlib
import os
import sys

sys.path.insert(0, os.path.join(r"D:\Groundtruth", "artifact_deepswe"))
sys.path.insert(0, os.path.join(r"D:\Groundtruth", "src"))

import gt_mini_patch as g  # noqa: E402


def _seal_and_last(monkeypatch, base, text):
    monkeypatch.setenv("GT_LANE_ENVELOPE", "1")
    monkeypatch.setattr(g, "_GT_BASELINE", False)
    monkeypatch.setattr(g, "_gt_gateway_deliveries", [])
    g._gt_gateway_chain_head = ""
    g._seal_lane_delivery("l5.no_test", text, "src/x.py", base_output=base)
    assert g._gt_gateway_deliveries, "seal produced no delivery record"
    return g._gt_gateway_deliveries[-1]


def _sha(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8", "surrogatepass")).hexdigest()


def test_lane_seal_hashes_the_boundary_joined_delta(monkeypatch):
    # Native steer: text does NOT open with `\n`; base does NOT end with `\n` -> the join inserts
    # ONE `\n` boundary, so the shipped delta != the raw text. The seal must hash the SHIPPED delta.
    base = "the command output ends here"
    text = "GT-NATIVE-STEER run a covering test"
    shipped = g._join_lane_output(base, text)[len(base):]
    assert shipped != text and shipped.startswith("\n"), "precondition: a boundary must be inserted"
    sealed = _seal_and_last(monkeypatch, base, text)
    assert sealed.rendered_bytes_hash == _sha(shipped), (
        f"lane seal must hash the SHIPPED delta {shipped!r}; got {sealed.rendered_bytes_hash}, "
        f"want {_sha(shipped)} (pre-fix it hashed the raw text -> {_sha(text)})")


def test_lane_seal_byte_identical_for_newline_opening_block(monkeypatch):
    # A tagged Lane-A block opens with `\n`: the join inserts NO boundary, shipped == text, so the
    # fix is byte-identical for that class (regression guard — this held pre-fix and must still).
    base = "prev observation"
    text = "\n<gt-nudge>GT: run a covering test</gt-nudge>"
    shipped = g._join_lane_output(base, text)[len(base):]
    assert shipped == text, "newline-opening block must ship verbatim"
    sealed = _seal_and_last(monkeypatch, base, text)
    assert sealed.rendered_bytes_hash == _sha(text)
