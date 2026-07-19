"""RED-first: the PRIMARY localization candidate's own mined co-change
neighbourhood must be a first-class ACQ SOURCE-5 (``cochange_history``) /
INFLUENCE-5 (``cochange_prior``) support signal.

Before the fix both features were DARK: the producer computed a candidate's
co-change list (the rendered ``Also changes: …`` line) but never stamped
``components["cochange"]`` or a witness onto the emitted ``localization_proof``,
and the binder only recognised the cross-domain ``git_log`` bridge witness — so a
primary-path witness was rejected and the SOURCE-5 join stayed blind. Every test
below fails on that pre-fix behaviour and passes after.

Not task/repo keyed: the witness is minted from any candidate that carries a real
(non-test) co-change list, sealed by content, and the cross-domain bridge proof is
left byte-identical.
"""

from __future__ import annotations

import copy
import json

from scripts.swebench.acq_provenance import (
    _source_features,
    _source_field_paths,
    _valid_cochange_evidence,
    collect_acq_provenance,
)
from groundtruth.pretask.v1r_brief import (
    _cochange_evidence,
    _primary_cochange_evidence,
    _primary_cochange_support,
)

from tests.swebench.test_acq_provenance import _artifacts


_CANDIDATE = "src/pkg/loader.py"
_NEIGHBOURS = ["src/pkg/cache.py", "src/pkg/config.py"]


def _stamp_primary_path(payload: dict) -> list[str]:
    """Attach exactly what the producer now emits for a primary candidate."""
    proof = payload["metrics"]["localization_proof"][0]
    evidence = _primary_cochange_evidence(
        candidate_path=_CANDIDATE, co_change_paths=list(_NEIGHBOURS),
    )
    proof["components"]["cochange"] = float(evidence["count"])
    proof["cochange_evidence"] = evidence
    proof["co_changes"] = list(evidence["co_change_paths"])
    return list(evidence["co_change_paths"])


def test_primary_path_cochange_goes_live_end_to_end(tmp_path):
    payload, ledger, trajectory = _artifacts(tmp_path)
    _stamp_primary_path(payload)

    row = collect_acq_provenance(payload, ledger, trajectory)["cochange_history"]

    # DARK pre-fix (binder rejects the non-git_log witness) -> UNMEASURED.
    assert row["status"] == "MEASURED"
    assert row["candidate_path"] == _CANDIDATE
    assert row["supported_fact_class"] == "localization"
    assert row["receipt_level"] == 2
    assert row["source_contribution_correct"] is True


def test_primary_path_witness_admitted_by_source_features():
    evidence = _primary_cochange_evidence(
        candidate_path=_CANDIDATE, co_change_paths=list(_NEIGHBOURS),
    )
    proof = {
        "path": _CANDIDATE,
        "components": {"cochange": float(evidence["count"])},
        "cochange_evidence": evidence,
        "co_changes": list(evidence["co_change_paths"]),
    }
    assert _valid_cochange_evidence(proof, _CANDIDATE) is True
    assert "cochange_history" in _source_features(proof, {})


def test_primary_path_source_fields_name_the_co_change_list():
    evidence = _primary_cochange_evidence(
        candidate_path=_CANDIDATE, co_change_paths=list(_NEIGHBOURS),
    )
    proof = {
        "path": _CANDIDATE,
        "components": {"cochange": float(evidence["count"])},
        "cochange_evidence": evidence,
        "co_changes": list(evidence["co_change_paths"]),
    }
    fields = _source_field_paths("cochange_history", 0, proof)
    # DARK pre-fix: the co_changes lineage pointer is absent.
    assert "metrics.localization_proof[0].co_changes" in fields
    assert "metrics.localization_proof[0].components.cochange" in fields
    assert "metrics.localization_proof[0].cochange_evidence" in fields


def test_primary_path_witness_is_tamper_evident(tmp_path):
    payload, ledger, trajectory = _artifacts(tmp_path)
    _stamp_primary_path(payload)
    # Mutating a neighbour after the self-seal must fail closed.
    payload["metrics"]["localization_proof"][0]["cochange_evidence"][
        "co_change_paths"
    ][0] = "src/pkg/unrelated.py"

    row = collect_acq_provenance(payload, ledger, trajectory)["cochange_history"]
    assert row["status"] == "UNMEASURED"
    assert row["source_contribution_correct"] is None


def test_component_count_must_equal_witness_count(tmp_path):
    payload, ledger, trajectory = _artifacts(tmp_path)
    _stamp_primary_path(payload)
    payload["metrics"]["localization_proof"][0]["components"]["cochange"] = 9.0

    row = collect_acq_provenance(payload, ledger, trajectory)["cochange_history"]
    assert row["status"] == "UNMEASURED"


def test_producer_support_helper_mints_stamps_and_filters_tests():
    components: dict[str, float] = {}
    evidence, proof_co_changes = _primary_cochange_support(
        candidate_path=_CANDIDATE,
        entry_co_changes=[
            "src/pkg/cache.py",
            "tests/test_loader.py",  # test path must be dropped
            _CANDIDATE,  # self must be dropped
        ],
        components=components,
        bridge_evidence=None,
    )
    assert isinstance(evidence, dict)
    assert evidence["source"] == "primary_path"
    assert components["cochange"] == 1.0
    assert proof_co_changes == ["src/pkg/cache.py"]
    # The minted witness validates in the binder (producer -> binder round-trip).
    proof = {
        "path": _CANDIDATE,
        "components": components,
        "cochange_evidence": evidence,
        "co_changes": proof_co_changes,
    }
    assert _valid_cochange_evidence(proof, _CANDIDATE) is True


def test_bridge_candidate_proof_stays_byte_identical():
    bridge = _cochange_evidence(
        candidate_path=_CANDIDATE,
        source_revision="a" * 40,
        source_rows=[
            {"commit": c * 40, "symptom_paths": ["src/symptom.py"]}
            for c in "abc"
        ],
        history_limit=100,
    )
    components = {"cochange": 3.0}
    before = copy.deepcopy(components)
    evidence, proof_co_changes = _primary_cochange_support(
        candidate_path=_CANDIDATE,
        entry_co_changes=["src/pkg/cache.py"],
        components=components,
        bridge_evidence=bridge,
    )
    # Bridge witness returned untouched; no co_changes pointer added; components
    # unmodified; the git_log lineage stays exactly two fields.
    assert evidence is bridge
    assert proof_co_changes is None
    assert components == before
    bproof = {
        "path": _CANDIDATE,
        "components": components,
        "cochange_evidence": bridge,
    }
    assert _valid_cochange_evidence(bproof, _CANDIDATE) is True
    assert _source_field_paths("cochange_history", 0, bproof) == [
        "metrics.localization_proof[0].components.cochange",
        "metrics.localization_proof[0].cochange_evidence",
    ]


def test_primary_path_floor_requires_a_real_neighbour():
    # An empty neighbour list mints nothing (never a hollow support row).
    components: dict[str, float] = {}
    evidence, proof_co_changes = _primary_cochange_support(
        candidate_path=_CANDIDATE,
        entry_co_changes=["tests/test_only.py", _CANDIDATE],
        components=components,
        bridge_evidence=None,
    )
    assert evidence is None
    assert proof_co_changes is None
    assert "cochange" not in components
