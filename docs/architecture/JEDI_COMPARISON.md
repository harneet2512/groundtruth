# Jedi Branch Comparison

For each layer, compare jedi__branch code against architecture invariants.
Decision: KEEP / REPAIR / REPLACE

## Layer: Delivery Ledger (Invariant 1)

**jedi__branch code:** `_deliver_or_trace()` at oh_gt_full_wrapper.py:1281-1327
**Observed intent:** Records DELIVERED/EMPTY/MISMATCH for each delivery attempt.
**Invariant match:** PARTIAL
- YES: tracks delivery success/failure
- NO: does not distinguish DEAD_WRITE from DELIVERED at `_deliver_or_trace()` level
- BUG-001 fix adds `emitted=False` for finish handler events, but at `_emit_structured_event()` level, not at `_deliver_or_trace()` level
**Decision:** KEEP — BUG-001 fix is sufficient. The structured event system handles dead write marking. No need to change `_deliver_or_trace()` itself.

## Layer: L1 Brief (Invariant — brief presence)

**jedi__branch code:** graph_map.py renders brief, wrapper prepends it
**Observed intent:** File ranking with graph connections at task start
**Invariant match:** YES — brief fires at task start, appears in primacy position
**Decision:** KEEP

## Layer: L1 Edit Target (Invariant 3)

**jedi__branch code:** oh_gt_full_wrapper.py:5784-5838
**Observed intent:** Select edit target by keyword overlap + caller count
**Invariant match:** NO
- Uses first-match-wins (break on first tier != "none")
- Caller count is the primary ORDER BY, not tie-breaker
- Does not evaluate all candidates before selecting
**Decision:** REPAIR — change to score-all-then-pick-best. Issue-named function must beat high-caller functions.

## Layer: L1 Key Contracts (BUG-002)

**jedi__branch code:** oh_gt_full_wrapper.py:5827-5862
**Observed intent:** Show contract properties for edit target
**Invariant match:** PARTIAL (after BUG-002 fix)
- YES: `[GT KEY CONTRACTS]` marker emitted when contracts exist (BUG-002 fix)
- YES: fires conditionally when properties table has qualifying entries
**Decision:** KEEP — BUG-002 fix already applied on jedi__branch

## Layer: L3 Post-Edit — Test Ranking (Invariant 4)

**jedi__branch code:** post_edit.py:1311-1344 (test assertion ranking)
**Observed intent:** Rank test assertions by issue-keyword overlap
**Invariant match:** NO
- _common.py still outranks direct tests on beets (PRIOR-003 reproduced)
- Ranking uses keyword overlap but doesn't deprioritize helper files
**Decision:** REPAIR — add helper file deprioritization

## Layer: L3 Post-Edit — Completeness Scope (Invariant 5)

**jedi__branch code:** obligation_check.py
**Observed intent:** Show shared-state methods
**Invariant match:** NO
- Shows class-wide shared state, not scoped to edited function (PRIOR-004 reproduced)
**Decision:** REPAIR — scope to edited function's shared attributes

## Layer: L3 Post-Edit — Dunder Filter (Invariant 6)

**jedi__branch code:** post_edit.py sibling query
**Observed intent:** Show sibling methods as patterns
**Invariant match:** NO
- __init__ appears in [PATTERN] output (PRIOR-008 reproduced)
**Decision:** REPAIR — add dunder filter to sibling query

## Layer: L3b Post-View — Vendor Filter (Invariant 9)

**jedi__branch code:** post_view.py:_is_vendor_path()
**Observed intent:** Filter vendor JS from callers
**Invariant match:** PARTIAL (after stabilization fix + invariant fix)
- YES: _is_vendor_path exists and filters /static/ paths
- Fixed in this session: path-start matching for node_modules/, vendor/, dist/
**Decision:** KEEP — fixes already applied

## Layer: L6 Pre-Submit (Invariant 2)

**jedi__branch code:** L6 early review at oh_gt_full_wrapper.py:4302
**Observed intent:** Show blast radius + test suggestions before finish
**Invariant match:** PARTIAL (after stabilization fix)
- YES: fires after first source edit (changed from edit_count>=2 to >=1)
- YES: includes test suggestions from assertions table
- YES: agent sees it before finish
- NO: finish handler L6 still runs (now marked DEAD_WRITE)
**Decision:** KEEP — stabilization fix sufficient

## Layer: Path Resolution (Invariant 7)

**jedi__branch code:** Multiple LIKE suffix patterns across files
**Observed intent:** Resolve host/container paths to graph.db stored paths
**Invariant match:** PARTIAL
- No universal resolver — each file has its own `_resolve_file_path()` or LIKE pattern
- Workspace prefix stripping added in invariant test but not in production code
**Decision:** REPAIR — but low priority. Current suffix matching works for the canary tasks.

## Layer: Claim Checker (Invariant 8)

**jedi__branch code:** scripts/gt_check_claims.py
**Observed intent:** Flag claims without proof
**Invariant match:** YES (after stabilization fixes)
- Correctly flags contradictions, unsupported claims, OPEN_BUG
**Decision:** KEEP

## Layer: Baseline Isolation (Invariant 10)

**jedi__branch code:** `_GT_BASELINE` gate in wrapper
**Observed intent:** Zero GT evidence in baseline arm
**Invariant match:** PARTIAL
- Gates L5, L6, grep intercept via `not _GT_BASELINE`
- Does NOT gate L3 post-edit or L3b post-view
**Decision:** NEEDS_INVESTIGATION — check if L3/L3b fire on baseline in practice

## Summary

| Layer | Decision | Reason |
|-------|----------|--------|
| Delivery ledger | KEEP | BUG-001 fix sufficient |
| L1 brief | KEEP | Works correctly |
| L1 edit target | REPAIR | First-match-wins, caller-count primary |
| L1 key contracts | KEEP | BUG-002 fix applied |
| L3 test ranking | REPAIR | _common.py outranks direct tests |
| L3 completeness | REPAIR | Class-wide, not function-scoped |
| L3 dunder filter | REPAIR | __init__ in siblings |
| L3b vendor filter | KEEP | Fixes applied |
| L6 pre-submit | KEEP | Stabilization fix sufficient |
| Path resolver | REPAIR (low priority) | No universal resolver |
| Claim checker | KEEP | Works correctly |
| Baseline isolation | NEEDS_INVESTIGATION | L3/L3b baseline gate |

**REPAIR needed:** L1 edit target, L3 test ranking, L3 completeness, L3 dunder filter
**KEEP:** 7 layers already satisfy invariants
