#!/bin/bash
set -euo pipefail

# Start LiteLLM proxy for DeepSeek V3.2 via Vertex AI MaaS.
# This is the canonical stage-1 baseline proxy path for SWE-agent runs.

CONFIG_PATH="${CONFIG_PATH:-/tmp/litellm_deepseek_v32.yaml}"
PORT="${PORT:-4000}"
MODEL_ALIAS="${MODEL_ALIAS:-deepseek-v3}"
VERTEX_MODEL="${VERTEX_MODEL:-vertex_ai/deepseek-v3.2-maas}"

kill $(cat /tmp/litellm_proxy.pid 2>/dev/null) 2>/dev/null || true
pkill -f "litellm.*${PORT}" 2>/dev/null || true

cat > "$CONFIG_PATH" <<EOF
model_list:
  - model_name: ${MODEL_ALIAS}
    litellm_params:
      model: ${VERTEX_MODEL}
EOF

echo "LiteLLM config written to: $CONFIG_PATH"
echo "Model alias: $MODEL_ALIAS"
echo "Vertex model: $VERTEX_MODEL"

nohup uv run litellm --config "$CONFIG_PATH" --port "$PORT" > /tmp/litellm.log 2>&1 &
echo $! > /tmp/litellm_proxy.pid
sleep 4

if ! curl -s --max-time 5 "http://localhost:${PORT}/health" >/dev/null 2>&1; then
    echo "Proxy failed to start. Check /tmp/litellm.log"
    exit 1
fi

echo "Proxy: OK"
echo "PID: $(cat /tmp/litellm_proxy.pid)"
echo "Health: http://localhost:${PORT}/health"
echo ""
echo "Smoke request:"
echo "curl -s http://localhost:${PORT}/v1/chat/completions \\"
echo "  -H 'Content-Type: application/json' \\"
echo "  -d '{\"model\":\"${MODEL_ALIAS}\",\"messages\":[{\"role\":\"user\",\"content\":\"Say hello in one word\"}],\"max_tokens\":5,\"temperature\":1.0,\"top_p\":0.95}'"
