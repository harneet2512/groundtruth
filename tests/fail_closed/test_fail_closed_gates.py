"""Spec-G fail-closed gate suite for GroundTruth.

Each test proves that a degraded / missing-capability condition is REFUSED under
the strict env gate (GT_REQUIRE_FULL_STACK / GT_REQUIRE_* = 1) rather than being
silently laundered into a pass. The whole point of the strict gate is fail-CLOSED:
a missing embedder, an absent/empty FTS5 index, an installed-but-silent LSP server,
a lexical-only brief, a swallowed preflight crash, or a dataset/preflight start-block
must NOT be charged to GT as a clean run or as a "no-patch model failure."

Every test builds its own tiny synthetic graph.db (stdlib sqlite3 + FTS5) and toggles
GT_REQUIRE_* via a save/restore os.environ fixture. No network, no committed model
(except where the test is specifically about the model loader, which is monkeypatched
to the missing/zero case).

Modules under test (imported by path where they are scripts):
  scripts/verify/preflight_pipeline.py
  scripts/swebench/gt_deep_metrics.py
  scripts/verify/legitimacy.py
  groundtruth.hooks.post_edit / post_view (installed package)
  scripts/swebench/oh_gt_full_wrapper.py (function extracted by regex+exec — has OH deps)
"""

from __future__ import annotations

import importlib.util
import os
import re
import sqlite3
import sys
import types
from pathlib import Path

import pytest

# --------------------------------------------------------------------------- #
# Repo layout + import-by-path helpers
# --------------------------------------------------------------------------- #

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _load_module_by_path(name: str, rel_path: str):
    """Load a script module by file path (the verify/ + swebench/ scripts are not
    importable as a package)."""
    path = _REPO_ROOT / rel_path
    spec = importlib.util.spec_from_file_location(name, str(path))
    assert spec and spec.loader, f"cannot build spec for {path}"
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def pp():
    """The preflight_pipeline module."""
    return _load_module_by_path(
        "gt_preflight_pipeline_under_test", "scripts/verify/preflight_pipeline.py"
    )


@pytest.fixture(scope="module")
def gdm():
    """The gt_deep_metrics module."""
    return _load_module_by_path(
        "gt_deep_metrics_under_test", "scripts/swebench/gt_deep_metrics.py"
    )


# --------------------------------------------------------------------------- #
# Env save/restore — every test toggles GT_REQUIRE_* then must clean up.
# --------------------------------------------------------------------------- #


@pytest.fixture
def clean_env():
    """Snapshot os.environ, yield, then restore it exactly (added keys removed,
    mutated keys reset, deleted keys re-added)."""
    saved = dict(os.environ)
    # Start from a clean slate for all GT strict flags so prior process state never
    # leaks into a test that asserts a specific gate is OFF.
    for k in list(os.environ):
        if k.startswith(("GT_REQUIRE_", "GT_FORCE_", "GT_FORBID_", "GT_LOCAL_DATASET")):
            del os.environ[k]
    try:
        yield os.environ
    finally:
        os.environ.clear()
        os.environ.update(saved)


# --------------------------------------------------------------------------- #
# Synthetic graph.db builders (stdlib only).
# --------------------------------------------------------------------------- #


def _base_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE nodes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            label TEXT NOT NULL,
            name TEXT NOT NULL,
            qualified_name TEXT,
            file_path TEXT NOT NULL,
            start_line INTEGER,
            end_line INTEGER,
            signature TEXT,
            return_type TEXT,
            is_exported BOOLEAN DEFAULT 0,
            is_test BOOLEAN DEFAULT 0,
            language TEXT NOT NULL,
            parent_id INTEGER
        );
        CREATE TABLE edges (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_id INTEGER NOT NULL,
            target_id INTEGER NOT NULL,
            type TEXT NOT NULL,
            source_line INTEGER,
            source_file TEXT,
            resolution_method TEXT,
            confidence REAL DEFAULT 0.0,
            metadata TEXT
        );
        CREATE TABLE project_meta (key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE properties (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            node_id INTEGER,
            kind TEXT,
            value TEXT
        );
        CREATE TABLE assertions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            target_node_id INTEGER,
            expr TEXT
        );
        """
    )
    conn.execute(
        "INSERT INTO project_meta(key, value) VALUES ('schema_version', '15')"
    )


def _add_node(
    conn: sqlite3.Connection,
    *,
    name: str,
    file_path: str,
    language: str = "python",
    label: str = "Function",
    return_type: str = "",
    signature: str = "",
    is_test: int = 0,
) -> int:
    cur = conn.execute(
        "INSERT INTO nodes(label, name, qualified_name, file_path, start_line, "
        "end_line, signature, return_type, language, is_test) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (label, name, name, file_path, 1, 10, signature, return_type, language, is_test),
    )
    return int(cur.lastrowid)


def _add_edge(
    conn: sqlite3.Connection,
    *,
    src: int,
    tgt: int,
    source_file: str,
    method: str = "import",
    confidence: float = 1.0,
    etype: str = "CALLS",
) -> None:
    conn.execute(
        "INSERT INTO edges(source_id, target_id, type, source_line, source_file, "
        "resolution_method, confidence) VALUES (?,?,?,?,?,?,?)",
        (src, tgt, etype, 1, source_file, method, confidence),
    )


def _make_fts5(conn: sqlite3.Connection, *, populate: bool) -> None:
    """Create nodes_fts (external-content table over nodes) and populate it from the
    non-test nodes (matching the production graph_localizer SQL)."""
    conn.execute(
        "CREATE VIRTUAL TABLE IF NOT EXISTS nodes_fts USING fts5("
        "name, qualified_name, signature, file_path, "
        "content='nodes', content_rowid='id')"
    )
    if populate:
        conn.execute(
            "INSERT INTO nodes_fts(rowid, name, qualified_name, signature, file_path) "
            "SELECT id, name, COALESCE(qualified_name,''), COALESCE(signature,''), "
            "file_path FROM nodes WHERE is_test = 0"
        )


def _make_empty_fts5(conn: sqlite3.Connection) -> None:
    """Create a STANDALONE nodes_fts (no external content) with 0 rows — the realistic
    'Go built the FTS5 virtual table but the population query wrote nothing' failure
    mode. COUNT(*) on this returns 0 (an external-content table would instead report
    the content row count), which is exactly the empty-index case check_fts5 guards."""
    conn.execute(
        "CREATE VIRTUAL TABLE IF NOT EXISTS nodes_fts USING fts5("
        "name, qualified_name, signature, file_path)"
    )


def _new_db(tmp_path: Path, name: str = "graph.db") -> str:
    return str(tmp_path / name)


# =========================================================================== #
# G1 — missing ONNX model fails under require_embedder
# =========================================================================== #


def test_missing_onnx_model_fails_under_require_embedder(pp, clean_env, tmp_path, monkeypatch):
    """With GT_REQUIRE_EMBEDDER=1 and the models root pointed at an empty dir,
    the embedder cannot load model.onnx -> check_semantic_embedder pass=False."""
    os.environ["GT_REQUIRE_EMBEDDER"] = "1"

    empty_models = tmp_path / "empty_models_root"
    empty_models.mkdir()

    import groundtruth.memory.enrich.embed as embed_mod

    # _MODELS_ROOT is read at import time; the model_dir property reads the module
    # global dynamically, so monkeypatching it redirects the loader to an empty dir.
    monkeypatch.setattr(embed_mod, "_MODELS_ROOT", empty_models, raising=True)
    # Clear the loader cache so we don't reuse a model from a prior test.
    monkeypatch.setattr(embed_mod, "_models", {}, raising=True)

    ok, detail = pp.check_semantic_embedder(str(tmp_path))
    assert ok is False, f"expected fail-closed under GT_REQUIRE_EMBEDDER, got pass: {detail}"
    assert "did NOT load" in detail or "FileNotFoundError" in detail, detail


# =========================================================================== #
# G2 — zero / empty embedding can never pass under require
# =========================================================================== #


def test_zero_embedding_fallback_impossible_under_require(pp, clean_env, tmp_path, monkeypatch):
    """Under GT_REQUIRE_EMBEDDER=1, a ZERO vector (the silent W_SEM=0 fallback) is
    caught as pass=False. We monkeypatch the loader to RETURN a real model object
    but embed_query to yield an all-zero vector — exercising the norm guard."""
    os.environ["GT_REQUIRE_EMBEDDER"] = "1"

    import groundtruth.memory.enrich.embed as embed_mod

    class _FakeModel:
        dim = 384
        model_dir = str(tmp_path / "fake_onnx")

    monkeypatch.setattr(embed_mod, "get_embedding_model", lambda *a, **k: _FakeModel())
    monkeypatch.setattr(embed_mod, "embed_query", lambda *a, **k: [0.0] * 384)

    ok, detail = pp.check_semantic_embedder(str(tmp_path))
    assert ok is False, f"a ZERO embedding must NOT pass under require: {detail}"
    assert "ZERO vector" in detail, detail

    # And the missing-model case is also pass=False (the canonical fail-closed path).
    empty_models = tmp_path / "empty2"
    empty_models.mkdir()
    monkeypatch.setattr(embed_mod, "_MODELS_ROOT", empty_models, raising=True)
    monkeypatch.setattr(embed_mod, "_models", {}, raising=True)
    # Restore the real loader so the missing-model FileNotFoundError actually fires.
    monkeypatch.undo()
    os.environ["GT_REQUIRE_EMBEDDER"] = "1"
    monkeypatch.setattr(embed_mod, "_MODELS_ROOT", empty_models, raising=True)
    monkeypatch.setattr(embed_mod, "_models", {}, raising=True)
    ok2, detail2 = pp.check_semantic_embedder(str(tmp_path))
    assert ok2 is False, f"missing model must be pass=False under require: {detail2}"


# =========================================================================== #
# G3 — missing FTS5 table fails under strict
# =========================================================================== #


def test_missing_fts5_table_fails(pp, clean_env, tmp_path):
    """graph.db with NO nodes_fts table -> under strict, check_fts5 refuses the
    runtime Python rebuild (a real run must have the Go-built FTS5 index)."""
    os.environ["GT_REQUIRE_FTS5"] = "1"
    os.environ["GT_REQUIRE_FULL_STACK"] = "1"

    db = _new_db(tmp_path)
    conn = sqlite3.connect(db)
    _base_schema(conn)
    _add_node(conn, name="handle_request", file_path="src/server.py")
    conn.commit()
    conn.close()

    ok, detail = pp.check_fts5(db)
    assert ok is False, f"absent nodes_fts must fail under strict: {detail}"
    assert "ABSENT" in detail or "Refusing" in detail, detail


# =========================================================================== #
# G4 — empty FTS5 table fails (present but 0 rows)
# =========================================================================== #


def test_empty_fts5_table_fails(pp, clean_env, tmp_path):
    """graph.db WITH a nodes_fts that has 0 rows -> check_fts5 pass=False
    ('present but EMPTY' — Go FTS5 population failed). Fails even WITHOUT a strict
    flag, because an empty FTS index is a hard population failure."""
    db = _new_db(tmp_path)
    conn = sqlite3.connect(db)
    _base_schema(conn)
    _add_node(conn, name="handle_request", file_path="src/server.py")
    # Standalone FTS5 table with 0 rows (population query wrote nothing).
    _make_empty_fts5(conn)
    conn.commit()
    # Sanity: the table exists and reports 0 rows.
    assert conn.execute("SELECT COUNT(*) FROM nodes_fts").fetchone()[0] == 0
    conn.close()

    ok, detail = pp.check_fts5(db)
    assert ok is False, f"empty nodes_fts must fail: {detail}"
    assert "EMPTY" in detail, detail


# =========================================================================== #
# G5 — installed LSP server with zero enrichment fails; documented-reason passes
# =========================================================================== #


def test_lsp_installed_zero_enrichment_fails_or_reason(pp, clean_env, tmp_path, monkeypatch):
    """lang=go, 0 lsp edges. If gopls is installed AND there is 0 return_type
    enrichment -> RAN BUT WROTE NOTHING -> pass=False. If return_type>0 (the pass
    ran and wrote SOMETHING) -> pass=True with the documented reason."""
    os.environ["GT_REQUIRE_LSP"] = "1"

    # --- variant A: 0 lsp edges AND 0 enrichment, gopls "installed" -> FAIL ---
    db_a = _new_db(tmp_path, "a.db")
    conn = sqlite3.connect(db_a)
    _base_schema(conn)
    _add_node(conn, name="ServeHTTP", file_path="server.go", language="go", return_type="")
    _add_node(conn, name="Handle", file_path="router.go", language="go", return_type="")
    conn.commit()
    conn.close()

    import shutil as _shutil

    monkeypatch.setattr(
        _shutil, "which",
        lambda exe: "/usr/bin/gopls" if exe == "gopls" else None,
    )
    ok_a, detail_a = pp.check_lsp_edges(db_a)
    assert ok_a is False, f"installed gopls + 0 edges + 0 enrichment must fail: {detail_a}"
    assert "WROTE NOTHING" in detail_a, detail_a

    # --- variant B: 0 lsp edges but return_type enrichment present -> PASS ---
    db_b = _new_db(tmp_path, "b.db")
    conn = sqlite3.connect(db_b)
    _base_schema(conn)
    _add_node(conn, name="ServeHTTP", file_path="server.go", language="go", return_type="error")
    _add_node(conn, name="Handle", file_path="router.go", language="go", return_type="int")
    conn.commit()
    conn.close()

    ok_b, detail_b = pp.check_lsp_edges(db_b)
    assert ok_b is True, f"return_type enrichment should document the no-edge reason: {detail_b}"
    # Align with the current check_lsp_edges wording (preflight_pipeline.py:358-360): a
    # 0-call-edge result on a CALLS-dominant lang is accepted when return_type is populated
    # AND the warm-probe gate is the LSP-ran proof. Intent unchanged: enrichment present +
    # documented no-edge reason.
    assert "return_type" in detail_b and "call-edge corrections" in detail_b, detail_b


# =========================================================================== #
# G6 — L1 graph-edge-count zero fails under full stack (lexical-only brief)
# =========================================================================== #


def test_l1_graph_edge_count_zero_fails_full_stack(pp, clean_env, tmp_path, monkeypatch):
    """A brief whose candidate files participate in NO graph edge is lexical-only.
    Under FULL_STACK, check_l1_graph_backed must HARD-fail. We monkeypatch
    _brief_candidate_files to return a file absent from the edges table while the
    graph DOES carry edges for other files (so the gate is about the candidate, not
    an empty graph)."""
    os.environ["GT_REQUIRE_FULL_STACK"] = "1"

    db = _new_db(tmp_path)
    conn = sqlite3.connect(db)
    _base_schema(conn)
    a = _add_node(conn, name="real_caller", file_path="src/real.py")
    b = _add_node(conn, name="real_callee", file_path="src/other.py")
    _add_edge(conn, src=a, tgt=b, source_file="src/real.py", method="import", confidence=1.0)
    conn.commit()
    conn.close()

    # The brief returns a candidate the graph has NO edge for.
    monkeypatch.setattr(pp, "_brief_candidate_files", lambda d, r: ["nonexistent/file.py"])

    ok, detail = pp.check_l1_graph_backed(db, str(tmp_path))
    assert ok is False, f"lexical-only candidate must fail under full stack: {detail}"
    assert "LEXICAL-ONLY" in detail, detail

    # Control: a candidate that IS graph-backed passes (proves the gate is not a
    # blanket reject).
    monkeypatch.setattr(pp, "_brief_candidate_files", lambda d, r: ["src/real.py"])
    ok2, detail2 = pp.check_l1_graph_backed(db, str(tmp_path))
    assert ok2 is True, f"graph-backed candidate should pass: {detail2}"


# =========================================================================== #
# G7 — L3 metadata-only evidence is accounted as degraded (real=0, metadata>0)
# =========================================================================== #


def test_l3_metadata_only_is_degraded(clean_env):
    """The honest hollow-signal accounting: a metadata-only L3 block (section
    headers / body_len placeholders with no real content) must yield
    real_evidence_count=0 and metadata_only_count>0. A block WITH real content
    (a populated [SIGNATURE] line) must be counted as real."""
    from groundtruth.hooks import post_edit

    metadata_only_blocks = [
        "[BEHAVIORAL CONTRACT]",          # bare header, no payload -> metadata
        "body_len=80",                    # placeholder stub -> metadata
        "[GT L3: set_fields]",            # header-only marker -> metadata
    ]
    real, meta = post_edit._l3_account_evidence(metadata_only_blocks)
    assert real == 0, f"metadata-only blocks must have real=0, got {real}"
    assert meta > 0, f"metadata-only blocks must have metadata_only>0, got {meta}"

    # Positive control: a real signature line is counted as real evidence.
    real2, meta2 = post_edit._l3_account_evidence(
        ["[SIGNATURE] def set_fields(self, values) -> None"]
    )
    assert real2 >= 1, f"a populated [SIGNATURE] must count as real, got {real2}"


# =========================================================================== #
# G8 — L3b over-cap trims within the 600-token cap and flags cap_enforced
# =========================================================================== #


def test_l3b_over_cap_trims_within_cap(clean_env):
    """Feed an over-budget line set to the L3b cap enforcer -> the returned token
    count is <= cap (600) and cap_enforced is True."""
    from groundtruth.hooks import post_view

    cap = post_view._L3B_MAX_TOKENS
    assert cap == 600, f"expected the documented 600-token L3b cap, got {cap}"

    # Build a clearly over-budget block of LOW-priority lines (caller edges) so the
    # enforcer can trim well under cap (it never drops the single top contract line).
    lines = [f"Called by: module_{i}.caller_{i}  -> some_target_{i}()" for i in range(400)]
    pre_tokens = post_view._estimate_l3b_tokens(lines)
    assert pre_tokens > cap, f"test setup must exceed the cap, pre={pre_tokens}"

    kept, final_tokens, cap_enforced = post_view._enforce_l3b_cap(lines, cap)
    assert cap_enforced is True, "dropping lines must set cap_enforced=True"
    assert final_tokens <= cap, f"final tokens {final_tokens} must be <= cap {cap}"
    assert len(kept) < len(lines), "some lines must have been dropped"


# =========================================================================== #
# G9 — runtime load_dataset is NOT called when GT_LOCAL_DATASET + HF offline
# =========================================================================== #


def _extract_load_dataset_fn():
    """Extract _load_dataset_offline_or_hf from the OH wrapper WITHOUT importing the
    whole wrapper (it pulls heavy OpenHands deps). Read the source, regex the function
    body up to the next top-level def, and exec it in a controlled namespace."""
    src = (_REPO_ROOT / "scripts/swebench/oh_gt_full_wrapper.py").read_text(encoding="utf-8")
    m = re.search(
        r"^def _load_dataset_offline_or_hf\(.*?\n(?=^def |\Z)",
        src,
        flags=re.DOTALL | re.MULTILINE,
    )
    assert m, "could not locate _load_dataset_offline_or_hf in the wrapper source"
    ns: dict = {"os": os}
    exec(compile(m.group(0), "<wrapper_extract>", "exec"), ns)
    return ns["_load_dataset_offline_or_hf"]


def test_runtime_load_dataset_not_called_in_matrix(clean_env, tmp_path, monkeypatch):
    """With GT_LOCAL_DATASET -> a tiny JSONL and HF_DATASETS_OFFLINE=1, the loader
    returns rows from the local file and NEVER touches HuggingFace. We prove the no-HF
    contract by making `import datasets` raise: the offline-local path must not need it."""
    fn = _extract_load_dataset_fn()

    # Sabotage 'datasets' so any attempt to load_dataset() blows up -> proves the
    # local path is self-sufficient.
    broken = types.ModuleType("datasets")

    def _boom(*a, **k):  # pragma: no cover - must never be called on the local path
        raise AssertionError("datasets.load_dataset was called on the offline-local path")

    broken.load_dataset = _boom  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "datasets", broken)

    local = tmp_path / "split.jsonl"
    local.write_text(
        '{"instance_id": "repo__task-1", "problem_statement": "fix the bug"}\n'
        '{"instance_id": "repo__task-2", "problem_statement": "another"}\n',
        encoding="utf-8",
    )
    os.environ["GT_LOCAL_DATASET"] = str(local)
    os.environ["HF_DATASETS_OFFLINE"] = "1"

    df = fn("princeton-nlp/SWE-bench", "test")
    rows = df.to_dict("records") if hasattr(df, "to_dict") else list(df)
    assert len(rows) == 2, f"expected 2 local rows, got {len(rows)}"
    ids = {r["instance_id"] for r in rows}
    assert ids == {"repo__task-1", "repo__task-2"}, ids

    # And the fail-closed branch: offline enforced but NO local artifact -> raise
    # dataset_missing_agent_not_started (never a silent network fallback).
    del os.environ["GT_LOCAL_DATASET"]
    with pytest.raises(RuntimeError, match="dataset_missing_agent_not_started"):
        fn("princeton-nlp/SWE-bench", "test")


# =========================================================================== #
# G10 — a swallowed/crashing preflight check is a FAILURE, not a pass
# =========================================================================== #


def test_swallowed_preflight_exception_is_failure(pp, clean_env, tmp_path, monkeypatch):
    """run_db_dimension_gate wraps each check; a crashing check must yield ok=False
    with a '<name> crashed: ...' row, and ok_all must be False — never silently pass."""
    db = _new_db(tmp_path)
    conn = sqlite3.connect(db)
    _base_schema(conn)
    a = _add_node(conn, name="f", file_path="src/a.py")
    b = _add_node(conn, name="g", file_path="src/b.py")
    _add_edge(conn, src=a, tgt=b, source_file="src/a.py", method="import", confidence=1.0)
    _make_fts5(conn, populate=True)
    conn.execute("INSERT INTO properties(node_id, kind, value) VALUES (?, 'data_flow', 'x')", (a,))
    conn.commit()
    conn.close()

    def _boom(_db):
        raise RuntimeError("synthetic edge_quality explosion")

    monkeypatch.setattr(pp, "check_edge_quality", _boom)

    ok_all, results = pp.run_db_dimension_gate(db)
    assert ok_all is False, "a crashing check must fail the gate"
    row = dict((name, (ok, msg)) for name, ok, msg in results).get("edge_quality")
    assert row is not None, "edge_quality must appear in the results"
    ok, msg = row
    assert ok is False, f"crashing check row must be ok=False: {msg}"
    assert "crashed" in msg and "synthetic edge_quality explosion" in msg, msg


# =========================================================================== #
# G11 — preflight/dataset start-block is NOT charged as a no-patch agent run
# =========================================================================== #


def test_preflight_failure_not_counted_as_no_patch(gdm, clean_env):
    """classify_outcome must attribute a preflight failure (agent never started) to
    'preflight_failed_agent_not_started' with agent_started=False — NOT
    'unresolved_no_patch_agent_ran'. A dataset_missing log -> dataset_missing."""
    # No agent actions in the trajectory; the log carries the preflight failure token.
    traj = {"action_count": 0, "has_patch": False, "resolved": None}

    preflight_log = (
        "OH_GT_FULL preflight starting\n"
        "  [FAIL] fts5: nodes_fts ABSENT\n"
        "  [FAIL] data_flow: 0 data_flow rows\n"
        "PREFLIGHT: 2 FAILURES: fts5, data_flow\n"
    )
    # classify_outcome reads the log via _safe_read_text(log_path); pass the text by
    # monkeypatching that reader so we don't need a temp file.
    import builtins  # noqa: F401  (kept explicit; not used but documents intent)

    orig = gdm._safe_read_text
    gdm._safe_read_text = lambda path, *a, **k: preflight_log  # type: ignore[assignment]
    try:
        verdict = gdm.classify_outcome("repo__task-1", "/tmp/full_run.log", traj, {}, "swe-live-openhands")
    finally:
        gdm._safe_read_text = orig  # type: ignore[assignment]

    assert verdict["outcome"] == "preflight_failed_agent_not_started", verdict
    assert verdict["agent_started"] is False, verdict
    assert verdict["failure_stage"] == "preflight", verdict
    assert verdict["outcome"] != "unresolved_no_patch_agent_ran"

    # Dataset-missing variant.
    dataset_log = (
        "OH_GT_FULL_ARGS starting matrix job\n"
        "RuntimeError: dataset_missing_agent_not_started: HF offline is enforced "
        "but GT_LOCAL_DATASET is unset\n"
    )
    gdm._safe_read_text = lambda path, *a, **k: dataset_log  # type: ignore[assignment]
    try:
        verdict2 = gdm.classify_outcome("repo__task-2", "/tmp/full_run.log", traj, {}, "swe-live-openhands")
    finally:
        gdm._safe_read_text = orig  # type: ignore[assignment]

    assert verdict2["outcome"] == "dataset_missing_agent_not_started", verdict2
    assert verdict2["agent_started"] is False, verdict2
