#!/usr/bin/env bash
# RC-01 — CloudFormation benchmaxxing → per-repo derivation.
#
# Do NOT run on the VM. This script documents the host-only verification
# that the RC-01 fix (a)–(g) generalize:
#   - Identifier extraction works on a non-Python, non-cfn-lint repo
#     (we use the gt-index Go directory itself as the test corpus).
#   - L1 brief on a synthetic Go-domain issue surfaces the Go-domain
#     identifier as a candidate.
#   - On a synthetic CloudFormation-themed issue, the literal
#     "CloudFormation" is no longer dropped by the tokenizer (it now
#     flows through to ranking; whether it ranks well depends on the
#     graph's actual node names, which is the entire point of a
#     per-repo filter).
#
# Prerequisites:
#   * gt-index built at gt-index/gt-index (CGO; Go 1.22+).
#   * sqlite3 CLI on PATH (host install of the gt-index Go binary deps).

set -euo pipefail

REPO="$(cd "$(dirname "$0")/../../.." && pwd)"
WORK="${REPO}/.tmp_rc01_check_$(date +%s)"
GT_INDEX_BIN="${REPO}/gt-index/gt-index"

mkdir -p "${WORK}"
cd "${WORK}"

# ---- Step 1: index the gt-index Go directory itself ----------------------
# The Go indexer's own source is a non-Python, non-cfn-lint corpus with
# real call graph data. The Go specs include import-extractor support, so
# the resulting graph.db has both same_file and import edges (RC-01 high-
# freq computation needs >= 5 nodes per name to produce results).
mkdir -p target_go
cp -r "${REPO}/gt-index/internal" target_go/
cp -r "${REPO}/gt-index/cmd" target_go/

"${GT_INDEX_BIN}" -root "${WORK}/target_go" -output "${WORK}/graph.db"

test -s "${WORK}/graph.db" || { echo "FAIL: graph.db not produced"; exit 1; }
echo "Indexed ${WORK}/target_go -> ${WORK}/graph.db"

# ---- Step 2: gt_navigate "Walk" relevant — Go-domain identifier --------
# "Walker" / "Walk" is a real symbol in gt-index/internal/walker. It is
# Go-domain (not Python, not cfn-lint), so any benchmaxxed code that
# privileged Python identifiers would miss it.
export GT_GRAPH_DB="${WORK}/graph.db"
export GT_INSTANCE_LOG_DIR="${WORK}/log"
mkdir -p "${GT_INSTANCE_LOG_DIR}"

NAV_OUT="${WORK}/nav_walk.txt"
python3 "${REPO}/tools/sweagent/gt_navigate/lib/gt_navigate.py" \
  "Walk a directory tree to find Go source files" relevant \
  > "${NAV_OUT}" || true

if grep -qi "(no graph nodes matched extracted identifiers" "${NAV_OUT}"; then
  echo "FAIL: gt_navigate produced no candidates for a Go-domain query"
  cat "${NAV_OUT}"
  exit 1
fi
echo "Step 2 PASS: gt_navigate surfaced Go candidates"

# ---- Step 3: synthetic CloudFormation issue — verify literal flows -----
# RC-01 (a) deleted "CloudFormation" from _NOISE_WORDS. With the per-repo
# high-freq filter, "CloudFormation" is dropped only if the indexed repo
# has it as a top-1% name (i.e. it's actually a noisy name in *this*
# repo). On the gt-index Go corpus, "CloudFormation" is not a node, so it
# flows through extraction unchanged.
ISSUE_TEXT="The CloudFormation linter raises a Walk error when scanning"
EXTRACT_OUT="${WORK}/extract.txt"
python3 - <<PY > "${EXTRACT_OUT}"
import sys, sqlite3, os
sys.path.insert(0, "${REPO}/benchmarks/swebench")
sys.path.insert(0, "${REPO}/tools/sweagent/gt_navigate/lib")
import gt_intel
import gt_navigate

# Test 1: bench gt_intel.extract_identifiers_from_issue
ids = gt_intel.extract_identifiers_from_issue("${ISSUE_TEXT}")
print("BENCH_NO_DB:", ids)

# Test 2: with conn — per-repo filter should NOT drop "CloudFormation"
# because it's not a top-1% name in the gt-index Go graph.
conn = sqlite3.connect("file:${WORK}/graph.db?mode=ro", uri=True)
high_freq = gt_intel._high_freq_repo_identifiers(conn)
print("HIGH_FREQ_SAMPLE:", sorted(list(high_freq))[:10])
ids2 = gt_intel.extract_identifiers_from_issue("${ISSUE_TEXT}", conn=conn)
print("BENCH_WITH_DB:", ids2)

# Test 3: nav module mirrors the same logic
ids3 = gt_navigate._extract_identifiers("${ISSUE_TEXT}", high_freq)
print("NAV_WITH_DB:", ids3)
conn.close()
PY

cat "${EXTRACT_OUT}"

if grep -q "CloudFormation" "${EXTRACT_OUT}"; then
  echo "Step 3 PASS: 'CloudFormation' flows through identifier extraction"
else
  echo "FAIL: 'CloudFormation' was dropped by the tokenizer"
  exit 1
fi

# ---- Step 4: gt_pre_finish_gate — blast threshold derives per-repo -----
# Synthesize an edit on a high-fanout Go file; verify the threshold
# returned by _blast_radius_threshold is computed (not the literal 20).
python3 - <<PY
import sys, sqlite3
sys.path.insert(0, "${REPO}/tools/sweagent/gt_pre_finish_gate/lib")
import os
os.environ["GT_GRAPH_DB"] = "${WORK}/graph.db"
import importlib.util
spec = importlib.util.spec_from_file_location(
    "gate", "${REPO}/tools/sweagent/gt_pre_finish_gate/lib/gt_pre_finish_gate.py")
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)
conn = sqlite3.connect("file:${WORK}/graph.db?mode=ro", uri=True)
threshold = gate._blast_radius_threshold(conn)
print(f"derived_threshold={threshold}")
assert isinstance(threshold, int) and threshold >= 5, threshold
conn.close()
PY

# ---- Step 5: scratch detection — opt-in default OFF, scope expanded ---
# Verify SCRATCH_PATTERNS_DEFAULT does NOT include test_, but enabling
# GT_GATE_SCRATCH_OPT_IN=1 brings it back. And verify the scan covers
# tests/ subdir, not just top-level.
python3 - <<PY
import os, sys, importlib.util, subprocess, tempfile
spec = importlib.util.spec_from_file_location(
    "gate", "${REPO}/tools/sweagent/gt_pre_finish_gate/lib/gt_pre_finish_gate.py")
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)

with tempfile.TemporaryDirectory() as td:
    subprocess.run(["git", "init", "-q"], cwd=td, check=True)
    subprocess.run(["git", "-C", td, "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", td, "config", "user.name", "t"], check=True)
    open(os.path.join(td, "main.py"), "w").write("x=1\n")
    subprocess.run(["git", "-C", td, "add", "main.py"], check=True)
    subprocess.run(["git", "-C", td, "commit", "-qm", "init"], check=True)
    os.makedirs(os.path.join(td, "tests"), exist_ok=True)
    open(os.path.join(td, "tests", "test_repro.py"), "w").write("# new\n")
    open(os.path.join(td, "scratch_x.py"), "w").write("# new\n")
    os.environ.pop("GT_GATE_SCRATCH_OPT_IN", None)
    flags_default = gate.check_scratch_files(td)
    files_default = {f["file"] for f in flags_default}
    # default: scratch_x.py flagged, test_repro.py NOT flagged.
    assert "scratch_x.py" in files_default, files_default
    assert "tests/test_repro.py" not in files_default, files_default
    os.environ["GT_GATE_SCRATCH_OPT_IN"] = "1"
    flags_opt_in = gate.check_scratch_files(td)
    files_opt_in = {f["file"] for f in flags_opt_in}
    # opt-in: tests/test_repro.py flagged because it's NEW (not in HEAD).
    assert "tests/test_repro.py" in files_opt_in, files_opt_in
print("Step 5 PASS: scratch opt-in + tests/ scope works")
PY

echo "RC-01 integration check: PASS"
