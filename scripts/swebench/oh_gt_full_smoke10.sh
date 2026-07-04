#!/usr/bin/env bash
set -euo pipefail

# OpenHands GT full-potential 10-task smoke.
#
# Intended VM: gt-t0
# Goal: prove GT layer delivery and hook behavior, not leaderboard evaluation.
# This script deliberately runs only the GT arm on 10 locked Live Lite tasks.
#
# By default NUM_WORKERS is set so OpenHands evaluates multiple instances in
# parallel (not one-by-one). Override with NUM_WORKERS=1 to serialize.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"

OH_DIR="${OH_DIR:-/home/ubuntu/OpenHands}"
OH_TAG="${OH_TAG:-0.54.0}"
PYTHON_BIN="${PYTHON_BIN:-python3.12}"
LLM_CONFIG="${LLM_CONFIG:-qwen3}"
IDS_FILE="${IDS_FILE:-$REPO_DIR/benchmarks/live_lite_300_ids.json}"
GT_INDEX_BINARY="${GT_INDEX_BINARY:-$REPO_DIR/tools/sweagent/gt_edit/bin/gt-index}"
OUT_ROOT="${OUT_ROOT:-/home/ubuntu/results/oh_gt_full_smoke10_$(date -u +%Y%m%dT%H%M%SZ)}"
TASK_COUNT="${TASK_COUNT:-10}"
MAX_ITERATIONS="${MAX_ITERATIONS:-100}"

# Parallel OpenHands evaluation (run_evaluation worker pool). Default: min(TASK_COUNT, 8).
# Export NUM_WORKERS=1 to force strictly serial instance evaluation.
if [[ -z "${NUM_WORKERS+x}" ]]; then
  if [[ "${TASK_COUNT}" -lt 8 ]]; then
    NUM_WORKERS="${TASK_COUNT}"
  else
    NUM_WORKERS=8
  fi
fi

DATASET="${DATASET:-SWE-bench-Live/SWE-bench-Live}"
SPLIT="${SPLIT:-lite}"

usage() {
  cat <<EOF
Usage: bash scripts/swebench/oh_gt_full_smoke10.sh [--setup-oh] [--run]

Options:
  --setup-oh      Fetch/check out OpenHands \$OH_TAG and create/install .venv.
  --run           Run the 10-task smoke after preflight.
  --oh-dir DIR    OpenHands checkout path. Default: $OH_DIR
  --out DIR       Output directory. Default: timestamped under /home/ubuntu/results

Environment overrides:
  OH_TAG, LLM_CONFIG, IDS_FILE, GT_INDEX_BINARY, TASK_COUNT, MAX_ITERATIONS,
  NUM_WORKERS (default: parallel, min(TASK_COUNT,8); set to 1 for serial),
  DATASET, SPLIT.
EOF
}

SETUP_OH=0
RUN_SMOKE=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --setup-oh) SETUP_OH=1; shift ;;
    --run) RUN_SMOKE=1; shift ;;
    --oh-dir) OH_DIR="$2"; shift 2 ;;
    --out) OUT_ROOT="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown arg: $1" >&2; usage; exit 2 ;;
  esac
done

if [[ "$SETUP_OH" -eq 0 && "$RUN_SMOKE" -eq 0 ]]; then
  usage
  exit 2
fi

need_file() {
  local path="$1"
  local label="$2"
  if [[ ! -f "$path" ]]; then
    echo "FATAL: missing $label at $path" >&2
    exit 1
  fi
}

need_cmd() {
  local cmd="$1"
  if ! command -v "$cmd" >/dev/null 2>&1; then
    echo "FATAL: missing command: $cmd" >&2
    exit 1
  fi
}

setup_oh() {
  need_cmd git
  need_cmd "$PYTHON_BIN"
  if [[ ! -d "$OH_DIR/.git" ]]; then
    echo "FATAL: $OH_DIR is not a git checkout. Clone OpenHands there first." >&2
    exit 1
  fi
  cd "$OH_DIR"
  git fetch --tags origin
  git checkout "$OH_TAG"
  "$PYTHON_BIN" -m venv .venv
  # shellcheck source=/dev/null
  source .venv/bin/activate
  python -m pip install --upgrade pip setuptools wheel
  if [[ -f pyproject.toml ]]; then
    python -m pip install -e .
  else
    python -m pip install -r requirements.txt
  fi
  python -m pip install "datasets>=2,<4"
  python - <<'PY'
import importlib
for name in ["openhands", "datasets"]:
    importlib.import_module(name)
print("OpenHands Python imports: OK")
PY
}

select_ids() {
  python3 - "$IDS_FILE" "$TASK_COUNT" <<'PY'
import json, sys
path, n = sys.argv[1], int(sys.argv[2])
data = json.load(open(path, encoding="utf-8"))
print(",".join(data["instance_ids"][:n]))
PY
}

preflight() {
  need_cmd docker
  if [[ "$LLM_CONFIG" == */* || "$LLM_CONFIG" == *.json || "$LLM_CONFIG" == *.toml ]]; then
    need_file "$LLM_CONFIG" "LLM config"
  fi
  need_file "$IDS_FILE" "Live Lite ids"
  need_file "$GT_INDEX_BINARY" "gt-index binary"
  need_file "$REPO_DIR/scripts/swebench/oh_gt_full_wrapper.py" "OH GT wrapper"
  if [[ ! -d "$OH_DIR" ]]; then
    echo "FATAL: OpenHands dir missing: $OH_DIR" >&2
    exit 1
  fi
  if [[ ! -x "$OH_DIR/.venv/bin/python" ]]; then
    echo "FATAL: OpenHands venv missing at $OH_DIR/.venv. Run --setup-oh first." >&2
    exit 1
  fi
  if ! docker info >/dev/null 2>&1; then
    echo "FATAL: docker daemon is not reachable" >&2
    exit 1
  fi
  if ! curl -fsS --max-time 3 http://localhost:4000/health >/dev/null 2>&1; then
    echo "WARN: LiteLLM proxy health check failed at localhost:4000" >&2
  fi
  cd "$OH_DIR"
  .venv/bin/python - <<'PY'
import importlib
importlib.import_module("evaluation.benchmarks.swe_bench.run_infer")
print("OpenHands run_infer import: OK")
PY
}

run_smoke() {
  preflight
  mkdir -p "$OUT_ROOT"
  local ids
  ids="$(select_ids)"

  cat > "$OUT_ROOT/METADATA.json" <<EOF
{
  "date": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
  "condition": "oh_gt_full_smoke10",
  "task_count": $TASK_COUNT,
  "max_iterations": $MAX_ITERATIONS,
  "num_workers": $NUM_WORKERS,
  "dataset": "$DATASET",
  "split": "$SPLIT",
  "oh_dir": "$OH_DIR",
  "oh_tag": "$OH_TAG",
  "gt_branch": "$(cd "$REPO_DIR" && git branch --show-current 2>/dev/null || echo unknown)",
  "gt_commit": "$(cd "$REPO_DIR" && git rev-parse HEAD 2>/dev/null || echo unknown)",
  "gt_index_binary": "$GT_INDEX_BINARY",
  "llm_config": "$LLM_CONFIG"
}
EOF

  echo "Output: $OUT_ROOT"
  echo "Instances: $ids"
  echo "OpenHands parallel workers: NUM_WORKERS=$NUM_WORKERS (TASK_COUNT=$TASK_COUNT; set NUM_WORKERS=1 for serial)"
  cd "$OH_DIR"
  GT_INDEX_BINARY="$GT_INDEX_BINARY" \
  GT_TELEMETRY_HOST_ROOT="$OUT_ROOT/telemetry" \
  PYTHONPATH="$REPO_DIR/src:$REPO_DIR:${PYTHONPATH:-}" \
  .venv/bin/python "$REPO_DIR/scripts/swebench/oh_gt_full_wrapper.py" \
    --instance-ids "$ids" \
    --llm-config "$LLM_CONFIG" \
    --dataset "$DATASET" \
    --split "$SPLIT" \
    --max-iterations "$MAX_ITERATIONS" \
    --eval-num-workers "$NUM_WORKERS" \
    --eval-output-dir "$OUT_ROOT" \
    2>&1 | tee "$OUT_ROOT/run.log"

  python3 "$REPO_DIR/scripts/swebench/oh_gt_full_smoke_gate.py" \
    --run-log "$OUT_ROOT/run.log" \
    --output-jsonl "$OUT_ROOT/output.jsonl" \
    --out "$OUT_ROOT/SMOKE_GATE_REPORT.md"

  python3 "$REPO_DIR/scripts/swebench/gt_utilization_report.py" \
    --deep "$OUT_ROOT/output.jsonl" \
    | tee "$OUT_ROOT/GT_UTILIZATION_DEEP.txt"
}

if [[ "$SETUP_OH" -eq 1 ]]; then
  setup_oh
fi
if [[ "$RUN_SMOKE" -eq 1 ]]; then
  run_smoke
fi
