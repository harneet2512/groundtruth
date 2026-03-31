#!/bin/bash
set -euo pipefail

# Full VM setup for OpenHands evaluation
# Cleans Docker, updates repos, installs deps, updates configs
# Usage: bash oh_vm_setup.sh

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
OH_DIR="$HOME/oh-benchmarks"

echo "============================================"
echo "  OpenHands VM Setup"
echo "  Started: $(date -u) UTC"
echo "============================================"

# --- Step 1: Clean Docker ---
echo ""
echo "=== Step 1: Clean Docker ==="
docker stop $(docker ps -q) 2>/dev/null || true
docker system prune -af --volumes 2>/dev/null || true
echo "Docker cleaned"
echo "Disk: $(df -h / | tail -1)"

# --- Step 2: Update oh-benchmarks ---
echo ""
echo "=== Step 2: Update oh-benchmarks ==="
cd "$OH_DIR"
git fetch origin
git checkout main 2>/dev/null || git checkout master 2>/dev/null || true
git pull
git submodule update --init --recursive
echo "oh-benchmarks at: $(git log --oneline -1)"

# --- Step 3: Install deps ---
echo ""
echo "=== Step 3: Install dependencies ==="
source ~/.local/bin/env 2>/dev/null || true
uv sync --dev 2>/dev/null || make build

# Verify CLI
if uv run swebench-infer --help > /dev/null 2>&1; then
    echo "swebench-infer CLI: OK"
else
    echo "ERROR: swebench-infer not available"
    exit 1
fi

# --- Step 4: Update LLM config ---
echo ""
echo "=== Step 4: Update LLM config ==="
cp "$SCRIPT_DIR/oh_llm_config_vertex_qwen3.json" "$OH_DIR/.llm_config/vertex_qwen3.json"
echo "Updated vertex_qwen3.json (temp=0.7, top_p=0.8)"

# --- Step 5: Copy GT prompt template ---
echo ""
echo "=== Step 5: Copy GT prompt template ==="
cp "$SCRIPT_DIR/prompts/gt_check_only.j2" "$OH_DIR/benchmarks/swebench/prompts/"
echo "Copied gt_check_only.j2"
ls "$OH_DIR/benchmarks/swebench/prompts/"

# --- Step 6: Update groundtruth repo ---
echo ""
echo "=== Step 6: Update groundtruth repo ==="
cd "$REPO_DIR"
git pull 2>/dev/null || echo "Not on a tracking branch, skipping pull"
echo "groundtruth at: $(git log --oneline -1)"

# --- Step 7: Start proxy ---
echo ""
echo "=== Step 7: Start litellm proxy ==="
bash "$SCRIPT_DIR/oh_setup_proxy.sh"

# --- Summary ---
echo ""
echo "============================================"
echo "  VM Setup Complete"
echo "  $(date -u) UTC"
echo "============================================"
echo ""
echo "Disk: $(df -h / | tail -1)"
echo "Docker images: $(docker images | wc -l)"
echo ""
echo "Next steps:"
echo "  1. Build images: cd $OH_DIR && uv run python -m benchmarks.swebench.build_images --dataset princeton-nlp/SWE-bench_Verified --split test --image ghcr.io/openhands/eval-agent-server --target source-minimal --max-workers 4"
echo "  2. Smoke test: bash $SCRIPT_DIR/oh_smoke_test.sh"
echo "  3. Full run: bash $SCRIPT_DIR/oh_run_full.sh --condition baseline --shard $SCRIPT_DIR/instances_a.txt"
