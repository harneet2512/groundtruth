"""TTD red-before-green for C1c (B3b empty contract) + C1d ([BEHAVIORAL CONTRACT]
dedup + ordering).

C1c DEFECT artifact (haystack, real run): the post-edit ``[BEHAVIORAL CONTRACT]``
block rendered a header followed by an EMPTY field

    [GT KEY CONTRACTS]
      Preserve: guard_clause:

— a ``guard_clause`` whose value is blank (nothing after the colon). The miner
appended ``  PRESERVE: {value}`` without checking the value was non-empty, and
the header was emitted unconditionally. Fix: suppress any contract line whose
VALUE is empty/whitespace; if ALL lines for a block are empty, suppress the whole
header (correct-or-quiet).

C1d DEFECT artifact (ev47, real run): duplicate lines reached the agent —

    PARAMS: lib [required] [required]                 (duplicated param)
    [RESOURCE] context_manager: lib.transaction()
    [RESOURCE] context_manager: lib.transaction()     (duplicated resource)

and the downstream char cap (owned by the wrapper) can cut the body so the
high-value guards/returns/raises are lost behind low-value params. Fix: DEDUP
contract lines, and ORDER guards/returns/raises BEFORE params/resources so the
deciding content survives any cap.

These fixtures encode the OBSERVED rendered output, not the implementation.
"""
from __future__ import annotations

import pytest

from groundtruth.hooks.post_edit import _normalize_contract_lines


# ===========================================================================
# C1c — empty-value contract lines are suppressed.
# ===========================================================================
def test_empty_preserve_value_dropped():
    """A ``PRESERVE:`` line whose value is blank must be dropped (the verified
    empty ``guard_clause`` haystack defect)."""
    out = _normalize_contract_lines([
        "  PARAMS: lib [required]",
        "  PRESERVE: ",          # empty value -> drop
        "  PRESERVE: not lib",   # real value -> keep
    ])
    assert "  PRESERVE: " not in out
    assert any(ln.strip() == "PRESERVE: not lib" for ln in out)


def test_empty_marker_value_dropped():
    """Any ``[MARKER] <empty>`` line is dropped, not just PRESERVE."""
    out = _normalize_contract_lines([
        "  [RESOURCE] ",                 # empty -> drop
        "  [RAISES] ValueError",         # real -> keep
        "  [CATCHES]   ",                # whitespace -> drop
    ])
    assert all(ln.strip() not in ("[RESOURCE]", "[CATCHES]") for ln in out)
    assert not any(ln.rstrip().endswith("[RESOURCE]") for ln in out)
    assert any("ValueError" in ln for ln in out)


def test_all_empty_yields_no_lines():
    """When every contract line is empty, the normalizer returns [] so the caller
    can suppress the whole [BEHAVIORAL CONTRACT] header (correct-or-quiet)."""
    out = _normalize_contract_lines([
        "  PRESERVE: ",
        "  [RESOURCE] ",
        "  PARAMS: ",
    ])
    assert out == []


# ===========================================================================
# C1d — dedup.
# ===========================================================================
def test_duplicate_lines_deduped():
    out = _normalize_contract_lines([
        "  [RESOURCE] context_manager: lib.transaction()",
        "  [RESOURCE] context_manager: lib.transaction()",  # exact dup -> drop
        "  PRESERVE: not lib",
        "  PRESERVE: not lib",                                # exact dup -> drop
    ])
    assert out.count("  [RESOURCE] context_manager: lib.transaction()") == 1
    assert out.count("  PRESERVE: not lib") == 1


def test_dedup_preserves_first_occurrence_order():
    out = _normalize_contract_lines([
        "  [RAISES] ValueError",
        "  PARAMS: lib [required]",
        "  [RAISES] ValueError",  # dup of first
    ])
    assert out == ["  [RAISES] ValueError", "  PARAMS: lib [required]"]


# ===========================================================================
# C1d — ordering: guards / returns / raises BEFORE params / resources.
# ===========================================================================
def test_high_value_lines_precede_params_and_resources():
    """guards/returns/raises must sort ahead of params/resources so a downstream
    char cap keeps the deciding content."""
    out = _normalize_contract_lines([
        "  PARAMS: lib [required]",
        "  [RESOURCE] context_manager: lib.transaction()",
        "  PRESERVE: not lib",
        "  [RAISES] ValueError",
        "  [RETURNS] value",
    ])
    idx = {seg: i for i, seg in enumerate(out)}
    last_high = max(
        idx["  PRESERVE: not lib"], idx["  [RAISES] ValueError"], idx["  [RETURNS] value"]
    )
    first_low = min(
        idx["  PARAMS: lib [required]"], idx["  [RESOURCE] context_manager: lib.transaction()"]
    )
    assert last_high < first_low, f"params/resources rendered before guards/raises:\n{out}"


def test_normalize_is_stable_on_clean_input():
    """A clean already-ordered block of distinct lines round-trips unchanged in
    membership (no spurious drops)."""
    lines = ["  [RAISES] ValueError", "  PRESERVE: not lib", "  PARAMS: x"]
    out = _normalize_contract_lines(lines)
    assert set(out) == set(lines)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
