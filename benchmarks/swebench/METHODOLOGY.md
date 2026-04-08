# SWE-bench Evaluation Methodology

## Overview

GroundTruth is evaluated on [SWE-bench Verified](https://www.swebench.com/) (500 tasks) using the [OpenHands](https://github.com/All-Hands-AI/OpenHands) CodeAct agent as the base harness. The evaluation measures whether injecting deterministic structural evidence into the agent's context improves task resolution rate.

## Experimental Setup

### Conditions

| Condition | Description |
|-----------|-------------|
| **Baseline** | OpenHands CodeAct with no modifications |
| **With GT** | OpenHands CodeAct + GroundTruth post-edit hook |

Both conditions use the same model, same temperature (0.0), and same max token budget per turn. The only variable is whether the GroundTruth evidence hook is active.

### Evidence Hook

When active, the GT hook:
1. Runs `gt-index` (Go binary) on the task repository to produce `graph.db`
2. After each file edit, runs `gt_intel.py` to query the graph for the modified function
3. Produces ranked evidence from 7 families (IMPORT, CALLER, SIBLING, TEST, IMPACT, TYPE, PRECEDENT)
4. Appends the evidence block to the agent's observation

The evidence layer makes **zero LLM calls**. All evidence is derived deterministically from the call graph.

### Infrastructure

- **VM:** GCP e2-standard-8 (8 vCPU, 32 GB RAM)
- **Disk:** 256 GB SSD (Docker images require ~100 GB)
- **Workers:** 4 parallel (matching CPU count)
- **Docker:** Each task runs in an isolated SWE-bench container

### Cost Parameters

| Parameter | Baseline | With GT |
|-----------|----------|---------|
| Max cost per task | $3.00 | $1.25 |
| Evidence generation cost | — | $0.00 |

## Evaluation

Resolution is determined by the official SWE-bench harness (`swebench.harness.run_evaluation`). A task is "resolved" if:
1. The agent produced a non-empty patch
2. The patch applies cleanly to the repository
3. All repository tests pass after applying the patch

### Metrics

- **Resolve rate** = resolved tasks / 500 (total tasks, not just completed)
- **Patch rate** = tasks with non-empty patches / 500
- **Evidence delivery rate** = tasks that received at least one evidence block / total tasks with GT active

## Models Tested

| Model | Baseline | With GT | Delta |
|-------|----------|---------|-------|
| GPT-5 Mini | 277/500 (55.4%) | 289/500 (57.8%) | +12 (+2.4pp) |
| Gemini 2.5 Flash | ~343/500 | ~357/500 | +14 (+2.8pp) |
| Gemini 3 Flash | 379/500 (75.8%) | 382/500 (76.4%) | +3 (+0.6pp) |

## Reproducing

```bash
# 1. Install with benchmark dependencies
pip install -e ".[benchmark]"

# 2. Index a task repo (happens automatically in the hook, shown here for reference)
gt-index -root /path/to/repo -output graph.db

# 3. Run with the hooked harness
python benchmarks/swebench/run_mini_gt_hooked.py \
    -c benchmarks/swebench/mini_swebench_verified_gt_v13.yaml \
    --model <model> --subset swe-bench-verified --split test -w 4

# 4. Evaluate with official harness
python -m swebench.harness.run_evaluation \
    --predictions_path <output>.jsonl \
    --swe_bench_tasks princeton-nlp/SWE-bench_Verified \
    --run_id <run_id>
```

See the project README for installation and setup.
