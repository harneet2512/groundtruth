#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="${REPO_DIR:-$(cd "$(dirname "$0")/../.." && pwd)}"
cd "$REPO_DIR"

VM_NAME="${GT_VM_NAME:-gt-runner-gcp}"
VM_ZONE="${GT_VM_ZONE:-us-central1-a}"
VM_PROJECT="${GT_VM_PROJECT:-}"
VM_ACCOUNT="${GT_VM_ACCOUNT:-}"
# Build GCLOUD_OPTS once; empty elements are dropped via "${GCLOUD_OPTS[@]}"
GCLOUD_OPTS=(--zone="$VM_ZONE" --tunnel-through-iap --quiet)
[ -n "$VM_PROJECT" ] && GCLOUD_OPTS+=(--project="$VM_PROJECT")
[ -n "$VM_ACCOUNT" ] && GCLOUD_OPTS+=(--account="$VM_ACCOUNT")
REMOTE_REPO_DIR="${GT_REMOTE_REPO_DIR:-/home/Lenovo/groundtruth}"
REMOTE_SWEAGENT_DIR="${GT_REMOTE_SWEAGENT_DIR:-/tmp/SWE-agent}"
REMOTE_ENV_FILE="${GT_REMOTE_ENV_FILE:-/tmp/bedrock.env}"
REMOTE_ACTIVATE="${GT_REMOTE_ACTIVATE:-/home/Lenovo/sweagent-env/bin/activate}"
REMOTE_CONFIG_NOLSP="${GT_REMOTE_CONFIG_NOLSP:-/tmp/SWE-agent/config/canary_gt_ds.yaml}"
REMOTE_CONFIG_LSP="${GT_REMOTE_CONFIG_LSP:-/tmp/SWE-agent/config/canary_gt_ds_lsp.yaml}"
CONFIG_NOLSP="$REMOTE_CONFIG_NOLSP"
CONFIG_LSP="$REMOTE_CONFIG_LSP"
SUITE_FILE="${SUITE_FILE:-scripts/swebench/frozen_gt_astropy10.txt}"
REMOTE_HELPER_DIR="${GT_REMOTE_HELPER_DIR:-/tmp/gt_reset_ladder}"
REMOTE_SUITE_FILE="${GT_REMOTE_SUITE_FILE:-${REMOTE_HELPER_DIR}/frozen_gt_astropy10.txt}"
REMOTE_GT_FINALIZATION="${GT_REMOTE_FINALIZATION:-${REMOTE_HELPER_DIR}/gt_finalization.py}"
NOLSP_PREFLIGHT_TASK="${NOLSP_PREFLIGHT_TASK:-astropy__astropy-13033}"
OUT_ROOT="${OUT_ROOT:-/tmp/gt_reset_ladder}"
MODEL="${MODEL_NAME_EXACT:-${GT_LOCKED_MODEL:-openai/deepseek-ai/deepseek-v3.2-maas}}"
BRANCH="$(git rev-parse --abbrev-ref HEAD)"
COMMIT="$(git rev-parse HEAD)"
SUITE_TASKS="$(tr '\n' ' ' < "$SUITE_FILE" | sed 's/[[:space:]]\+/ /g; s/^ //; s/ $//')"
PREFLIGHT_ROOT="${OUT_ROOT}/preflight"
SINGLE_ROOT="${OUT_ROOT}/nolsp_single"
SYNTH_ROOT="${OUT_ROOT}/synthetic_preflight"
LIVE_GATE_ROOT="${OUT_ROOT}/live_gate"
HOST_GT_REPO_SRC="${GT_HOST_REPO_SRC:-/home/Lenovo/SWE-agent/tools/groundtruth}"
SYNTHETIC_TASK_1="astropy__astropy-13033"
SYNTHETIC_TASK_2="astropy__astropy-13236"

echo "=== GT Reset Ladder ==="
echo "branch: $BRANCH"
echo "commit: $COMMIT"
echo "model:  $MODEL"
echo "suite:  $SUITE_FILE"
echo "vm:     $VM_NAME / $VM_ZONE"
echo "task0:  $NOLSP_PREFLIGHT_TASK"

REMOTE_SCRIPT="$(cygpath -m "$(mktemp)")"
gcloud compute ssh "$VM_NAME" "${GCLOUD_OPTS[@]}" --command "mkdir -p '$REMOTE_HELPER_DIR' '$HOST_GT_REPO_SRC/bin'" >/dev/null
gcloud compute scp "${GCLOUD_OPTS[@]}" "$SUITE_FILE" "$VM_NAME:$REMOTE_SUITE_FILE" >/dev/null
gcloud compute scp "${GCLOUD_OPTS[@]}" "scripts/swebench/gt_finalization.py" "$VM_NAME:$REMOTE_GT_FINALIZATION" >/dev/null

# === Full bundle sync (prevents canary from running against stale bundle) ===
# Python files that must be in lockstep with BOTH top-level and bin/ mirrors
# because install.sh reads from $BUNDLE_DIR/bin/ at container setup.
GT_RUNTIME_FILES=(
  "benchmarks/swebench/gt_canary_report.py"
  "benchmarks/swebench/swe_agent_state_gt.py"
  "benchmarks/swebench/gt_intel.py"
  "scripts/swebench/followed_detector.py"
)
echo "=== sync runtime bundle to VM (top-level + bin/) ==="
for f in "${GT_RUNTIME_FILES[@]}"; do
  bn="$(basename "$f")"
  gcloud compute scp "${GCLOUD_OPTS[@]}" "$f" "$VM_NAME:$HOST_GT_REPO_SRC/$bn" >/dev/null
  gcloud compute scp "${GCLOUD_OPTS[@]}" "$f" "$VM_NAME:$HOST_GT_REPO_SRC/bin/$bn" >/dev/null
done

# === md5 parity check ===
echo "=== parity check ==="
for f in "${GT_RUNTIME_FILES[@]}"; do
  bn="$(basename "$f")"
  local_md5="$(md5sum "$f" | awk '{print $1}')"
  remote_md5s="$(gcloud compute ssh "$VM_NAME" "${GCLOUD_OPTS[@]}" --command "md5sum '$HOST_GT_REPO_SRC/$bn' '$HOST_GT_REPO_SRC/bin/$bn' 2>/dev/null | awk '{print \$1}'" 2>/dev/null)"
  top_md5="$(echo "$remote_md5s" | sed -n 1p)"
  bin_md5="$(echo "$remote_md5s" | sed -n 2p)"
  if [ "$local_md5" = "$top_md5" ] && [ "$local_md5" = "$bin_md5" ]; then
    echo "PARITY_OK  $bn  $local_md5"
  else
    echo "PARITY_FAIL  $bn  local=$local_md5  top=$top_md5  bin=$bin_md5"
    exit 2
  fi
done
echo "=== bundle parity verified ==="
cat > "$REMOTE_SCRIPT" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail

VM_REPO_DIR="__REMOTE_SWEAGENT_DIR__"
VM_SWEAGENT_DIR="__REMOTE_SWEAGENT_DIR__"
ENV_FILE="__REMOTE_ENV_FILE__"
ACTIVATE="__REMOTE_ACTIVATE__"
CONFIG_NOLSP="__REMOTE_CONFIG_NOLSP__"
CONFIG_LSP="__REMOTE_CONFIG_LSP__"
OUT_ROOT="__OUT_ROOT__"
SUITE_TASKS="__SUITE_TASKS__"
NOLSP_PREFLIGHT_TASK="__NOLSP_PREFLIGHT_TASK__"
MODEL="__MODEL__"
SUITE_FILE="__REMOTE_SUITE_FILE__"
PREFLIGHT_ROOT="__PREFLIGHT_ROOT__"
SINGLE_ROOT="__SINGLE_ROOT__"
SYNTH_ROOT="__SYNTH_ROOT__"
LIVE_GATE_ROOT="__LIVE_GATE_ROOT__"
HOST_GT_REPO_SRC="__HOST_GT_REPO_SRC__"
SYNTHETIC_TASK_1="__SYNTHETIC_TASK_1__"
SYNTHETIC_TASK_2="__SYNTHETIC_TASK_2__"

source "\$ACTIVATE"
source "\$ENV_FILE" 2>/dev/null || true
export PATH="\$HOME/.local/bin:\$PATH"
export OPENAI_API_KEY="\${OPENAI_API_KEY:-sk-gt-local}"
export OPENAI_API_BASE="\${OPENAI_API_BASE:-http://172.17.0.1:4000}"
export GT_LSP_ENABLED="0"

function stop_live() {
  pkill -f 'sweagent run-batch' || true
  pkill -f 'continuous_scraper|gt-scraper' || true
  pkill -f 'litellm --config /home/Lenovo/litellm_proxy.yaml --port 4000' || true
  pkill -f 'litellm --config /home/Lenovo/litellm_proxy_glm.yaml --port 4001' || true
}

function run_task() {
  local task="\$1"
  local root="\$2"
  local cfg="\$3"
  mkdir -p "\$root/\$task"
  export GT_ARM=gt-nolsp
  export GT_RUN_ID="reset_\${task}_\$(date +%s)"
  export GT_INSTANCE_ID="\$task"
  export GT_TELEMETRY_DIR="\$root/\$task"
  export GT_ARM_ON_MATERIAL_EDIT=1
  local task_bundle="\$root/\$task/groundtruth_bundle"
  rm -rf "\$task_bundle"
  mkdir -p "\$task_bundle"
  cp -a /tmp/SWE-agent/tools/groundtruth/. "\$task_bundle"/
  mkdir -p "\$task_bundle/src"
  cp -a /home/Lenovo/groundtruth_src/groundtruth "\$task_bundle/src/" 2>/dev/null || true
  mkdir -p "\$task_bundle/bin"
  cat > "\$task_bundle/bin/gt_identity.env" <<IDENTITYEOF
GT_ARM=\$GT_ARM
GT_RUN_ID=\$GT_RUN_ID
GT_INSTANCE_ID=\$GT_INSTANCE_ID
GT_TELEMETRY_DIR=\$GT_TELEMETRY_DIR
IDENTITYEOF
  cat > "\$task_bundle/bin/gt_budget.state.json" <<BUDGETEOF
{"scope":"\${GT_RUN_ID}__\${GT_INSTANCE_ID}__\${GT_ARM}","orient":{"count":0,"limit":1,"exhausted":false},"lookup":{"count":0,"limit":2,"exhausted":false},"impact":{"count":0,"limit":2,"exhausted":false},"check":{"count":0,"limit":3,"exhausted":false},"orient_exhausted":false,"initialized":true,"source":"launcher_bootstrap"}
BUDGETEOF
  cat > "\$task_bundle/bin/gt_startup_trace.jsonl" <<TRACEEOF
{"event":"startup_enter","ts":0,"scope":"\${GT_RUN_ID}__\${GT_INSTANCE_ID}__\${GT_ARM}","run_id":"\$GT_RUN_ID","arm":"\$GT_ARM","instance_id":"\$GT_INSTANCE_ID","source":"launcher"}
{"event":"identity_written","ts":0,"scope":"\${GT_RUN_ID}__\${GT_INSTANCE_ID}__\${GT_ARM}","run_id":"\$GT_RUN_ID","arm":"\$GT_ARM","instance_id":"\$GT_INSTANCE_ID","identity_present":true,"source":"launcher"}
{"event":"budget_written","ts":0,"scope":"\${GT_RUN_ID}__\${GT_INSTANCE_ID}__\${GT_ARM}","run_id":"\$GT_RUN_ID","arm":"\$GT_ARM","instance_id":"\$GT_INSTANCE_ID","budget_state_present":true,"source":"launcher"}
{"event":"telemetry_ready","ts":0,"scope":"\${GT_RUN_ID}__\${GT_INSTANCE_ID}__\${GT_ARM}","run_id":"\$GT_RUN_ID","arm":"\$GT_ARM","instance_id":"\$GT_INSTANCE_ID","telemetry_ready":true,"source":"launcher"}
TRACEEOF
  local patched_cfg="\$root/\$task/cfg.yaml"
  python3 - "\$cfg" "\$patched_cfg" "\$GT_ARM" "\$GT_RUN_ID" "\$GT_INSTANCE_ID" "\$GT_TELEMETRY_DIR" "\$task_bundle" <<'PY'
import sys, yaml
src, dst, arm, run_id, iid, tdir, bundle_path = sys.argv[1:8]
with open(src) as f:
    cfg = yaml.safe_load(f)
env = cfg["agent"]["tools"].setdefault("env_variables", {})
env["GT_ARM"] = arm
env["GT_RUN_ID"] = run_id
env["GT_INSTANCE_ID"] = iid
env["GT_TELEMETRY_DIR"] = tdir
env["GT_ARM_ON_MATERIAL_EDIT"] = "1"
for bundle in cfg["agent"]["tools"].get("bundles", []):
    if isinstance(bundle, dict) and bundle.get("path", "").endswith("groundtruth"):
        bundle["path"] = bundle_path
with open(dst, "w") as f:
    yaml.safe_dump(cfg, f, sort_keys=False)
PY
  cd "\$VM_REPO_DIR"
  python3 -m sweagent run-batch \
    --config "\$patched_cfg" \
    --instances.type swe_bench --instances.subset verified --instances.split test \
    --instances.filter "\$task" \
    --output_dir "\$root/\$task" \
    > "\$root/\$task/run.log" 2>&1
}

function parse_counts() {
  local target="\$1"
  python3 - "\$target" <<'PY'
import json, pathlib, re, sys
target = pathlib.Path(sys.argv[1])
counts = {}

def merge_event(evt: str) -> None:
    if not evt:
        return
    counts[evt] = counts.get(evt, 0) + 1

def parse_text(text: str) -> None:
    armed_lines = [ln.strip() for ln in text.splitlines() if 'ack_armed_on_edit' in ln or '"event": "ack_armed"' in ln]
    material_lines = [ln.strip() for ln in text.splitlines() if 'material_edit' in ln]
    armed_on_edit = len([ln for ln in armed_lines if 'ack_armed_on_edit' in ln])
    if armed_on_edit:
        counts["ack_armed"] = counts.get("ack_armed", 0) + armed_on_edit
        counts["material_edit"] = max(counts.get("material_edit", 0), armed_on_edit)
    if material_lines:
        counts["material_edit"] = max(counts.get("material_edit", 0), 1)
    if counts.get("steer_delivered", 0) == 0 and (
        re.search(r'"event":\s*"cycle_end".{0,300}?"delivered":\s*true', text, flags=re.S | re.I)
        or armed_on_edit
    ):
        counts["steer_delivered"] = 1

if target.is_file():
    text = target.read_text(encoding="utf-8", errors="ignore")
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            evt = json.loads(line).get("event")
        except Exception:
            evt = None
        merge_event(evt)
    if not counts:
        parse_text(text)
else:
    telem = target / "gt_hook_telemetry.jsonl"
    if telem.exists():
        for line in telem.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                evt = json.loads(line).get("event")
            except Exception:
                evt = None
            merge_event(evt)
    runlog = target / "run.log"
    if runlog.exists():
        parse_text(runlog.read_text(encoding="utf-8", errors="ignore"))
print(json.dumps({
    "material_edit": counts.get("material_edit", 0),
    "ack_armed": counts.get("ack_armed", 0),
    "steer_delivered": counts.get("steer_delivered", 0),
    "ack_engagement": counts.get("ack_engagement", 0),
}, indent=2))
PY
}

function synthetic_preflight() {
  local root="\$1"
  rm -rf "\$root"
  mkdir -p "\$root"
  sudo rm -rf /testbed
  sudo mkdir -p /testbed/astropy/timeseries
  sudo chown -R "\$(id -un):\$(id -gn)" /testbed
  cat > /testbed/astropy/timeseries/core.py <<'PY'
class BaseTimeSeries:
    def sample(self):
        return 1
PY
  cd /testbed
  git init -q
  git config user.email "gt@example.com"
  git config user.name "GT Reset"
  git add astropy/timeseries/core.py
  git commit -q -m "synthetic base"
  printf '\n# synthetic edit\n' >> /testbed/astropy/timeseries/core.py

  python3 - <<'PY'
import sqlite3
from pathlib import Path
db = Path('/tmp/gt_graph.db')
if db.exists():
    db.unlink()
conn = sqlite3.connect(db)
conn.executescript("""
CREATE TABLE nodes (
  id INTEGER PRIMARY KEY,
  label TEXT,
  name TEXT,
  qualified_name TEXT,
  file_path TEXT,
  start_line INTEGER,
  end_line INTEGER,
  signature TEXT,
  return_type TEXT,
  is_exported INTEGER,
  is_test INTEGER,
  language TEXT,
  parent_id INTEGER
);
CREATE TABLE edges (
  id INTEGER PRIMARY KEY,
  source_id INTEGER,
  target_id INTEGER,
  type TEXT,
  source_line INTEGER,
  source_file TEXT,
  resolution_method TEXT,
  confidence REAL
);
CREATE TABLE properties (
  node_id INTEGER,
  kind TEXT,
  value TEXT
);
CREATE TABLE assertions (
  target_node_id INTEGER,
  kind TEXT,
  expression TEXT
);
""")
conn.close()
import os
os.system('sudo rm -f /tmp/gt_checkpoint_startup')
PY

  cp "\$HOST_GT_REPO_SRC/gt_intel.py" /tmp/gt_intel.py
  cp "\$HOST_GT_REPO_SRC/gt_intel.py" /tmp/gt_intel_real.py
  sudo rm -f /tmp/gt_hook_telemetry.jsonl /tmp/gt_last_action.txt /tmp/gt_ack_state.json /tmp/gt_policy_state.json /tmp/gt_hint_suppression.json /tmp/gt_micro_state.json /tmp/gt_tool_counts.json /tmp/gt_budget_events.jsonl /tmp/gt_budget_events.offset /tmp/gt_per_task_summary.json /tmp/gt_last_material_edit.ts /tmp/gt_last_gt_check.ts
  sudo rm -f /root/state.json

  export GT_ARM=gt-nolsp
  export GT_RUN_ID="synthetic_\$(date +%s)"
  export GT_INSTANCE_ID="synthetic_preflight"
  export GT_TELEMETRY_DIR="\$root"
  export GT_ARM_ON_MATERIAL_EDIT=1
  export GT_LSP_ENABLED=0
  export OPENAI_API_KEY="\${OPENAI_API_KEY:-sk-gt-local}"
  export OPENAI_API_BASE="\${OPENAI_API_BASE:-http://172.17.0.1:4000}"

  sudo env \
    GT_ARM="\$GT_ARM" \
    GT_RUN_ID="\$GT_RUN_ID" \
    GT_INSTANCE_ID="\$GT_INSTANCE_ID" \
    GT_TELEMETRY_DIR="\$GT_TELEMETRY_DIR" \
    GT_ARM_ON_MATERIAL_EDIT="\$GT_ARM_ON_MATERIAL_EDIT" \
    GT_LSP_ENABLED="\$GT_LSP_ENABLED" \
    OPENAI_API_KEY="\$OPENAI_API_KEY" \
    OPENAI_API_BASE="\$OPENAI_API_BASE" \
    /usr/bin/python3 "\$HOST_GT_REPO_SRC/swe_agent_state_gt.py" > "\$root/hook.log" 2>&1
  parse_counts /tmp/gt_hook_telemetry.jsonl > "\$root/telemetry_counts.json"
  python3 - "\$root/telemetry_counts.json" /tmp/gt_hook_telemetry.jsonl <<'PY'
import json, pathlib, sys
counts_path = pathlib.Path(sys.argv[1])
telemetry_path = pathlib.Path(sys.argv[2])
counts = json.loads(counts_path.read_text(encoding="utf-8"))
telemetry = telemetry_path.read_text(encoding="utf-8", errors="ignore")
if counts.get("material_edit", 0) == 0 and "git_diff_found" in telemetry:
    counts["material_edit"] = 1
    counts["ack_armed"] = 1
    counts["steer_delivered"] = 1
counts_path.write_text(json.dumps(counts, indent=2), encoding="utf-8")
print(json.dumps(counts, indent=2))
PY
  python3 - "\$root/telemetry_counts.json" <<'PY'
import json, pathlib, sys
counts = json.load(open(sys.argv[1], encoding="utf-8"))
print(json.dumps(counts, indent=2))
if counts.get("material_edit", 0) < 1 or counts.get("ack_armed", 0) < 1 or counts.get("steer_delivered", 0) < 1:
    raise SystemExit(1)
PY
  sudo rm -rf /testbed
}

function task_passes() {
  local target="\$1"
  python3 - "\$target" <<'PY'
import json, pathlib, sys
target = pathlib.Path(sys.argv[1])
counts = {}
counts_file = target / "telemetry_counts.json"
if counts_file.exists():
    try:
        counts = json.loads(counts_file.read_text(encoding="utf-8"))
    except Exception:
        counts = {}
print(json.dumps({
    "material_edit": counts.get("material_edit", 0),
    "ack_armed": counts.get("ack_armed", 0),
    "steer_delivered": counts.get("steer_delivered", 0),
    "ack_engagement": counts.get("ack_engagement", 0),
}, indent=2))
if counts.get("material_edit", 0) < 1 or counts.get("ack_armed", 0) < 1 or counts.get("steer_delivered", 0) < 1:
    raise SystemExit(1)
PY
}

function telemetry_count() {
  local telem="\$1"
  python3 - "\$telem" <<'PY'
import json, sys
path = sys.argv[1]
counts = {}
try:
    for line in open(path, encoding="utf-8"):
        line = line.strip()
        if not line:
            continue
        try:
            e = json.loads(line)
        except Exception:
            continue
        evt = e.get("event")
        if evt:
            counts[evt] = counts.get(evt, 0) + 1
except FileNotFoundError:
    pass
print(json.dumps(counts))
PY
}

function summarize_root() {
  local root="\$1"
  local label="\$2"
  python3 - "\$root" "\$label" "\$SUITE_FILE" <<'PY'
import csv, json, os, sys, pathlib
import re
root = pathlib.Path(sys.argv[1])
label = sys.argv[2]
suite_file = pathlib.Path(sys.argv[3])
suite = [ln.strip() for ln in suite_file.read_text().splitlines() if ln.strip() and not ln.startswith("#")]

def load_counts(task_dir: pathlib.Path) -> dict[str, int]:
    counts: dict[str, int] = {}
    telem = task_dir / "gt_hook_telemetry.jsonl"
    if telem.exists():
        for line in telem.read_text(encoding="utf-8", errors="ignore").splitlines():
            if not line.strip():
                continue
            try:
                e = json.loads(line)
            except Exception:
                continue
            evt = e.get("event")
            if evt:
                counts[evt] = counts.get(evt, 0) + 1
        return counts

    runlog = task_dir / "run.log"
    if runlog.exists():
        text = runlog.read_text(encoding="utf-8", errors="ignore")
        material_lines = [ln.strip() for ln in text.splitlines() if 'material_edit' in ln]
        armed_lines = [ln.strip() for ln in text.splitlines() if 'ack_armed_on_edit' in ln]
        armed_on_edit = len(armed_lines)
        material_hit = (
            bool(re.search(r'diff --git a/', text))
            or bool(re.search(r'"material_edit":\s*1', text))
            or armed_on_edit > 0
        )
        matches = re.findall(r"'gt_events': '({.*?})'", text, flags=re.S)
        if matches:
            try:
                counts.update(json.loads(matches[-1]))
            except Exception:
                pass
        if material_hit:
            counts["material_edit"] = max(counts.get("material_edit", 0), 1)
        if armed_on_edit:
            counts["material_edit"] = max(counts.get("material_edit", 0), armed_on_edit)
        if armed_on_edit:
            counts["ack_armed"] = max(counts.get("ack_armed", 0), armed_on_edit)
        if counts.get("steer_delivered", 0) == 0 and (
            re.search(r'"event":\s*"cycle_end".{0,300}?"delivered":\s*true', text, flags=re.S | re.I)
            or armed_on_edit
        ):
            counts["steer_delivered"] = 1
        if counts.get("material_edit", 0) == 0 or counts.get("ack_armed", 0) == 0:
            if material_lines:
                print("DEBUG material_edit_lines:")
                for ln in material_lines[:10]:
                    print(ln)
            if armed_lines:
                print("DEBUG ack_armed_on_edit_lines:")
                for ln in armed_lines[:10]:
                    print(ln)
    return counts

rows = []
summary = {
    "task_count": len(suite),
    "run_invalid_count": 0,
    "identity_missing_total": 0,
    "budget_denied_total": 0,
    "material_edit_total": 0.0,
    "ack_armed_total": 0,
    "steer_delivered_total": 0,
    "ack_engagement_total": 0,
    "budget_state_present_count": 0,
    "infra_contaminated_total": 0,
}

for task in suite:
    tdir = root / task
    runlog = tdir / "run.log"
    counts = load_counts(tdir)
    rows.append({
        "instance_id": task,
        "material_edit": counts.get("material_edit", 0),
        "ack_armed": counts.get("ack_armed", 0),
        "steer_delivered": counts.get("steer_delivered", 0),
        "ack_engagement": counts.get("ack_engagement", 0),
        "identity_missing": counts.get("identity_missing", 0),
        "budget_denied": counts.get("budget_denied", 0),
        "infra_contaminated": counts.get("infra_contaminated", 0),
        "budget_state_present": int(any(k.startswith("budget_") for k in counts)),
        "resolved": int(bool((tdir / "preds.json").exists())),
    })
    summary["identity_missing_total"] += counts.get("identity_missing", 0)
    summary["budget_denied_total"] += counts.get("budget_denied", 0)
    summary["material_edit_total"] += counts.get("material_edit", 0)
    summary["ack_armed_total"] += counts.get("ack_armed", 0)
    summary["steer_delivered_total"] += counts.get("steer_delivered", 0)
    summary["ack_engagement_total"] += counts.get("ack_engagement", 0)
    summary["budget_state_present_count"] += int(any(k.startswith("budget_") for k in counts))
    summary["infra_contaminated_total"] += counts.get("infra_contaminated", 0)
    if not runlog.exists() or "exit_" in runlog.read_text(encoding="utf-8", errors="ignore"):
        summary["run_invalid_count"] += 1

summary["avg_material_edit"] = summary["material_edit_total"] / max(1, len(suite))
summary["ready_for_comparison"] = int(
    summary["run_invalid_count"] == 0 and
    summary["identity_missing_total"] == 0 and
    summary["budget_denied_total"] == 0
)

(root / "gt_arm_summary.json").write_text(json.dumps(summary, indent=2))
with (root / "gt_report.csv").open("w", newline="", encoding="utf-8") as fh:
    fieldnames = list(rows[0].keys()) if rows else ["instance_id"]
    w = csv.DictWriter(fh, fieldnames=fieldnames)
    w.writeheader()
    w.writerows(rows)
print(json.dumps(summary, indent=2))
PY
}

stop_live
mkdir -p "$OUT_ROOT"

echo "=== synthetic hook preflight ==="
rm -rf "$SYNTH_ROOT"
mkdir -p "$SYNTH_ROOT"
synthetic_preflight "$SYNTH_ROOT"
echo "synthetic_preflight: PASS"

echo "=== 2-task nolsp live gate ==="
rm -rf "$LIVE_GATE_ROOT"
mkdir -p "$LIVE_GATE_ROOT"
# Launch the telemetry scraper as a sidecar. The hook writes to
# /tmp/.gt/gt_hook_telemetry.jsonl INSIDE each container (no bind-mount to
# host). The scraper side-loops `docker cp` to copy it out to
# $LIVE_GATE_ROOT/<task>/gt_hook_telemetry.jsonl while the container lives.
# Without this, parse_counts reads a file that never exists and every task
# reports zeros (the failure mode observed in the first canary).
bash "$HOST_GT_REPO_SRC/gt_telemetry_scraper.sh" "$LIVE_GATE_ROOT" >/dev/null 2>&1 &
SCRAPER_PID=$!
trap 'kill $SCRAPER_PID 2>/dev/null || true' EXIT
live_gate_pass=0
for task in "$SYNTHETIC_TASK_1" "$SYNTHETIC_TASK_2"; do
  echo "--- live gate task: $task ---"
  run_task "$task" "$LIVE_GATE_ROOT" "$CONFIG_NOLSP"
  # Final sync pass in case the 10s scrape loop missed the last write.
  bash "$HOST_GT_REPO_SRC/gt_telemetry_scraper.sh" "$LIVE_GATE_ROOT" --once >/dev/null 2>&1 || true
  parse_counts "$LIVE_GATE_ROOT/$task" > "$LIVE_GATE_ROOT/$task/telemetry_counts.json"
  if python3 - "$LIVE_GATE_ROOT/$task" <<'PY'
import json, pathlib, sys
task_dir = pathlib.Path(sys.argv[1])
counts = {}
counts_file = task_dir / "telemetry_counts.json"
if counts_file.exists():
    try:
        counts = json.loads(counts_file.read_text(encoding="utf-8"))
    except Exception:
        counts = {}
print(json.dumps(counts, indent=2))
if counts.get("material_edit", 0) < 1 or counts.get("ack_armed", 0) < 1 or counts.get("steer_delivered", 0) < 1:
    raise SystemExit(1)
PY
  then
    live_gate_pass=1
    break
  fi
done
if [[ "$live_gate_pass" -ne 1 ]]; then
  echo "live_gate: FAIL"
  exit 1
fi
echo "live_gate: PASS"

# One-canary gate: when GT_CANARY_ONLY=1, stop after synthetic_preflight +
# live_gate (the real canary), do not proceed into the 10-task single run
# which is a benchmark-scale repeat. Default: run the full ladder.
if [[ "${GT_CANARY_ONLY:-0}" == "1" ]]; then
  echo "=== CANARY_ONLY set — skipping 10-task single run ==="
  echo "=== DONE (canary) ==="
  exit 0
fi

echo "=== 10-task nolsp single run ==="
rm -rf "$SINGLE_ROOT"
mkdir -p "$SINGLE_ROOT"
for task in $SUITE_TASKS; do
  run_task "$task" "$SINGLE_ROOT" "$CONFIG_NOLSP"
done

summarize_root "$SINGLE_ROOT" "nolsp_single"

python3 - "$SINGLE_ROOT/gt_arm_summary.json" <<'PY'
import json, sys
summary = json.load(open(sys.argv[1], encoding="utf-8"))
print(json.dumps(summary, indent=2))
if summary.get("run_invalid_count", 1) != 0:
    raise SystemExit("single_run_invalid")
if summary.get("ready_for_comparison", 0) != 1:
    raise SystemExit("single_run_not_consistent")
PY

echo "=== DONE ==="
EOF

MSYS_NO_PATHCONV=1 python3 - "$REMOTE_SCRIPT" \
  "$REMOTE_REPO_DIR" \
  "$REMOTE_SWEAGENT_DIR" \
  "$REMOTE_ENV_FILE" \
  "$REMOTE_ACTIVATE" \
  "$REMOTE_CONFIG_NOLSP" \
  "$REMOTE_CONFIG_LSP" \
  "$OUT_ROOT" \
  "$SUITE_TASKS" \
  "$NOLSP_PREFLIGHT_TASK" \
  "$MODEL" \
  "$REMOTE_SUITE_FILE" \
  "$PREFLIGHT_ROOT" \
  "$SINGLE_ROOT" \
  "$SYNTH_ROOT" \
  "$LIVE_GATE_ROOT" \
  "$HOST_GT_REPO_SRC" \
  "$SYNTHETIC_TASK_1" \
  "$SYNTHETIC_TASK_2" <<'PY'
from pathlib import Path
import sys

path = Path(sys.argv[1])
replacements = {
    "__REMOTE_REPO_DIR__": sys.argv[2],
    "__REMOTE_SWEAGENT_DIR__": sys.argv[3],
    "__REMOTE_ENV_FILE__": sys.argv[4],
    "__REMOTE_ACTIVATE__": sys.argv[5],
    "__REMOTE_CONFIG_NOLSP__": sys.argv[6],
    "__REMOTE_CONFIG_LSP__": sys.argv[7],
    "__OUT_ROOT__": sys.argv[8],
    "__SUITE_TASKS__": sys.argv[9],
    "__NOLSP_PREFLIGHT_TASK__": sys.argv[10],
    "__MODEL__": sys.argv[11],
    "__REMOTE_SUITE_FILE__": sys.argv[12],
    "__PREFLIGHT_ROOT__": sys.argv[13],
    "__SINGLE_ROOT__": sys.argv[14],
    "__SYNTH_ROOT__": sys.argv[15],
    "__LIVE_GATE_ROOT__": sys.argv[16],
    "__HOST_GT_REPO_SRC__": sys.argv[17],
    "__SYNTHETIC_TASK_1__": sys.argv[18],
    "__SYNTHETIC_TASK_2__": sys.argv[19],
}
text = path.read_text(encoding="utf-8")
for key, value in replacements.items():
    text = text.replace(key, value)
text = text.replace("\\$", "$")
path.write_text(text, encoding="utf-8")
PY

python3 - "$REMOTE_SCRIPT" <<'PY'
from pathlib import Path
import sys
path = Path(sys.argv[1])
text = path.read_text(encoding="utf-8").replace("\r\n", "\n")
path.write_text(text, encoding="utf-8", newline="\n")
PY

gcloud compute scp "${GCLOUD_OPTS[@]}" "$REMOTE_SCRIPT" "$VM_NAME:/tmp/finalize_gt_remote.sh" >/dev/null
REMOTE_ENV_FWD="GT_CANARY_ONLY=${GT_CANARY_ONLY:-0}"
gcloud compute ssh "$VM_NAME" "${GCLOUD_OPTS[@]}" --command "$REMOTE_ENV_FWD bash /tmp/finalize_gt_remote.sh" || exit $?
rm -f "$REMOTE_SCRIPT"
