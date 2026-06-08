#!/usr/bin/env bash
# =============================================================================
# gt-substrate — run GT's ENTIRE substrate against a repo, IN this container, using
# only the bundled /opt/gt closure. Because gt-index is static, the Python is self-
# contained, and pyright ships its own Node, none of this depends on the eval image's
# interpreter/libs — and because it runs where the repo's source + installed deps are,
# the LSP resolves real imports and the embedder/gates see real files. ONE environment,
# no host/image split (the thing that starved conan's embedder), no per-job pip-install.
#
#   index -> demand-scope -> resolve(LSP) -> 3-GATE verdict (embedder runs here)
#
# Usage: gt-substrate <repo_root> <issue_file> [out_dir]
#   exit 0 => all three substrate gates GREEN ; exit 1 => a gate is OFF (fail-closed)
# =============================================================================
set -uo pipefail

REPO="${1:?usage: gt-substrate <repo_root> <issue_file> [out_dir]}"
ISSUE="${2:-/dev/null}"
OUT="${3:-/tmp/gt}"
mkdir -p "$OUT"

export GT_HOME=/opt/gt
export GT_MODELS_ROOT=/opt/gt/models
export GT_FORCE_ONNX_EMBEDDER=1
export GT_REQUIRE_FTS5=1
# PROOF/BENCHMARK MODE is the DEFAULT for the substrate: this script IS the single
# full-runtime boundary (index->scope->resolve(LSP)->closure->gates, all in-container),
# so it ARMS all 8 fail-closed flags itself instead of trusting a caller to (finding B4).
# Each defaults ON but a caller may override to 0 for local debugging. With GT_PROOF_MODE=1
# the runtime preflight (step 0) and every wired guard (FTS5-native, closure-after-LSP,
# semantic-consumed) fail-close on any degraded dimension instead of silently warning.
export GT_PROOF_MODE="${GT_PROOF_MODE:-1}"
export GT_CONTAINERIZED="${GT_CONTAINERIZED:-1}"
export GT_REQUIRE_EMBEDDER="${GT_REQUIRE_EMBEDDER:-1}"
export GT_REQUIRE_LSP="${GT_REQUIRE_LSP:-1}"
export GT_REQUIRE_FULL_STACK="${GT_REQUIRE_FULL_STACK:-1}"
export GT_FORBID_PREBUILT_GRAPH="${GT_FORBID_PREBUILT_GRAPH:-1}"
# Reliability audit: the brief/gate write per-stage candidate snapshots here
# (read-only; no ranking effect). The contract emitter reads them after the gates.
export GT_AUDIT_DIR="$OUT/audit"
# Canonical runtime paths the GTRuntimeContext + every component resolves from.
export GT_SOURCE_ROOT="$REPO"
export GT_GRAPH_DB="$OUT/graph.db"
mkdir -p "$OUT/audit" "$OUT/contracts"
export PYTHONPATH="/opt/gt/src:/opt/gt/scripts:/opt/gt/scripts/swebench:/opt/gt/benchmarks/swebench:${PYTHONPATH:-}"
export PATH="/opt/gt/bin:/opt/gt/node/bin:/opt/gt/python/bin:${PATH}"
PY=/opt/gt/python/bin/python3

# Always emit the reliability contracts on exit — success OR fail-fast — so the probe
# can classify EVERY task (even one that aborts at preflight/index) from its contracts.
_emit_contracts() {
  echo "=== reliability contracts (emitted on exit) ==="
  "$PY" -m reliability.emit_incontainer --task-id "${GT_TASK_ID:-unknown}" \
    --repo-root "$REPO" --graph-db "$OUT/graph.db" --lsp-metrics "$OUT/lsp_metrics.txt" \
    --snapshot-dir "$OUT/audit" --out "$OUT/contracts" 2>&1 | tail -4 || true
}
trap _emit_contracts EXIT

echo "=== gt-substrate closure self-check ==="
gt-index -h >/dev/null 2>&1 && echo "  gt-index: ok (static)"
"$PY" -c "import onnxruntime, numpy, pydantic, tokenizers" && echo "  py-deps: ok"
pyright --version >/dev/null 2>&1 && echo "  pyright: $(pyright --version 2>/dev/null)"
test -s "$GT_MODELS_ROOT/e5-small-v2/model.onnx" && echo "  e5: ok"

echo "=== (0) RUNTIME PREFLIGHT — fail-fast (proof mode) BEFORE any benchmark work ==="
# Single GTRuntimeContext contract: inside container, import under /opt/gt, baked
# model, real (non-zero, discriminative) ONNX embedder, LSP server present, all 8
# proof flags set. In GT_PROOF_MODE=1 a failure aborts before indexing.
"$PY" -m groundtruth.runtime.preflight --mode runtime --source "$REPO" \
    --audit "$OUT/audit" --out "$OUT/contracts/runtime_preflight.json"
PRE_RC=$?
[ "$PRE_RC" -eq 0 ] || { echo "FATAL: runtime preflight failed (proof mode) rc=$PRE_RC"; exit "$PRE_RC"; }

echo "=== (1) index -> graph.db (FTS5 enforced) ==="
gt-index -root "$REPO" -output "$OUT/graph.db" 2>&1 | tail -3
IDX_RC=${PIPESTATUS[0]}
[ "$IDX_RC" -eq 0 ] || { echo "FATAL: gt-index failed (rc=$IDX_RC)"; exit 1; }

echo "=== (2) demand-driven scope (multi-signal, any-language, never-empty) ==="
# Same proven seed as Point-A: union of (a) any-extension file refs in the issue mapped
# to on-disk source, (b) reused issue-anchor files, (c) a bounded BFS over graph.db.
# Parametrized via env so it runs against the IN-CONTAINER repo root (GT_SRC=$REPO).
GT_SRC="$REPO" GT_DB="$OUT/graph.db" GT_ISSUE="$ISSUE" GT_SCOPE_OUT="$OUT/scope.txt" "$PY" - <<'PY' || true
import re, os, json, sqlite3
SRC = os.environ["GT_SRC"]; DB = os.environ["GT_DB"]
ISSUE = os.environ.get("GT_ISSUE", ""); OUTF = os.environ["GT_SCOPE_OUT"]
issue = open(ISSUE, encoding="utf-8", errors="ignore").read() if ISSUE and os.path.exists(ISSUE) else ""

def rel(p):
    try:
        r = os.path.relpath(p, SRC) if os.path.isabs(p) or p.startswith(SRC) else p
    except ValueError:
        r = p
    return r.replace("\\", "/").lstrip("./")

basemap, rel_set = {}, set()
for root, _, fs in os.walk(SRC):
    if "/.git" in root:
        continue
    for f in fs:
        rp = rel(os.path.join(root, f)); rel_set.add(rp); basemap.setdefault(f, set()).add(rp)

scoped, syms = set(), []
for tok in re.findall(r'[\w./\\-]+\.[A-Za-z0-9_]+', issue):
    tok = tok.replace("\\", "/").strip("`'\"()[],"); rtok = rel(tok)
    if rtok in rel_set: scoped.add(rtok)
    b = os.path.basename(tok)
    if b in basemap: scoped.update(basemap[b])
try:
    if os.path.exists("/tmp/gt_issue_anchors.json"):
        anch = json.loads(open("/tmp/gt_issue_anchors.json", encoding="utf-8").read() or "{}")
        for p in (anch.get("paths") or []):
            rp = rel(p)
            if rp in rel_set: scoped.add(rp)
            elif os.path.basename(p) in basemap: scoped.update(basemap[os.path.basename(p)])
        syms = [s for s in (anch.get("symbols") or []) if s][:40]
except Exception:
    syms = []
try:
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    terms = set(syms)
    for t in re.findall(r'[A-Za-z_][A-Za-z0-9_]{3,}', issue)[:60]: terms.add(t)
    seed_files, seed_ids = set(), []
    for t in list(terms)[:80]:
        for (fp, nid) in conn.execute("SELECT file_path, id FROM nodes WHERE name = ? LIMIT 8", (t,)).fetchall():
            if fp: seed_files.add(rel(fp))
            seed_ids.append(nid)
    for nid in seed_ids[:200]:
        for (fp,) in conn.execute(
            "SELECT n.file_path FROM edges e JOIN nodes n ON e.target_id=n.id "
            "WHERE e.source_id = ? AND e.type='CALLS' LIMIT 8", (nid,)).fetchall():
            if fp: seed_files.add(rel(fp))
    conn.close()
    scoped.update(f for f in seed_files if f in rel_set or basemap.get(os.path.basename(f)))
except Exception:
    pass
scoped = sorted(p for p in scoped if p)
open(OUTF, "w").write("\n".join(scoped))
print(f"  demand scope: {len(scoped)} issue-relevant source files")
PY
SCOPE_ARG=""; [ -s "$OUT/scope.txt" ] && SCOPE_ARG="--source-files $OUT/scope.txt"

echo "=== (3) LSP resolve (bundled pyright, repo deps present in-container) ==="
"$PY" -m groundtruth.resolve --db "$OUT/graph.db" --root "$REPO" --resolve --lang python $SCOPE_ARG \
    2>&1 | tee "$OUT/lsp_metrics.txt" | grep -aE 'LSP_METRICS' || true
LSP_RC=${PIPESTATUS[0]}
[ "$LSP_RC" -eq 0 ] || echo "WARN: resolve rc=$LSP_RC (GATE 2 will judge from the contract line)"

echo "=== (3.5) GRAPH PREFLIGHT census (reuse preflight_pipeline; recorded, non-fatal) ==="
# Records the full-stack dimension census (graph/fts5/edge_quality/data_flow/lsp/embedder/
# prebuilt) into a contract; the authoritative fail-closed verdict is the 3-GATE step below.
GT_LSP_METRICS_FILE="$OUT/lsp_metrics.txt" "$PY" -m groundtruth.runtime.preflight \
    --mode graph --source "$REPO" --graph-db "$OUT/graph.db" --census \
    --out "$OUT/contracts/runtime_preflight_graph.json" 2>&1 | tail -3 || true

echo "=== (4) 3-GATE VERDICT (fail-closed) — resolution / LSP / embedder ==="
GT_LSP_METRICS_FILE="$OUT/lsp_metrics.txt" \
GT_GATES_DEEP_JSON="$OUT/gt_gates_deep.json" \
"$PY" /opt/gt/scripts/metrics/foundational_gates.py \
    "$OUT/graph.db" "$REPO" "$ISSUE" "$OUT/lsp_metrics.txt"
RC=$?

echo "=== gt-substrate done (gate rc=$RC); reliability contracts emitted on exit (trap) ==="
exit $RC
