#!/bin/bash
set -euo pipefail

# Start litellm proxy for Qwen3-Coder via Vertex AI
# Usage: bash oh_setup_proxy.sh

OH_DIR="$HOME/oh-benchmarks"

# Kill existing proxy
kill $(cat /tmp/litellm_proxy.pid 2>/dev/null) 2>/dev/null || true
sleep 2

# Write litellm config for Qwen3-Coder via Vertex AI
cat > /tmp/litellm_config.yaml << 'EOF'
model_list:
  - model_name: "qwen3-coder"
    litellm_params:
      model: "vertex_ai/qwen/qwen3-coder-480b-a35b-instruct-maas"
      vertex_project: "regal-scholar-442803-e1"
      vertex_location: "us-south1"
EOF

echo "=== Starting LiteLLM Proxy ==="
echo "Config: /tmp/litellm_config.yaml"

cd "$OH_DIR"
source ~/.local/bin/env 2>/dev/null || true
nohup uv run litellm --config /tmp/litellm_config.yaml --port 4000 > /tmp/litellm.log 2>&1 &
echo $! > /tmp/litellm_proxy.pid
echo "Proxy PID: $(cat /tmp/litellm_proxy.pid)"

# Wait for proxy to start
echo "Waiting for proxy..."
sleep 8

# Check health
HEALTH=$(curl -s --max-time 5 http://localhost:4000/health 2>&1 || echo '{"error":"not responding"}')
echo "Health: $HEALTH"

# Verify Qwen3-Coder is reachable
echo ""
echo "=== Testing Model ==="
RESPONSE=$(curl -s --max-time 30 http://localhost:4000/v1/chat/completions \
    -H "Content-Type: application/json" \
    -H "Authorization: Bearer dummy" \
    -d '{"model":"qwen3-coder","messages":[{"role":"user","content":"Say hello in one word"}],"max_tokens":5,"temperature":0.7,"top_p":0.8}' 2>&1)

if echo "$RESPONSE" | python3 -c "import sys,json; d=json.load(sys.stdin); print('Model response:', d['choices'][0]['message']['content'])" 2>/dev/null; then
    echo "Model: OK"
else
    echo "Model: FAILED"
    echo "Response: $RESPONSE"
    echo ""
    echo "Check /tmp/litellm.log for details"
    exit 1
fi

echo ""
echo "=== Proxy Ready ==="
