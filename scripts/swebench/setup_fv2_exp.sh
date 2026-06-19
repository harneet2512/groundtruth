#!/bin/bash
set -euo pipefail

echo "=== Foundation v2 Experiment Setup ==="

# Kill old proxy
kill $(cat /tmp/litellm_proxy.pid 2>/dev/null) 2>/dev/null || true
sleep 2

# Restart litellm proxy
cd ~/oh-benchmarks
source ~/.local/bin/env 2>/dev/null || true
nohup uv run litellm --config /tmp/litellm_config.yaml --port 4000 > /tmp/litellm.log 2>&1 &
echo $! > /tmp/litellm_proxy.pid
echo "Proxy PID: $(cat /tmp/litellm_proxy.pid)"
sleep 8

# Check health
echo "=== Proxy Health ==="
HEALTH=$(curl -s http://localhost:4000/health 2>&1)
echo "$HEALTH" | python3 -c '
import sys, json
d = json.load(sys.stdin)
h = d["healthy_count"]
u = d["unhealthy_count"]
print(f"Healthy: {h}, Unhealthy: {u}")
if u > 0:
    for ep in d.get("unhealthy_endpoints", []):
        err = ep.get("error", "unknown")[:300]
        print(f"Error: {err}")
'

# Create experiment directories
mkdir -p ~/foundation_v2/manifests
mkdir -p ~/foundation_v2/canary_a
mkdir -p ~/foundation_v2/canary_b
mkdir -p ~/foundation_v2/condition_a
mkdir -p ~/foundation_v2/condition_b

echo "=== Disk usage ==="
df -h / | tail -1

echo "=== Docker ==="
docker info 2>&1 | grep -E "Server Version|Storage Driver|Total Memory"

echo "=== Setup complete ==="
