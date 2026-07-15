"""ACQ observability must survive the single-generation brief cache."""

from __future__ import annotations

import json
import os
import sqlite3
from types import SimpleNamespace

import pytest

from groundtruth.runtime import brief_cache


def test_atomic_publication_sets_world_readable_mode_before_replace(tmp_path, monkeypatch):
    """The published handoff must not inherit NamedTemporaryFile's private mode."""
    events = []
    real_chmod = os.chmod
    real_replace = os.replace

    def record_chmod(path, mode):
        events.append(("chmod", mode))
        return real_chmod(path, mode)

    def record_replace(src, dst):
        events.append(("replace", None))
        return real_replace(src, dst)

    monkeypatch.setattr(brief_cache.os, "chmod", record_chmod)
    monkeypatch.setattr(brief_cache.os, "replace", record_replace)

    brief_cache._atomic_write_payload(str(tmp_path / "brief.json"), {"ok": True})

    assert events == [("chmod", 0o644), ("replace", None)]


def test_initial_persist_uses_atomic_publication_before_determinism_verdict(
    tmp_path, monkeypatch,
):
    """A refused repeat proof must still leave its primary handoff readable."""
    events = []
    real_chmod = os.chmod
    real_replace = os.replace

    def record_chmod(path, mode):
        events.append(("chmod", mode))
        return real_chmod(path, mode)

    def record_replace(src, dst):
        events.append(("replace", None))
        return real_replace(src, dst)

    monkeypatch.setattr(brief_cache.os, "chmod", record_chmod)
    monkeypatch.setattr(brief_cache.os, "replace", record_replace)
    first = _deterministic_result("primary diagnostic")

    primary = brief_cache.persist_brief(
        str(tmp_path), first.brief_text, first, identity="request-id"
    )
    verdict = brief_cache.verify_independent_generation(
        str(tmp_path), primary,
        lambda: _deterministic_result("different diagnostic"),
        expect_identity="request-id",
    )

    assert verdict["matched"] is False
    assert events == [
        ("chmod", 0o644), ("replace", None),
        ("chmod", 0o644), ("replace", None),
    ]
    assert brief_cache.load_cached_brief(
        str(tmp_path), expect_identity="request-id"
    )["brief_text"] == "primary diagnostic"


def _deterministic_result(text: str) -> SimpleNamespace:
    return SimpleNamespace(
        brief_text=text,
        effective_w_sem=0.4,
        semantic_signal_count=1,
        rendered_candidate_count=1,
        k_sem_top=1,
        sem_components=[0.7],
        localization_proof=[{
            "rank": 1,
            "path": "pkg/loader.py",
            "components": {"lex": 0.8},
            "acquisition_sources": {},
        }],
        graph_edge_count=1,
        structural_signal_count=0,
        fts5_signal_count=1,
        block_receipts=[],
        tokenizer_used="char4-estimate",
        budget_suppressed=[],
    )


def test_acq_observability_fields_survive_persist_and_load(tmp_path):
    receipts = [
        {
            "block_id": "localization-header",
            "fact_class": "localization",
            "label": "localization-header",
            "char_span": [0, 12],
            "content_hash": "a" * 64,
        }
    ]
    result = SimpleNamespace(
        graph_edge_count=3,
        structural_signal_count=4,
        fts5_signal_count=5,
        block_receipts=receipts,
        tokenizer_used="char4-estimate",
        budget_suppressed=["scope-chain"],
    )

    written = brief_cache.persist_brief(
        str(tmp_path), "sealed brief", result, identity="request-id"
    )
    loaded = brief_cache.load_cached_brief(str(tmp_path), expect_identity="request-id")

    assert loaded is not None
    expected = {
        "graph_edge_count": 3,
        "structural_signal_count": 4,
        "fts5_signal_count": 5,
        "block_receipts": receipts,
        "tokenizer_used": "char4-estimate",
        "budget_suppressed": ["scope-chain"],
    }
    assert {key: written["metrics"][key] for key in expected} == expected
    assert {key: loaded["metrics"][key] for key in expected} == expected
    # Pin that the persisted payload really contains the fields, not merely an
    # in-memory return value added after serialization.
    on_disk = json.loads((tmp_path / brief_cache.BRIEF_CACHE_BASENAME).read_text("utf-8"))
    assert {key: on_disk["metrics"][key] for key in expected} == expected


def test_independent_equal_generation_persists_candidate_local_repeat_identity(tmp_path):
    first = _deterministic_result("same sealed brief")
    primary = brief_cache.persist_brief(
        str(tmp_path), first.brief_text, first, identity="request-id"
    )
    calls = {"count": 0}
    stale_diagnostic = tmp_path / brief_cache.BRIEF_DETERMINISM_DIAGNOSTIC_BASENAME
    stale_diagnostic.write_text('{"stale":true}', encoding="utf-8")

    def generate_again():
        calls["count"] += 1
        return _deterministic_result("same sealed brief")

    verdict = brief_cache.verify_independent_generation(
        str(tmp_path), primary, generate_again, expect_identity="request-id"
    )

    assert verdict["matched"] is True
    assert calls["count"] == 1
    assert len(verdict["canonical_sha256"]) == 2
    assert len(set(verdict["canonical_sha256"])) == 1
    assert not stale_diagnostic.exists()
    loaded = brief_cache.load_cached_brief(str(tmp_path), expect_identity="request-id")
    assert loaded is not None
    assert loaded["brief_text"] == "same sealed brief"
    witness = loaded["metrics"]["localization_proof"][0][
        "acquisition_sources"
    ]["determinism"]
    assert witness == {
        "kind": "repeat_identity",
        "runs": 2,
        "canonical_sha256": verdict["canonical_sha256"],
    }


def test_independent_mismatch_fails_closed_without_persisting_claim(tmp_path):
    first = _deterministic_result("first brief")
    primary = brief_cache.persist_brief(
        str(tmp_path), first.brief_text, first, identity="request-id"
    )

    verdict = brief_cache.verify_independent_generation(
        str(tmp_path), primary,
        lambda: _deterministic_result("different brief"),
        expect_identity="request-id",
    )

    assert verdict["matched"] is False
    loaded = brief_cache.load_cached_brief(str(tmp_path), expect_identity="request-id")
    assert loaded is not None
    proof = loaded["metrics"]["localization_proof"][0]
    assert "determinism" not in proof["acquisition_sources"]
    diagnostic_path = tmp_path / brief_cache.BRIEF_DETERMINISM_DIAGNOSTIC_BASENAME
    diagnostic = json.loads(diagnostic_path.read_text(encoding="utf-8"))
    assert diagnostic["schema"] == "gt.brief_determinism_mismatch.v1"
    assert diagnostic["canonical_sha256"] == verdict["canonical_sha256"]
    assert diagnostic["difference_count"] >= 1
    assert diagnostic["differences"][0]["json_pointer"] == "/brief_text"
    serialized = diagnostic_path.read_text(encoding="utf-8")
    assert "first brief" not in serialized
    assert "different brief" not in serialized


def test_request_identity_changes_when_graph_bytes_change_at_same_size(tmp_path):
    graph = tmp_path / "graph.db"
    graph.write_bytes(b"AAAA")
    first = brief_cache.request_identity("same issue", str(graph))

    graph.write_bytes(b"BBBB")
    second = brief_cache.request_identity("same issue", str(graph))

    assert first != second


def test_request_identity_includes_committed_sqlite_wal_state(tmp_path):
    graph = tmp_path / "graph.db"
    connection = sqlite3.connect(graph)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA wal_autocheckpoint=0")
    connection.execute("CREATE TABLE facts(value TEXT)")
    connection.commit()
    first = brief_cache.request_identity("same issue", str(graph))

    connection.execute("INSERT INTO facts VALUES ('new logical state')")
    connection.commit()
    second = brief_cache.request_identity("same issue", str(graph))
    connection.close()

    assert first != second


def test_identity_handoff_detects_graph_change_before_delivery(tmp_path):
    graph = tmp_path / "graph.db"
    graph.write_bytes(b"AAAA")
    handoff = brief_cache.capture_request_identity("issue", str(graph))
    graph.write_bytes(b"BBBB")

    with pytest.raises(ValueError, match="graph changed"):
        brief_cache.validate_request_identity(handoff)


def test_equal_generation_without_candidates_persists_run_level_witness(tmp_path):
    first = _deterministic_result("new-file task brief")
    first.localization_proof = []
    primary = brief_cache.persist_brief(
        str(tmp_path), first.brief_text, first, identity="request-id"
    )
    second = _deterministic_result("new-file task brief")
    second.localization_proof = []

    verdict = brief_cache.verify_independent_generation(
        str(tmp_path), primary, lambda: second, expect_identity="request-id"
    )

    assert verdict["matched"] is True
    loaded = brief_cache.load_cached_brief(str(tmp_path), expect_identity="request-id")
    assert loaded is not None
    assert loaded["metrics"]["localization_proof"] == []
    assert loaded["metrics"]["determinism_witness"] == {
        "kind": "repeat_identity",
        "runs": 2,
        "canonical_sha256": verdict["canonical_sha256"],
    }
