"""C16: acquisition witnesses must survive localization delivery re-slotting."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from groundtruth.pretask import v1r_brief as vb
from groundtruth.runtime import brief_cache
from scripts.swebench import acq_provenance
from scripts.swebench.acq_provenance import collect_acq_provenance
from scripts.swebench.gt_feature_inventory import ACQ_FEATURES
from scripts.swebench.ss_proof_manifest import _acq_row


ISSUE = "SafeWatchdog._fd leaks the watchdog file descriptor when the postmaster restarts"
CORE_ACQUISITION_FEATURES = (
    "graph_validity",
    "structural_depth",
    "lexical_FTS5",
    "semantic_embedder",
)
DELIVERY_FEATURES = tuple(
    feature for feature in ACQ_FEATURES if feature not in CORE_ACQUISITION_FEATURES
)


@pytest.fixture
def repo_root(tmp_path: Path) -> str:
    pkg = tmp_path / "repo" / "patroni"
    pkg.mkdir(parents=True)
    (pkg / "watchdog.py").write_text(
        "class SafeWatchdog:\n    def _fd(self):\n        return 1\n",
        encoding="utf-8",
    )
    (pkg / "postmaster.py").write_text(
        "class Postmaster:\n    pass\n",
        encoding="utf-8",
    )
    return str(tmp_path / "repo")


def _reslotted_brief(graph_db: str, repo: str, monkeypatch):
    monkeypatch.setenv("GT_BRIEF_MINIMAL", "1")
    monkeypatch.setenv("GT_LOC_RESLOT", "1")
    return vb.generate_v1r_brief(
        ISSUE, repo, graph_db, bug_id="c16", repo="patroni",
    )


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _acquisition_only_payload() -> dict:
    brief = "Requirements to satisfy (from the issue):\n- preserve behavior"
    return {
        "schema": "gt.brief_result.v1",
        "brief_text": brief,
        "brief_sha256": _sha(brief),
        "metrics": {
            "acquired_graph_edge_count": 1,
            "acquired_structural_signal_count": 1,
            "acquired_fts5_signal_count": 1,
            "acquired_semantic_signal_count": 1,
            "acquisition_proof": [{
                "candidate_id": "localization:src/pkg/loader.py",
                "rank": 1,
                "path": "src/pkg/loader.py",
                "witness": "load called by run [CALLS]",
                "witness_verified": True,
                "components": {"reach": 0.5, "lex": 0.8, "sem": 0.7},
            }],
            "localization_proof": [],
            "block_receipts": [],
        },
    }


def test_real_reslot_keeps_acquisition_proof_and_delivery_proof_empty(
    tiny_graph_db, repo_root, monkeypatch,
):
    result = _reslotted_brief(tiny_graph_db, repo_root, monkeypatch)

    assert result.files == []
    assert result.localization_proof == []
    assert result.acquisition_proof
    evidenced = {
        feature
        for proof in result.acquisition_proof
        for feature in acq_provenance._acquisition_source_features(proof, vars(result))
    }
    # This fixture intentionally contains only name_match edges, so graph_validity
    # remains correct-quiet rather than laundering an unresolved edge as verified.
    assert evidenced == {
        "structural_depth",
        "lexical_FTS5",
        "semantic_embedder",
    }
    assert all(
        "contribution_attestation" not in proof
        and "cochange_evidence" not in proof
        and "acquisition_sources" not in proof
        for proof in result.acquisition_proof
    )


def test_acquisition_proof_crosses_cache_without_changing_brief_bytes(
    tiny_graph_db, repo_root, monkeypatch, tmp_path,
):
    result = _reslotted_brief(tiny_graph_db, repo_root, monkeypatch)
    before = result.brief_text.encode("utf-8")

    persisted = brief_cache.persist_brief(
        str(tmp_path), result.brief_text, result, identity="c16",
    )

    assert persisted["brief_text"].encode("utf-8") == before
    assert persisted["metrics"]["acquisition_proof"] == result.acquisition_proof
    assert persisted["metrics"]["localization_proof"] == []


def test_acquisition_projection_is_model_byte_identical_when_disabled(
    tiny_graph_db, repo_root, monkeypatch,
):
    original = vb._acquisition_proof_rows
    monkeypatch.setattr(vb, "_acquisition_proof_rows", lambda *args, **kwargs: [])
    without_projection = _reslotted_brief(
        tiny_graph_db, repo_root, monkeypatch,
    )
    monkeypatch.setattr(vb, "_acquisition_proof_rows", original)
    with_projection = _reslotted_brief(
        tiny_graph_db, repo_root, monkeypatch,
    )

    assert without_projection.acquisition_proof == []
    assert with_projection.acquisition_proof
    assert (
        without_projection.brief_text.encode("utf-8")
        == with_projection.brief_text.encode("utf-8")
    )


def test_acquisition_only_rows_name_source_without_fabricating_delivery():
    rows = collect_acq_provenance(_acquisition_only_payload(), {}, None)

    for feature in CORE_ACQUISITION_FEATURES:
        row = rows[feature]
        assert row["status"] == "UNMEASURED"
        assert row["source_artifact"] == "brief_result.json"
        assert row["blocker"] == "candidate_delivery_absent"
        assert row["source_fields"]
        assert any(
            "metrics.acquisition_proof[0]" in field
            for field in row["source_fields"]
        )
        assert row["content_sha256_16"] is None
        assert row["chars_delivered"] is None
        assert row["block_id"] is None
        assert row["receipt_level"] is None

    for feature in DELIVERY_FEATURES:
        row = rows[feature]
        assert row["source_artifact"] is None
        assert row["source_fields"] == []
        assert row["blocker"] == "source_witness_absent"


def test_only_four_manifest_consumers_move_to_acquisition_proof():
    assert acq_provenance.ACQUISITION_PROOF_FEATURES == frozenset(
        CORE_ACQUISITION_FEATURES
    )
    for feature in ACQ_FEATURES:
        expected = (
            "brief_result.json#metrics.acquisition_proof"
            if feature in CORE_ACQUISITION_FEATURES
            else "brief_result.json#metrics.localization_proof"
        )
        assert _acq_row(feature)["eligibility"]["authority"] == expected


@pytest.mark.parametrize("new_field", ["missing", "empty"])
def test_historical_delivery_proof_keeps_core_attribution(new_field):
    brief = "<gt-task-brief>\n1. src/pkg/loader.py\n</gt-task-brief>"
    candidate_id = "localization:src/pkg/loader.py"
    metrics = {
        "graph_edge_count": 1,
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
        }],
        "block_receipts": vb._brief_block_receipts(
            brief, localization_candidate_ids=[candidate_id],
        ),
    }
    if new_field == "empty":
        metrics["acquisition_proof"] = []
    payload = {
        "schema": "gt.brief_result.v1",
        "brief_text": brief,
        "brief_sha256": _sha(brief),
        "metrics": metrics,
    }
    trajectory = {
        "messages": [
            {"role": "user", "content": brief},
            {
                "role": "assistant",
                "content": "I will preserve src/pkg/loader.py behavior.",
            },
        ],
    }
    ledger = {
        "schema": "gt.consumption_ledger.v2",
        "entries": [{
            "source": "trajectory",
            "joined": True,
            "join_method": "seal",
            "ledger_layer": "brief.localization",
            "rendered_text": brief,
            "content_sha256_16": _sha(brief)[:16],
            "chars": len(brief),
            "ledger_chars": len(brief),
            "msg_index": 0,
        }],
    }

    rows = collect_acq_provenance(payload, ledger, trajectory)

    for feature in CORE_ACQUISITION_FEATURES:
        assert rows[feature]["status"] == "MEASURED"
        assert any(
            "metrics.localization_proof[0]" in field
            for field in rows[feature]["source_fields"]
        )
