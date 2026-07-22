"""RED-first: the two remaining ACQ/influence source-truth gaps in CODEX_129_BUGLIST.

Three independent findings, each reproduced BEFORE the fix:

1. influence ``cochange_prior`` — the internal-support ``support_correct`` gate BORROWS the
   generic ACQ ``source_contribution_correct`` field instead of an independently-enforced,
   dedicated cochange influence-truth witness. A record can carry a truthy contribution
   field with NO validated cochange evidence and still promote ``support_correct``.

2. ACQ ``cochange_history`` — Codex claimed the collector "hardcodes" ``source_contribution_correct
   =True``. This is a FALSE POSITIVE: the True is only reached AFTER ``_valid_cochange_evidence``
   admits the row, and disappears the moment the self-sealed evidence is absent/tampered. The
   guard below proves the alleged laundering path does not exist.

3. ACQ ``determinism`` — the repeat-identity witness is inserted into ``acquisition_sources``
   by ``brief_cache.verify_independent_generation`` AFTER ``v1r_brief`` already sealed the
   contribution attestation, and the attestation is never rebuilt. So determinism can never be
   named in the sealed ``sources`` and its ``source_contribution_correct`` stays permanently
   ``None`` — structurally unable to meet the ACQ bar.

Not task/repo keyed: every witness is minted from generic candidate-local producer data.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

from scripts.swebench.acq_provenance import collect_acq_provenance
from groundtruth.pretask.v1r_brief import (
    _attest_source_contributions,
    _cochange_evidence,
)
from groundtruth.runtime import brief_cache

from tests.swebench.test_acq_provenance import _artifacts, _sha

_SCRIPTS = str(Path(__file__).resolve().parents[2] / "scripts" / "swebench")
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)
import gt_feature_metrics as gfm  # noqa: E402


# --------------------------------------------------------------------------- #
# Finding 1: cochange_prior support_correct must not borrow the generic field.
# --------------------------------------------------------------------------- #

def _internal_support_record(**overrides: object) -> dict:
    record = {
        "status": "MEASURED",
        "candidate_id": "localization:src/pkg/loader.py",
        "candidate_path": "src/pkg/loader.py",
        "supported_fact_class": "localization",
        "source_artifact": "brief_result.json",
        "source_fields": ["metrics.localization_proof[0].components.cochange"],
        "content_sha256_16": "a" * 16,
        "chars_delivered": 42,
        "block_content_sha256_16": "b" * 16,
        "block_char_span": [0, 10],
        "producer_payload_scope": "whole_brief",
        "producer_entry_index": 0,
        "producer_ledger_layer": "brief",
        "delivery_message_index": 0,
        "receipt_level": 2,
        "receipt_evidence": {
            "referenced_message_index": 1,
            "acted_message_index": None,
        },
    }
    record.update(overrides)
    return record


def test_cochange_prior_support_correct_must_not_borrow_generic_contribution_field():
    """A truthy generic ACQ contribution field, with NO dedicated cochange influence
    witness, must NOT promote the internal-support ``support_correct`` gate."""
    record = _internal_support_record(source_contribution_correct=True)
    readiness = gfm._internal_fact_support_readiness(
        "cochange_prior", {}, record, ledger_artifact="runtime.jsonl",
    )
    # RED (pre-fix): support_correct borrows source_contribution_correct -> True.
    assert readiness["gates"]["support_correct"] is None


def test_cochange_prior_support_correct_true_only_from_dedicated_witness():
    record = _internal_support_record(
        source_contribution_correct=None,
        cochange_influence_witness=True,
    )
    readiness = gfm._internal_fact_support_readiness(
        "cochange_prior", {}, record, ledger_artifact="runtime.jsonl",
    )
    # RED (pre-fix): the dedicated witness is never read -> None.
    assert readiness["gates"]["support_correct"] is True


def test_collector_emits_dedicated_cochange_influence_witness(tmp_path):
    payload, ledger, trajectory = _artifacts(tmp_path)
    proof = payload["metrics"]["localization_proof"][0]
    proof["components"]["cochange"] = 3.0
    proof["cochange_evidence"] = _cochange_evidence(
        candidate_path="src/pkg/loader.py",
        source_revision="a" * 40,
        source_rows=[
            {"commit": chr(ord("a") + i) * 40, "symptom_paths": ["src/symptom.py"]}
            for i in range(3)
        ],
        history_limit=100,
    )
    row = collect_acq_provenance(payload, ledger, trajectory)["cochange_history"]
    # RED (pre-fix): the collector never emits this dedicated witness field.
    assert row["cochange_influence_witness"] is True


def test_absent_cochange_evidence_leaves_influence_witness_none(tmp_path):
    payload, ledger, trajectory = _artifacts(tmp_path)
    row = collect_acq_provenance(payload, ledger, trajectory)["cochange_history"]
    assert row["cochange_influence_witness"] is None


# --------------------------------------------------------------------------- #
# Finding 2: cochange_history "hardcode" is a FALSE POSITIVE — guard it.
# --------------------------------------------------------------------------- #

def test_cochange_history_contribution_is_gated_by_valid_evidence_never_blind(tmp_path):
    """The alleged laundering path (True without a validated witness) cannot occur:
    with a positive cochange component but NO self-sealed evidence, the row is
    UNMEASURED and ``source_contribution_correct`` stays ``None``."""
    payload, ledger, trajectory = _artifacts(tmp_path)
    payload["metrics"]["localization_proof"][0]["components"]["cochange"] = 3.0
    # NO cochange_evidence attached -> _valid_cochange_evidence fails.
    row = collect_acq_provenance(payload, ledger, trajectory)["cochange_history"]
    assert row["status"] == "UNMEASURED"
    assert row["source_contribution_correct"] is None


# --------------------------------------------------------------------------- #
# Finding 3: determinism must be attestable — reseal after the witness join.
# --------------------------------------------------------------------------- #

def _cochange_block_result(brief_text: str) -> SimpleNamespace:
    candidate_id = "localization:src/pkg/loader.py"
    receipts = [{
        "block_id": "file-entry-1",
        "fact_class": "localization",
        "label": "file-entry-1",
        "candidate_id": candidate_id,
        "char_span": [0, len(brief_text)],
        "content_hash": _sha(brief_text),
    }]
    localization_proof = [{
        "candidate_id": candidate_id,
        "rank": 1,
        "path": "src/pkg/loader.py",
        "components": {"lex": 0.8},
        "acquisition_sources": {},
    }]
    # Seal the attestation the way v1r_brief does at generation time — BEFORE any
    # determinism witness exists.
    _attest_source_contributions(localization_proof, receipts)
    return SimpleNamespace(
        brief_text=brief_text,
        effective_w_sem=0.4,
        semantic_signal_count=1,
        rendered_candidate_count=1,
        k_sem_top=1,
        sem_components=[0.7],
        localization_proof=localization_proof,
        graph_edge_count=1,
        structural_signal_count=0,
        fts5_signal_count=1,
        block_receipts=receipts,
        tokenizer_used="char4-estimate",
        budget_suppressed=[],
    )


def test_verify_independent_generation_reseals_attestation_for_determinism(tmp_path):
    text = "1. src/pkg/loader.py"
    primary = brief_cache.persist_brief(
        str(tmp_path), text, _cochange_block_result(text), identity="rid",
    )
    # Sanity: the generation-time seal did NOT (and could not) name determinism.
    assert "determinism" not in (
        primary["metrics"]["localization_proof"][0]["contribution_attestation"]["sources"]
    )

    verdict = brief_cache.verify_independent_generation(
        str(tmp_path), primary,
        lambda: _cochange_block_result(text),
        expect_identity="rid",
    )
    assert verdict["matched"] is True

    loaded = brief_cache.load_cached_brief(str(tmp_path), expect_identity="rid")
    proof = loaded["metrics"]["localization_proof"][0]
    # The witness reached acquisition_sources (existing behaviour)...
    assert "determinism" in proof["acquisition_sources"]
    # ...and (RED pre-fix) the sealed attestation was rebuilt to name it, with a
    # self-consistent seal.
    attestation = proof["contribution_attestation"]
    assert "determinism" in attestation["sources"]

    import hashlib
    import json as _json
    unsigned = {k: v for k, v in attestation.items() if k != "attestation_sha256"}
    canonical = _json.dumps(
        unsigned, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    )
    assert hashlib.sha256(canonical.encode("utf-8")).hexdigest() == attestation[
        "attestation_sha256"
    ]
