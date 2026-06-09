#!/usr/bin/env python3
"""Stage 3 — embedder-usage certificate classifier.

Proves the embedder is not merely AVAILABLE but CONSUMED by every semantic path that claims
semantic evidence. The certificate is assembled by ``proof.build_embedder_certificate`` (src,
called from run_v74) and written by ``proof.write_embedder_certificate``; this module reads it
and emits one verdict. No ranking-weight or task-specific logic.

The all-zero rule is STRICT: in proof mode with GT_REQUIRE_EMBEDDER, non-empty candidates that
render all-zero semantic components are EMBEDDER_USAGE_FAIL (or _DROPPED_SEMANTIC when upstream
scores existed) — never a broad escape hatch.
"""
from __future__ import annotations

import json
import os

# Tokens that mark a degraded loader in the embedder CLASS name.
_ZERO_TOKENS = ("Zero", "_ZeroEmbeddingModel")
_ST_TOKENS = ("SentenceTransformer", "MiniLM")


def _root_of(identity):
    """Extract the models_root from an identity dict or a 'root|class|dim|force' sig string."""
    if not identity:
        return ""
    if isinstance(identity, dict):
        return str(identity.get("models_root", "") or "")
    if isinstance(identity, str):
        return identity.split("|", 1)[0]
    return ""


# Minimum related-vs-unrelated cosine separation for a healthy e5 embedder. e5-small-v2 yields a
# margin well above this on the probe strings; a degenerate/constant embedder yields ~0.
_DISC_FLOOR = 0.02


def classify_embedder(cert, *, proof_mode: bool = False, require_embedder: bool = False):
    """Hard gates over the embedder certificate -> (verdict, ok).

    PASS: EMBEDDER_USAGE_VALID, EMBEDDER_USAGE_VALID_NOOP (no candidates / outside proof).
    FAIL: EMBEDDER_FAIL_NO_CERT, EMBEDDER_FAIL_ZERO_MODEL, EMBEDDER_FAIL_LOAD_ERROR,
          EMBEDDER_FAIL_ST_UNDER_FORCED_ONNX, EMBEDDER_FAIL_MODEL_ROOT_DIVERGENCE,
          EMBEDDER_FAIL_MODEL_DOWNLOAD, EMBEDDER_FAIL_DROPPED_SEMANTIC, EMBEDDER_USAGE_FAIL.
    """
    if not cert:
        return ("EMBEDDER_FAIL_NO_CERT", False)
    cls = str(cert.get("embedder_class", "") or "")
    if any(t in cls for t in _ZERO_TOKENS):
        return ("EMBEDDER_FAIL_ZERO_MODEL", False)
    if "load_error" in cls:
        return ("EMBEDDER_FAIL_LOAD_ERROR", False)
    force_onnx = str(cert.get("GT_FORCE_ONNX_EMBEDDER", "")) == "1"
    if force_onnx and any(t in cls for t in _ST_TOKENS):
        return ("EMBEDDER_FAIL_ST_UNDER_FORCED_ONNX", False)
    # model-root divergence across semantic paths (run_v74 / localize / v1r)
    roots = [r for r in (
        _root_of(cert.get("run_v74_embedder_identity")),
        _root_of(cert.get("localize_embedder_identity")),
        _root_of(cert.get("v1r_render_semantic_identity")),
    ) if r]
    if len(set(roots)) > 1:
        return ("EMBEDDER_FAIL_MODEL_ROOT_DIVERGENCE", False)
    if cert.get("model_download_attempted"):
        return ("EMBEDDER_FAIL_MODEL_DOWNLOAD", False)

    cand = int(cert.get("semantic_candidate_count", 0) or 0)
    rendered_nz = int(cert.get("rendered_semantic_nonzero_count", 0) or 0)
    upstream_nz = int(cert.get("upstream_semantic_nonzero_count", 0) or 0)

    # Discrimination floor: the gt-run-proof direct probe (empty-issue path) records
    # discrimination_margin = cos(related) - cos(unrelated). A non-positive / below-floor margin is a
    # degenerate (constant-vector) embedder -> FAIL even with no candidates. None (probe encode
    # failed) under proof+require is also a FAIL. Closes the "load-only" gap on the portable path.
    if "discrimination_margin" in cert:
        dm = cert.get("discrimination_margin", None)
        if dm is None:
            if proof_mode and require_embedder:
                return ("EMBEDDER_FAIL_NO_DISCRIMINATION", False)
        else:
            try:
                if float(dm) <= _DISC_FLOOR:
                    return ("EMBEDDER_FAIL_NO_DISCRIMINATION", False)
            except (TypeError, ValueError):
                if proof_mode and require_embedder:
                    return ("EMBEDDER_FAIL_NO_DISCRIMINATION", False)

    # Non-empty candidates that render all-zero semantic, under a required embedder in proof
    # mode, are a FAIL — no escape hatch. If upstream scores existed, they were DROPPED.
    if proof_mode and require_embedder and cand > 0 and rendered_nz == 0:
        if upstream_nz > 0:
            return ("EMBEDDER_FAIL_DROPPED_SEMANTIC", False)
        return ("EMBEDDER_USAGE_FAIL", False)

    # cand==0 (nothing to embed) or outside proof/require => valid (correct-or-quiet).
    if cand == 0:
        return ("EMBEDDER_USAGE_VALID_NOOP", True)
    return ("EMBEDDER_USAGE_VALID", True)


def load_embedder_cert(path=None):
    p = path or os.environ.get("GT_EMBEDDER_CERT", "/tmp/gt/embedder_certificate.json")
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def main():
    import argparse
    ap = argparse.ArgumentParser(description="Classify the Stage-3 embedder-usage certificate.")
    ap.add_argument("--cert", default=os.environ.get("GT_EMBEDDER_CERT", "/tmp/gt/embedder_certificate.json"))
    ap.add_argument("--proof-mode", action="store_true", default=os.environ.get("GT_PROOF_MODE") == "1")
    ap.add_argument("--require-embedder", action="store_true",
                    default=os.environ.get("GT_REQUIRE_EMBEDDER") == "1")
    a = ap.parse_args()
    cert = load_embedder_cert(a.cert)
    verdict, ok = classify_embedder(cert, proof_mode=a.proof_mode, require_embedder=a.require_embedder)
    c = cert or {}
    print(f"[EMBEDDER CERTIFICATE] {verdict} {'PASS' if ok else 'FAIL'} "
          f"class={c.get('embedder_class')} dim={c.get('embedder_dim')} "
          f"force_onnx={c.get('GT_FORCE_ONNX_EMBEDDER')} models_root={c.get('GT_MODELS_ROOT')} "
          f"candidates={c.get('semantic_candidate_count')} rendered_nz={c.get('rendered_semantic_nonzero_count')} "
          f"upstream_nz={c.get('upstream_semantic_nonzero_count')} w_sem={c.get('effective_w_sem')} "
          f"reason={c.get('all_zero_semantic_reason')!r}")
    return 0 if ok else 1


if __name__ == "__main__":
    import sys
    sys.exit(main())
