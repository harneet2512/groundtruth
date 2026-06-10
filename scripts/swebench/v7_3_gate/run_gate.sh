#!/usr/bin/env bash
# v7.3 gate -- runs ON VM1.
# Launches paired arms (v7.3 + v7.2) on the same task IDs in the same order.
# Pre-flight assertions per CLAUDE.md SWE-bench rules:
#   - archive output.jsonl if present (per feedback_oh_resume_skips_errors)
#   - workers <= nproc
#   - /var/lib/containerd free > 50GB (per reference_swebench_disk.md)
#   - v7.2 bundle present at expected path (assumption documented; bails clearly if missing)
#   - v7.3 bundle present at /opt/gt_bundle/gt_pretask_brief_v7_full.py

set -euo pipefail

# --- Defaults ---
INSTANCE_IDS_FILE=""
WORKERS=""
MODEL="MiMo-V2-Flash"

# Assumed v7.2 bundle path on VM1. If your prior v7.2 smoke deployed it
# elsewhere, override via --v72-bundle. Script bails with a clear error if
# nothing exists at the resolved path.
V72_BUNDLE_DEFAULT="/opt/gt_bundle/gt_pretask_brief_v7_2_full.py"
V72_BUNDLE=""

# Assumed harness entry point for an OH paired smoke run. The user's prior
# smokes used scripts/swebench/oh_run_v7_smoke.sh and oh_gt_hook_wrapper.py;
# this gate calls a wrapper script that takes (--bundle, --task-ids, --model,
# --output-dir) so the same harness can target either bundle. If you have
# a different launcher, override via --launcher.
LAUNCHER_DEFAULT="/opt/groundtruth/scripts/swebench/oh_run_v7_smoke.sh"
LAUNCHER=""

# --- Parse args ---
while [ $# -gt 0 ]; do
  case "$1" in
    --instance-ids-file) INSTANCE_IDS_FILE="$2"; shift 2 ;;
    --workers)           WORKERS="$2"; shift 2 ;;
    --model)             MODEL="$2"; shift 2 ;;
    --v72-bundle)        V72_BUNDLE="$2"; shift 2 ;;
    --launcher)          LAUNCHER="$2"; shift 2 ;;
    -h|--help)
      echo "Usage: bash run_gate.sh --instance-ids-file <path> [--workers N]"
      echo "                        [--model NAME] [--v72-bundle PATH] [--launcher PATH]"
      exit 0 ;;
    *) echo "FAIL: unknown arg: $1"; exit 2 ;;
  esac
done

V72_BUNDLE="${V72_BUNDLE:-$V72_BUNDLE_DEFAULT}"
LAUNCHER="${LAUNCHER:-$LAUNCHER_DEFAULT}"

# --- Validate args ---
if [ -z "$INSTANCE_IDS_FILE" ]; then
  echo "FAIL: --instance-ids-file is required."
  exit 2
fi
if [ ! -f "$INSTANCE_IDS_FILE" ]; then
  echo "FAIL: instance-ids file not found: $INSTANCE_IDS_FILE"
  exit 2
fi

# --- Pre-flight 1: bundles present ---
echo "=== Pre-flight 1: bundles present ==="
V73_BUNDLE="/opt/gt_bundle/gt_pretask_brief_v7_full.py"
if [ ! -f "$V73_BUNDLE" ]; then
  echo "FAIL: v7.3 bundle missing at $V73_BUNDLE"
  echo "      Run deploy.sh from workstation first."
  exit 1
fi
if [ ! -f "$V72_BUNDLE" ]; then
  echo "FAIL: v7.2 bundle missing at $V72_BUNDLE"
  echo "      Override with --v72-bundle <path> if v7.2 lives elsewhere on this VM."
  echo "      Refusing to run the gate without a paired control."
  exit 1
fi
echo "PASS: v7.3 bundle = $V73_BUNDLE"
echo "PASS: v7.2 bundle = $V72_BUNDLE"

# --- Pre-flight 2: launcher present ---
echo "=== Pre-flight 2: launcher present ==="
if [ ! -x "$LAUNCHER" ] && [ ! -f "$LAUNCHER" ]; then
  echo "FAIL: launcher not found or not a file: $LAUNCHER"
  echo "      Override with --launcher <path>."
  exit 1
fi
echo "PASS: launcher = $LAUNCHER"

# --- Pre-flight 3: workers <= nproc ---
echo "=== Pre-flight 3: workers <= nproc ==="
NPROC=$(nproc)
if [ -z "$WORKERS" ]; then
  WORKERS=$NPROC
  if [ "$WORKERS" -gt 4 ]; then WORKERS=4; fi
fi
if [ "$WORKERS" -gt "$NPROC" ]; then
  echo "FAIL: workers ($WORKERS) > nproc ($NPROC). Per CLAUDE.md, refusing."
  exit 1
fi
echo "PASS: workers=$WORKERS, nproc=$NPROC"

# --- Pre-flight 4: containerd disk free > 50GB ---
echo "=== Pre-flight 4: containerd disk free ==="
CONTAINERD_DIR="/var/lib/containerd"
if [ ! -d "$CONTAINERD_DIR" ]; then
  echo "FAIL: $CONTAINERD_DIR does not exist. Per reference_swebench_disk.md, "
  echo "      this VM should mount the data disk at /var/lib/containerd."
  exit 1
fi
FREE_KB=$(df -P "$CONTAINERD_DIR" | awk 'NR==2 {print $4}')
FREE_GB=$(( FREE_KB / 1024 / 1024 ))
if [ "$FREE_GB" -lt 50 ]; then
  echo "FAIL: only ${FREE_GB}GB free on $CONTAINERD_DIR (need >=50GB)."
  echo "      Prune images: docker system prune -af --volumes"
  echo "      Or resize the data disk per reference_swebench_disk.md."
  exit 1
fi
echo "PASS: ${FREE_GB}GB free on $CONTAINERD_DIR"

# --- Pre-flight 5: archive existing output.jsonl(s) ---
echo "=== Pre-flight 5: archive prior output.jsonl ==="
ARCHIVE_TS=$(date +%s)
shopt -s nullglob
for f in $HOME/runs/v7_3_gate_arm_*/output.jsonl; do
  if [ -f "$f" ]; then
    mv "$f" "${f}.${ARCHIVE_TS}.bak"
    echo "INFO: archived $f -> ${f}.${ARCHIVE_TS}.bak"
  fi
done
shopt -u nullglob
echo "PASS: archive step complete (no-op if nothing pre-existed)."

# --- Allocate output dirs ---
TS=$(date +%s)
ARM_V73_DIR="$HOME/runs/v7_3_gate_arm_v73_${TS}"
ARM_V72_DIR="$HOME/runs/v7_3_gate_arm_v72_${TS}"
mkdir -p "$ARM_V73_DIR" "$ARM_V72_DIR"

echo ""
echo "=== Output dirs ==="
echo "  v7.3: $ARM_V73_DIR"
echo "  v7.2: $ARM_V72_DIR"

# --- Snapshot the run config ---
cat > "$ARM_V73_DIR/gate_config.json" <<EOF
{
  "arm": "v7.3",
  "ts": $TS,
  "bundle": "$V73_BUNDLE",
  "model": "$MODEL",
  "workers": $WORKERS,
  "instance_ids_file": "$INSTANCE_IDS_FILE",
  "launcher": "$LAUNCHER"
}
EOF
cat > "$ARM_V72_DIR/gate_config.json" <<EOF
{
  "arm": "v7.2",
  "ts": $TS,
  "bundle": "$V72_BUNDLE",
  "model": "$MODEL",
  "workers": $WORKERS,
  "instance_ids_file": "$INSTANCE_IDS_FILE",
  "launcher": "$LAUNCHER"
}
EOF

# Copy the task ID file into both run dirs for forensic record.
cp "$INSTANCE_IDS_FILE" "$ARM_V73_DIR/instance_ids.txt"
cp "$INSTANCE_IDS_FILE" "$ARM_V72_DIR/instance_ids.txt"

# --- Launch arms ---
# Arms run in parallel via nohup. The launcher contract is:
#   bash $LAUNCHER --bundle <path> --task-ids <file> --model <name> \
#                  --workers <N> --output-dir <dir>
# If the existing launcher takes different flags, edit this single block.
echo ""
echo "=== Launching ARM A (v7.3) ==="
nohup bash "$LAUNCHER" \
  --bundle "$V73_BUNDLE" \
  --task-ids "$INSTANCE_IDS_FILE" \
  --model "$MODEL" \
  --workers "$WORKERS" \
  --output-dir "$ARM_V73_DIR" \
  > "$ARM_V73_DIR/run.log" 2>&1 &
PID_V73=$!
echo "  PID: $PID_V73"

echo "=== Launching ARM B (v7.2) ==="
nohup bash "$LAUNCHER" \
  --bundle "$V72_BUNDLE" \
  --task-ids "$INSTANCE_IDS_FILE" \
  --model "$MODEL" \
  --workers "$WORKERS" \
  --output-dir "$ARM_V72_DIR" \
  > "$ARM_V72_DIR/run.log" 2>&1 &
PID_V72=$!
echo "  PID: $PID_V72"

# --- Persist PIDs and dirs for monitor.sh / verify_and_report.sh ---
cat > /tmp/v7_3_gate.pids <<EOF
TS=$TS
ARM_V73_DIR=$ARM_V73_DIR
ARM_V72_DIR=$ARM_V72_DIR
PID_V73=$PID_V73
PID_V72=$PID_V72
EOF

echo ""
echo "============================================================"
echo "  PASS: paired arms launched"
echo "  PIDs: v7.3=$PID_V73  v7.2=$PID_V72"
echo "  Tracking file: /tmp/v7_3_gate.pids"
echo "============================================================"
echo ""
echo "Next step (on VM1):"
echo "  bash /opt/gt_bundle/v7_3_gate/monitor.sh"
echo ""
echo "When monitor exits, run:"
echo "  bash /opt/gt_bundle/v7_3_gate/verify_and_report.sh"
echo ""
