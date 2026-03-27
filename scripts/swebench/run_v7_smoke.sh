#!/bin/bash
set -euo pipefail

# GT v7 Smoke Test using mini-swe-agent
# Runs N SWE-bench Lite tasks with gt_hook.py injected via setup_commands.
#
# Usage:
#   bash run_v7_smoke.sh [--tasks 10] [--workers 2] [--model openai/qwen3-coder]

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
OH_DIR="$HOME/oh-benchmarks"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
NUM_TASKS=10
NUM_WORKERS=2
MODEL="openai/qwen3-coder"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --tasks)   NUM_TASKS="$2"; shift 2 ;;
        --workers) NUM_WORKERS="$2"; shift 2 ;;
        --model)   MODEL="$2"; shift 2 ;;
        *) shift ;;
    esac
done

OUTPUT_DIR="$HOME/results/v7_smoke_${NUM_TASKS}t_${TIMESTAMP}"
GT_HOOK="$REPO_DIR/benchmarks/swebench/gt_hook.py"
GT_CONFIG="$REPO_DIR/benchmarks/swebench/mini_swebench_gt_v7.yaml"

# Validate
if [ ! -f "$GT_HOOK" ]; then
    echo "ERROR: gt_hook.py not found at $GT_HOOK"
    exit 1
fi
if [ ! -f "$GT_CONFIG" ]; then
    echo "ERROR: mini_swebench_gt_v7.yaml not found at $GT_CONFIG"
    exit 1
fi

# Check proxy
if ! curl -s --max-time 3 http://localhost:4000/health > /dev/null 2>&1; then
    echo "ERROR: litellm proxy not running on port 4000."
    echo "Start it with: cd $OH_DIR && uv run litellm --config /tmp/litellm_config.yaml --port 4000"
    exit 1
fi
echo "Proxy: OK"

echo "gt_hook.py: $(wc -c < "$GT_HOOK") bytes"
mkdir -p "$OUTPUT_DIR"

# Base64 encode gt_hook.py for injection
GT_B64=$(base64 -w0 "$GT_HOOK")

# Create a temporary config that includes setup_commands to inject gt_hook.py
# We write gt_hook.py into the container via base64 in setup_commands
CHUNK_SIZE=8000
TEMP_CONFIG="$OUTPUT_DIR/config_v7.yaml"

# Split base64 into chunks for shell safety
python3 << PYEOF
import base64, os, yaml

# Read the base config
with open("$GT_CONFIG") as f:
    config = yaml.safe_load(f)

# Read gt_hook.py and create injection commands
with open("$GT_HOOK", "rb") as f:
    gt_bytes = f.read()

gt_b64 = base64.b64encode(gt_bytes).decode()
chunk_size = $CHUNK_SIZE
chunks = [gt_b64[i:i+chunk_size] for i in range(0, len(gt_b64), chunk_size)]

# Build setup commands
setup_cmds = []
for i, chunk in enumerate(chunks):
    op = ">" if i == 0 else ">>"
    setup_cmds.append(f"echo -n '{chunk}' {op} /tmp/gt_hook.b64")
setup_cmds.append("base64 -d /tmp/gt_hook.b64 > /tmp/gt_hook.py && rm -f /tmp/gt_hook.b64")

# Add to config
config.setdefault("environment", {})["setup_commands"] = setup_cmds

# Set model base_url for litellm proxy
config.setdefault("model", {})["base_url"] = "http://172.17.0.1:4000/v1"
config["model"]["api_key"] = "dummy"

with open("$TEMP_CONFIG", "w") as f:
    yaml.dump(config, f, default_flow_style=False, width=200)

print(f"Config written: {len(chunks)} chunks for {len(gt_bytes)} byte hook")
PYEOF

if [ $? -ne 0 ]; then
    echo "ERROR: Failed to create config. Installing pyyaml..."
    cd "$OH_DIR"
    uv add pyyaml 2>&1 | tail -3
    # Retry with uv run
    cd "$REPO_DIR"
    uv run python3 -c "
import base64, os, sys
sys.path.insert(0, '$OH_DIR/.venv/lib/python3.12/site-packages')
import yaml

with open('$GT_CONFIG') as f:
    config = yaml.safe_load(f)
with open('$GT_HOOK', 'rb') as f:
    gt_bytes = f.read()
gt_b64 = base64.b64encode(gt_bytes).decode()
chunks = [gt_b64[i:i+$CHUNK_SIZE] for i in range(0, len(gt_b64), $CHUNK_SIZE)]
setup_cmds = []
for i, chunk in enumerate(chunks):
    op = '>' if i == 0 else '>>'
    setup_cmds.append(f\"echo -n '{chunk}' {op} /tmp/gt_hook.b64\")
setup_cmds.append('base64 -d /tmp/gt_hook.b64 > /tmp/gt_hook.py && rm -f /tmp/gt_hook.b64')
config.setdefault('environment', {})['setup_commands'] = setup_cmds
config.setdefault('model', {})['base_url'] = 'http://172.17.0.1:4000/v1'
config['model']['api_key'] = 'dummy'
with open('$TEMP_CONFIG', 'w') as f:
    yaml.dump(config, f, default_flow_style=False, width=200)
print(f'Config written: {len(chunks)} chunks')
"
fi

echo ""
echo "================================================="
echo "  GT v7 Smoke Test (mini-swe-agent)"
echo "  Started:  $(date -u) UTC"
echo "  Output:   $OUTPUT_DIR"
echo "  Workers:  $NUM_WORKERS"
echo "  Tasks:    $NUM_TASKS"
echo "  Model:    $MODEL"
echo "================================================="
echo ""

cd "$OH_DIR"
export PATH="$HOME/.local/bin:$PATH"

uv run mini-extra swebench \
    -c "$TEMP_CONFIG" \
    --model "$MODEL" \
    --subset lite \
    --split test \
    --slice "0:$NUM_TASKS" \
    -w "$NUM_WORKERS" \
    -o "$OUTPUT_DIR" \
    2>&1 | tee "$OUTPUT_DIR/run.log"

echo ""
echo "================================================="
echo "  Smoke test complete: $(date -u) UTC"
echo "================================================="
echo ""

# Analyze results
if [ -f "$OUTPUT_DIR/results.json" ] || [ -f "$OUTPUT_DIR/predictions.jsonl" ]; then
    echo "Results available at: $OUTPUT_DIR"
    ls -la "$OUTPUT_DIR/"
fi

# Check for hook logs in Docker containers (mini-swe-agent may leave containers)
echo ""
echo "=== Checking for GT hook logs ==="
GT_LOG_DIR="$OUTPUT_DIR/gt_logs"
mkdir -p "$GT_LOG_DIR"

for container_id in $(docker ps -a --filter "label=mini-swe-agent" -q 2>/dev/null); do
    instance_id=$(docker inspect --format '{{.Name}}' "$container_id" | tr -d '/')
    docker cp "$container_id:/tmp/gt_hook_log.jsonl" "$GT_LOG_DIR/${instance_id}.jsonl" 2>/dev/null && \
        echo "  Extracted: ${instance_id}" || true
done

if [ -d "$GT_LOG_DIR" ] && [ "$(ls -A "$GT_LOG_DIR" 2>/dev/null)" ]; then
    echo ""
    echo "=== GT v7 Hook Analysis ==="
    python3 "$SCRIPT_DIR/analyze_hook_logs.py" "$GT_LOG_DIR" --detail
else
    echo "No hook logs found. The agent may not have called understand/verify."
fi
