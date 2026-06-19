# Regression Autopsy — Qwen3-Coder 2026-04-24

## Run summary

| | nolsp | lsp-hybrid | baseline (historical) |
|---|---|---|---|
| resolved | 2/10 | 3/10 | 5/10 |
| patches produced | 4/10 | 8/10 | — |

Baseline "always resolved" tasks: 12907, 13453, 13579, 14309.
Regression cases (baseline solved, GT failed):
- **nolsp/13453** — 0 edits, bootstrap crash
- **nolsp/13579** — 0 edits, bootstrap crash
- **lsp/13453** — 5 edits, correct file, wrong fix

## Classifier results

| case | behavioral_alignment | steer_targets_gold | low_info_confirmation | repeated_steer | noncompliance | failure_class | excluded_from_effectiveness |
|---|---|---|---|---|---|---|---|
| lsp/13453 | 5 | True | True | 4 | False | steer_too_noisy | No |
| nolsp/13453 | 0 | n/a | False | 0 | False | bootstrap_infra_failure | Yes |
| nolsp/13579 | 0 | n/a | False | 0 | False | bootstrap_infra_failure | Yes |

## Case 1: lsp/13453

### Timeline

| cycle | event | detail |
|---|---|---|
| 1 | checkpoint_startup | briefing emitted |
| 14 | material_edit | astropy/io/ascii/html.py |
| 14 | steer_delivered | micro → html.py (same cycle as edit) |
| 18 | ack_not_observed | agent didn't run gt_check |
| 19 | material_edit | html.py (2nd edit) |
| 19 | steer_delivered | material_edit → html.py (repeat) |
| 23 | material_edit + steer | html.py (3rd edit, 3rd steer) |
| 25 | material_edit + steer | html.py (4th edit, 4th steer) |
| 30 | material_edit + steer | html.py (5th edit, 5th steer) |
| 33 | terminated | periodic_no_edit |

### Autopsy

| Field | Value |
|---|---|
| instance_id | astropy__astropy-13453 |
| baseline resolved? | YES (always) |
| GT resolved? | NO |
| gold file | astropy/io/ascii/html.py |
| GT steered file | astropy/io/ascii/html.py (correct) |
| agent edited file | astropy/io/ascii/html.py (correct) |
| gold patch | 2 lines: `self.data.cols = cols` + `self.data._set_col_formats()` |
| agent patch | 16+ lines: full formatting setup, cached str_vals, modified multicolumn loop |
| steer arrived before/after edit? | same cycle (concurrent) |
| did agent edit steered file? | YES (5 times) |
| did steer add new info? | NO (agent already editing that file) |
| did steer cause extra exploration? | YES (5 repeated steers consumed context) |
| **failure class** | **steer_too_noisy** |

### Causal analysis

A. **Steer correct?** YES — targets the gold file.
B. **Steer useful?** NO — agent was already editing html.py when the first steer arrived. Low-information confirmation.
C. **Seen before decision?** CONCURRENT — same cycle as first edit.
D. **Followed behaviorally?** YES — 5 edits to steered file. `behavioral_alignment = 5`.
E. **Help or hurt?** HURT (marginally). The ack mechanism didn't count the edits as engagement (expects gt_check, not file edits), so it re-delivered the same steer 4 more times. Each re-delivery consumed context and may have prompted over-revision (16 lines vs 2-line gold fix).
F. **If ignored?** NOT ignored — agent followed the steer. The metric's `ack_not_observed` is a false negative on the behavioral dimension.

## Case 2: nolsp/13453

### Timeline

| cycle | event |
|---|---|
| 1 | pre_edit_briefing |
| 1 | checkpoint_startup (empty) |
| 1 | startup_complete |
| 1 | cycle |
| — | terminated. 0 edits. 0 steers. 0 patch. |

### Autopsy

| Field | Value |
|---|---|
| instance_id | astropy__astropy-13453 |
| baseline resolved? | YES (always) |
| GT resolved? | NO |
| total cycles | 1 |
| material edits | 0 |
| steers delivered | 0 |
| patch bytes | 0 |
| **failure class** | **bootstrap_infra_failure** |

### Causal analysis

The agent never started. `checkpoint_startup: empty` means the briefing returned nothing. The agent ran 1 cycle, produced 0 actions, and sweagent terminated. This happened on **6 of 10 nolsp tasks** — it is NOT task-specific. The nolsp arm is systematically broken at bootstrap for Qwen3-Coder on this config.

**NOT a steer problem.** No steer was ever delivered. Cannot be used as evidence for or against steer quality.

## Case 3: nolsp/13579

Same pattern as Case 2. 3 events, 0 edits, 0 patch. **bootstrap_infra_failure**.

## Conclusions

### Is the low engagement number trustworthy?

**Partially.** The existing ack mechanism counts explicit gt_check calls as engagement. On lsp/13453, the agent edited the steered file 5 times but ack counted only 3 of those cycles as engagement (via other paths). The `behavioral_alignment` metric from the trace classifier shows 5/5 — the agent WAS following. The discrepancy between ack_engagement=3 and behavioral_alignment=5 means the ack metric has a ~40% false-negative rate on this trace for the "edit the steered file" behavior pattern.

### Are agents ignoring good steers?

**No.** On lsp/13453, the agent edited the steered file every single time. `behavioral_alignment = 5`. The agent is NOT ignoring steers — the ack mechanism just doesn't fully count "editing the steered file" as engagement.

### Are steers bad/noisy/late?

**Noisy.** The steer targets the correct file (matches gold patch). But 5 steers to the same file is noise — steers 3-5 add nothing the agent doesn't already know. `repeated_steer_count = 4`. The ack-failure → re-delivery loop is the noise source.

### Is GT actively derailing baseline-success cases?

**nolsp arm is invalid.** 6/10 tasks die at cycle 1 due to bootstrap_infra_failure unrelated to steers. The nolsp arm cannot be compared against baseline — it is broken infrastructure, not evidence of GT harm. No steer was ever delivered on these tasks.

**lsp arm shows correct targeting and behavioral alignment, with repeated low-information steer delivery.** On lsp/13453, GT steered to the correct file (matches gold patch) and the agent followed behaviorally (5 edits to the steered file). The steer was a low-information confirmation (agent was already editing that file). The ack-failure → re-delivery loop generated 4 redundant steers to the same file.

**lsp harm is plausible but not proven.** The agent's patch was overengineered (16 lines vs 2-line gold fix). Whether steer repetition caused the over-revision or whether this is stochastic model variance cannot be determined without a side-by-side baseline trajectory on the same task with the same model and no GT hooks. The repeated steer dedup is justified as **noise reduction** independent of whether it improves outcomes — delivering the same steer 5 times adds context bloat with zero marginal information.

### What should change before another smoke?

1. ~~Investigate nolsp bootstrap crash~~ **DONE (2026-04-24).** Root cause: orient-budget accounting bug (startup consumed agent budget). Fixed in commit ff84895. Budget-split smoke: nolsp 3/10 resolved, 7/10 patched (up from 2/10 and 4/10).
2. **Bootstrap pre-smoke gate** — implemented in commit bd98df1. Blocks arms with >30% bootstrap failure rate.
3. **Steer dedup** — tested but not integrated (steer_dedup.py exists, not wired into hook). Justified as noise reduction. Deferred until ablations isolate scaffold vs intelligence effects.

### Fresh no-GT baseline finding (2026-04-24)

A fresh no-GT Qwen3-Coder baseline on the same frozen 10 tasks, same model,
same SWE-agent scaffold, no GT hooks/prompt/budget:

- **0/10 resolved, 1/10 patched, 9/10 zero-edit**

Root cause: **model_scaffold_mismatch**. Qwen emits a full solution in one
turn with ~43 code blocks (20,608 chars). SWE-agent's `thought_action` parser
executes only one action per turn (usually the final `submit`). The 42
intermediate actions (find, grep, sed, test) are never executed. See
`no_gt_qwen_scaffold_mismatch.md` for full analysis.

This means:
- Historical baseline (5/10) is **invalid** for comparison — cannot be
  reproduced on this scaffold/model/runner.
- Fresh no-GT baseline (0/10) is **not a fair intelligence comparator** — it
  measures parser compatibility, not coding ability.
- The GT condition includes prompt/scaffold stabilization that constrains
  Qwen into one-action turns. The observed lift (0/10 → 3/10) may come from
  scaffold stabilization, GroundTruth code intelligence, or both.
- Ablations (B-D) are required before any product claim.

### Corrected status

Orient-budget accounting bug is fixed. Fresh no-GT baseline revealed
Qwen/scaffold incompatibility: raw baseline is nonfunctional. Current GT lift
may reflect scaffold stabilization, code intelligence, or both. Need ablations
before product claims.
