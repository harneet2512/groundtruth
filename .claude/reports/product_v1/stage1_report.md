# Stage 1 Runtime Proof Report — Product-v1

**Run ID:** 26276599683
**Branch:** jedi__branch
**HEAD:** 92fef445 (pushed), product-v1 code at e0a50f72
**Date:** 2026-05-22
**Model:** DeepSeek V4 Flash
**Tasks:** 5

---

## Resolution Results

| Task | Resolved | Previous (RESPEC.md §9) | Regression? |
|---|---|---|---|
| amoffat__sh-744 | RESOLVED | RESOLVED | NO |
| beeware__briefcase-2085 | NOT_RESOLVED | NOT_RESOLVED | NO |
| conan-io__conan-17102 | NOT_RESOLVED | NOT_RESOLVED | NO |
| pallets__flask-5637 | NOT_RESOLVED | N/A (new task) | N/A |
| pylint-dev__pylint-10044 | NOT_RESOLVED | N/A (new task) | N/A |

**1/5 resolved. Zero regressions.**

---

## Invariant Matrix

| Invariant | sh-744 | briefcase-2085 | conan-17102 | flask-5637 | pylint-10044 |
|---|---|---|---|---|---|
| Pollution | **FAIL** (GT_STATUS in 2 obs) | PASS | PASS | PASS | PASS (minor: GT_STATUS visible) |
| L1 brief | DELIVERED (361c) | DELIVERED (1877c) | DELIVERED (2155c) | DELIVERED (1924c) | DELIVERED (1870c) |
| L3 post-edit | DELIVERED x2 | DELIVERED x2 | NOT_FIRED (no edits) | DELIVERED x1 | SUPPRESSED (0 evidence) |
| L3b post-view | DELIVERED x3 | DELIVERED x3 | DELIVERED x3 | DELIVERED x3 | DELIVERED x3 |
| Patch A: conf filter | NOT_CHECKED | NOT_CHECKED | NOT_CHECKED | NOT_CHECKED | NOT_CHECKED |
| Patch B: neighbor cap | N/A (small) | N/A (small) | NOT_CHECKED (nodes=7054) | N/A (nodes=902) | NOT_CHECKED (nodes=9066) |
| Patch C: G7 silence | NOT_EXERCISED | NOT_EXERCISED | NOT_EXERCISED | NOT_EXERCISED | NOT_EXERCISED |
| Patch D: dedup | NOT_EXERCISED | NOT_EXERCISED | NOT_EXERCISED | NOT_EXERCISED | NOT_EXERCISED |
| Patch E: anchors | ACTIVE (40 sym) | ACTIVE (153 sym, 5 paths) | ACTIVE (35 sym) | ACTIVE (12 sym) | ACTIVE (86 sym) |
| Patch F: test bonus | FIRED | FIRED | NOT_EXERCISED | NOT_EXERCISED | NOT_EXERCISED |

---

## Gate Evaluation

| Gate | Condition | Result | Detail |
|---|---|---|---|
| G1 | Zero regressions on controls | **PASS** | sh-744 still RESOLVED; briefcase-2085 was NOT_RESOLVED before (RESPEC.md §9) |
| G2 | All invariants PASS or NOT_EXERCISED | **FAIL** | GT_STATUS pollution on sh-744 |
| G3 | Pollution = pass | **FAIL** | GT_STATUS leaked via L3b on sh-744 and pylint-10044 |
| G4 | Patch integrity | PASS | All 5 tasks produced well-formed patches |
| G5 | >=1 signal per task | PASS | Anchors ACTIVE on all 5; L3b DELIVERED on all 5 |
| G6 | No verifier errors | PASS | |
| G7 | Task count = 5 | PASS | |

---

## Critical Finding: GT_STATUS Pollution is PRE-EXISTING

The `[GT_STATUS] no_evidence:no_graph_edges` string leaked into agent-visible observations on sh-744 (via L3b prepend) and pylint-10044 (via L3b on files with no graph edges).

**This is NOT caused by Product-v1 patches.** It is a pre-existing issue:
- RESPEC.md §9 A3 claimed this was a FALSE_ALARM ("Double filtering prevents this")
- But the filtering only works on the L3 post-edit path (wrapper line 3281)
- The L3b prepend path does NOT strip GT_STATUS from hook output before delivery
- This bug existed before commit e0a50f72

**Product-v1 patches did not touch GT_STATUS filtering logic.**

---

## Logging Gap: Patches A/B/C/D Leave No Runtime Traces

Patches A (confidence filter), B (neighbor cap), C (G7 silence), and D (normalized dedup) modify SQL queries and control flow inside post_view.py and post_edit.py. None of them emit dedicated log lines when they fire. The only way to confirm they executed is to read the Python source and verify the code path — runtime logs show no evidence of their activation.

**This means:**
- Confidence filter (Patch A): code is deployed, queries have `>= 0.7`, but we cannot prove from logs that any edge was excluded
- Neighbor cap (Patch B): code caps limit to 3 when nodes > 5000, but no log line says "cap applied"
- G7 silence (Patch C): never triggered (no isolated function edited across 5 tasks)
- Normalized dedup (Patch D): dedup keyword absent from all logs; router-level dedup (separate mechanism) did fire

**Recommendation:** Add one-line logging for each patch in the next code change cycle (not now — no code changes during runtime analysis per runbook).

---

## What Was Proven

| Patch | Runtime Status | Evidence |
|---|---|---|
| A: Confidence filter | DEPLOYED, NOT_CHECKED | Code present in queries, no log trace |
| B: Neighbor cap | DEPLOYED, NOT_CHECKED | Code present, conan node_count=7054 > 5000, but no cap log |
| C: G7 silence | DEPLOYED, NOT_EXERCISED | Zero isolated-function edits across 5 tasks |
| D: Normalized dedup | DEPLOYED, NOT_EXERCISED | No dedup messages in logs |
| E: Issue anchors | **RUNTIME_PROVEN** | All 5 tasks: anchors loaded (12-153 symbols) |
| F: Visible-test bonus | **RUNTIME_PROVEN** (2/5) | sh-744 and briefcase-2085 show [TEST] markers in L3 |

---

## Verdict

**NOT_READY for Stage 2 due to G2/G3 FAIL (pollution).**

However: the pollution is pre-existing (not Product-v1). The runbook says STOP on G3 fail. Two options:

1. **Strict interpretation:** STOP. Fix GT_STATUS leak in L3b path. Re-run Stage 1.
2. **Pragmatic interpretation:** Document as known pre-existing issue. Proceed to Stage 2 with the understanding that GT_STATUS pollution predates Product-v1 and must be fixed separately.

**Awaiting user decision.**

---

## Artifacts

```
.claude/reports/product_v1/stage1_artifacts/
  task-amoffat__sh-744/
  task-beeware__briefcase-2085/
  task-conan-io__conan-17102/
  task-pallets__flask-5637/
  task-pylint-dev__pylint-10044/
```
