# Offline Preflight Cases — 2026-05-15

## Test Results

53 tests pass across 2 test files:
- tests/preflight/test_l5_event_governor_preflight.py
- tests/preflight/test_deep_layer_grounded_metrics_preflight.py

## Cases Covered

| Case | Scenario | Tests | Status |
|---|---|---|---|
| 3 | L3 next_action ignored -> L5 STRUCTURAL_WITNESS_IGNORED | 3 tests | PASS |
| 4 | Weak verification after edit | 2 tests | PASS |
| 5 | Finish with unverified edit | 3 tests | PASS |
| 6 | Patch collapsed | 2 tests | PASS |
| 7 | No durable progress | 3 tests | PASS |
| 9 | Strong verification clears warning | 1 test | PASS |
| 10 | Step 75 no restart | 4 tests | PASS |
| 11 | Low-confidence drift suppressed | 2 tests | PASS |
| 12 | Metrics proof completeness | 8 tests | PASS |
| - | Debounce + max emissions | 3 tests | PASS |
| - | Event classifier (file_kind, check_kind, event_bucket, verification_strength) | 15 tests | PASS |
| - | Schema validation (invalid enums rejected) | 4 tests | PASS |
| - | L5 event types contain no framework names | 1 test | PASS |

## Not Yet Covered (deferred to P1/P2)

| Case | Scenario | Why Deferred |
|---|---|---|
| 1 | L1 collaboration, not obedience | Requires L1 wrapper integration changes |
| 2 | L3 structural next_action followed | Covered by existing reaction_joiner tests |
| 8 | Repeated unproductive loop | P1 event, not yet agent-visible |
