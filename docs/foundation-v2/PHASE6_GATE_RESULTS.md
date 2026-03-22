# Phase 6 — Integration: Gate Results

**Date:** 2026-03-21
**Branch:** `research/foundation-v2`

## Gate 0: All tests pass

```
1222 passed, 4 skipped — no regressions (1061 existing + 161 foundation)
```

## Gate 1: Phase complete

| Criterion | Status | Evidence |
|-----------|--------|---------|
| Full pipeline works end-to-end | PASS | `run_pipeline()` produces candidates from graph expansion on test fixture |
| Obligation expansion adds candidates | PASS | Graph expansion finds callers, same-class, callees as candidates |
| Flag-OFF parity | PASS | `enhance_obligations(None, None)` returns existing unchanged (same object) |
| Each flag independently testable | PASS | `GT_ENABLE_FOUNDATION` defaults OFF; `foundation_enabled()` tested |
| Evidence attached to every output | PASS | `result.evidence` matches `result.candidates` 1:1 |
| Confidence capped below attribute-traced | PASS | Foundation confidence ≤ 0.7 vs attribute-traced 0.8-0.95 |
| Similarity-sourced kind used | PASS | All foundation candidates tagged `kind="similarity_sourced"` |

## Key design decisions

1. **enhance_obligations() is the integration point**: takes existing obligations + optional foundation components, returns augmented list. When components are None, returns the exact same object (identity, not just equality).

2. **Foundation candidates are strictly additive**: they never modify, reorder, or remove existing attribute-traced obligations. The obligation list grows, never shrinks.

3. **Confidence hierarchy**: attribute-traced (0.8-0.95) > foundation similarity (≤0.7) > foundation graph (≤0.5). This ensures foundation findings never outrank proven deterministic obligations.

4. **Freshness suppression**: candidates from stale files are tagged but excluded from results. They don't appear as false positives.

5. **Non-fatal failures**: if similarity or graph expansion fails (exception), the pipeline continues with whatever stages succeeded. Partial results are better than no results.

## Files created

- `src/groundtruth/foundation/integration/__init__.py`
- `src/groundtruth/foundation/integration/pipeline.py` — `run_pipeline()`, `enhance_obligations()`
- `tests/foundation/test_pipeline.py` — 17 tests

## Files modified

- `src/groundtruth/core/flags.py` — added `foundation_enabled()` flag

## Phase 6 complete. Phase 7 (Evaluation) can begin.
