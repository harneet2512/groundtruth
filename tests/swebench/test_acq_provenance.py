"""Fail-closed source -> sealed brief block -> assistant receipt joins."""

from __future__ import annotations

import hashlib
import copy
import json
import sys
from pathlib import Path

import pytest

from scripts.swebench.acq_provenance import collect_acq_provenance
from scripts.swebench.consumption_ledger import build_consumption_ledger
from scripts.swebench.gt_feature_inventory import ACQ_FEATURES
from groundtruth.pretask.v1r_brief import _brief_block_receipts, _reduce_brief_to_minimal


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _runtime_join(tmp_path, brief, trajectory, *, producer_seal):
    if not producer_seal:
        return build_consumption_ledger(trajectory)
    # Collector tests start after the canonical provider boundary has proved an
    # exact terminal delivery. Do not route this fixture through the headless
    # runner: task construction owns native task bytes only and is not a
    # delivery writer.
    ledger_path = tmp_path / "gt_runtime_ledger_task.jsonl"
    ledger_path.write_text(
        json.dumps({
            "layer": "canonical_runtime.capsule",
            "event_type": "provider_terminal",
            "file_path": "",
            "outcome": "delivered",
            "chars_delivered": len(brief),
            "content_sha256_16": _sha(brief)[:16],
            "iteration": 0,
        }) + "\n",
        encoding="utf-8",
    )
    return build_consumption_ledger(
        trajectory, runtime_ledger_path=str(ledger_path)
    )


def _artifacts(
    tmp_path,
    *,
    receipt: int = 2,
    producer_seal: bool = True,
    extended_sources: bool = False,
):
    # Production shape: GT_BRIEF_NATIVE has already rendered plain obligations;
    # GT_BRIEF_MINIMAL removes localization narration and reduces file entries
    # to their rank/path header before block receipts are computed.
    full = "\n".join([
        "<gt-task-brief>",
        '<gt-localization confidence="HIGH">',
        "src/pkg/loader.py",
        "</gt-localization>",
        "Requirements to satisfy (from the issue):",
        "- [ ] preserve loader behavior",
        "",
        "1. src/pkg/loader.py",
        "    Signature: load(config)",
        "    Callers: run",
        "</gt-task-brief>",
    ])
    brief = _reduce_brief_to_minimal(full)
    candidate_id = "localization:src/pkg/loader.py"
    receipts = _brief_block_receipts(
        brief, localization_candidate_ids=[candidate_id]
    )
    payload = {
        "schema": "gt.brief_result.v1",
        "brief_text": brief,
        "brief_sha256": _sha(brief),
        "metrics": {
            "graph_edge_count": 4,
            "structural_signal_count": 1,
            "fts5_signal_count": 1,
            "semantic_signal_count": 1,
            "localization_proof": [{
                "candidate_id": candidate_id,
                "rank": 1,
                "path": "src/pkg/loader.py",
                "witness": "load called by run [CALLS]",
                "witness_verified": True,
                "components": {"reach": 0.5, "lex": 0.8, "sem": 0.7},
                "acquisition_sources": ({
                    "resolution_honesty": {
                        "kind": "resolution_methods",
                        "methods": ["import", "type_flow", "lsp"],
                        "all_verified": True,
                    },
                    "type_intelligence": {
                        "kind": "type_resolution",
                        "methods": ["type_flow"],
                    },
                    "LSP": {
                        "kind": "lsp_resolution",
                        "methods": ["lsp"],
                    },
                    "freshness_basis": {
                        "kind": "content_revision",
                        "indexed_sha256": "a" * 64,
                        "observed_sha256": "a" * 64,
                    },
                    "repo_scope": {
                        "kind": "repo_partition",
                        "is_multi_repo": True,
                        "resolved": True,
                        "active_repo_id": 7,
                        "candidate_repo_id": 7,
                    },
                    "determinism": {
                        "kind": "repeat_identity",
                        "runs": 2,
                        "canonical_sha256": ["b" * 64, "b" * 64],
                    },
                } if extended_sources else {}),
            }],
            "block_receipts": receipts,
        },
    }
    trajectory = {
        "messages": [
            {"role": "user", "content": brief + "\n\nFix the issue."},
            {
                "role": "assistant",
                "content": (
                    "I will preserve src/pkg/loader.py behavior."
                    if receipt >= 2 else "I will continue."
                ),
            },
        ]
    }
    ledger = _runtime_join(
        tmp_path, brief, trajectory, producer_seal=producer_seal
    )
    return payload, ledger, trajectory


def test_real_v2_producer_seal_is_split_into_production_shaped_source_receipts(tmp_path):
    payload, ledger, trajectory = _artifacts(tmp_path)
    assert "<gt-localization" not in payload["brief_text"]
    assert "Requirements to satisfy (from the issue):" in payload["brief_text"]
    assert "Signature:" not in payload["brief_text"]
    assert ledger["entries"][0]["rendered_text"] == payload["brief_text"]
    assert ledger["entries"][0]["joined"] is True
    assert ledger["entries"][0]["join_method"] == "seal"
    rows = collect_acq_provenance(payload, ledger, trajectory)

    assert tuple(rows) == ACQ_FEATURES
    assert rows["graph_validity"]["status"] == "MEASURED"
    assert rows["structural_depth"]["status"] == "MEASURED"
    assert rows["lexical_FTS5"]["status"] == "MEASURED"
    assert rows["semantic_embedder"]["status"] == "MEASURED"
    assert rows["graph_validity"]["receipt_level"] == 2
    file_receipt = next(
        r for r in payload["metrics"]["block_receipts"]
        if r["block_id"] == "file-entry-1"
    )
    assert rows["graph_validity"]["block_content_sha256_16"] == _sha(
        payload["brief_text"][
            file_receipt["char_span"][0]:file_receipt["char_span"][1]
        ]
    )[:16]
    # No structured source witness exists for these rows in brief_result.v1.
    for feature in (
        "resolution_honesty", "type_intelligence", "LSP",
        "freshness_basis", "repo_scope", "determinism",
    ):
        assert rows[feature]["status"] == "UNMEASURED"


def test_acquisition_namespace_joins_matching_delivered_candidate(tmp_path):
    payload, ledger, trajectory = _artifacts(tmp_path)
    payload["metrics"]["acquisition_proof"] = [
        copy.deepcopy(payload["metrics"]["localization_proof"][0])
    ]

    rows = collect_acq_provenance(payload, ledger, trajectory)

    for feature in (
        "graph_validity",
        "structural_depth",
        "lexical_FTS5",
        "semantic_embedder",
    ):
        assert rows[feature]["status"] == "MEASURED"
        assert any(
            field.startswith("metrics.acquisition_proof[0]")
            for field in rows[feature]["source_fields"]
        )


def test_acquisition_namespace_never_borrows_another_candidates_delivery(tmp_path):
    payload, ledger, trajectory = _artifacts(tmp_path)
    acquisition = copy.deepcopy(payload["metrics"]["localization_proof"][0])
    acquisition.update({
        "candidate_id": "localization:src/pkg/other.py",
        "path": "src/pkg/other.py",
    })
    payload["metrics"]["acquisition_proof"] = [acquisition]

    rows = collect_acq_provenance(payload, ledger, trajectory)

    for feature in (
        "graph_validity",
        "structural_depth",
        "lexical_FTS5",
        "semantic_embedder",
    ):
        assert rows[feature]["status"] == "UNMEASURED"
        assert rows[feature]["blocker"] == "candidate_delivery_absent"
        assert rows[feature]["candidate_id"] == "localization:src/pkg/other.py"


def test_missing_acquisition_namespace_preserves_legacy_delivery_join(tmp_path):
    payload, ledger, trajectory = _artifacts(tmp_path)
    assert "acquisition_proof" not in payload["metrics"]

    rows = collect_acq_provenance(payload, ledger, trajectory)

    for feature in (
        "graph_validity",
        "structural_depth",
        "lexical_FTS5",
        "semantic_embedder",
    ):
        assert rows[feature]["status"] == "MEASURED"
        assert any(
            field.startswith("metrics.localization_proof[0]")
            for field in rows[feature]["source_fields"]
        )


def test_minimal_contention_candidate_joins_shared_header_bytes(tmp_path):
    """A retained MEDIUM candidate keeps source->block->seal->receipt lineage."""
    payload, _, trajectory = _artifacts(tmp_path)
    brief = "\n".join([
        '<gt-localization confidence="medium">',
        "Candidate edit targets (reason over these — confirm the edit target with grep):",
        "  1. ./.github/scripts/check.py — load",
        "  2. src/pkg/other.py — parse",
        "</gt-localization>",
        "<gt-task-brief>",
        "Requirements to satisfy (from the issue):",
        "- [ ] preserve loader behavior",
        "</gt-task-brief>",
    ])
    candidate_id = "localization:.github/scripts/check.py"
    payload["brief_text"] = brief
    payload["brief_sha256"] = _sha(brief)
    payload["metrics"]["localization_proof"][0]["path"] = (
        "./.github/scripts/check.py"
    )
    payload["metrics"]["localization_proof"][0]["candidate_id"] = candidate_id
    payload["metrics"]["block_receipts"] = _brief_block_receipts(
        brief,
        localization_candidate_ids=[
            candidate_id, "localization:src/pkg/other.py",
        ],
    )
    trajectory["messages"][0]["content"] = brief + "\n\nFix the issue."
    trajectory["messages"][1]["content"] = (
        "I will preserve ./.github/scripts/check.py behavior."
    )
    ledger = _runtime_join(tmp_path, brief, trajectory, producer_seal=True)

    row = collect_acq_provenance(payload, ledger, trajectory)["graph_validity"]

    assert row["status"] == "MEASURED"
    assert row["receipt_level"] == 2
    assert row["block_id"] == "localization-header:candidate-1"
    receipt = next(
        item for item in payload["metrics"]["block_receipts"]
        if item.get("candidate_id") == candidate_id
    )
    start, end = receipt["char_span"]
    assert brief[start:end] == "  1. ./.github/scripts/check.py — load"
    assert row["block_content_sha256_16"] == receipt["content_hash"][:16]


def test_typed_candidate_local_sources_join_to_the_same_sealed_file_receipt(tmp_path):
    payload, ledger, trajectory = _artifacts(tmp_path, extended_sources=True)
    rows = collect_acq_provenance(payload, ledger, trajectory)

    for feature in (
        "resolution_honesty", "type_intelligence", "LSP",
        "freshness_basis", "repo_scope", "determinism",
    ):
        assert rows[feature]["status"] == "MEASURED"
        assert rows[feature]["block_id"] == "file-entry-1"
        assert rows[feature]["receipt_level"] == 2


def test_acq_row_preserves_source_block_and_actual_producer_seal_identities(tmp_path):
    payload, ledger, trajectory = _artifacts(tmp_path, extended_sources=True)
    row = collect_acq_provenance(payload, ledger, trajectory)["freshness_basis"]
    producer = ledger["entries"][0]
    block = next(
        receipt
        for receipt in payload["metrics"]["block_receipts"]
        if receipt["block_id"] == "file-entry-1"
    )

    # Gate 1 is owned by the producer seal.  The candidate block has a distinct
    # identity that proves which bytes inside that sealed payload came from ACQ.
    assert row["content_sha256_16"] == producer["content_sha256_16"]
    assert row["chars_delivered"] == producer["chars"]
    assert row["block_content_sha256_16"] == block["content_hash"][:16]
    assert row["block_char_span"] == block["char_span"]
    assert row["producer_payload_scope"] == "whole_brief"
    assert row["producer_entry_index"] == 0
    assert row["producer_ledger_layer"] == producer["ledger_layer"]
    assert row["delivery_message_index"] == 0
    assert row["receipt_evidence"] == {
        "referenced_message_index": 1,
        "acted_message_index": None,
    }
    assert row["source_artifact"] == "brief_result.json"
    assert row["source_fields"] == [
        "metrics.localization_proof[0].acquisition_sources.freshness_basis"
    ]

    # A source-to-receipt join is not source truth, chronological timing, or a
    # causal fair probe.  Those remain unknown until their own live authorities join.
    assert row["source_contribution_correct"] is None
    assert row["timing_inherited_from_fact_delivery"] is None
    assert row["source_causal_fair_probe"] is None


def test_exact_block_producer_seal_is_a_valid_gate_one_identity(tmp_path):
    payload, _, trajectory = _artifacts(tmp_path, extended_sources=True)
    block_receipt = next(
        receipt for receipt in payload["metrics"]["block_receipts"]
        if receipt["block_id"] == "file-entry-1"
    )
    start, end = block_receipt["char_span"]
    block = payload["brief_text"][start:end]
    ledger_path = tmp_path / "exact_block_runtime_ledger.jsonl"
    ledger_path.write_text(json.dumps({
        "layer": "canonical_runtime.capsule",
        "event_type": "provider_terminal",
        "file_path": "src/pkg/loader.py",
        "outcome": "delivered",
        "chars_delivered": len(block),
        "content_sha256_16": _sha(block)[:16],
        "iteration": 0,
    }) + "\n", encoding="utf-8")
    ledger = build_consumption_ledger(
        trajectory, runtime_ledger_path=str(ledger_path)
    )

    row = collect_acq_provenance(payload, ledger, trajectory)["freshness_basis"]
    assert row["status"] == "MEASURED"
    assert row["content_sha256_16"] == _sha(block)[:16]
    assert row["chars_delivered"] == len(block)
    assert row["producer_payload_scope"] == "exact_block"


@pytest.mark.parametrize(
    ("field", "bad_value"),
    [("chars", "1"), ("ledger_chars", 1.0), ("msg_index", False)],
)
def test_malformed_producer_seal_types_fail_closed(tmp_path, field, bad_value):
    payload, ledger, trajectory = _artifacts(tmp_path)
    ledger["entries"][0][field] = bad_value

    row = collect_acq_provenance(payload, ledger, trajectory)["graph_validity"]
    assert row["status"] == "UNMEASURED"
    assert row["content_sha256_16"] is None
    assert row["chars_delivered"] is None
    assert row["blocker"] == "producer_seal_absent"


def test_strongest_receipt_wins_with_first_candidate_as_stable_tie_break(tmp_path):
    payload, _, trajectory = _artifacts(tmp_path, receipt=1)
    brief = payload["brief_text"].replace(
        "</gt-task-brief>", "2. src/pkg/other.py\n</gt-task-brief>"
    )
    payload["brief_text"] = brief
    payload["brief_sha256"] = _sha(brief)
    payload["metrics"]["block_receipts"] = _brief_block_receipts(brief)
    second = copy.deepcopy(payload["metrics"]["localization_proof"][0])
    second.update({"rank": 2, "path": "src/pkg/other.py"})
    payload["metrics"]["localization_proof"].append(second)
    trajectory["messages"][0]["content"] = brief + "\n\nFix the issue."
    trajectory["messages"][1] = {
        "role": "assistant",
        "content": "I will inspect src/pkg/loader.py.",
        "extra": {"actions": [{"command": "pytest src/pkg/loader.py"}]},
    }
    trajectory["messages"].append({
        "role": "assistant", "content": "I will inspect src/pkg/other.py."
    })
    ledger = _runtime_join(tmp_path, brief, trajectory, producer_seal=True)

    row = collect_acq_provenance(payload, ledger, trajectory)["graph_validity"]
    assert row["receipt_level"] == 3
    assert row["block_id"] == "file-entry-1"
    assert row["receipt_evidence"]["acted_message_index"] == 1


def test_every_acq_surface_names_its_exact_source_fields(tmp_path):
    payload, ledger, trajectory = _artifacts(tmp_path, extended_sources=True)
    components = payload["metrics"]["localization_proof"][0]["components"]
    components.update({"body": 1.0, "cochange": 3.0})
    payload["metrics"]["localization_proof"][0]["cochange_evidence"] = (
        _cochange_evidence(count=3)
    )
    rows = collect_acq_provenance(payload, ledger, trajectory)

    expected = {
        "graph_validity": [
            "metrics.graph_edge_count",
            "metrics.localization_proof[0].witness_verified",
            "metrics.localization_proof[0].witness",
        ],
        "structural_depth": [
            "metrics.structural_signal_count",
            "metrics.localization_proof[0].components.reach",
        ],
        "lexical_FTS5": [
            "metrics.fts5_signal_count",
            "metrics.localization_proof[0].components.lex",
        ],
        "semantic_embedder": [
            "metrics.semantic_signal_count",
            "metrics.localization_proof[0].components.sem",
        ],
        "body_retrieval": [
            "metrics.localization_proof[0].components.body",
        ],
        "cochange_history": [
            "metrics.localization_proof[0].components.cochange",
            "metrics.localization_proof[0].cochange_evidence",
        ],
    }
    expected.update({
        feature: [
            f"metrics.localization_proof[0].acquisition_sources.{feature}"
        ]
        for feature in (
            "resolution_honesty", "type_intelligence", "LSP",
            "freshness_basis", "repo_scope", "determinism",
        )
    })
    assert tuple(rows) == ACQ_FEATURES
    assert {feature: rows[feature]["source_fields"] for feature in rows} == expected


@pytest.mark.parametrize(
    ("feature", "mutation"),
    [
        ("resolution_honesty", {"all_verified": False}),
        ("type_intelligence", {"methods": ["name_match"]}),
        ("LSP", {"methods": ["import"]}),
        ("freshness_basis", {"observed_sha256": "c" * 64}),
        ("repo_scope", {"candidate_repo_id": 8}),
        ("determinism", {"canonical_sha256": ["b" * 64, "c" * 64]}),
    ],
)
def test_extended_source_claims_fail_closed_when_their_own_proof_disagrees(
    tmp_path, feature, mutation,
):
    payload, ledger, trajectory = _artifacts(tmp_path, extended_sources=True)
    source = payload["metrics"]["localization_proof"][0]["acquisition_sources"][feature]
    source.update(mutation)

    row = collect_acq_provenance(payload, ledger, trajectory)[feature]
    assert row["status"] == "UNMEASURED"


@pytest.mark.parametrize("mutation", ["hash", "span", "source", "source_suffix"])
def test_hash_span_and_source_mismatch_never_promote(tmp_path, mutation):
    payload, ledger, trajectory = _artifacts(tmp_path)
    if mutation == "hash":
        ledger["entries"][0]["content_sha256_16"] = "0" * 16
    elif mutation == "span":
        ledger["entries"][0]["chars"] -= 1
    elif mutation == "source":
        payload["metrics"]["localization_proof"][0]["path"] = "src/other.py"
    else:
        # Substring/basename agreement is not source identity.
        payload["metrics"]["localization_proof"][0]["path"] = "pkg/loader.py"

    rows = collect_acq_provenance(payload, ledger, trajectory)
    assert rows["graph_validity"]["status"] == "UNMEASURED"


def test_silent_receipt_is_delivered_but_never_promoted(tmp_path):
    payload, ledger, trajectory = _artifacts(tmp_path, receipt=1)
    rows = collect_acq_provenance(payload, ledger, trajectory)
    row = rows["lexical_FTS5"]
    assert row["status"] == "UNMEASURED"
    assert row["receipt_level"] == 1
    assert row["blocker"] == "assistant_receipt_below_2"


def test_whole_brief_receipt_for_another_block_is_not_inherited(tmp_path):
    payload, _, trajectory = _artifacts(tmp_path, receipt=1)
    extra = "2. src/other.py\n"
    brief = payload["brief_text"].replace("</gt-task-brief>", extra + "</gt-task-brief>")
    payload["brief_text"] = brief
    payload["brief_sha256"] = _sha(brief)
    trajectory["messages"][0]["content"] = brief + "\n\nFix the issue."
    trajectory["messages"][1]["content"] = "I will inspect src/other.py."
    ledger = _runtime_join(tmp_path, brief, trajectory, producer_seal=True)

    assert ledger["entries"][0]["receipt"] == 2
    row = collect_acq_provenance(payload, ledger, trajectory)["graph_validity"]
    assert row["status"] == "UNMEASURED"
    assert row["receipt_level"] == 1


def test_absent_artifacts_return_exact_unmeasured_inventory():
    rows = collect_acq_provenance(None, {}, {})
    assert tuple(rows) == ACQ_FEATURES
    assert all(row["status"] == "UNMEASURED" for row in rows.values())


def test_malformed_claimed_block_proof_fails_loudly(tmp_path):
    payload, ledger, trajectory = _artifacts(tmp_path)
    payload["metrics"]["block_receipts"][0]["content_hash"] = "f" * 64
    with pytest.raises(ValueError, match="block content hash mismatch"):
        collect_acq_provenance(payload, ledger, trajectory)


def test_auditor_recomputed_tag_entry_without_producer_seal_never_promotes(tmp_path):
    payload, ledger, trajectory = _artifacts(tmp_path, producer_seal=False)
    assert ledger["entries"][0]["joined"] is not True

    row = collect_acq_provenance(payload, ledger, trajectory)["graph_validity"]
    assert row["status"] == "UNMEASURED"
    assert row["blocker"] == "producer_seal_absent"
    assert row["content_sha256_16"] is None
    assert row["chars_delivered"] is None


def test_nonseal_join_method_never_promotes_acquisition(tmp_path):
    payload, ledger, trajectory = _artifacts(tmp_path)
    ledger["entries"][0]["join_method"] = "text_containment"

    row = collect_acq_provenance(payload, ledger, trajectory)["graph_validity"]
    assert row["status"] == "UNMEASURED"
    assert row["blocker"] == "producer_seal_absent"
    assert row["content_sha256_16"] is None
    assert row["chars_delivered"] is None


def test_candidate_local_body_and_history_sources_require_an_acted_receipt(tmp_path):
    payload, ledger, trajectory = _artifacts(tmp_path)
    components = payload["metrics"]["localization_proof"][0]["components"]
    components.update({"body": 1.0, "cochange": 3.0})
    payload["metrics"]["localization_proof"][0]["cochange_evidence"] = (
        _cochange_evidence(count=3)
    )
    trajectory["messages"][1]["extra"] = {
        "actions": [{"command": "pytest src/pkg/loader.py"}]
    }

    rows = collect_acq_provenance(payload, ledger, trajectory)
    assert rows["body_retrieval"]["status"] == "MEASURED"
    assert rows["cochange_history"]["status"] == "MEASURED"
    assert rows["body_retrieval"]["receipt_level"] == 3
    assert rows["cochange_history"]["receipt_level"] == 3


def test_cochange_history_preserves_candidate_to_downstream_fact_join(tmp_path):
    payload, ledger, trajectory = _artifacts(tmp_path)
    payload["metrics"]["localization_proof"][0]["components"]["cochange"] = 3.0
    payload["metrics"]["localization_proof"][0]["cochange_evidence"] = (
        _cochange_evidence(count=3)
    )

    row = collect_acq_provenance(payload, ledger, trajectory)["cochange_history"]

    assert row["candidate_id"] == "localization:src/pkg/loader.py"
    assert row["supported_fact_class"] == "localization"
    assert row["candidate_path"] == "src/pkg/loader.py"
    assert row["status"] == "MEASURED"
    assert row["content_sha256_16"] == ledger["entries"][0]["content_sha256_16"]
    assert row["chars_delivered"] == ledger["entries"][0]["chars"]
    assert row["delivery_message_index"] == 0
    assert row["receipt_level"] == 2
    assert row["receipt_evidence"]["referenced_message_index"] == 1
    assert row["source_contribution_correct"] is True


def _cochange_evidence(*, count: int = 3, source_revision: str | None = None) -> dict:
    from groundtruth.pretask.v1r_brief import _cochange_evidence

    revisions = [chr(ord("a") + index) * 40 for index in range(count)]
    return _cochange_evidence(
        candidate_path="src/pkg/loader.py",
        source_revision=source_revision or revisions[0],
        source_rows=[
            {"commit": revision, "symptom_paths": ["src/symptom.py"]}
            for revision in revisions
        ],
        history_limit=100,
    )


def test_cochange_history_rejects_aggregate_without_exact_producer_witness(tmp_path):
    payload, ledger, trajectory = _artifacts(tmp_path)
    payload["metrics"]["localization_proof"][0]["components"]["cochange"] = 3.0

    row = collect_acq_provenance(payload, ledger, trajectory)["cochange_history"]

    assert row["status"] == "UNMEASURED"
    assert row["blocker"] == "source_witness_absent"
    assert row["source_contribution_correct"] is None


def test_cochange_history_rejects_tampered_source_count(tmp_path):
    payload, ledger, trajectory = _artifacts(tmp_path)
    proof = payload["metrics"]["localization_proof"][0]
    proof["components"]["cochange"] = 3.0
    proof["cochange_evidence"] = _cochange_evidence(count=3)
    proof["cochange_evidence"]["count"] = 4

    row = collect_acq_provenance(payload, ledger, trajectory)["cochange_history"]

    assert row["status"] == "UNMEASURED"
    assert row["source_contribution_correct"] is None


def test_cochange_history_rejects_source_row_changed_after_identity(tmp_path):
    payload, ledger, trajectory = _artifacts(tmp_path)
    proof = payload["metrics"]["localization_proof"][0]
    proof["components"]["cochange"] = 3.0
    proof["cochange_evidence"] = _cochange_evidence(count=3)
    proof["cochange_evidence"]["source_rows"][1]["symptom_paths"].append(
        "src/zzz.py"
    )

    row = collect_acq_provenance(payload, ledger, trajectory)["cochange_history"]

    assert row["status"] == "UNMEASURED"
    assert row["source_contribution_correct"] is None


def test_cochange_history_accepts_newer_snapshot_than_contributing_rows(tmp_path):
    payload, ledger, trajectory = _artifacts(tmp_path)
    proof = payload["metrics"]["localization_proof"][0]
    proof["components"]["cochange"] = 3.0
    proof["cochange_evidence"] = _cochange_evidence(
        count=3, source_revision="f" * 40,
    )

    row = collect_acq_provenance(payload, ledger, trajectory)["cochange_history"]

    assert row["status"] == "MEASURED"
    assert row["source_contribution_correct"] is True


def test_task_collector_joins_cochange_internal_support_to_localization_receipt(
    tmp_path,
):
    scripts = str(Path(__file__).resolve().parents[2] / "scripts" / "swebench")
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    import gt_feature_metrics

    payload, _ledger, trajectory = _artifacts(tmp_path)
    payload["metrics"]["localization_proof"][0]["components"]["cochange"] = 3.0
    payload["metrics"]["localization_proof"][0]["cochange_evidence"] = (
        _cochange_evidence(count=3)
    )
    (tmp_path / "brief_result.json").write_text(
        json.dumps(payload), encoding="utf-8"
    )
    (tmp_path / "mini-swe-agent.trajectory.json").write_text(
        json.dumps(trajectory), encoding="utf-8"
    )

    record = gt_feature_metrics.collect_task("org__repo-1", str(tmp_path))
    readiness = record["ss_features"]["cochange_prior"]["ss_readiness"]

    assert readiness["role"] == "internal_support"
    assert readiness["candidate_id"] == "localization:src/pkg/loader.py"
    assert readiness["supported_fact_class"] == "localization"
    assert readiness["gates"]["runtime_support_receipt"] is True
    assert readiness["gates"]["supported_candidate_id"] is True
    assert readiness["gates"]["downstream_decision_join"] is True
    assert readiness["gates"]["support_correct"] is True
    assert readiness["gates"]["support_causal_fair_probe"] is None


def test_zero_candidate_repeat_identity_is_observed_without_delivery_promotion(tmp_path):
    payload, _, _ = _artifacts(tmp_path)
    digest = "a" * 64
    payload["metrics"]["localization_proof"] = []
    payload["metrics"]["block_receipts"] = []
    payload["metrics"]["determinism_witness"] = {
        "kind": "repeat_identity",
        "runs": 2,
        "canonical_sha256": [digest, digest],
    }

    row = collect_acq_provenance(payload, {}, None)["determinism"]

    assert row["status"] == "UNMEASURED"
    assert row["source_artifact"] == "brief_result.json"
    assert row["source_fields"] == ["metrics.determinism_witness"]
    assert row["blocker"] == "candidate_delivery_absent"
    assert row["content_sha256_16"] is None
    assert row["chars_delivered"] is None
    assert row["receipt_level"] is None
