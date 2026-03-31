#!/bin/bash
set -euo pipefail

# Start litellm proxy for Kimi K2-Thinking via Vertex AI MaaS (global endpoint)
# Usage: bash oh_setup_proxy_kimi.sh

OH_DIR="$HOME/oh-benchmarks"

# Kill existing proxy
kill $(cat /tmp/litellm_proxy.pid 2>/dev/null) 2>/dev/null || true
sleep 2

# Write litellm config for Kimi K2-Thinking via Vertex AI
cat > /tmp/litellm_config.yaml << 'EOF'
model_list:
  - model_name: "kimi-k2-thinking"
    litellm_params:
      model: "vertex_ai/moonshotai/kimi-k2-thinking-maas"
      vertex_project: "serious-water-484116-j0"
      vertex_location: "global"
EOF

echo "=== Starting LiteLLM Proxy (Kimi K2-Thinking) ==="
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

# Verify Kimi K2-Thinking is reachable
echo ""
echo "=== Testing Model ==="
RESPONSE=$(curl -s --max-time 60 http://localhost:4000/v1/chat/completions \
    -H "Content-Type: application/json" \
    -H "Authorization: Bearer dummy" \
    -d '{"model":"kimi-k2-thinking","messages":[{"role":"user","content":"Say hello in one word"}],"max_tokens":2048,"temperature":1.0,"top_p":0.95}' 2>&1)

if echo "$RESPONSE" | grep -q '"choices"'; then
    echo "Model: OK"
    echo "Response: $(echo "$RESPONSE" | grep -o '"content":"[^"]*"' | head -1)"
else
    echo "Model: FAILED"
    echo "Response: $RESPONSE"
    echo ""
    echo "Check /tmp/litellm.log for details"
    exit 1
fi

echo ""
echo "=== Proxy Ready (Kimi K2-Thinking) ==="
