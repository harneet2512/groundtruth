#!/bin/bash
set -e
source /opt/oh-env/bin/activate

echo "=== Installing Inspect AI ==="
pip install inspect-ai "inspect-evals[swe_bench]" openai -q

echo "=== Verify ==="
python3 -c "import inspect_ai; print(f'inspect_ai OK: {inspect_ai.__version__}')"
python3 -c "from inspect_evals.swe_bench import swe_bench; print('inspect_evals OK')"
inspect version 2>/dev/null || echo "inspect CLI not in PATH"

echo "=== Clone GT inspect_urself branch ==="
cd /opt/groundtruth
git fetch origin inspect_urself
git worktree add /opt/gt_inspect inspect_urself 2>/dev/null || (cd /opt/gt_inspect && git checkout inspect_urself && git pull origin inspect_urself)

echo "=== Verify adapter ==="
export PYTHONPATH=/opt/gt_inspect
python3 -c "from adapters.inspect.tools import gt_tools; print(f'GT tools: {len(gt_tools())}')"
python3 -c "from adapters.inspect.task import swebench_gt_baseline; print('task OK')"

echo "=== INSPECT SETUP COMPLETE ==="
