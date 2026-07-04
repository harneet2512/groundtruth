#!/bin/bash
# VM Setup for GT vNext 4-arm benchmark comparison
# Contract: benchmarks/swebench/fast_diag/GT_VNEXT_BENCHMARK_CONTRACT.md
#
# Run this on the VM (gt-runner-gcp) ONCE before launching arms.
#
# Prerequisites:
#   - gcloud SSH access to gt-runner-gcp
#   - Git push from local: git push origin baseline-oh-qwen3coder-live-lite-2026-04-20
#   - Qwen API key (Vertex AI MaaS or OpenRouter)
#
# Usage:
#   gcloud compute ssh gt-runner-gcp --zone=us-central1-a
#   bash vm_setup_vnext.sh

set -euo pipefail

echo "=== GT vNext VM Setup ==="
echo "Date: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo ""

# ── 1. Check disk ──
echo "--- Disk check ---"
df -h / | tail -1
AVAIL=$(df / --output=avail -BG | tail -1 | tr -d ' G')
if [ "$AVAIL" -lt 200 ]; then
    echo "ERROR: Need 200GB+ free, have ${AVAIL}GB. Resize disk first."
    exit 1
fi
echo "OK: ${AVAIL}GB available"
echo ""

# ── 2. Clone/update repo ──
echo "--- Repo setup ---"
REPO_DIR="$HOME/Groundtruth"
BRANCH="baseline-oh-qwen3coder-live-lite-2026-04-20"

if [ -d "$REPO_DIR/.git" ]; then
    echo "Repo exists, fetching..."
    cd "$REPO_DIR"
    git fetch origin
    git checkout "$BRANCH"
    git pull origin "$BRANCH"
else
    echo "Cloning repo..."
    git clone --branch "$BRANCH" https://github.com/harneet2512/Groundtruth.git "$REPO_DIR"
    cd "$REPO_DIR"
fi
echo "Commit: $(git rev-parse HEAD)"
echo ""

# ── 3. Python environment ──
echo "--- Python setup ---"
python3 -m venv "$REPO_DIR/.venv" 2>/dev/null || true
source "$REPO_DIR/.venv/bin/activate"
pip install -q -e "$REPO_DIR" 2>/dev/null || pip install -q -e "$REPO_DIR[dev]" 2>/dev/null || true
pip install -q minisweagent swebench 2>/dev/null || true
echo "Python: $(python3 --version)"
echo ""

# ── 4. Docker check ──
echo "--- Docker check ---"
docker info 2>/dev/null | grep "Server Version" || { echo "ERROR: Docker not available"; exit 1; }
RUNNING=$(docker ps -q | wc -l)
if [ "$RUNNING" -gt 0 ]; then
    echo "WARNING: $RUNNING Docker containers already running"
    docker ps --format "table {{.ID}}\t{{.Image}}\t{{.Status}}"
fi
echo ""

# ── 5. Go binary check ──
echo "--- gt-index binary ---"
GT_BIN="$REPO_DIR/gt-index/gt-index-static"
if [ -f "$GT_BIN" ]; then
    echo "OK: $(file $GT_BIN | head -1)"
else
    echo "WARNING: gt-index-static not found at $GT_BIN"
    echo "Building from source..."
    cd "$REPO_DIR/gt-index"
    CGO_ENABLED=1 go build -tags sqlite_fts5 -o gt-index-static ./cmd/gt-index/ 2>/dev/null || echo "Build failed — need Go 1.22+ and GCC"
fi
echo ""

# ── 6. Verify task suite ──
echo "--- Task suite ---"
SUITE="$REPO_DIR/scripts/swebench/frozen_gt_astropy10.txt"
if [ -f "$SUITE" ]; then
    TASK_COUNT=$(wc -l < "$SUITE")
    echo "OK: $TASK_COUNT tasks in frozen_gt_astropy10.txt"
    cat "$SUITE"
else
    echo "ERROR: $SUITE not found"
    exit 1
fi
echo ""

# ── 7. API key check ──
echo "--- API key check ---"
if [ -n "${OPENROUTER_API_KEY:-}" ]; then
    echo "OK: OPENROUTER_API_KEY is set (${#OPENROUTER_API_KEY} chars)"
elif [ -n "${VERTEX_AI_KEY:-}" ]; then
    echo "OK: VERTEX_AI_KEY is set"
elif gcloud auth print-access-token &>/dev/null; then
    echo "OK: gcloud authenticated (can use Vertex AI)"
else
    echo "WARNING: No API key found. Set OPENROUTER_API_KEY or configure Vertex AI."
    echo "  export OPENROUTER_API_KEY=sk-or-..."
fi
echo ""

# ── 8. Contract summary ──
echo "=== Frozen Contract Summary ==="
echo "Tasks:       frozen_gt_astropy10 (10 astropy)"
echo "Model:       qwen3-coder-480b-a35b-instruct-maas"
echo "Temperature: 0.7"
echo "Step limit:  250"
echo "Cost limit:  \$3.00/task"
echo "Cmd timeout: 60s"
echo "Workers:     4"
echo "GT_MAX_FILES: 5000"
echo "Commit:      $(git rev-parse --short HEAD)"
echo ""
echo "Arms: B (baseline), C (shell-only), F1 (vNext noLSP), F2 (vNext LSP)"
echo ""
echo "=== Setup complete. Ready for arm launches. ==="
