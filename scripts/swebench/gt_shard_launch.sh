#!/bin/bash
# Launch GT condition shard inference.
# Usage: bash gt_shard_launch.sh <shard_file> <output_dir> [num_workers]
set -euo pipefail
source ~/.local/bin/env 2>/dev/null || true
cd ~/oh-benchmarks

SHARD_FILE="$1"
OUTPUT_DIR="$2"
NUM_WORKERS="${3:-1}"

# Encode gt_tool.py
GT_B64_FILE=$(mktemp /tmp/gt_b64_XXXXXX.txt)
base64 -w0 ~/groundtruth/benchmarks/swebench/gt_tool.py > "$GT_B64_FILE"
echo "gt_tool.py encoded: $(wc -c < "$GT_B64_FILE") bytes"

# Copy prompt template
cp ~/groundtruth/benchmarks/swebench/prompts/gt_phase3.j2 \
   ~/oh-benchmarks/benchmarks/swebench/prompts/gt_phase3.j2

# Export for inject script
export GT_B64_FILE
export GT_TOOL_PATH=~/groundtruth/benchmarks/swebench/gt_tool.py

# Create inject script
INJECT_SCRIPT=$(mktemp /tmp/gt_inject_XXXXXX.py)
python3 -c "
import os
b64_file = os.environ['GT_B64_FILE']
code = '''
import sys, os, re, textwrap

GT_B64_FILE = \"''' + b64_file + '''\"
with open(GT_B64_FILE) as f:
    gt_b64 = f.read().strip()

CHUNK_SIZE = 50000
chunks = textwrap.wrap(gt_b64, CHUNK_SIZE)
setup_cmds = [\"rm -f /tmp/gt_b64.txt && touch /tmp/gt_b64.txt\"]
for chunk in chunks:
    setup_cmds.append(f\"echo -n '\" + chunk + \"' >> /tmp/gt_b64.txt\")
setup_cmds.append(\"base64 -d /tmp/gt_b64.txt > /tmp/gt_tool.py && chmod +x /tmp/gt_tool.py && rm /tmp/gt_b64.txt && echo GT_INSTALLED\")

import benchmarks.swebench.run_infer as run_infer_mod
original_init = run_infer_mod.SWEBenchEvaluation.__init__

def patched_init(self, *args, **kwargs):
    original_init(self, *args, **kwargs)
    if self.metadata.env_setup_commands is None:
        self.metadata.env_setup_commands = []
    self.metadata.env_setup_commands.extend(setup_cmds)
    print(f\"[GT] Injected gt_tool.py via {len(setup_cmds)} setup commands\")

run_infer_mod.SWEBenchEvaluation.__init__ = patched_init

def extract_symbols(ps, max_s=3):
    import re as _re
    camel = _re.findall(r\"\\\\b([A-Z][a-z]+(?:[A-Z][a-z]+)+)\\\\b\", ps)
    dotted = _re.findall(r\"\\\\b([A-Z][a-zA-Z]*)\\\\.[a-z_]\\\\w*\", ps)
    backtick = _re.findall(r\"`([A-Z][a-zA-Z]+)`\", ps)
    seen = set()
    symbols = []
    for s in camel + dotted + backtick:
        if s not in seen and len(s) > 2 and s not in (\"The\",\"This\",\"That\",\"When\",\"With\"):
            seen.add(s)
            symbols.append(s)
            if len(symbols) >= max_s: break
    return symbols

try:
    import jinja2
    _orig = jinja2.Template.render
    def _patched(self, *args, **kwargs):
        instance = None
        if \"instance\" in kwargs and isinstance(kwargs[\"instance\"], dict):
            instance = kwargs[\"instance\"]
        elif args and isinstance(args[0], dict) and \"instance\" in args[0]:
            instance = args[0][\"instance\"]
        if instance and isinstance(instance, dict):
            iid = instance.get(\"instance_id\", \"\")
            ps = instance.get(\"problem_statement\", \"\")
            if iid and \"gt_analysis\" not in instance:
                symbols = extract_symbols(ps)
                if symbols:
                    parts = [f\"Key symbols from the issue: {', '.join(symbols)}\",
                             \"Run these commands inside the repo for detailed analysis:\"]
                    for sym in symbols[:2]:
                        parts.append(f\"  python3 /tmp/gt_tool.py groundtruth_impact {sym}\")
                    if symbols:
                        parts.append(f\"  python3 /tmp/gt_tool.py groundtruth_references {symbols[0]}\")
                    instance[\"gt_analysis\"] = \"\\\\n\".join(parts)
        return _orig(self, *args, **kwargs)
    jinja2.Template.render = _patched
    print(\"[GT] Patched Jinja2 for per-instance analysis injection\")
except Exception as e:
    print(f\"[GT] Warning: {e}\")

if __name__ == \"__main__\":
    sys.argv = [sys.argv[0]] + sys.argv[1:]
    run_infer_mod.main()
'''
with open('$INJECT_SCRIPT', 'w') as f:
    f.write(code)
print(f'Inject script written to $INJECT_SCRIPT')
" || { echo "Failed to create inject script"; exit 1; }

echo "Inject script: $INJECT_SCRIPT"
echo "Shard: $SHARD_FILE ($(wc -l < "$SHARD_FILE") tasks)"
echo "Output: $OUTPUT_DIR"
echo "Workers: $NUM_WORKERS"

OPENHANDS_SUPPRESS_BANNER=1 uv run python "$INJECT_SCRIPT" \
    .llm_config/vertex_qwen3.json \
    --dataset princeton-nlp/SWE-bench_Lite \
    --split test \
    --max-iterations 100 \
    --prompt-path gt_phase3.j2 \
    --workspace docker \
    --n-critic-runs 1 \
    --max-retries 1 \
    --num-workers "$NUM_WORKERS" \
    --select "$SHARD_FILE" \
    --output-dir "$OUTPUT_DIR"

rm -f "$INJECT_SCRIPT" "$GT_B64_FILE"
echo "GT RUN COMPLETE"
