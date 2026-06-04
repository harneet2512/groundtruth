"""Configurable ONNX E5 embedding pipeline."""

from __future__ import annotations

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
