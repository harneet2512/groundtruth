#!/bin/bash
set -euo pipefail
# ─────────────────────────────────────────────────────────────────────────────
# Create VM for GT V2 Pull Architecture eval
# GCP Project: fit-parity-491905-t9 (crym) — the ONLY project to use
# ─────────────────────────────────────────────────────────────────────────────

PROJECT="fit-parity-491905-t9"
ZONE="us-central1-a"
VM_NAME="gt-v2-eval"
MACHINE_TYPE="e2-standard-8"  # 8 vCPU, 32GB RAM
DISK_SIZE="200GB"

echo "Creating VM: $VM_NAME on project $PROJECT"

gcloud compute instances create "$VM_NAME" \
    --project="$PROJECT" \
    --zone="$ZONE" \
    --machine-type="$MACHINE_TYPE" \
    --image-family=ubuntu-2204-lts \
    --image-project=ubuntu-os-cloud \
    --boot-disk-size="$DISK_SIZE" \
    --scopes=cloud-platform \
    --quiet

echo ""
echo "VM created. SSH in with:"
echo "  gcloud compute ssh $VM_NAME --project=$PROJECT --zone=$ZONE"
echo ""
echo "Then run bootstrap:"
echo "  curl -sSL https://raw.githubusercontent.com/harneet2512/groundtruth/master/scripts/swebench/vm_bootstrap.sh | bash"
echo ""
echo "Then checkout v7 branch, install, and run smoke:"
echo "  cd ~/groundtruth && git checkout v7-constraint-map && git pull"
echo "  pip install -e '.[dev,benchmark]' && pip install litellm"
echo "  bash scripts/swebench/v2_pull_smoke.sh"
