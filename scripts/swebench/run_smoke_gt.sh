#!/usr/bin/env bash
# Smoke test: 4 SWE-bench instances with Qwen3-Coder + GroundTruth gt_check.
#
# Usage: bash scripts/swebench/run_smoke_gt.sh
#
# Prerequisites:
#   1. oh-benchmarks cloned and deps installed at ~/oh-benchmarks
#   2. litellm proxy running on port 4000 (this script starts it if not)
#   3. /tmp/regal-key.json (Vertex AI service account)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
OH_DIR="${OH_DIR:-$HOME/oh-benchmarks}"
GT_OBS_LOG="${GT_OBS_LOG:-/tmp/gt_obs.jsonl}"

echo "=== GroundTruth Smoke Test (Qwen3-Coder + gt_check) ==="
echo "Repo:     $REPO_DIR"
echo "OH dir:   $OH_DIR"
echo "Obs log:  $GT_OBS_LOG"
echo ""

# ── Verify prerequisites ─────────────────────────────────────────────
if [ ! -d "$OH_DIR" ]; then
    echo "ERROR: OpenHands benchmarks not found at $OH_DIR"
    echo "Run: git clone https://github.com/OpenHands/benchmarks.git $OH_DIR"
    exit 1
fi

# ── Set Vertex AI credentials ────────────────────────────────────────
export GOOGLE_APPLICATION_CREDENTIALS="${GOOGLE_APPLICATION_CREDENTIALS:-/tmp/regal-key.json}"
if [ ! -f "$GOOGLE_APPLICATION_CREDENTIALS" ]; then
    echo "ERROR: Service account key not found at $GOOGLE_APPLICATION_CREDENTIALS"
    exit 1
fi
echo "Vertex AI creds: $GOOGLE_APPLICATION_CREDENTIALS"

# ── Start litellm proxy if not running ────────────────────────────────
if ! curl -s http://localhost:4000/health > /dev/null 2>&1; then
    echo ""
    echo "Starting litellm proxy..."
    LITELLM_CONFIG="$SCRIPT_DIR/litellm_qwen_gtcheck.yaml"
    if [ ! -f "$LITELLM_CONFIG" ]; then
        echo "ERROR: litellm config not found at $LITELLM_CONFIG"
        exit 1
    fi
    nohup python3 -m litellm --config "$LITELLM_CONFIG" --port 4000 --host 0.0.0.0 > /tmp/litellm_proxy.log 2>&1 &
    echo "Waiting for proxy..."
    for i in $(seq 1 20); do
        if curl -s http://localhost:4000/health > /dev/null 2>&1; then
            echo "Proxy healthy (attempt $i)"
            break
        fi
        if [ "$i" -eq 20 ]; then
            echo "ERROR: litellm proxy did not start. Check /tmp/litellm_proxy.log"
            tail -20 /tmp/litellm_proxy.log 2>/dev/null
            exit 1
        fi
        sleep 2
    done
else
    echo "litellm proxy already running on port 4000"
fi

# ── Verify Vertex AI connectivity ────────────────────────────────────
echo ""
echo "Testing Vertex AI via litellm proxy..."
VERTEX_TEST=$(curl -s http://localhost:4000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer dummy" \
  -d '{"model":"qwen3-coder","messages":[{"role":"user","content":"Say OK"}],"max_tokens":5}' 2>&1)

if echo "$VERTEX_TEST" | python3 -c "import sys,json; d=json.load(sys.stdin); print('Vertex AI OK:', d['choices'][0]['message']['content'])" 2>/dev/null; then
    echo "Vertex AI connectivity verified"
else
    echo "ERROR: Vertex AI test failed:"
    echo "$VERTEX_TEST" | head -5
    exit 1
fi

# ── Clear observability log ──────────────────────────────────────────
echo "" > "$GT_OBS_LOG"
echo "Cleared obs log: $GT_OBS_LOG"

# ── Create smoke instance list ───────────────────────────────────────
SMOKE_INSTANCES=$(mktemp /tmp/gt_smoke_XXXXXX.txt)
cat > "$SMOKE_INSTANCES" << 'EOF'
django__django-12856
django__django-14608
sympy__sympy-17655
django__django-10914
EOF
echo ""
echo "Smoke instances:"
cat "$SMOKE_INSTANCES"

# ── Run evaluation ────────────────────────────────────────────────────
echo ""
echo "=== Starting evaluation ==="
echo "Model: Qwen3-Coder-480B via Vertex AI MaaS"
echo "Params: temp=0.7 top_p=0.8 top_k=20 rep_penalty=1.05"
echo "Max iterations: 100"
echo ""

cd "$OH_DIR"

GT_BRIDGE_PATH="$REPO_DIR/benchmarks/swebench/gt_mcp_bridge.py" \
GT_OBS_LOG="$GT_OBS_LOG" \
GOOGLE_APPLICATION_CREDENTIALS="$GOOGLE_APPLICATION_CREDENTIALS" \
python3 "$REPO_DIR/benchmarks/swebench/run_swebench_gt.py" \
    "$REPO_DIR/benchmarks/swebench/.llm_config/qwen_vertex_litellm.json" \
    --workspace docker \
    --max-iterations 100 \
    --select "$SMOKE_INSTANCES" \
    --num-workers 1 \
    --output-dir "$REPO_DIR/benchmarks/swebench/results/smoke_gt" \
    --note "smoke-gt-check" \
    --prompt-path "$REPO_DIR/benchmarks/swebench/prompts/gt_check_only.j2"

# ── Observability Report ──────────────────────────────────────────────
echo ""
echo "=== GT Observability Report ==="
echo ""

if [ -s "$GT_OBS_LOG" ]; then
    python3 -c "
import json

entries = []
with open('$GT_OBS_LOG') as f:
    for line in f:
        line = line.strip()
        if line:
            entries.append(json.loads(line))

starts = [e for e in entries if e.get('event') == 'instance_start']
calls = [e for e in entries if e.get('tool') == 'groundtruth_check']

print(f'Instances started:     {len(starts)}')
print(f'GT check calls:        {len(calls)}')
print(f'GT utilization rate:   {len(calls)}/{len(starts)} = {len(calls)/max(len(starts),1)*100:.0f}%')
print()

if calls:
    violations = [c for c in calls if c.get('has_violations')]
    errors = [c for c in calls if c.get('is_error')]
    latencies = [c['latency_ms'] for c in calls if 'latency_ms' in c]

    print(f'Calls with violations: {len(violations)}')
    print(f'Calls with errors:     {len(errors)}')
    if latencies:
        print(f'Avg latency:           {sum(latencies)/len(latencies):.0f}ms')
        print(f'Max latency:           {max(latencies)}ms')
    print()
    print('Per-call details:')
    for i, c in enumerate(calls):
        print(f'  [{i+1}] {c.get(\"latency_ms\",\"?\")}ms, '
              f'{c.get(\"response_bytes\",\"?\")}B, '
              f'violations={c.get(\"has_violations\",\"?\")}, '
              f'error={c.get(\"is_error\",\"?\")}')
else:
    print('WARNING: No GT check calls recorded!')
    print('The agent did NOT utilize GroundTruth.')
"
else
    echo "WARNING: Obs log is empty — no GT activity recorded"
fi

# ── Cleanup ──────────────────────────────────────────────────────────
rm -f "$SMOKE_INSTANCES"

echo ""
echo "=== Smoke test complete ==="
echo "Results: $REPO_DIR/benchmarks/swebench/results/smoke_gt/"
echo "Obs log: $GT_OBS_LOG"
