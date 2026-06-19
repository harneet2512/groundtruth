# HOW TO RUN EVALS — SWE-bench-Live Lite

**READ THIS BEFORE RUNNING ANY EVALUATION. NO EXCEPTIONS.**

---

## The ONE legitimate eval path

```bash
# 1. Clone Microsoft's python-only branch (NOT main, NOT Princeton's)
git clone --branch python-only --recursive \
  https://github.com/microsoft/SWE-bench-Live.git /home/ubuntu/SWE-bench-Live

# 2. Install in a clean venv
cd /home/ubuntu/SWE-bench-Live
python3 -m venv .venv
source .venv/bin/activate
pip install -U pip
pip install -e .

# 3. Convert OH output to predictions
cd /home/ubuntu/OpenHands-0.54.0
source .venv/bin/activate  # OH venv for the convert script
python evaluation/benchmarks/swe_bench/scripts/live/convert.py \
  --output_jsonl <path/to/output.jsonl> > preds.jsonl

# 4. Run eval (switch back to SWE-bench-Live venv)
source /home/ubuntu/SWE-bench-Live/.venv/bin/activate
python -m swebench.harness.run_evaluation \
  --dataset_name SWE-bench-Live/SWE-bench-Live \
  --split lite \
  --namespace starryzhang \
  --predictions_path <path/to/preds.jsonl> \
  --max_workers 4 \
  --run_id <your_run_name>
```

That's it. Nothing else. No shortcuts.

---

## What each flag means

| Flag | Value | Why |
|------|-------|-----|
| `--dataset_name` | `SWE-bench-Live/SWE-bench-Live` | The HuggingFace dataset (Microsoft's, NOT Princeton's) |
| `--split` | `lite` | 300-task subset (we use first 20-30) |
| `--namespace` | `starryzhang` | Pre-built Docker images on Docker Hub |
| `--max_workers` | `4` | Parallel eval containers (match to vCPU count) |
| `--run_id` | any string | Names the eval run, used for report file paths |

---

## What the harness checks (per task)

1. Applies your patch to the repo at `base_commit`
2. Applies the `test_patch` (gold test additions)
3. Runs `FAIL_TO_PASS` tests — these MUST now pass (bug is fixed)
4. Runs `PASS_TO_PASS` tests — these MUST still pass (nothing broken)
5. **RESOLVED** = F2P all pass AND P2P all pass
6. **NOT RESOLVED** = anything else

Binary. No partial credit.

---

## Where results land

The report JSON is written to:
```
<run_id>.<model_name>.json
```
in the current working directory (where you ran the command).

Also check:
```
logs/run_evaluation/<run_id>/
```

Read results:
```bash
python3 -c "
import json, glob
files = glob.glob('*pregen*.json') + glob.glob('logs/run_evaluation/*/report.json')
for f in files:
    with open(f) as fh:
        d = json.load(fh)
    if isinstance(d, dict) and any('resolved' in str(v) for v in d.values()):
        resolved = [k for k,v in d.items() if isinstance(v, dict) and v.get('resolved')]
        print(f'{f}: {len(resolved)}/{len(d)} RESOLVED')
"
```

---

## NEVER do this

- **NEVER** write a custom eval script (grep exit codes, parse logs, Docker run + check)
- **NEVER** use Princeton's `pip install swebench` (4.1.0) — doesn't know Live repos
- **NEVER** use Microsoft's `main` branch for Python eval — that's for MultiLang/Windows
- **NEVER** use `pip install git+https://github.com/microsoft/SWE-bench-Live.git` — circular dep, breaks import
- **NEVER** check only FAIL_TO_PASS without PASS_TO_PASS
- **NEVER** report numbers from anything other than this harness

---

## Common errors and fixes

### `KeyError: 'aiogram/aiogram'`
You're using Princeton's swebench, not Microsoft's python-only branch. Start over.

### `ModuleNotFoundError: No module named 'swebench'`
You installed from Microsoft's main branch or PyPI. The `python-only` branch is the one with `swebench/` directory. Clone with `--branch python-only`.

### `FileNotFoundError: tokio-rs__tokio-6724.Cargo.lock`
You're using Princeton's latest main (has Rust fixtures). Use Microsoft's `python-only` branch.

### Docker images not found
Pass `--namespace starryzhang`. Images are `starryzhang/sweb.eval.x86_64.<org>_1776_<repo>-<id>`.

---

## Quick reference

| Item | Value |
|------|-------|
| Dataset | `SWE-bench-Live/SWE-bench-Live` (HuggingFace) |
| Split | `lite` (300 tasks) |
| Harness repo | `github.com/microsoft/SWE-bench-Live` |
| Harness branch | **`python-only`** |
| Install | `pip install -e .` (editable, from cloned repo) |
| Docker images | `starryzhang/sweb.eval.x86_64.*` (Docker Hub) |
| Also cached at | `ghcr.io/harneet2512/sweb.eval.x86_64.*` (GHCR) |
| Predictions format | JSONL: `{"instance_id": "...", "model_patch": "...", "model_name_or_path": "..."}` |
