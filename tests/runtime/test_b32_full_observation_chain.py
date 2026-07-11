"""B-32 — the TITO hash-chain must commit the FULL observation, not just the GT delta.

Finding (verifyandobserve B-32 / bug-journey B-32): ``seal_delivery`` advanced
``chain_hash(parent_hash, rendered_bytes)`` where ``rendered_bytes`` was ONLY the
appended GT delta. The base tool output + the full sampled observation were never
hashed, so two DIFFERENT agent observations carrying identical GT deltas produced the
SAME chain — it could not establish observation-prefix / TITO-replay integrity.

Fix: ``chain_hash(parent_hash, gt_bytes, *, tool_output_bytes=b"", boundary=b"")``
commits ``prev_head + framed(tool_output_bytes) + framed(gt_bytes) + framed(boundary)``
(length-prefixed fields, injective on the tuple). ``seal_delivery`` threads the base
tool-output bytes + boundary through. The two new inputs default to ``b""`` — the
DOCUMENTED "seam not yet wired" degrade — so 2-argument callers are unchanged.

TTD: mandate (a) two observations, IDENTICAL gt_bytes, DIFFERENT tool_output_bytes ->
DIFFERENT chain heads (and identical inputs -> identical head, determinism). Biting
mutation M5 = drop ``tool_output_bytes`` from the commitment (restores the pre-fix
delta-only chain) -> ``test_b32_tool_output_changes_chain_head`` fails.

PURE · DETERMINISTIC · LLM-FREE · stdlib only. No network, no time, no randomness.
"""
from __future__ import annotations

import hashlib

import pytest

from groundtruth.runtime.adapters import miniswe as ad
from groundtruth.runtime.evidence_envelope import (
    RECEIPT_DELIVERED,
    VERIFIED,
    EvidenceEnvelope,
    chain_hash,
)
from groundtruth.runtime.evidence_envelope import validate as validate_env


def _env(**kw) -> EvidenceEnvelope:
    base = dict(producer="p", fact_id="sym", target="a/x.py",
                evidence_type="def_ref_partition", payload=("def: a/x.py:1",),
                provenance=(("a/x.py", 1),), confidence=0.9, tier=VERIFIED)
    base.update(kw)
    return EvidenceEnvelope.build(**base)


# --------------------------------------------------------------------------- #
# chain_hash — the full-observation commitment (mandate a)
# --------------------------------------------------------------------------- #
def test_b32_tool_output_changes_chain_head() -> None:
    """THE finding: identical parent + identical GT delta, but DIFFERENT base tool
    output -> DIFFERENT chain heads. (Mutation M5 — dropping tool_output_bytes from
    the commitment — makes these EQUAL, so this test bites.)"""
    gt = b"<gt-fact>def a/x.py:1</gt-fact>\n"
    h_a = chain_hash("", gt, tool_output_bytes=b"a/x.py:1: def run(): ...", boundary=b"")
    h_b = chain_hash("", gt, tool_output_bytes=b"b/y.py:9: def other(): ...", boundary=b"")
    assert h_a != h_b


def test_b32_identical_inputs_identical_head() -> None:
    """Determinism: identical (parent, tool_output, gt, boundary) -> identical head,
    across repeated calls (no time / randomness / set-iteration)."""
    gt, out, bnd = b"DELTA", b"BASE OUTPUT", b"boundary=42"
    a = chain_hash("", gt, tool_output_bytes=out, boundary=bnd)
    b = chain_hash("", gt, tool_output_bytes=out, boundary=bnd)
    assert a == b
    assert len(a) == 64 and all(c in "0123456789abcdef" for c in a)


def test_b32_boundary_changes_chain_head() -> None:
    """The message-boundary metadata is committed too: identical parent + tool output
    + GT delta but a different boundary descriptor -> different heads."""
    gt, out = b"DELTA", b"BASE OUTPUT"
    a = chain_hash("", gt, tool_output_bytes=out, boundary=b"offset=10")
    b = chain_hash("", gt, tool_output_bytes=out, boundary=b"offset=20")
    assert a != b


def test_b32_default_unwired_degrades_to_gt_only() -> None:
    """The DOCUMENTED "seam not yet wired" default: omitting tool_output_bytes/boundary
    is identical to passing b"" for both, i.e. the GT-delta-only commitment. This is
    the byte-identity guarantee for un-migrated (2-argument) callers."""
    gt = b"DELTA"
    assert chain_hash("", gt) == chain_hash("", gt, tool_output_bytes=b"", boundary=b"")
    p = hashlib.sha256(b"p").hexdigest()
    assert chain_hash(p, gt) == chain_hash(p, gt, tool_output_bytes=b"", boundary=b"")


def test_b32_framing_prevents_field_boundary_shift() -> None:
    """The three variable-length fields are length-prefixed, so bytes cannot migrate
    across a field boundary: (tool_output=b"AB", gt=b"C") and (tool_output=b"A",
    gt=b"BC") share the same raw concatenation b"ABC" but MUST commit differently.
    (Mutation M4 — dropping _framed — makes these EQUAL.)"""
    a = chain_hash("", b"C", tool_output_bytes=b"AB")
    b = chain_hash("", b"BC", tool_output_bytes=b"A")
    assert a != b


def test_b32_empty_tool_output_distinct_from_shifted() -> None:
    """A different split between GT delta and boundary also commits distinctly —
    (gt=b"XY", boundary=b"") vs (gt=b"X", boundary=b"Y")."""
    a = chain_hash("", b"XY", boundary=b"")
    b = chain_hash("", b"X", boundary=b"Y")
    assert a != b


def test_b32_parent_still_load_bearing() -> None:
    """The parent remains folded in (M1 guard): same (tool_output, gt, boundary) under
    different 64-hex parents -> different heads."""
    args = dict(tool_output_bytes=b"o", boundary=b"z")
    assert chain_hash("a" * 64, b"same", **args) != chain_hash("b" * 64, b"same", **args)


def test_b32_non_bytes_inputs_raise() -> None:
    """Fail-loud on corrupt input: non-bytes gt/tool_output/boundary raise ValueError
    (never a raw TypeError deep in hashlib), consistent with the parent-hash contract."""
    with pytest.raises(ValueError):
        chain_hash("", "not-bytes")            # type: ignore[arg-type]
    with pytest.raises(ValueError):
        chain_hash("", b"ok", tool_output_bytes="nope")   # type: ignore[arg-type]
    with pytest.raises(ValueError):
        chain_hash("", b"ok", boundary=123)    # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# seal_delivery — the seam-facing API carries the base observation through
# --------------------------------------------------------------------------- #
def test_b32_seal_delivery_threads_tool_output() -> None:
    """seal_delivery folds tool_output_bytes/boundary into the chain: two seals with
    the SAME parent + rendered_bytes but DIFFERENT base tool output -> DIFFERENT heads
    (and each sealed copy is still a law-abiding envelope)."""
    e = _env()
    rendered = b"the appended bytes\n"
    _, head_a = ad.seal_delivery(
        e, episode_id="t", event_id="1", parent_hash="", rendered_bytes=rendered,
        renderer_id="native", tool_output_bytes=b"OBS A", boundary=b"o=1")
    sealed_b, head_b = ad.seal_delivery(
        e, episode_id="t", event_id="1", parent_hash="", rendered_bytes=rendered,
        renderer_id="native", tool_output_bytes=b"OBS B", boundary=b"o=1")
    assert head_a != head_b
    assert head_a == chain_hash("", rendered, tool_output_bytes=b"OBS A", boundary=b"o=1")
    # identity untouched, delivery metadata still valid
    assert sealed_b.receipt_state == RECEIPT_DELIVERED
    assert sealed_b.rendered_bytes_hash == hashlib.sha256(rendered).hexdigest()
    assert validate_env(sealed_b) == []
    assert sealed_b.dedup_key == e.dedup_key


def test_b32_seal_default_matches_gt_only_chain() -> None:
    """When the seam has NOT wired the base observation, seal_delivery degrades to the
    GT-delta-only chain — so the existing 2-argument chain relationship still holds."""
    e = _env()
    rendered = b"the appended bytes\n"
    _, head = ad.seal_delivery(
        e, episode_id="t", event_id="1", parent_hash="", rendered_bytes=rendered,
        renderer_id="native")
    assert head == chain_hash("", rendered)   # gt-only default, byte-identical
