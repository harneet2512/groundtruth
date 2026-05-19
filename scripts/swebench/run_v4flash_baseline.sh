#!/bin/bash
set -e

source /home/ubuntu/inspect-venv/bin/activate
pkill -f run_infer 2>/dev/null || true
pkill -f litellm 2>/dev/null || true
docker stop $(docker ps -q) 2>/dev/null || true
sleep 2

# Write litellm proxy config
cat > /tmp/litellm_v4flash.yaml << 'EOF'
model_list:
  - model_name: deepseek-v4-flash-nothink
    litellm_params:
      model: deepseek/deepseek-v4-flash
      api_key: $DEEPSEEK_API_KEY
      api_base: https://api.deepseek.com
      thinking:
        type: disabled
      num_retries: 5
      rpm: 30
      tpm: 400000

litellm_settings:
  drop_params: true
  set_verbose: false
  num_retries: 5
  request_timeout: 300

general_settings:
  master_key: sk-gt-local
EOF

# Start proxy
litellm --config /tmp/litellm_v4flash.yaml --port 4000 > /tmp/litellm_proxy.log 2>&1 &
PROXY_PID=$!
echo "Proxy PID: $PROXY_PID"
sleep 8

# Test proxy
echo "=== PROXY TEST ==="
RESP=$(curl -s -X POST http://localhost:4000/v1/chat/completions \
  -H "Authorization: Bearer sk-gt-local" \
  -H "Content-Type: application/json" \
  -d '{"model":"deepseek-v4-flash-nothink","messages":[{"role":"user","content":"say ok"}],"max_tokens":5}')
echo "$RESP" | python3 -c 'import json,sys; d=json.load(sys.stdin); print("content:",d["choices"][0]["message"].get("content")); print("reasoning:",d["choices"][0]["message"].get("reasoning_content","NONE"))'

# Write OH config
cat > /home/ubuntu/OpenHands/config.toml << 'EOF'
[core]
workspace_base = "/tmp/workspace"

[llm.deepseek_v4_flash]
model = "litellm_proxy/deepseek-v4-flash-nothink"
api_key = "sk-gt-local"
base_url = "http://localhost:4000"
temperature = 1.0
top_p = 1.0
max_output_tokens = 65536
native_tool_calling = true
caching_prompt = false
drop_params = true
num_retries = 5
timeout = 300
EOF

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
