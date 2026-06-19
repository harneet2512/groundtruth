#!/bin/bash
export HOME=/home/Lenovo
source /home/Lenovo/.local/bin/env 2>/dev/null || true
LOG=/home/Lenovo/eval_only.log

echo '=== EVAL ONLY (skip build, 285/300 images) ===' > $LOG
echo "$(date -u) UTC" >> $LOG

cd /home/Lenovo/oh-benchmarks

# Proxy
echo 'Proxy...' >> $LOG
cat > /tmp/litellm_config.yaml << 'LCF'
model_list:
  - model_name: qwen3-coder
    litellm_params:
      model: vertex_ai/qwen/qwen3-coder-480b-a35b-instruct-maas
      vertex_project: regal-scholar-442803-e1
      vertex_location: us-south1
LCF
kill $(pgrep -f litellm | head -3 | tr '\n' ' ') 2>/dev/null || true
sleep 2
uv run litellm --config /tmp/litellm_config.yaml --port 4000 >> /tmp/litellm.log 2>&1 &
for i in $(seq 1 30); do curl -s http://localhost:4000/health > /dev/null 2>&1 && break; sleep 2; done
curl -s http://localhost:4000/health > /dev/null 2>&1 && echo 'Proxy: OK' >> $LOG || echo 'Proxy: FAIL' >> $LOG

# GT eval (8 workers)
echo 'GT eval (8 workers, 285 images)...' >> $LOG
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
echo "GT done: $GT_COUNT patches at $(date -u)" >> $LOG

# Grade baseline
echo 'Grading baseline...' >> $LOG
docker buildx create --use --name sweb --driver docker-container 2>/dev/null || true
uv run swebench-eval /home/Lenovo/results/v3/baseline/princeton-nlp__SWE-bench_Lite-test/openai/qwen3-coder_sdk_62c2e7c_maxiter_100/output.jsonl \
    --dataset princeton-nlp/SWE-bench_Lite --split test --workers 8 --no-modal --run-id v3-baseline-full \
    >> $LOG 2>&1
echo "Baseline grading done at $(date -u)" >> $LOG

# Grade GT
if [ -n "$GT_FILE" ] && [ "$GT_COUNT" -gt 0 ]; then
    echo 'Grading GT...' >> $LOG
    uv run swebench-eval "$GT_FILE" \
        --dataset princeton-nlp/SWE-bench_Lite --split test --workers 8 --no-modal --run-id v3-gt \
        >> $LOG 2>&1
    echo "GT grading done at $(date -u)" >> $LOG
fi

# Final results
echo '=== FINAL RESULTS ===' >> $LOG
python3 << 'PYEOF' >> $LOG 2>&1
import json, os, glob
for cond in ['baseline', 'gt']:
    pats = glob.glob(f'/home/Lenovo/results/v3/{cond}/**/report.json', recursive=True)
    res = tot = 0
    for p in pats:
        tot += 1
        r = json.load(open(p))
        for v in r.values():
            if isinstance(v, dict) and v.get('resolved'): res += 1
    print(f'{cond}: {res}/{tot} = {res/tot*100:.1f}%' if tot else f'{cond}: no results')
PYEOF
echo "=== ALL DONE $(date -u) ===" >> $LOG
