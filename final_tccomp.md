# GroundTruth Internal Run Comparison

**Date:** 2026-05-09
**Author:** Autopsy analysis across all experiment sessions (2026-04-28 through 2026-05-09)
**Evidence base:** 55 output.jsonl files, 17 handoff docs, 60+ memory entries, 6 distinct GT configurations tested on overlapping task sets

---

## Executive Verdict

- **Cleanest run:** Compression GT (4/30). Zero noise, zero contamination, zero regressions, zero infra failures. Baseline parity achieved through the least invasive GT integration ever tested.
- **Best numerical run:** Noisy GT (claimed 6/30). UNVERIFIED. If real, Fisher's exact p=0.73 vs baseline 4/30 — statistically indistinguishable from noise. The +2 (beets-5495, xarray-9971) could be stochastic model variance, not GT contribution.
- **Best run to build on:** Compression GT. It proves the structural observation thesis (placeholder presence matters) without adding noise. It is the minimum viable GT integration.
- **Main product lesson:** GT's graph-based intelligence is real but irrelevant at the current bottleneck. The bottleneck is not localization (agents reach gold files 88% of the time without GT) — it is post-localization execution: scaffolding traps, multi-file scope misses, and wrong fix mechanisms. GT attacks the wrong problem.
- **Biggest false trail to stop chasing:** L3 post-edit evidence content. 75% empty, 22% signal-to-noise ratio, cry-wolf attention damage. Fixing L3 content quality (which consumed ~5 sessions of engineering) produced zero resolve rate improvement. The content was never the issue — the agent ignores it regardless of quality.

---

## Run Inventory Table

| # | Run Name | Date | Config | Tasks | Resolved | Rate | Harness | Model | Verified | Key Finding |
|---|----------|------|--------|-------|----------|------|---------|-------|----------|-------------|
| 1 | OH Baseline (no GT) | 2026-05-09 | Pure OH, zero GT | 30 | 4 | 13.3% | OpenHands 0.54.0 | Qwen3-Coder-480B | YES (eval on VMs) | This is the floor. |
| 2 | Noisy GT (original) | 2026-05-08 | All 6 layers, 75% empty evidence injected | 30 | 6 | 20.0% | OpenHands 0.54.0 | Qwen3-Coder-480B | NO (prior session memory only) | Claimed +2 over baseline. |
| 3 | Clean GT (eliminate empty) | 2026-05-09 | 13 noise fixes, empty evidence suppressed | 29 (1 infra) | 3 | 10.3% | OpenHands 0.54.0 | Qwen3-Coder-480B | YES | Removing empty evidence HURT. Lost weasyprint-2303. |
| 4 | Phase 5 Enforce GT | 2026-05-09 | Scaffold strip + L3 framing + L5 redirect | 29 (1 infra) | 3 | 10.3% | OpenHands 0.54.0 | Qwen3-Coder-480B | YES | No improvement over clean GT. |
| 5 | Compression GT | 2026-05-09 | Empty evidence compressed to [GT_OK] | 30 | 4 | 13.3% | OpenHands 0.54.0 | Qwen3-Coder-480B | YES | Recovered weasyprint-2303. Matches baseline exactly. |
| 6 | Phase 4 Clean (SWE-agent) | 2026-05-07 | GT + SWE-agent, no oracle leaks | 30 | 3 | 10.0% | SWE-agent 1.0 | Qwen3-Coder-480B | YES | 63% patch rate vs OH's 97-100%. |
| 7 | Phase 5 SWE-agent 300 | 2026-05-07 | GT + SWE-agent, full Lite | 200 (gt-t0) | 5 | 2.5% | SWE-agent 1.0 | Qwen3-Coder-480B | YES | 34% submission rate. Thought_action parsing bottleneck. |
| 8 | ULTRAREVIEW Phase 4 | 2026-05-06 | 9 RC fixes, SWE-agent Track 4 | 5 | 1 | 20.0% | SWE-agent 1.1.0 | Qwen3-Coder-480B | YES | 0/6 layers engaged behaviorally. |
| 9 | ULTRAREVIEW Phase 6 | 2026-05-06 | Post-Phase-5 fixes | 5 | 0 | 0.0% | SWE-agent 1.1.0 | Qwen3-Coder-480B | YES | 0/6 layers engaged. $0.84. |
| 10 | V1R 4-arm smoke | 2026-05-03 | BL/V1/V1R-map/V1R-map+hook on 15 tasks | 15 | partial | - | SWE-agent | Qwen3-Coder-480B | PARTIAL | V1R brief was EMPTY for all 15 tasks (CONFIDENCE_FLOOR=0.3 bug). |
| 11 | OH smoke r2 | 2026-05-07 | OH + GT, 10 tasks | 10 | unknown | - | OpenHands 0.54.0 | Qwen3-Coder-480B | YES (layers) | All layers except L6 broken. Legacy gt_hook.py routed instead of real hooks. |
| 12 | OH smoke gate (first) | 2026-04-28 | OH v0.44.0 + GT watcher | 10 | 0 | 0% | OpenHands 0.44.0 | Qwen3-Coder-480B | YES | Agent never SAW GT evidence. Watcher logged to files, not observation stream. |
| 13 | v7.5 holdout ablation | 2026-05-02 | Retrieval-level hypothesis testing | 40 (holdout) | n/a | n/a | Offline eval | n/a | YES | H1 falsified. H2+H5 best. 5/6 gates pass. Gate 1 CI fails. |
| 14 | Kernel n=1 live fire | 2026-05-01 | GT kernel on babel-1141 | 1 | 0 | 0% | OpenHands | mimo-v2-flash | YES | Kernel blocked root-scaffold, agent redirected to gold file in 14s. Resolved=0 (dithered). |
| 15 | Phase 1 validation | 2026-05-06 | Proper launcher, cfn-lint-3890 | 1 | 0 | 0% | SWE-agent | Qwen3-Coder-480B | YES | First time all 4 behavioral tests passed. Root cause of BUG-A/B was launcher bypass. |

---

## Best vs Cleanest

The **best numerical result** is noisy GT at 6/30 (20%). The **cleanest run** is compression GT at 4/30 (13.3%).

These are different because the noisy GT run is unverified and likely reflects stochastic variance rather than GT contribution. The two extra resolves (beets-5495 and xarray-9971) appeared in the noisy run, disappeared in the clean GT run, and did not reappear in the compression run. At n=30, the probability that 6/30 vs 4/30 is a real difference is p=0.73 (Fisher's exact, two-tailed). This is nowhere near statistical significance.

The compression run is "cleanest" because:
1. Every metric is independently verified (eval on VMs, not session memory)
2. Zero infra failures (30/30 tasks ran)
3. Zero GT contamination in patches
4. Zero noise markers in agent context
5. Matches baseline exactly — no regressions, no false positives
6. The JetBrains NeurIPS 2025 "Complexity Trap" finding (observation slot presence matters independently of content) is properly implemented

The noisy GT run produced 2 extra resolves but also injected 2,200 tokens of noise per task (65% of total GT output), corrupted 5 patches via L5 advisory leaks, and had the cry-wolf effect training agents to ignore all GT evidence by iteration 20. It is not a foundation to build on.

---

## What We Learned From Each Run

### Run 1: OH Baseline (4/30)

The control. No GT, no hooks, no evidence, no brief. Pure OpenHands 0.54.0 with Qwen3-Coder-480B via LiteLLM proxy on Vertex MaaS.

**Resolved:** beancount-931, briefcase-2075, weasyprint-2303, twine-1225.

**Lesson:** These 4 tasks are "easy" for the OH+Qwen3 combination. They resolve with or without GT. Any GT run that loses any of these 4 has regressed.

### Run 2: Noisy GT (claimed 6/30, UNVERIFIED)

All 6 GT layers active. 75% of L3 evidence blocks were empty. L5 advisory fired 4 times per task, never reached the agent, and corrupted 5 patches.

**Resolved (claimed):** beancount-931, briefcase-2075, weasyprint-2303, twine-1225, beets-5495, xarray-9971.

**Lesson:** The 6/30 number is the best GT has ever shown, but it is unverified. The +2 over baseline (beets-5495, xarray-9971) is not reproducible: beets-5495 appears in clean GT (run 3) but xarray-9971 does not appear in any subsequent run. Given the deep audit found 17 bugs in GT's integration at this point, attributing the +2 to GT rather than stochastic variance requires faith, not evidence.

### Run 3: Clean GT — Eliminate Empty Evidence (3/29)

The "do the obvious thing" fix. All 13 noise issues from DEFICIENT.md and DEFICIENT_DEEP.md were fixed. Empty evidence blocks suppressed. Reindex hidden. L5 removed. Total GT noise reduced from 2,200 tokens/task to ~400 tokens/task.

**Resolved:** beancount-931, briefcase-2075, twine-1225. Lost weasyprint-2303. Briefcase-2085 had infra failure.

**Lesson:** This is the most important run in the entire experiment history. Removing noise HURT performance. The clean GT run lost 1 task (weasyprint-2303) compared to baseline, and the 2 noisy-GT-only tasks (beets-5495, xarray-9971) also disappeared. The obvious interpretation — "noise is bad, remove it" — was wrong.

The JetBrains NeurIPS 2025 "Complexity Trap" research explains why: observation slot PRESENCE matters independently of content. When the agent sees `<gt-evidence>` blocks on every edit (even empty ones), the structural rhythm keeps the agent anchored in a "check before proceeding" pattern. When those blocks vanish, the agent loses the pacing signal and goes into unconstrained scaffolding mode.

### Run 4: Phase 5 Enforce GT (3/29)

Built on clean GT + added scaffold stripping (delete new files before git add), L3 localization framing ("this is/is not a briefed candidate"), and L5 redirect ("you haven't edited any candidate files yet").

**Resolved:** Same 3 as clean GT. No improvement.

**Lesson:** The enforcement mechanisms don't help because the dominant failure mode (scaffolding) is not addressable by GT's current architecture:
- Scaffold strip works mechanically (removes new files) but the agent still wastes its iteration budget creating them
- L5 redirect fires too late (33%/66% of max_iter) — by then the agent has committed to its scaffolding strategy
- L3 framing is ignored because the agent was already ignoring all GT evidence

### Run 5: Compression GT (4/30)

Empty evidence blocks replaced with `[GT_OK] No concerns.` placeholder instead of being eliminated. This preserves the structural pacing signal while eliminating the cry-wolf effect.

**Resolved:** beancount-931, briefcase-2075, weasyprint-2303, twine-1225. Same 4 as baseline.

**Lesson:** Compression recovered the weasyprint-2303 regression from clean GT. The structural observation thesis is confirmed: placeholder presence matters. But compression does not beat baseline — it only matches it. GT with placeholders is equivalent to no GT at all on resolve rate.

### Run 6: Phase 4 Clean (SWE-agent, 3/30)

Same 30 tasks, SWE-agent instead of OpenHands. GT with no oracle leaks, in-container graph.db build.

**Lesson:** SWE-agent's 63% patch submission rate is the bottleneck, not GT. The harness switch from SWE-agent to OpenHands (100% patch rate) was worth more than any GT improvement.

### Run 7: Phase 5 SWE-agent 300-task (5/200)

The only large-n run. 200 tasks on gt-t0.

**Lesson:** 2.5% resolve rate. 34% submission rate. The thought_action parsing mode for Qwen3 was the wrong choice (vs native tool calling). GT cannot compensate for harness-level inefficiency. This run is not comparable to the OH runs.

### Runs 8-9: ULTRAREVIEW (1/5, 0/5)

5-task smokes on SWE-agent Track 4, before and after RC fixes.

**Lesson:** The big discovery was that 0/6 layers were engaged behaviorally despite all isolated probes passing. Root cause: launches bypassed `run_with_gt_hook.py`, so `GTTrack4PreRunHook` was never added. The brief was 0 bytes on all tasks. "Delivery works, engagement does not" became the defining insight of the project.

### Run 10: V1R 4-arm smoke (empty briefs)

BL/V1/V1R-map/V1R-map+hook on 15 SWE-bench-Live tasks.

**Lesson:** CONFIDENCE_FLOOR=0.3 in v1r_brief.py filtered every candidate. V1R-map ran as baseline. The architectural comparison was degraded. The only useful signal was steps-to-gold: V1R-map+hook showed 36% fewer wasted edits, but this is the lean hook's contribution, not the brief's.

### Run 11: OH smoke r2 (all layers broken)

10-task smoke where every layer except L6 was broken.

**Lesson:** The wrapper used legacy gt_hook.py instead of real hooks. L1 was a static template. 161 `[GT_PATCH_SHAPE]` tags, zero real evidence tags. The smoke gate report said PASS on all checks because it only checked token presence, not content quality. Lesson: **gate checks that don't verify content are worthless.**

### Run 12: First OH smoke (agent never saw GT)

10 tasks on OH v0.44.0.

**Lesson:** The watcher ran gt_hook.py but output went to log files, not the agent's observation stream. Agent never saw GT evidence. Lesson: **wiring is not delivery is not engagement.** Three tiers that must all be confirmed before declaring anything "working."

### Runs 13-15: Retrieval-level and kernel experiments

These are on different axes than the main OH integration:
- **v7.5 holdout:** H1 (structural seed expansion) falsified. H2+H5 (path-specificity weighted reach + depth=3) best, but Gate 1 CI fails (ci_lo=-0.029). Retrieval improvements have diminishing returns.
- **Kernel n=1:** Proved the behavioral intervention concept (blocked root-scaffold, agent redirected to gold file in 14 seconds). But resolved=0 because the agent dithered after redirection.
- **Phase 1 validation:** First time all 4 behavioral tests passed. Root cause of prior failures was operational (wrong launcher), not code bugs.

---

## Layer-by-Layer Strength Analysis

### L1: Pre-task Brief (File Localization)

**What it is good at:** Providing 3-5 candidate file names before the agent reads any code. When files are correct, the agent has a foothold from iteration 1.

**What it is bad at:** The brief's candidate accuracy was never audited against gold edit files across the full 30 tasks. This is the single most impactful unknown in the entire project. We have anecdotal evidence that localization is not the bottleneck (agents reach gold files 88% of the time without GT, per 5-case diagnosis), but we never measured L1's precision/recall.

**Evidence:** The brief fires on most tasks (14 lines, ~210 tokens). Double-wrapping bug fixed. Fallback brief replaced with empty. But brief content quality was never measured against gold.

**Generalizable or benchmark-specific:** Generalizable in principle — graph-based file localization is language-agnostic. But the actual localization accuracy is unknown.

**Verdict:** Core product. The brief is the right mechanism. The content needs auditing.

### L3: Post-edit Evidence (Caller/Contract/Pattern/Structural/Semantic)

**What it is good at:** Providing caller counts, contract signatures, and structural patterns for files with rich graph connections.

**What it is bad at:** 75% of evidence blocks are empty on SWE-bench-Live repos because the graph is 70-80% name_match edges at low confidence, and the abstention filter (0.55, later 0.40) correctly rejects most findings. The 5 evidence families require dense cross-file call graphs to produce useful output. Framework-heavy repos (cfn-lint rules dispatched by runtime, standalone linter rules) have sparse graphs.

**Evidence:**
- Token-match engagement rate: ~20% (median 0.187 in Phase 4 smokes, below 0.30 threshold)
- The one resolved task in ULTRAREVIEW had below-average L3 engagement — resolve was NOT GT-driven
- Removing empty L3 blocks hurt (clean GT regression), but replacing with placeholders recovered baseline
- Real evidence blocks (~4.5 per task) contain genuine caller/contract data, but the agent ignores them

**Generalizable or benchmark-specific:** The sparse graph problem is general — any repo where functions are dispatched by framework registration (Django views, Flask routes, FastAPI endpoints, pytest fixtures, linter rules) will have sparse call graphs. This is not a benchmark-specific issue.

**Verdict:** L3's only proven contribution is PACING (structural observation presence), not CONTENT. The evidence families are technically correct but practically useless because (a) most repos have sparse graphs, and (b) the agent ignores even good evidence. Deprioritize content quality work. Maintain placeholder pacing.

### L3b: Post-view Evidence (Structural Coupling)

**What it is bad at:** Same sparse graph problem as L3. Dedup=0 across all tasks (agents rarely view the same file twice). Class coupling analysis requires classes with 2+ methods sharing attributes — most SWE-bench repos don't hit this threshold.

**Verdict:** Deprioritize. Same issues as L3, lower firing rate.

### L4: Tools/Prefetch (gt_query, gt_search, gt_navigate, gt_validate)

**What it is good at:** Prefetch (injecting pre-computed gt_query results for issue-relevant symbols into the brief) is technically sound. The AutoCodeRover/Aider-inspired pattern of issue-text-seeded graph queries is the right approach.

**What it is bad at:** Tool usage is 0% across ALL runs. The agent never calls gt_query despite:
- Imperative docstring
- Instance_template directive ("run gt_query instead of grep")
- 4-line tool footer in brief
- Tool registered and available on PATH

The agent uses `bash` + `grep -r` for every caller query (89 near-misses in Phase 4 alone). This is a model bias issue — Qwen3-Coder's training heavily weights shell tools over custom CLIs. No amount of prompt engineering has changed this behavior.

**Evidence:** 3 total gt_query invocations across all runs (all on cfn-lint-3890 in Phase 4). 0 invocations in Phase 6.

**Generalizable or benchmark-specific:** The tool-usage-is-zero problem is likely model-specific (Qwen3-Coder). Claude and GPT-4 may behave differently. But we have no data.

**Verdict:** Tool surface is dead. Prefetch is the right pattern — inject evidence at init time instead of hoping the agent discovers tools. Kill the tool footer. Double down on prefetch quality.

### L5: Pre-submit Gate/Checkpoint

**What it is good at:** In theory, catching structural problems (hallucinated imports, caller-blind edits, scratch files) before the agent submits. The kernel n=1 live fire proved the behavioral intervention concept works: blocking a root-scaffold edit redirected the agent to the gold file in 14 seconds.

**What it is bad at:** In OH's event model, L5 cannot fire before the agent's submit decision because submit is atomic. The advisory fires on finish (too late), on git diff --cached (contaminates patch extraction), and at 33%/66% of max_iter (agent already committed to scaffolding strategy). In SWE-agent, L5 fires as a state command on the submit tool, which works better architecturally but has its own issues (gate too lenient, never catches cfn-lint failures).

**Evidence:**
- Advisory fired 4x per task, never reached agent before submit (noisy GT)
- Advisory contaminated 5 patches (fixed)
- L5 percentage checkpoints at 33%/66%: unresolved>0 never fires because unresolved check requires source edits, but scaffolding agents don't edit source files
- Kernel n=1 proof: behavioral intervention works when timed correctly

**Verdict:** The concept is proven (kernel n=1) but the implementation is architecturally mismatched to OH's event model. The checkpoint timing (33%/66%) is too late. An iteration-1 or iteration-3 gate would catch scaffolding before the agent commits. This requires understanding the agent's first-action distribution, which we have data for (22/26 first edits were root-scaffolds in the counterfactual smoke).

### L6: Incremental Reindex

**What it is good at:** Keeping graph.db fresh after each source edit. Sub-500ms latency (24ms measured). Correct implementation. Only layer with zero bugs across all audits.

**What it is bad at:** The freshness metric is tautological (the post-edit hook names the file the agent just edited — always true by construction).

**Verdict:** Keep. Works correctly. Silent. No harm. Supports L3/L4 evidence freshness.

### Scaffold Strip

**What it is good at:** Mechanically removing new files (reproduce_issue.py, test_fix.py, debug_*.py) before git add -A. Same pattern as SWE-agent's submit command.

**What it is bad at:** Does not change resolve rate. The agent still wastes its iteration budget creating scaffolding — strip only removes the evidence of scaffolding from the final patch, it doesn't prevent the wasted iterations. 15/30 tasks had scaffolding in the clean GT run.

**Verdict:** Keep as hygiene (cleaner patches for evaluation). Not a lever for resolve rate improvement.

### Truncation Fix

**What it is good at:** Prevents 30K patch truncation in live_utils.complete_runtime by writing to file instead of stdout capture.

**Verdict:** Keep. Infrastructure bug fix. Not GT-specific.

---

## Ranking

### 1. Best foundation to build on: Compression GT

Compression GT (run 5) is the only configuration that:
- Matches baseline resolve rate (4/30)
- Has zero regressions
- Has zero infra failures
- Has zero contamination
- Implements the structural observation thesis correctly
- Is the simplest integration (brief + placeholder evidence + reindex)

### 2. Best raw result: Noisy GT (UNVERIFIED)

6/30 is the highest number GT has ever produced. But it is unverified, unreproducible, and statistically indistinguishable from baseline at p=0.73. Building on noisy GT means accepting 17 known bugs and 2,200 tokens/task of noise as features, not bugs.

### 3. Safest mechanism: L1 Brief (file localization)

The brief is the only GT mechanism with a plausible causal path to resolve rate improvement. If the candidate files are correct, the agent starts from the right location. The 5-case diagnosis showed agents reach gold files 88% of the time anyway, but the brief reduces wasted exploration iterations. The V1R smoke showed 36% fewer wasted edits before first gold edit with the lean hook (which includes the brief).

### 4. Most generalizable mechanism: L6 Reindex + L1 Brief

L6 is perfectly generalizable — tree-sitter parsing works on any language, incremental reindex is sub-500ms, zero harm. L1 brief is generalizable in principle (graph-based localization is language-agnostic) but its actual accuracy is unmeasured.

### 5. Least promising direction: L3 content quality / L4 tool surface

Improving L3 evidence content quality has been the single largest engineering investment (~5 sessions, ~20 hours) and has produced exactly zero resolve rate improvement. The agent ignores evidence regardless of quality. L4 tool surface is dead — 0% usage across 200+ task-runs. Both of these directions are empirically falsified.

---

## What To Stop, What To Keep, What To Double Down On

### STOP

- **L3 content quality work.** Five sessions of engineering (abstention threshold tuning, 5-family evidence architecture, adaptive filtering, framing, lost-in-the-middle optimization) produced zero improvement. The agent does not read the evidence. The content is irrelevant. Stop polishing evidence that nobody reads.

- **L4 tool surface engineering.** 0% usage across all runs. No amount of prompt engineering, tool renaming, imperative docstrings, or instance_template directives has changed Qwen3-Coder's behavior. The model uses grep. Accept this. If future models have different tool-usage behavior, revisit.

- **L5 pre-submit gate in OH's event model.** OH's atomic submit makes pre-submit intervention architecturally impossible. The advisory fires too late, contaminates patches, and provides no value. The kernel's iteration-1 intervention pattern is better but requires a different integration point.

- **V1R brief with CONFIDENCE_FLOOR.** The 0.3 threshold killed all output on SWE-bench-Live. The v1r_brief.py codepath is dead unless the scoring is recalibrated. Use v7_brief.py directly.

- **SWE-agent as the primary harness.** SWE-agent's 34% submission rate and thought_action parsing are fundamental bottlenecks that GT cannot overcome. OH's 97-100% patch rate is strictly better. All future experiments should use OpenHands.

- **Multi-hypothesis retrieval iteration without downstream eval.** v7.5 ran H1/H2/H3/H5 through 13 holdout variants. Gate 1 CI never passed. The retrieval channel is saturated — agents reach gold files without GT. More retrieval engineering will not move resolve rate.

### KEEP

- **L1 Brief.** The brief provides file localization. Compression GT proved that the brief alone (plus placeholders) matches baseline. The brief is the minimum viable GT.

- **Compression/placeholder pacing.** The JetBrains NeurIPS 2025 finding is confirmed: structural observation presence matters independently of content. Keep `[GT_OK] No concerns.` on every edit. Do not eliminate empty evidence.

- **L6 Reindex.** Works correctly, zero bugs, zero harm, sub-500ms. Keeps graph.db fresh. Keep it.

- **Scaffold strip.** Hygiene. Cleaner patches. Keep as a submit-time cleanup, not as a resolve-rate lever.

- **Truncation fix.** Infrastructure bug fix. Keep permanently.

- **gt_interactions logging (when fixed).** Currently broken (flush never fires). When working, this is the only way to measure agent behavior in response to GT. Fix the flush, then use it.

### DOUBLE DOWN ON

- **L1 brief localization accuracy audit.** The single highest-ROI unknown in the entire project. For each of 30 tasks, check: does the brief's top-3 candidate files include at least one gold edit file? If yes, the problem is downstream (agent doesn't follow localization). If no, the problem is upstream (GT points at wrong files). This 30-minute research task has never been done. Do it before any more engineering.

- **Iteration-1 behavioral intervention.** The kernel n=1 proof showed that blocking a root-scaffold first edit redirected the agent to the gold file in 14 seconds. 22/26 first edits in the counterfactual smoke were root-scaffolds. An early intervention (at iteration 1-3, not 33-66%) that says "you are scaffolding, edit source files from the brief instead" could address the dominant failure mode (scaffolding trap: 8/30 = 27% of Phase 4 tasks). This is the only mechanism with demonstrated behavioral impact.

- **Different model testing.** All experiments used Qwen3-Coder-480B. The 0% L4 tool usage, the scaffolding behavior, and the evidence-ignoring pattern may all be model-specific. One 30-task run with Claude or GPT-4o on the same tasks would determine whether GT's value is model-dependent.

- **Understanding what the 4 baseline resolves have in common.** Beancount-931, briefcase-2075, weasyprint-2303, and twine-1225 resolve with and without GT. Understanding WHY these resolve (single-file? clear error message? small diff?) and what distinguishes them from the 26 unresolved tasks would inform where GT could help.

---

## Final Recommendation

**GT is net-zero on resolve rate.** Across 5 verified configurations on the same 30 tasks, GT never outperforms baseline. The best unverified result (6/30 noisy GT) is p=0.73 vs baseline and unreproducible. The engineering investment (17 bugs fixed, 13 noise fixes, 5 sessions of L3 quality work, 2 harness integrations, $100+ in VM costs, $50+ in LLM costs) has produced a system that, at best, matches a system that does nothing.

**The product thesis — "deterministic MCP, $0 AI, compiler-grade codebase intelligence" — is not falsified.** It is untested. GT's signal quality has never been proven to be correct (L1 localization accuracy never audited), and the agent has never been observed to act on GT's signal in a way that changes outcomes (L3 engagement rate ~20%, L4 usage 0%).

**Concrete next step:** Do not build more features. Do not run more 30-task experiments. Do the L1 localization accuracy audit (30 minutes, $0). If L1 points at the right files on >60% of tasks and the agent still doesn't resolve them, the problem is post-localization and GT needs to pivot from information delivery to behavioral intervention (kernel-style). If L1 points at the wrong files on >50% of tasks, the problem is retrieval quality and GT needs better localization before anything else.

**The hard truth:** The dominant failure modes — scaffolding traps (27%), multi-file scope misses, wrong fix mechanisms — are not addressable by any information-delivery system. They are addressable only by behavioral constraints (preventing the agent from scaffolding, forcing multi-file edits, embedding test expectations in the prompt). GT's architecture is information-delivery. The kernel's architecture is behavioral intervention. The kernel showed a proof-of-concept (n=1). The kernel is deferred until the product is shipping. The product cannot ship until it beats baseline. The product cannot beat baseline with information-delivery alone.

This is a strategic impasse. The thing that works (behavioral intervention) is deferred behind the thing that doesn't work (information delivery) being "shipping-grade." Consider removing the deferral and making the kernel the primary product instead of a Phase N add-on.

---

## Appendix: Evidence Discipline Notes

1. The "6/30 noisy GT" number (run 2) is UNVERIFIED. It comes from a prior session's DEFICIENT.md. The output.jsonl files exist locally but the evals were run on VMs that are now stopped. Until re-evaluated, treat this as an upper bound, not a measurement.

2. Fisher's exact test on 6/30 vs 4/30 gives p=0.73 (two-tailed). This means there is a 73% chance that the observed difference is due to random variation alone. This is not evidence of GT's effectiveness.

3. Patch-apply failures are NOT infrastructure errors. They are wrong patches (agent behavior). The 7 patch-apply failures in run 3 are 7 tasks where the agent wrote syntactically invalid diffs. Calling them "errors" misattributes the failure mode.

4. Briefcase-2085 (container stuck) in runs 3 and 4 is a genuine infrastructure failure. It reduces the denominator from 30 to 29 for those runs.

5. The "8-13/30 target after fixes" stated in DEFICIENT.md was optimistic by a factor of 2-3x. Actual result after fixes: 3/29 (clean GT), 4/30 (compression GT). Targets should be calibrated from empirical data, not from bug-fix expectations.

6. The 5-case diagnosis (2026-04-30) found that retrieval improvements won't move 0/50 resolve because the bottleneck is post-localization. This finding held across all subsequent runs. Brief recall is capped at ~22% but agents reach gold files in 88% of tasks without GT. The bottleneck prediction was correct.

7. The "all layers working in isolation but broken in production" pattern repeated across ULTRAREVIEW (Phase 4+6), OH smoke r2, and Phase 1 validation. Root causes were different each time (launcher bypass, legacy hook routing, watcher-to-logfile). Integration testing catches what unit testing cannot.

8. Total verified spend on experiments: ~$150-200 in GCP VM costs + ~$50 in LLM API costs across all sessions. Total engineering time: ~60-80 hours across 12+ sessions. Output: a system that matches baseline.
