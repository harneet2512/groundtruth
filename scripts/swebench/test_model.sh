#!/bin/bash
# Test Qwen3-Coder via OpenAI API
KEY=$(grep OPENAI_API_KEY ~/.bashrc | sed 's/export OPENAI_API_KEY=//')
echo "Key prefix: ${KEY:0:15}"

if [ -z "$KEY" ]; then
    echo "ERROR: No OPENAI_API_KEY found in ~/.bashrc"
    exit 1
fi

echo ""
echo "=== Testing qwen3-coder ==="
curl -s --max-time 30 https://api.openai.com/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $KEY" \
  -d '{"model":"qwen3-coder","messages":[{"role":"user","content":"Say hello in one word"}],"max_tokens":5,"temperature":0.7}' 2>&1

echo ""
echo ""
echo "=== Listing available models with qwen ==="
curl -s --max-time 10 https://api.openai.com/v1/models \
  -H "Authorization: Bearer $KEY" 2>&1 | python3 -c "
import sys, json
d = json.load(sys.stdin)
models = [m['id'] for m in d.get('data', []) if 'qwen' in m['id'].lower()]
print(f'Qwen models: {models}')
if not models:
    all_models = sorted([m['id'] for m in d.get('data', [])])
    print(f'All models ({len(all_models)}): {all_models[:20]}...')
" 2>/dev/null
