"""Fail-closed source -> sealed brief block -> assistant receipt joins."""

from __future__ import annotations

import hashlib

import pytest

from scripts.swebench.acq_provenance import collect_acq_provenance
from scripts.swebench.consumption_ledger import build_consumption_ledger
from scripts.swebench.gt_feature_inventory import ACQ_FEATURES
from groundtruth.pretask.v1r_brief import _brief_block_receipts, _reduce_brief_to_minimal
from artifact_deepswe import gt_headless_runner


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _runtime_join(tmp_path, brief, trajectory, *, producer_seal):
    if not producer_seal:
        return build_consumption_ledger(trajectory)
    brief_path = tmp_path / "brief.txt"
    ledger_path = tmp_path / "gt_runtime_ledger_task.jsonl"
    brief_path.write_text(brief, encoding="utf-8", newline="\n")
    delivered_task = gt_headless_runner._resolve_task({
        "GT_RUN_TASK": "Fix the issue.",
        "GT_BRIEF_FILE": str(brief_path),
        "GT_INSEAM_METRICS": "1",
        "GT_RUNTIME_LEDGER": str(ledger_path),
    })
    trajectory["messages"][0]["content"] = delivered_task
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
    receipts = _brief_block_receipts(brief)
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
    assert rows["graph_validity"]["content_sha256_16"] == _sha(
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


def test_nonseal_join_method_never_promotes_acquisition(tmp_path):
    payload, ledger, trajectory = _artifacts(tmp_path)
    ledger["entries"][0]["join_method"] = "text_containment"

    row = collect_acq_provenance(payload, ledger, trajectory)["graph_validity"]
    assert row["status"] == "UNMEASURED"
    assert row["blocker"] == "producer_seal_absent"


def test_candidate_local_body_and_history_sources_require_an_acted_receipt(tmp_path):
    payload, ledger, trajectory = _artifacts(tmp_path)
    components = payload["metrics"]["localization_proof"][0]["components"]
    components.update({"body": 0.6, "commit": 0.4})
    trajectory["messages"][1]["extra"] = {
        "actions": [{"command": "pytest src/pkg/loader.py"}]
    }

    rows = collect_acq_provenance(payload, ledger, trajectory)
    assert rows["body_retrieval"]["status"] == "MEASURED"
    assert rows["cochange_history"]["status"] == "MEASURED"
    assert rows["body_retrieval"]["receipt_level"] == 3
