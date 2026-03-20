#!/usr/bin/env bash
# OpenHands VM setup for SWE-bench A/B testing with GroundTruth.
# Prerequisites: vm_bootstrap.sh already run (Docker, Python, git, gt-venv).
#
# Usage: bash scripts/swebench/openhands_setup_vm.sh
set -euo pipefail

REPO_DIR="${REPO_DIR:-$HOME/groundtruth}"
OH_DIR="${OH_DIR:-$HOME/oh-benchmarks}"

echo "=== OpenHands VM Setup ==="
echo "GT Repo: $REPO_DIR"
echo "OH Dir:  $OH_DIR"

# ── Install uv ────────────────────────────────────────────────────────
if ! command -v uv &>/dev/null && [ ! -f "$HOME/.local/bin/uv" ]; then
    echo ""
    echo "=== Installing uv ==="
    curl -LsSf https://astral.sh/uv/install.sh | sh
fi
source "$HOME/.local/bin/env" 2>/dev/null || true
echo "uv: $(uv --version)"

# ── Clone OpenHands benchmarks ────────────────────────────────────────
if [ ! -d "$OH_DIR" ]; then
    echo ""
    echo "=== Cloning OpenHands benchmarks ==="
    git clone https://github.com/OpenHands/benchmarks.git "$OH_DIR"
    cd "$OH_DIR"
    git submodule update --init --recursive
else
    echo "OpenHands benchmarks already at $OH_DIR"
    cd "$OH_DIR"
    git pull --rebase 2>/dev/null || true
fi

# ── Install deps ──────────────────────────────────────────────────────
echo ""
echo "=== Installing OpenHands deps (uv sync) ==="
cd "$OH_DIR"
uv sync 2>&1 | tail -5
echo "OpenHands SDK: $(OPENHANDS_SUPPRESS_BANNER=1 uv run python -c 'print("OK")' 2>&1 | tail -1)"

# ── Verify Docker ─────────────────────────────────────────────────────
echo ""
echo "=== Verifying Docker ==="
docker info > /dev/null 2>&1 || { echo "ERROR: Docker not available."; exit 1; }
echo "Docker OK — $(docker --version)"

# ── Create LLM config ────────────────────────────────────────────────
echo ""
echo "=== LLM Config ==="
source "$HOME/gt-env.sh" 2>/dev/null || true

LLM_CONFIG="$OH_DIR/.llm_config/openai_gpt54nano.json"
mkdir -p "$OH_DIR/.llm_config"

if [ -n "${OPENAI_API_KEY:-}" ]; then
    cat > "$LLM_CONFIG" << JSONEOF
{
  "model": "openai/gpt-5.4-nano",
  "api_key": "$OPENAI_API_KEY",
  "temperature": 0
}
JSONEOF
    echo "Created LLM config at $LLM_CONFIG"
else
    echo "WARNING: OPENAI_API_KEY not set. Set it in ~/gt-env.sh"
    echo "LLM config NOT created — will need to be created manually"
fi

# ── Copy GT prompt template ──────────────────────────────────────────
echo ""
echo "=== GT Prompt Template ==="
GT_PROMPT_SRC="$REPO_DIR/benchmarks/swebench/prompts/gt_phase3.j2"
GT_PROMPT_DST="$OH_DIR/benchmarks/swebench/prompts/gt_phase3.j2"
if [ -f "$GT_PROMPT_SRC" ]; then
    cp "$GT_PROMPT_SRC" "$GT_PROMPT_DST"
    echo "Copied to $GT_PROMPT_DST"
else
    echo "WARNING: GT prompt not found at $GT_PROMPT_SRC"
fi

# ── Build Docker images for smoke tasks ──────────────────────────────
echo ""
echo "=== Building Docker images for smoke test tasks ==="
SMOKE_INSTANCES="$OH_DIR/smoke_instances.txt"
cat > "$SMOKE_INSTANCES" << 'EOF'
django__django-12856
django__django-13158
sympy__sympy-17655
django__django-10914
EOF

cd "$OH_DIR"
echo "Building images (this may take a while on first run)..."
uv run python -m benchmarks.swebench.build_images \
    --dataset princeton-nlp/SWE-bench_Lite \
    --split test \
    --image ghcr.io/openhands/eval-agent-server \
    --target source-minimal \
    --instances "$SMOKE_INSTANCES" \
    2>&1 | tail -20 || echo "WARNING: Image build may have failed — check above"

# ── Verify swebench-infer works ──────────────────────────────────────
echo ""
echo "=== Verifying swebench-infer CLI ==="
uv run swebench-infer --help 2>&1 | head -5 || echo "WARNING: swebench-infer not found as CLI entry point"

# ── Summary ──────────────────────────────────────────────────────────
echo ""
echo "=== Setup Complete ==="
echo ""
echo "LLM config:     $LLM_CONFIG"
echo "GT prompt:      $GT_PROMPT_DST"
echo "Smoke instances: $SMOKE_INSTANCES"
echo ""
echo "Next steps:"
echo "  1. Smoke test (4 tasks × 2 conditions):"
echo "     bash $REPO_DIR/scripts/swebench/run_smoke_openhands.sh"
echo "  2. Full 300-task A/B run:"
echo "     bash $REPO_DIR/scripts/swebench/run_300_openhands.sh"
