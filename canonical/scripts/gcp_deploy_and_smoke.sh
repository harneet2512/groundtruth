#!/bin/bash
# Deploy canonical GT hybrid to GCP and run 10-task smoke test.
#
# Run this ON the GCP VM (gt-fast):
#   bash canonical/scripts/gcp_deploy_and_smoke.sh
#
# Prerequisites:
#   - GCP VM: gt-fast (n2-standard-8, us-west1-a)
#   - Project: project-ed1b5fef-f35f-4251-a77
#   - Docker installed and running
#   - Vertex AI access for DeepSeek V3.2 MaaS

set -euo pipefail

TIMESTAMP=$(date +%Y%m%d_%H%M%S)
REPO_DIR="${REPO_DIR:-$HOME/groundtruth}"
BRANCH="research/vnext-substrate-plan-2026-04-11"
WORKERS=4

echo "================================================================"
echo "GCP DEPLOY + SMOKE TEST"
echo "Timestamp: $TIMESTAMP"
echo "VM: $(hostname)"
echo "Repo: $REPO_DIR"
echo "Branch: $BRANCH"
echo "Workers: $WORKERS"
echo "================================================================"

# ── Step 1: Pull latest code ──────────────────────────────────────────
echo ""
echo "[1/7] Pulling latest code..."
if [ -d "$REPO_DIR" ]; then
    cd "$REPO_DIR"
    git fetch origin
    git checkout "$BRANCH"
    git pull origin "$BRANCH"
else
    git clone https://github.com/harneet2512/groundtruth.git "$REPO_DIR"
    cd "$REPO_DIR"
    git checkout "$BRANCH"
fi
echo "  Branch: $(git branch --show-current)"
echo "  Commit: $(git log --oneline -1)"

# ── Step 2: Install dependencies ──────────────────────────────────────
echo ""
echo "[2/7] Installing dependencies..."

# swe-agent
if ! python3 -c "import sweagent" 2>/dev/null; then
    echo "  Installing swe-agent..."
    pip install swe-agent 2>&1 | tail -3
else
    echo "  swe-agent: already installed"
fi

# litellm
if ! command -v litellm &>/dev/null; then
    echo "  Installing litellm..."
    pip install litellm[proxy] 2>&1 | tail -3
else
    echo "  litellm: already installed"
fi

# datasets
if ! python3 -c "import datasets" 2>/dev/null; then
    echo "  Installing datasets..."
    pip install datasets 2>&1 | tail -3
else
    echo "  datasets: already installed"
fi

# ── Step 3: Check gt-index binary ─────────────────────────────────────
echo ""
echo "[3/7] Checking gt-index binary..."
GT_INDEX=""
for p in /tmp/gt-index-static "$REPO_DIR/gt-index/gt-index-static" "$REPO_DIR/gt-index/gt-index-linux"; do
    if [ -f "$p" ]; then
        GT_INDEX="$p"
        break
    fi
done

if [ -z "$GT_INDEX" ]; then
    echo "  WARNING: gt-index binary not found!"
    echo "  Building from source..."
    if [ -d "$REPO_DIR/gt-index" ] && command -v go &>/dev/null; then
        cd "$REPO_DIR/gt-index"
        CGO_ENABLED=1 go build -o gt-index-static ./cmd/gt-index/ 2>&1 | tail -3
        GT_INDEX="$REPO_DIR/gt-index/gt-index-static"
        cd "$REPO_DIR"
    else
        echo "  ERROR: Cannot build gt-index. Install Go 1.22+ and GCC."
        exit 1
    fi
fi
echo "  gt-index: $GT_INDEX ($(stat -c%s "$GT_INDEX" 2>/dev/null || echo '?') bytes)"
export GT_INDEX_PATH="$GT_INDEX"

# ── Step 4: Check disk space ──────────────────────────────────────────
echo ""
echo "[4/7] Checking disk space..."
FREE_GB=$(df -BG / | tail -1 | awk '{print $4}' | tr -d 'G')
echo "  Free disk: ${FREE_GB}GB"
if [ "$FREE_GB" -lt 30 ]; then
    echo "  WARNING: Less than 30GB free. Consider resizing disk for full run."
fi

# ── Step 5: Start LiteLLM proxy ───────────────────────────────────────
echo ""
echo "[5/7] Starting LiteLLM proxy..."
if curl -sf http://localhost:4000/health > /dev/null 2>&1; then
    echo "  LiteLLM proxy already running"
else
    echo "  Starting litellm proxy in background..."
    nohup litellm --config "$REPO_DIR/canonical/config/litellm_vertex_deepseek.yaml" \
        --port 4000 > /tmp/litellm_proxy.log 2>&1 &
    LITELLM_PID=$!
    echo "  LiteLLM PID: $LITELLM_PID"

    # Wait for proxy to be ready
    for i in $(seq 1 30); do
        if curl -sf http://localhost:4000/health > /dev/null 2>&1; then
            echo "  LiteLLM proxy ready after ${i}s"
            break
        fi
        sleep 1
    done

    if ! curl -sf http://localhost:4000/health > /dev/null 2>&1; then
        echo "  ERROR: LiteLLM proxy failed to start. Check /tmp/litellm_proxy.log"
        cat /tmp/litellm_proxy.log | tail -20
        exit 1
    fi
fi

# ── Step 6: Quick model connectivity test ─────────────────────────────
echo ""
echo "[6/7] Testing model connectivity..."
RESPONSE=$(curl -sf http://localhost:4000/v1/chat/completions \
    -H "Content-Type: application/json" \
    -H "Authorization: Bearer dummy" \
    -d '{
        "model": "openai/deepseek-v3",
        "messages": [{"role": "user", "content": "Say hello in one word."}],
        "max_tokens": 10,
        "temperature": 1.0
    }' 2>&1 || echo "CONNECTIVITY_FAILED")

if echo "$RESPONSE" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d['choices'][0]['message']['content'])" 2>/dev/null; then
    echo "  Model connectivity: OK"
else
    echo "  WARNING: Model connectivity test failed. Response: ${RESPONSE:0:200}"
    echo "  Continuing anyway (may fail during run)..."
fi

# ── Step 7: Run smoke test ────────────────────────────────────────────
echo ""
echo "[7/7] Running 10-task smoke test with $WORKERS workers..."
echo ""

SMOKE_DIR="$REPO_DIR/results/smoke_${TIMESTAMP}"

# Run GT hybrid only (skip baseline for speed — we have baseline data from last_ride)
echo "Running GT HYBRID (canonical path)..."
python3 "$REPO_DIR/canonical/scripts/run_gt_hybrid.py" \
    --config "$REPO_DIR/canonical/config/sweagent_deepseek_v3.2_gt.yaml" \
    --output-dir "$SMOKE_DIR/gt_hybrid" \
    --dataset "SWE-bench-Live/SWE-bench-Live" \
    --split "lite" \
    --max-instances 10 \
    --workers "$WORKERS" \
    2>&1 | tee "$SMOKE_DIR/gt_hybrid_stdout.log"

echo ""
echo "Analyzing telemetry..."
python3 "$REPO_DIR/canonical/scripts/analyze_telemetry.py" \
    --input-dir "$SMOKE_DIR/gt_hybrid" --verbose

echo ""
echo "================================================================"
echo "SMOKE TEST COMPLETE"
echo "Output: $SMOKE_DIR/gt_hybrid/"
echo "Telemetry: $SMOKE_DIR/gt_hybrid/telemetry_report.json"
echo "Summary: $SMOKE_DIR/gt_hybrid/telemetry_summary.json"
echo "================================================================"
echo ""
echo "Next steps:"
echo "  1. scp telemetry_summary.json to local for review"
echo "  2. If PASS: run full 300 with --max-instances 300 --workers $WORKERS"
echo "  3. Convert: python3 canonical/scripts/convert_to_jsonl.py --input-dir $SMOKE_DIR/gt_hybrid"
echo "================================================================"
