#!/usr/bin/env bash
###############################################################################
# verify_miniswe_closure.sh — gt_trial full-stack verification of the mini-swe
# per-action closure (commit 971f0e46 squash + the computed-closure fix) ACROSS
# 5 LANGUAGES, in the REAL Codespaces environment. NO agent, NO LLM (~5 min).
#
# Why this exists: the per-action graph engines run in the agent container as
#   python3 -S -m groundtruth.hooks.post_edit|post_view   (PYTHONPATH=/opt/gt/_drift)
# with NO site-packages. A local Windows `python -S` smoke proves the import
# closure but NOT the full stack (real Linux gt-index with FTS5, real ONNX
# embedder, real graph). This script runs the gt_trial §1 + §1.5 substrate gates
# AND then runs the EXACT shipped closure's engines under -S on a file of EVERY
# language in the graph — the language-agnostic in-container proof, in the box.
#
# RUN (in the Codespace, on branch deepswe-delta-stage1):
#   git fetch && git checkout deepswe-delta-stage1 && git pull
#   bash railway/verify_miniswe_closure.sh
#
# Optional env:
#   REPO_ROOT=/workspaces/groundtruth   (default)
#   GT_INDEX_ROOT=<dir to index>        (default: the repo itself — multi-language)
###############################################################################
set -eo pipefail

REPO="${REPO_ROOT:-/workspaces/groundtruth}"
[ -d "$REPO" ] || REPO="$(cd "$(dirname "$0")/.." && pwd)"   # fallback: repo root
GT_INDEX_BIN="/tmp/gt-index"
DB="/tmp/gt_verify_graph.db"
INDEX_ROOT="${GT_INDEX_ROOT:-$REPO}"
export GT_MODELS_ROOT="${GT_MODELS_ROOT:-$REPO/models}"
export PYTHONPATH="$REPO/src"
cd "$REPO"

echo "======================================================================="
echo " gt_trial verification — mini-swe per-action closure, 5-language, in-box"
echo "   repo        = $REPO"
echo "   index root  = $INDEX_ROOT"
echo "   branch/commit = $(git rev-parse --abbrev-ref HEAD) / $(git rev-parse --short HEAD)"
echo "======================================================================="

# ---------------------------------------------------------------------------
# [1] Deps (fail-LOUD per gt_trial §1.5) + e5 model for the embedder gate.
#     site-packages deps are for the GATES (foundational_gates runs WITH
#     site-packages); the per-action engine check below runs WITHOUT them (-S).
# ---------------------------------------------------------------------------
echo "=== [1] deps (fail-loud) + e5 embedder model ==="
python3 -m pip install -q onnxruntime tokenizers numpy pydantic structlog 2>&1 | tail -2 || true
python3 -c "import onnxruntime,tokenizers,numpy,pydantic; print('deps OK: onnxruntime+tokenizers+numpy+pydantic importable')"
if [ ! -f "$GT_MODELS_ROOT/e5-small-v2/model.onnx" ]; then
  mkdir -p "$GT_MODELS_ROOT"
  curl -fsSL -o /tmp/e5.tar.gz \
    "https://github.com/harneet2512/groundtruth/releases/download/gt-runtime-models/default.tmp_e5-small-v2.tar.gz" \
    && tar -xzf /tmp/e5.tar.gz -C "$GT_MODELS_ROOT" \
    && echo "e5 model fetched -> $GT_MODELS_ROOT/e5-small-v2" \
    || echo "WARN: e5 fetch failed — GATE 3a embedder will FAIL-closed (still informative)"
else
  echo "e5 model present at $GT_MODELS_ROOT/e5-small-v2"
fi

# ---------------------------------------------------------------------------
# [2] Build the REAL Linux gt-index from THIS branch, with FTS5 (gt_trial §1:
#     GT_REQUIRE_FTS5). This carries the 971f0e46 parser/resolver/store fixes.
# ---------------------------------------------------------------------------
echo "=== [2] build gt-index (-tags sqlite_fts5) from this branch ==="
command -v go >/dev/null || { echo "FATAL: go toolchain missing (Codespace should have it)"; exit 1; }
( cd gt-index && CGO_ENABLED=1 go build -buildvcs=false -tags sqlite_fts5 -o "$GT_INDEX_BIN" ./cmd/gt-index/ )
test -x "$GT_INDEX_BIN" || { echo "FATAL: gt-index build failed"; exit 1; }
echo "gt-index built: $(stat -c%s "$GT_INDEX_BIN") bytes"

# ---------------------------------------------------------------------------
# [3] Index a MULTI-LANGUAGE source tree -> real graph.db (FTS5 ON, fail-closed).
# ---------------------------------------------------------------------------
echo "=== [3] index multi-language source -> graph.db (FTS5 required) ==="
rm -f "$DB"
GT_REQUIRE_FTS5=1 "$GT_INDEX_BIN" -root "$INDEX_ROOT" -output "$DB" 2>&1 | tail -10
test -f "$DB" || { echo "FATAL: graph.db not produced"; exit 1; }
echo "--- graph.db: nodes by language ---"
python3 - "$DB" <<'PY'
import sqlite3,sys
c=sqlite3.connect(sys.argv[1]); c.row_factory=sqlite3.Row
for r in c.execute("SELECT language,COUNT(*) n FROM nodes GROUP BY language ORDER BY n DESC"):
    print(f"   {r['language']:14} {r['n']}")
fts=c.execute("SELECT COUNT(*) FROM nodes_fts").fetchone()[0] if \
    c.execute("SELECT name FROM sqlite_master WHERE name='nodes_fts'").fetchone() else 0
print(f"   nodes_fts rows: {fts}  (FTS5 {'ON' if fts>0 else 'OFF'})")
PY

# ---------------------------------------------------------------------------
# [4] FOUNDATIONAL GATES — gt_trial §1.5 (substrate CONSUMED, not just present).
#     GATE 1 resolution (det% + name_match non-dominance + typing tiers),
#     GATE 2 LSP (best-effort: run resolve.py to produce the LSP_METRICS line),
#     GATE 3 embedder (3a present/separating; 3b consumption needs a task issue).
# ---------------------------------------------------------------------------
echo "=== [4] foundational gates (§1.5) ==="
export GT_FORCE_ONNX_EMBEDDER=1
# best-effort LSP metrics so GATE 2 has a real contract line (python subgraph)
LSP_METRICS=/tmp/gt_lsp_metrics.txt
( command -v pyright >/dev/null 2>&1 || npm install -g pyright >/dev/null 2>&1 || true
  timeout 240 python3 -m groundtruth.resolve --db "$DB" --root "$INDEX_ROOT" --resolve --lang python \
    > "$LSP_METRICS" 2>/dev/null || echo "WARN: resolve LSP pass non-fatal" ) || true
ISSUE=/tmp/gt_verify_issue.txt
echo "Fix the per-action import-closure builder so post_edit and post_view run under python -S across all languages." > "$ISSUE"
set +e
GT_GATES_DEEP_JSON=/tmp/gt_gates_deep.json \
  python3 scripts/metrics/foundational_gates.py "$DB" "$INDEX_ROOT" "$ISSUE" "$LSP_METRICS"
GATES_RC=$?
set -e
echo "foundational_gates exit=$GATES_RC (0=all 3 ON; non-zero=a gate OFF)"

# ---------------------------------------------------------------------------
# [5] THE CORE — build the EXACT shipped closure (gt_agent._build_drift_tarball_b64)
#     and run post_edit + post_view  -m UNDER -S  against the real graph.db on a
#     file of EVERY language present. This is the in-container, language-agnostic
#     proof that the 971f0e46 consumer fixes actually RUN for the agent.
# ---------------------------------------------------------------------------
echo "=== [5] per-action engines under -S, per language (in the real box) ==="
python3 - "$DB" "$REPO" <<'PY'
import io,tarfile,base64,tempfile,os,subprocess,sys,pathlib,ast,logging,sqlite3
db, repo = sys.argv[1], sys.argv[2]
gt_agent = pathlib.Path(repo)/"artifact_deepswe"/"gt_agent.py"
# Build the closure by exec-ing the EXACT shipped functions (no reimplementation).
ns={"__file__":str(gt_agent),"pathlib":pathlib,"base64":base64,"logger":logging.getLogger("v")}
ns["_THIS_DIR"]=gt_agent.parent.resolve(); ns["_SRC_GT"]=ns["_THIS_DIR"].parent/"src"/"groundtruth"
src=gt_agent.read_text(encoding="utf-8"); L=src.splitlines(keepends=True)
for nd in ast.parse(src).body:
    if isinstance(nd,ast.FunctionDef) and nd.name in {"_drift_closure_modules","_build_drift_tarball_b64"}:
        exec(compile("".join(L[nd.lineno-1:nd.end_lineno]),"slice","exec"),ns)
members=ns["_drift_closure_modules"](); b64=ns["_build_drift_tarball_b64"]()
subs={}
for arc,_ in members:
    p=arc.split("/"); s=p[1] if len(p)>2 else "(root)"; subs[s]=subs.get(s,0)+1
print(f"closure: {len(members)} modules / {len(subs)} subpackages -> {dict(sorted(subs.items()))}")
for must in ("index/path_resolver","evidence/contract","telemetry/constants","graph/ego"):
    assert any(must in a for a,_ in members), f"closure MISSING core module {must}"
for never in ("memory/enrich/embed","pretask/v1r_brief","pretask/v7_4_brief"):
    assert not any(never in a for a,_ in members), f"closure WRONGLY ships heavy {never}"
print("closure core-present + heavy-excluded asserts: PASS")

tmp=tempfile.mkdtemp(prefix="gtclosure_box_")
with tarfile.open(fileobj=io.BytesIO(base64.b64decode(b64))) as t: t.extractall(tmp)

con=sqlite3.connect(db); con.row_factory=sqlite3.Row
picks={}
for r in con.execute("SELECT DISTINCT language FROM nodes WHERE language IS NOT NULL"):
    lg=r["language"]
    if lg in ("bash","markdown","yaml","toml","html","css","sql","json"): continue
    f=con.execute("SELECT file_path,COUNT(*) c FROM nodes WHERE language=? GROUP BY file_path ORDER BY c DESC LIMIT 1",(lg,)).fetchone()
    if f: picks[lg]=f["file_path"]

env={**os.environ,"PYTHONPATH":tmp,"GT_REPO_ROOT":repo}
def run(mod,rel,extra):
    r=subprocess.run([sys.executable,"-S","-m",mod,f"--root={repo}",f"--db={db}",f"--file={rel}"]+extra,
                     env=env,capture_output=True,text=True,timeout=180)
    err=r.stderr.strip(); crash=any(x in err for x in ("ModuleNotFoundError","ImportError","Traceback"))
    return r.returncode,("CRASH" if crash else ("DELIVERED" if r.stdout.strip() else "quiet")),(err.splitlines()[-6:] if crash else [])
print(f"\n{'language':12} {'post_edit':14} {'post_view':14}  file")
allok=True
for lg,rel in sorted(picks.items()):
    rc1,t1,e1=run("groundtruth.hooks.post_edit",rel,["--quiet","--max-items=3"])
    rc2,t2,e2=run("groundtruth.hooks.post_view",rel,[])
    if "CRASH" in (t1,t2): allok=False
    print(f"{lg:12} rc={rc1} {t1:9} rc={rc2} {t2:9}  {rel}")
    for l in e1+e2: print("      ",l)
print(f"\nIN-BOX 5-LANGUAGE NO-CRASH (post_edit+post_view under -S): {'PASS' if allok else 'FAIL'}")
sys.exit(0 if allok else 1)
PY
CORE_RC=$?

echo
echo "======================================================================="
echo " VERDICT"
echo "   foundational_gates (§1.5) exit = $GATES_RC  (0 = all 3 substrate gates ON)"
echo "   per-action closure 5-lang -S    = $([ $CORE_RC -eq 0 ] && echo PASS || echo FAIL)"
echo "   (closure runs the 971f0e46 consumer fixes in-container, every language)"
echo "======================================================================="
exit $CORE_RC
