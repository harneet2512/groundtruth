#!/bin/bash
set -euo pipefail

# Start litellm proxy with function_calling support for Qwen3-Coder.
#
# Key difference from oh_setup_proxy.sh: model_name matches what SWE-agent
# sends (qwen3-coder-480b-a35b-instruct-maas) and explicitly enables
# supports_function_calling.
#
# Usage: bash setup_proxy_fc.sh

# Kill existing proxy
kill $(cat /tmp/litellm_proxy.pid 2>/dev/null) 2>/dev/null || true
sleep 2

cat > /tmp/litellm_config.yaml << 'EOF'
model_list:
  - model_name: "qwen3-coder-480b-a35b-instruct-maas"
    litellm_params:
      model: "vertex_ai/qwen/qwen3-coder-480b-a35b-instruct-maas"
      vertex_project: "regal-scholar-442803-e1"
      vertex_location: "us-south1"
      supports_function_calling: true
EOF

echo "=== Starting LiteLLM Proxy (FC-enabled) ==="
echo "Config: /tmp/litellm_config.yaml"
cat /tmp/litellm_config.yaml

source ~/.local/bin/env 2>/dev/null || true
nohup uv run litellm --config /tmp/litellm_config.yaml --port 4000 > /tmp/litellm.log 2>&1 &
echo $! > /tmp/litellm_proxy.pid
echo "Proxy PID: $(cat /tmp/litellm_proxy.pid)"

echo "Waiting for proxy..."
sleep 8

HEALTH=$(curl -s --max-time 5 http://localhost:4000/health 2>&1 || echo '{"error":"not responding"}')
echo "Health: $HEALTH"

# Quick tool_call test
echo ""
echo "=== Testing tool_calls ==="
RESP=$(curl -s --max-time 30 http://localhost:4000/v1/chat/completions \
    -H "Content-Type: application/json" \
    -d '{
        "model": "qwen3-coder-480b-a35b-instruct-maas",
        "messages": [{"role": "user", "content": "List files in /testbed"}],
        "tools": [{"type":"function","function":{"name":"bash","description":"Run bash","parameters":{"type":"object","properties":{"command":{"type":"string"}},"required":["command"]}}}],
        "max_tokens": 200,
        "temperature": 0.0
    }' 2>&1)

python3 -c "
import sys, json
r = json.loads('''$RESP''')
msg = r['choices'][0]['message']
tc = msg.get('tool_calls', [])
if tc:
    print(f'PASS: {len(tc)} tool_call(s)')
    for t in tc:
        fn = t.get('function', {})
        print(f'  -> {fn[\"name\"]}({fn[\"arguments\"]})')
else:
    print('FAIL: No tool_calls in response')
    print(f'  content: {msg.get(\"content\", \"\")[:200]}')
    sys.exit(1)
" 2>/dev/null || {
    echo "Could not parse response. Raw:"
    echo "$RESP" | head -5
    exit 1
}

echo ""
echo "=== Proxy Ready (FC-enabled) ==="
