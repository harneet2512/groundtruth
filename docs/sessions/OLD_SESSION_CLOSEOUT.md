# Session Closeout — 2026-05-17 Static Retrieval Session

**Status:** CLOSED  
**Reason:** Blind holdout invalidated static retrieval direction  
**Branch:** `jedi__branch`  
**Final commit:** `7908cd33`

---

## What Was Attempted

Improve GT pre-task localization (L1 brief) by:
1. Fixing plumbing bugs (fused_n always 0, modulus gate suppression)
2. Adding graph neighbor expansion (callers/callees as ranked candidates)
3. Increasing token budget (400→600)
4. Path-match preservation (path_score >= 0.5 survives)
5. Hub demotion (never suppress entire brief)
6. Defining FINAL_ARCH with timing-correct layers A-E

## What Failed

**Blind holdout (10 unseen tasks) showed GT makes the agent SLOWER:**
- first_gold_view: +27 steps worse than baseline
- action economy: 1.09 (9% more actions with GT)
- GT faster on 1/4 paired tasks, baseline faster on 2/4
- 11 late guidance events (evidence after decisions made)
- 2 bridge events (minimal collaboration)
- 0/10 resolved on both arms

## Why This Invalidates the 5-Task Result

The 5-task debug set showed L1 hit@5 improving from 0% to 60%. This measured GT's standalone retrieval quality, not GT-agent collaboration. On unseen tasks:
- The static brief points to wrong files → agent follows brief → wastes time
- Baseline agent (no brief) greps/searches freely → finds gold faster
- GT's "improvement" was optimized for 5 specific task topologies

Two thresholds tuned from the 5-task set:
- `MAX_BRIEF_TOKENS=600` (was 400, changed for loguru-1306)
- `path_score >= 0.5` preservation (tuned for loguru-1306 _colorama.py)

## Exact Blind Holdout Metrics

```
Task                      GT_1st_gold  BL_1st_gold  Delta  GT_actions  BL_actions  Economy
flexget-4244              30           -            GT only 101         101         1.00
flexget-4306              4            4            +0      31          24          1.29
weasyprint-2303           -            -            -       86          97          0.89
weasyprint-2387           -            8            BL only 55          51          1.08
weasyprint-2398           39           8            +31     58          79          0.73
weasyprint-2405           88           9            +79     91          54          1.69
pypsa-1091                6            8            -2      45          57          0.79
pypsa-1112                4            -            GT only 66          51          1.29
pypsa-1172                -            -            -       100         95          1.05
pypsa-1195                -            6            BL only 81          73          1.11
```

## Files/Docs Produced This Session

| File | Trust Level |
|------|-------------|
| `AUDIT_MAP.md` | USEFUL — component map with citations is accurate |
| `METRICS_CONTRACT.md` | USEFUL — metric definitions are correct |
| `LOCALIZATION_DIAGNOSIS.md` | LIMITED — diagnosis of 5-task set, overfit |
| `VALIDATION_REPORT.md` | INVALID — claims gates pass based on L1 hit@5 |
| `FINAL_ARCH_VALIDATION.md` | INVALID — claims based on overfit metrics |
| `DECISIONS.md ## FINAL_ARCH` | SUPERSEDED by FINAL_ARCH_V2 |
| `benchmarks/holdout_10_locked.json` | USEFUL — holdout task list |

## What Should NOT Be Trusted

1. Any claim that "all gates pass" — gates measured the wrong thing (L1 hit@5)
2. Any claim that "Layer A works" — it works standalone, hurts in collaboration
3. Any claim that "L3b doesn't need to compensate" — 0 bridges = no collaboration happening
4. Any claim based on 5-task metrics — dev-set overfitting confirmed
5. FINAL_ARCH layer hierarchy as written — static timing doesn't match how agents actually explore

## What the New Session Should Start From

1. **FINAL_ARCH_V2** in DECISIONS.md (already written) — defines AgentState tracker + Collaboration Router + WHAT/WHEN separation
2. **Paired holdout methodology** — always measure GT vs baseline on same tasks, never L1 hit@5 alone
3. **Correct principle:** GT observes agent trajectory → decides WHEN to inject → selects WHAT evidence is useful NOW → measures by paired first_gold_view delta
4. **AUDIT_MAP.md** — component citations are accurate even if the architecture is wrong
5. **METRICS_CONTRACT.md** — metric definitions are correct, just the gate thresholds were wrong

## Code Changes That Should Be Reviewed

| Change | Commit | Classification |
|--------|--------|----------------|
| Graph neighbor expansion in L1 | `60d285f5` | EXPERIMENTAL — adds 1-hop callers/callees to ranked brief. Generalized logic but contributes to wrong-brief-following on unseen tasks. Should be feature-flagged. |
| Modulus gate removal | `74666227` | SAFE TO KEEP — never suppressing the brief is correct regardless of direction. Empty brief is always worse. |
| fused_n fix (ranked_count) | `382b52b0` | SAFE TO KEEP — pure plumbing bug fix. Without this, brief is always replaced by fallback. |
| MAX_BRIEF_TOKENS 400→600 | `ca57c3be` | EXPERIMENTAL — tuned from 5-task set. Should be feature-flagged or reverted to 400. |
| Path-match preservation | `0036a412` | EXPERIMENTAL — threshold 0.5 tuned from loguru-1306. Should be feature-flagged. |
| Sparse graph W_PATH | `0036a412` | SAFE TO KEEP — adds W_PATH to sparse mode weights. Generalized, no task-specific logic. |
| Brief runner diagnostics | `382b52b0` | SAFE TO KEEP — logging only, helps debug. |
| Metric fixes (6 bugs) | `4a064e6c` | SAFE TO KEEP — fixes measurement correctness regardless of direction. |

## Summary Classification

| Category | Commits |
|----------|---------|
| SAFE TO KEEP | 382b52b0 (fused_n), 74666227 (no-suppress), sparse W_PATH, metric fixes, diagnostics |
| EXPERIMENTAL (feature-flag) | 60d285f5 (neighbor expansion), ca57c3be (token cap 600), path-match 0.5 |
| SHOULD REVERT IF HARMFUL | None confirmed harmful yet — holdout shows net negative but individual contributions unclear |
