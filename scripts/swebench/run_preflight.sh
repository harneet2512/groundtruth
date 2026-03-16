#!/usr/bin/env bash
# Preflight: model smoke test, then smoke run (2 tasks, 1 worker, both conditions).
# Ensures model and MCP path work before stability or full Lite.
set -euo pipefail

REPO_DIR="${REPO_DIR:-$(cd "$(dirname "$0")/../.." && pwd)}"
cd "$REPO_DIR"

echo "=== 1. Model smoke test ==="
python3 scripts/swebench/resolve_model.py --smoke-test --json >/dev/null
echo "Model OK."

echo "=== 2. Smoke run (baseline + groundtruth_mcp, 2 tasks, 1 worker) ==="
bash scripts/swebench/run_smoke.sh
echo "Preflight done."
