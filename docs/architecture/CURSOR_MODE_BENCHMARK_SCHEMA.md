# Cursor-Mode Benchmark Schema

How to judge a GT eval run. This schema defines the standard for the 6-task rerun.

## Delivery Decision States

Every GT delivery attempt must produce one of these states:

| State | Meaning |
|-------|---------|
| DELIVERED_VISIBLE | High-confidence, actionable evidence reached agent |
| SUPPRESSED_LOW_CONFIDENCE | Evidence below confidence threshold |
| SUPPRESSED_NO_EVIDENCE | No relevant data found |
| SUPPRESSED_NOT_ACTIONABLE | Agent can't act (e.g., after FINISHED) |
| SUPPRESSED_NOISE_RISK | Evidence likely noisy (class-wide completeness, repeated L5) |
| FAILED_REASON | Generation error |

## Benchmark Behavior Labels

For each GT injection in the trajectory, classify as:

| Label | Meaning | Is it a problem? |
|-------|---------|------------------|
| helpful_visible | High-confidence evidence the agent used or could use | NO — this is the goal |
| correct_silence | GT had nothing useful, stayed quiet | NO — silence is success when evidence is weak |
| wrong_or_noisy_visible | GT showed bad/misleading/noisy evidence | YES — evidence quality bug |
| missing_high_confidence_evidence | GT should have shown something but didn't | YES — under-delivery bug |
| truth_violation | emitted=true but not visible, or vice versa | YES — Tier-0 truth bug |
| interference | GT distracted or misled the agent | YES — worst case |
| under_delivery_regression | Layer that worked in gen6 no longer fires | YES — regression from fix |

## Rerun Pass Criteria

All must hold for the rerun to pass:

| Criterion | Target |
|-----------|--------|
| PRIOR-004 class-wide completeness recurrence | 0/6 |
| Completeness not globally killed (fires when edited function known) | verified |
| L1 brief delivery | 6/6 (same as gen6) |
| L3b post-view delivery | appears after views (no regression) |
| L4a auto-query delivery | appears when structurally relevant (no regression) |
| New truth bugs | 0 |
| New interference events | 0 |
| Under-delivery regressions vs gen6 | 0 |
| Claim checker contradictions | 0 |
| Resolve rate | reported separately from architecture quality |

## Before/After Table Format

### Fixed Bug Recurrence

| Bug | Gen6 before (count/6) | Rerun after (count/6) | Status |
|-----|-----------------------|-----------------------|--------|
| PRIOR-004 class-wide completeness | 2/6 | ? | target: 0/6 |
| L5 noise/spam | 0/6 (clean) | ? | target: still clean |

### Regression Check

| Issue | Gen6 | Rerun | Status |
|-------|------|-------|--------|
| L1 brief delivery | 6/6 | ? | must not drop |
| L3b post-view delivery | 6/6 | ? | must not drop |
| L4a auto-query delivery | 6/6 | ? | must not drop |
| L6 [REVIEW] delivery | 3/6 | ? | must not drop |
| New truth bugs | 0 | ? | must stay 0 |
| New interference | 0 | ? | must stay 0 |

### Resolve Rate (separate from architecture quality)

| Metric | Gen6 | Rerun |
|--------|------|-------|
| Resolve rate | 2/4 evaluated | ? |

A task can resolve while GT evidence is wrong.
A task can fail while GT architecture is correct.
Report both. Do not hide behind resolve rate.

## Per-Task Audit Format

For each task, produce a table with real verbatim values from output.jsonl:

```
Layer                  Verbatim value from output.jsonl
---------------------- --------------------------------------------------------
L1 Brief files         [actual file names]
L1+ Edit target        [actual function, file, callers]
L1+ Key contracts      [actual content or (not fired)]
L3 [SIGNATURE]         [entry N: actual signature text]
L3 Called by           [entry N: actual caller paths]
L3 Calls into          [entry N: actual callee paths]
L3 [TEST]              [entry N: actual test content]
L3 [COMPLETENESS]      [entry N: actual content or (not fired)]
L3 [PATTERN]           [entry N: actual sibling name]
L4a [GT_AUTO]          [entry N: actual content]
L5 Scaffold            [entry N: variant and iteration]
L5b Reminder           [entry N: suggestion target]
L6 [REVIEW]            [entry N: PRESERVE targets]
Consensus scope        [entry N: files and count]
Grep intercept         [entry N: symbol]
Vendor JS in callers   [clean or VENDOR FOUND]
Hidden prefix leak     [clean or leaked prefix]
```

No YES/NO. Real values only.
