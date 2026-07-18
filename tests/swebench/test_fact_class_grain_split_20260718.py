"""Fact-class evidence-type GRAIN split (Cluster-5 ITEM 3, 2026-07-18).

``trace_frame`` (an evidence-type alias) and ``ranked_localization`` (a producer) BOTH collapse to
the canonical fact_class ``localization`` in the 11-key §1 vocabulary, which muddies a loc_reslot
audit (it cannot tell a stack-frame localizer from a ranked-localization delivery). This surfaces
the finer producer/evidence-type grain alongside fact_class in the SS-integrity FACT projection
WITHOUT changing the canonical registry mapping.

RED-first: before ITEM 3 there was no ``fact_registry.evidence_grain_for`` accessor and the FACT
rows in ``run_metrics["ss_features"]`` carried no ``evidence_grain`` — the collapse was invisible.
"""
from __future__ import annotations

import sys
from pathlib import Path

from groundtruth.runtime import fact_registry as fr

ROOT = Path(__file__).resolve().parents[2]
for _p in (ROOT / "src", ROOT / "scripts" / "swebench"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import gt_feature_metrics as fm  # noqa: E402


def test_localization_grain_disambiguates_trace_frame_from_ranked_localization() -> None:
    grain = fr.evidence_grain_for("localization")
    assert "trace_frame" in grain  # the stack-frame localizer evidence-type alias
    assert "ranked_localization" in grain  # the ranked-localization producer
    # Distinct identifiers -> a loc_reslot audit can now separate them.
    assert grain == tuple(sorted(set(grain)))  # sorted + de-duplicated, deterministic


def test_canonical_registry_mapping_is_unchanged() -> None:
    # The grain accessor is additive: the canonical §1 mapping still collapses both to localization.
    assert fr._canonical_fact_class("trace_frame") == "localization"
    assert fr.registration_for("trace_frame").fact_class == "localization"
    assert "localization" in fr.all_fact_classes()
    assert len(fr.all_fact_classes()) == 11  # still the exact 11-row FACT inventory


def test_grain_covers_other_multi_producer_classes() -> None:
    # MUTATION-resistant coverage: the executed-RED and def-partition families also expose grain.
    assert "covering_verdict" in fr.evidence_grain_for("covering_red")
    dp = fr.evidence_grain_for("def_partition")
    assert {"def_ref_partition", "name_fold", "wrong_surface"} <= set(dp)
    assert fr.evidence_grain_for("not_a_registered_class") == ()  # fail-closed empty


def test_ss_integrity_fact_projection_surfaces_evidence_grain() -> None:
    """Integration: the FACT rows of the run aggregate carry evidence_grain; non-FACT rows do not."""
    agg = fm.aggregate_run("run-x", [], profile="2", expected_task_ids=["some__task-1"])
    ss_features = agg["run_metrics"]["ss_features"]
    loc = ss_features["localization"]
    assert loc["family"] == "FACT"
    assert "evidence_grain" in loc
    assert "trace_frame" in loc["evidence_grain"] and "ranked_localization" in loc["evidence_grain"]
    # A CAP row (a Profile-2 member) must NOT carry a fact-grain field — grain is FACT-only.
    assert "evidence_grain" not in ss_features["GT_POST_SEARCH"]
