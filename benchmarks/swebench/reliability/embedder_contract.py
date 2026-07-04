"""EMBEDDER contract — the ONNX e5 surface is real, baked, and discriminative.

Verifies (read-only) the same things GATE 3a asserts plus the no-download /
baked-model facts: the embedder loads (not _ZeroEmbeddingModel), separates
related>unrelated cosine, both halves are forced onto the identical ONNX surface
(GT_FORCE_ONNX_EMBEDDER), and the model is on disk under GT_MODELS_ROOT (no
runtime HuggingFace download). It does NOT change anything.
"""
from __future__ import annotations

import math
import os


def _cos(x, y) -> float:
    d = sum(i * j for i, j in zip(x, y))
    nx = math.sqrt(sum(i * i for i in x))
    ny = math.sqrt(sum(i * i for i in y))
    return d / (nx * ny) if nx and ny else 0.0


def build_embedder_contract() -> dict:
    c: dict = {
        "contract": "embedder",
        "GT_FORCE_ONNX_EMBEDDER": os.environ.get("GT_FORCE_ONNX_EMBEDDER", ""),
        "GT_REQUIRE_EMBEDDER": os.environ.get("GT_REQUIRE_EMBEDDER", ""),
        "GT_MODELS_ROOT": os.environ.get("GT_MODELS_ROOT", ""),
    }

    # model files present on disk (baked) -> no runtime download required
    root = os.environ.get("GT_MODELS_ROOT", "")
    onnx = os.path.join(root, "e5-small-v2", "model.onnx") if root else ""
    tok = os.path.join(root, "e5-small-v2", "tokenizer.json") if root else ""
    c["model_onnx_path"] = onnx
    c["model_files_exist"] = bool(onnx and os.path.exists(onnx) and tok and os.path.exists(tok))

    # load + probe the embedder (same shape as foundational_gates GATE 3a)
    try:
        from groundtruth.memory.enrich.embed import get_embedding_model

        m = get_embedding_model()
        cls = type(m).__name__
        c["embedder_class"] = cls
        c["is_zero_embedding_model"] = "Zero" in cls

        def emb(t, q):
            return list(m.embed_batch([t], is_query=q)[0])

        a = emb("read configuration from a file", True)
        rel = emb("parse config settings from disk", False)
        unrel = emb("compute the determinant of a matrix", False)
        sim, dis = _cos(a, rel), _cos(a, unrel)
        c["finite_nonzero_vector"] = bool(a) and all(math.isfinite(v) for v in a) and any(v != 0.0 for v in a)
        c["cos_related"] = round(sim, 8)
        c["cos_unrelated"] = round(dis, 8)
        c["discriminates"] = (not c["is_zero_embedding_model"]) and sim > dis and sim > 0.0
        c["loaded"] = True
    except Exception as e:
        c["loaded"] = False
        c["embedder_class"] = None
        c["load_error"] = str(e)
        c["discriminates"] = False
        c["is_zero_embedding_model"] = None

    # network download is NOT attempted when the model is baked + ONNX is forced.
    # We cannot prove a negative from inside, but we record the preconditions that
    # make a download impossible: forced ONNX + model files present.
    c["network_download_impossible"] = bool(
        c.get("GT_FORCE_ONNX_EMBEDDER") == "1" and c.get("model_files_exist"))

    hf: list[str] = []
    if not c.get("loaded"):
        hf.append("embedder_load_failed")
    elif c.get("is_zero_embedding_model"):
        hf.append("zero_embedding_model")
    elif not c.get("discriminates"):
        hf.append("embedder_not_discriminative")
    if not c.get("model_files_exist"):
        hf.append("model_not_baked")
    c["hard_fail"] = hf
    return c


if __name__ == "__main__":
    import json

    print(json.dumps(build_embedder_contract(), indent=2, default=str))
