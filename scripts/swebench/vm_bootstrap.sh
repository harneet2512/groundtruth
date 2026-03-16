#!/usr/bin/env bash
# Reproducible VM bootstrap for SWE-bench on GCP (Ubuntu 22.04).
# Run once after SSH into the VM.
set -euo pipefail

echo "=== System packages ==="
sudo apt-get update
sudo apt-get install -y git docker.io python3.11 python3.11-venv python3-pip build-essential curl jq

echo "=== Docker ==="
sudo systemctl start docker || true
sudo systemctl enable docker || true
sudo usermod -aG docker "$USER" 2>/dev/null || true

echo "=== Python venv ==="
PYVENV="${PYVENV:-$HOME/gt-venv}"
python3.11 -m venv "$PYVENV"
# shellcheck source=/dev/null
source "$PYVENV/bin/activate"

echo "=== Clone repo (if not already) ==="
REPO_DIR="${REPO_DIR:-$HOME/groundtruth}"
if [ ! -d "$REPO_DIR" ]; then
  git clone https://github.com/harneet2512/groundtruth.git "$REPO_DIR"
fi
cd "$REPO_DIR"

echo "=== Install deps ==="
pip install -e ".[dev,benchmark]"

echo "=== Load API key ==="
if [ -f .env ]; then
  set -a
  # shellcheck source=/dev/null
  grep -v '^#' .env | grep -v '^$' | while read -r line; do export "$line"; done
  set +a
fi

echo "=== Sanity checks ==="
python3 -m benchmarks.swebench.runner --help
python3 -m pytest tests/ -x -q --timeout=30 -k "not real_lsp" 2>/dev/null || echo "WARN: some tests failed (ok for bootstrap)"

echo "Bootstrap complete. Activate venv: source $PYVENV/bin/activate"
