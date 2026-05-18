#!/bin/bash
# vm_inspect_setup.sh — Lean VM setup for Inspect AI + GroundTruth runs on GCP.
#
# No poetry, no OpenHands, no litellm proxy. Just:
#   - Python 3.12 venv with inspect-ai, inspect-evals, and GT
#   - gt-index binary
#   - Docker (for SWE-bench sandbox containers)
#   - DeepSeek API key verification
#
# Usage:
#   gcloud compute ssh ubuntu@gt-t0 --zone=us-central1-a \
#     --project=project-3d0018fc-54e4-4a4d-97c \
#     --command='bash /home/ubuntu/Groundtruth/scripts/swebench/vm_inspect_setup.sh'
#
# Prerequisites:
#   - GT repo cloned to /home/ubuntu/Groundtruth
#   - gt-index linux binary at /home/ubuntu/Groundtruth/bin/gt-index-linux
#     (build with: bash scripts/swebench/build_gt_index_linux.sh)
#   - DEEPSEEK_API_KEY exported or in ~/.bashrc

set -euo pipefail
exec > >(tee -a /tmp/inspect_setup.log) 2>&1
echo "=== $(date -u) :: vm_inspect_setup begin ==="

GT_DIR="/home/ubuntu/Groundtruth"
VENV_DIR="/home/ubuntu/inspect-venv"

# ── 1. Check Docker ──
echo "--- Docker check ---"
if docker info >/dev/null 2>&1; then
    echo "OK: Docker running ($(docker info --format '{{.ServerVersion}}' 2>/dev/null))"
else
    echo "ERROR: Docker not running. Start with: sudo systemctl start docker"
    exit 1
fi

# ── 2. Python venv + deps ──
echo "--- Python setup ---"
if [ ! -d "$VENV_DIR" ]; then
    python3 -m venv "$VENV_DIR"
fi
source "$VENV_DIR/bin/activate"

pip install --quiet --upgrade pip wheel
pip install --quiet "inspect-ai" "inspect-evals[swe_bench]" datasets openai

# Install GT package
cd "$GT_DIR"
pip install --quiet -e .

echo "Python: $(python3 --version)"
echo "inspect-ai: $(pip show inspect-ai 2>/dev/null | grep Version)"
echo "inspect-evals: $(pip show inspect-evals 2>/dev/null | grep Version)"

# ── 3. gt-index binary ──
echo "--- gt-index binary ---"
GT_BIN="/usr/local/bin/gt-index"
if [ -f "$GT_BIN" ] && [ -x "$GT_BIN" ]; then
    echo "OK: gt-index already at $GT_BIN"
elif [ -f "$GT_DIR/bin/gt-index-linux" ]; then
    sudo cp "$GT_DIR/bin/gt-index-linux" "$GT_BIN"
    sudo chmod +x "$GT_BIN"
    echo "OK: installed gt-index from bin/gt-index-linux"
elif [ -f "$GT_DIR/gt-index/gt-index" ]; then
    sudo cp "$GT_DIR/gt-index/gt-index" "$GT_BIN"
    sudo chmod +x "$GT_BIN"
    echo "OK: installed gt-index from gt-index/gt-index"
else
    echo "WARNING: No gt-index binary found. Build with:"
    echo "  bash scripts/swebench/build_gt_index_linux.sh"
    echo "  OR: cd gt-index && CGO_ENABLED=1 go build -o gt-index ./cmd/gt-index/"
fi

if [ -x "$GT_BIN" ]; then
    "$GT_BIN" --help 2>&1 | head -1 || true
fi

# ── 4. DeepSeek API check ──
echo "--- DeepSeek API check ---"
DS_KEY="${DEEPSEEK_API_KEY:-}"
if [ -z "$DS_KEY" ]; then
    echo "WARNING: DEEPSEEK_API_KEY not set. Export it or add to ~/.bashrc"
    echo "  export DEEPSEEK_API_KEY=sk-..."
else
    HTTP_CODE=$(curl -s -o /tmp/ds_probe.json -w "%{http_code}" \
        -X POST "https://api.deepseek.com/chat/completions" \
        -H "Authorization: Bearer $DS_KEY" \
        -H "Content-Type: application/json" \
        -d '{"model":"deepseek-v4-flash","messages":[{"role":"user","content":"say ok"}],"max_tokens":5,"thinking":{"type":"disabled"}}')
    if [ "$HTTP_CODE" = "200" ]; then
        echo "OK: DeepSeek V4 Flash responds (thinking disabled)"
    else
        echo "WARNING: DeepSeek HTTP $HTTP_CODE"
        cat /tmp/ds_probe.json 2>/dev/null || true
    fi
fi

# ── 5. Inspect smoke check ──
echo "--- Inspect smoke ---"
python3 -c "
from adapters.inspect.task import swebench_gt_baseline, swebench_gt
print('CHECK: baseline task imports OK')
print('CHECK: gt task imports OK')
from adapters.inspect.tools import gt_tools
tools = gt_tools()
print(f'CHECK: {len(tools)} GT tools registered')
for t in tools:
    print(f'  - {t.__name__}')
"

echo ""
echo "=== Setup complete ==="
echo "To run a smoke test:"
echo "  source $VENV_DIR/bin/activate"
echo "  export OPENAI_API_KEY=\$DEEPSEEK_API_KEY"
echo "  inspect eval adapters/inspect/task.py@swebench_gt_baseline \\"
echo "    --model openai/deepseek-v4-flash \\"
echo "    --model-base-url https://api.deepseek.com \\"
echo "    --sample-id beancount__beancount-931 \\"
echo "    --log-dir /tmp/inspect_logs"
echo ""
echo "=== $(date -u) :: vm_inspect_setup done ==="
