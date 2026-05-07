#!/usr/bin/env bash
# RC-06 — Language-agnostic L5 + tools + identifier extraction.
#
# Do NOT run on the VM. This script documents the local synthetic check
# proving the L5 gate, gt_validate, _is_test_file, identifier extractors,
# and the F2P seed extractor are no longer Python-only.
#
# Coverage: index gt-index's Go directory, run gt_pre_finish_gate against a
# synthetic .go edit with no test added, and assert
# check_blast_radius_no_test fires with non-zero blast radius. Run
# gt_validate against a .go file and assert a structural finding (not
# silent green-light). Verify _is_test_file recognizes Go/Java/Ruby/C#/PHP
# patterns. Verify CamelCase regex captures Go single-hump and ALL_CAPS.
#
# Expected dollar cost: $0 (no LLM calls; pure local synthetic).

set -euo pipefail

REPO="$(cd "$(dirname "$0")/../../.." && pwd)"
TMP="$(mktemp -d)"
trap "rm -rf ${TMP}" EXIT

cd "${REPO}"

# ---- Step 1: build graph.db over gt-index's own Go source ------------------
# gt-index can index its own source — that exercises the Go tree-sitter spec.
GRAPH_DB="${TMP}/graph.db"
if [ -x "${REPO}/gt-index/gt-index" ]; then
  GTINDEX="${REPO}/gt-index/gt-index"
elif [ -x "${REPO}/gt-index/gt-index.exe" ]; then
  GTINDEX="${REPO}/gt-index/gt-index.exe"
else
  echo "RC-06: gt-index binary not found under ${REPO}/gt-index/"
  echo "       Build with: cd gt-index && CGO_ENABLED=1 go build -o gt-index ./cmd/gt-index/"
  exit 2
fi
"${GTINDEX}" -root "${REPO}/gt-index" -output "${GRAPH_DB}"
test -s "${GRAPH_DB}" || { echo "FAIL: graph.db empty"; exit 1; }

# ---- Step 2: synthetic .go edit, no test --> blast-radius must fire --------
SYN="${TMP}/syn_go_repo"
mkdir -p "${SYN}"
cd "${SYN}"
git init -q
git -c user.email=t@t -c user.name=t commit -q --allow-empty -m init

# Pick a high-blast-radius Go file from gt-index. The walker is a popular
# target — many specs reference its packages.
TARGET_FILE="internal/parser/parser.go"
mkdir -p "$(dirname "${TARGET_FILE}")"
# Stage a copy of the real file, then edit it.
cp "${REPO}/gt-index/${TARGET_FILE}" "${TARGET_FILE}"
git add "${TARGET_FILE}"
git -c user.email=t@t -c user.name=t commit -q -m "seed real file"
# Edit: append a benign no-op function so the diff is non-empty.
echo "" >> "${TARGET_FILE}"
echo "// rc06_synthetic_test" >> "${TARGET_FILE}"

GT_INSTANCE_LOG_DIR="${TMP}/log" \
GT_GRAPH_DB="${GRAPH_DB}" \
GT_GATE_CWD="${SYN}" \
python3 "${REPO}/tools/sweagent/gt_pre_finish_gate/lib/gt_pre_finish_gate.py" \
  > "${TMP}/gate.out" 2>&1 || true

cat "${TMP}/log/gt_pre_finish_gate.json" \
  | python3 -c "
import json, sys
v = json.load(sys.stdin)
br = v['checks'].get('blast_radius_no_test') or []
print('blast_radius hits:', len(br))
print('files:', [r.get('file') for r in br][:5])
assert any(r.get('file', '').endswith('.go') for r in br), \
  'RC-06 FAIL: blast_radius_no_test did not fire on .go edit'
print('OK: BLAST-RADIUS-NO-TEST fired on Go edit')
"

# ---- Step 3: gt_validate on a .go file --> structural finding --------------
GT_GRAPH_DB="${GRAPH_DB}" \
GT_REPO_ROOT="${SYN}" \
python3 "${REPO}/tools/sweagent/gt_validate/lib/gt_validate.py" \
  "${TARGET_FILE}" > "${TMP}/validate.out" 2>&1 || true

if grep -q "no structural flags raised" "${TMP}/validate.out"; then
  echo "RC-06 FAIL: gt_validate emitted silent green-light on .go file"
  cat "${TMP}/validate.out"
  exit 1
fi
grep -E "BLAST-RADIUS|callers=" "${TMP}/validate.out" \
  || { echo "RC-06 FAIL: gt_validate produced no structural finding on .go"; \
       cat "${TMP}/validate.out"; exit 1; }
echo "OK: gt_validate produced a structural finding on .go"

# ---- Step 4: _is_test_file recognizes per-language patterns ----------------
python3 - <<'PY'
import sys
sys.path.insert(0, "tools/sweagent/gt_pre_finish_gate/lib")
from gt_pre_finish_gate import _is_test_file
cases = [
    ("FooTest.java",       True,  "Java leaf-name"),
    ("src/test/java/x.java", True, "Java test dir"),
    ("foo_test.go",        True,  "Go canonical"),
    ("foo_spec.rb",        True,  "Ruby spec"),
    ("FooTests.cs",        True,  "C# tests"),
    ("FooTest.php",        True,  "PHP test"),
    ("conftest.py",        True,  "pytest infra"),
    ("foo.py",             False, "non-test py"),
    ("Latest.java",        False, "endswith 'est' but not Test/Tests"),
]
fails = []
for p, expect, label in cases:
    got = _is_test_file(p)
    mark = "OK" if got == expect else "FAIL"
    print(f"  {mark}: {label}: {p} -> {got} (expected {expect})")
    if got != expect:
        fails.append((p, expect, got))
if fails:
    print(f"RC-06 FAIL: _is_test_file mismatches: {fails}")
    sys.exit(1)
PY

# ---- Step 5: gt_intel CamelCase / ALL_CAPS extension ------------------------
python3 - <<'PY'
import sys
sys.path.insert(0, "benchmarks/swebench")
from gt_intel import extract_identifiers_from_issue
go_issue = "The func Run(ctx) does not honor cancel. type Server struct should be exported."
ids = set(extract_identifiers_from_issue(go_issue))
assert "Run" in ids, f"RC-06 FAIL: Go single-hump 'Run' missing from {sorted(ids)}"
assert "Server" in ids, f"RC-06 FAIL: Go single-hump 'Server' missing from {sorted(ids)}"
c_issue = "Returns EINVAL when SIGINT is delivered. MAX_BUFFER_SIZE may be exceeded."
ids2 = set(extract_identifiers_from_issue(c_issue))
assert "EINVAL" in ids2, f"RC-06 FAIL: ALL_CAPS 'EINVAL' missing from {sorted(ids2)}"
assert "MAX_BUFFER_SIZE" in ids2, f"RC-06 FAIL: ALL_CAPS 'MAX_BUFFER_SIZE' missing"
print("OK: gt_intel CamelCase + ALL_CAPS extension")
PY

# ---- Step 6: F2P test-file token extractor for Java/C#/Ruby ----------------
python3 - <<'PY'
import sys
sys.path.insert(0, "scripts/swebench")
sys.path.insert(0, "benchmarks/swebench")
from gt_track4_pre_run import _extract_test_file_tokens
cases = [
    ("diff --git a/src/test/java/com/foo/FooTest.java b/.../FooTest.java\n", "Foo"),
    ("diff --git a/Tests/FooTests.cs b/Tests/FooTests.cs\n",                "Foo"),
    ("diff --git a/spec/foo_spec.rb b/spec/foo_spec.rb\n",                  "foo"),
]
for diff, expected in cases:
    seeds = _extract_test_file_tokens(diff)
    assert expected in seeds, f"RC-06 FAIL: expected '{expected}' in {seeds} for {diff[:60]}"
print("OK: _extract_test_file_tokens handles Java/C#/Ruby suffixes")
PY

echo "RC-06 integration check: PASS"
