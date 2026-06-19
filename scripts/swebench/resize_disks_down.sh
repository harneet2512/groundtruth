#!/bin/bash
# Resize both VM disks back to 300GB to save costs
# Run this AFTER both VMs are stopped
# Note: GCP only allows disk resize UP, not down. To downsize:
# 1. Create a snapshot
# 2. Create a new smaller disk from snapshot
# 3. Swap the disk
# OR just delete the VMs entirely (cheapest — we have all results)

set -euo pipefail
PROJECT=regal-scholar-442803-e1
ZONE=us-central1-a

echo "=== Post-Eval Cleanup ==="

# Check VM states
for VM in openhands-gt-eval openhands-gt-eval-b; do
    STATUS=$(gcloud compute instances describe $VM --zone=$ZONE --project=$PROJECT --format="value(status)" 2>/dev/null || echo "NOT_FOUND")
    echo "$VM: $STATUS"
    if [ "$STATUS" != "TERMINATED" ]; then
        echo "WARNING: $VM is not stopped. Stop it first."
    fi
done

echo ""
echo "Option 1 (recommended): Delete both VMs entirely"
echo "  gcloud compute instances delete openhands-gt-eval openhands-gt-eval-b --zone=$ZONE --project=$PROJECT --quiet"
echo ""
echo "Option 2: Just stop them (still charges for disk)"
echo "  Already stopped — disk charges ~\$0.17/GB/month"
echo "  VM-A: 500GB = ~\$85/month if left"
echo "  VM-B: 1000GB = ~\$170/month if left"
echo ""
echo "IMPORTANT: Copy results off VMs before deleting!"
