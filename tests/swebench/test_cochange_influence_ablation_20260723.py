"""RED-first: cochange influence witness + holdout ablation stay seal-safe.

Pins:
1. Ablation sidecar keys must NOT break ``source_identity_sha256`` validation.
2. Rate>0 DELIVER stamps ``cochange_ablation`` + keeps ``cochange_influence_witness``.
3. Rate>0 HOLDOUT withholds the influence stamp but still admits the ACQ row so the
   causal probe is identifiable via ``cochange_causal_fair_probe``.
4. Rate 0 (production default) leaves the causal probe None (never inherits a fake).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from scripts.swebench.acq_provenance import (
    _valid_cochange_evidence,
    collect_acq_provenance,
)
from groundtruth.pretask.v1r_brief import (
    _primary_cochange_evidence,
    _primary_cochange_support,
)
from groundtruth.runtime import cochange_holdout as ch

from tests.swebench.test_acq_provenance import _artifacts

_SCRIPTS = str(Path(__file__).resolve().parents[2] / "scripts" / "swebench")
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)
import gt_feature_metrics as gfm  # noqa: E402


_CANDIDATE = "src/pkg/loader.py"
_NEIGHBOURS = ["src/pkg/cache.py", "src/pkg/config.py"]


def test_rate_zero_always_deliver_and_seal_survives(monkeypatch):
    monkeypatch.delenv("GT_COCHANGE_HOLDOUT_RATE", raising=False)
    components: dict[str, float] = {}
    evidence, paths = _primary_cochange_support(
        candidate_path=_CANDIDATE,
        entry_co_changes=list(_NEIGHBOURS),
        components=components,
        bridge_evidence=None,
    )
    assert components["cochange"] == 2.0
    assert paths == sorted(_NEIGHBOURS)
    assert isinstance(evidence, dict)
    assert evidence.get("kind") == "cochange_history"
    # No ablation sidecar left on the sealed witness.
    assert "ablation_assignment" not in evidence
    assert "_ablation_assignment" not in evidence
    proof = {
        "path": _CANDIDATE,
        "components": components,
        "cochange_evidence": evidence,
        "co_changes": paths,
    }
    assert _valid_cochange_evidence(proof, _CANDIDATE) is True


def test_holdout_withholds_influence_but_records_ablation(monkeypatch):
    monkeypatch.setenv("GT_COCHANGE_HOLDOUT_RATE", "1")
    monkeypatch.setenv("GT_COCHANGE_HOLDOUT_SEED", "pin")
    components: dict[str, float] = {}
    evidence, paths = _primary_cochange_support(
        candidate_path=_CANDIDATE,
        entry_co_changes=list(_NEIGHBOURS),
        components=components,
        bridge_evidence=None,
    )
    assert "cochange" not in components or components.get("cochange", 0) == 0
    assert paths is None
    assert isinstance(evidence, dict)
    assert evidence.get("kind") == "cochange_ablation"
    assert evidence.get("withheld") is True
    assert evidence.get("ablation_assignment") == ch.HOLDOUT


def test_deliver_arm_at_positive_rate_keeps_witness_and_ablation_meta(
    monkeypatch, tmp_path,
):
    # Force DELIVER by rate just above 0 with a seed that hashes into DELIVER.
    # Exhaustive: try seeds until DELIVER, or fall back to rate=0 path covered above.
    monkeypatch.setenv("GT_COCHANGE_HOLDOUT_RATE", "0.01")
    deliver_seed = None
    for seed in (f"seed-{i}" for i in range(200)):
        monkeypatch.setenv("GT_COCHANGE_HOLDOUT_SEED", seed)
        if ch.assign(
            task_id=ch.task_seed(),
            candidate_id=f"localization:{_CANDIDATE}",
            rate=0.01,
        ) == ch.DELIVER:
            deliver_seed = seed
            break
    assert deliver_seed is not None, "could not find DELIVER seed for rate=0.01"

    payload, ledger, trajectory = _artifacts(tmp_path)
    proof = payload["metrics"]["localization_proof"][0]
    components: dict[str, float] = dict(proof.get("components") or {})
    evidence, paths = _primary_cochange_support(
        candidate_path=_CANDIDATE,
        entry_co_changes=list(_NEIGHBOURS),
        components=components,
        bridge_evidence=None,
    )
    # Mimic the producer: lift ablation off the sealed witness.
    ablation = None
    if isinstance(evidence, dict) and (
        "_ablation_assignment" in evidence or "ablation_assignment" in evidence
    ):
        assign = evidence.pop(
            "_ablation_assignment", evidence.pop("ablation_assignment", None))
        rate = evidence.pop(
            "_ablation_rate", evidence.pop("ablation_rate", None))
        ablation = {"assignment": assign, "rate": rate, "withheld": False}
    proof["components"] = components
    proof["cochange_evidence"] = evidence
    proof["co_changes"] = paths
    if ablation is not None:
        proof["cochange_ablation"] = ablation

    row = collect_acq_provenance(payload, ledger, trajectory)["cochange_history"]
    assert row["cochange_influence_witness"] is True
    assert row["cochange_causal_fair_probe"] is True
    readiness = gfm._internal_fact_support_readiness(
        "cochange_prior", {}, row, ledger_artifact="runtime.jsonl",
    )
    assert readiness["gates"]["support_correct"] is True
    assert readiness["gates"]["support_causal_fair_probe"] is True
    assert readiness.get("support_causal_fair_probe_from_cochange_ablation") is True


def test_holdout_arm_collector_marks_causal_probe_without_influence_witness(
    monkeypatch, tmp_path,
):
    monkeypatch.setenv("GT_COCHANGE_HOLDOUT_RATE", "1")
    payload, ledger, trajectory = _artifacts(tmp_path)
    proof = payload["metrics"]["localization_proof"][0]
    proof["cochange_ablation"] = {
        "assignment": "HOLDOUT", "rate": 1.0, "withheld": True,
    }
    # No cochange_evidence / components.cochange — withheld by design.
    row = collect_acq_provenance(payload, ledger, trajectory)["cochange_history"]
    assert row["status"] in {"MEASURED", "UNMEASURED"}
    assert row["cochange_influence_witness"] is None
    assert row["cochange_causal_fair_probe"] is True
