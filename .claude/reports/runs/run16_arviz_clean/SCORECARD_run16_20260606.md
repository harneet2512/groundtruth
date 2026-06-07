# run16 — CLEAN online gt_trial run (arviz-devs__arviz-2413, GT-on, DeepSeek v4-flash), 2026-06-06

First fully-clean online run of the session: stable codespace (Monitor kept it alive — no /tmp wipe),
full-stack env, eval completed with 0 infra errors, trajectory audited chronologically by a verifier.

## gt_trial §5 scorecard

### Tier 1 — OUTCOME
| metric | value |
|---|---|
| resolved | **false** (official eval: resolved 0 / unresolved 1 / errors 0) |
| baseline_pass | false (arviz-2413 not in frozen resolved_ids) |
| flip | **false** |
| regression | false |

### Tier 2 — CAUSALITY (gt_gt gates, from output.jsonl)
| gate | value | evidence |
|---|---|---|
| delivered | 1 | EV1 `<gt-task-brief> 1. arviz/plots/hdiplot.py (def plot_hdi())`; EV9/71 post-view contract hooks |
| correct | **PARTIAL** | structural claims accurate, BUT residual leak (caller/co-change test refs) — now fixed |
| consumed | 1 | agent edited the gold function (EV70-71); did NOT act on the leaked tests |
| fair_probe | **0** | the issue self-localizes — names plot_hdi + links hdiplot.py#L171-182, even says "a TypeError would be helpful" |
| right_trajectory | **0** | correct site, wrong fix detail |
| **gt_caused** | **false** | localization was issue-driven, not GT-caused |

### Tier 3 — LOCALIZATION
gold_file_reached=1 · first_gold_rank=**#0** (`edit_target plot_hdi @ hdiplot.py score=1015 tier=high`) · gold_edited=1

### Tier 6 — LEGITIMACY
env_full_stack=**GREEN** (`ENV gates: EMBEDDER=on(ONNX) FTS5=on`; `HOST-PRIMARY brief OK — host ONNX
embedder … no silent W_SEM=0`) · test_names_leaked in run16=**>0** (graph-map `called by:` 6 test fns,
`Also changes:` test file, post-view `[test]` callers) → **run16 itself had residual leakage** → not a
clean flip surface; BUT the agent did NOT benchmaxx (verified) and the grading test was never leaked.
**Leak now CLOSED + verified 0** (commits dcbb9790 assertions, 4bfd7a57 ego, 047aa825 callers/co-change).

## Why no resolve — post-localization correctness (NOT model failure)
The agent localized perfectly and edited `plot_hdi` at the exact right spot, raising on string/categorical
`x`. Graded wrong on two exact contracts the HIDDEN test pins: gold wants `NotImplementedError` + the exact
message `"…does not support categorical data. Consider using arviz.plot_forest()."`; the agent wrote
`TypeError` + a self-authored message — patterning on the only error-test visible to it
(`test_plot_hdi_datetime_error`, which uses TypeError) AND following the issue's own (misleading) hint that
"a TypeError would be helpful." No no-leakage context layer can supply that exact hidden-test contract.

## Verdict
- **Pipeline works online**: full-stack, semantic real, gold localized #0, eval graded — the whole
  apparatus the W_SEM fix + leak sweep restored is now legitimate.
- **arviz is not a fair GT flip surface** (issue self-localizes; the contract is hidden-test-only) — a
  re-run will not flip it. Not a GT failure.
- **Next**: a BATCH on flip-candidate tasks where the issue does NOT pre-localize the gold (GT localization
  is actually the bottleneck) — that is where GT can cause a flip, measured paired vs the frozen baseline.

## Commits
host-brief routing · leak sweep (dcbb9790/4bfd7a57/047aa825) · localization stack. Trajectory: output.jsonl (this dir).
