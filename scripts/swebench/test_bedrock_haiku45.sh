#!/bin/bash
set -euo pipefail
source ~/gt-venv/bin/activate
source ~/gt-env.sh 2>/dev/null || true

CONFIG_PATH="${CONFIG_PATH:-scripts/swebench/litellm_bedrock_haiku45.yaml}"
MODEL="${MODEL:-claude-haiku-4-5-bedrock}"

if [ -z "${AWS_ACCESS_KEY_ID:-}" ] || [ -z "${AWS_SECRET_ACCESS_KEY:-}" ]; then
    echo "ERROR: AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY must be set."
    exit 1
fi

export AWS_REGION="${AWS_REGION:-us-east-1}"

pkill -f 'litellm.*4000' 2>/dev/null || true
sleep 2

litellm --config "$CONFIG_PATH" --port 4000 > /tmp/litellm.log 2>&1 &
LITELLM_PID=$!
trap 'kill $LITELLM_PID 2>/dev/null || true' EXIT

for i in $(seq 1 30); do curl -s http://localhost:4000/health >/dev/null 2>&1 && break; sleep 2; done
curl -s http://localhost:4000/health >/dev/null 2>&1 || { echo "Proxy: FAIL - check /tmp/litellm.log"; exit 1; }

curl -s http://localhost:4000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer dummy" \
  -d "{
    \"model\": \"$MODEL\",
    \"messages\": [{\"role\": \"user\", \"content\": \"Say hello in 5 words\"}],
    \"max_tokens\": 32
  }"
