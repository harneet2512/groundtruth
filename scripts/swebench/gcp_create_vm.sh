#!/usr/bin/env bash
# Create a GCP VM for OpenHands + Qwen + gt_check benchmarking.
#
# Prerequisites:
#   - gcloud CLI authenticated
#   - Project: regal-scholar-442803-e1
#
# Usage:
#   bash scripts/swebench/gcp_create_vm.sh [VM_NAME]
#   bash scripts/swebench/gcp_create_vm.sh gt-bench-01
set -euo pipefail

PROJECT="regal-scholar-442803-e1"
ZONE="us-central1-a"
VM_NAME="${1:-gt-qwen-bench}"
MACHINE_TYPE="n2-standard-16"  # 16 vCPU, 64 GB RAM — enough for 5-8 workers
DISK_SIZE="200"  # GB — SWE-bench images are large

echo "=== Creating GCP VM ==="
echo "Project:  $PROJECT"
echo "Zone:     $ZONE"
echo "VM:       $VM_NAME"
echo "Machine:  $MACHINE_TYPE"
echo "Disk:     ${DISK_SIZE}GB"
echo ""

gcloud compute instances create "$VM_NAME" \
    --project="$PROJECT" \
    --zone="$ZONE" \
    --machine-type="$MACHINE_TYPE" \
    --boot-disk-size="${DISK_SIZE}GB" \
    --boot-disk-type=pd-ssd \
    --image-project=ubuntu-os-cloud \
    --image-family=ubuntu-2204-lts \
    --scopes=cloud-platform \
    --metadata=enable-oslogin=TRUE

echo ""
echo "VM created. Waiting for SSH..."
sleep 10

# Verify SSH works
gcloud compute ssh "$VM_NAME" --zone="$ZONE" --project="$PROJECT" --command="echo 'SSH OK'"

echo ""
echo "=== Next Steps ==="
echo "1. SSH into the VM:"
echo "   gcloud compute ssh $VM_NAME --zone=$ZONE --project=$PROJECT"
echo ""
echo "2. Run the bootstrap script:"
echo "   bash scripts/swebench/gcp_bootstrap_and_smoke.sh"
echo ""
echo "3. Or copy and run directly:"
echo "   gcloud compute scp scripts/swebench/gcp_bootstrap_and_smoke.sh $VM_NAME:~ --zone=$ZONE --project=$PROJECT"
echo "   gcloud compute ssh $VM_NAME --zone=$ZONE --project=$PROJECT -- 'bash ~/gcp_bootstrap_and_smoke.sh'"
