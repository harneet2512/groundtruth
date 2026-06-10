"""Configurable ONNX embedding pipeline (code-tuned default, e5 fallback).

The PRETASK-LOCALIZATION embedder identity is single-sourced here: ``get_embedding_model``
defaults to ``GT_EMBED_MODEL_NAME`` / ``GT_EMBED_DIM`` (and, when both are unset, to the
code-tuned ``DEFAULT_EMBED_MODEL`` / ``DEFAULT_EMBED_DIM`` below). The sqlite-vec MEMORY
store is a SEPARATE subsystem pinned to e5/384 via ``MemoryConfig`` — it always passes its
own explicit ``model_name``/``dim`` to ``embed_query``/``embed_passage``/``insert_embedding``,
so flipping the localization default here never migrates the memory vectors.
"""

from __future__ import annotations

import hashlib
import os
import time
import threading
from collections import OrderedDict
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np

if TYPE_CHECKING:
    import onnxruntime as ort
    from tokenizers import Tokenizer

# GT_MODELS_ROOT lets a baked image point the embedder at its pre-fetched models
# (e.g. /opt/groundtruth/models) even when GT itself is pip-installed from a different
# checkout — so a `container:` job doesn't re-download the model. Falls back to the
# repo-relative models/ dir when unset.
_MODELS_ROOT = (
    Path(os.environ["GT_MODELS_ROOT"])
    if os.environ.get("GT_MODELS_ROOT")
    else Path(__file__).parent.parent.parent.parent.parent / "models"
)

# ---------------------------------------------------------------------------
# Single-sourced PRETASK-LOCALIZATION embedder identity (CHANGE 2)
# ---------------------------------------------------------------------------
# The DeepSWE benchmark is polyglot (TS/Go/Rust/JS/Python; ~70% non-Python), so the
# localization embedder is code-tuned + multilingual, NOT general-text e5. Default to
# Alibaba-NLP/gte-modernbert-base (Apache-2.0, 149M, 768-dim, ONNX published incl int8;
# ModernBERT => NO token_type_ids input, CLS pooling, symmetric / no query-passage prefix).
# Both env vars override the default so an image can pin a different OPEN model with $0 / no
# API. The sqlite-vec memory store does NOT read these — it stays on e5/384 (MemoryConfig).
DEFAULT_EMBED_MODEL = "Alibaba-NLP/gte-modernbert-base"
DEFAULT_EMBED_DIM = 768

# e5 stays a first-class citizen as the runtime fallback (both baked during transition).
E5_MODEL = "intfloat/e5-small-v2"
E5_DIM = 384

# Models whose tokenizer/ONNX use e5-style query:/passage: prefixes AND mean pooling.
# Everything else (gte-modernbert, jina-code, ...) is symmetric: no prefix, CLS pooling.
_E5_FAMILY = {"intfloat/e5-small-v2", "intfloat/e5-base-v2", "intfloat/e5-large-v2"}


def _default_embed_model() -> str:
    return os.environ.get("GT_EMBED_MODEL_NAME") or DEFAULT_EMBED_MODEL


def _default_embed_dim() -> int:
    raw = os.environ.get("GT_EMBED_DIM")
    if raw:
        try:
            return int(raw)
        except (TypeError, ValueError):
            pass
    # When only the name is overridden, keep dim consistent with the known model.
    name = os.environ.get("GT_EMBED_MODEL_NAME")
    if name == E5_MODEL:
        return E5_DIM
    return DEFAULT_EMBED_DIM


class EmbeddingModel:
    """Lazy-loading ONNX embedding model (model-aware prefix + pooling + ONNX inputs).

    Two model families are supported by the SAME code path:
      * e5 family (mean pooling, ``query:``/``passage:`` prefixes, token_type_ids declared)
      * code-tuned / ModernBERT family (CLS pooling, NO prefix, NO token_type_ids input)
    The pooling and prefix behaviour are selected from ``model_name``; the ONNX input names
    are INTROSPECTED at load time so a model whose graph does not declare ``token_type_ids``
    (ModernBERT) is never fed one (the load-bearing fix for CHANGE 2)."""

    def __init__(
        self,
        model_name: str,
        dim: int,
        prefix_query: str | None = None,
        prefix_passage: str | None = None,
        pooling: str | None = None,
    ) -> None:
        self.model_name = model_name
        self.dim = dim
        _is_e5 = model_name in _E5_FAMILY
        # Prefix: e5 needs query:/passage:; everything else is symmetric (no prefix).
        self.prefix_query = prefix_query if prefix_query is not None else ("query: " if _is_e5 else "")
        self.prefix_passage = prefix_passage if prefix_passage is not None else ("passage: " if _is_e5 else "")
        # Pooling: e5 = mean (over attention mask); ModernBERT/gte/jina-code = CLS ([:,0]).
        self.pooling = pooling if pooling is not None else ("mean" if _is_e5 else "cls")
        self._session: "ort.InferenceSession | None" = None
        self._tokenizer: "Tokenizer | None" = None
        # The exact input names the loaded ONNX graph declares (introspected once).
        self._input_names: list[str] = []
        self._last_used = 0.0
        self._lock = threading.Lock()

    @property
    def model_dir(self) -> Path:
        return _MODELS_ROOT / self.model_name.split("/")[-1]

    def _resolve_onnx_path(self) -> Path:
        """Pick the ONNX file: model.onnx, else a quantized/int8 variant if that is all
        that was baked (setup_models writes model.onnx, but an int8-only image is valid)."""
        d = self.model_dir
        primary = d / "model.onnx"
        if primary.exists():
            return primary
        for alt in ("model_int8.onnx", "model_quantized.onnx", "model_uint8.onnx"):
            if (d / alt).exists():
                return d / alt
        return primary  # report the canonical name in the FileNotFoundError below

    def _ensure_loaded(self) -> tuple["ort.InferenceSession", "Tokenizer"]:
        with self._lock:
            if self._session is not None and self._tokenizer is not None:
                self._last_used = time.monotonic()
                return self._session, self._tokenizer

            import onnxruntime as ort_mod
            from tokenizers import Tokenizer as Tok

            onnx_path = self._resolve_onnx_path()
            tok_path = self.model_dir / "tokenizer.json"
            if not onnx_path.exists():
                raise FileNotFoundError(f"ONNX model not found at {onnx_path}. Run: python scripts/setup_models.py")

            tokenizer = Tok.from_file(str(tok_path))
            tokenizer.enable_padding(length=128)
            tokenizer.enable_truncation(max_length=128)

            self._tokenizer = tokenizer
            self._session = ort_mod.InferenceSession(str(onnx_path), providers=["CPUExecutionProvider"])
            # Introspect the declared inputs ONCE. ModernBERT's ONNX declares only
            # input_ids + attention_mask (no token_type_ids); e5's declares all three.
            # Feeding an input the graph does not declare raises in onnxruntime, so we
            # build the feed dict from THIS set, never unconditionally.
            self._input_names = [i.name for i in self._session.get_inputs()]
            self._last_used = time.monotonic()
            return self._session, self._tokenizer

    def unload(self) -> None:
        with self._lock:
            self._session = None
            self._tokenizer = None

    def maybe_unload(self, idle_seconds: int = 600) -> None:
        if self._session is not None and (time.monotonic() - self._last_used) > idle_seconds:
            self.unload()

    def _embed_prefixed(self, texts: list[str]) -> list[list[float]]:
        # Chunked encode: ONE session.run over N=thousands of passages allocates
        # N x seq x hidden x layers of activations (~1.8MB/passage measured) — a 4096-passage
        # budget = ~7.3GB anon-rss, OOM-killed in capped containers (proven live,
        # astropy-13236 repro 2026-06-10) and the killer of the 8-par VM sweep box.
        # Padding is FIXED at 128 (enable_padding(length=128)), so chunking is numerically
        # IDENTICAL to the single call — only peak memory changes (~60MB at B=32).
        out: list[list[float]] = []
        B = max(1, int(os.environ.get("GT_EMBED_ENCODE_BATCH", "32")))
        for i in range(0, len(texts), B):
            out.extend(self._embed_chunk(texts[i:i + B]))
        return out

    def _embed_chunk(self, texts: list[str]) -> list[list[float]]:
        session, tokenizer = self._ensure_loaded()
        encoded = tokenizer.encode_batch(texts)
        input_ids = np.array([e.ids for e in encoded], dtype=np.int64)
        attention_mask = np.array([e.attention_mask for e in encoded], dtype=np.int64)

        # Feed ONLY the inputs the ONNX graph declares. ModernBERT (gte/jina-code) has no
        # token_type_ids tensor — feeding one raises "Unexpected input"; e5 has all three.
        feed: dict[str, np.ndarray] = {}
        if "input_ids" in self._input_names:
            feed["input_ids"] = input_ids
        if "attention_mask" in self._input_names:
            feed["attention_mask"] = attention_mask
        if "token_type_ids" in self._input_names:
            feed["token_type_ids"] = np.zeros_like(input_ids, dtype=np.int64)
        # Some exports name it position_ids etc.; only token_type_ids is the known variant.

        outputs = session.run(None, feed)
        token_embeddings = outputs[0]  # (batch, seq, hidden)
        if self.pooling == "cls":
            # CLS pooling ([:, 0]) — the gte-modernbert / jina-code recipe.
            pooled = token_embeddings[:, 0]
        else:
            # Mean pooling over the attention mask — the e5 recipe.
            mask_expanded = np.expand_dims(attention_mask, axis=-1).astype(np.float32)
            pooled = np.sum(token_embeddings * mask_expanded, axis=1) / np.clip(
                mask_expanded.sum(axis=1), 1e-9, None
            )
        normalized = pooled / np.clip(np.linalg.norm(pooled, axis=1, keepdims=True), 1e-12, None)
        return [vec.tolist() for vec in normalized]

    def embed(self, text: str, is_query: bool = False) -> list[float]:
        prefix = self.prefix_query if is_query else self.prefix_passage
        return self._embed_prefixed([f"{prefix}{text}"])[0]

    def embed_batch(self, texts: list[str], is_query: bool = False) -> list[list[float]]:
        prefix = self.prefix_query if is_query else self.prefix_passage
        return self._embed_prefixed([f"{prefix}{text}" for text in texts])


_models: dict[tuple[str, int], EmbeddingModel] = {}


def get_embedding_model(model_name: str | None = None, dim: int | None = None) -> EmbeddingModel:
    """Return the cached EmbeddingModel for (model_name, dim).

    With NO args (the PRETASK-LOCALIZATION call sites: anchor_select, graph_localizer,
    v7_4_brief, proof/context self-checks) this resolves to the CHANGE-2 code-tuned default
    (``GT_EMBED_MODEL_NAME``/``GT_EMBED_DIM`` -> gte-modernbert-base / 768). The sqlite-vec
    MEMORY store NEVER calls this no-arg — it passes its own e5/384 via embed_query/passage —
    so the memory subsystem is unaffected by the localization default flip."""
    if model_name is None:
        model_name = _default_embed_model()
    if dim is None:
        dim = _default_embed_dim()
    key = (model_name, dim)
    if key not in _models:
        _models[key] = EmbeddingModel(model_name=model_name, dim=dim)
    return _models[key]


def unload() -> None:
    for model in _models.values():
        model.unload()


def maybe_unload(idle_seconds: int = 600) -> None:
    for model in _models.values():
        model.maybe_unload(idle_seconds)


def embed_query(text: str, model_name: str = "intfloat/e5-small-v2", dim: int = 384) -> list[float]:
    return get_embedding_model(model_name, dim).embed(text, is_query=True)


def embed_passage(text: str, model_name: str = "intfloat/e5-small-v2", dim: int = 384) -> list[float]:
    return get_embedding_model(model_name, dim).embed(text, is_query=False)


def embed_batch(passages: list[str], model_name: str = "intfloat/e5-small-v2", dim: int = 384) -> list[list[float]]:
    return get_embedding_model(model_name, dim).embed_batch(passages, is_query=False)


# ---------------------------------------------------------------------------
# Symbol-level semantic granularity (CHANGE 1)
# ---------------------------------------------------------------------------
#
# A file embedded as ONE vector from a concatenated symbol-bag averages the gold
# function into its 60 siblings, so sibling files in a licensed repo cluster at
# cosine 0.80-0.84 (measured mad=0.0145) and the semantic ranker cannot separate
# the file that actually contains the issue function. The fix is to embed each
# symbol as its own short passage and score a file by the MAX cosine over its
# symbols plus a small top-k mean. This is exactly the ColBERT MaxSim late-
# interaction (Khattab & Zaharia, SIGIR 2020) + passage-level MaxP aggregation
# (Dai & Callan, SIGIR 2019): a document is relevant if its single best passage
# matches the query, not if its average passage does. Pure functions here so both
# localization paths (anchor_select.semantic_top_k and
# graph_localizer._semantic_score_by_file) share ONE implementation and ONE cache
# key — no per-language or per-task logic.

# Per-symbol passage token cap (~80 tokens). The model truncates at 128 tokens
# (see _ensure_loaded), so 80 tokens of "name signature\n<body snippet>" stays
# inside the window while leaving headroom for the "passage: " prefix.
SYMBOL_PASSAGE_TOKEN_CAP = 80
# A token is ~4 chars for code identifiers; cap the passage CHARACTERS so the
# tokenizer rarely has to truncate. 80 tokens * ~5 chars/token ≈ 400 chars.
_SYMBOL_PASSAGE_CHAR_CAP = SYMBOL_PASSAGE_TOKEN_CAP * 5


def read_agg_params() -> tuple[float, int]:
    """Read the (alpha, top_k) aggregation parameters from the environment.

    ``GT_SEM_AGG_ALPHA`` (default 0.7) weights MAX vs top-k mean.
    ``GT_SEM_TOPK`` (default 3) bounds the mean term.
    Malformed values fall back to the defaults (correct-or-quiet: never crash a
    brief on a typo'd env var)."""
    alpha = 0.7
    top_k = 3
    raw_alpha = os.environ.get("GT_SEM_AGG_ALPHA")
    if raw_alpha is not None:
        try:
            alpha = float(raw_alpha)
        except (TypeError, ValueError):
            alpha = 0.7
    raw_k = os.environ.get("GT_SEM_TOPK")
    if raw_k is not None:
        try:
            top_k = int(raw_k)
        except (TypeError, ValueError):
            top_k = 3
    # Clamp to sane ranges. alpha in [0,1]; top_k >= 1.
    alpha = min(1.0, max(0.0, alpha))
    top_k = max(1, top_k)
    return alpha, top_k


def symbol_passage(name: str, signature: str, body_snippet: str = "") -> str:
    """Build the per-symbol passage ``"{name} {signature}\\n{body_snippet}"``.

    Returns ``""`` when name+signature+body are all blank (correct-or-quiet — a
    blank symbol is NEVER embedded). The result is character-capped to stay
    within the model's 128-token window after the e5 ``passage:`` prefix."""
    head = f"{(name or '').strip()} {(signature or '').strip()}".strip()
    body = (body_snippet or "").strip()
    if not head and not body:
        return ""
    passage = f"{head}\n{body}".strip() if body else head
    return passage[:_SYMBOL_PASSAGE_CHAR_CAP]


def passage_hash(passage: str, model_name: str, dim: int, version: str) -> str:
    """Content-addressed cache key for a single symbol vector.

    Keyed on (version, model, dim, passage_text) so a vector is reused across
    runs/graphs whenever the same passage is embedded by the same model — and is
    automatically invalidated when any of those change."""
    sig = f"{version}:{model_name}:{dim}:{passage}"
    return hashlib.sha256(sig.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Shared content-addressed passage-vector cache (encode-blowup fix 2026-06-09)
# ---------------------------------------------------------------------------
# Run 27249519544: 29/113 tasks SIGKILL (exit 137) during BRIEF generation because
# graph_localizer._semantic_score_by_file re-encoded EVERY witnessed candidate
# file's per-symbol passages — hundreds of files × ≤80 passages per task — with NO
# cache. gt_gt §11.2 prescribes "cache by node-content hash"; this is that cache,
# single-sourced HERE (per the module contract above: both semantic halves share
# ONE implementation and ONE cache key) so a file scored by BOTH halves
# (anchor_select.semantic_top_k and graph_localizer._semantic_score_by_file)
# within one task is encoded exactly once.

# The single version tag folded into every passage_hash. Bump when the passage
# CONTENT shape changes (it started life as anchor_select._SUMMARY_VERSION, which
# now aliases this constant so the two halves can never drift apart).
PASSAGE_CACHE_VERSION = "sym2-fn"


class _PassageVecCache(OrderedDict):
    """Bounded LRU dict mapping ``passage_hash`` -> float32 vector.

    Plain dict API (``in`` / ``[k]`` / ``.get`` / ``.clear``) so the existing
    anchor_select call sites work unchanged; inserts and reads refresh recency,
    and inserts evict the least-recently-used entries past ``maxsize`` — the
    unbounded growth of the old per-module dict is what compounded the OOM kills
    on big repos. ``maxsize <= 0`` disables eviction (explicit opt-out only)."""

    def __init__(self, maxsize: int) -> None:
        super().__init__()
        self.maxsize = int(maxsize)

    def __getitem__(self, key):
        value = super().__getitem__(key)
        self.move_to_end(key)
        return value

    def get(self, key, default=None):
        try:
            return self[key]
        except KeyError:
            return default

    def __setitem__(self, key, value) -> None:
        super().__setitem__(key, value)
        self.move_to_end(key)
        while self.maxsize > 0 and len(self) > self.maxsize:
            self.popitem(last=False)


def _passage_cache_max() -> int:
    """``GT_SYMVEC_CACHE_MAX``: max cached passage vectors (default 100_000 ≈
    300MB worst-case at 768-dim float32). Malformed values fall back to the
    default (correct-or-quiet: never crash a brief on a typo'd env var)."""
    raw = os.environ.get("GT_SYMVEC_CACHE_MAX")
    if raw:
        try:
            return int(raw)
        except (TypeError, ValueError):
            pass
    return 100_000


_PASSAGE_VEC_CACHE = _PassageVecCache(_passage_cache_max())


def model_identity(model: object) -> tuple[str, int]:
    """Best-effort (model_name, dim) for the passage cache key — shared by BOTH
    semantic halves so identical passages hash identically everywhere. Defaults
    to the CONFIGURED localization embedder identity when the adapter does not
    expose them, so the content-addressed key flips with the model and stale
    foreign-model vectors are never reused."""
    name = getattr(model, "model_name", None)
    if not name:
        inner = getattr(model, "_m", None)
        name = getattr(inner, "model_name", None)
    dim = getattr(model, "dim", None)
    if dim is None:
        inner = getattr(model, "_m", None)
        dim = getattr(inner, "dim", None)
    return (str(name or _default_embed_model()), int(dim or _default_embed_dim()))


def aggregate_symbol_cosines(cosines: list[float], *, alpha: float, top_k: int) -> float:
    """Aggregate per-symbol cosines into ONE file score in [0, 1].

    ``file_score = alpha * max_i(cos_i) + (1 - alpha) * mean(top_k cos_i)``,
    with ``k = min(top_k, n)``. ColBERT MaxSim (max term) + MaxP top-k mean.

    Inputs are cosines of UNIT vectors so each is in [-1, 1]; negative cosines
    are floored at 0 (correct-or-quiet: a symbol that points AWAY from the issue
    is no evidence, not negative evidence, and the score must stay commensurate
    with W_LEX/reach which live in [0, 1]). Empty input -> 0.0."""
    if not cosines:
        return 0.0
    clipped = [c if c > 0.0 else 0.0 for c in cosines]
    mx = max(clipped)
    k = min(top_k, len(clipped))
    topk = sorted(clipped, reverse=True)[:k]
    mean_topk = sum(topk) / len(topk) if topk else 0.0
    score = alpha * mx + (1.0 - alpha) * mean_topk
    # Numerically pin into [0,1] (rounding could nudge a max=1.0 cosine to 1+eps).
    return min(1.0, max(0.0, score))
