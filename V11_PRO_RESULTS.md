# GT v11 SWE-bench Pro 60-Task Results — 2026-03-29

## Run Completed

| Condition | Predictions | Time | Workers |
|---|---|---|---|
| Baseline | 60/60 | 32:31 | 8 |
| GT v11 (Go indexer + ranked evidence) | 60/60 | 33:04 | 8 |

Both conditions ran in parallel on swebench-ab (e2-standard-16, 500GB disk).

## GT Utilization

- Go indexer: built graph.db for 31+ tasks (8-10s each, multi-language)
- Evidence delivery: hook fired on some Python tasks (ansible)
- Evidence families: CALLER, IMPORT, SIBLING, TEST, IMPACT, TYPE all working when evidence available

## Eval Status: BLOCKED

SWE-bench Pro eval harness (SWE-bench_Pro-os) requires per-instance Dockerfiles at `dockerfiles/base_dockerfile/` which are not included in the eval repo. Both conditions show 0% accuracy because the eval couldn't run ANY tests — it returned None for all instances.

This is an eval infrastructure issue, not a GT issue. The patches exist and are in the correct format.

## Next Steps

1. Fix Pro eval setup — either generate Dockerfiles or use the correct eval invocation
2. Alternatively, run eval on SWE-bench Lite subset (we know this harness works) to validate the v11 Go indexer pipeline
3. Manual inspection of patches to assess quality differences
