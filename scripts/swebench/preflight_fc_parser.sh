#!/bin/bash
# Preflight: verify function_calling parser works with Qwen3-Coder via LiteLLM proxy.
#
# Tests:
# 1. LiteLLM proxy is reachable
# 2. Qwen3-Coder responds to a basic completion
# 3. Qwen3-Coder responds to a tool_call request (function_calling)
# 4. SWE-agent can parse the tool_call response
#
# Usage: bash preflight_fc_parser.sh [PROXY_URL]
#   Default PROXY_URL: http://172.17.0.1:4000

set -euo pipefail

PROXY="${1:-http://172.17.0.1:4000}"
MODEL="openai/qwen3-coder-480b-a35b-instruct-maas"

echo "=== FC Parser Preflight ==="
echo "Proxy: $PROXY"
echo "Model: $MODEL"
echo ""

# Test 1: proxy reachable
echo "--- Test 1: Proxy health ---"
if curl -sf "$PROXY/health" > /dev/null 2>&1; then
    echo "PASS: Proxy reachable"
elif curl -sf "$PROXY/v1/models" > /dev/null 2>&1; then
    echo "PASS: Proxy reachable (v1/models)"
else
    echo "FAIL: Proxy not reachable at $PROXY"
    exit 1
fi
echo ""

# Test 2: basic completion
echo "--- Test 2: Basic completion ---"
RESP=$(curl -sf "$PROXY/v1/chat/completions" \
    -H "Content-Type: application/json" \
    -d '{
        "model": "qwen3-coder-480b-a35b-instruct-maas",
        "messages": [{"role": "user", "content": "Say hello in 3 words"}],
        "max_tokens": 50,
        "temperature": 0.0
    }' 2>&1) || { echo "FAIL: Basic completion failed"; echo "$RESP"; exit 1; }

CONTENT=$(echo "$RESP" | python3 -c "import sys,json; r=json.load(sys.stdin); print(r['choices'][0]['message']['content'][:80])" 2>/dev/null) || {
    echo "FAIL: Could not parse response"
    echo "$RESP" | head -5
    exit 1
}
echo "PASS: Got response: $CONTENT"
echo ""

# Test 3: tool_call request (function_calling)
echo "--- Test 3: Function calling (tool_calls) ---"
TOOL_RESP=$(curl -sf "$PROXY/v1/chat/completions" \
    -H "Content-Type: application/json" \
    -d '{
        "model": "qwen3-coder-480b-a35b-instruct-maas",
        "messages": [
            {"role": "system", "content": "You are a helpful coding assistant. Use the provided tools to interact with the system."},
            {"role": "user", "content": "List the files in the current directory."}
        ],
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": "bash",
                    "description": "Execute a bash command",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "command": {
                                "type": "string",
                                "description": "The bash command to execute"
                            }
                        },
                        "required": ["command"]
                    }
                }
            },
            {
                "type": "function",
                "function": {
                    "name": "submit",
                    "description": "Submit your changes",
                    "parameters": {
                        "type": "object",
                        "properties": {},
                        "required": []
                    }
                }
            }
        ],
        "max_tokens": 500,
        "temperature": 0.0
    }' 2>&1) || { echo "FAIL: Tool call request failed"; echo "$TOOL_RESP"; exit 1; }

# Check if response contains tool_calls
HAS_TOOL_CALLS=$(echo "$TOOL_RESP" | python3 -c "
import sys, json
r = json.load(sys.stdin)
msg = r['choices'][0]['message']
tc = msg.get('tool_calls', [])
if tc:
    print(f'YES: {len(tc)} tool call(s)')
    for t in tc:
        fn = t.get('function', {})
        print(f'  -> {fn.get(\"name\", \"?\")}({fn.get(\"arguments\", \"\")})')
else:
    content = msg.get('content', '')[:200]
    print(f'NO: model returned text instead of tool_calls')
    print(f'  content: {content}')
" 2>/dev/null) || {
    echo "FAIL: Could not parse tool_call response"
    echo "$TOOL_RESP" | python3 -m json.tool 2>/dev/null | head -30 || echo "$TOOL_RESP" | head -10
    exit 1
}

if echo "$HAS_TOOL_CALLS" | grep -q "^YES"; then
    echo "PASS: $HAS_TOOL_CALLS"
else
    echo "WARN: $HAS_TOOL_CALLS"
    echo ""
    echo "The model did NOT return tool_calls. function_calling parser will fail."
    echo "Possible fixes:"
    echo "  1. LiteLLM proxy needs tool_call support for this model"
    echo "  2. Add to litellm config: enable_tool_calling: true"
    echo "  3. Vertex MaaS endpoint may not support tool calling for Qwen"
    echo ""
    echo "Raw response (first 500 chars):"
    echo "$TOOL_RESP" | head -c 500
    exit 1
fi
echo ""

# Test 4: single-task dry run with SWE-agent (if available)
echo "--- Test 4: SWE-agent single-task dry run ---"
if command -v sweagent &>/dev/null || python3 -m sweagent --help &>/dev/null 2>&1; then
    echo "SWE-agent found. Running single-task canary on 13453..."
    echo "(This will take 2-5 minutes)"

    OUTDIR="/tmp/fc_preflight_$(date +%s)"
    timeout 300 python3 -m sweagent run-batch \
        --config benchmarks/swebench/canary_nogt_qwen_fc.yaml \
        --instances.subset verified \
        --instances.split test \
        --instances.filter "astropy__astropy-13453" \
        --output_dir "$OUTDIR" \
        --num_workers 1 \
        2>&1 | tail -20 || true

    if [ -d "$OUTDIR" ]; then
        STEPS=$(find "$OUTDIR" -name "*.traj" -exec python3 -c "
import sys, json
with open(sys.argv[1]) as f:
    traj = json.load(f)
history = traj.get('history', traj.get('trajectory', []))
print(len(history))
" {} \; 2>/dev/null | head -1)

        PATCH=$(find "$OUTDIR" -name "preds.json" -exec python3 -c "
import sys, json
with open(sys.argv[1]) as f:
    preds = json.load(f)
if isinstance(preds, list):
    preds = preds[0] if preds else {}
patch = preds.get('model_patch', preds.get('patch', ''))
print('YES' if patch.strip() else 'NO')
" {} \; 2>/dev/null | head -1)

        echo "Steps: ${STEPS:-unknown}"
        echo "Patch: ${PATCH:-unknown}"

        if [ "${STEPS:-0}" -gt 3 ] && [ "${PATCH:-NO}" = "YES" ]; then
            echo "PASS: Agent engaged and produced a patch"
        elif [ "${STEPS:-0}" -gt 3 ]; then
            echo "PASS: Agent engaged ($STEPS steps) but no patch"
        else
            echo "WARN: Only $STEPS steps — possible instant-submit"
        fi
    else
        echo "WARN: No output directory created"
    fi
else
    echo "SKIP: sweagent not installed. Install with: pip install sweagent"
    echo "Run test 4 manually on the VM after setup."
fi
echo ""

echo "=== Preflight complete ==="
