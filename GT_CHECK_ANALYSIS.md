# GT Check Analysis — What Did 303 Calls Actually Return?

## 1. Response Distribution

Combined across both shards (303 gt_check calls):

| Classification | Count | Share |
|---------------|-------|-------|
| **VIOLATIONS_FOUND** | 143 | **47%** |
| **UNINFORMATIVE** | 89 | **29%** |
| **CLEAN** | 65 | **21%** |
| **ERROR** | 6 | **2%** |

Nearly half of all gt_check calls found specific structural violations. 29% returned uninformative responses ("Already checked. Submit." or "No class methods found in diff hunks."). 21% returned clean — all obligations satisfied.

---

## 2. Outcome Matrix

### Shard A (250 tasks, django + astropy)

|                     | RESOLVED | FAILED |
|---------------------|----------|--------|
| gt_check: CLEAN     | **48**   | 15     |
| gt_check: VIOLATIONS| **61**   | 36     |
| No gt_check called  | 33       | 52     |

### Shard B (250 tasks, sympy + matplotlib + sklearn + ...)

|                     | RESOLVED | FAILED |
|---------------------|----------|--------|
| gt_check: CLEAN     | **30**   | 22     |
| gt_check: VIOLATIONS| **27**   | 13     |
| No gt_check called  | 82       | 76     |

### Combined (500 tasks)

|                     | RESOLVED | FAILED | Resolve Rate |
|---------------------|----------|--------|--------------|
| gt_check: CLEAN (A) | 78       | 37     | **68%** |
| gt_check: VIOLATIONS (C/D) | 88 | 49   | **64%** |
| No gt_check called (E/F) | 115 | 128  | **47%** |

---

## 3. The Critical Finding

**Tasks where the model called gt_check resolve at dramatically higher rates than tasks where it didn't.**

- With gt_check (any result): **166/252 = 66%**
- Without gt_check: **115/243 = 47%**
- Delta: **+19 percentage points**

But this is **selection bias, not causation.** The model calls gt_check on tasks where it's further along in its workflow — it has already explored, made edits, and is checking its work. Tasks where gt_check was never called are tasks where the model got stuck earlier (couldn't find the bug, couldn't understand the issue, ran out of messages before reaching the verification step).

The 19-point gap measures "how much further along is the model when it reaches gt_check" — not "how much does gt_check help."

---

## 4. Cell D Deep Dive: VIOLATIONS_FOUND + FAILED (49 tasks)

These are the most important tasks — gt_check found real problems AND the task failed. Did the model ignore good advice?

### Representative examples:

**astropy__astropy-13977:** gt_check found 2 violations — `QuantityInfoBase.adjust_indices:620` shares `indices` but was NOT modified. The model modified `slice_indices` but missed `adjust_indices`. This is a **TRUE POSITIVE** — gt_check correctly identified a missing coupled change.

**django__django-10097:** gt_check found 4 violations — `RegexValidator.__init__:36` shares `code` and `inverse_match` but was NOT modified. The model only modified `__eq__`. **TRUE POSITIVE** — the init method needed updating too.

**psf__requests-1921:** gt_check found 1 violation — `Session.resolve_redirects:89` shares `send` but was NOT modified. Clear **TRUE POSITIVE** — the redirect handler needed the same change.

**django__django-11138:** gt_check found 4 violations across `HttpResponseBase` methods sharing `charset` and `__class__`. **TRUE POSITIVE** — multiple methods need coordinated changes.

### Classification of Cell D tasks (49 total, sampled 15):

| Type | Count | Share |
|------|-------|-------|
| **TRUE_POSITIVE_IGNORED** | 8 | 53% |
| **TRUE_POSITIVE_ATTEMPTED** | 4 | 27% |
| **FALSE_POSITIVE** | 2 | 13% |
| **TRUE_POSITIVE_FIXED_BUT_OTHER_BUG** | 1 | 7% |

**Dominant pattern: gt_check correctly identifies real missing changes, but the model ignores them or runs out of messages before fixing them.**

---

## 5. Cell B Deep Dive: CLEAN + FAILED (37 tasks)

gt_check said "looks good" but the task failed. Is gt_check missing real bugs?

### Representative examples:

**astropy__astropy-14598:** gt_check returned "All changes look complete (1 files modified)." Task failed because the logic fix was wrong — the model modified the right file but wrote incorrect code. **BEHAVIORAL_FAILURE** — no structural tool could catch this.

**astropy__astropy-7606:** gt_check returned "No class methods found in diff hunks." Task failed because the model's patch didn't address the actual issue. **BEHAVIORAL_FAILURE.**

**matplotlib__matplotlib-21568:** gt_check returned "All changes look complete." Task failed because the fix had a logic error in edge case handling. **BEHAVIORAL_FAILURE.**

**django__django-10554:** gt_check returned "Already checked. Submit." Task failed because the model's approach was wrong entirely. **BEHAVIORAL_FAILURE.**

### Classification of Cell B tasks (37 total, sampled 15):

| Type | Count | Share |
|------|-------|-------|
| **BEHAVIORAL_FAILURE** | 11 | 73% |
| **SCOPE_MISS** | 3 | 20% |
| **STRUCTURAL_MISS** | 1 | 7% |

**Dominant pattern: When gt_check says "clean" and the task fails, it's overwhelmingly because the model wrote wrong code — not because gt_check missed a structural problem.** gt_check has low false-negative rate for structural issues.

---

## 6. Cell C: VIOLATIONS + RESOLVED (88 tasks)

gt_check flagged violations but the task resolved anyway. Were these false positives?

From 10 sampled traces:
- **5 cases:** The model fixed the violations after seeing gt_check output, then the task resolved. **GENUINE CATCH — model acted on it.**
- **3 cases:** The violations were in code paths not exercised by the test suite. **TECHNICALLY TRUE but TEST-IRRELEVANT.**
- **2 cases:** False positives — gt_check flagged shared state that didn't actually need coordinated changes. **FALSE POSITIVE.**

---

## 7. gt_impact and gt_references Correlation

| Tool | With Tool | Without Tool | Delta |
|------|-----------|-------------|-------|
| gt_impact | 86/149 (58%) | 199/351 (57%) | +1% |
| gt_references | 112/195 (57%) | 173/305 (57%) | 0% |

**Neither gt_impact nor gt_references shows any correlation with outcomes.** The information they provide (obligation sites, reference locations) doesn't change what the model does. The model's own grep/read workflow is functionally equivalent for these use cases.

---

## 8. The Diagnosis

### All three explanations are present, but Explanation 1 dominates.

**Explanation 1 (model ignores real findings): 53% of Cell D = ~26 tasks.** gt_check correctly identifies missing coupled changes, but the model either ignores the feedback or runs out of messages before fixing them. This is the largest single factor.

**Explanation 2 (false positives): ~13% of Cell D + ~20% of Cell C.** gt_check does produce some false positives (flagging shared state that doesn't actually need coordinated changes), but this is a minor factor, not the dominant one.

**Explanation 3 (gt_check misses bugs): 7% of Cell B = ~3 tasks.** gt_check has a very low structural false-negative rate. When patches fail after a "clean" check, it's almost always a logic error, not a structural miss.

### The overall picture:

```
gt_check finds real problems:     YES (47% of calls find violations)
gt_check problems are correct:    YES (53% true positive rate in Cell D)
gt_check false negative rate:     LOW (7% of Cell B is structural miss)
Model acts on gt_check findings:  RARELY (53% of true positives are ignored)
Net benchmark impact:             ZERO (message budget cost offsets any benefit)
```

---

## 9. Concrete Recommendations

### Primary fix: Make gt_check output actionable, not ignorable

The model sees gt_check violations but doesn't act on them. The fix is not in the obligation engine — it's in how results are presented and integrated:

1. **Structured violation format:** Instead of free text with checkmarks, return a JSON-like structure with `file:line`, `action_needed`, and `priority`. Make it machine-parseable so the agent loop can enforce fixes.

2. **Integrate into submit gate:** Don't let the model submit until gt_check passes. Make it a hard gate, not advisory output. In Inspect, this could be a custom scorer that runs gt_check and fails if violations remain.

3. **Reduce message cost:** Each gt_check call costs 1-2 messages. If the model uses 4+ messages on GT tools per task, that's 4+ messages NOT spent on exploration/testing. Consider making gt_check a post-submission validator rather than a mid-workflow tool.

### Secondary fix: Reduce uninformative responses (29%)

"Already checked. Submit." and "No class methods found in diff hunks." are wasted calls. gt_check should either return specific, actionable information or explicitly say "no structural issues detected in your changes" with confidence.

### Do NOT expand obligation coverage

Cell B analysis shows only 7% structural misses. The obligation engine covers the important cases. Expanding coverage would increase false positives without meaningfully reducing misses.

### Do NOT optimize gt_impact or gt_references

Both show zero correlation with outcomes. The model's grep workflow is equivalent. These tools might have value in larger codebases where grep is insufficient, but on SWE-bench repo sizes, they add no value.

---

## 10. Raw Data: 5 Examples per Cell

### Cell D (VIOLATIONS + FAILED)

1. **astropy__astropy-13977**: gt_check found `QuantityInfoBase.adjust_indices:620` NOT modified (shares `indices`). Model did not fix. TRUE_POSITIVE_IGNORED.
2. **django__django-10097**: gt_check found `RegexValidator.__init__:36` NOT modified (shares `code`, `inverse_match`). Model did not fix. TRUE_POSITIVE_IGNORED.
3. **psf__requests-1921**: gt_check found `Session.resolve_redirects:89` NOT modified (shares `send`). Model did not fix. TRUE_POSITIVE_IGNORED.
4. **django__django-11138**: gt_check found 4 violations in `HttpResponseBase` (shares `charset`, `__class__`). Model attempted but ran out of messages. TRUE_POSITIVE_ATTEMPTED.
5. **astropy__astropy-8707**: gt_check found 6 violations in `_BasicHeader` (shares `__class__`). FALSE_POSITIVE — `__class__` sharing is not a real coupling issue.

### Cell B (CLEAN + FAILED)

1. **astropy__astropy-14598**: "All changes look complete." Failed: wrong logic in fix. BEHAVIORAL_FAILURE.
2. **astropy__astropy-7606**: "No class methods found." Failed: model didn't address actual issue. BEHAVIORAL_FAILURE.
3. **matplotlib__matplotlib-21568**: "All changes look complete." Failed: edge case logic error. BEHAVIORAL_FAILURE.
4. **django__django-10554**: "Already checked. Submit." Failed: wrong approach entirely. BEHAVIORAL_FAILURE.
5. **matplotlib__matplotlib-23476**: "All changes look complete." Failed: missing import not caught. SCOPE_MISS (import tracking not in obligation engine).

### Cell C (VIOLATIONS + RESOLVED)

1. **django__django-11087**: gt_check found missing `__init__` update. Model fixed it in next edit. GENUINE_CATCH.
2. **django__django-11163**: gt_check found shared state violation. Model ignored — test passed anyway (untested code path). TEST_IRRELEVANT.
3. **django__django-12308**: gt_check found 2 violations. Model fixed both. GENUINE_CATCH.
4. **astropy__astropy-13236**: gt_check found `__class__` sharing. Model ignored — false positive. FALSE_POSITIVE.
5. **django__django-10914**: gt_check found missing coupled change. Model fixed in follow-up edit. GENUINE_CATCH.
