#!/usr/bin/env bash
###############################################################################
# gt_trial_task.sh — gt_trial §1.5 gate check + per-action closure check on ONE
# REAL task, ANY language. NO agent, NO LLM. Generalizes gate_check.yml (which is
# python-only) to go/rust/typescript/javascript/python.
#
# It gets a REAL per-language repo at the task's exact base_commit by a shallow
# git fetch (no Docker image needed) + the REAL issue (problem_statement) from the
# dataset — reading ONLY problem_statement (never patch / test_patch / FAIL_TO_PASS).
# Then: strip tests -> gt-index -> demand-driven LSP resolve scoped to the issue's
# file mentions -> foundational_gates.py (§1.5) -> build the EXACT shipped mini-swe
# closure and run post_edit/post_view -m UNDER -S on issue-relevant files.
#
# USAGE:
#   bash railway/gt_trial_task.sh <instance_id> <lang> <hf_dataset> <split>
#   e.g. bash railway/gt_trial_task.sh gin-gonic__gin-1805 go \
#            swe-bench/SWE-bench_Multilingual test
#        bash railway/gt_trial_task.sh conan-io__conan-17123 python \
#            SWE-bench-Live/SWE-bench-Live lite
###############################################################################
set -eo pipefail

INSTANCE="${1:?instance_id required}"
LANG_ID="${2:?lang required (go|rust|typescript|javascript|python)}"
DATASET="${3:-swe-bench/SWE-bench_Multilingual}"
SPLIT="${4:-test}"

REPO="${REPO_ROOT:-/tmp/gt-delta}"
[ -d "$REPO/src/groundtruth" ] || REPO="$(cd "$(dirname "$0")/.." && pwd)"
GT_INDEX_BIN="${GT_INDEX_BIN:-/tmp/gt-index}"
TB="/tmp/tb_${LANG_ID}_$(echo "$INSTANCE" | tr -cd 'a-zA-Z0-9')"
DB="/tmp/g_${LANG_ID}.db"
ISSUE="/tmp/issue_${LANG_ID}.txt"
LSPM="/tmp/lsp_${LANG_ID}.txt"
export GT_MODELS_ROOT="${GT_MODELS_ROOT:-$REPO/models}"
export PYTHONPATH="$REPO/src:$REPO/scripts/metrics"
export GT_FORCE_ONNX_EMBEDDER=1

echo "############################################################"
echo "# gt_trial task: $INSTANCE  lang=$LANG_ID"
echo "#   dataset=$DATASET split=$SPLIT   repo_src=$TB"
echo "############################################################"

# ---------------------------------------------------------------------------
# [A] Pull the REAL repo+commit+issue from the dataset (problem_statement ONLY).
# ---------------------------------------------------------------------------
echo "=== [A] dataset row -> repo/base_commit/issue (issue text only) ==="
python3 - "$INSTANCE" "$DATASET" "$SPLIT" "$ISSUE" <<'PY'
import sys, json
from datasets import load_dataset
inst, name, split, issue_path = sys.argv[1:5]
ds = load_dataset(name, split=split)
row = next((d for d in ds if d.get("instance_id") == inst), None)
if not row:
    print(f"FATAL: instance {inst} not in {name}:{split}"); sys.exit(1)
# LEGITIMACY: read ONLY repo / base_commit / problem_statement. NEVER patch,
# test_patch, FAIL_TO_PASS, PASS_TO_PASS (those are the grader — leakage).
open(issue_path, "w", encoding="utf-8").write(row.get("problem_statement", "") or "")
meta = {"repo": row["repo"], "base_commit": row["base_commit"],
        "issue_bytes": len(row.get("problem_statement", "") or "")}
open("/tmp/gt_task_meta.json", "w").write(json.dumps(meta))
print("repo=%s base_commit=%s issue_bytes=%d" % (meta["repo"], meta["base_commit"], meta["issue_bytes"]))
PY
REPO_SLUG="$(python3 -c "import json;print(json.load(open('/tmp/gt_task_meta.json'))['repo'])")"
BASE_COMMIT="$(python3 -c "import json;print(json.load(open('/tmp/gt_task_meta.json'))['base_commit'])")"

# ---------------------------------------------------------------------------
# [B] Shallow-fetch the repo at the exact base_commit (no Docker image needed).
# ---------------------------------------------------------------------------
echo "=== [B] git fetch $REPO_SLUG @ $BASE_COMMIT (shallow) ==="
rm -rf "$TB"; mkdir -p "$TB"
( cd "$TB" && git init -q && git remote add origin "https://github.com/${REPO_SLUG}.git" \
  && ( GIT_LFS_SKIP_SMUDGE=1 git fetch -q --depth 1 origin "$BASE_COMMIT" \
       || GIT_LFS_SKIP_SMUDGE=1 git fetch -q --depth 50 origin ) \
  && GIT_LFS_SKIP_SMUDGE=1 git checkout -q "$BASE_COMMIT" 2>/dev/null || GIT_LFS_SKIP_SMUDGE=1 git checkout -q FETCH_HEAD )
echo "checked out: $(cd "$TB" && git rev-parse --short HEAD 2>/dev/null || echo '?')  files=$(find "$TB" -type f | wc -l)"

# ---------------------------------------------------------------------------
# [C] Strip tests (legitimacy: GT surfaces ZERO test names / assertions).
# ---------------------------------------------------------------------------
echo "=== [C] strip test files/dirs ==="
find "$TB" -type d \( -name tests -o -name test -o -name __tests__ -o -name testing -o -name spec \) -prune -exec rm -rf {} + 2>/dev/null || true
find "$TB" -type f \( -name 'test_*.py' -o -name '*_test.py' -o -name 'conftest.py' \
  -o -name '*_test.go' -o -name '*.test.js' -o -name '*.test.ts' -o -name '*.spec.js' \
  -o -name '*.spec.ts' -o -name '*_spec.rb' -o -name '*Test.java' -o -name '*_test.rs' \) -delete 2>/dev/null || true

# ---------------------------------------------------------------------------
# [D] Best-effort dep install so the LSP can resolve cross-module (per language).
# ---------------------------------------------------------------------------
echo "=== [D] deps for LSP cross-module resolution (best-effort) ==="
case "$LANG_ID" in
  go)         ( cd "$TB" && timeout 180 go mod download 2>/dev/null ) || echo "  go mod download warn" ;;
  rust)       ( cd "$TB" && timeout 180 cargo fetch 2>/dev/null ) || echo "  cargo fetch warn" ;;
  typescript|javascript) ( cd "$TB" && timeout 180 npm install --ignore-scripts 2>/dev/null | tail -1 ) || echo "  npm install warn" ;;
  python)     ( cd "$TB" && timeout 180 pip install -e . 2>/dev/null | tail -1 ) || echo "  pip -e . warn" ;;
esac

# ---------------------------------------------------------------------------
# [E] gt-index the real repo -> graph.db (FTS5 on).
# ---------------------------------------------------------------------------
echo "=== [E] gt-index -> $DB ==="
rm -f "$DB"
GT_REQUIRE_FTS5=1 "$GT_INDEX_BIN" -root "$TB" -output "$DB" 2>&1 | tail -4
test -f "$DB" || { echo "FATAL: graph.db not produced"; exit 1; }

# ---------------------------------------------------------------------------
# [F] Demand-driven LSP scope = issue's file mentions; resolve -> LSP_METRICS.
# ---------------------------------------------------------------------------
echo "=== [F] demand-driven LSP resolve (lang=$LANG_ID) ==="
python3 - "$ISSUE" "$TB" <<'PY'
import re, os, sys
issue = open(sys.argv[1], encoding="utf-8").read() if os.path.exists(sys.argv[1]) else ""
tb = sys.argv[2]
exts = r'\.(py|go|rs|ts|tsx|js|jsx|java|mjs|cjs)'
base = set(os.path.basename(m) for m in re.findall(r'[\w./-]+'+exts, issue))
scoped = [os.path.join(r, f) for r, _, fs in os.walk(tb) for f in fs if f in base]
open("/tmp/gt_scope_files.txt", "w").write("\n".join(scoped))
print("demand scope:", len(scoped), "files from", len(base), "issue mentions")
PY
SCOPE_ARG=""; [ -s /tmp/gt_scope_files.txt ] && SCOPE_ARG="--source-files /tmp/gt_scope_files.txt"
timeout 260 python3 -m groundtruth.resolve --db "$DB" --root "$TB" --resolve --lang "$LANG_ID" $SCOPE_ARG \
  > "$LSPM" 2>&1 || echo "  resolve non-fatal"
grep -E "LSP_METRICS|resolved=" "$LSPM" | tail -2 || echo "  (no LSP_METRICS line)"

# ---------------------------------------------------------------------------
# [G] FOUNDATIONAL GATES — §1.5 (resolution / LSP / embedder), real issue.
# ---------------------------------------------------------------------------
echo "=== [G] foundational gates (§1.5) ==="
set +e
GT_GATES_DEEP_JSON="/tmp/gates_${LANG_ID}.json" GT_LSP_METRICS_FILE="$LSPM" \
  python3 scripts/metrics/foundational_gates.py "$DB" "$TB" "$ISSUE" "$LSPM" \
  2>&1 | grep -E "GATE 1|GATE 2|GATE 3|3-GATE VERDICT|GT_GATE_METRICS|cos\(|typing_fired|WARNING|FLAG" | head -30
GATES_RC=${PIPESTATUS[0]}
set -e

# ---------------------------------------------------------------------------
# [H] CLOSURE — run the EXACT shipped mini-swe per-action engines UNDER -S on
#     issue-relevant files of THIS language, against the real task graph.db.
# ---------------------------------------------------------------------------
echo "=== [H] per-action closure under -S (the mini-swe in-container engines) ==="
python3 - "$DB" "$REPO" "$TB" "$LANG_ID" <<'PY'
import io,tarfile,base64,tempfile,os,subprocess,sys,pathlib,ast,logging,sqlite3
db, repo, tb, lang = sys.argv[1:5]
gt_agent = pathlib.Path(repo)/"artifact_deepswe"/"gt_agent.py"
ns={"__file__":str(gt_agent),"pathlib":pathlib,"base64":base64,"logger":logging.getLogger("v")}
ns["_THIS_DIR"]=gt_agent.parent.resolve(); ns["_SRC_GT"]=ns["_THIS_DIR"].parent/"src"/"groundtruth"
src=gt_agent.read_text(encoding="utf-8"); L=src.splitlines(keepends=True)
for nd in ast.parse(src).body:
    if isinstance(nd,ast.FunctionDef) and nd.name in {"_drift_closure_modules","_build_drift_tarball_b64"}:
        exec(compile("".join(L[nd.lineno-1:nd.end_lineno]),"slice","exec"),ns)
b64=ns["_build_drift_tarball_b64"]()
tmp=tempfile.mkdtemp(prefix="cl_")
with tarfile.open(fileobj=io.BytesIO(base64.b64decode(b64))) as t: t.extractall(tmp)
con=sqlite3.connect(db); con.row_factory=sqlite3.Row
# pick up to 3 of THIS language's files that have outgoing CALLS (so the engines have signal)
rows=con.execute("""SELECT n.file_path, COUNT(*) c FROM nodes n WHERE n.language=?
                    GROUP BY n.file_path ORDER BY c DESC LIMIT 3""",(lang,)).fetchall()
picks=[r["file_path"] for r in rows]
if not picks:
    print(f"  no {lang} files in graph — SKIP"); sys.exit(0)
env={**os.environ,"PYTHONPATH":tmp,"GT_REPO_ROOT":tb}
def run(mod,rel,extra):
    r=subprocess.run([sys.executable,"-S","-m",mod,f"--root={tb}",f"--db={db}",f"--file={rel}"]+extra,
                     env=env,capture_output=True,text=True,timeout=180)
    err=r.stderr.strip(); crash=any(x in err for x in ("ModuleNotFoundError","ImportError","Traceback"))
    return r.returncode,("CRASH" if crash else ("DELIVERED" if r.stdout.strip() else "quiet")),(err.splitlines()[-6:] if crash else [])
allok=True
for rel in picks:
    rc1,t1,e1=run("groundtruth.hooks.post_edit",rel,["--quiet","--max-items=3"])
    rc2,t2,e2=run("groundtruth.hooks.post_view",rel,[])
    if "CRASH" in (t1,t2): allok=False
    print(f"  {lang:11} post_edit rc={rc1} {t1:9} post_view rc={rc2} {t2:9}  {rel}")
    for l in e1+e2: print("       ",l)
print(f"  CLOSURE under -S ({lang}): {'PASS' if allok else 'FAIL'}")
sys.exit(0 if allok else 1)
PY
CLOSURE_RC=$?

echo "=== RESULT $INSTANCE ($LANG_ID): gates_exit=$GATES_RC closure=$([ $CLOSURE_RC -eq 0 ] && echo PASS || echo FAIL) ==="
exit $CLOSURE_RC
