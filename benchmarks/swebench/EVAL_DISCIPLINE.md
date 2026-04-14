# GT vNext — Evaluation Discipline

**Branch:** `research/vnext-substrate-plan-2026-04-11`
**Phase 7 of the vNext deepening plan (enchanted-bouncing-manatee)**
**Date:** 2026-04-14

---

## Purpose

This document defines how GT contract evidence is measured and reported. Its job is to prevent conflating "code was written" with "the substrate is decision-grade." Every claim must be grounded in one of the measurement categories below.

---

## Measurement Hierarchy

### Level 1 — Pairwise ranking gate (machine-checkable, before any canary)

**What it tests:** Does the contract layer assign a higher score to the correct patch than to a plausible wrong patch, for the same bug?

**Gate:** ≥70% correct-over-wrong across all fixture pairs. Per-family ≥60%.

**How to run:**
```bash
python benchmarks/swebench/pairwise_rank_eval.py \
  --db /d/tmp/swe_repos/<repo>.db \
  --fixtures benchmarks/swebench/pairwise_fixtures.jsonl \
  --root /d/tmp/swe_repos/<repo>
```

**Current result (2026-04-14, commit 1007ef1):** 14/14 = 100%

| Family | Correct/Total | Rate |
|--------|--------------|------|
| behavioral_assertion | 13/13 | 100% |
| paired_behavior | 1/1 | 100% |
| constructor_postcondition | not in gate (no init_attr in indexed DBs) | N/A |
| dispatch_registration | not in gate (no init_attr in indexed DBs) | N/A |

**Gating rule:** Do not proceed to any canary run until this gate passes. A gate failure invalidates any canary flips claimed during that period.

---

### Level 2 — Canary (5–10 tasks, targeted)

**What it tests:** Does GT evidence produce a behavior-changing difference in agent output on real SWE-bench tasks?

**Required before claiming canary success:**
- Agent + GT must use a *real* harness (SWE-agent, OpenHands — not minisweagent for benchmark claims)
- Run GT vs baseline in parallel on the same tasks with the same model
- Report: tasks where GT flipped (baseline wrong, GT correct) AND tasks where GT hurt (GT wrong, baseline correct)
- Report: evidence families that fired (not just "GT hooked")

**Abstention discipline:** A task where GT fired but the agent still failed is NOT a failure of GT — it is expected (GT is evidence, not a solve oracle). Report it as "fired but did not flip."

---

### Level 3 — Full benchmark run

**Required before claiming full-run results:**
- Pairwise gate passed (Level 1)
- Canary showed ≥1 reliable flip (Level 2)
- Baseline run on same set with same model is available for comparison
- Disk: ≥128GB before launch
- Workers ≤ CPU count

**Report format:**
```
GT resolved: X/N (X.X%)
Baseline resolved: Y/N (Y.Y%)
Delta: +Z tasks

Per-family breakdown:
  behavioral_assertion: fired on A tasks, behavior-changed on B
  paired_behavior: fired on C tasks, behavior-changed on D
  constructor_postcondition: fired on E tasks, behavior-changed on F
  dispatch_registration: fired on G tasks, behavior-changed on H

Overturn analysis:
  GT-only correct: [list tasks]
  Baseline-only correct: [list tasks]
```

---

## Per-Family Metric Targets

| Metric | Target | Red Flag |
|--------|--------|----------|
| Pairwise ranking rate (overall) | ≥70% | <60% |
| Pairwise ranking rate per family | ≥60% | <50% |
| Abstention precision | track % abstained | firing on every task = wrong |
| Hard-block false positive rate | <5% | >10% |
| Evidence used (behavior-changing) | track vs emitted | <1% used = evidence flood |
| Avg evidence lines per briefing | 5–10 lines | >20 lines |

---

## What Counts as Evidence

**Counts:**
- `behavioral_assertion`: nullability:not_none fires on `return None` addition; exception_message fires on string literal removal; exact_value fires when literal changes
- `paired_behavior`: sentinel_preservation fires on `return NotImplemented` → `return None`
- `constructor_postcondition`: fires when `self.<attr> = value` replaced by `self.<attr> = None`, or `super().__init__(kwarg=val)` removed
- `dispatch_registration`: fires when `@register` decorator removed, symbol de-exported from `__all__`, or routing dict loses key

**Does not count:**
- "GT hooked" — hook ran, returned evidence, agent ignored it
- "GT fired" — contract produced, checker abstained (no machine-checkable signal)
- "Task resolved" — cannot attribute to GT without baseline comparison

---

## Abstention Discipline

The checker must abstain when it cannot machine-check an obligation from the diff alone.

**Abstain cases (explicit):**
- Cannot identify which function's body changed (diff covers multiple functions)
- Contract normalized_form is malformed or missing expected component
- Exception or literal appears in multiple functions — cannot isolate scope
- Diff adds/removes lines outside the function body of the contracted function

**Never abstain for superficial benchmark wins.** Abstaining on uncertain cases preserves the signal-to-noise ratio. A false positive (wrong patch marked as violation) is worse than a missed catch.

---

## Benchmark Role

SWE-bench-Live is a **stress test** for the substrate families, not the product definition.

Report results as:
- "behavioral_assertion family fired on N tasks, helped on M" — not "task X flipped"
- "constructor_postcondition checker caught Y wrong patches" — not "run 2 flipped 14182"

Single task anecdotes are useful for debugging but not for claims.

---

## Family Readiness Checklist

### behavioral_assertion
- [x] Extractor: nullability:not_none, raises_exception, exception_message, exact_value, output_contains
- [x] Checker: nullability, raises_exception, exact_value, exception_message, output_contains
- [x] Pairwise fixtures: 13 pairs (all correct)
- [x] Gate: 100%
- [ ] constructor_postcondition pairwise fixtures (blocked: no init_attr in indexed DBs)
- [ ] dispatch_registration pairwise fixtures (blocked: no init_attr in indexed DBs)

### paired_behavior
- [x] Extractor: sentinel_preservation, protocol_return
- [x] Checker: sentinel_preservation (NotImplemented→None), protocol_return
- [x] Test linkage: co-body callee or roundtrip assertion required (name co-mention deprecated)
- [x] Pairwise fixtures: 1 pair (astropy-13977 NotImplemented)
- [ ] Add roundtrip-shaped fixture (encode/decode pattern)

### constructor_postcondition
- [x] Bug fixes: assertIsNotNone duplicate, caller threshold ≥2, attr regex
- [x] Checker: null-regression, forward_to_parent removal
- [ ] Pairwise fixtures: blocked until a DB with init_attr properties is available
- [ ] Verify: run `SELECT kind, COUNT(*) FROM properties GROUP BY kind` on test DB

### dispatch_registration
- [x] File-level vs function-level separation
- [x] String-key routing check
- [x] Same-dispatcher sibling counting
- [ ] Pairwise fixtures: blocked until DB with relevant properties is available

---

## How to Add New Pairwise Fixtures

1. Confirm the target function exists in an indexed DB:
   ```sql
   SELECT id, name, file_path FROM nodes WHERE name = 'target_function';
   ```

2. Confirm the relevant property kind exists:
   ```sql
   SELECT kind, COUNT(*) FROM properties GROUP BY kind;
   ```

3. Confirm the contract fires on the correct patch and not the wrong patch:
   ```bash
   python benchmarks/swebench/pairwise_rank_eval.py --db ... --fixtures <single_fixture.jsonl>
   ```

4. Only add fixtures for patterns confirmed to produce distinct correct>wrong scores.
   TIE (same score for both) = the contract does not yet discriminate this case.

---

## Do NOT

- Add new semantic families before the four current families are decision-grade
- Add repo-name or benchmark-specific logic in extractors/checkers
- Weaken abstention discipline for superficial gate improvements
- Claim generalization from one task or one run
- Count "GT hooked" as "GT helped"
