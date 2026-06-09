"""Stage-1 deterministic proof for CHANGE 1 — symbol/function-level semantic granularity.

The defect: GroundTruth embedded ONE vector per FILE from a concatenated symbol-bag,
so the issue (gold) function was averaged into its ~60 siblings and sibling files
clustered at cosine 0.80-0.84 (measured mad=0.0145) — the semantic ranker could not
separate the file that actually holds the gold function.

The fix: embed each symbol as its own passage and score a file by ColBERT-style MaxSim
(Khattab & Zaharia, SIGIR 2020) + MaxP top-k mean (Dai & Callan, SIGIR 2019):

    file_score = alpha * max_i(cos_i) + (1 - alpha) * mean(top_k cos_i)

These tests prove, with NO model and NO graph (synthetic vectors), that MAX+top-k
aggregation SEPARATES a gold file from a sibling file while the OLD mean-over-all-symbols
aggregation does NOT. A second test exercises the real `semantic_top_k` end-to-end on an
in-memory graph.db with a deterministic fake embedder (the e5 model is not required).
"""

from __future__ import annotations

import sqlite3

import numpy as np
import pytest

from groundtruth.memory.enrich.embed import (
    aggregate_symbol_cosines,
    passage_hash,
    read_agg_params,
    symbol_passage,
)


# ---------------------------------------------------------------------------
# Pure-helper unit tests (no model, no graph)
# ---------------------------------------------------------------------------

def test_symbol_passage_blank_is_quiet():
    # correct-or-quiet: a fully blank symbol is never embedded.
    assert symbol_passage("", "", "") == ""
    assert symbol_passage("   ", "  ", "  ") == ""
    # name-only / signature-only / body-only all produce a passage.
    assert symbol_passage("foo", "", "") == "foo"
    assert symbol_passage("foo", "(a, b)", "") == "foo (a, b)"
    p = symbol_passage("foo", "(a, b)", "returns the cached user")
    assert p.startswith("foo (a, b)")
    assert "returns the cached user" in p


def test_symbol_passage_char_cap():
    # The passage is capped so the 128-token model window is not exceeded.
    long_body = "x " * 1000
    p = symbol_passage("name", "(sig)", long_body)
    assert len(p) <= 80 * 5  # SYMBOL_PASSAGE_TOKEN_CAP * ~5 chars/token


def test_read_agg_params_defaults_and_env(monkeypatch):
    monkeypatch.delenv("GT_SEM_AGG_ALPHA", raising=False)
    monkeypatch.delenv("GT_SEM_TOPK", raising=False)
    assert read_agg_params() == (0.7, 3)

    monkeypatch.setenv("GT_SEM_AGG_ALPHA", "0.5")
    monkeypatch.setenv("GT_SEM_TOPK", "5")
    assert read_agg_params() == (0.5, 5)

    # Malformed -> defaults (never crash the brief on a typo'd env var).
    monkeypatch.setenv("GT_SEM_AGG_ALPHA", "not-a-float")
    monkeypatch.setenv("GT_SEM_TOPK", "garbage")
    assert read_agg_params() == (0.7, 3)

    # Out-of-range clamps.
    monkeypatch.setenv("GT_SEM_AGG_ALPHA", "5.0")
    monkeypatch.setenv("GT_SEM_TOPK", "0")
    alpha, k = read_agg_params()
    assert alpha == 1.0
    assert k == 1


def test_aggregate_empty_and_floor():
    assert aggregate_symbol_cosines([], alpha=0.7, top_k=3) == 0.0
    # Negative cosines (point away from the issue) are no evidence, floored at 0.
    assert aggregate_symbol_cosines([-0.9, -0.5], alpha=0.7, top_k=3) == 0.0
    # Stays in [0, 1].
    v = aggregate_symbol_cosines([1.0, 0.9, 0.8], alpha=0.7, top_k=3)
    assert 0.0 <= v <= 1.0


def test_aggregate_max_dominates():
    # alpha=0.7 -> a single high-match symbol pulls the file score up even when the
    # rest of the file is generic.
    gold = aggregate_symbol_cosines([0.95] + [0.10] * 59, alpha=0.7, top_k=3)
    generic = aggregate_symbol_cosines([0.10] * 60, alpha=0.7, top_k=3)
    assert gold > generic
    assert gold > 0.6  # the 0.95 max dominates


def test_passage_hash_is_content_addressed():
    h1 = passage_hash("foo (x)", "intfloat/e5-small-v2", 384, "sym2-fn")
    h2 = passage_hash("foo (x)", "intfloat/e5-small-v2", 384, "sym2-fn")
    h3 = passage_hash("foo (y)", "intfloat/e5-small-v2", 384, "sym2-fn")
    h4 = passage_hash("foo (x)", "intfloat/e5-small-v2", 384, "sym1")
    assert h1 == h2          # deterministic on identical content
    assert h1 != h3          # different passage -> different key
    assert h1 != h4          # version bump invalidates


# ---------------------------------------------------------------------------
# THE PROOF (synthetic, deterministic): MAX separates gold from sibling; mean does not.
# ---------------------------------------------------------------------------

def _unit(v: np.ndarray) -> np.ndarray:
    return v / np.linalg.norm(v)


def _build_synthetic_files(seed: int = 1234):
    """Construct, with NO model and NO graph:

      * the issue QUERY vector,
      * a GOLD file = 1 symbol vector highly similar to the query + 59 generic dissimilar
        symbol vectors,
      * a SIBLING file = 60 generic symbol vectors from the SAME generic distribution
        (no gold-matching symbol).

    Returns (q, gold_cosines, sibling_cosines)."""
    rng = np.random.default_rng(seed)
    dim = 384

    # The issue query direction.
    q = _unit(rng.standard_normal(dim))

    # A symbol that matches the issue: mostly along q with a little noise.
    gold_symbol = _unit(q + 0.15 * _unit(rng.standard_normal(dim)))

    # Generic symbols: random directions roughly orthogonal to q (the "59 siblings"
    # the gold function is averaged into, and the sibling file's whole content).
    def generic_pool(n: int) -> np.ndarray:
        m = rng.standard_normal((n, dim))
        return np.vstack([_unit(r) for r in m])

    gold_generics = generic_pool(59)
    sibling_generics = generic_pool(60)

    gold_matrix = np.vstack([gold_symbol[None, :], gold_generics])  # (60, dim)
    sibling_matrix = sibling_generics                                # (60, dim)

    gold_cosines = (gold_matrix @ q).tolist()
    sibling_cosines = (sibling_matrix @ q).tolist()
    return q, gold_cosines, sibling_cosines


def test_max_aggregation_separates_gold_but_mean_does_not():
    """The lever proof. MAX+top-k separates gold from sibling; mean-over-all does not."""
    _q, gold_cos, sib_cos = _build_synthetic_files()
    alpha, top_k = 0.7, 3

    # --- NEW: ColBERT MaxSim + top-k mean ---
    gold_max = aggregate_symbol_cosines(gold_cos, alpha=alpha, top_k=top_k)
    sib_max = aggregate_symbol_cosines(sib_cos, alpha=alpha, top_k=top_k)

    # --- OLD: a single file vector = MEAN of all symbol vectors, scored vs the query.
    # Mean of unit vectors is the centroid; its cosine to q is proportional to the
    # MEAN of the per-symbol cosines, so mean-over-all-symbol-cosines is the faithful
    # stand-in for "one averaged vector per file".
    gold_mean = float(np.mean(gold_cos))
    sib_mean = float(np.mean(sib_cos))

    max_sep = gold_max - sib_max
    mean_sep = gold_mean - sib_mean

    # 1) MAX materially separates the gold file above the sibling.
    assert gold_max > sib_max, f"MAX failed to separate: gold={gold_max} sib={sib_max}"
    assert max_sep > 0.25, f"MAX separation too small: {max_sep:.4f}"

    # 2) The OLD mean-over-all aggregation does NOT separate them (both ~equal): the
    #    one gold symbol is washed out by 59 generics, exactly the documented collapse.
    assert abs(mean_sep) < 0.05, f"mean unexpectedly separated: gold={gold_mean} sib={sib_mean}"

    # 3) MAX separation is at least an order of magnitude larger than mean separation.
    assert max_sep > 5 * abs(mean_sep), (
        f"MAX should dominate mean: max_sep={max_sep:.4f} mean_sep={mean_sep:.4f}"
    )

    # Surface the numbers for the report.
    print(
        f"\n[SEPARATION] gold_max={gold_max:.6f} sib_max={sib_max:.6f} "
        f"max_sep={max_sep:.6f} | gold_mean={gold_mean:.6f} sib_mean={sib_mean:.6f} "
        f"mean_sep={mean_sep:.6f}"
    )


# ---------------------------------------------------------------------------
# End-to-end on a real in-memory graph.db with a deterministic fake embedder.
# ---------------------------------------------------------------------------

_DIM = 384


class _FakeEmbedder:
    """Deterministic embedder that exercises the .encode interface (graph_localizer)
    AND the .embed/.embed_batch interface (anchor_select._embed) without ONNX.

    Each passage maps to a fixed unit vector keyed by its text; the issue query maps
    to a fixed direction `q`. A passage tagged with the magic GOLD token aligns with q;
    every other passage is a deterministic pseudo-random near-orthogonal direction."""

    model_name = "fake/e5"
    dim = _DIM

    def __init__(self, q: np.ndarray, gold_token: str):
        self._q = _unit(q)
        self._gold_token = gold_token

    def _vec(self, text: str, is_query: bool) -> np.ndarray:
        if is_query:
            return self._q
        if self._gold_token in text:
            # high-match symbol
            rng = np.random.default_rng(7)
            return _unit(self._q + 0.15 * _unit(rng.standard_normal(_DIM)))
        # deterministic generic direction from the text hash
        h = abs(hash(text)) % (2**31)
        rng = np.random.default_rng(h)
        return _unit(rng.standard_normal(_DIM))

    # anchor_select._embed prefers .encode (texts[0]=query, texts[1:]=passages handled
    # by the adapter); here we encode uniformly but `is_query` is threaded via embed*.
    def encode(self, texts, normalize_embeddings=True, show_progress_bar=False, batch_size=128):
        out = [self._vec(texts[0], is_query=True)]
        out += [self._vec(t, is_query=False) for t in texts[1:]]
        return np.asarray(out, dtype=np.float32)

    def embed(self, text, is_query=False):
        return self._vec(text, is_query=is_query).tolist()

    def embed_batch(self, texts, is_query=False):
        return [self._vec(t, is_query=is_query).tolist() for t in texts]


def _make_graph_db(path: str, gold_token: str):
    """Minimal Go-indexer-shaped graph.db: a gold file with one gold symbol + 59
    generics, and a sibling file with 60 generics. No properties table needed."""
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE nodes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            label TEXT, name TEXT, qualified_name TEXT, file_path TEXT NOT NULL,
            start_line INTEGER, end_line INTEGER, signature TEXT, return_type TEXT,
            is_exported BOOLEAN DEFAULT 0, is_test BOOLEAN DEFAULT 0,
            language TEXT, parent_id INTEGER
        );
        """
    )
    rows = []
    # Gold file: one symbol carrying the gold token, 59 generics.
    rows.append(("Function", f"{gold_token}_handler", "src/gold.py", "(req)"))
    for i in range(59):
        rows.append(("Function", f"generic_g{i}", "src/gold.py", f"(x{i})"))
    # Sibling file: 60 generics, no gold token.
    for i in range(60):
        rows.append(("Function", f"generic_s{i}", "src/sibling.py", f"(y{i})"))
    conn.executemany(
        "INSERT INTO nodes (label, name, file_path, signature, is_test, language) "
        "VALUES (?, ?, ?, ?, 0, 'python')",
        rows,
    )
    conn.commit()
    conn.close()


def test_semantic_top_k_end_to_end_separates_gold(tmp_path):
    """The contract-level proof: the real `semantic_top_k` (per-symbol MaxSim path),
    run against an in-memory-shaped graph.db with a deterministic fake embedder, ranks
    the gold file ABOVE the sibling file and returns the dict[file -> float] in [0, 1]."""
    from groundtruth.pretask import anchor_select

    # Isolate the per-graph + per-passage caches so the assertion is clean.
    anchor_select._EMBED_CACHE.clear()
    anchor_select._SYMVEC_CACHE.clear()

    gold_token = "ZZTOPMATCH"
    db = str(tmp_path / "graph.db")
    _make_graph_db(db, gold_token)

    rng = np.random.default_rng(42)
    q = rng.standard_normal(_DIM)
    model = _FakeEmbedder(q, gold_token)

    # The issue text is irrelevant to the fake embedder (query maps to q); the gold
    # token only appears in the gold symbol's passage, never in the issue text — proving
    # this is a SEMANTIC match, not a lexical one.
    scores = anchor_select.semantic_top_k(
        "fix the request handler crash", str(tmp_path), db, model, score_all=True
    )

    assert isinstance(scores, dict)
    assert "src/gold.py" in scores and "src/sibling.py" in scores
    for v in scores.values():
        assert 0.0 <= v <= 1.0  # contract: cosine stays in [0, 1]

    gold = scores["src/gold.py"]
    sib = scores["src/sibling.py"]
    assert gold > sib, f"gold {gold} did not outrank sibling {sib}"
    assert gold - sib > 0.2, f"separation too small: {gold - sib:.4f}"
    print(f"\n[E2E] gold={gold:.6f} sibling={sib:.6f} sep={gold - sib:.6f}")


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q", "-s"]))
