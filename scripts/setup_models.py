"""Fetch the ONNX embedder files that EmbeddingModel (groundtruth.memory.enrich.embed)
expects, so the semantic localization ranker works WITHOUT torch (container-viable).

Writes models/e5-small-v2/{model.onnx, tokenizer.json}. Run once at image-build time
(baked into ghcr.io/.../gt-eval-runner) so it is present OFFLINE — DeepSWE task
containers have allow_internet=false, and per-run downloads are slow/flaky.

Source: the Xenova transformers.js ONNX port of intfloat/e5-small-v2 (a no-torch ONNX
export). 384-dim, ~90MB. Idempotent: skips files already present.

    python scripts/setup_models.py            # fetch e5-small-v2
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

_MODELS = Path(__file__).resolve().parent.parent / "models"

# (dest_filename, hf_repo, hf_path) — first repo/path that resolves wins per file.
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


def main() -> int:
    dest = _MODELS / "e5-small-v2"
    print(f"Fetching e5-small-v2 ONNX -> {dest}")
    ok = _fetch(dest, _E5_SMALL_V2)
    if ok:
        print("Embedder model ready. Semantic localization will run (no torch).")
        return 0
    print("WARN: embedder model incomplete -> semantic ranker stays OFF (deterministic "
          "2-signal fallback). Not a hard failure; install/network issue.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
