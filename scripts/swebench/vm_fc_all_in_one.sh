#!/bin/bash
set -euo pipefail

# All-in-one: start proxy, preflight test, launch 2-arm comparison.
# Run on VM: bash /tmp/Groundtruth_vnext/scripts/swebench/vm_fc_all_in_one.sh

REPO="/tmp/Groundtruth_vnext"
SWEAGENT="/tmp/SWE-agent"
TIMESTAMP=$(date +%s)
OUTDIR="/tmp/fc_comparison_$TIMESTAMP"
PROXY_PORT=4000
PROXY_URL="http://localhost:$PROXY_PORT"

echo "=== FC All-in-One ==="
echo "Time: $(date -u)"
echo "Repo: $REPO"
echo ""

# Activate env
source ~/sweagent-env/bin/activate

# ── 1. Kill stale processes ──
echo "--- Cleanup ---"
pkill -f "litellm" 2>/dev/null || true
pkill -f "sweagent run-batch" 2>/dev/null || true
sleep 3
echo "OK"

# ── 2. Start proxy ──
echo ""
echo "--- Starting LiteLLM proxy ---"
cat > /tmp/litellm_fc.yaml << 'LCFG'
model_list:
  - model_name: qwen3-coder-480b-a35b-instruct-maas
    litellm_params:
      model: vertex_ai/qwen/qwen3-coder-480b-a35b-instruct-maas
      vertex_project: regal-scholar-442803-e1
      vertex_location: us-south1
      supports_function_calling: true
LCFG

nohup litellm --config /tmp/litellm_fc.yaml --port $PROXY_PORT > /tmp/litellm.log 2>&1 &
PROXY_PID=$!
echo "Proxy PID: $PROXY_PID"
echo "Waiting 12s for startup..."
sleep 12

# Health check
if curl -sf "$PROXY_URL/health" > /dev/null 2>&1; then
    echo "Proxy: HEALTHY"
else
    echo "Proxy: NOT READY"
    tail -20 /tmp/litellm.log
    exit 1
fi

# ── 3. Test basic completion ──
echo ""
echo "--- Test: basic completion ---"
BASIC=$(curl -sf --max-time 30 "$PROXY_URL/v1/chat/completions" \
    -H "Content-Type: application/json" \
    -d '{"model":"qwen3-coder-480b-a35b-instruct-maas","messages":[{"role":"user","content":"Say hello in 3 words"}],"max_tokens":50,"temperature":0.0}' 2>&1) || {
    echo "FAIL: basic completion"
    exit 1
}
echo "$BASIC" | python3 -c "import sys,json; print('OK:', json.load(sys.stdin)['choices'][0]['message']['content'][:80])"

# ── 4. Test function calling ──
echo ""
echo "--- Test: function calling (tool_calls) ---"
FC_RESP=$(curl -sf --max-time 60 "$PROXY_URL/v1/chat/completions" \
    -H "Content-Type: application/json" \
    -d '{
        "model":"qwen3-coder-480b-a35b-instruct-maas",
        "messages":[
            {"role":"system","content":"You are a helpful coding assistant."},
            {"role":"user","content":"List the files in /testbed directory."}
        ],
        "tools":[
            {"type":"function","function":{"name":"bash","description":"Execute a bash command","parameters":{"type":"object","properties":{"command":{"type":"string","description":"The bash command to execute"}},"required":["command"]}}},
            {"type":"function","function":{"name":"submit","description":"Submit your changes","parameters":{"type":"object","properties":{},"required":[]}}}
        ],
        "max_tokens":500,
        "temperature":0.0
    }' 2>&1) || {
    echo "FAIL: function calling request failed"
    echo "Check /tmp/litellm.log"
    tail -20 /tmp/litellm.log
    exit 1
}

FC_OK=$(python3 -c "
import json, sys
r = json.loads(sys.argv[1])
msg = r['choices'][0]['message']
tc = msg.get('tool_calls', [])
if tc:
    print(f'PASS: {len(tc)} tool_call(s)')
    for t in tc:
        fn = t.get('function', {})
        print(f'  -> {fn.get(\"name\", \"?\")}({fn.get(\"arguments\", \"\")})')
    sys.exit(0)
else:
    content = msg.get('content', '')[:200]
    print(f'FAIL: no tool_calls returned')
    print(f'  content: {content}')
    sys.exit(1)
" "$FC_RESP" 2>&1) || {
    echo "$FC_OK"
    echo ""
    echo "Function calling NOT supported by this endpoint."
    echo "Cannot use function_calling parser. Exiting."
    exit 1
}
echo "$FC_OK"

# ── 5. Copy configs to SWE-agent ──
echo ""
echo "--- Setup configs ---"
cp "$REPO/benchmarks/swebench/canary_gt_qwen_fc.yaml" "$SWEAGENT/config/"
cp "$REPO/benchmarks/swebench/canary_nogt_qwen_fc.yaml" "$SWEAGENT/config/"

# Copy GT tools bundle
if [ -d "$SWEAGENT/tools/groundtruth" ]; then
    echo "GT tools bundle: exists at $SWEAGENT/tools/groundtruth"
else
    echo "WARNING: GT tools bundle not found at $SWEAGENT/tools/groundtruth"
fi

# ── 6. Set OPENAI_API_BASE for SWE-agent ──
export OPENAI_API_BASE="http://172.17.0.1:$PROXY_PORT/v1"
export OPENAI_API_KEY="dummy"

# ── 7. Launch 2-arm comparison ──
echo ""
echo "=== Launching 2-arm comparison ==="
echo "Output: $OUTDIR"
mkdir -p "$OUTDIR"

TASKS=$(paste -sd'|' "$REPO/scripts/swebench/frozen_gt_astropy10.txt")
echo "Tasks: $TASKS"

# Arm BL (baseline, no GT)
echo ""
echo "--- Arm BL: baseline (function_calling, no GT) ---"
BL_DIR="$OUTDIR/arm_BL"
mkdir -p "$BL_DIR"
cd "$SWEAGENT"
nohup python3 -m sweagent run-batch \
    --config config/canary_nogt_qwen_fc.yaml \
    --instances.subset verified \
    --instances.split test \
    --instances.filter "$TASKS" \
    --output_dir "$BL_DIR" \
    --num_workers 4 \
    > "$BL_DIR/run.log" 2>&1 &
BL_PID=$!
echo "  PID=$BL_PID"

# Arm GT (GroundTruth, function_calling)
echo ""
echo "--- Arm GT: GroundTruth (function_calling) ---"
GT_DIR="$OUTDIR/arm_GT"
mkdir -p "$GT_DIR"
nohup python3 -m sweagent run-batch \
    --config config/canary_gt_qwen_fc.yaml \
    --instances.subset verified \
    --instances.split test \
    --instances.filter "$TASKS" \
    --output_dir "$GT_DIR" \
    --num_workers 4 \
    > "$GT_DIR/run.log" 2>&1 &
GT_PID=$!
echo "  PID=$GT_PID"

echo ""
echo "=== Both arms launched ==="
echo "BL PID: $BL_PID"
echo "GT PID: $GT_PID"
echo "Output: $OUTDIR"
echo ""
echo "Monitor:"
echo "  tail -f $BL_DIR/run.log"
echo "  tail -f $GT_DIR/run.log"
echo ""
echo "Wait:  wait $BL_PID $GT_PID"
