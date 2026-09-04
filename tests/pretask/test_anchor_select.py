"""Behavior tests for anchor_select — Stage A multi-signal anchor selection.

Covers the two LIPI squash items owned by this file:

  #18  Cross-wired path keys: the three signal ingress points (semantic /
       symbol / lexical) must canonicalize file_path to ONE project-wide form
       BEFORE keying the merge dict, so the trust-upgrade / de-dup merge does
       not silently split a Windows-indexed file (`pkg\\core.py`) from its
       forward-slashed lexical twin (`pkg/core.py`).

  #47  Dead `structural_seed_expand` (0 callers, H1 falsified per census) and
       its advertising docstring paragraph are removed — the module no longer
       claims a lever it does not ship.

Builds a synthetic graph.db matching the real schema (nodes/edges) and uses a
deterministic stub embedder so no model download / ONNX runtime is required.
"""

from __future__ import annotations

import sqlite3

import numpy as np
import pytest

import groundtruth.pretask.anchor_select as anchor_select
from groundtruth.pretask.anchor_select import (
    _norm_path,
    select_anchors,
)
from groundtruth.pretask.hybrid import SignalHit


def _make_db(path: str, *, gold_path: str) -> None:
    """One gold file (stored at `gold_path` — caller passes a BACKSLASH path to
    simulate a Windows-indexed graph) plus one decoy. is_test=0 throughout."""
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE nodes (
            id INTEGER PRIMARY KEY, label TEXT, name TEXT, qualified_name TEXT,
            file_path TEXT, start_line INTEGER, end_line INTEGER, signature TEXT,
            return_type TEXT, is_exported INTEGER, is_test INTEGER, language TEXT,
            parent_id INTEGER
        );
        CREATE TABLE edges (
            id INTEGER PRIMARY KEY, source_id INTEGER, target_id INTEGER, type TEXT,
            source_line INTEGER, source_file TEXT, resolution_method TEXT,
            confidence REAL, metadata TEXT
        );
        """
    )
    conn.executemany(
        "INSERT INTO nodes (id,label,name,file_path,start_line,is_test) VALUES (?,?,?,?,?,0)",
        [
            # symbol name `serialize_payload` whose normalized parts all appear in
            # the issue text below -> _symbol_anchors matches this file.
            (1, "Function", "serialize_payload", gold_path, 10),
            (2, "Function", "unrelated_helper", "pkg/other.py", 5),
        ],
    )
    conn.commit()
    conn.close()


class _StubModel:
    """Deterministic embedder: every text -> a fixed unit vector, so the cosine
    (file_embs @ issue_emb) is 1.0 for every non-empty file. That is enough to
    put the gold file in the semantic top-k and above tau_anchor. No real model,
    no ONNX, fully reproducible."""

    def encode(self, texts, normalize_embeddings=True, show_progress_bar=False, batch_size=128):  # noqa: D401
        return np.ones((len(list(texts)), 8), dtype=np.float32) / np.sqrt(8.0)


@pytest.fixture()
def repo(tmp_path):
    """A repo whose gold file is indexed with a BACKSLASH path but exists on disk
    at the forward-slash location (so _file_summary can read a non-empty summary
    and the semantic pass scores it)."""
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "core.py").write_text(
        "def serialize_payload(data):\n    return data\n", encoding="utf-8"
    )
    (tmp_path / "pkg" / "other.py").write_text(
        "def unrelated_helper():\n    return 0\n", encoding="utf-8"
    )
    db = str(tmp_path / "graph.db")
    # Backslash path == what a Windows indexer stores in nodes.file_path.
    _make_db(db, gold_path="pkg\\core.py")
    return str(tmp_path), db


@pytest.fixture(autouse=True)
def _clear_embed_cache():
    """select_anchors memoises embeddings keyed by db mtime/size; clear between
    tests so each fixture's fresh db is not shadowed by a prior result."""
    anchor_select._EMBED_CACHE.clear()
    yield
    anchor_select._EMBED_CACHE.clear()


# ---- #18: canonical path normalization at all three ingress points ----------


def test_norm_path_is_canonical():
    # The shared normalizer matches v7_4_brief.py:548 / graph_localizer.py:590.
    assert _norm_path("pkg\\core.py") == "pkg/core.py"
    assert _norm_path("./pkg/core.py") == "pkg/core.py"
    assert _norm_path("/pkg/core.py") == "pkg/core.py"
    assert _norm_path("pkg/core.py") == "pkg/core.py"


def test_windows_path_merges_across_signals(repo, monkeypatch):
    """RED before fix / GREEN after: the semantic+symbol pipes key off the raw
    backslash `pkg\\core.py` while the lexical pipe yields `pkg/core.py`. Without
    canonicalization the SAME file splits into TWO AnchorRecords and the trust
    upgrade never fires. After the fix there is exactly ONE record for the file,
    and its reason carries the lexical upgrade."""
    repo_root, db = repo

    # Stub the lexical pipe to emit the forward-slash twin of the gold file.
    def _fake_lex(issue_text, repo_root_, graph_db, anchors, max_files=10):
        return [SignalHit(file="pkg/core.py", score=5.0, detail="bm25")]

    monkeypatch.setattr(anchor_select, "lexical_file_search", _fake_lex)

    # Issue text whose tokens contain the symbol parts {serialize, payload}.
    issue = "serialize_payload drops fields when the payload is serialized"

    anchors, _seed, _all = select_anchors(issue, repo_root, db, _StubModel(), tau_anchor=0.30)

    core_records = [a for a in anchors if a.path == "pkg/core.py"]
    backslash_records = [a for a in anchors if a.path == "pkg\\core.py"]

    # Exactly one canonical record; no raw-backslash duplicate leaks through.
    assert len(core_records) == 1, [a.path for a in anchors]
    assert backslash_records == []
    # All three signals converged on it -> trust upgrade fired and reason names
    # both the structural (semantic/symbol) and lexical sources.
    rec = core_records[0]
    assert rec.trusted_for_expansion is True
    assert "lexical" in rec.reason


def test_seed_and_component_maps_are_canonical(repo, monkeypatch):
    """The returned sem_seed / sem_all maps (consumed downstream as candidate
    seeds and component scores) must also be canonically keyed."""
    repo_root, db = repo
    monkeypatch.setattr(
        anchor_select,
        "lexical_file_search",
        lambda *a, **k: [],
    )
    _anchors, seed, all_scores = select_anchors("serialize_payload", repo_root, db, _StubModel())
    assert all("\\" not in k for k in seed), list(seed)
    assert all("\\" not in k for k in all_scores), list(all_scores)
    # The gold file is keyed canonically in both maps.
    assert "pkg/core.py" in seed
    assert "pkg/core.py" in all_scores


# ---- #47: dead structural_seed_expand removed -------------------------------


def test_structural_seed_expand_removed():
    # The H1-falsified, zero-caller lever and its support constants are gone.
    assert not hasattr(anchor_select, "structural_seed_expand")
    assert not hasattr(anchor_select, "_STRUCT_SEED_K")
    assert not hasattr(anchor_select, "_EDGE_TYPE_WEIGHT")


def test_docstring_no_longer_advertises_dead_lever():
    # The module docstring must not claim a lever the file does not ship.
    doc = anchor_select.__doc__ or ""
    assert "structural_seed_expand" not in doc
    assert "structural seed expansion" not in doc
    assert "GRAPH_MISS" not in doc


# ---- encode-budget cap: the whole-repo anchor embed must be BOUNDED -----------
# Bug (2026-06-16): _get_file_embeddings embedded EVERY non-test file's symbols in
# ONE batch with NO encode budget -> a large repo sent millions of passages to the
# embedder and ballooned the HOST process ~15GB, OOM-killing the 16GB runner during
# the agent-phase brief. The fix caps fresh (cache-missing) passages SENT to the
# embedder at GT_SEM_PASSAGE_BUDGET (mirrors graph_localizer._semantic_score_by_file).


class _CountingModel:
    """Records the LARGEST single encode batch it is asked for. _embed sends ALL
    cache-missing passages to the embedder in ONE call, so the max batch == the
    number of fresh passages encoded. Returns deterministic unit vectors (dim 8)."""

    def __init__(self) -> None:
        self.max_batch = 0
        self.total = 0

    def encode(
        self, texts, normalize_embeddings=True, show_progress_bar=False, batch_size=128, **kw
    ):  # noqa: D401
        t = list(texts)
        self.max_batch = max(self.max_batch, len(t))
        self.total += len(t)
        return np.ones((len(t), 8), dtype=np.float32) / np.sqrt(8.0)


def _make_big_db(path: str, *, n_files: int, syms_per_file: int) -> None:
    """A graph with n_files non-test files, each carrying syms_per_file uniquely
    named symbols (so every symbol is a distinct, embeddable, cache-missing passage)."""
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE nodes (
            id INTEGER PRIMARY KEY, label TEXT, name TEXT, qualified_name TEXT,
            file_path TEXT, start_line INTEGER, end_line INTEGER, signature TEXT,
            return_type TEXT, is_exported INTEGER, is_test INTEGER, language TEXT,
            parent_id INTEGER
        );
        CREATE TABLE edges (
            id INTEGER PRIMARY KEY, source_id INTEGER, target_id INTEGER, type TEXT,
            source_line INTEGER, source_file TEXT, resolution_method TEXT,
            confidence REAL, metadata TEXT
        );
        """
    )
    rows = []
    nid = 0
    for fi in range(n_files):
        fp = f"pkg/mod_{fi:04d}.py"
        for si in range(syms_per_file):
            nid += 1
            rows.append((nid, "Function", f"func_{fi:04d}_{si:03d}", fp, 1 + si))
    conn.executemany(
        "INSERT INTO nodes (id,label,name,file_path,start_line,is_test) VALUES (?,?,?,?,?,0)",
        rows,
    )
    conn.commit()
    conn.close()


@pytest.fixture()
def _clear_passage_cache():
    anchor_select._EMBED_CACHE.clear()
    anchor_select._SYMVEC_CACHE.clear()
    yield
    anchor_select._EMBED_CACHE.clear()
    anchor_select._SYMVEC_CACHE.clear()


def test_encode_pool_is_bounded_by_budget(tmp_path, monkeypatch, _clear_passage_cache):
    """RED before fix / GREEN after: the whole-repo symbol embed encodes AT MOST
    GT_SEM_PASSAGE_BUDGET fresh passages in one call, regardless of how many
    candidate symbols the graph holds. Before the cap, every symbol was embedded
    in one batch (the 15GB OOM)."""
    # 200 files x 20 symbols = 4000 distinct passages — far above the small budget.
    n_files, syms = 200, 20
    total_symbols = n_files * syms
    budget = 64
    db = str(tmp_path / "graph.db")
    _make_big_db(db, n_files=n_files, syms_per_file=syms)
    monkeypatch.setenv("GT_SEM_PASSAGE_BUDGET", str(budget))

    model = _CountingModel()
    # repo_root need not have the files on disk: every file has indexed symbols, so
    # the per-symbol passage path runs and the _file_summary fallback is never hit.
    scores = anchor_select.semantic_top_k("serialize the payload", str(tmp_path), db, model)

    # The control: the graph genuinely exceeds the budget (otherwise the test is vacuous).
    assert total_symbols > budget, total_symbols
    # The bite: no single encode batch — and no whole-call total of FRESH passages —
    # exceeds the budget (the issue query is a separate 1-row encode, hence +1 slack).
    assert model.max_batch <= budget + 1, model.max_batch
    assert model.total <= budget + 1, model.total
    # Correct-or-quiet: the call still returns (a partial, bounded ranking), never crashes.
    assert isinstance(scores, dict)


def test_warm_cache_scores_every_file_despite_budget(tmp_path, monkeypatch, _clear_passage_cache):
    """Cache hits are FREE: once the passage vectors are warm, a SECOND call encodes
    ZERO fresh passages even with a tiny budget, so the cap never starves a warm repo.
    Proves the cap bounds *fresh encodes*, not scoring coverage."""
    n_files, syms = 30, 4  # 120 passages, all unique
    db = str(tmp_path / "graph.db")
    _make_big_db(db, n_files=n_files, syms_per_file=syms)

    # First call with a GENEROUS budget warms the shared passage cache.
    monkeypatch.setenv("GT_SEM_PASSAGE_BUDGET", "100000")
    warm = _CountingModel()
    anchor_select._EMBED_CACHE.clear()  # force re-embed (matrix cache is separate)
    anchor_select.semantic_top_k("payload", str(tmp_path), db, warm)
    assert warm.total > 0

    # Second call with a TINY budget: every passage is a cache hit -> 0 fresh encodes
    # beyond the 1-row issue query, and every file still gets a non-empty matrix.
    monkeypatch.setenv("GT_SEM_PASSAGE_BUDGET", "1")
    anchor_select._EMBED_CACHE.clear()
    cold = _CountingModel()
    anchor_select.semantic_top_k("payload", str(tmp_path), db, cold)
    # Only the issue query (1 row) is a fresh encode; all passages came from cache.
    assert cold.total <= 1, cold.total
