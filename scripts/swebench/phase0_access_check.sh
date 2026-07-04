#!/usr/bin/env bash
set -euo pipefail

OUT_DIR="${1:-phase0_access_report}"
mkdir -p "$OUT_DIR"
JSON_OUT="$OUT_DIR/phase0_access_check.json"
MD_OUT="$OUT_DIR/phase0_access_check.md"

PROJECT="$(gcloud config get-value project 2>/dev/null || true)"
ACCOUNT="$(gcloud config get-value account 2>/dev/null || true)"
VM_T0="$(gcloud compute instances describe gt-t0 --zone us-central1-a --format='value(status)' 2>/dev/null || echo MISSING)"
VM_V1="$(gcloud compute instances describe gt-v1 --zone us-central1-a --format='value(status)' 2>/dev/null || echo MISSING)"
MODELS_OK=0
PING_OK=0

if curl -sS -m 10 -H "Authorization: Bearer sk-gt-local" http://localhost:4000/v1/models >/dev/null; then
  MODELS_OK=1
fi
if curl -sS -m 10 -H "Authorization: Bearer sk-gt-local" -H "Content-Type: application/json" \
  -d '{"model":"qwen3-coder-480b-a35b-instruct-maas","messages":[{"role":"user","content":"ping"}],"max_tokens":1}' \
  http://localhost:4000/v1/chat/completions >/dev/null; then
  PING_OK=1
fi

cat > "$JSON_OUT" <<EOF
{
  "project": "$PROJECT",
  "account": "$ACCOUNT",
  "vm_status": {"gt-t0": "$VM_T0", "gt-v1": "$VM_V1"},
  "models_ok": $MODELS_OK,
  "ping_ok": $PING_OK,
  "banned_project_used": false,
  "passed": $( [ "$VM_T0" = "RUNNING" ] && [ "$VM_V1" = "RUNNING" ] && [ $MODELS_OK -eq 1 ] && [ $PING_OK -eq 1 ] && echo true || echo false )
}
EOF

cat > "$MD_OUT" <<EOF
# Phase0 Access Check

- project: \
  $PROJECT
- account: \
  $ACCOUNT
- gt-t0: $VM_T0
- gt-v1: $VM_V1
- /v1/models: $MODELS_OK
- ping: $PING_OK

See JSON: \
$JSON_OUT
EOF

echo "wrote $JSON_OUT and $MD_OUT"
