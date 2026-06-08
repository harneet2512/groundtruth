#!/usr/bin/env bash
# =============================================================================
# gt-substrate — run GT's ENTIRE substrate against a repo, using only the bundled
# /opt/gt closure, IN whatever container this is invoked in (i.e. the eval image).
# Because gt-index is static, the Python is self-contained, and pyright ships with
# its own Node, none of this depends on the eval image's interpreter or libs — and
# because it runs where the repo's source + installed deps already are, the LSP can
# resolve real imports and the embedder/gates see the real files. One environment.
#
#   index -> resolve(LSP) -> 3-GATE verdict (embedder runs here)  [+ optional brief]
#
# Usage: gt-substrate <repo_root> <issue_file> [out_dir]
#   exit 0  => all three substrate gates GREEN (substrate present + consumed)
#   exit 1  => a gate is OFF (fail-closed) — printed, never silently skipped
# =============================================================================
set -euo pipefail

REPO="${1:?usage: gt-substrate <repo_root> <issue_file> [out_dir]}"
ISSUE="${2:-/dev/null}"
OUT="${3:-/tmp/gt}"
mkdir -p "$OUT"

export GT_HOME=/opt/gt
export GT_PYTHON=/opt/gt/python/bin/python3
export GT_INDEX_BINARY=/opt/gt/bin/gt-index
export GT_MODELS_ROOT=/opt/gt/models
export GT_FORCE_ONNX_EMBEDDER=1
export GT_REQUIRE_FTS5=1
export PYTHONPATH="/opt/gt/src:/opt/gt/scripts/swebench:/opt/gt/benchmarks/swebench:${PYTHONPATH:-}"
# pyright (bundled Node) must be discoverable by resolve.py's LSP dispatch
export PATH="/opt/gt/bin:/opt/gt/node/bin:/opt/gt/python/bin:${PATH}"

echo "=== gt-substrate: closure self-check (must all be present) ==="
"$GT_INDEX_BINARY" -h >/dev/null 2>&1 && echo "  gt-index: ok (static)"
"$GT_PYTHON" -c "import onnxruntime, numpy, pydantic, tokenizers" && echo "  py-deps: ok"
pyright --version >/dev/null 2>&1 && echo "  pyright: $(pyright --version)"
test -s "$GT_MODELS_ROOT/e5-small-v2/model.onnx" && echo "  e5 model: ok"

echo "=== (1) index -> graph.db (FTS5 enforced) ==="
"$GT_INDEX_BINARY" -root "$REPO" -output "$OUT/graph.db"

echo "=== (2) LSP resolve (bundled pyright) -> enrich + LSP_METRICS contract ==="
# Demand-driven scope (issue-relevant files) is computed by resolve.py; pyright runs
# from /opt/gt/node so external repo imports resolve where the repo's deps are present.
"$GT_PYTHON" -m groundtruth.resolve \
    --db "$OUT/graph.db" --root "$REPO" --resolve --lang python \
    2>&1 | tee "$OUT/gt_lsp_metrics.txt" | grep -aE 'LSP_METRICS' || true

echo "=== (3) 3-GATE VERDICT (fail-closed) — resolution / LSP / embedder ==="
# Embedder + gates run HERE, with the repo source present -> no host/image split.
GT_LSP_METRICS_FILE="$OUT/gt_lsp_metrics.txt" \
GT_GATES_DEEP_JSON="$OUT/gt_gates_deep.json" \
"$GT_PYTHON" /opt/gt/scripts/metrics/foundational_gates.py \
    "$OUT/graph.db" "$REPO" "$ISSUE" "$OUT/gt_lsp_metrics.txt"
RC=$?

echo "=== gt-substrate done (rc=$RC); artifacts in $OUT ==="
exit $RC
