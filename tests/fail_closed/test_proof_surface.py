"""Fail-closed proof-mode surface tests (plan section 5).

Each test encodes a partial-operation failure mode the plan flags and asserts the
TWO-SIDED contract:
  * GT_PROOF_MODE=1 + degraded input  -> raises GTProofModeError (fail closed)
  * not proof mode (or no GT_REQUIRE_EMBEDDER) -> NO-OP, byte-identical behaviour

The "would have passed silently before" is exactly the bug; the proof-mode raise is
the fix. Outside proof mode every guard must be inert so dev/CI is unchanged.
"""
from __future__ import annotations

import os
import sqlite3

import pytest

from groundtruth.runtime import proof
from groundtruth.runtime.proof import GTProofModeError


# ───────────────────────────── fixtures ──────────────────────────────────────


@pytest.fixture(autouse=True)
def _clean_gt_env(monkeypatch):
    """Each test starts from a known env: no GT_* proof flags / host aliases set."""
    for k in list(os.environ):
        if k.startswith("GT_"):
            monkeypatch.delenv(k, raising=False)
    yield


_FTS_DDL = (
    "CREATE VIRTUAL TABLE nodes_fts USING fts5("
    "name, qualified_name, signature, file_path, content='nodes', content_rowid='id')"
)


@pytest.fixture
def graph_db(tmp_path):
    """A minimal Go-schema graph.db with nodes + a populated FTS5 table."""
    db = str(tmp_path / "graph.db")
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE nodes (id INTEGER PRIMARY KEY, name TEXT, qualified_name TEXT, "
        "signature TEXT, file_path TEXT, is_test INTEGER DEFAULT 0)"
    )
    conn.executemany(
        "INSERT INTO nodes(id, name, qualified_name, signature, file_path, is_test) VALUES(?,?,?,?,?,?)",
        [(1, "set_fields", "ImportTask.set_fields", "def set_fields(self)", "beets/importer.py", 0),
         (2, "write", "Library.write", "def write(self)", "beets/library.py", 0)],
    )
    try:
        conn.execute(_FTS_DDL)
        conn.execute(
            "INSERT INTO nodes_fts(rowid, name, qualified_name, signature, file_path) "
            "SELECT id, name, COALESCE(qualified_name,''), COALESCE(signature,''), file_path FROM nodes"
        )
    except sqlite3.OperationalError:
        pytest.skip("SQLite build lacks FTS5 — cannot exercise the native-FTS5 path")
    conn.commit()
    conn.close()
    return db


@pytest.fixture
def graph_db_no_fts(tmp_path):
    """A graph.db WITHOUT nodes_fts (Go indexer compiled without -tags sqlite_fts5)."""
    db = str(tmp_path / "nofts.db")
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE nodes (id INTEGER PRIMARY KEY, name TEXT, file_path TEXT, is_test INTEGER)")
    conn.execute("INSERT INTO nodes VALUES (1, 'x', 'a.py', 0)")
    conn.commit()
    conn.close()
    return db


# ───────────────────────────── require() core ────────────────────────────────


def test_require_raises_in_proof(monkeypatch):
    monkeypatch.setenv("GT_PROOF_MODE", "1")
    with pytest.raises(GTProofModeError):
        proof.require(False, "x", "boom")
    assert proof.require(True, "x") is True


def test_require_noop_outside_proof():
    # No proof mode -> never raises; returns the (falsy) ok unchanged.
    assert proof.require(False, "x", "boom") is False
    assert proof.require(True, "x") is True


# ───────────────────────────── host aliases (A1) ─────────────────────────────


def test_canonical_host_handoff_allowed_in_proof(monkeypatch):
    # GT_HOST_GRAPH_DB / GT_HOST_SRC_ROOT are the LEGITIMATE host->agent graph handoff
    # (gt_gt §1) — the agent hooks onto the same LSP-enriched graph the gates measured.
    # They must NOT raise in proof mode.
    monkeypatch.setenv("GT_PROOF_MODE", "1")
    monkeypatch.setenv("GT_HOST_GRAPH_DB", "/host/graph.db")
    monkeypatch.setenv("GT_HOST_SRC_ROOT", "/host/src")
    proof.reject_host_aliases()  # canonical handoff allowed


def test_reject_noncanonical_host_alias_proof(monkeypatch):
    # A NON-canonical alias the pipeline never sets == a misconfiguration -> fail-closed.
    monkeypatch.setenv("GT_PROOF_MODE", "1")
    monkeypatch.setenv("GT_HOST_GRAPH", "/host/typo.db")
    with pytest.raises(GTProofModeError):
        proof.reject_host_aliases()


def test_noncanonical_host_alias_allowed_outside_proof(monkeypatch):
    monkeypatch.setenv("GT_HOST_GRAPH", "/host/typo.db")
    proof.reject_host_aliases()  # no-op outside proof mode


def test_context_from_env_rejects_noncanonical_host_alias_in_proof(monkeypatch):
    from groundtruth.runtime.context import GTRuntimeContext
    monkeypatch.setenv("GT_PROOF_MODE", "1")
    monkeypatch.setenv("GT_HOST_GRAPH", "/host/typo.db")
    with pytest.raises(GTProofModeError):
        GTRuntimeContext.from_env()


def test_context_from_env_uses_host_alias_outside_proof(monkeypatch):
    from groundtruth.runtime.context import GTRuntimeContext
    monkeypatch.setenv("GT_HOST_GRAPH_DB", "/host/graph.db")
    ctx = GTRuntimeContext.from_env()
    assert ctx.graph_db == "/host/graph.db"  # fallback still works for dev


# ───────────────────────────── FTS5 native (Stage 2) ─────────────────────────


def test_fts5_native_passes_when_present(graph_db):
    conn = sqlite3.connect(graph_db)
    try:
        assert proof.assert_fts5_native(conn) is True
    finally:
        conn.close()


def test_fts5_missing_raises_in_proof(monkeypatch, graph_db_no_fts):
    monkeypatch.setenv("GT_PROOF_MODE", "1")
    conn = sqlite3.connect(graph_db_no_fts)
    try:
        with pytest.raises(GTProofModeError):
            proof.assert_fts5_native(conn)
    finally:
        conn.close()


def test_fts5_missing_noop_outside_proof(graph_db_no_fts):
    conn = sqlite3.connect(graph_db_no_fts)
    try:
        assert proof.assert_fts5_native(conn) is False  # warns, no raise
    finally:
        conn.close()


# ───────────────────────────── LSP/closure timing (Stage 2) ──────────────────


def test_closure_after_lsp_passes_when_fresh(monkeypatch, graph_db):
    monkeypatch.setenv("GT_PROOF_MODE", "1")
    proof.stamp_lsp(graph_db, metrics="verified=3")
    proof.stamp_closure(graph_db)
    assert proof.assert_closure_after_lsp(graph_db) is True


def test_closure_missing_raises_in_proof(monkeypatch, graph_db):
    monkeypatch.setenv("GT_PROOF_MODE", "1")
    proof.stamp_lsp(graph_db)  # LSP stamped, closure NOT
    with pytest.raises(GTProofModeError):
        proof.assert_closure_after_lsp(graph_db)


def test_stale_closure_raises_in_proof(monkeypatch, graph_db):
    monkeypatch.setenv("GT_PROOF_MODE", "1")
    # Closure stamped BEFORE lsp -> stale.
    proof.stamp_meta(graph_db, proof.K_CLOSURE_TS, repr(100.0))
    proof.stamp_meta(graph_db, proof.K_LSP_TS, repr(200.0))
    with pytest.raises(GTProofModeError):
        proof.assert_closure_after_lsp(graph_db)


def test_timing_noop_outside_proof(graph_db):
    # No stamps at all, but not proof mode -> inert.
    assert proof.assert_closure_after_lsp(graph_db) is True
    assert proof.assert_lsp_before_scoring(graph_db) is True


def test_meta_roundtrip(graph_db):
    proof.stamp_meta(graph_db, "k", "v")
    assert proof.read_meta(graph_db, "k") == "v"
    assert proof.read_meta(graph_db, "absent") is None


# ───────────────────────────── embedder usage (Stage 3) ──────────────────────


def test_semantic_consumed_passes_with_signal(monkeypatch):
    monkeypatch.setenv("GT_PROOF_MODE", "1")
    monkeypatch.setenv("GT_REQUIRE_EMBEDDER", "1")
    assert proof.assert_semantic_consumed(0.15, [0.0, 0.42, 0.0], 3) is True


def test_semantic_all_zero_raises_in_proof(monkeypatch):
    monkeypatch.setenv("GT_PROOF_MODE", "1")
    monkeypatch.setenv("GT_REQUIRE_EMBEDDER", "1")
    with pytest.raises(GTProofModeError):
        proof.assert_semantic_consumed(0.15, [0.0, 0.0, 0.0], 3)


def test_semantic_zero_weight_raises_in_proof(monkeypatch):
    monkeypatch.setenv("GT_PROOF_MODE", "1")
    monkeypatch.setenv("GT_REQUIRE_EMBEDDER", "1")
    with pytest.raises(GTProofModeError):
        proof.assert_semantic_consumed(0.0, [0.3, 0.4], 2)


def test_semantic_noop_without_require_embedder(monkeypatch):
    monkeypatch.setenv("GT_PROOF_MODE", "1")  # proof but embedder not required
    assert proof.assert_semantic_consumed(0.0, [0.0, 0.0], 5) is True


def test_semantic_noop_zero_candidates(monkeypatch):
    monkeypatch.setenv("GT_PROOF_MODE", "1")
    monkeypatch.setenv("GT_REQUIRE_EMBEDDER", "1")
    assert proof.assert_semantic_consumed(0.0, [], 0) is True


@pytest.mark.parametrize("ablation,rrf,wsem", [
    ("A", "", 0.15),       # no-sem ablation
    ("B0", "", 0.15),
    ("C", "det", 0.15),    # RRF drops sem
    ("C", "nosem", 0.15),
    ("C", "", 0.0),        # W_SEM zeroed
])
def test_forbid_no_sem_config_raises_in_proof(monkeypatch, ablation, rrf, wsem):
    monkeypatch.setenv("GT_PROOF_MODE", "1")
    monkeypatch.setenv("GT_REQUIRE_EMBEDDER", "1")
    with pytest.raises(GTProofModeError):
        proof.forbid_no_sem_config(ablation, rrf, wsem)


def test_forbid_no_sem_config_passes_live_path(monkeypatch):
    monkeypatch.setenv("GT_PROOF_MODE", "1")
    monkeypatch.setenv("GT_REQUIRE_EMBEDDER", "1")
    proof.forbid_no_sem_config("C", "", 0.15)  # live config: no raise


def test_forbid_no_sem_config_noop_outside_proof():
    proof.forbid_no_sem_config("A", "det", 0.0)  # not proof: inert


# ───────────────────────────── context id ────────────────────────────────────


def test_context_id_stable_and_path_sensitive(monkeypatch):
    monkeypatch.setenv("GT_SOURCE_ROOT", "/opt/gt/src/repo")
    monkeypatch.setenv("GT_GRAPH_DB", "/opt/gt/graph.db")
    a = proof.context_id()
    assert a == proof.context_id()  # stable
    monkeypatch.setenv("GT_GRAPH_DB", "/host/other.db")
    assert proof.context_id() != a  # changes with the runtime-defining paths
    assert len(a) == 16
