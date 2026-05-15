# Phase 8: Stop/Go Decision

## Build Now (Items 1-5, highest ROI)

| # | Item | LOC | Risk | Expected Impact |
|---|------|-----|------|-----------------|
| 1 | L3 structural next_action (callers first) | ~30 | LOW | HIGH — unblocks entire reaction chain |
| 2 | L3b primary-edge selection + pruning | ~40 | LOW | HIGH — cuts 1810 avg to ~300 |
| 3 | Reaction joiner structural actions | ~20 | LOW | HIGH — classifies read_file follows |
| 4 | L5 ignored_next_action + structural_unverified | ~50 | MEDIUM | HIGH — detects 90% of missed patterns |
| 5 | L5b caller/consumer/signature actions | ~30 | LOW | MEDIUM — concrete instead of vague |

**Total: ~170 LOC, 5 items, all flag-gated.**

## Build Second (Items 6-9, incremental)

| # | Item | LOC | Risk |
|---|------|-----|------|
| 6 | Hygiene collapse detection | ~20 | LOW |
| 7 | L6 relationship freshness | ~15 | LOW |
| 8 | L1 primary witness enrichment | ~25 | LOW |
| 9 | L4 compact risk frame | ~15 | LOW |

**Total: ~75 LOC, all independent of items 1-5.**

## Defer

| Item | Reason |
|------|--------|
| Relationship extractors (decorators/routes/config/events) | Requires Go indexer changes. HIGH effort + risk. Prove the query patterns work with CALLS/IMPORTS first. |
| L4 interactive tools (gt_query/gt_search) | 0 agent usage in all runs. Remove dead weight or redesign before investing more. |
| Cross-layer causal measurement | No proven method to attribute agent behavior to specific GT signal vs coincidence. Report correlation, don't claim causation. |

## Reject

| Item | Reason |
|------|--------|
| Task-specific test commands | Violates anti-overfitting rule |
| Benchmark-specific conditionals | Same |
| L3b full graph dumps after early phase | SWE-Pruner proves less context = better |
| Test-only next_action | Dead for 90% of real repos |

## Expected Outcomes After Items 1-5

- **next_action > 0** on every task where L3 fires with callers (currently 0/12)
- **Reactions produced** for structural actions (currently 0)
- **L3b chars reduced** from 1810 avg to ~300 avg per fire
- **L5 fires on collapsed trajectories** (currently misses them)
- **No regression** — all changes flag-gated, flags OFF = identical behavior

## Expected Token Savings

- L3b: 1810 → ~300 avg/fire = ~83% reduction
- L3: no change (already capped at 1200 chars)
- L5b: no change (already capped at 180 tokens)
- Net: ~3000 tokens saved per task from L3b pruning alone

## Ready to Code?

YES — after this document + decisions.md update are committed and approved.

The 5 highest-ROI items are:
1. Small (170 LOC total)
2. Flag-gated (zero risk when off)
3. Research-backed (RepoGraph callers, SWE-Pruner pruning, Agentless no-test validation)
4. Provably measurable (reaction chain fires end-to-end)
5. Generalizes to real repos (callers always exist when graph edges exist)
