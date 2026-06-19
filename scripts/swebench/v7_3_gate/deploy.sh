#!/usr/bin/env bash
# v7.3 gate -- deploy bundle from workstation to VM1.
# Runs ON the user's workstation (Windows + Git Bash, or WSL, or any POSIX shell with gcloud).
# Uploads:
#   1. v7.3 bundle  -> /opt/gt_bundle/gt_pretask_brief_v7_full.py
#   2. run_gate.sh, monitor.sh, verify_and_report.sh, decision_tree.md -> /opt/gt_bundle/v7_3_gate/
#   3. instance_ids.txt (paired n=10 task IDs)            -> /opt/gt_bundle/v7_3_gate/instance_ids.txt
# Verifies SHA256 on the VM after upload. Bails on any mismatch.

set -euo pipefail

# --- Constants (locked per reference_gcp_swebench_vms.md) ---
GCP_PROJECT="project-26227097-98fa-4016-a54"
GCP_ZONE="us-central1-a"
GCP_VM="robbymd-umls-t0"

# --- Workstation paths ---
BUNDLE_LOCAL="D:/Groundtruth/.tmp_v7_bundle/gt_pretask_brief_v7_full.py"
SCRIPT_DIR_LOCAL="D:/Groundtruth/scripts/swebench/v7_3_gate"

# Recorded SHA256 of the v7.3 bundle (computed at deploy.sh authoring time).
BUNDLE_SHA256_EXPECTED="73aa607926c838ab1e4b4a17fa39df40bb04986c958ef71d3292c806289828e4"

# --- Required argument: workstation path to instance_ids.txt ---
INSTANCE_IDS_LOCAL="${1:-}"
if [ -z "$INSTANCE_IDS_LOCAL" ]; then
  echo "USAGE: bash deploy.sh <path-to-instance_ids.txt>"
  echo ""
  echo "  <path-to-instance_ids.txt> is a workstation file with one SWE-bench"
  echo "  instance_id per line, the SAME 10 IDs used in the v7.2 smoke."
  echo "  Reference dir: C:/Users/Lenovo/Downloads/gt_v7_2_smoke_analysis/"
  echo ""
  echo "  If you do not have it as a single-column file, extract it from the"
  echo "  v7.2 smoke run's gt_report.csv via:"
  echo "    awk -F, 'NR>1 {print \$1}' /path/to/v7_2_smoke/gt_report.csv | sort -u > instance_ids.txt"
  exit 2
fi

if [ ! -f "$INSTANCE_IDS_LOCAL" ]; then
  echo "FAIL: instance_ids file not found at: $INSTANCE_IDS_LOCAL"
  exit 2
fi

LINE_COUNT=$(grep -c -v '^[[:space:]]*$' "$INSTANCE_IDS_LOCAL" || true)
if [ "$LINE_COUNT" -lt 1 ]; then
  echo "FAIL: instance_ids file is empty: $INSTANCE_IDS_LOCAL"
  exit 2
fi
echo "INFO: instance_ids file has $LINE_COUNT non-empty lines."
if [ "$LINE_COUNT" -ne 10 ]; then
  echo "WARN: expected 10 task IDs for paired n=10 gate, found $LINE_COUNT."
  echo "      Continuing because the gate spec only locks pairing, not count."
fi

# --- Step 0: gcloud auth check ---
echo "=== Step 0: gcloud auth ==="
ACCOUNT=$(gcloud auth list --filter=status:ACTIVE --format="value(account)" 2>/dev/null || true)
if [ -z "$ACCOUNT" ]; then
  echo "FAIL: no active gcloud account. Run: gcloud auth login"
  exit 1
fi
echo "PASS: active account = $ACCOUNT"

CURRENT_PROJECT=$(gcloud config get-value project 2>/dev/null || true)
if [ "$CURRENT_PROJECT" != "$GCP_PROJECT" ]; then
  echo "INFO: gcloud project is '$CURRENT_PROJECT', expected '$GCP_PROJECT'."
  echo "      Using --project=$GCP_PROJECT explicitly on every call."
fi

# --- Step 1: verify local bundle exists + SHA256 ---
echo "=== Step 1: verify local bundle SHA256 ==="
if [ ! -f "$BUNDLE_LOCAL" ]; then
  echo "FAIL: bundle not found at: $BUNDLE_LOCAL"
  echo "      Build it first with: python D:/Groundtruth/.tmp_v7_bundle/build_v7_bundle.py"
  exit 1
fi

# Cross-platform sha256 (sha256sum on Linux/Git-Bash, shasum -a 256 on macOS).
if command -v sha256sum >/dev/null 2>&1; then
  BUNDLE_SHA256_ACTUAL=$(sha256sum "$BUNDLE_LOCAL" | awk '{print $1}')
elif command -v shasum >/dev/null 2>&1; then
  BUNDLE_SHA256_ACTUAL=$(shasum -a 256 "$BUNDLE_LOCAL" | awk '{print $1}')
else
  echo "FAIL: neither sha256sum nor shasum found on workstation."
  exit 1
fi

echo "expected: $BUNDLE_SHA256_EXPECTED"
echo "actual:   $BUNDLE_SHA256_ACTUAL"
if [ "$BUNDLE_SHA256_ACTUAL" != "$BUNDLE_SHA256_EXPECTED" ]; then
  echo "FAIL: local bundle SHA256 mismatch."
  echo "      Either the bundle was rebuilt (update BUNDLE_SHA256_EXPECTED in this script)"
  echo "      or the file was corrupted. Refusing to deploy a drifted artifact."
  exit 1
fi
echo "PASS: local bundle SHA256 matches recorded value."

# --- Step 2: ensure remote dirs exist ---
echo "=== Step 2: prepare remote dirs ==="
gcloud compute ssh "$GCP_VM" \
  --project="$GCP_PROJECT" --zone="$GCP_ZONE" \
  --command="sudo mkdir -p /opt/gt_bundle/v7_3_gate && sudo chown -R \$USER:\$USER /opt/gt_bundle"

# --- Step 3: scp the bundle ---
echo "=== Step 3: scp v7.3 bundle ==="
gcloud compute scp "$BUNDLE_LOCAL" \
  "$GCP_VM:/opt/gt_bundle/gt_pretask_brief_v7_full.py" \
  --project="$GCP_PROJECT" --zone="$GCP_ZONE"

# --- Step 4: scp the gate scripts ---
echo "=== Step 4: scp gate scripts ==="
for f in run_gate.sh monitor.sh verify_and_report.sh decision_tree.md README.md; do
  if [ ! -f "$SCRIPT_DIR_LOCAL/$f" ]; then
    echo "FAIL: missing local file: $SCRIPT_DIR_LOCAL/$f"
    exit 1
  fi
  gcloud compute scp "$SCRIPT_DIR_LOCAL/$f" \
    "$GCP_VM:/opt/gt_bundle/v7_3_gate/$f" \
    --project="$GCP_PROJECT" --zone="$GCP_ZONE"
done

# --- Step 5: scp the instance_ids file ---
echo "=== Step 5: scp instance_ids.txt ==="
gcloud compute scp "$INSTANCE_IDS_LOCAL" \
  "$GCP_VM:/opt/gt_bundle/v7_3_gate/instance_ids.txt" \
  --project="$GCP_PROJECT" --zone="$GCP_ZONE"

# --- Step 6: remote SHA256 verification ---
echo "=== Step 6: verify remote SHA256 ==="
REMOTE_SHA=$(gcloud compute ssh "$GCP_VM" \
  --project="$GCP_PROJECT" --zone="$GCP_ZONE" \
  --command="sha256sum /opt/gt_bundle/gt_pretask_brief_v7_full.py | awk '{print \$1}'")
REMOTE_SHA=$(echo "$REMOTE_SHA" | tr -d '\r\n[:space:]')

echo "expected: $BUNDLE_SHA256_EXPECTED"
echo "remote:   $REMOTE_SHA"
if [ "$REMOTE_SHA" != "$BUNDLE_SHA256_EXPECTED" ]; then
  echo "FAIL: remote bundle SHA256 mismatch after upload."
  exit 1
fi

# --- Step 7: chmod +x the scripts on VM ---
gcloud compute ssh "$GCP_VM" \
  --project="$GCP_PROJECT" --zone="$GCP_ZONE" \
  --command="chmod +x /opt/gt_bundle/v7_3_gate/*.sh"

# --- Banner ---
echo ""
echo "============================================================"
echo "  PASS: v7.3 gate artifacts deployed to $GCP_VM"
echo "  Bundle:        /opt/gt_bundle/gt_pretask_brief_v7_full.py"
echo "  Gate scripts:  /opt/gt_bundle/v7_3_gate/"
echo "  Instance IDs:  /opt/gt_bundle/v7_3_gate/instance_ids.txt ($LINE_COUNT lines)"
echo "============================================================"
echo ""
echo "Next step (run on VM1):"
echo ""
echo "  gcloud compute ssh $GCP_VM --project=$GCP_PROJECT --zone=$GCP_ZONE \\"
echo "    -- 'bash /opt/gt_bundle/v7_3_gate/run_gate.sh \\"
echo "          --instance-ids-file /opt/gt_bundle/v7_3_gate/instance_ids.txt'"
echo ""
