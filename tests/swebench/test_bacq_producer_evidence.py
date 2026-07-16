"""B-ACQ: producer source-contribution attestation + inherited timing / fair-probe joins.

These prove the typed producer evidence that unblocks the 12 ACQ features'
``source_contribution_correct`` and ``timing_inherited_from_fact_delivery`` gates
plus ``cochange_prior``'s ``support_causal_fair_probe`` gate.

Discipline (ea0eb16c0): producer-owned authority only. Absent evidence -> None
(fail-closed, byte-identical on old artifacts); a tampered seal or an unattested
feature -> rejected (None), never fabricated True.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

from scripts.swebench.acq_provenance import (
    _valid_contribution_attestation,
    collect_acq_provenance,
)
from groundtruth.pretask.v1r_brief import (
    _attest_source_contributions,
    _candidate_local_contribution_sources,
)

from tests.swebench.test_acq_provenance import _artifacts, _sha  # reuse fixtures

_SCRIPTS = str(Path(__file__).resolve().parents[2] / "scripts" / "swebench")
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)
import gt_feature_metrics as gfm  # noqa: E402


# --------------------------------------------------------------------------- #
# 1. Producer emits a self-sealed attestation; collector joins it.
# --------------------------------------------------------------------------- #

def _attest(payload: dict) -> None:
    """Run the REAL producer over a hand-built payload's proof + receipts."""
    _attest_source_contributions(
        payload["metrics"]["localization_proof"],
        payload["metrics"]["block_receipts"],
    )


def test_producer_names_the_candidate_local_sources_it_used():
    proof = {
        "witness": "load called by run [CALLS]",
        "witness_verified": True,
        "components": {"reach": 0.5, "lex": 0.8, "sem": 0.7},
        "acquisition_sources": {"LSP": {"kind": "lsp_resolution"}},
    }
    assert _candidate_local_contribution_sources(proof) == [
        "LSP", "graph_validity", "lexical_FTS5", "semantic_embedder",
        "structural_depth",
    ]


def test_valid_producer_attestation_promotes_source_contribution_correct(tmp_path):
    payload, ledger, trajectory = _artifacts(tmp_path)
    # Before attestation: honest dark (old-artifact shape).
    before = collect_acq_provenance(payload, ledger, trajectory)["graph_validity"]
    assert before["source_contribution_correct"] is None
    # Producer seals the contribution -> collector joins it.
    _attest(payload)
    rows = collect_acq_provenance(payload, ledger, trajectory)
    assert rows["graph_validity"]["source_contribution_correct"] is True
    assert rows["lexical_FTS5"]["source_contribution_correct"] is True
    assert rows["structural_depth"]["source_contribution_correct"] is True
    assert rows["semantic_embedder"]["source_contribution_correct"] is True
    # Its status/receipt chain is unchanged — the attestation only adds truth.
    assert rows["graph_validity"]["status"] == "MEASURED"


def test_extended_sources_are_attested_when_the_producer_emitted_them(tmp_path):
    payload, ledger, trajectory = _artifacts(tmp_path, extended_sources=True)
    _attest(payload)
    rows = collect_acq_provenance(payload, ledger, trajectory)
    for feature in ("resolution_honesty", "type_intelligence", "LSP",
                    "freshness_basis", "repo_scope", "determinism"):
        assert rows[feature]["source_contribution_correct"] is True, feature


def test_absent_attestation_stays_none_byte_identical(tmp_path):
    payload, ledger, trajectory = _artifacts(tmp_path, extended_sources=True)
    rows = collect_acq_provenance(payload, ledger, trajectory)
    for feature in ("graph_validity", "lexical_FTS5", "freshness_basis"):
        assert rows[feature]["source_contribution_correct"] is None, feature


# --------------------------------------------------------------------------- #
# 2. Biting mutations on the attestation fail closed (never fabricate True).
# --------------------------------------------------------------------------- #

def test_tampered_block_seal_is_rejected(tmp_path):
    payload, ledger, trajectory = _artifacts(tmp_path)
    _attest(payload)
    proof = payload["metrics"]["localization_proof"][0]
    # Point the (still self-consistently sealed) attestation at other bytes.
    att = proof["contribution_attestation"]
    att["block_content_sha256"] = "d" * 64
    canonical = json.dumps(
        {k: v for k, v in att.items() if k != "attestation_sha256"},
        sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    )
    att["attestation_sha256"] = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    row = collect_acq_provenance(payload, ledger, trajectory)["graph_validity"]
    assert row["source_contribution_correct"] is None


def test_broken_self_seal_is_rejected(tmp_path):
    payload, ledger, trajectory = _artifacts(tmp_path)
    _attest(payload)
    # Add a source without re-sealing -> attestation_sha256 no longer matches.
    payload["metrics"]["localization_proof"][0]["contribution_attestation"][
        "sources"
    ].append("body_retrieval")
    row = collect_acq_provenance(payload, ledger, trajectory)["body_retrieval"]
    # body has a positive component only when we add it; here body is absent AND
    # the seal is broken -> stays None regardless.
    assert row["source_contribution_correct"] is None
    graph = collect_acq_provenance(payload, ledger, trajectory)["graph_validity"]
    assert graph["source_contribution_correct"] is None


def test_feature_not_in_attested_sources_stays_none(tmp_path):
    payload, ledger, trajectory = _artifacts(tmp_path)
    _attest(payload)
    proof = payload["metrics"]["localization_proof"][0]
    att = proof["contribution_attestation"]
    att["sources"] = [s for s in att["sources"] if s != "graph_validity"]
    canonical = json.dumps(
        {k: v for k, v in att.items() if k != "attestation_sha256"},
        sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    )
    att["attestation_sha256"] = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    rows = collect_acq_provenance(payload, ledger, trajectory)
    assert rows["graph_validity"]["source_contribution_correct"] is None
    # A still-attested sibling keeps its True verdict — surgical, not blanket.
    assert rows["lexical_FTS5"]["source_contribution_correct"] is True


def test_candidate_mismatch_is_rejected(tmp_path):
    payload, ledger, trajectory = _artifacts(tmp_path)
    _attest(payload)
    att = payload["metrics"]["localization_proof"][0]["contribution_attestation"]
    att["candidate_id"] = "localization:other.py"
    canonical = json.dumps(
        {k: v for k, v in att.items() if k != "attestation_sha256"},
        sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    )
    att["attestation_sha256"] = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    row = collect_acq_provenance(payload, ledger, trajectory)["graph_validity"]
    assert row["source_contribution_correct"] is None


# --------------------------------------------------------------------------- #
# 3. Readiness joins: inherited timing (J3) + cochange support fair-probe (J4).
# --------------------------------------------------------------------------- #

def _support_record() -> dict:
    return {
        "status": "MEASURED",
        "source_artifact": "brief_result.json",
        "block_id": "file-entry-1",
        "content_sha256_16": "a" * 16,
        "receipt_level": 2,
        "supported_fact_class": "localization",
    }


def test_acq_timing_inherits_fact_class_verdict_when_measured():
    record = _support_record()
    record["source_contribution_correct"] = True
    r = gfm._acquisition_readiness(
        record, leak_free=True, dose_ok=True, live_witness=False,
        fair_probe_by_fc={"localization": True},
        timing_by_fc={"localization": True},
    )
    assert r["gates"]["timing_inherited_from_fact_delivery"] is True
    assert r["gates"]["source_contribution_correct"] is True
    assert r["gates"]["source_causal_fair_probe"] is True
    assert r["timing_inherited_from_fact_class"] == "localization"


def test_acq_timing_and_contribution_stay_none_when_absent():
    record = _support_record()  # no source_contribution_correct field
    r = gfm._acquisition_readiness(
        record, leak_free=True, dose_ok=True, live_witness=False,
        fair_probe_by_fc={}, timing_by_fc={},
    )
    assert r["gates"]["timing_inherited_from_fact_delivery"] is None
    assert r["gates"]["source_contribution_correct"] is None
    assert "timing_inherited_from_fact_class" not in r


def test_acq_timing_false_when_fact_delivery_was_late():
    record = _support_record()
    r = gfm._acquisition_readiness(
        record, leak_free=True, dose_ok=True, live_witness=False,
        timing_by_fc={"localization": False},
    )
    assert r["gates"]["timing_inherited_from_fact_delivery"] is False
    assert r["timing_inherited_from_fact_class"] == "localization"


def test_cochange_support_fair_probe_inherits_localization_verdict():
    source_record = {
        "supported_fact_class": "localization",
        "source_fields": [],
        "source_contribution_correct": True,
    }
    r = gfm._internal_fact_support_readiness(
        "cochange_prior", {}, source_record,
        ledger_artifact="ledger.jsonl",
        fair_probe_by_fc={"localization": True},
    )
    assert r["gates"]["support_causal_fair_probe"] is True
    assert r["support_causal_fair_probe_inherited_from_fact"] == "localization"


def test_cochange_support_fair_probe_none_when_unmeasured():
    source_record = {
        "supported_fact_class": "localization",
        "source_fields": [],
    }
    r = gfm._internal_fact_support_readiness(
        "cochange_prior", {}, source_record,
        ledger_artifact="ledger.jsonl",
        fair_probe_by_fc={},
    )
    assert r["gates"]["support_causal_fair_probe"] is None
    assert "support_causal_fair_probe_inherited_from_fact" not in r
