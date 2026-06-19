#!/bin/bash
set -uo pipefail
export HOME=/home/Lenovo
source /home/Lenovo/.local/bin/env 2>/dev/null || true
LOG=/home/Lenovo/final_pipeline.log

echo "=== FINAL PIPELINE ===" > $LOG
echo "$(date -u) UTC" >> $LOG

cd /home/Lenovo/oh-benchmarks

# Step 1: Build ALL images
echo "Step 1: Building all 300 images (4 workers)..." >> $LOG
uv run python -m benchmarks.swebench.build_images \
    --dataset princeton-nlp/SWE-bench_Lite --split test --max-workers 4 --force-build \
    >> $LOG 2>&1
IMGS=$(docker images | grep -c ghcr || echo 0)
echo "Step 1 done: $IMGS images at $(date -u)" >> $LOG

# Step 2: Retag images for swebench grader
echo "Step 2: Retagging images..." >> $LOG
python3 << 'PYEOF'
import subprocess, re
result = subprocess.run(['docker', 'images', '--format', '{{.Repository}}:{{.Tag}}'], capture_output=True, text=True)
fixed = 0
for line in result.stdout.strip().split('\n'):
    if 'ghcr.io/openhands/eval-agent-server' not in line or 'tag_latest' in line:
        continue
    tag = line.split(':')[1]
    m = re.match(r'[a-f0-9]+-(.+)-source-minimal', tag)
    if m:
        name = m.group(1)
        name = re.sub(r'_\d+_', '__', name)
        new_tag = f'swebench/{name}:latest'
        subprocess.run(['docker', 'tag', line, new_tag], capture_output=True)
        fixed += 1
print(f'Retagged {fixed} images')
PYEOF
echo "Step 2 done at $(date -u)" >> $LOG

# Step 3: Start proxy
echo "Step 3: Proxy..." >> $LOG
cat > /tmp/litellm_config.yaml << 'LCF'
model_list:
  - model_name: qwen3-coder
    litellm_params:
      model: vertex_ai/qwen/qwen3-coder-480b-a35b-instruct-maas
      vertex_project: regal-scholar-442803-e1
      vertex_location: us-south1
LCF
uv run litellm --config /tmp/litellm_config.yaml --port 4000 >> /tmp/litellm.log 2>&1 &
for i in $(seq 1 30); do curl -s http://localhost:4000/health > /dev/null 2>&1 && break; sleep 2; done
curl -s http://localhost:4000/health > /dev/null 2>&1 && echo "Proxy: OK" >> $LOG || echo "Proxy: FAIL — aborting" >> $LOG

# Step 4: GT eval (8 workers)
echo "Step 4: GT eval (8 workers)..." >> $LOG
mkdir -p /home/Lenovo/results/v3/gt
GT_TOOL_PATH=/home/Lenovo/groundtruth/benchmarks/swebench/gt_tool_v3.py \
uv run python /home/Lenovo/groundtruth/scripts/swebench/oh_gt_v3_wrapper.py \
    /home/Lenovo/oh-benchmarks/.llm_config/vertex_qwen3.json \
    --dataset princeton-nlp/SWE-bench_Lite --split test --workspace docker \
    --max-iterations 100 --num-workers 8 \
    --prompt-path gt_v3_hardgate.j2 \
    --output-dir /home/Lenovo/results/v3/gt \
    >> $LOG 2>&1
GT_FILE=$(find /home/Lenovo/results/v3/gt/ -name 'output.jsonl' 2>/dev/null | head -1)
GT_COUNT=$(wc -l < "$GT_FILE" 2>/dev/null || echo 0)
echo "Step 4 done: $GT_COUNT GT patches at $(date -u)" >> $LOG

# Step 5: Grade baseline
echo "Step 5: Grade baseline (8 workers)..." >> $LOG
uv run swebench-eval \
    /home/Lenovo/results/v3/baseline/princeton-nlp__SWE-bench_Lite-test/openai/qwen3-coder_sdk_62c2e7c_maxiter_100/output.jsonl \
    --dataset princeton-nlp/SWE-bench_Lite --split test --workers 8 --no-modal --run-id v3-baseline \
    >> $LOG 2>&1
echo "Step 5 done at $(date -u)" >> $LOG

# Step 6: Grade GT
if [ -n "$GT_FILE" ] && [ "$GT_COUNT" -gt 0 ]; then
    echo "Step 6: Grade GT (8 workers)..." >> $LOG
    uv run swebench-eval "$GT_FILE" \
        --dataset princeton-nlp/SWE-bench_Lite --split test --workers 8 --no-modal --run-id v3-gt \
        >> $LOG 2>&1
    echo "Step 6 done at $(date -u)" >> $LOG
fi

# Step 7: Final results + apples-to-apples comparison
echo "=== FINAL RESULTS ===" >> $LOG
python3 << 'PYEOF' >> $LOG 2>&1
import json, glob

results = {}
for cond in ['baseline', 'gt']:
    pats = glob.glob(f'/home/Lenovo/results/v3/{cond}/**/report.json', recursive=True)
    for p in pats:
        r = json.load(open(p))
        for inst_id, result in r.items():
            if isinstance(result, dict):
                if inst_id not in results:
                    results[inst_id] = {}
                results[inst_id][cond] = result.get('resolved', False)

for cond in ['baseline', 'gt']:
    tot = sum(1 for v in results.values() if cond in v)
    res = sum(1 for v in results.values() if v.get(cond, False))
    print(f'{cond}: {res}/{tot} = {res/tot*100:.1f}%' if tot else f'{cond}: no results')

both = {k: v for k, v in results.items() if 'baseline' in v and 'gt' in v}
bl_r = sum(1 for v in both.values() if v['baseline'])
gt_r = sum(1 for v in both.values() if v['gt'])
print(f'\nAPPLES-TO-APPLES ({len(both)} tasks):')
print(f'  Baseline: {bl_r}/{len(both)} = {bl_r/len(both)*100:.1f}%')
print(f'  GT:       {gt_r}/{len(both)} = {gt_r/len(both)*100:.1f}%')
print(f'  Delta:    {gt_r/len(both)*100 - bl_r/len(both)*100:+.1f}%')

gt_helped = sum(1 for v in both.values() if v['gt'] and not v['baseline'])
gt_hurt = sum(1 for v in both.values() if v['baseline'] and not v['gt'])
both_pass = sum(1 for v in both.values() if v['baseline'] and v['gt'])
both_fail = sum(1 for v in both.values() if not v['baseline'] and not v['gt'])
print(f'\nTASK BREAKDOWN:')
print(f'  Both resolved: {both_pass}')
print(f'  Both failed:   {both_fail}')
print(f'  GT helped:     {gt_helped}')
print(f'  GT hurt:       {gt_hurt}')
print(f'  Net:           {gt_helped - gt_hurt:+d} tasks')
PYEOF
echo "=== ALL DONE $(date -u) ===" >> $LOG
