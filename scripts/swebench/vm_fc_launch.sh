#!/bin/bash
# Self-contained launch: fix project, restart proxy, preflight, launch arms.
# Usage on VM: nohup bash /tmp/Groundtruth_vnext/scripts/swebench/vm_fc_launch.sh > /tmp/fc_launch.log 2>&1 &

set -euo pipefail

REPO="/tmp/Groundtruth_vnext"
SWEAGENT="/tmp/SWE-agent"
TIMESTAMP=$(date +%s)
OUTDIR="/tmp/fc_comparison_$TIMESTAMP"
PROJECT=$(gcloud config get project 2>/dev/null || echo "project-c9a6fdd8-8d56-4e88-ad6")

echo "=== FC Launch ==="
echo "Time: $(date -u)"
echo "Project: $PROJECT"

source ~/sweagent-env/bin/activate

# Kill stale
kill $(pgrep -f "litellm --config" 2>/dev/null) 2>/dev/null || true
kill $(pgrep -f "sweagent run-batch" 2>/dev/null) 2>/dev/null || true
sleep 3

# Write corrected proxy config with correct project
cat > /tmp/litellm_fc.yaml << LCFG
model_list:
  - model_name: qwen3-coder-480b-a35b-instruct-maas
    litellm_params:
      model: vertex_ai/qwen/qwen3-coder-480b-a35b-instruct-maas
      vertex_project: $PROJECT
      vertex_location: us-south1
      supports_function_calling: true
LCFG

echo "--- Proxy config ---"
cat /tmp/litellm_fc.yaml

# Start proxy
nohup litellm --config /tmp/litellm_fc.yaml --port 4000 > /tmp/litellm_fc_run.log 2>&1 &
PROXY_PID=$!
echo "Proxy PID: $PROXY_PID"
sleep 12

# Health
if curl -sf http://localhost:4000/health > /dev/null; then
    echo "Proxy: HEALTHY"
else
    echo "Proxy: FAILED"
    tail -20 /tmp/litellm_fc_run.log
    exit 1
fi

# Basic completion test
echo ""
echo "--- Basic completion test ---"
BASIC=$(curl -sf --max-time 60 http://localhost:4000/v1/chat/completions \
    -H "Content-Type: application/json" \
    -d "{\"model\":\"qwen3-coder-480b-a35b-instruct-maas\",\"messages\":[{\"role\":\"user\",\"content\":\"Say hello\"}],\"max_tokens\":20,\"temperature\":0.0}") || {
    echo "FAIL"
    tail -10 /tmp/litellm_fc_run.log
    exit 1
}
echo "OK: $(echo $BASIC | python3 -c 'import sys,json;print(json.load(sys.stdin)["choices"][0]["message"]["content"][:60])' 2>/dev/null || echo 'parse error')"

# Function calling test
echo ""
echo "--- Function calling test ---"
FC=$(curl -sf --max-time 60 http://localhost:4000/v1/chat/completions \
    -H "Content-Type: application/json" \
    -d '{"model":"qwen3-coder-480b-a35b-instruct-maas","messages":[{"role":"system","content":"Use the tools."},{"role":"user","content":"List files in /testbed"}],"tools":[{"type":"function","function":{"name":"bash","description":"Run bash command","parameters":{"type":"object","properties":{"command":{"type":"string"}},"required":["command"]}}}],"max_tokens":300,"temperature":0.0}') || {
    echo "FAIL: curl error"
    tail -10 /tmp/litellm_fc_run.log
    exit 1
}

python3 << 'PYEOF'
import json, sys
r = json.loads("""PLACEHOLDER""")
PYEOF

python3 -c "
import json,sys
r = json.loads(sys.argv[1])
msg = r['choices'][0]['message']
tc = msg.get('tool_calls',[])
if tc:
    print(f'PASS: {len(tc)} tool_call(s)')
    for t in tc:
        fn = t.get('function',{})
        print(f'  -> {fn.get(\"name\",\"?\")}({fn.get(\"arguments\",\"\")})')
else:
    c = msg.get('content','')[:200]
    print(f'WARN: no tool_calls, got text: {c}')
    print('Proceeding anyway — thought_action may still work as fallback')
" "$FC"

# Copy configs
echo ""
echo "--- Setup ---"
cp "$REPO/benchmarks/swebench/canary_gt_qwen_fc.yaml" "$SWEAGENT/config/"
cp "$REPO/benchmarks/swebench/canary_nogt_qwen_fc.yaml" "$SWEAGENT/config/"
echo "Configs copied"

export OPENAI_API_BASE="http://172.17.0.1:4000/v1"
export OPENAI_API_KEY="dummy"

TASKS=$(paste -sd'|' "$REPO/scripts/swebench/frozen_gt_astropy10.txt")
mkdir -p "$OUTDIR"

# Launch arms
echo ""
echo "=== Launching arms ==="

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
echo "BL PID=$BL_PID"

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
echo "GT PID=$GT_PID"

echo ""
echo "=== LAUNCHED ==="
echo "Output: $OUTDIR"
echo "BL: $BL_DIR (PID $BL_PID)"
echo "GT: $GT_DIR (PID $GT_PID)"
echo "Monitor: tail -f $OUTDIR/arm_*/run.log"
