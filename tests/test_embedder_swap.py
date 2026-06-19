"""CHANGE 2 — swap the pretask-localization embedder to a code-tuned, multilingual,
open-source ONNX model (Alibaba-NLP/gte-modernbert-base, Apache-2.0, 768-dim) with
e5-small-v2 (MIT, 384-dim) as the runtime fallback.

What these tests prove (deterministically, no benchmark task ids):

  1. Identity is single-sourced: get_embedding_model() with no args resolves to the
     CHANGE-2 default; GT_EMBED_MODEL_NAME / GT_EMBED_DIM override it; the e5 fallback
     keeps its e5/384 identity, prefixes (query:/passage:) and MEAN pooling, while the
     code-tuned default is symmetric (no prefix) with CLS pooling.

  2. The ONNX-input introspection is CONDITIONAL: the feed dict is built from the model's
     declared input names, so a ModernBERT graph (no token_type_ids) is never fed one and
     an e5 graph still receives all three. This is the load-bearing risk for the swap.

  3. Dim-agnostic scoring: aggregate_symbol_cosines / the MaxSim aggregation and the
     zero-fallback width follow model.dim (384 OR 768), and _total_score consumes the
     scalar cosine component identically at any dim.

  4. The sqlite-vec MEMORY store stays pinned to e5/384 (MemoryConfig defaults) — flipping
     the localization default does NOT migrate the memory subsystem.

The live model-load tests (gte ONNX loads, no token_type_ids error, related>unrelated
discrimination) RUN ONLY WHEN the model files are baked under models/ — they skip cleanly
otherwise so CI without the baked model still passes the structural assertions. Bake with
`python scripts/setup_models.py`.
"""

from __future__ import annotations

import importlib
import math
import os

import numpy as np
import pytest

import groundtruth.memory.enrich.embed as embmod
from groundtruth.memory.enrich.embed import (
    DEFAULT_EMBED_DIM,
    DEFAULT_EMBED_MODEL,
    E5_DIM,
    E5_MODEL,
    EmbeddingModel,
    aggregate_symbol_cosines,
    get_embedding_model,
)


# ---------------------------------------------------------------------------
# (1) Single-sourced identity + per-model prefix/pooling (no ONNX needed)
# ---------------------------------------------------------------------------

def test_default_is_code_tuned_and_open():
    assert DEFAULT_EMBED_MODEL == "Alibaba-NLP/gte-modernbert-base"
    assert DEFAULT_EMBED_DIM == 768
    # e5 remains a first-class fallback identity.
    assert E5_MODEL == "intfloat/e5-small-v2"
    assert E5_DIM == 384


def test_no_arg_default_resolves_to_code_tuned(monkeypatch):
    monkeypatch.delenv("GT_EMBED_MODEL_NAME", raising=False)
    monkeypatch.delenv("GT_EMBED_DIM", raising=False)
    importlib.reload(embmod)
    assert embmod._default_embed_model() == "Alibaba-NLP/gte-modernbert-base"
    assert embmod._default_embed_dim() == 768


def test_env_overrides_identity(monkeypatch):
    monkeypatch.setenv("GT_EMBED_MODEL_NAME", "intfloat/e5-small-v2")
    monkeypatch.setenv("GT_EMBED_DIM", "384")
    importlib.reload(embmod)
    assert embmod._default_embed_model() == "intfloat/e5-small-v2"
    assert embmod._default_embed_dim() == 384
    # name-only override of a KNOWN model keeps dim consistent.
    monkeypatch.delenv("GT_EMBED_DIM", raising=False)
    importlib.reload(embmod)
    assert embmod._default_embed_dim() == 384
    # cleanup
    monkeypatch.delenv("GT_EMBED_MODEL_NAME", raising=False)
    importlib.reload(embmod)


def test_code_tuned_model_is_symmetric_cls():
    m = EmbeddingModel(DEFAULT_EMBED_MODEL, DEFAULT_EMBED_DIM)
    assert m.prefix_query == ""
    assert m.prefix_passage == ""
    assert m.pooling == "cls"
    assert m.dim == 768


def test_e5_model_is_prefixed_mean():
    m = EmbeddingModel(E5_MODEL, E5_DIM)
    assert m.prefix_query == "query: "
    assert m.prefix_passage == "passage: "
    assert m.pooling == "mean"
    assert m.dim == 384


# ---------------------------------------------------------------------------
# (2) ONNX-input introspection is CONDITIONAL (load-bearing) — no real ONNX
# ---------------------------------------------------------------------------

class _FakeInput:
    def __init__(self, name: str) -> None:
        self.name = name


class _FakeSession:
    """Stand-in for ort.InferenceSession that RECORDS the feed dict it is given and
    returns a fixed token-embedding tensor, so we can assert which inputs were fed
    WITHOUT downloading a model."""

    def __init__(self, input_names: list[str], hidden: int) -> None:
        self._inputs = [_FakeInput(n) for n in input_names]
        self._hidden = hidden
        self.last_feed: dict | None = None

    def get_inputs(self):
        return self._inputs

    def run(self, _outputs, feed):
        self.last_feed = feed
        batch = feed["input_ids"].shape[0]
        seq = feed["input_ids"].shape[1]
        return [np.ones((batch, seq, self._hidden), dtype=np.float32)]


class _FakeEncoding:
    def __init__(self, ids, mask):
        self.ids = ids
        self.attention_mask = mask


class _FakeTokenizer:
    def encode_batch(self, texts):
        return [_FakeEncoding([1, 2, 3], [1, 1, 1]) for _ in texts]


def _wire_fake(model: EmbeddingModel, input_names: list[str]) -> _FakeSession:
    sess = _FakeSession(input_names, model.dim)
    model._session = sess  # type: ignore[assignment]
    model._tokenizer = _FakeTokenizer()  # type: ignore[assignment]
    model._input_names = input_names
    return sess


def test_modernbert_inputs_omit_token_type_ids():
    """gte-modernbert ONNX declares only input_ids + attention_mask — the feed dict
    must NOT contain token_type_ids (feeding it would raise in real onnxruntime)."""
    m = EmbeddingModel(DEFAULT_EMBED_MODEL, DEFAULT_EMBED_DIM)
    sess = _wire_fake(m, ["input_ids", "attention_mask"])
    out = m.embed_batch(["some code symbol passage"], is_query=False)
    assert sess.last_feed is not None
    assert set(sess.last_feed.keys()) == {"input_ids", "attention_mask"}
    assert "token_type_ids" not in sess.last_feed
    assert len(out[0]) == 768  # CLS-pooled width follows hidden size


def test_e5_inputs_include_token_type_ids():
    """e5 ONNX declares all three inputs — token_type_ids MUST be fed for e5."""
    m = EmbeddingModel(E5_MODEL, E5_DIM)
    sess = _wire_fake(m, ["input_ids", "attention_mask", "token_type_ids"])
    m.embed_batch(["passage text"], is_query=False)
    assert sess.last_feed is not None
    assert set(sess.last_feed.keys()) == {"input_ids", "attention_mask", "token_type_ids"}


def test_pooling_differs_by_model():
    """CLS picks token 0; mean averages over the mask. With an all-ones token tensor both
    pool to the same value, so assert the CODE PATH taken, not the (degenerate) value."""
    cls_m = EmbeddingModel(DEFAULT_EMBED_MODEL, DEFAULT_EMBED_DIM)
    _wire_fake(cls_m, ["input_ids", "attention_mask"])
    assert cls_m.pooling == "cls"
    vec = cls_m.embed("x", is_query=True)
    assert len(vec) == 768
    mean_m = EmbeddingModel(E5_MODEL, E5_DIM)
    _wire_fake(mean_m, ["input_ids", "attention_mask", "token_type_ids"])
    assert mean_m.pooling == "mean"
    assert len(mean_m.embed("x", is_query=True)) == 384


# ---------------------------------------------------------------------------
# (3) Dim-agnostic scoring at 768
# ---------------------------------------------------------------------------

def test_aggregate_symbol_cosines_dim_agnostic():
    """The MaxSim aggregation operates on COSINES (scalars), so it is independent of dim.
    Build cosines from real 768-dim unit vectors to prove the contract at the new dim."""
    rng = np.random.default_rng(0)
    issue = rng.standard_normal(768).astype(np.float32)
    issue /= np.linalg.norm(issue)
    syms = rng.standard_normal((5, 768)).astype(np.float32)
    syms /= np.linalg.norm(syms, axis=1, keepdims=True)
    cosines = (syms @ issue).tolist()
    score = aggregate_symbol_cosines(cosines, alpha=0.7, top_k=3)
    assert 0.0 <= score <= 1.0


def test_zero_fallback_width_follows_model_dim(monkeypatch):
    """_ZeroEmbeddingModel must emit model.dim-wide zero vectors (768 default, 384 under
    e5 override) — not a hardcoded 384."""
    from groundtruth.pretask.v7_4_brief import _ZeroEmbeddingModel

    monkeypatch.delenv("GT_EMBED_MODEL_NAME", raising=False)
    monkeypatch.delenv("GT_EMBED_DIM", raising=False)
    importlib.reload(embmod)
    z = _ZeroEmbeddingModel()
    assert z.dim == 768
    out = z.encode(["a", "b"])
    assert np.asarray(out).shape == (2, 768)

    monkeypatch.setenv("GT_EMBED_MODEL_NAME", "intfloat/e5-small-v2")
    monkeypatch.setenv("GT_EMBED_DIM", "384")
    importlib.reload(embmod)
    z2 = _ZeroEmbeddingModel()
    assert z2.dim == 384
    assert np.asarray(z2.encode(["x"])).shape == (1, 384)
    monkeypatch.delenv("GT_EMBED_MODEL_NAME", raising=False)
    monkeypatch.delenv("GT_EMBED_DIM", raising=False)
    importlib.reload(embmod)


def test_total_score_consumes_scalar_sem_component():
    """_total_score takes the cosine as a scalar component — dim never reaches it."""
    from groundtruth.pretask.v7_4_brief import _total_score

    weights = {"W_SEM": 0.15, "W_LEX": 0.50, "W_REACH": 0.05}
    s_hi = _total_score({"sem": 0.9, "lex": 0.2, "reach": 0.0}, weights)
    s_lo = _total_score({"sem": 0.1, "lex": 0.2, "reach": 0.0}, weights)
    assert s_hi > s_lo  # higher cosine -> higher score, at any embedding dim


# ---------------------------------------------------------------------------
# (4) Memory vec store stays pinned to e5/384 (NOT migrated)
# ---------------------------------------------------------------------------

def test_memory_config_pinned_to_e5(monkeypatch):
    """MemoryConfig defaults to e5/384 and is independent of GT_EMBED_MODEL_NAME — the
    sqlite-vec store does not flip with the localization embedder.

    The memory subsystem (groundtruth.memory.config) is gitignored (`.gitignore: Memory/`)
    and is NOT shipped to the localization worktree/container; skip when it is absent. The
    pin is ALSO covered structurally by test_memory_embed_helpers_default_to_e5 (the embed
    helpers the memory store actually calls keep e5/384 defaults regardless of env)."""
    pytest.importorskip(
        "groundtruth.memory.config",
        reason="memory subsystem not present in this checkout (gitignored) — pin also "
        "covered by test_memory_embed_helpers_default_to_e5",
    )
    from groundtruth.memory.config import MemoryConfig

    monkeypatch.setenv("GT_EMBED_MODEL_NAME", "Alibaba-NLP/gte-modernbert-base")
    monkeypatch.setenv("GT_EMBED_DIM", "768")
    cfg = MemoryConfig()
    # MemoryConfig reads GT_EMBEDDING_MODEL/GT_EMBEDDING_DIM (distinct env vars), defaulting
    # to e5/384 — it deliberately does NOT read the localization GT_EMBED_MODEL_NAME/DIM.
    assert cfg.embedding_model == "intfloat/e5-small-v2"
    assert cfg.embedding_dim == 384
    monkeypatch.delenv("GT_EMBED_MODEL_NAME", raising=False)
    monkeypatch.delenv("GT_EMBED_DIM", raising=False)


def test_memory_embed_helpers_default_to_e5():
    """embed_query/embed_passage/embed_batch keep e5/384 defaults so a memory caller that
    relies on the default never picks up the localization model."""
    import inspect

    from groundtruth.memory.enrich import embed as e
    for fn in (e.embed_query, e.embed_passage, e.embed_batch):
        sig = inspect.signature(fn)
        assert sig.parameters["model_name"].default == "intfloat/e5-small-v2"
        assert sig.parameters["dim"].default == 384


# ---------------------------------------------------------------------------
# (5) Model-keyed embedding cache (P0 fix 2026-06-09) — a gte<->e5 swap must MISS
# ---------------------------------------------------------------------------

import pickle
import sqlite3
from pathlib import Path


class _DimEmbedder:
    """Deterministic fake embedder exposing the container interface
    (.embed_batch/.embed, model_name + dim) at an arbitrary dim."""

    def __init__(self, name: str, dim: int) -> None:
        self.model_name = name
        self.dim = dim

    def _vec(self, text: str) -> list[float]:
        v = np.zeros(self.dim, dtype=np.float32)
        v[abs(hash(text)) % self.dim] = 1.0
        return v.tolist()

    def embed(self, text, is_query=False):
        return self._vec(text)

    def embed_batch(self, texts, is_query=False):
        return [self._vec(t) for t in texts]


def _mini_graph(path: str) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(
        "CREATE TABLE nodes (id INTEGER PRIMARY KEY AUTOINCREMENT, label TEXT,"
        " name TEXT, qualified_name TEXT, file_path TEXT NOT NULL,"
        " start_line INTEGER, end_line INTEGER, signature TEXT, return_type TEXT,"
        " is_exported BOOLEAN DEFAULT 0, is_test BOOLEAN DEFAULT 0,"
        " language TEXT, parent_id INTEGER);"
    )
    conn.execute(
        "INSERT INTO nodes (label, name, file_path, signature, is_test, language) "
        "VALUES ('Function', 'load_config', 'src/app.py', '(path)', 0, 'python')"
    )
    conn.commit()
    conn.close()


def _fresh_anchor_select():
    from groundtruth.pretask import anchor_select as a

    a._EMBED_CACHE.clear()
    a._SYMVEC_CACHE.clear()
    return a


def test_embed_memory_cache_is_model_keyed(tmp_path):
    """RED before the fix: _cache_key had NO model identity, so the gte call
    short-circuited into the e5 matrices (384-wide) from the in-memory cache."""
    a = _fresh_anchor_select()
    db = str(tmp_path / "graph.db")
    _mini_graph(db)

    e5 = _DimEmbedder(E5_MODEL, E5_DIM)
    gte = _DimEmbedder(DEFAULT_EMBED_MODEL, DEFAULT_EMBED_DIM)

    _, m_e5 = a._get_file_embeddings(db, str(tmp_path), e5)
    assert m_e5["src/app.py"].shape[1] == E5_DIM

    _, m_gte = a._get_file_embeddings(db, str(tmp_path), gte)
    assert m_gte["src/app.py"].shape[1] == DEFAULT_EMBED_DIM, (
        "model swap reused the OTHER model's cached matrices (model-blind cache key)"
    )


def test_embed_disk_cache_is_model_keyed(tmp_path):
    """A .embed_cache pkl written under the e5 identity must MISS under gte
    (the disk short-circuit was also keyed without model identity)."""
    a = _fresh_anchor_select()
    db = str(tmp_path / "graph.db")
    _mini_graph(db)

    _, m_e5 = a._get_file_embeddings(db, str(tmp_path), _DimEmbedder(E5_MODEL, E5_DIM))
    assert m_e5["src/app.py"].shape[1] == E5_DIM
    # Drop the memory cache so only the on-disk pkl could satisfy the next call.
    a._EMBED_CACHE.clear()

    _, m_gte = a._get_file_embeddings(
        db, str(tmp_path), _DimEmbedder(DEFAULT_EMBED_MODEL, DEFAULT_EMBED_DIM)
    )
    assert m_gte["src/app.py"].shape[1] == DEFAULT_EMBED_DIM, (
        "disk pkl written under e5 was consumed under gte (model-blind disk key)"
    )


def test_embed_disk_cache_width_mismatch_is_miss(tmp_path):
    """Defense-in-depth: a pkl under the CORRECT key whose matrices have the WRONG
    vector width (stale/corrupt) is treated as a MISS and recomputed."""
    a = _fresh_anchor_select()
    db = str(tmp_path / "graph.db")
    _mini_graph(db)

    key = a._cache_key(db, DEFAULT_EMBED_MODEL, DEFAULT_EMBED_DIM)
    cache_dir = Path(db).parent / ".embed_cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    forged = (["src/app.py"], {"src/app.py": np.zeros((1, E5_DIM), dtype=np.float32)})
    with open(cache_dir / f"{key}.pkl", "wb") as f:
        pickle.dump(forged, f)

    _, m = a._get_file_embeddings(
        db, str(tmp_path), _DimEmbedder(DEFAULT_EMBED_MODEL, DEFAULT_EMBED_DIM)
    )
    assert m["src/app.py"].shape[1] == DEFAULT_EMBED_DIM, (
        "wrong-width pkl matrices were consumed instead of being treated as a miss"
    )


# ---------------------------------------------------------------------------
# (6) The ST hole (P0 fix 2026-06-09): GT_REQUIRE_EMBEDDER=1 means the CONFIGURED
# model, full stop — sentence-transformers must NOT satisfy "required".
# ---------------------------------------------------------------------------


def _fake_st_module():
    import types

    fake = types.ModuleType("sentence_transformers")

    class _FakeST:
        def __init__(self, name):
            self.name = name

        def encode(self, texts, **kw):
            return np.zeros((len(list(texts)), 3), dtype=np.float32)

    fake.SentenceTransformer = _FakeST
    return fake, _FakeST


def test_require_embedder_skips_st_in_run_v74(monkeypatch):
    """RED before the fix: with GT_REQUIRE_EMBEDDER=1 (no GT_FORCE_ONNX),
    sentence-transformers loaded FIRST and satisfied "required" with an arbitrary
    host model. Now the ST step is skipped: configured-ONNX-or-raise."""
    import sys

    from groundtruth.pretask import v7_4_brief as b

    fake, _FakeST = _fake_st_module()
    monkeypatch.setitem(sys.modules, "sentence_transformers", fake)
    monkeypatch.setenv("GT_REQUIRE_EMBEDDER", "1")
    monkeypatch.delenv("GT_FORCE_ONNX_EMBEDDER", raising=False)
    monkeypatch.setattr(b, "_CACHED_MODEL", None)
    monkeypatch.setattr(b, "_SEMANTIC_AVAILABLE", None)

    got = None
    try:
        got = b._get_model()
    except RuntimeError as e:
        # Configured ONNX not baked in this checkout -> fail-loud is the contract.
        assert "GT_REQUIRE_EMBEDDER" in str(e)
    assert not isinstance(got, _FakeST), (
        "required run satisfied by an arbitrary host sentence-transformers model"
    )
    if got is not None:  # configured ONNX baked here -> must be the ONNX adapter
        assert type(got).__name__ == "_OnnxEmbedderAdapter"


def test_require_embedder_skips_st_in_localizer(monkeypatch):
    """Same hole, second half (graph_localizer._get_embedder) — both semantic
    halves must refuse the ST substitution under require (one surface)."""
    import sys

    from groundtruth.pretask import graph_localizer as gl

    fake, _FakeST = _fake_st_module()
    monkeypatch.setitem(sys.modules, "sentence_transformers", fake)
    monkeypatch.setenv("GT_REQUIRE_EMBEDDER", "1")
    monkeypatch.delenv("GT_FORCE_ONNX_EMBEDDER", raising=False)
    monkeypatch.setattr(gl, "_EMBEDDER", None)
    monkeypatch.setattr(gl, "_EMBEDDER_TRIED", False)

    got = None
    try:
        got = gl._get_embedder()
    except RuntimeError as e:
        assert "GT_REQUIRE_EMBEDDER" in str(e)
    assert not isinstance(got, _FakeST), (
        "required localize satisfied by an arbitrary host sentence-transformers model"
    )
    if got is not None:
        assert type(got).__name__ == "_OnnxEmbedderAdapter"


def test_st_still_available_when_require_off(monkeypatch):
    """No-regression: with the flag OFF, the ST step still loads first (graceful
    dev-path behavior unchanged)."""
    import sys

    from groundtruth.pretask import v7_4_brief as b

    fake, _FakeST = _fake_st_module()
    monkeypatch.setitem(sys.modules, "sentence_transformers", fake)
    monkeypatch.delenv("GT_REQUIRE_EMBEDDER", raising=False)
    monkeypatch.delenv("GT_FORCE_ONNX_EMBEDDER", raising=False)
    monkeypatch.setattr(b, "_CACHED_MODEL", None)
    monkeypatch.setattr(b, "_SEMANTIC_AVAILABLE", None)

    got = b._get_model()
    assert isinstance(got, _FakeST)


# ---------------------------------------------------------------------------
# LIVE model-load validation — RUN-WHEN-PRESENT (skips if model not baked)
# ---------------------------------------------------------------------------

def _model_baked(model_name: str) -> bool:
    try:
        m = EmbeddingModel(model_name, 1)
        d = m.model_dir
        return (d / "model.onnx").exists() and (d / "tokenizer.json").exists()
    except Exception:
        return False


def _cos(x, y):
    d = sum(i * j for i, j in zip(x, y))
    nx = math.sqrt(sum(i * i for i in x))
    ny = math.sqrt(sum(i * i for i in y))
    return d / (nx * ny) if nx and ny else 0.0


@pytest.mark.skipif(
    not _model_baked(DEFAULT_EMBED_MODEL),
    reason="gte-modernbert-base ONNX not baked — run scripts/setup_models.py (live load pending fetch)",
)
def test_live_gte_loads_without_token_type_error():
    m = get_embedding_model(DEFAULT_EMBED_MODEL, DEFAULT_EMBED_DIM)
    m._ensure_loaded()  # would raise if onnxruntime fed a non-declared input
    assert "token_type_ids" not in m._input_names
    assert set(m._input_names) >= {"input_ids", "attention_mask"}
    vec = m.embed("def add(a, b): return a + b", is_query=False)
    assert len(vec) == 768


@pytest.mark.skipif(
    not _model_baked(DEFAULT_EMBED_MODEL),
    reason="gte-modernbert-base ONNX not baked — run scripts/setup_models.py",
)
def test_live_gte_discriminates_related_over_unrelated():
    m = get_embedding_model(DEFAULT_EMBED_MODEL, DEFAULT_EMBED_DIM)
    q = m.embed("parse configuration settings from a file on disk", is_query=True)
    related = m.embed("def load_config(path): return read_yaml_file(path)", is_query=False)
    unrelated = m.embed("compute the determinant of a square matrix via LU", is_query=False)
    assert _cos(q, related) > _cos(q, unrelated)


@pytest.mark.skipif(
    not _model_baked(E5_MODEL),
    reason="e5-small-v2 ONNX not baked — run scripts/setup_models.py",
)
def test_live_e5_fallback_loads_with_token_type_ids():
    m = get_embedding_model(E5_MODEL, E5_DIM)
    m._ensure_loaded()
    assert "token_type_ids" in m._input_names
    vec = m.embed("read config from disk", is_query=True)
    assert len(vec) == 384
