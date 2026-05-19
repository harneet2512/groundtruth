#!/bin/bash
set -e

source /home/ubuntu/inspect-venv/bin/activate
pkill -f run_infer 2>/dev/null || true
pkill -f litellm 2>/dev/null || true
docker stop $(docker ps -q) 2>/dev/null || true
sleep 2

# Direct config — OH llm.py is already patched to add thinking:disabled for deepseek-v4
cat > /home/ubuntu/OpenHands/config.toml << 'EOF'
[core]
workspace_base = "/tmp/workspace"

[llm.deepseek_v4_flash]
model = "deepseek/deepseek-v4-flash"
api_key = "${DEEPSEEK_API_KEY}"
base_url = "https://api.deepseek.com"
temperature = 1.0
top_p = 1.0
max_output_tokens = 65536
native_tool_calling = true
caching_prompt = false
drop_params = true
num_retries = 5
timeout = 300
EOF

# Replace env var placeholder with actual key
sed -i "s|\${DEEPSEEK_API_KEY}|${DEEPSEEK_API_KEY}|g" /home/ubuntu/OpenHands/config.toml

# Clean selected_ids
sed -i '/selected_ids/d' /home/ubuntu/OpenHands/evaluation/benchmarks/swe_bench/config.toml 2>/dev/null

echo ""
echo "=== LAUNCHING 1-TASK OH BASELINE ==="
echo "Balance before:"
curl -s https://api.deepseek.com/user/balance -H "Authorization: Bearer $DEEPSEEK_API_KEY" | python3 -c 'import json,sys; print(json.load(sys.stdin)["balance_infos"][0]["total_balance"])'

export DEEPSEEK_API_KEY=$DEEPSEEK_API_KEY
cd /home/ubuntu/OpenHands

python3 evaluation/benchmarks/swe_bench/run_infer.py \
  --llm-config deepseek_v4_flash \
  --dataset SWE-bench-Live/SWE-bench-Live \
  --split lite \
  --max-iterations 100 \
  --eval-num-workers 1 \
  --eval-n-limit 1 \
  --eval-output-dir /tmp/oh_v4flash_proxy \
  2>&1 | tee /tmp/oh_v4flash_proxy.log

echo ""
echo "=== RESULT ==="
cat /tmp/oh_v4flash_proxy/*/output.jsonl 2>/dev/null | python3 -c '
import json, sys, re
for line in sys.stdin:
    d = json.loads(line)
    patch = d.get("test_result", {}).get("git_patch", "")
    files = re.findall(r"diff --git a/(\S+)", patch)
    src = [f for f in files if f.endswith((".py", ".js", ".ts", ".go"))]
    hist = d.get("history", [])
    err = d.get("error", "")
    print(f"{d['instance_id']}: {len(hist)} events, {len(src)} source files, patch={len(patch)} chars")
    if err:
        print(f"  ERROR: {str(err)[:300]}")
'

echo ""
echo "Balance after:"
curl -s https://api.deepseek.com/user/balance -H "Authorization: Bearer $DEEPSEEK_API_KEY" | python3 -c 'import json,sys; print(json.load(sys.stdin)["balance_infos"][0]["total_balance"])'

# Kill proxy
kill $PROXY_PID 2>/dev/null
