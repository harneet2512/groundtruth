# GT v8 Evaluation Analysis — 2026-03-28

## Setup
- **Scaffold**: mini-swe-agent 2.2.7 (direct Docker exec, no OpenHands overhead)
- **Model**: Qwen3-Coder-480B via Vertex AI (litellm proxy)
- **GT delivery**: Precomputed context injection (zero iteration cost)
- **Eval**: SWE-bench harness (actual test suite execution per patch)
- **VM**: swebench-ab (e2-standard-16)

## 10-Task Smoke Test Results

| | Baseline | GT v8 |
|---|---|---|
| Tasks run | 10 | 10 (1 malformed patch) |
| **Resolved** | **5/10 (50%)** | **4/9 (44%)** |
| Shared resolves | 4 | 4 |
| Baseline only | 1 (astropy-14995) | 0 |
| GT only | 0 | 0 |

### Per-task breakdown

| Task | Baseline | GT v8 | GT Context | GT Helped? |
|---|---|---|---|---|
| astropy-12907 | PASS | PASS | 907 chars (separable.py fingerprints) | Neutral — same patch |
| astropy-14182 | FAIL | FAIL | 2127 chars (ascii io: connect, core, ui) | No — different but both wrong |
| astropy-14365 | FAIL | FAIL | 1051 chars (QDP parser fingerprints) | No — different but both wrong |
| astropy-14995 | PASS | ERROR | 315 chars (NDDataRef — nearly empty) | **Hurt** — malformed patch |
| astropy-6938 | FAIL | FAIL | 1105 chars (fitsrec.py fingerprints+callers) | No — different but both wrong |
| astropy-7746 | FAIL | FAIL | 1212 chars (WCS callers+tests) | No — different but both wrong |
| django-10914 | PASS | PASS | 1722 chars (Storage norms, uploadedfile) | Neutral — same resolve |
| django-10924 | PASS | PASS | 1621 chars (FilePathField, migrations) | Neutral — same resolve |
| django-11001 | PASS | PASS | 1328 chars (SQLCompiler, RawSQL norms) | Neutral — same resolve |
| django-11019 | FAIL | FAIL | 935 chars (widgets.py norms) | No — different but both wrong |

### Net impact: -1 task (baseline 5, GT 4)

## What GT Context Looked Like

GT injected behavioral fingerprints: what functions read, write, return, call. Example for django-11001:

```
## django/db/backends/mysql/compiler.py
SQLCompiler:
  as_subquery_condition: reads self.as_sql, self.connection → tuple(2)
  Rule: no methods write to self.* (stateless/immutable) (3/3)
  Rule: reads self.query (3/3)

## django/db/models/expressions.py
  Rule: returns scalar (18/18 methods)
  Rule: calls self._combine() (17/18 methods)
```

## Why GT Didn't Help

### What GT provides (fingerprints + norms):
- What functions READ, WRITE, RETURN, CALL
- Statistical patterns across sibling methods ("returns scalar 11/12 methods")
- Cross-file callers (who calls what, from where)
- Test file discovery

### What the agent actually needs:
1. **WHERE to edit** — fault localization. GT doesn't tell the agent which line to change.
2. **HOW similar bugs were fixed** — precedent. GT doesn't show past changes.
3. **WHAT the test expects** — test assertions, not just test file locations. GT finds test files but doesn't parse what they assert.
4. **The conceptual understanding** — "this function hashes mutable user fields so tokens invalidate when those fields change." GT shows mechanical behavior, not intent.

### The fingerprint format is the wrong abstraction

The agent reads source code directly. When it sees:
```python
def _make_hash_value(self, user, timestamp):
    return str(user.pk) + user.password + str(timestamp)
```

It already knows what the function reads and returns. GT's fingerprint:
```
_make_hash_value: reads user.pk, user.password, param:timestamp → scalar
```

This is **redundant with reading the file**. The agent already has this information. GT needs to provide information the agent CANNOT derive from reading one file.

## What Would Actually Help (from research)

### 1. Fault localization ("where to edit")
CodeRAG-Bench shows oracle context is a 3x multiplier. The highest-value context is "edit this specific function in this specific file." GT could compute this by:
- Matching issue keywords to function names/docstrings
- Ranking files by relevance to the issue
- Showing the top 1-3 functions with their line numbers

### 2. Similar past changes ("how it was fixed before")
If the issue says "add email to hash," GT could search git history for commits that modified the same function and show the diff pattern. This is proven in CodeR and AutoCodeRover.

### 3. Test assertions ("what correctness looks like")
GT finds test files but doesn't show WHAT they test. If GT extracted:
```
test_tokens.py:
  test_token_invalidation_on_password_change: creates user, gets token, changes password, asserts old token invalid
  test_token_invalidation_on_email_change: THIS TEST DOESN'T EXIST (the issue is asking to add this)
```
That's decision-changing — it tells the agent the test gap.

### 4. Impact/blast radius ("what breaks if you change this")
GT has callers but doesn't frame them as risk:
```
WARNING: _make_hash_value is called by check_token() and _make_token_with_timestamp().
Both callers compare hash values. If you change the hash inputs, existing tokens will invalidate.
This is the INTENDED behavior for this issue.
```

## File Detection: Fixed

| Version | Injection rate | How |
|---|---|---|
| v8 initial | 3/10 (30%) | File paths only |
| v8 + backtick | 5/10 (50%) | + backtick-quoted filenames |
| v8 + grep fix | **10/10 (100%)** | + class name grep, fixed `/testbed/` filter |

Key bug fixed: `grep -v test` was filtering ALL results because every path starts with `/testbed/` which contains "test". Changed to `grep -v '/tests/'`.

## Infrastructure: mini-swe-agent vs OpenHands

| Metric | OpenHands | mini-swe-agent |
|---|---|---|
| 10 tasks (baseline) | ~50 min | **7 min** |
| 10 tasks (GT) | ~55 min | **8 min** |
| 50 tasks (baseline) | ~5 hours | **34 min** |
| Container overhead | Agent-server Docker per task | Direct bash in Docker |
| Crash rate | ~30% | ~0% |
| Worker contention | Disk I/O saturation at 6+ | No issues at 4 |

## 50-Task Baseline (completed)

The 50-task baseline finished in 34 min. The GT condition was paused to fix file detection. The baseline results are saved at:
```
/home/Lenovo/results/v8_mini_20260328_061103/baseline/preds.json
```

## Next Steps

1. **Run 50-task GT with fixed detection** — 100% injection rate now works
2. **Evaluate both conditions** — SWE-bench harness on baseline + GT patches
3. **If still no lift**: GT needs to shift from fingerprints to fault localization + test assertions + precedent
4. **Research confirms**: the gap between oracle and retrieved context IS the opportunity. GT's compiler-grade index is closer to oracle than embedding-based retrieval for deterministic signals.

## Key Lesson

**Redundancy kills value.** GT's behavioral fingerprints tell the agent what it can already see by reading the file. The agent doesn't need to be told that `_make_hash_value` reads `user.pk` — it can see that in the source code. GT needs to provide the 20 lines of context that the agent CANNOT derive from reading any single file:

1. Who calls this function and how they use the result (cross-file)
2. What tests verify and what test gaps exist (test intelligence)
3. What similar changes looked like in git history (precedent)
4. What the blast radius is if this function changes (impact framing)

These are all deterministically extractable. They're just not what GT currently computes.
