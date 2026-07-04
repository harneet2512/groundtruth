#!/usr/bin/env bash
# Create a \$10 GCP budget alert (requires Billing Budget API and billing account).
# Usage: BILLING_ACCOUNT_ID=xxx GCP_PROJECT_ID=yyy ./gcp_budget_alert.sh
set -euo pipefail

BILLING_ACCOUNT_ID="${BILLING_ACCOUNT_ID:-}"
GCP_PROJECT_ID="${GCP_PROJECT_ID:-}"

if [ -z "$BILLING_ACCOUNT_ID" ] || [ -z "$GCP_PROJECT_ID" ]; then
  echo "Set BILLING_ACCOUNT_ID and GCP_PROJECT_ID" >&2
  exit 1
fi

echo "Creating budget for project $GCP_PROJECT_ID (billing account $BILLING_ACCOUNT_ID)..."

# Create budget with 10 USD limit; alerts at 50%, 90%, 100%
gcloud billing budgets create \
  --billing-account="$BILLING_ACCOUNT_ID" \
  --display-name="groundtruth-swebench-10usd" \
  --budget-amount=10USD \
  --threshold-rule=percent=50,basis=CURRENT_SPEND \
  --threshold-rule=percent=90,basis=CURRENT_SPEND \
  --threshold-rule=percent=100,basis=CURRENT_SPEND \
  --filter-projects="projects/$GCP_PROJECT_ID" \
  2>/dev/null || echo "Note: If this fails, enable Billing Budget API and ensure you have permissions."

echo "Done. Check GCP Console > Billing > Budgets."
