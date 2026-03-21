# Branch Plan: [branch-name]

## Meta

- **Branch:** `[branch-name]`
- **Type:** research | productization | bugfix
- **Created:** [YYYY-MM-DD]
- **Author:** [name]

## Capability

What this branch adds or changes. One paragraph max.

## Problem

What specific problem this solves. Link to issue if applicable.

## Hypothesis

> If we [do X], then [metric Y] will [improve/change] because [reason].

Required for research branches. For productization, state the expected outcome instead.

## Scope

- [What IS in scope]

## Out of Scope

- [What is explicitly NOT in scope]

## Primary Metric

The single number that determines success. Must be measurable.

## Secondary Metrics

Optional. Other numbers worth tracking but not gating on.

## Cheapest Benchmark

The minimum eval to validate progress. Reference the eval ladder gate number.
Do NOT default to 300-task. Use `/eval-ladder` to choose.

## Merge Threshold

Exact number(s) that earn a merge to main. E.g., "obligation recall >= 0.85 on fixture set with zero false positives."

## Kill Condition

When to abandon this branch. Required for research. For productization, use "N/A — ship incrementally" if appropriate.

## Gate Plan

- [ ] **Gate 0 — Unit Tests:** `[exact command]`
- [ ] **Gate 1 — Fixture / Microbench:** `[exact command or description]`
- [ ] **Gate 2 — 10-Task Diagnostic:** [details, or "deferred"]
- [ ] **Gate 3 — 50-Task Intermediate:** [details, or "deferred"]
- [ ] **Gate 4 — 300-Task Authoritative:** [details, or "promotion candidates only"]

## Files Likely to Change

- `path/to/file.py` — [reason]

## Risks

- **[Risk]:** [mitigation]

## Precision / Abstention Rules

For validation or obligation changes. Skip for non-validation work.

- What false-positive scenarios have been considered?
- Under what conditions should the feature abstain rather than guess?

## Output Contract

Optional. If this branch changes tool output format, document the before/after schema.

## Evaluation Notes

Optional. How to interpret eval results, known confounders, baseline comparisons.

## Status

Update as gates complete.

- [ ] Gate 0 passed
- [ ] Gate 1 passed
- [ ] Gate 2 passed
- [ ] Gate 3 passed (if applicable)
- [ ] Gate 4 passed (if applicable)

## Final Decision

Filled at branch end: **merge** | **continue** | **kill**

Rationale: [1-2 sentences]
