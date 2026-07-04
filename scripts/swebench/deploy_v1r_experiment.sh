#!/usr/bin/env bash
# Deploy V1R experiment files to GCP VM t0.
# Run from workstation: bash scripts/swebench/deploy_v1r_experiment.sh

set -euo pipefail

GCP_PROJECT="project-26227097-98fa-4016-a54"
GCP_ZONE="us-central1-a"
GCP_VM="robbymd-umls-t0"
REMOTE_DIR="/home/Lenovo/v1r_experiment"

echo "=== Deploying V1R experiment to $GCP_VM ==="

# Create remote directory
gcloud compute ssh "$GCP_VM" \
  --project="$GCP_PROJECT" --zone="$GCP_ZONE" \
  --command="mkdir -p $REMOTE_DIR"

# Upload gt_intel.py (with --lean flag)
gcloud compute scp benchmarks/swebench/gt_intel.py \
  "$GCP_VM:$REMOTE_DIR/gt_intel.py" \
  --project="$GCP_PROJECT" --zone="$GCP_ZONE"

# Upload v1r_brief.py
gcloud compute scp src/groundtruth/pretask/v1r_brief.py \
  "$GCP_VM:$REMOTE_DIR/v1r_brief.py" \
  --project="$GCP_PROJECT" --zone="$GCP_ZONE"

# Upload run_arm_v1r.py (the experiment runner — created below)
gcloud compute scp scripts/swebench/run_arm_v1r.py \
  "$GCP_VM:$REMOTE_DIR/run_arm_v1r.py" \
  --project="$GCP_PROJECT" --zone="$GCP_ZONE"

# Verify
gcloud compute ssh "$GCP_VM" \
  --project="$GCP_PROJECT" --zone="$GCP_ZONE" \
  --command="ls -la $REMOTE_DIR/"

echo "=== Deploy complete ==="
echo "Next: SSH to VM and run:"
echo "  cd $REMOTE_DIR"
echo "  bash run_all_arms.sh"
