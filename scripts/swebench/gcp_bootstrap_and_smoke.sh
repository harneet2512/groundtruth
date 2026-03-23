#!/usr/bin/env bash
# GCP VM bootstrap + OpenHands + Qwen + gt_check smoke test.
#
# Run this ON the GCP VM after creation.
# Does everything: installs deps, clones repos, builds images, runs smoke test.
#
# Usage:
#   bash gcp_bootstrap_and_smoke.sh
#   bash gcp_bootstrap_and_smoke.sh --skip-build    # skip image building if already done
#   bash gcp_bootstrap_and_smoke.sh --smoke-only     # only run smoke test
set -euo pipefail

REPO_BRANCH="claude/openhands-qwen-gtcheck-v1-H0CP8"
REPO_URL="https://github.com/harneet2512/groundtruth.git"
GT_DIR="$HOME/groundtruth"
OH_DIR="$HOME/oh-benchmarks"
PROJECT="regal-scholar-442803-e1"

SKIP_BUILD=false
SMOKE_ONLY=false
for arg in "$@"; do
    case $arg in
        --skip-build) SKIP_BUILD=true ;;
        --smoke-only) SMOKE_ONLY=true ;;
    esac
done

log() { echo "[$(date '+%H:%M:%S')] $*"; }

# ═══════════════════════════════════════════════════════════════════════
# PHASE 1: System Dependencies
# ═══════════════════════════════════════════════════════════════════════

if [ "$SMOKE_ONLY" = false ]; then

log "=== Phase 1: System Dependencies ==="

# Docker
if ! command -v docker &>/dev/null; then
    log "Installing Docker..."
    curl -fsSL https://get.docker.com | sh
    sudo usermod -aG docker "$USER"
    log "Docker installed. You may need to re-login for group membership."
fi
sudo systemctl start docker 2>/dev/null || true
docker info > /dev/null 2>&1 || { log "ERROR: Docker not running"; exit 1; }
log "Docker: $(docker --version)"

# Python + uv
if ! command -v uv &>/dev/null; then
    log "Installing uv..."
    curl -LsSf https://astral.sh/uv/install.sh | sh
fi
source "$HOME/.local/bin/env" 2>/dev/null || export PATH="$HOME/.local/bin:$PATH"
log "uv: $(uv --version)"

# Git
sudo apt-get update -qq && sudo apt-get install -y -qq git jq curl > /dev/null 2>&1
log "git: $(git --version)"

# ═══════════════════════════════════════════════════════════════════════
# PHASE 2: Clone Repos
# ═══════════════════════════════════════════════════════════════════════

log "=== Phase 2: Clone Repos ==="

if [ ! -d "$GT_DIR" ]; then
    log "Cloning GroundTruth..."
    git clone -b "$REPO_BRANCH" "$REPO_URL" "$GT_DIR"
else
    log "GroundTruth already at $GT_DIR, pulling..."
    cd "$GT_DIR"
    git fetch origin "$REPO_BRANCH"
    git checkout "$REPO_BRANCH"
    git pull origin "$REPO_BRANCH"
fi

if [ ! -d "$OH_DIR" ]; then
    log "Cloning OpenHands benchmarks..."
    git clone https://github.com/OpenHands/benchmarks.git "$OH_DIR"
    cd "$OH_DIR"
    git submodule update --init --recursive
else
    log "OpenHands benchmarks already at $OH_DIR"
    cd "$OH_DIR"
    git pull --rebase 2>/dev/null || true
fi

# Install OpenHands deps
cd "$OH_DIR"
uv sync 2>&1 | tail -5
log "OpenHands deps installed"

# ═══════════════════════════════════════════════════════════════════════
# PHASE 3: Vertex AI + litellm
# ═══════════════════════════════════════════════════════════════════════

log "=== Phase 3: Vertex AI + litellm ==="

# Verify gcloud ADC
if ! gcloud auth application-default print-access-token --project="$PROJECT" > /dev/null 2>&1; then
    log "Setting up Vertex AI credentials..."
    gcloud auth application-default login --project="$PROJECT"
fi
log "Vertex AI credentials: OK"

# Install litellm
cd "$OH_DIR"
uv pip install "litellm[proxy]" 2>&1 | tail -3

# Start litellm proxy
pkill -f "litellm.*--port 4000" 2>/dev/null || true
sleep 1

LITELLM_CONFIG="$GT_DIR/scripts/swebench/litellm_qwen_gtcheck.yaml"
nohup uv run litellm --config "$LITELLM_CONFIG" --port 4000 --host 0.0.0.0 > /tmp/litellm_proxy.log 2>&1 &
log "litellm proxy starting..."

for i in $(seq 1 20); do
    if curl -s http://localhost:4000/health > /dev/null 2>&1; then
        log "litellm proxy: healthy"
        break
    fi
    sleep 2
done

# Quick connectivity test
log "Testing Vertex AI connectivity..."
RESPONSE=$(curl -s http://localhost:4000/v1/chat/completions \
    -H "Content-Type: application/json" \
    -d '{"model":"qwen3-coder","messages":[{"role":"user","content":"Say OK"}],"max_tokens":5}' 2>&1)
if echo "$RESPONSE" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d['choices'][0]['message']['content'])" 2>/dev/null; then
    log "Vertex AI: connected"
else
    log "WARNING: Vertex AI connectivity test failed. Response: $RESPONSE"
    log "Continuing anyway — may need manual gcloud auth."
fi

# ═══════════════════════════════════════════════════════════════════════
# PHASE 4: Build SWE-bench Images
# ═══════════════════════════════════════════════════════════════════════

SMOKE_TASKS="django__django-12856
django__django-14608
sympy__sympy-17655
django__django-10914
django__django-11179"

if [ "$SKIP_BUILD" = false ]; then
    log "=== Phase 4: Build SWE-bench Images ==="

    SMOKE_FILE=$(mktemp /tmp/smoke_instances_XXXXXX.txt)
    echo "$SMOKE_TASKS" > "$SMOKE_FILE"

    cd "$OH_DIR"
    log "Building Docker images for smoke test tasks..."
    uv run python -m benchmarks.swebench.build_images \
        --dataset princeton-nlp/SWE-bench_Lite \
        --split test \
        --image ghcr.io/openhands/eval-agent-server \
        --target source-minimal \
        --instances "$SMOKE_FILE" \
        2>&1 | tail -30

    rm -f "$SMOKE_FILE"

    # Bake gt_tool.py into images
    log "Baking gt_tool.py into eval images..."
    bash "$GT_DIR/scripts/swebench/build_gt_images.sh"
else
    log "Skipping image build (--skip-build)"
fi

fi  # end of SMOKE_ONLY=false block

# ═══════════════════════════════════════════════════════════════════════
# PHASE 5: Smoke Test
# ═══════════════════════════════════════════════════════════════════════

log "=== Phase 5: Smoke Test (5 tasks) ==="

cd "$OH_DIR"
source "$HOME/.local/bin/env" 2>/dev/null || true

# Ensure litellm proxy is running
if ! curl -s http://localhost:4000/health > /dev/null 2>&1; then
    LITELLM_CONFIG="$GT_DIR/scripts/swebench/litellm_qwen_gtcheck.yaml"
    nohup uv run litellm --config "$LITELLM_CONFIG" --port 4000 --host 0.0.0.0 > /tmp/litellm_proxy.log 2>&1 &
    sleep 10
fi

# Copy configs
mkdir -p "$OH_DIR/.llm_config" "$OH_DIR/benchmarks/swebench/prompts"
cp "$GT_DIR/benchmarks/swebench/.llm_config/qwen_gtcheck.json" "$OH_DIR/.llm_config/"
cp "$GT_DIR/benchmarks/swebench/prompts/gt_check_only.j2" "$OH_DIR/benchmarks/swebench/prompts/"

# Prepare OpenHands GT config
BRIDGE_PATH="$GT_DIR/benchmarks/swebench/gt_mcp_bridge.py"
OH_CONFIG="$OH_DIR/openhands_config_qwen_gt.toml"
cp "$GT_DIR/benchmarks/swebench/openhands_config_qwen_gt.toml" "$OH_CONFIG"
sed -i "s|BRIDGE_PATH_PLACEHOLDER|$BRIDGE_PATH|g" "$OH_CONFIG"

# Prepare instances file
SMOKE_FILE="/tmp/smoke_instances.txt"
cat > "$SMOKE_FILE" << 'TASKEOF'
django__django-12856
django__django-14608
sympy__sympy-17655
django__django-10914
django__django-11179
TASKEOF

TIMESTAMP=$(date '+%Y%m%d_%H%M%S')
OUTPUT_DIR="$GT_DIR/results/smoke_gt_$TIMESTAMP"
mkdir -p "$OUTPUT_DIR"

log "Running smoke test with GT (5 tasks, 1 worker)..."
log "Output: $OUTPUT_DIR"
log "Config: $OH_CONFIG"

uv run swebench-infer \
    .llm_config/qwen_gtcheck.json \
    --dataset princeton-nlp/SWE-bench_Lite \
    --split test \
    --max-iterations 100 \
    --prompt-path gt_check_only.j2 \
    --workspace docker \
    --config "$OH_CONFIG" \
    --select "$SMOKE_FILE" \
    --output-dir "$OUTPUT_DIR" \
    2>&1 | tee "$OUTPUT_DIR/run.log"

log "Smoke test complete. Results at $OUTPUT_DIR"

# ═══════════════════════════════════════════════════════════════════════
# PHASE 6: Analyze Results
# ═══════════════════════════════════════════════════════════════════════

log "=== Phase 6: Analyze Results ==="

# Check if any trajectories were generated
TRAJ_COUNT=$(find "$OUTPUT_DIR" -name "*.json" -o -name "*.jsonl" 2>/dev/null | wc -l)
log "Trajectory files: $TRAJ_COUNT"

# Check for gt_check calls in trajectories
GT_CHECK_COUNT=0
if [ "$TRAJ_COUNT" -gt 0 ]; then
    GT_CHECK_COUNT=$(grep -rl "groundtruth_check" "$OUTPUT_DIR" 2>/dev/null | wc -l)
fi
log "Tasks with gt_check calls: $GT_CHECK_COUNT"

# Summary
echo ""
echo "════════════════════════════════════════════════"
echo "  SMOKE TEST SUMMARY"
echo "════════════════════════════════════════════════"
echo "  Tasks attempted:    5"
echo "  Trajectory files:   $TRAJ_COUNT"
echo "  gt_check usage:     $GT_CHECK_COUNT / 5"
echo "  Results dir:        $OUTPUT_DIR"
echo "════════════════════════════════════════════════"
echo ""
echo "Next steps:"
echo "  1. Review trajectories in $OUTPUT_DIR"
echo "  2. Check gt_check outputs: grep -r 'STRUCTURAL VIOLATION' $OUTPUT_DIR"
echo "  3. If stable, run 10-task warm test:"
echo "     bash $GT_DIR/scripts/swebench/openhands_run_qwen_gt.sh --max-iterations 100"
echo ""

rm -f "$SMOKE_FILE"
