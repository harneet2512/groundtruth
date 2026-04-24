# Ablation Results — Qwen3-Coder 2026-04-24

## Purpose

Isolate what contributes to GT's observed lift on Qwen3-Coder (0/10 raw → 3/10 with GT). The fresh no-GT baseline revealed model_scaffold_mismatch: Qwen emits multi-block monologue responses that the thought_action parser can't execute. GT's prompt constrains Qwen into single-action turns. The question: is the lift from scaffold stabilization or code intelligence?

## Setup

- Model: qwen3-coder-480b-a35b-instruct-maas via Vertex AI MaaS
- Suite: frozen_gt_astropy10.txt (10 astropy tasks)
- Runner: SWE-agent 1.1.0 with thought_action parser
- Commit: bd98df1 (budget split)

## Results

| Ablation | Condition | patched | resolved | zero_edit | resolved IDs |
|---|---|---|---|---|---|
| **A** | no_GT_raw | 1/10 | 0/10 | 9/10 | — |
| **B** | no_GT + action_format_repair | 5/10 | 2/10 | 5/10 | 12907, 13236 |
| **C** | B + test_before_submit | 6/10 | 2/10 | 4/10 | 12907, 13579 |
| **D** | GT_prompt_shell (no intelligence) | 5/10* | 2/5* | 5/10* | 13579, 14309 |
| **E** | GT_budget_split_current (nolsp) | 7/10 | 3/10 | 3/10 | 12907, 13453, 14309 |

*D incomplete: 3 tasks hung (13398, 13579, 13977 ran 2-4 hours with Vertex API timeouts). 5/10 produced patches. Eval on 5 submitted patches: 2/5 resolved.

## Analysis

### A → B: scaffold stabilization effect

Adding "emit exactly one fenced bash block per turn" to the system prompt:
- patched: 1 → 5 (+4)
- resolved: 0 → 2 (+2)
- zero_edit: 9 → 5 (-4)

**This is the dominant effect.** The one-action format instruction alone accounts for most of the observed GT lift. Without it, Qwen produces 43-block monologues that the parser can't execute.

### B → C: test-before-submit effect

Adding "run pytest before submit":
- patched: 5 → 6 (+1)
- resolved: 2 → 2 (unchanged)

**Marginal.** One more patch produced, no additional resolution. The test prompt may help Qwen discover its fix is wrong on some tasks, but it doesn't improve solve rate on this sample.

### B → D: GT scaffold structure effect

D uses the full GT prompt structure (detailed instruction format, editing patterns, parsing rules) but without actual GT evidence/steers/tools:
- patched: comparable (5/10 vs 5/10 on completed tasks)
- resolved: comparable rate (2/5 on submitted = 40%, vs B's 2/10 = 20%)

**Inconclusive due to D's incomplete run.** The 3 hung tasks skew the comparison. D's prompt structure may be helping (40% hit rate on patches) or the difference may be from which tasks happened to complete.

### B → E: full GT effect

E (full GT with code intelligence) vs B (format repair only):
- patched: 5 → 7 (+2)
- resolved: 2 → 3 (+1)

**GT adds +1 resolved and +2 patches over format repair alone.** On n=10, this is within stochastic variance. Cannot claim code intelligence contribution is proven.

## Conclusions

1. **The primary GT lift is scaffold stabilization.** Action format repair (B) accounts for 0→2 resolved and 1→5 patched. This is a prompt engineering effect, not code intelligence.

2. **GT's code intelligence contribution is at most +1 resolved.** E vs B: 3 vs 2 resolved. Within stochastic variance for n=10. Cannot be attributed with confidence.

3. **GT's incremental value over a repaired baseline is uncertain.** To prove code intelligence adds value, need: (a) larger task set (≥50 tasks), (b) multiple runs for variance estimation, (c) controlled D comparison (fix the hung tasks).

4. **The historical 5/10 baseline remains invalid.** Cannot be reproduced on this scaffold/model/runner. All comparisons must use the fresh A/B baselines from this ablation.

## Corrected status

Orient-budget accounting bug is fixed. Fresh no-GT baseline revealed Qwen/scaffold incompatibility (model_scaffold_mismatch). Ablation shows the primary GT lift is scaffold stabilization (action format repair). GT code intelligence contribution is +1 resolved over repaired baseline — within stochastic variance, not a proven product win.
