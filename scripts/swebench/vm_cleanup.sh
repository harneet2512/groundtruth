#!/usr/bin/env bash
# Stop or delete the GCP VM after results are secured.
# Usage: ./vm_cleanup.sh [stop|delete]
# Optional: upload results to GCS first with GCS_BUCKET=my-bucket ./vm_cleanup.sh stop
set -euo pipefail

ACTION="${1:-stop}"
REPO_DIR="${REPO_DIR:-$HOME/groundtruth}"
GCS_BUCKET="${GCS_BUCKET:-}"

if [ -n "$GCS_BUCKET" ] && [ -d "$REPO_DIR/benchmarks/swebench/results" ]; then
  echo "Uploading results to gs://$GCS_BUCKET/swebench-results/..."
  gsutil -m cp -r "$REPO_DIR/benchmarks/swebench/results" "gs://$GCS_BUCKET/swebench-results/" 2>/dev/null || echo "Upload failed or gsutil not configured"
fi

INSTANCE_NAME="${GCP_INSTANCE_NAME:-}"
ZONE="${GCP_ZONE:-}"

if [ -z "$INSTANCE_NAME" ] || [ -z "$ZONE" ]; then
  echo "To stop/delete VM from this machine, set GCP_INSTANCE_NAME and GCP_ZONE and run:"
  echo "  gcloud compute instances $ACTION \$GCP_INSTANCE_NAME --zone=\$GCP_ZONE"
  echo "Or run that command from your local machine with the correct instance and zone."
  exit 0
fi

case "$ACTION" in
  stop)
    gcloud compute instances stop "$INSTANCE_NAME" --zone="$ZONE"
    echo "VM stopped."
    ;;
  delete)
    gcloud compute instances delete "$INSTANCE_NAME" --zone="$ZONE" --quiet
    echo "VM deleted."
    ;;
  *)
    echo "Usage: $0 stop|delete"
    exit 1
    ;;
esac
