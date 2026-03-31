# GT v9 50-Task Results — 2026-03-28

## Setup
- Scaffold: mini-swe-agent 2.2.7 + Qwen3-Coder-480B (Vertex AI)
- GT delivery: Precomputed structured cross-file facts (v9)
- 50 SWE-bench Lite tasks, 4 workers, single attempt per task
- Eval: SWE-bench harness (actual test suite execution)

## Results

| | Baseline | GT v9 | Delta |
|---|---|---|---|
| **Resolved** | **29/50 (58%)** | **23/50 (46%)** | **-6** |
| Both resolved | 19 | 19 | |
| Baseline only | 10 | - | |
| GT only | - | 4 | |

**Net: -6 tasks. GT v9 regresses at 50-task scale.**

## What Happened

### GT context injection: only 8/50 tasks (16%)

The v9 suppression threshold (requires >=3 cross-file facts) correctly suppressed low-quality output for 42 tasks. But this means 84% of tasks ran with the GT prompt template but NO GT context — the worst of both worlds:
- Different prompt template mentions `<gt_codebase_context>` section
- Agent may be "looking for" context that doesn't exist
- Net effect: slight confusion from the template, no benefit from GT

### Tasks with GT context (8 tasks):

| Task | GT chars | Context content | Baseline | GT | |
|---|---|---|---|---|---|
| astropy-14365 | 523 | CALLER + TEST + CO-CHANGE | FAIL | FAIL | No change |
| astropy-14182 | 1166 | CALLER + PATTERN + CO-CHANGE | FAIL | FAIL | No change |
| astropy-6938 | 521 | CALLER + TEST + PRECEDENT | FAIL | FAIL | No change |
| astropy-7746 | 937 | CALLER + TEST + CO-CHANGE | FAIL | FAIL | No change |
| django-10914 | 965 | CALLER + CONTRACT + TEST | PASS | PASS | No change |
| django-11133 | 631 | CALLER + CO-CHANGE + PATTERN | FAIL | FAIL | No change |
| django-11564 | 729 | CALLER + PATTERN | FAIL | FAIL | No change |
| django-12747 | 453 | CALLER + CONTRACT + PATTERN | PASS | FAIL | **GT HURT** |

django-12747: GT context was injected and the task REGRESSED (baseline passed, GT failed). The structured facts may have misled the agent.

### Tasks GT gained (4) — all WITHOUT GT context:
- django-10924, django-11179, django-11620, django-12184
- These are stochastic variation from the different prompt template, not GT intelligence

### Tasks GT lost (10) — all WITHOUT GT context:
- django-11848, 11964, 12125, 12284, 12308, 12700, 12747, 12856, 12908, 13158
- Most are stochastic variation. django-12747 is the one task with GT context that regressed.

## Root Cause Analysis

### 1. Suppression too aggressive (42/50 suppressed)
The v9 invariant "suppress if <3 cross-file facts" is correct in principle but results in GT being silent for 84% of tasks. Combined with a different prompt template, this creates a net-negative effect.

### 2. Prompt template contamination
The GT condition uses `mini_swebench_gt_v7.yaml` which mentions `<gt_codebase_context>` in the instructions. Even when no context is injected, this different template changes agent behavior. Should use IDENTICAL template for both conditions when no context is injected.

### 3. Cross-file facts are sparse for most tasks
Only 8/50 tasks had >=3 cross-file facts. This means the index doesn't capture enough cross-file relationships for most files. The callers, test mapping, and co-change data are only populated for frequently-referenced symbols.

### 4. Stochastic noise dominates at small scale
With different random seeds, 10-task showed +1, 50-task showed -6. The true effect is likely near 0, with noise dominating.

## Comparison Across All GT Versions

| Version | Scaffold | Content | 10-task | 50-task | Lesson |
|---|---|---|---|---|---|
| v7 (passive hook) | OpenHands | Hook fires, agent ignores | 7/7 vs 5/5 | 8/8 tie | Hook output not rendered to agent |
| v8 (active calls) | OpenHands | Agent calls understand 51x | 6/6 vs 5/5 | 8/8 tie | Over-exploration burns iterations |
| v8 (precompute) | mini-swe | Fingerprints injected | 4/9 vs 5/10 | - | Fingerprints redundant with reading file |
| v8 (fixed detect) | mini-swe | Fingerprints, 100% inject | 4/10 vs 5/10 | - | Same: redundant |
| **v9 (structured)** | mini-swe | Cross-file facts only | **5/10 vs 4/10** | **23/50 vs 29/50** | Sparse cross-file data + template contamination |

## What's Needed for a Real Lift

1. **Identical prompts when no context**: Use same YAML template for both conditions. Only difference should be the prepended context.
2. **Higher injection rate**: Need >=3 cross-file facts for >=50% of tasks. Currently at 16%. Requires richer cross-file analysis or lower threshold.
3. **Better signals**: CALLER with "1 callers" is low-value. Need minimum caller threshold (>=3 callers to surface).
4. **Richer test mapping**: Current test discovery has false positives. Need symbol-specific test matching.
5. **Matched prompt template**: The GT condition's different template is a confound. Must control for this.
