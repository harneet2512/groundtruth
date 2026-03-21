# Branch Plan: research/obligation-gate2

## Meta

- **Branch:** `research/obligation-gate2`
- **Type:** research
- **Created:** 2026-03-21
- **Author:** Claude / founder

## Capability

Validate the obligation engine's recall and precision on 10 real SWE-bench tasks. Determine whether obligation analysis catches coupled-change requirements that agents currently miss.

## Problem

The obligation engine (constructor_symmetry, override_contract, caller_contract, shared_state) passes unit tests and fixture evals, but has never been measured against real-world tasks. We don't know if the obligation types we detect actually appear in SWE-bench diffs, or if detection is precise enough to help rather than distract.

## Hypothesis

> If obligation analysis detects coupled-change requirements in SWE-bench diffs, then agents using check-diff will produce fewer incomplete patches. Measurable by a 10-task diagnostic showing obligation recall >= 0.80 with precision >= 0.90.

## Scope

- Hand-label 10 SWE-bench tasks for expected obligations
- Run obligation engine against gold-patch diffs for those 10 tasks
- Measure recall (obligations detected / obligations present) and precision (correct detections / total detections)
- Classify missed obligations by kind to identify gaps

## Out of Scope

- New obligation kinds (this branch measures existing ones)
- Briefing or adaptive context changes
- MCP tool surface changes
- Full 300-task evaluation
- Agent integration testing (this is engine-only measurement)

## Primary Metric

Obligation recall on 10 hand-labeled tasks: target >= 0.80.

## Secondary Metrics

- Precision (target >= 0.90 — false positives destroy trust)
- Coverage: what % of the 10 tasks have at least one detectable obligation
- Per-kind recall breakdown (constructor_symmetry, override_contract, caller_contract, shared_state)

## Cheapest Benchmark

Gate 0: `python -m pytest tests/unit/test_obligations.py -v` (existing, must still pass)
Gate 1: `python -m pytest tests/fixtures/obligation_diffs/ -v` (existing fixture corpus)

## Merge Threshold

- Recall >= 0.80 on 10-task labeled set
- Precision >= 0.90 (no more than 1 false positive in 10 tasks)
- Zero regressions on existing fixture corpus
- Labeled task set committed as reusable eval artifact

## Kill Condition

- Recall < 0.50 after 2 iterations of tuning obligation rules
- OR fewer than 3 of 10 tasks contain any detectable obligation (insufficient signal)

## Gate Plan

- [ ] **Gate 0 — Unit Tests:** `python -m pytest tests/unit/test_obligations.py -v`
- [ ] **Gate 1 — Fixture Corpus:** `python -m pytest tests/fixtures/obligation_diffs/ -v`
- [ ] **Gate 2 — 10-Task Diagnostic:** Run obligation engine against 10 hand-labeled SWE-bench gold patches, compute recall/precision
- [ ] **Gate 3 — 50-Task Intermediate:** Deferred. Only if Gate 2 recall >= 0.80
- [ ] **Gate 4 — 300-Task Authoritative:** Deferred. Only for merge to main

## Files Likely to Change

- `src/groundtruth/validators/obligations.py` — tuning detection logic if recall is low
- `tests/fixtures/obligation_diffs/` — adding labeled SWE-bench diffs
- `scripts/swebench/` — diagnostic script for running obligation eval on task set
- `benchmarks/swebench/` — task selection and labeling artifacts

## Risks

- **10-task sample is small:** Mitigate by selecting tasks that span different obligation kinds. Acknowledge statistical limits in findings.
- **Obligation kinds may not match SWE-bench distribution:** Mitigate by classifying missed obligations — if a common pattern isn't covered, note it for future work without scope-creeping this branch.
- **Gold patches may not expose obligations clearly:** Mitigate by selecting tasks with multi-file changes where coupled changes are likely.

## Precision / Abstention Rules

- If the obligation engine confidence is below 0.7 for a detection, abstain rather than report it
- Never report obligations for files not touched by the diff
- Comment-only changes must not trigger obligations (existing false-positive test covers this)
- New standalone functions must not trigger caller_contract obligations (existing FP test covers this)

## Output Contract

No tool output changes. This branch only measures existing engine output against labeled data.

## Evaluation Notes

- Recall = (obligations correctly detected) / (total obligations in labeled set)
- Precision = (correct detections) / (total detections including false positives)
- A "miss" means the obligation exists in the labeled set but the engine didn't detect it
- A "false positive" means the engine reported an obligation that doesn't exist in the labeled set
- Task selection should favor multi-file diffs where coupled changes are plausible

## Status

- [ ] Gate 0 passed
- [ ] Gate 1 passed
- [ ] 10 tasks selected and hand-labeled
- [ ] Gate 2 diagnostic run complete
- [ ] Results analyzed, decision made

## Final Decision

Pending.
