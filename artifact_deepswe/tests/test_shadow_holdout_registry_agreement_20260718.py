"""CLASS 4 — shadow_holdout._CLASS_RESOLUTION must AGREE with the registry FACT truth.

THE BUG (detect.coherence). The shadow cohort join keys a holdout draw on the FACT class the
seam's typed lineage records for that delivery (gt_mini_patch._ss_shadow_withheld stamps the
holdout ledger row's ``fact_class`` = ``shadow_holdout.canonical_class(kind)``; the delivered
arm's outcome/lineage stamps ``registration_for(evidence_type).fact_class``). The map filed
``detect.coherence`` under ``cochange_prior`` — the global-arbiter ``causal_chain`` LADDER
grouping (a RANKING axis) — but the coherence-collapse steer's typed lineage is
``coherence_collapse`` -> ``recovery`` (fact_registry._EVIDENCE_TYPE_ALIASES). So a holdout draw
recorded ``cochange_prior`` while the outcome recorded ``recovery`` and the delivered-vs-withheld
cohorts NEVER joined.

This suite locks EVERY registry-adjudicable _CLASS_RESOLUTION entry to registry truth so this
drift class fails at test time. It resolves truth through the SAME two authorities the seam's
outcome side uses — the registry evidence-type aliases and the seam's own kind->evidence_type
lane bridge — NEVER the arbiter ladder.

RED-first: on the pre-fix tree ``"detect.coherence": "cochange_prior"`` the agreement assertion
FAILS with one disagreement. MUTATION: reverting the map value to ``cochange_prior`` re-reddens
BOTH ``test_class_resolution_agrees_with_registry_truth`` and ``test_detect_coherence_is_recovery``.
"""
from __future__ import annotations

import os
import sys

import gt_mini_patch as g
from groundtruth.runtime import fact_registry as fr
from groundtruth.runtime import shadow_holdout as sh


def _registry_truth(key: str) -> "str | None":
    """The canonical FACT class the OUTCOME/lineage side records for a _CLASS_RESOLUTION key.

    Resolved through the two authorities the seam uses, in order:
      1. a registry evidence_type / canonical class -> ``registration_for(key).fact_class``;
      2. a seam-tagged lane kind -> its registered evidence_type via the seam's OWN
         ``_LANE_REGISTERED_PRODUCERS`` bridge -> ``registration_for(evidence_type).fact_class``.
    Returns ``None`` only for a pure seam kind with no registry evidence type (the registry has
    no opinion; those are governed by the outcome reader's legacy table, cross-checked below).
    NEVER consults the arbiter ladder — that projection is what mis-filed detect.coherence.
    """
    reg = fr.registration_for(key)
    if reg is not None:
        return reg.fact_class
    bound = g._LANE_REGISTERED_PRODUCERS.get(key)
    if bound is not None:
        reg2 = fr.registration_for(bound[1])
        if reg2 is not None:
            return reg2.fact_class
    return None


def test_class_resolution_agrees_with_registry_truth():
    """Every _CLASS_RESOLUTION entry the registry can adjudicate MUST equal registry truth."""
    disagreements = []
    adjudicated = 0
    for key, mapped in sh._CLASS_RESOLUTION.items():
        truth = _registry_truth(key)
        if truth is None:
            continue  # pure seam kind, no registry evidence type — see the outcome-reader test
        adjudicated += 1
        if mapped != truth:
            disagreements.append(f"{key!r}: map={mapped!r} != registry-truth={truth!r}")
    assert not disagreements, (
        "shadow_holdout._CLASS_RESOLUTION disagrees with fact_registry:\n"
        + "\n".join(disagreements)
    )
    # the sweep must actually adjudicate the registry-vocab + lane-bridged entries (not a no-op).
    assert adjudicated >= 25, f"only {adjudicated} entries adjudicated — resolution path broke"
    # MUTATION[revert detect.coherence -> cochange_prior] -> one disagreement -> RED.


def test_detect_coherence_is_recovery():
    """Regression pin: the collapse steer's FACT identity is recovery, resolved THROUGH the seam
    lineage bridge (proving the bridge path — not just direct registry keys — is exercised)."""
    assert "detect.coherence" not in fr.REGISTRY  # it is a seam kind, never a registry key
    # the bridge maps it to the coherence_collapse evidence_type, which aliases to recovery
    assert g._LANE_REGISTERED_PRODUCERS["detect.coherence"][1] == "coherence_collapse"
    assert fr.registration_for("coherence_collapse").fact_class == "recovery"
    assert _registry_truth("detect.coherence") == "recovery"
    assert sh._CLASS_RESOLUTION["detect.coherence"] == "recovery"
    assert sh.canonical_class("detect.coherence") == "recovery"
    # and it stays a PARTICIPATING (advisory) holdout class — recovery is participating.
    assert sh.is_participating("detect.coherence")


def test_map_agrees_with_the_outcome_reader_for_shared_seam_kinds():
    """The cohort JOIN partner for a seam kind WITHOUT typed lineage is the outcome reader's
    legacy layer table (gt_feature_metrics._LEGACY_LAYER_FACTCLASS). For every kind BOTH tables
    name, the shadow map and the outcome reader must record the SAME fact class — else the
    delivered and withheld cohorts split on that kind."""
    sys.path.insert(
        0, os.path.join(os.path.dirname(__file__), "..", "..", "scripts", "swebench")
    )
    import gt_feature_metrics as m

    shared = set(sh._CLASS_RESOLUTION) & set(m._LEGACY_LAYER_FACTCLASS)
    assert shared, "shadow map and outcome-reader tables share no seam kinds (vacuous guard)"
    mismatches = [
        f"{k!r}: shadow={sh._CLASS_RESOLUTION[k]!r} != outcome-reader={m._LEGACY_LAYER_FACTCLASS[k]!r}"
        for k in sorted(shared)
        if sh._CLASS_RESOLUTION[k] != m._LEGACY_LAYER_FACTCLASS[k]
    ]
    assert not mismatches, "shadow map vs outcome reader:\n" + "\n".join(mismatches)
    # MUTATION[change detect.loop -> localization in either table] -> a mismatch -> RED.
