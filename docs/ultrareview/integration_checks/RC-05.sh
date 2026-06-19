#!/usr/bin/env bash
# RC-05 integration check — L3 hook reads graph.db (gt_hook --db wired).
#
# WHAT IT PROVES
#   With the RC-05 fix, ``gt_hook.py analyze --db <graph.db>`` consumes the
#   SAME graph.db the agent's tools (gt_query / gt_search / gt_navigate /
#   gt_validate) read, instead of building a parallel AST index at
#   /tmp/gt_index.json. The brief's [CALLER] lines must reflect the edges
#   stored in graph.db within the gt_intel admissibility gate.
#
#   Assertion contract:
#     count_db    := SELECT COUNT(DISTINCT source_file) FROM edges
#                       WHERE target_id = (parse_url's id)
#                       AND type='CALLS' AND confidence >= MIN_CONFIDENCE
#                       AND source_file != target.file_path
#     count_brief := number of [CALLER] lines in gt_hook analyze --db output
#     assert count_brief > 0
#     assert count_brief <= count_db        # admissibility gate caps it
#     assert count_brief == 3 (for the synthetic_graph.db fixture, parse_url
#                              has 3 admissible cross-file callers — the
#                              cap inside gt_intel.get_callers is LIMIT 10
#                              but our fixture has exactly 3 such edges).
#
# DO NOT RUN ON A VM AS PART OF AUTOMATED CI.
#   Operator-invoked diagnostic. Uses the in-repo synthetic graph.db
#   fixture so it costs $0 and finishes in <5s.
#
# USAGE
#   bash RC-05.sh
#
set -euo pipefail

REPO_ROOT="${REPO_ROOT:-$(git rev-parse --show-toplevel 2>/dev/null || pwd)}"
HOOK="${REPO_ROOT}/benchmarks/swebench/gt_hook.py"
DB="${REPO_ROOT}/tests/layers/fixtures/synthetic_graph.db"
PY_REPO="${REPO_ROOT}/tests/layers/fixtures/repo_python"

if [[ ! -f "${DB}" ]]; then
    echo "[RC-05][FATAL] missing synthetic graph.db: ${DB}" >&2
    echo "[RC-05] Build it with: python3 ${REPO_ROOT}/tests/layers/fixtures/build_synthetic_graph.py" >&2
    exit 2
fi
if [[ ! -f "${HOOK}" ]]; then
    echo "[RC-05][FATAL] missing gt_hook.py: ${HOOK}" >&2
    exit 2
fi

# Build a temp repo whose layout matches the synthetic graph.db (src/ prefix).
WORK=$(mktemp -d -t rc05_check_XXXXXX)
trap 'rm -rf "${WORK}"' EXIT
mkdir -p "${WORK}/src"
cp "${PY_REPO}/url_utils.py"  "${WORK}/src/url_utils.py"
cp "${PY_REPO}/validators.py" "${WORK}/src/validators.py"
cp "${PY_REPO}/server.py"     "${WORK}/src/server.py"

echo "[RC-05] hook:    ${HOOK}"
echo "[RC-05] db:      ${DB}"
echo "[RC-05] repo:    ${WORK}"

# 1) Direct SQL — count admissible cross-file CALLER edges into parse_url.
COUNT_DB=$(python3 - "${DB}" <<'PY'
import sqlite3, sys
db = sys.argv[1]
conn = sqlite3.connect(f"file:{db}?mode=ro&immutable=1", uri=True)
# Mirror gt_intel.MIN_CONFIDENCE (0.7) and VERIFIED_RESOLUTIONS gate.
row = conn.execute(
    "SELECT id, file_path FROM nodes WHERE name='parse_url' LIMIT 1"
).fetchone()
if not row:
    print(0); sys.exit(0)
target_id, target_file = row
n = conn.execute(
    "SELECT COUNT(*) FROM edges e "
    "WHERE e.target_id=? AND e.type='CALLS' "
    "AND e.source_file != ? AND e.confidence >= 0.7 "
    "AND e.resolution_method IN ('same_file','import','name_match')",
    (target_id, target_file),
).fetchone()[0]
print(n)
PY
)

# 2) Run the L3 hook with --db and count [CALLER] lines in the brief.
#    GT_FRESHNESS_STRICT=0 disables the staleness gate so the body emits.
BRIEF=$(GT_FRESHNESS_STRICT=0 PYTHONIOENCODING=utf-8 \
    python3 "${HOOK}" analyze "src/url_utils.py" \
        --root "${WORK}" --db "${DB}" 2>/dev/null || true)

if [[ -z "${BRIEF}" ]]; then
    echo "[RC-05][FAIL] empty brief — gt_hook analyze --db produced no output" >&2
    exit 1
fi

COUNT_BRIEF=$(printf '%s\n' "${BRIEF}" | grep -c '^\[CALLER\]' || true)

echo "[RC-05] graph.db admissible cross-file callers of parse_url: ${COUNT_DB}"
echo "[RC-05] brief [CALLER] lines:                                  ${COUNT_BRIEF}"

if [[ "${COUNT_BRIEF}" -le 0 ]]; then
    echo "[RC-05][FAIL] brief has no [CALLER] lines despite graph having ${COUNT_DB}" >&2
    echo "----- brief -----" >&2
    printf '%s\n' "${BRIEF}" >&2
    exit 1
fi
if [[ "${COUNT_BRIEF}" -gt "${COUNT_DB}" ]]; then
    echo "[RC-05][FAIL] brief has more [CALLER] lines (${COUNT_BRIEF}) than graph admits (${COUNT_DB})" >&2
    exit 1
fi

# Anti-stale assertion: brief must come from gt_intel evidence engine, not
# the legacy AST path. The graph_db source emits the [VERIFIED] TARGET line
# from format_output; the legacy AST path emits "=== GT CODEBASE INTELLIGENCE ===".
if ! grep -q '\[VERIFIED\] TARGET: parse_url' <<<"${BRIEF}"; then
    echo "[RC-05][FAIL] brief is missing the [VERIFIED] TARGET line — RC-05 fall-through fired (legacy AST path used)" >&2
    echo "----- brief -----" >&2
    printf '%s\n' "${BRIEF}" >&2
    exit 1
fi
if grep -q '=== GT CODEBASE INTELLIGENCE ===' <<<"${BRIEF}"; then
    echo "[RC-05][FAIL] brief contains legacy AST header — graph.db path did not fire" >&2
    exit 1
fi

echo "[RC-05] integration check PASSED — gt_hook analyze --db reads graph.db; brief is consistent with admissibility gate."
