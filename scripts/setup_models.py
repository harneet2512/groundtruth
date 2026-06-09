"""Fetch the ONNX embedder files that EmbeddingModel (groundtruth.memory.enrich.embed)
expects, so the semantic localization ranker works WITHOUT torch (container-viable).

Run once at image-build time (baked into ghcr.io/.../gt-eval-runner) so the models are
present OFFLINE — DeepSWE task containers have allow_internet=false, and per-run downloads
are slow/flaky. Idempotent: skips files already present.

CHANGE 2 — the PRIMARY localization model is now the code-tuned, multilingual
``Alibaba-NLP/gte-modernbert-base`` (Apache-2.0, 768-dim, ModernBERT => NO token_type_ids).
We prefer its INT8/quantized ONNX (~150MB) over the fp32 (~600MB) to hold the image small.
``intfloat/e5-small-v2`` (MIT, 384-dim, ~90MB) is STILL fetched as the runtime fallback so
both are baked during the transition (the loader falls back to e5 if gte is absent).

    python scripts/setup_models.py            # fetch gte-modernbert-base + e5-small-v2
    python scripts/setup_models.py --e5-only  # fetch only the e5 fallback
    python scripts/setup_models.py --gte-only # fetch only the code-tuned primary

The dest dirname is the model's HF basename (``model_name.split('/')[-1]``), which is how
``EmbeddingModel.model_dir`` and ``proof.embedder_model_path`` resolve the baked files.
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

_MODELS = Path(__file__).resolve().parent.parent / "models"

# Code-tuned PRIMARY (CHANGE 2). gte-modernbert-base publishes a full ONNX suite in onnx/.
# Prefer quantized/int8 (~150MB) to keep the baked image small; fall back to fp32 model.onnx.
# The local dest is always "model.onnx" so the loader + proof cert find it by canonical name.
_GTE_MODERNBERT = {
    "model.onnx": [
        ("Alibaba-NLP/gte-modernbert-base", "onnx/model_int8.onnx"),
        ("Alibaba-NLP/gte-modernbert-base", "onnx/model_quantized.onnx"),
        ("Alibaba-NLP/gte-modernbert-base", "onnx/model_uint8.onnx"),
        ("Alibaba-NLP/gte-modernbert-base", "onnx/model.onnx"),
    ],
    "tokenizer.json": [
        ("Alibaba-NLP/gte-modernbert-base", "tokenizer.json"),
    ],
}

# Runtime FALLBACK (MIT). Xenova transformers.js no-torch ONNX port of intfloat/e5-small-v2.
_E5_SMALL_V2 = {
    "model.onnx": [
        ("Xenova/e5-small-v2", "onnx/model.onnx"),
        ("Xenova/e5-small-v2", "onnx/model_quantized.onnx"),
    ],
    "tokenizer.json": [
        ("Xenova/e5-small-v2", "tokenizer.json"),
        ("intfloat/e5-small-v2", "tokenizer.json"),
    ],
}


def _fetch(dest_dir: Path, spec: dict[str, list[tuple[str, str]]]) -> bool:
    from huggingface_hub import hf_hub_download

    dest_dir.mkdir(parents=True, exist_ok=True)
    ok = True
    for fname, candidates in spec.items():
        out = dest_dir / fname
        if out.exists() and out.stat().st_size > 0:
            print(f"  skip (present): {out}")
            continue
        got = False
        for repo, path in candidates:
            try:
                src = hf_hub_download(repo_id=repo, filename=path)
                shutil.copyfile(src, out)
                mb = out.stat().st_size // (1024 * 1024)
                print(f"  OK: {out} <- {repo}/{path} ({mb}MB)")
                got = True
                break
            except Exception as e:  # try next candidate
                print(f"  miss {repo}/{path}: {str(e)[:80]}")
        if not got:
            print(f"  FAILED to fetch {fname} from any source")
            ok = False
    return ok


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    e5_only = "--e5-only" in args
    gte_only = "--gte-only" in args

    targets: list[tuple[str, dict]] = []
    if not e5_only:
        targets.append(("gte-modernbert-base", _GTE_MODERNBERT))
    if not gte_only:
        targets.append(("e5-small-v2", _E5_SMALL_V2))

    all_ok = True
    primary_ok = True  # the FIRST target is the one that drives the verdict
    for i, (dirname, spec) in enumerate(targets):
        dest = _MODELS / dirname
        print(f"Fetching {dirname} ONNX -> {dest}")
        ok = _fetch(dest, spec)
        all_ok = all_ok and ok
        if i == 0:
            primary_ok = ok

    if primary_ok:
        print("Primary embedder model ready. Semantic localization will run (no torch).")
        # A baked e5 fallback that failed is non-fatal (loader still uses the primary).
        if not all_ok:
            print("NOTE: a fallback model was incomplete (loader still uses the primary).",
                  file=sys.stderr)
        return 0
    print("WARN: primary embedder model incomplete. The loader will fall back to e5 if "
          "baked, else the semantic ranker stays OFF (deterministic 2-signal fallback). "
          "Not a hard failure; install/network issue.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
