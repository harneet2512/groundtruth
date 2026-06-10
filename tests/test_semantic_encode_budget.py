"""Red->green proof for the per-symbol MaxSim ENCODE BLOWUP fix (2026-06-09).

THE BUG (run 27249519544, 113-task sweep): 29 tasks SIGKILL exit-137 during BRIEF
generation on large repos; survivors hit 783s wall-times. Root cause:
``graph_localizer._semantic_score_by_file`` ran CHANGE-1 per-symbol MaxSim with NO
cache on the FULL pre-truncation candidate set (the localize call site passed every
witnessed file BEFORE the top_k cut) — hundreds of files x <=80 ONNX-encoded
passages per task, every task.

THE FIX under test (three levers, all generalized, no benchmark logic):
  1. Call-site pool: only the top ``GT_SEM_POOL_FILES`` candidates under the
     deterministic pre-semantic ordering (grep floor, depth authority, 2-way RRF)
     are semantically scored. Final top_k window is a subset of the pool by
     construction (the semantic term only ADDS RRF mass to pool members).
  2. Content-addressed shared cache: vectors keyed by
     ``passage_hash(model, dim, version, content)`` in the bounded LRU
     ``embed._PASSAGE_VEC_CACHE`` — shared with ``anchor_select`` so a file scored
     by BOTH semantic halves within one task is encoded once (gt_gt section 11.2:
     "cache by node-content hash").
  3. Hard budget: ``GT_SEM_PASSAGE_BUDGET`` caps fresh encodes per call; past it,
     lowest-priority files stay unscored and ONE
     ``[GT_SEM] passage budget hit (X/Y encoded)`` line goes to stderr.

Everything here is deterministic: synthetic graph.db fixtures + a counting fake
embedder (no ONNX, no network, no SWE-bench tasks, no gold labels).
"""

from __future__ import annotations

import sqlite3
import time

import numpy as np
import pytest

from groundtruth.memory.enrich import embed as embed_mod
from groundtruth.pretask import anchor_select
from groundtruth.pretask import graph_localizer as gl
from groundtruth.pretask.anchors import IssueAnchors

_DIM = 64


def _unit(v: np.ndarray) -> np.ndarray:
    return v / np.linalg.norm(v)


class CountingEmbedder:
    """Deterministic fake embedder that COUNTS encoded texts — the proof metric.

    Exposes ``.encode`` (graph_localizer contract: texts[0]=query, texts[1:]=
    passages) AND ``.embed``/``.embed_batch`` (anchor_select/ONNX contract). The
    vector for a given text is fixed (hash-seeded rng), so scores are reproducible
    within a process and cache reuse yields identical results."""

    model_name = "fake/counting"
    dim = _DIM

    def __init__(self) -> None:
        self.encode_calls = 0
        self.texts_encoded = 0  # total texts sent through any encode surface

    def _vec(self, text: str) -> np.ndarray:
        rng = np.random.default_rng(abs(hash(text)) % (2**31))
        return _unit(rng.standard_normal(_DIM)).astype(np.float32)

    def encode(self, texts, normalize_embeddings=True, show_progress_bar=False,
               batch_size=128):
        texts = list(texts)
        self.encode_calls += 1
        self.texts_encoded += len(texts)
        return np.asarray([self._vec(t) for t in texts], dtype=np.float32)

    def embed(self, text, is_query=False):
        self.texts_encoded += 1
        return self._vec(text).tolist()

    def embed_batch(self, texts, is_query=False):
        texts = list(texts)
        self.texts_encoded += len(texts)
        return [self._vec(t).tolist() for t in texts]


def _clear_caches() -> None:
    """Isolate every per-passage / per-graph cache between tests."""
    anchor_select._EMBED_CACHE.clear()
    anchor_select._SYMVEC_CACHE.clear()
    shared = getattr(embed_mod, "_PASSAGE_VEC_CACHE", None)
    if shared is not None:
        shared.clear()


_SCHEMA = """
CREATE TABLE nodes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    label TEXT, name TEXT, qualified_name TEXT, file_path TEXT NOT NULL,
    start_line INTEGER, end_line INTEGER, signature TEXT, return_type TEXT,
    is_exported BOOLEAN DEFAULT 0, is_test BOOLEAN DEFAULT 0,
    language TEXT, parent_id INTEGER
);
CREATE TABLE edges (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id INTEGER NOT NULL, target_id INTEGER NOT NULL, type TEXT NOT NULL,
    source_line INTEGER, source_file TEXT,
    resolution_method TEXT, confidence REAL DEFAULT 0.0, metadata TEXT
);
"""


def _make_flat_graph(path: str, n_files: int, syms_per_file: int) -> list[str]:
    """Go-indexer-shaped graph.db: n_files files x syms_per_file unique Function
    nodes, no edges (unit-level fixture for _semantic_score_by_file)."""
    conn = sqlite3.connect(path)
    conn.executescript(_SCHEMA)
    rows = []
    files = []
    for i in range(n_files):
        fp = f"src/mod_{i:03d}.py"
        files.append(fp)
        for j in range(syms_per_file):
            rows.append(("Function", f"fn_{i}_{j}", fp, f"(a{j}, b{j})"))
    conn.executemany(
        "INSERT INTO nodes (label, name, file_path, signature, is_test, language) "
        "VALUES (?, ?, ?, ?, 0, 'python')",
        rows,
    )
    conn.commit()
    conn.close()
    return files


def _make_witnessed_graph(path: str, n_files: int, syms_per_file: int) -> None:
    """BIG-GRAPH fixture for the full localize() path: ONE seed symbol
    (``frobnicate_widget`` in src/seed.py) with a verified CALLS edge into each of
    n_files neighbor files -> every neighbor becomes a witnessed hop-1 candidate,
    reproducing the 'hundreds of witnessed files' shape that OOM-killed the sweep."""
    conn = sqlite3.connect(path)
    conn.executescript(_SCHEMA)
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO nodes (label, name, file_path, signature, is_test, language) "
        "VALUES ('Function', 'frobnicate_widget', 'src/seed.py', '(cfg)', 0, 'python')"
    )
    seed_id = cur.lastrowid
    edge_rows = []
    for i in range(n_files):
        fp = f"pkg/neigh_{i:03d}.py"
        first_id = None
        for j in range(syms_per_file):
            cur.execute(
                "INSERT INTO nodes (label, name, file_path, signature, is_test, language) "
                "VALUES ('Function', ?, ?, ?, 0, 'python')",
                (f"helper_{i}_{j}", fp, f"(x{j})"),
            )
            if first_id is None:
                first_id = cur.lastrowid
        edge_rows.append((seed_id, first_id, "CALLS", "import", 1.0))
    cur.executemany(
        "INSERT INTO edges (source_id, target_id, type, resolution_method, confidence) "
        "VALUES (?, ?, ?, ?, ?)",
        edge_rows,
    )
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# 1. HARD ENCODE BUDGET on _semantic_score_by_file (RED pre-fix: encoded all 9000)
# ---------------------------------------------------------------------------

def test_encode_budget_bounds_encodes_and_logs(tmp_path, monkeypatch, capsys):
    _clear_caches()
    n_files, spf = 300, 30                      # 9000 unique passages
    db = str(tmp_path / "graph.db")
    files = _make_flat_graph(db, n_files, spf)

    model = CountingEmbedder()
    monkeypatch.setattr(gl, "_get_embedder", lambda: model)
    monkeypatch.setenv("GT_SEM_PASSAGE_BUDGET", "500")
    monkeypatch.delenv("GT_PROOF_MODE", raising=False)

    t0 = time.monotonic()
    scores = gl._semantic_score_by_file("fix the frobnicate issue", db, files)
    wall = time.monotonic() - t0

    total = n_files * spf
    # THE bound: fresh encodes <= budget (+1 query text per encode call).
    # OLD path encoded every one of the 9000 passages -> RED.
    assert model.texts_encoded <= 500 + model.encode_calls, (
        f"encode blowup: {model.texts_encoded} texts encoded, budget was 500 "
        f"(old behavior = all {total} passages)"
    )
    assert model.texts_encoded < total, "budget did not bound the encode set"
    # ONE budget line, stderr only, exact prefix per the fix spec.
    err = capsys.readouterr().err
    assert "[GT_SEM] passage budget hit (500/9000 encoded" in err, (
        f"budget log line missing/wrong, stderr was: {err!r}"
    )
    # Contract preserved: dict[file -> float] in [0, 1].
    assert scores and isinstance(scores, dict)
    for v in scores.values():
        assert 0.0 <= v <= 1.0
    # PRIORITY semantics: the budget truncates from the BACK of the caller's
    # order — the first (highest-priority) file is scored, the last is not.
    assert "src/mod_000.py" in scores
    assert "src/mod_299.py" not in scores
    # Bounded-perf assertion (fake embedder; guards future accidental O(N) work).
    assert wall < 30.0, f"_semantic_score_by_file took {wall:.1f}s on the fixture"


# ---------------------------------------------------------------------------
# 2. CONTENT-ADDRESSED CACHE dedup (RED pre-fix: second call re-encoded all 50)
# ---------------------------------------------------------------------------

def test_passage_cache_dedups_repeat_scoring(tmp_path, monkeypatch):
    _clear_caches()
    db = str(tmp_path / "graph.db")
    files = _make_flat_graph(db, 10, 5)         # 50 unique passages
    monkeypatch.setenv("GT_SEM_PASSAGE_BUDGET", "4096")
    monkeypatch.delenv("GT_PROOF_MODE", raising=False)

    m1 = CountingEmbedder()
    monkeypatch.setattr(gl, "_get_embedder", lambda: m1)
    s1 = gl._semantic_score_by_file("fix the widget bug", db, files)
    assert m1.texts_encoded == 50 + m1.encode_calls  # 50 passages + the query

    m2 = CountingEmbedder()
    monkeypatch.setattr(gl, "_get_embedder", lambda: m2)
    s2 = gl._semantic_score_by_file("fix the widget bug", db, files)
    # Every passage is now a cache hit: ONLY the query text is encoded.
    assert m2.texts_encoded == m2.encode_calls, (
        f"cache miss on identical passages: {m2.texts_encoded} texts re-encoded"
    )
    # Cached vectors reproduce the exact same scores (determinism).
    assert s2 == s1


def test_cross_half_cache_reuse_anchor_select_primes_localizer(tmp_path, monkeypatch):
    """The within-task dedup that matters most: the SAME file scored by BOTH
    semantic halves (anchor_select.semantic_top_k for run_v74, then
    graph_localizer._semantic_score_by_file for localize) encodes each passage
    ONCE — the two halves share one content-addressed store."""
    _clear_caches()
    db = str(tmp_path / "graph.db")
    files = _make_flat_graph(db, 10, 5)
    monkeypatch.setenv("GT_SEM_PASSAGE_BUDGET", "4096")
    monkeypatch.delenv("GT_PROOF_MODE", raising=False)

    m1 = CountingEmbedder()
    # Half 1 (run_v74 path) embeds every indexed file's passages.
    anchor_select.semantic_top_k(
        "fix the widget bug", str(tmp_path), db, m1, score_all=True
    )
    assert m1.texts_encoded >= 50  # primed the shared store

    m2 = CountingEmbedder()
    monkeypatch.setattr(gl, "_get_embedder", lambda: m2)
    gl._semantic_score_by_file("fix the widget bug", db, files)
    # Half 2 re-encodes NOTHING but its query text.
    assert m2.texts_encoded == m2.encode_calls, (
        f"localizer half re-encoded {m2.texts_encoded - m2.encode_calls} passages "
        "already paid for by the anchor_select half"
    )


def test_shared_cache_is_one_object_and_bounded():
    """anchor_select._SYMVEC_CACHE and embed._PASSAGE_VEC_CACHE are the SAME
    store (no third cache variant), and the LRU evicts past maxsize."""
    assert anchor_select._SYMVEC_CACHE is embed_mod._PASSAGE_VEC_CACHE
    c = embed_mod._PassageVecCache(maxsize=3)
    for i in range(5):
        c[f"h{i}"] = np.zeros(2, dtype=np.float32)
    assert len(c) == 3
    assert "h0" not in c and "h1" not in c and "h4" in c


# ---------------------------------------------------------------------------
# 3. CALL-SITE POOL: localize() scores only the top pre-semantic candidates
#    (RED pre-fix: passed ALL witnessed files, encoded every passage)
# ---------------------------------------------------------------------------

def test_localize_semantic_pool_caps_callsite_and_encodes(tmp_path, monkeypatch):
    _clear_caches()
    n_files, spf = 250, 20                      # 250 witnessed files, 5001 passages
    db = str(tmp_path / "graph.db")
    _make_witnessed_graph(db, n_files, spf)

    model = CountingEmbedder()
    monkeypatch.setattr(gl, "_get_embedder", lambda: model)
    monkeypatch.delenv("GT_SEM_PASSAGE_BUDGET", raising=False)
    monkeypatch.delenv("GT_SEM_POOL_FILES", raising=False)
    monkeypatch.delenv("GT_PROOF_MODE", raising=False)

    seen: dict[str, int] = {}
    orig = gl._semantic_score_by_file

    def spy(issue, dbp, files):
        files = list(files)
        seen["n_files"] = len(files)
        return orig(issue, dbp, files)

    monkeypatch.setattr(gl, "_semantic_score_by_file", spy)

    t0 = time.monotonic()
    res = gl.localize(
        "frobnicate_widget crashes when the config is reloaded twice",
        db,
        issue_anchors=IssueAnchors(symbols={"frobnicate_widget"}),
        top_k=8,
    )
    wall = time.monotonic() - t0

    pool_cap = gl._sem_pool_files(8)            # default max(6*8, 48) = 48
    # The semantic half saw the POOL, not the full pre-truncation candidate set.
    # OLD behavior: all 251 witnessed files -> RED.
    assert seen.get("n_files", 0) > 0, "semantic ranker never ran"
    assert seen["n_files"] <= pool_cap, (
        f"semantic ranker scored {seen['n_files']} files; pool cap is {pool_cap} "
        f"(old behavior = all {n_files + 1} witnessed candidates)"
    )
    # Encode work is O(pool x passages), not O(all-witnessed-files x passages).
    max_allowed = pool_cap * spf + model.encode_calls
    total_passages = n_files * spf + 1
    assert model.texts_encoded <= max_allowed, (
        f"encoded {model.texts_encoded} texts > pool bound {max_allowed}"
    )
    assert model.texts_encoded < total_passages, (
        f"encoded the whole witness set ({model.texts_encoded} texts) — blowup"
    )
    # Localize still returns a sane, truncated, scored result.
    assert res.candidates and len(res.candidates) <= 8
    # Bounded-perf assertion on the big fixture.
    assert wall < 30.0, f"localize took {wall:.1f}s on the {n_files}-file fixture"


def test_final_window_is_subset_of_semantic_pool(tmp_path, monkeypatch):
    """The set-choice soundness claim, asserted: every candidate in the final
    top_k window carries a semantic score (i.e. was IN the scored pool) whenever
    the semantic ranker ran — the pool can change WHO wins, but no candidate
    outside the scored pool can leak into the window past unscored peers."""
    _clear_caches()
    db = str(tmp_path / "graph.db")
    _make_witnessed_graph(db, 100, 10)

    model = CountingEmbedder()
    monkeypatch.setattr(gl, "_get_embedder", lambda: model)
    monkeypatch.delenv("GT_PROOF_MODE", raising=False)

    captured: dict[str, dict] = {}
    orig = gl._semantic_score_by_file

    def spy(issue, dbp, files):
        out = orig(issue, dbp, list(files))
        captured["sem"] = out
        return out

    monkeypatch.setattr(gl, "_semantic_score_by_file", spy)
    res = gl.localize(
        "frobnicate_widget crashes when the config is reloaded twice",
        db,
        issue_anchors=IssueAnchors(symbols={"frobnicate_widget"}),
        top_k=8,
    )
    sem = captured.get("sem", {})
    assert sem, "semantic ranker produced no scores on the fixture"
    for c in res.candidates:
        key = c.file_path.replace("\\", "/").lstrip("./").lstrip("/")
        assert key in sem, (
            f"final-window candidate {c.file_path} was never semantically scored "
            "(final top_k must be a subset of the scored pool)"
        )


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q", "-s"]))
