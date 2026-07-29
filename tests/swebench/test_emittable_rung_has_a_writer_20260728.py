"""An EMITTABLE receipt rung must have a PRODUCTION WRITER, or a grader silently inverts.

THE COUPLING THIS FILE EXISTS TO POLICE. Three sites read
``evidence_envelope.RUNTIME_EMITTABLE_RECEIPT_STATES``, and they depend on two DIFFERENT facts:

  * ``gt_mini_patch._persist_receipt``  -- "MAY the writer emit this rung?" (a permission)
  * ``gt_mini_patch._xsession_flush``   -- same permission question
  * ``gt_feature_metrics._runtime_ladder_is_capped`` -- "DOES a writer emit this rung?"

Only the third needs the stronger fact, and nothing enforces it.

WHY THAT IS DANGEROUS RATHER THAN UNTIDY. The 2026-07-28 wave deleted the only producer of
``referenced``/``acted`` (the receipt-promotion block in ``_gt_gateway_deliver``; the seam
cannot evaluate those rungs -- it has no ``policy_text`` and no decision-commit index, and the
real authority is ``fair_probe_result._treatment_acted``). ``_runtime_ladder_is_capped`` then
stops ``gt_feature_metrics`` from reading a ``delivered`` receipt as DISPROOF of acknowledgment
-- because under the cap ``delivered`` is not evidence of anything, it is the only thing the
writer is ALLOWED to say.

Now add ``RECEIPT_REFERENCED`` back to the emittable set WITHOUT restoring a writer. That edit
looks like a harmless widening. What actually happens: ``_runtime_ladder_is_capped()`` flips to
False, the sidecar is re-armed as disproof, every envelope-owned registered fact class grades
``acknowledged=False`` instead of deferring to the trajectory predicate, and
``ss_live_diagnosis`` terminals them all ``NOVEL_IGNORED``. SS-LIVE returns to 0/17 -- but
unfalsifiably, in the opposite direction from the original defect, because the disproof is now
manufactured rather than absent.

So the invariant is: **the emittable set may not name a rung above ``delivered`` unless some
production call site actually writes it.** A source scan is the right instrument here, and it
has repo precedent -- ``tests/runtime/test_legacy_only_layers_20260728.py`` polices call-site
topology the same way.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "src"))

from groundtruth.runtime.evidence_envelope import (  # noqa: E402
    RECEIPT_DELIVERED,
    RECEIPT_NONE,
    RECEIPT_RANK,
    RUNTIME_EMITTABLE_RECEIPT_STATES,
)

_SEAM = _REPO / "artifact_deepswe" / "gt_mini_patch.py"

# Every `transition=` literal handed to _persist_receipt anywhere in the seam.
_TRANSITION_ARG = re.compile(r"_persist_receipt\([^)]*?transition=\"([a-z_]+)\"", re.S)


def _production_written_rungs() -> set[str]:
    """The receipt rungs some production call site actually passes to ``_persist_receipt``."""
    return set(_TRANSITION_ARG.findall(_SEAM.read_text(encoding="utf-8", errors="replace")))


def test_the_scan_finds_the_known_writers() -> None:
    """Calibration. If the scan finds nothing, every assertion below passes vacuously.

    This is the guard against the instrument silently breaking -- a refactor that renames
    ``_persist_receipt`` or moves to a non-literal ``transition=`` would otherwise turn this
    whole file green while measuring nothing.
    """
    written = _production_written_rungs()
    assert written, (
        "found ZERO `_persist_receipt(..., transition=\"...\")` call sites in "
        f"{_SEAM.name} -- the scan is broken, not the code. Every other assertion in this "
        "file is vacuous until this passes."
    )
    assert RECEIPT_DELIVERED in written, (
        f"the seam no longer writes {RECEIPT_DELIVERED!r}; receipts are the delivery-proof "
        f"surface, so this is a real regression. Found: {sorted(written)}"
    )


def test_every_emittable_rung_above_delivered_has_a_production_writer() -> None:
    """THE INVARIANT. Widening the emittable set without a writer must fail HERE.

    ``none`` and ``delivered`` are exempt: ``none`` is the default (never written as a
    transition), and ``delivered`` is asserted present by the calibration above.
    """
    written = _production_written_rungs()
    delivered_rank = RECEIPT_RANK[RECEIPT_DELIVERED]
    unwritten = sorted(
        rung
        for rung in RUNTIME_EMITTABLE_RECEIPT_STATES
        if rung not in (RECEIPT_NONE, RECEIPT_DELIVERED)
        and RECEIPT_RANK.get(rung, 0) > delivered_rank
        and rung not in written
    )
    assert unwritten == [], (
        f"RUNTIME_EMITTABLE_RECEIPT_STATES permits {unwritten} but NO production call site "
        f"writes them (writers found: {sorted(written)}).\n\n"
        "This is not a tidiness problem. `gt_feature_metrics._runtime_ladder_is_capped()` "
        "reads this same constant to decide whether a `delivered` receipt may be treated as "
        "DISPROOF of acknowledgment. Permitting a higher rung with no writer flips it to "
        "False, re-arms the sidecar as disproof, and terminals every envelope-owned "
        "registered fact class NOVEL_IGNORED -- SS-LIVE 0/17, manufactured rather than "
        "measured.\n\n"
        "Either restore a writer for these rungs, or remove them from the emittable set."
    )


def test_the_seam_writes_nothing_it_is_not_permitted_to_write() -> None:
    """The converse direction: a writer must not out-run the permission set.

    ``_persist_receipt`` enforces this at runtime (it refuses and records
    ``receipt.rung_refused``), but that guard is only reachable if a caller passes a
    non-permitted rung. This makes the same contract checkable statically, so the runtime
    guard staying dead is a PASS rather than an absence of evidence.
    """
    written = _production_written_rungs()
    forbidden = sorted(written - set(RUNTIME_EMITTABLE_RECEIPT_STATES))
    assert forbidden == [], (
        f"the seam passes {forbidden} to _persist_receipt, which "
        "RUNTIME_EMITTABLE_RECEIPT_STATES does not permit. The runtime guard would refuse "
        "these and emit `receipt.rung_refused`, so the receipt would be silently absent "
        "rather than wrong -- fail loudly here instead."
    )


def test_the_grader_guard_agrees_with_the_constant() -> None:
    """`_runtime_ladder_is_capped()` must be a function OF the constant, not a hardcoded bool.

    Patching the constant to permit ``referenced`` must flip the guard off; restoring it must
    flip it back. A guard that ignores its own input would keep the Gate-4 protection in place
    forever, which sounds safe and is actually a different silent inversion -- it would keep
    deferring to the trajectory long after the sidecar regained the ability to disagree.
    """
    import importlib

    sys.path.insert(0, str(_REPO / "scripts" / "swebench"))
    import gt_feature_metrics as gfm
    import groundtruth.runtime.evidence_envelope as ee

    assert gfm._runtime_ladder_is_capped() is True, (
        "the runtime ladder is expected to be capped at `delivered` today"
    )

    original = ee.RUNTIME_EMITTABLE_RECEIPT_STATES
    try:
        ee.RUNTIME_EMITTABLE_RECEIPT_STATES = frozenset(
            set(original) | {"referenced"}
        )
        assert gfm._runtime_ladder_is_capped() is False, (
            "_runtime_ladder_is_capped() ignored a widened emittable set -- it is not "
            "actually derived from RUNTIME_EMITTABLE_RECEIPT_STATES"
        )
    finally:
        ee.RUNTIME_EMITTABLE_RECEIPT_STATES = original
        importlib.invalidate_caches()

    assert gfm._runtime_ladder_is_capped() is True, "the patch leaked past its own teardown"
