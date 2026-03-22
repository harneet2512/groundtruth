# Phase 3 — Multi-Signal Similarity: Gate Results

**Date:** 2026-03-21
**Branch:** `research/foundation-v2`

## Gate 0: All tests pass
```
1205 passed, 4 skipped — no regressions
```

## Gate 1: Phase complete

| Criterion | Status | Evidence |
|-----------|--------|---------|
| 3 extractors implemented + registered | PASS | fingerprint_v1, astvec_v1, tokensketch_v1 — all auto-register |
| Fingerprint rename detection | PASS | Same-body-different-name → identical fingerprint |
| Structural vector clustering | PASS | Similar methods cosine > 0.85, different < 0.5 |
| Token sketch disambiguation | PASS | Same structure + different identifiers → low Jaccard |
| Composite query: 4 use cases | PASS | rename_move, obligation_expansion, convention_cluster, test_matching |
| sqlite-vec fallback | PASS | Python brute-force by default |
| No existing files modified | PASS | All new code in foundation/similarity/ |

## Files: 5 source + 4 test files, 37 tests
