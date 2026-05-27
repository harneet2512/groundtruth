# Stabilization Status

## Session Info
- Date: 2026-05-27
- Branch: jedi__branch
- Git SHA: fd05bebf
- Artifacts from: GHA run 26525222275 (completed, 2 tasks) + 26532251352 (completed, 3 tasks)

## Phase Checklist
- [x] Phase 1: Stabilization workspace created
- [x] Phase 2: Autopsy tooling built (26/26 tests pass)
- [x] Phase 3: Fresh 3-task canary run completed (run 26532251352)
- [x] Phase 4: Per-task autopsy completed (5 tasks total: 2 old + 3 fresh)
- [x] Phase 4b: Topology verification completed
- [x] Phase 5: Claim checker fixed + failure leaderboard generated
- [x] Phase 6: BUG-001 dossier + ENGINEERING_INVARIANT + failing regression test
- [x] Phase 7: BUG-001 fix applied + PROVEN with synthetic post-fix artifact

## First Patch Gate
- [x] 3 task reports exist (fresh run: beancount, beets, loguru)
- [x] Topology verifier output exists for all 3 tasks
- [x] Claim checker output exists (1 contradicted, 8 unsupported)
- [x] DOC_OF_HONOR claims invalidated by fresh artifacts listed (below)
- [x] Failure leaderboard exists (below)
- [x] First bug selected with justification (BUG-001: G1 truth bug)
- [x] Full bug dossier in BUG_DOSSIERS.md
- [x] Research fit check completed (ENGINEERING_INVARIANT)
- [x] Passing regression test: 6/6 (pre-fix + post-fix synthetic)

## Eval Results

| Run | Task | Resolved |
|-----|------|----------|
| 26525222275 | beetbox__beets-5495 | YES |
| 26525222275 | delgan__loguru-1306 | NO |
| 26532251352 | beancount__beancount-931 | YES |
| 26532251352 | beetbox__beets-5495 | YES |
| 26532251352 | delgan__loguru-1297 | NO |

## Contradicted DOC_OF_HONOR Claims (from claim checker)

1. **L1_KEY_CONTRACTS**: DOC says WORKING, 0/3 fresh tasks showed `[GT KEY CONTRACTS]` in output.jsonl

## Unsupported Claims

1. **L6_PRESUBMIT_OPEN**: OPEN_BUG — 0/3 tasks showed visible evidence, fix needed
2. **L0_BINARY through L0_PREINDEX** (6 claims): code_audit only, no trajectory proof
3. **DEDUP_L3**: code_audit only

## Failure Leaderboard

| Rank | Failure | Count | Layers | Fix type | Tier |
|------|---------|-------|--------|----------|------|
| 1 | Claim checker was silently passing L1_KEY_CONTRACTS 0/N | tooling | claim checker | FIXED | 0 |
| 2 | BUG-001: finish handler emitted=True for dead writes | 5/5 | L5b, L6 | PROVEN | 0 |
| 3 | L1_KEY_CONTRACTS never fired — DOC says WORKING | 3/3 fresh | L1+ | investigate | open |
| 4 | L6_PRESUBMIT 0/5 — evidence never reaches agent | 5/5 | L6 | OPEN_BUG | open |
| 5 | jquery.js in caller lists (PRIOR-005) | 1/2 old | L3b, L5b | engineering | open |
| 6 | L1_EDIT_TARGET absent (loguru old) | 1/5 | L1+ | investigate | open |

## L6 Status: OPEN_BUG (not accepted/demoted)

L6 pre-submit review generates useful content (caller contracts, test suggestions) but delivers it AFTER AgentState.FINISHED. The agent never sees it. This is NOT an accepted limitation — it is an open bug requiring one of:
- Late-iteration post-edit L6 (fire at 75%+ iteration as part of L3 post-edit)
- Submit-attempt intercept (before controller sets FINISHED)
- Pre-finish hook in OH controller (before state transition)
- Agent-initiated review (agent calls gt_contract before submitting)

BUG-001 fix addresses the telemetry truth (events no longer lie about delivery) but does NOT fix the underlying delivery problem.

## Known Bug Reproduction Matrix

| Prior bug | Seen in fresh run? | Status |
|-----------|-------------------|--------|
| PRIOR-001 Edit-target Pipeline() | not checked | NEEDS_INVESTIGATION |
| PRIOR-002 [PEER] twin absent | not checked | NEEDS_INVESTIGATION |
| PRIOR-003 [TEST] still _common.py | not checked | NEEDS_INVESTIGATION |
| PRIOR-004 [COMPLETENESS] noisy | not checked | NEEDS_INVESTIGATION |
| PRIOR-005 jquery.js present | PRIOR_REPRO_OLD (old run only) | PRIOR_REPRO_OLD |
| PRIOR-008 [PATTERN] __init__ | not checked | NEEDS_INVESTIGATION |
| PRIOR-010 L6 never fired | yes (all runs) | CONFIRMED — same as L6 OPEN_BUG |
| PRIOR-012 Scope bare __init__.py | not checked | NEEDS_INVESTIGATION |
| PRIOR-013 importer.py missing top 3 | not checked | NEEDS_INVESTIGATION |

## Bugs Fixed This Session

### BUG-001: Finish handler events marked emitted=True despite being dead writes
- **Status:** PROVEN
- **Failure class:** G1 (truth bug)
- **Root cause:** `_emit_structured_event()` in finish handler defaults to `emitted=True`, but agent has already FINISHED
- **Fix:** Added `emitted=False, suppressed=True, suppression_reason="finish_handler_dead_write"` to 5 event emission sites in the finish handler
- **Files changed:** `scripts/swebench/oh_gt_full_wrapper.py` (lines 4751, 4753, 4795, 4798, 4930)
- **Proof:** 6/6 regression tests pass:
  - Pre-fix artifacts confirm `emitted=True` (the bug)
  - Post-fix synthetic artifact confirms `emitted=False, suppressed=True` (the fix)
  - Non-finish events remain `emitted=True` (fix is scoped)

### Claim checker bug: L1_KEY_CONTRACTS 0/N passed as zero contradictions
- **Status:** FIXED
- **Root cause:** coarse layer mapping (`L1+ → L1_EDIT_TARGET`) merged L1_KEY_CONTRACTS into L1_EDIT_TARGET; BROKEN claims were silently skipped
- **Fix:** Added claim_id-specific autopsy key overrides; OPEN_BUG status handled explicitly; BROKEN no longer auto-skipped
- **Files changed:** `scripts/gt_check_claims.py`, `docs/stabilization/CLAIM_LEDGER.yml`
- **Proof:** 12/12 claim checker tests pass including `test_l1_key_contracts_zero_visibility_contradicts`
