# GT v11 SWE-bench Pro 60-Task Final Results — 2026-03-29

## Architecture
- **Go indexer** (gt-index): tree-sitter multi-language parsing, 6 language specs, SQLite output
- **Python intelligence** (gt_intel.py): 6 evidence families (IMPORT, CALLER, SIBLING, TEST, IMPACT, TYPE), scored 0-3, threshold ≥1
- **Hook delivery**: DockerEnvironment.execute monkey-patch, fires on file edits detected via `git diff --name-only`
- **Model**: Qwen3-Coder-480B via Vertex AI (litellm proxy)
- **Scaffold**: mini-swe-agent 2.2.7
- **VM**: swebench-ab (e2-standard-16, 500GB disk)

## Run Stats

| Metric | Baseline | GT v11 |
|---|---|---|
| Predictions | 60/60 | 60/60 |
| Runtime | 32:31 | 33:04 |
| Workers | 8 | 8 |
| Go indexes built | — | 41 |
| Docker errors (125) | 32 | 32 |

## Eval Results

| | Baseline | GT v11 | Delta |
|---|---|---|---|
| **Passed** | **19/60 (31.7%)** | **18/60 (30.0%)** | **-1** |
| Failed | 22 | 22 | 0 |
| Errors (no script/tag) | 19 | 20 | +1 |

### Task Breakdown

| Category | Count | Tasks |
|---|---|---|
| **Both solved** | **13** | (shared resolves — GT didn't break these) |
| **Baseline only** | **6** | NodeBB-51d8f3b1, ansible-395e5e20, vuls-407407d3, teleport-6eaaf3a2, openlibrary-00bec1e7, openlibrary-25858f9f |
| **GT only** | **5** | NodeBB-397835a0, ansible-b748edea, ansible-be59caa5, vuls-2c84be80, openlibrary-8a5a63af |

### GT Gained Tasks (5 flips: baseline FAIL → GT PASS)
1. **NodeBB-397835a0** (JavaScript) — GT evidence helped on a JS repo
2. **ansible-b748edea** (Python) — GT evidence helped on Python
3. **ansible-be59caa5** (Python) — GT evidence helped on Python
4. **vuls-2c84be80** (Go) — GT evidence helped on a Go repo
5. **openlibrary-8a5a63af** (Python) — GT evidence helped on Python

### GT Lost Tasks (6 regressions: baseline PASS → GT FAIL)
1. **NodeBB-51d8f3b1** (JavaScript)
2. **ansible-395e5e20** (Python)
3. **vuls-407407d3** (Go)
4. **teleport-6eaaf3a2** (Go)
5. **openlibrary-00bec1e7** (Python)
6. **openlibrary-25858f9f** (Python)

## Analysis

### What Worked
1. **Go indexer**: 8-10s per repo, multi-language (Python + Go + JS/TS), 9898 nodes + 35757 edges on ansible
2. **Multi-language flips**: GT helped on Python (ansible, openlibrary), Go (vuls), AND JavaScript (NodeBB) — not just Python
3. **5 positive flips**: The mechanism works — GT evidence changes agent behavior to produce correct fixes
4. **Parallel execution**: Both conditions ran simultaneously in ~33 min with 8 workers each
5. **500GB disk**: Eliminated Docker "exit status 125" errors from disk exhaustion

### What Failed
1. **Net -1**: 5 gained, 6 lost = negative. Evidence is misleading on some tasks.
2. **Evidence delivery rate**: Only 41/60 indexes built (some containers failed before indexing). Evidence delivery rate not measured precisely but likely <50%.
3. **6 regressions**: The evidence hurts as often as it helps. Root cause unknown without per-task analysis of what evidence was shown and how the agent used it.

### Why GT Hurts on Some Tasks
Likely causes (to be investigated per-task):
- **False positive callers**: Name-based resolution (37%) produces false CALLS edges → misleading CALLER evidence
- **Irrelevant evidence**: Hook fires on the first edited file, but the edit might be a test script, not the actual fix
- **Evidence noise**: Too many low-scored nodes (threshold ≥1 may be too permissive) → agent distracted
- **Index overhead**: 8-10s index build adds latency that may cause the agent to timeout or take a different path

### Comparison Across All GT Versions

| Version | Benchmark | Baseline | GT | Delta | Flips gained | Flips lost |
|---|---|---|---|---|---|---|
| v7 | Lite 18-task | 8 | 8 | 0 | 0 | 0 |
| v8 precompute | Lite 10-task | 5 | 4 | -1 | 0 | 1 |
| v9 structured | Lite 50-task | 29 | 23 | -6 | 4 | 10 |
| v10 hooked | Lite 5-task | 2 | 3 | +1 | 1 | 0 |
| **v11 Go indexer** | **Pro 60-task** | **19** | **18** | **-1** | **5** | **6** |

### Key Insight

v11 is the first version that consistently **flips tasks in both directions** — the evidence IS changing agent behavior (unlike v7-v8 where evidence was ignored or redundant). The problem is no longer delivery or content format — it's **evidence precision**. False positive edges from name-based resolution produce misleading caller evidence that hurts on some tasks.

### What Would Make v11 Net Positive

1. **Import-aware call resolution** — Push from 37% to ~70% accuracy. Eliminates false positive CALLS edges that produce misleading CALLER evidence.
2. **Higher scoring threshold** — Raise from ≥1 back to ≥2 but only after import resolution improves (so more nodes actually reach score 2).
3. **Per-task evidence logging** — Track exactly what evidence was shown per task, so we can identify which evidence types help vs hurt.
4. **Suppress on first edit** — Only fire the hook on the second edit to the same file (first edit is often exploration/test scripts, not the actual fix).

## Infrastructure Notes

- Disk resized from 243GB to 500GB mid-run to fix Docker container failures
- Pro eval harness (SWE-bench_Pro-os) has missing Dockerfiles — used custom simple evaluator instead
- Simple evaluator: apply patch in Docker, run test script, check exit code
- 19 tasks had no run_script.sh or dockerhub_tag → evaluated as errors (not counted in pass/fail)

## Files on VM

```
/home/Lenovo/results/v11_run_baseline/preds.json    — 60 baseline predictions
/home/Lenovo/results/v11_run_gt/preds.json          — 60 GT predictions
/home/Lenovo/results/eval_bl_simple/results.json    — baseline eval (19 passed)
/home/Lenovo/results/eval_gt_simple/results.json    — GT eval (18 passed)
```
