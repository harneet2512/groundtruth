#!/usr/bin/env bash
# OpenHands VM setup for SWE-bench A/B testing with GroundTruth MCP bridge.
# Extends vm_bootstrap.sh — run that first, then this.
#
# Usage: bash scripts/swebench/openhands_setup_vm.sh
set -euo pipefail

REPO_DIR="${REPO_DIR:-$HOME/groundtruth}"
PYVENV="${PYVENV:-$HOME/gt-venv}"

echo "=== OpenHands VM Setup ==="
echo "Repo: $REPO_DIR"
echo "Venv: $PYVENV"

# ── Activate venv ─────────────────────────────────────────────────────
if [ -f "$PYVENV/bin/activate" ]; then
    # shellcheck source=/dev/null
    source "$PYVENV/bin/activate"
else
    echo "ERROR: venv not found at $PYVENV. Run vm_bootstrap.sh first."
    exit 1
fi

# ── Install OpenHands ─────────────────────────────────────────────────
echo ""
echo "=== Installing OpenHands ==="
pip install --upgrade openhands-ai 2>&1 | tail -5
echo "OpenHands version: $(python3 -c 'import openhands; print(openhands.__version__)' 2>/dev/null || echo 'import check failed — trying CLI')"
# Some versions expose version via CLI only
openhands --version 2>/dev/null || true

# ── Install MCP for bridge ────────────────────────────────────────────
echo ""
echo "=== Installing MCP (FastMCP for bridge) ==="
pip install --upgrade "mcp[cli]" 2>&1 | tail -3
python3 -c "from mcp.server.fastmcp import FastMCP; print('FastMCP import OK')"

# ── Verify Docker ─────────────────────────────────────────────────────
echo ""
echo "=== Verifying Docker ==="
docker info > /dev/null 2>&1 || { echo "ERROR: Docker not available. Install Docker first."; exit 1; }
echo "Docker OK — $(docker --version)"

# ── Patch config with actual bridge path ──────────────────────────────
echo ""
echo "=== Patching OpenHands configs ==="
BRIDGE_PATH="$REPO_DIR/benchmarks/swebench/gt_mcp_bridge.py"
GT_CONFIG="$REPO_DIR/benchmarks/swebench/openhands_config_gt.toml"

if [ -f "$GT_CONFIG" ]; then
    sed -i "s|BRIDGE_PATH_PLACEHOLDER|$BRIDGE_PATH|g" "$GT_CONFIG"
    echo "Patched bridge path in $GT_CONFIG"
    grep "gt_mcp_bridge" "$GT_CONFIG" || true
else
    echo "WARNING: $GT_CONFIG not found"
fi

# ── Verify bridge starts ──────────────────────────────────────────────
echo ""
echo "=== Testing MCP bridge startup ==="
timeout 5 python3 "$BRIDGE_PATH" < /dev/null > /dev/null 2>&1 || true
# Bridge exits immediately with no stdin — that's expected.
# Check it at least imports cleanly:
python3 -c "
import importlib.util, sys
spec = importlib.util.spec_from_file_location('bridge', '$BRIDGE_PATH')
mod = importlib.util.module_from_spec(spec)
# Don't execute — just verify it parses
import ast
with open('$BRIDGE_PATH') as f:
    ast.parse(f.read())
print('Bridge parses OK')
"

# ── Pre-pull smoke test Docker images ─────────────────────────────────
echo ""
echo "=== Pre-pulling smoke test Docker images ==="
echo "This may take a while on first run..."

SMOKE_TASKS=(
    "django__django-12856"
    "django__django-13158"
    "sympy__sympy-17655"
    "django__django-10914"
)

for task in "${SMOKE_TASKS[@]}"; do
    img="sweb.eval.x86_64.${task}:latest"
    if docker image inspect "$img" > /dev/null 2>&1; then
        echo "  $img — already pulled"
    else
        echo "  $img — pulling (or will be built by SWE-bench harness)"
        docker pull "$img" 2>/dev/null || echo "  (not in registry — SWE-bench will build it)"
    fi
done

# ── Load API key ──────────────────────────────────────────────────────
echo ""
echo "=== API Key Check ==="
if [ -n "${OPENAI_API_KEY:-}" ]; then
    echo "OPENAI_API_KEY is set (${#OPENAI_API_KEY} chars)"
elif [ -f "$HOME/gt-env.sh" ]; then
    echo "Sourcing ~/gt-env.sh..."
    # shellcheck source=/dev/null
    source "$HOME/gt-env.sh"
    echo "OPENAI_API_KEY: ${OPENAI_API_KEY:+set (${#OPENAI_API_KEY} chars)}"
else
    echo "WARNING: OPENAI_API_KEY not set. Export it or add to ~/gt-env.sh"
fi

# ── Summary ───────────────────────────────────────────────────────────
echo ""
echo "=== Setup Complete ==="
echo "Next steps:"
echo "  1. Verify OpenHands basics:  bash scripts/swebench/run_smoke_openhands.sh --verify-only"
echo "  2. Smoke test (4 tasks):     bash scripts/swebench/run_smoke_openhands.sh"
echo "  3. Full 300-task run:        bash scripts/swebench/run_300_openhands.sh"
