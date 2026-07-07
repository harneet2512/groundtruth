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
# LSP servers resolve.py can spawn (config.py::LSP_SERVERS): py/ts/go/rust/java. Each is
# a soft check (echo present/MISSING) — the authoritative fail-closed verdict for the
# dominant-language LSP is GATE 2 below; a non-dominant server merely being absent must
# not abort the whole substrate (correct-or-quiet). Probed by the exact spawned binary.
for _srv in pyright typescript-language-server gopls rust-analyzer jdtls; do
  if command -v "$_srv" >/dev/null 2>&1; then echo "  lsp[$_srv]: ok"; else echo "  lsp[$_srv]: MISSING"; fi
done
# Embedder: the loader DEFAULT is gte-modernbert-base; e5 is the runtime fallback. Report
# both; the configured default is what proof.embedder_model_path / the gates assert.
test -s "$GT_MODELS_ROOT/gte-modernbert-base/model.onnx" && echo "  embedder[gte-modernbert-base]: ok" || echo "  embedder[gte-modernbert-base]: MISSING"
test -s "$GT_MODELS_ROOT/e5-small-v2/model.onnx" && echo "  embedder[e5-small-v2 fallback]: ok" || echo "  embedder[e5-small-v2 fallback]: MISSING"

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

# SCOPE = the bug's GRAPH-REACHABLE NEIGHBORHOOD, derived from code STRUCTURE — not from
# how many words the issue happens to share with file/symbol names. The old heuristic
# matched every 4+-char issue word to node NAMES, which swung the scope from 1 file
# (flexget: specific words matched little -> type-checker STARVED) to 1158 (checkov:
# common words like "check" matched thousands -> type-checker DROWNED). Same gate failed
# for OPPOSITE reasons, so no threshold fixes both. Fix: seed on the PRECISE anchors the
# issue actually names (verbatim file paths + backtick code symbols), then expand over the
# CALL GRAPH in BOTH directions (callers AND callees) for a small fixed ego-radius
# (RepoGraph ICLR 2025: a k=1-2 ego-graph IS the bug's neighborhood), bounded by a uniform
# COST budget. A tiny repo -> tiny neighborhood; a huge repo -> the closest neighborhood,
# bounded; an empty anchor set -> empty scope -> whole-graph resolve (never starves). Same
# mechanism on any repo; the two numbers below are a structural ego-radius and a uniform
# cost ceiling, NOT a per-task threshold.
EGO_HOPS = 2                 # ego-graph radius (structural; RepoGraph ICLR 2025)
NEIGHBORHOOD_BUDGET = 400    # uniform COST ceiling on files the type-checker processes
_FRONTIER_CAP = 300          # per-hop BFS fan-out bound (query-cost, uniform)

def rel(p):
    try:
        r = os.path.relpath(p, SRC) if os.path.isabs(p) or p.startswith(SRC) else p
    except ValueError:
        r = p
    return r.replace("\\", "/").lstrip("./")

rel_set = set()
for root, _, fs in os.walk(SRC):
    if "/.git" in root:
        continue
    for f in fs:
        rel_set.add(rel(os.path.join(root, f)))

# ---- precise seeds: verbatim file paths + anchor symbols (NOT broad word matching) ----
seed_files = set()
for tok in re.findall(r'[\w./\\-]+\.[A-Za-z0-9_]+', issue):
    rtok = rel(tok.replace("\\", "/").strip("`'\"()[],"))
    if rtok in rel_set:
        seed_files.add(rtok)
syms = []
try:
    if os.path.exists("/tmp/gt_issue_anchors.json"):
        anch = json.loads(open("/tmp/gt_issue_anchors.json", encoding="utf-8").read() or "{}")
        for p in (anch.get("paths") or []):
            rp = rel(p)
            if rp in rel_set:
                seed_files.add(rp)
        syms = [s for s in (anch.get("symbols") or []) if s and len(s) >= 4][:60]
        syms += [str(s).split(".")[-1] for s in (anch.get("code_symbols") or []) if s][:60]
except Exception:
    syms = []

scoped = set(seed_files)
try:
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    # anchor node ids: nodes IN the seeded files + nodes NAMED by the precise symbols
    seed_ids = set()
    for f in list(seed_files)[:80]:
        for (nid,) in conn.execute(
            "SELECT id FROM nodes WHERE (file_path = ? OR file_path LIKE ?) AND is_test=0 LIMIT 40",
            (f, "%/" + f)).fetchall():
            seed_ids.add(nid)
    for s in set(syms):
        for (fp, nid) in conn.execute(
            "SELECT file_path, id FROM nodes WHERE name = ? AND is_test=0 LIMIT 12", (s,)).fetchall():
            seed_ids.add(nid)
            if fp:
                scoped.add(rel(fp))
    # bidirectional ego-graph BFS over CALLS edges, cost-bounded
    frontier = list(seed_ids); seen = set(seed_ids)
    for _hop in range(EGO_HOPS):
        if not frontier or len(scoped) >= NEIGHBORHOOD_BUDGET:
            break
        nxt = []
        for nid in frontier[:_FRONTIER_CAP]:
            if len(scoped) >= NEIGHBORHOOD_BUDGET:
                break
            for q in (
                "SELECT n.file_path, n.id FROM edges e JOIN nodes n ON n.id=e.target_id WHERE e.source_id=? AND e.type='CALLS' LIMIT 24",
                "SELECT n.file_path, n.id FROM edges e JOIN nodes n ON n.id=e.source_id WHERE e.target_id=? AND e.type='CALLS' LIMIT 24",
            ):
                for (fp, oid) in conn.execute(q, (nid,)).fetchall():
                    if fp:
                        scoped.add(rel(fp))
                    if oid not in seen:
                        seen.add(oid); nxt.append(oid)
        frontier = nxt
    conn.close()
except Exception:
    pass

scoped = sorted(p for p in scoped if p and p in rel_set)[:NEIGHBORHOOD_BUDGET]
open(OUTF, "w").write("\n".join(scoped))
print(f"  graph-neighborhood scope: {len(scoped)} files (bidirectional {EGO_HOPS}-hop ego-graph from {len(seed_files)} seed files, budget {NEIGHBORHOOD_BUDGET})")
PY
SCOPE_ARG=""; [ -s "$OUT/scope.txt" ] && SCOPE_ARG="--source-files $OUT/scope.txt"

echo "=== (3) LSP resolve (bundled pyright, repo deps present in-container) ==="
# LSP language is NOT hardcoded to python: detect the DOMINANT language in the graph
# so the precision pass serves whatever language the task repo is (resolve.py dispatches
# per-ext via _LANG_TO_EXT; an unsupported language resolves 0 -> GATE 2 fails-closed
# = explicit proof classification, never a silent pass).
LSP_LANG=$("$PY" -c "import sqlite3; r=sqlite3.connect('$OUT/graph.db').execute(\"select language from nodes where is_test=0 and language is not null and trim(language)!='' group by language order by count(*) desc limit 1\").fetchone(); print(r[0] if r else 'python')" 2>/dev/null || echo python)
echo "  dominant graph language: $LSP_LANG"
"$PY" -m groundtruth.resolve --db "$OUT/graph.db" --root "$REPO" --resolve --lang "$LSP_LANG" $SCOPE_ARG \
    2>&1 | tee "$OUT/lsp_metrics.txt" | grep -aE 'LSP_METRICS' || true
LSP_RC=${PIPESTATUS[0]}
# PROOF MODE: a non-zero resolve exit is FATAL — it means a fail-closed guard fired
# (LSP-before-scoring / closure-after-LSP / semantic-consumed) or resolve crashed. The
# old WARN-and-continue masked exactly the partial-operation the guards exist to catch
# (the substrate swallowed resolve failures into the gates). Outside proof mode: WARN
# (GATE 2 still judges from the contract line) — byte-identical to before.
if [ "$LSP_RC" -ne 0 ]; then
  if [ "${GT_PROOF_MODE:-0}" = "1" ]; then
    echo "FATAL: resolve rc=$LSP_RC in PROOF MODE — fail-closed (a proof guard fired or resolve crashed; not masking it)"; exit "$LSP_RC"
  fi
  echo "WARN: resolve rc=$LSP_RC (not proof mode; GATE 2 will judge from the contract line)"
fi

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
