"""Configurable ONNX E5 embedding pipeline."""

from __future__ import annotations

import hashlib
import os
import time
import threading
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np

if TYPE_CHECKING:
    import onnxruntime as ort
    from tokenizers import Tokenizer

# GT_MODELS_ROOT lets a baked image point the embedder at its pre-fetched models
# (e.g. /opt/groundtruth/models) even when GT itself is pip-installed from a different
# checkout — so a `container:` job doesn't re-download the e5 model. Falls back to the
# repo-relative models/ dir when unset.
_MODELS_ROOT = (
    Path(os.environ["GT_MODELS_ROOT"])
    if os.environ.get("GT_MODELS_ROOT")
    else Path(__file__).parent.parent.parent.parent.parent / "models"
)


class EmbeddingModel:
    """Lazy-loading ONNX embedding model."""

    def __init__(self, model_name: str, dim: int, prefix_query: str = "query: ", prefix_passage: str = "passage: ") -> None:
        self.model_name = model_name
        self.dim = dim
        self.prefix_query = prefix_query
        self.prefix_passage = prefix_passage
        self._session: "ort.InferenceSession | None" = None
        self._tokenizer: "Tokenizer | None" = None
        self._last_used = 0.0
        self._lock = threading.Lock()

    @property
    def model_dir(self) -> Path:
        return _MODELS_ROOT / self.model_name.split("/")[-1]

    def _ensure_loaded(self) -> tuple["ort.InferenceSession", "Tokenizer"]:
        with self._lock:
            if self._session is not None and self._tokenizer is not None:
                self._last_used = time.monotonic()
                return self._session, self._tokenizer

            import onnxruntime as ort_mod
            from tokenizers import Tokenizer as Tok

            onnx_path = self.model_dir / "model.onnx"
            tok_path = self.model_dir / "tokenizer.json"
            if not onnx_path.exists():
                raise FileNotFoundError(f"ONNX model not found at {onnx_path}. Run: python scripts/setup_models.py")

            tokenizer = Tok.from_file(str(tok_path))
            tokenizer.enable_padding(length=128)
            tokenizer.enable_truncation(max_length=128)

            self._tokenizer = tokenizer
            self._session = ort_mod.InferenceSession(str(onnx_path), providers=["CPUExecutionProvider"])
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
        session, tokenizer = self._ensure_loaded()
        encoded = tokenizer.encode_batch(texts)
        input_ids = np.array([e.ids for e in encoded], dtype=np.int64)
        attention_mask = np.array([e.attention_mask for e in encoded], dtype=np.int64)
        token_type_ids = np.zeros_like(input_ids, dtype=np.int64)

        outputs = session.run(
            None,
            {
                "input_ids": input_ids,
                "attention_mask": attention_mask,
                "token_type_ids": token_type_ids,
            },
        )
        token_embeddings = outputs[0]
        mask_expanded = np.expand_dims(attention_mask, axis=-1).astype(np.float32)
        pooled = np.sum(token_embeddings * mask_expanded, axis=1) / np.clip(mask_expanded.sum(axis=1), 1e-9, None)
        normalized = pooled / np.clip(np.linalg.norm(pooled, axis=1, keepdims=True), 1e-12, None)
        return [vec.tolist() for vec in normalized]

    def embed(self, text: str, is_query: bool = False) -> list[float]:
        prefix = self.prefix_query if is_query else self.prefix_passage
        return self._embed_prefixed([f"{prefix}{text}"])[0]

    def embed_batch(self, texts: list[str], is_query: bool = False) -> list[list[float]]:
        prefix = self.prefix_query if is_query else self.prefix_passage
        return self._embed_prefixed([f"{prefix}{text}" for text in texts])


_models: dict[tuple[str, int], EmbeddingModel] = {}


def get_embedding_model(model_name: str = "intfloat/e5-small-v2", dim: int = 384) -> EmbeddingModel:
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
