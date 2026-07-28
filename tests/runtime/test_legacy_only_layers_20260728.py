"""C17 (#19) — the layers invoked ONLY by the legacy fallback route, pinned executably.

`_augment_output` reaches `_augment_output_legacy` in exactly two situations: there is no
canonical runtime attachment, or the canonical observer has gone DARK. So these layers are the
FALLBACK ROUTE — not dead code, and not the healthy production path either. Both misreadings
cost something:

  * "never runs"      -> someone deletes a live fallback whose whole purpose is that a dark
                         observer degrades to untimed delivery rather than to silence.
  * "runs normally"   -> its counters get audited as canonical behaviour. That is how the
                         historical "137/138 wrong_phase suppressions" figure became folklore
                         about the canonical path and had to be retracted.

WHY A TEST INSTEAD OF 48 COMMENTS. A comment cannot notice when a layer is re-homed. This
pins the class so BOTH directions of drift surface:
  * a layer gains a call site outside the legacy body  -> it is no longer legacy-only, and the
    list must shrink (that is a re-homing, and it should be deliberate).
  * a new function becomes legacy-only                 -> the list must grow, forcing someone
    to decide whether that was intended.

METHOD, and its LIMIT stated honestly. Membership is decided by LOCATION of direct call sites,
not by call-graph resolution — three separate call-graph attempts produced three wrong answers
(371, a vacuous 0, and 425) because this file mixes module functions, methods and dynamic
dispatch, and attribute names collide with stdlib methods (`execute`, `add`, `observe`, `flush`
are all both).

This set is therefore FIRST-ORDER and CONSERVATIVE: functions called DIRECTLY from inside
`_augment_output_legacy` and nowhere else. A transitive callee of one of these is NOT included.
Under-reporting is the safe direction for a labelling task; over-reporting would invite deleting
something live.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest


_SEAM = (
    Path(__file__).resolve().parents[2] / "artifact_deepswe" / "gt_mini_patch.py"
)
_LEGACY_ROOT = "_augment_output_legacy"

# Every function whose DIRECT call sites all lie inside the legacy body, as of 2026-07-28.
LEGACY_ONLY = frozenset(
    {
        "_cochange_block",
        "_coherence_collapse_candidate",
        "_concern_lane_a_append",
        "_consensus_block",
        "_consensus_collect",
        "_consensus_progressive",
        "_consensus_scope_lane_a_append",
        "_current_event",
        "_degenerate_loop_candidate",
        "_edit_credit_body_tokens",
        "_edit_syntax_candidate",
        "_evidence",
        "_executed_covering_candidate",
        "_filter_candidates_by_phase",
        "_global_pool_add_lane_a",
        "_global_pool_add_steer",
        "_global_pool_flush",
        "_graph_contract_block",
        "_gt_edit_overlay_transaction",
        "_gt_gateway_caller_contract_ready",
        "_gt_gateway_deliver",
        "_gt_hypothesis_classify_turn",
        "_l5_failure_nudge",
        "_l5_no_test_evidence_nudge",
        "_l5_nudge",
        "_l5_unresolved_build_guard",
        "_lane_a_retired_under_gateway",
        "_ledger_judge_pending",
        "_maybe_persist_obligation_status",
        "_obligation_resurface_candidate",
        "_recovery_candidate",
        "_review_obligation_candidate",
        "_scope_completeness_block",
        "_search_localize_block",
        "_search_localize_subject",
        "_seed_read_targets",
        "_sem_seed",
        "_semantic_drift_candidate",
        "_ss_model_text",
        "_ss_observe_turn",
        "_ss_record_behavioral_proof",
        "_ss_record_edit",
        "_ss_record_test",
        "_ss_scan_acks",
        "_ss_write_observation_required",
        "_v2_attribute_resurface_silence",
        "_v2_obligation_result_ready",
        "_verification_horizon_candidate",
    }
)


@pytest.fixture(scope="module")
def seam():
    text = _SEAM.read_text(encoding="utf-8", errors="replace")
    lines = text.split("\n")
    spans = {
        node.name: (node.lineno, node.end_lineno)
        for node in ast.parse(text).body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    return lines, spans


def _direct_call_sites(lines, name, own_span):
    """Line numbers of DIRECT `name(` calls, excluding the definition's own body."""
    dlo, dhi = own_span
    pat = re.compile(rf"(?<![\w.]){re.escape(name)}\s*\(")
    return [
        i + 1
        for i, line in enumerate(lines)
        if pat.search(line)
        and not (dlo <= i + 1 <= dhi)
        and not line.lstrip().startswith(("def ", "#"))
    ]


def test_the_instrument_can_produce_a_non_zero(seam) -> None:
    """CALIBRATION. Without this, an empty result is unreadable.

    The attribute-call probe below must be able to FIND attribute calls; if it silently found
    nothing for everything, every function would look legacy-only.
    """
    lines, spans = seam
    lo, hi = spans[_LEGACY_ROOT]
    for known in ("observe_action_result", "ingest_evidence"):
        pat = re.compile(rf"\.{re.escape(known)}\s*\(")
        hits = [
            i + 1
            for i, line in enumerate(lines)
            if pat.search(line) and not (lo <= i + 1 <= hi)
        ]
        assert hits, f".{known}( should be found outside the legacy body"


def test_every_pinned_layer_is_still_called_only_from_the_legacy_body(seam) -> None:
    lines, spans = seam
    lo, hi = spans[_LEGACY_ROOT]
    rehomed = {}
    for name in sorted(LEGACY_ONLY):
        assert name in spans, f"{name} no longer exists as a module-level function"
        outside = [
            s
            for s in _direct_call_sites(lines, name, spans[name])
            if not (lo <= s <= hi)
        ]
        if outside:
            rehomed[name] = outside[:3]
    assert rehomed == {}, (
        "these layers gained call sites OUTSIDE the legacy fallback route — they are no "
        "longer legacy-only. If that was a deliberate re-homing, remove them from "
        f"LEGACY_ONLY; if not, it is an accidental canonical dependency: {rehomed}"
    )


def test_no_new_layer_became_legacy_only_unnoticed(seam) -> None:
    """The other direction of drift: the set must not silently GROW."""
    lines, spans = seam
    lo, hi = spans[_LEGACY_ROOT]
    found = set()
    for name, span in spans.items():
        if name == _LEGACY_ROOT:
            continue
        sites = _direct_call_sites(lines, name, span)
        if sites and all(lo <= s <= hi for s in sites):
            found.add(name)
    new = sorted(found - LEGACY_ONLY)
    assert new == [], (
        "new function(s) are now called ONLY from the legacy fallback route. Decide "
        "deliberately whether that is intended, then add them to LEGACY_ONLY: "
        f"{new}"
    )


def test_the_legacy_route_is_a_live_fallback_not_dead_code(seam) -> None:
    """Pins the REASON these layers must not be deleted.

    `_augment_output` calls the legacy route when there is no canonical attachment or the
    canonical observer is dark. If those call sites ever disappear, this set really would be
    dead — and that is a decision someone must make explicitly, not discover later.
    """
    lines, spans = seam
    lo, hi = spans["_augment_output"]
    body = "\n".join(lines[lo - 1 : hi])
    assert body.count(f"{_LEGACY_ROOT}(") >= 2, (
        "the legacy fallback should be reachable from BOTH the no-attachment and the "
        "dark-observer branches of _augment_output"
    )
