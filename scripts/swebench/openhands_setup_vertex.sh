#!/usr/bin/env bash
# OpenHands VM setup for SWE-bench A/B testing with GroundTruth + Vertex AI.
# Prerequisites: vm_bootstrap.sh already run (Docker, Python, git, gt-venv).
#
# Usage: bash scripts/swebench/openhands_setup_vertex.sh
set -euo pipefail

REPO_DIR="${REPO_DIR:-$HOME/groundtruth}"
OH_DIR="${OH_DIR:-$HOME/oh-benchmarks}"

echo "=== OpenHands VM Setup (Vertex AI) ==="
echo "GT Repo: $REPO_DIR"
echo "OH Dir:  $OH_DIR"

# ── Verify gcloud ADC ─────────────────────────────────────────────────
echo ""
echo "=== Verifying Vertex AI credentials ==="
gcloud auth application-default print-access-token --project=regal-scholar-442803-e1 > /dev/null 2>&1 || {
    echo "ERROR: No Application Default Credentials."
    echo "Run: gcloud auth application-default login"
    exit 1
}
echo "Vertex AI ADC: OK"

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

# ── Install litellm with Vertex AI support ────────────────────────────
echo ""
echo "=== Installing litellm[proxy] for Vertex AI routing ==="
uv pip install "litellm[proxy]" 2>&1 | tail -3
echo "litellm: $(uv run python -c 'import litellm; print(litellm.__version__)' 2>&1)"

# ── Verify Docker ─────────────────────────────────────────────────────
echo ""
echo "=== Verifying Docker ==="
docker info > /dev/null 2>&1 || { echo "ERROR: Docker not available."; exit 1; }
echo "Docker OK — $(docker --version)"

# ── Create LLM config ────────────────────────────────────────────────
echo ""
echo "=== LLM Config (Vertex AI) ==="

LLM_CONFIG="$OH_DIR/.llm_config/vertex_qwen3.json"
mkdir -p "$OH_DIR/.llm_config"

cp "$REPO_DIR/benchmarks/swebench/.llm_config/vertex_qwen3.json" "$LLM_CONFIG"
echo "Created LLM config at $LLM_CONFIG"

# ── Start litellm proxy for Vertex AI routing ─────────────────────────
echo ""
echo "=== Setting up litellm proxy ==="

cat > /tmp/litellm_config.yaml << 'YAMLEOF'
model_list:
  - model_name: "qwen3-coder"
    litellm_params:
      model: "vertex_ai/qwen/qwen3-coder-480b-a35b-instruct-maas"
      vertex_project: "regal-scholar-442803-e1"
      vertex_location: "global"
YAMLEOF

# Kill any existing proxy
pkill -f "litellm.*--port 4000" 2>/dev/null || true
sleep 1

nohup uv run litellm --config /tmp/litellm_config.yaml --port 4000 --host 0.0.0.0 > /tmp/litellm_proxy.log 2>&1 &
PROXY_PID=$!
echo "litellm proxy started (PID: $PROXY_PID)"

# Wait for proxy to be ready
for i in $(seq 1 15); do
    if curl -s http://localhost:4000/health > /dev/null 2>&1; then
        echo "Proxy healthy"
        break
    fi
    sleep 2
done

# Test Vertex AI connectivity via proxy
echo ""
echo "=== Testing Vertex AI connectivity ==="
curl -s http://localhost:4000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"qwen3-coder","messages":[{"role":"user","content":"Say OK"}],"max_tokens":5}' \
  | python3 -c "import sys,json; d=json.load(sys.stdin); print('Vertex AI OK:', d['choices'][0]['message']['content'])"

# ── Copy GT prompt templates ──────────────────────────────────────────
echo ""
echo "=== GT Prompt Templates ==="
mkdir -p "$OH_DIR/benchmarks/swebench/prompts"

GT_PROMPT_SRC="$REPO_DIR/benchmarks/swebench/prompts/gt_phase3.j2"
GT_PROMPT_DST="$OH_DIR/benchmarks/swebench/prompts/gt_phase3.j2"
if [ -f "$GT_PROMPT_SRC" ]; then
    cp "$GT_PROMPT_SRC" "$GT_PROMPT_DST"
    echo "Copied GT prompt to $GT_PROMPT_DST"
else
    echo "WARNING: GT prompt not found at $GT_PROMPT_SRC"
fi

BASELINE_PROMPT_SRC="$REPO_DIR/benchmarks/swebench/prompts/baseline_vertex.j2"
BASELINE_PROMPT_DST="$OH_DIR/benchmarks/swebench/prompts/baseline_vertex.j2"
if [ -f "$BASELINE_PROMPT_SRC" ]; then
    cp "$BASELINE_PROMPT_SRC" "$BASELINE_PROMPT_DST"
    echo "Copied baseline prompt to $BASELINE_PROMPT_DST"
else
    echo "WARNING: Baseline prompt not found at $BASELINE_PROMPT_SRC"
fi

# ── Build Docker images for smoke tasks ──────────────────────────────
echo ""
echo "=== Building Docker images for smoke test tasks ==="
SMOKE_INSTANCES="$OH_DIR/smoke_instances.txt"
cat > "$SMOKE_INSTANCES" << 'EOF'
django__django-12856
django__django-14608
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
echo "=== Setup Complete (Vertex AI) ==="
echo ""
echo "LLM config:      $LLM_CONFIG"
echo "GT prompt:       $GT_PROMPT_DST"
echo "Baseline prompt: $BASELINE_PROMPT_DST"
echo "Smoke instances: $SMOKE_INSTANCES"
echo ""
echo "Next steps:"
echo "  1. Smoke test (4 tasks × 2 conditions):"
echo "     bash $REPO_DIR/scripts/swebench/run_ab_vertex.sh"
echo "  2. Or run individually:"
echo "     bash $REPO_DIR/scripts/swebench/openhands_run_vertex_baseline.sh --instances smoke_instances.txt"
echo "     bash $REPO_DIR/scripts/swebench/openhands_run_vertex_gt.sh --instances smoke_instances.txt"
