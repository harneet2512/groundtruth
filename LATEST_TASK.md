# LATEST TASK — Graph Quality Verification Smoke

**Status:** READY TO SMOKE (locally verified — all layers proven working)
**Branch:** `jedi__branch` (commit `157c8e3`)
**Last updated:** 2026-05-16
**Coordinator plan:** `we-will-run-a-twinkly-elephant.md`
**Work log:** `jedi_WORK.md`

---

## What Was Done

Commit `e72690c` implements graph quality infrastructure:
1. Schema: `trust_tier`, `candidate_count`, `evidence_type`, `verification_status` columns added to edges table
2. Resolver: persists candidate count + trust tier at resolution time (CERTIFIED/CANDIDATE/SPECULATIVE)
3. V1R confidence floor: 4 unfiltered queries now filter `AND e.confidence >= 0.7`
4. V1R graph_reach: min_confidence raised from 0.5 to 0.7
5. New: `scripts/graph_quality_metrics.py` — quality metrics from any graph.db

## Local Verification (2026-05-16, Phase 0-4 complete)

All layers proven working locally:
- **Graph metrics:** 5 repos × 4 languages tested. Confidence floor eliminates 45% noise connections.
- **V1R brief:** G3a removed, W_SEM=0 fallback works. 41 ranked files produced locally.
- **L3 evidence:** 4/5 smoke tasks have rich evidence (635, 136, 1678 callers at conf=1.0).
- **L3b navigation:** Iteration-aware decay implemented (1000/640/320/0 char caps by band).
- **L5 governor:** 49 tests pass, infrastructure correct.
- **D29 fixes:** All 4 applied (Fix A via graph connectivity gate, B/C/D confirmed).
- **Tests:** 376/377 pass (1 pre-existing failure unrelated).

See `jedi_WORK.md` for full evidence chain and `reports/PHASE1_GRAPH_VERIFICATION.md` for metrics.

## What Needs Verification (VM/GHA only)

Run a 5-task smoke test with DeepSeek V4 Flash on GHA to prove:
- The code runs (no crashes from schema changes)
- V1R brief produces non-empty output with the confidence floor
- Resolved count >= 3/5 (no regression from baseline)
- beancount-931, beets-5495, xarray-9760 still resolve

## How To Run The Smoke

### Option 1: GitHub Actions (preferred)

1. Go to: `Actions` tab → `SWE-bench-Live 30-task (VM baseline)` workflow
2. Click "Run workflow"
3. Parameters:
   - `gt_commit`: `jedi__branch`
   - `max_iterations`: `100`
   - `baseline`: `false`
   - `temperature`: `0.7`
   - `top_p`: `0.8`
4. Wait for 5 tasks to complete (~60-90 min)
5. Check: resolved count in eval results

### Option 2: Manual single-task test

```bash
# On a machine with Docker + DeepSeek API key
export DEEPSEEK_API_KEY="..."
python scripts/swebench/oh_gt_full_wrapper.py \
    --instance-ids 'beancount__beancount-931' \
    -l eval \
    -i 100 \
    --eval-num-workers 1 \
    --eval-output-dir /tmp/results \
    --dataset 'SWE-bench-Live/SWE-bench-Live' \
    --split lite
```

## DeepSeek V4 Flash Config

```toml
[llm.eval]
model = "deepseek/deepseek-v4-flash"
api_key = "${DEEPSEEK_API_KEY}"
base_url = "https://api.deepseek.com"
temperature = 0.7
top_p = 0.8
max_output_tokens = 65536
drop_params = true
num_retries = 10
timeout = 300
```

**Cost estimate:** ~$0.02-0.05 per task (DeepSeek V4 Flash is cheap). 5 tasks = ~$0.10-0.25 total.

## GHA Workflow Details

- **File:** `.github/workflows/swebench_30task.yml`
- **Runner:** `ubuntu-latest` (not VM — DeepSeek direct API doesn't need WIF/GCP)
- **Docker:** `ghcr.io/all-hands-ai/runtime:0.54-nikolaik`
- **Task images:** `ghcr.io/harneet2512/sweb.eval.x86_64.<repo>_1776_<task>:latest`
- **Parallel:** up to 20 (but only 5 tasks in current config)
- **Timeout:** 90 min per task

## Feature Flags (all enabled in workflow)

```
GT_REBUILD_L1=1
GT_REBUILD_L3=1
GT_REBUILD_L3B=1
GT_REBUILD_L5=1
GT_LAYER_EVENTS=1
GT_STRUCTURED_EVENTS=1
GT_STRUCTURAL_NEXT_ACTION=1
GT_L3B_PRIMARY_EDGE=1
GT_L5_STRUCTURAL_UNVERIFIED=1
GT_L5_GOKU_EVENTS=1
GT_DEEP_LAYER_GROUNDED_METRICS=1
GT_L5B_SAFETY_REQUIRED=1
GT_LSP_VERIFY=1
EVAL_CONDENSER=recent_events:5
```

## 5 Smoke Tasks

| Task | Repo | Previous Result | Expected |
|------|------|----------------|----------|
| beancount__beancount-931 | beancount/beancount | RESOLVED | RESOLVED |
| beetbox__beets-5495 | beetbox/beets | RESOLVED | RESOLVED |
| pydata__xarray-9760 | pydata/xarray | RESOLVED | RESOLVED |
| aws-cloudformation__cfn-lint-3821 | aws-cloudformation/cfn-lint | FAILED | FAILED (acceptable) |
| delgan__loguru-1306 | delgan/loguru | FAILED | FAILED (acceptable) |

## Acceptance Gates

| Gate | Criterion | Action if FAIL |
|------|-----------|----------------|
| G1 | No crash / infra error | Fix code, don't rerun |
| G2 | V1R brief non-empty on >= 4/5 tasks | Lower confidence floor or investigate |
| G3 | Resolved >= 3/5 | Revert confidence floor changes |
| G4 | beancount + beets + xarray all resolve | Revert — regression detected |
| G5 | No new GT-caused timeouts | Investigate LSP/query latency |

## After Smoke Passes

1. Run `scripts/graph_quality_metrics.py` on the per-task graph.db files
2. Compare metrics before/after (are certified edges actually surfacing in brief?)
3. Check GT logs for `[GT_META]` lines — does V1R brief content differ from pre-fix?
4. Document results in `reports/` directory

## After Smoke Fails

1. Check which gate failed
2. If G3/G4 (regression): `git revert e72690c`, push, re-smoke to confirm revert fixes it
3. If G1 (crash): read error logs, fix the code, push fix, re-smoke
4. If G2 (empty brief): confidence floor may be too high for small repos — investigate per-repo metrics

---

## Context Documents (for deep understanding)

- `reports/GRAPH_CREATION_METRIC_DIAGNOSIS.md` — measured graph quality on 4 repos
- `reports/GENERAL_GRAPH_CREATION_DESIGN.md` — generalized architecture design (19 sections)
- `reports/HYPOTHESIS_ISOLATION_PLAN.md` — causal research design (5 hypotheses)

## Key Proven Facts (from this session's analysis)

- dagster graph: 27% of edges are noise (conf < 0.5)
- 5,726 cross-language false positives (Python → TypeScript)
- `docs_snippets/ops.py` ranked #2 in entire codebase (93% noise edges)
- `execute_in_process` had 1,318 edges — ALL noise (0 certified)
- After confidence floor: docs/TS/utilities all drop off top-10
- Confidence floor is the MINIMAL safe infrastructure change
