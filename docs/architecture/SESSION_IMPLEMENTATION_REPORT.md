# Session Implementation Report — 2026-05-27/28

## Branch: gt-architecture-rebuild

## Objective
Make every layer in DOC_OF_HONOR work at runtime. Reduce noise without killing signal.
Topic-by-topic verification from DOC_OF_HONOR. Phase 3 output diet. Deep trajectory analysis.

## Commits (17 total)

| # | SHA | Message | Tests |
|---|-----|---------|-------|
| 1 | 73713a4e | Cursor-mode benchmark gate (docs) | 0 |
| 2 | 7f473f36 | Weasyprint regression analysis (docs) | 0 |
| 3 | 0a702238 | L1 edit-target issue-symbol matching | +10 |
| 4 | 4dd16785 | Consensus bridge for issue-symbol files | 0 |
| 5 | 6b6bfd66 | L5b cap at 2 + relevance + file dedup | +9 |
| 6 | 9b653b55 | L3b hybrid dedup (per-file-once + reindex reset) | +11 |
| 7 | 0e0e9acd | Test naming convention fallback | +5 |
| 8 | a5b69d6d | Assertion resolution: dynamic threshold + rescue + score column | 0 (Go) |
| 9 | 38c2e0aa | DOC_OF_HONOR 5 stale claims fixed | 0 |
| 10 | b5eb55c1 | Phase 3 output diet (REVIEW first, RAISES gate, callee suppress, L4a dedup) | +11 |
| 11 | 031102cd | Preflight checks script (20 checks) | 0 |
| 12 | 6bf7e753 | Fix Go incremental path 2-value return | 0 |
| 13 | 1ea25557 | Fix preflight for v15.2 schema | 0 |
| 14 | d6c3adea | Fix REQUIRED_SCHEMA_VERSION + router defense-in-depth | 0 |
| 15 | 17848a5c | Class vs function scoring + MISMATCH fix + reindex scope | +1 |
| 16 | e2274317 | L5 nudge specific file + remove tool instructions | 0 |

**Total: 47 new tests. 158 passing.**

## Phase 3 Implementation Plan Coverage

| Item | Priority | Status | Evidence |
|------|----------|--------|----------|
| 1.1 Remove Ignored Structural Witness | P0 | DONE | Capped to 2, relevance-gated |
| 1.2 Deduplicate L3 callers | P0 | DONE | Per-file-once + edited-file-only reset |
| 1.3 Hub rejection | P0 | DONE | Issue-symbol injection + Class +200 scoring |
| 2.1 L6 review first | P0 | DONE | PRESERVE/REVIEW in U-shape primacy |
| 3.1 Gate RAISES/CATCHES | P1 | DONE | Error keyword gate |
| 3.2 Suppress callees | P1 | DONE | Removed from graph_navigation() |
| 3.3 Suppress L4a after L3 | P1 | DONE | l3b_file: key check |
| 3.4 PRIOR-004 completeness | P1 | ALREADY DONE | Pre-session commit |
| 4.1 Filter test assertions | P2 | PARTIAL | Naming convention fallback added |
| 4.2 Gate patterns on keywords | P2 | INTENTIONALLY SKIPPED | sh-744 trajectory proves pattern value comes from non-obvious matches |

## Smoke Results

### Phase 3 smoke (run 26551984847, commit d6c3adea)

| Task | Cursor rerun | Phase 3 | GT inj before | GT inj after |
|------|-------------|---------|---------------|-------------|
| sh-744 | True | **True** | 41 | 27 (-34%) |
| weasyprint | False | **True (FLIP)** | 54 | 29 (-46%) |
| flexget | False | False | 20 | 22 (+10%) |
| cfn-lint | False | False | 73 | 44 (-40%) |
| pypsa | False | False | 47 | 26 (-45%) |
| arviz | False | False | 31 | 15 (-52%) |
| **Total** | **1/6** | **2/6** | **266** | **163 (-39%)** |

### Deep Trajectory Findings (6 tasks, line-by-line)

| Metric | sh-744 | weasyprint | arviz | flexget | cfn-lint | pypsa |
|--------|--------|-----------|-------|---------|---------|-------|
| Resolved | True | **True (FLIP)** | False | False | False | False |
| GT engagement | 31% | 12% | 17% | 0% | ~20% | ~10% |
| Critical GT injection | wait() pattern | establishes_formatting_context callers | none | none | none | none |
| Edit-target correct | No | No | Yes | No | No | No |
| Why failed | — | — | Wrong exception type | Missing pyproject.toml | Ran out of iterations | Reproduction loop |

### Metrics That Matter (derived from trajectories)

1. **Edit-target accuracy** (1/6 = 17%, target ≥50%)
2. **Critical injection count** (2/6 tasks had one)
3. **Noise ratio** (~60%, target <30%)
4. **GT engagement rate** (0-31%, target >40%)
5. **Misleading injection count** (0-3 per task, target 0)

## Bugs Found and Fixed During Smoke

| Bug | Root cause | Fix |
|-----|-----------|-----|
| All 6 tasks failed (run 26551502317) | Go incremental path: 1 var but function returns 2 | `6bf7e753` |
| All 6 tasks failed (run 26551793605) | Preflight expects v15.1, gt-index produces v15.2 | `1ea25557` |
| 0 L3b delivery (run 26551984847 attempt) | REQUIRED_SCHEMA_VERSION=v15.1 vs produced v15.2, SchemaMismatch kills router, no try/except at call site | `d6c3adea` |

## Item Intentionally Not Done

**P2 4.2: Gate patterns on issue-keyword overlap.**

The deep trajectory analysis proved this would be harmful. sh-744's resolution depended on [PATTERN] sibling wait() — the agent used this to add self.wait() in the fix. The issue text doesn't mention "wait". Gating patterns on issue keywords would have suppressed the single most valuable GT injection in the entire 6-task run.

Pattern evidence's value comes from showing structural alternatives the agent didn't think of. By definition, these won't match issue keywords. Research supports this: Beyond Resolution Rates (Mehtiyev 2026) — "agents that gather context before editing succeed more often." The context includes patterns the agent can't predict from the issue text.

## Rollback Plan

Each commit is independent and reversible:
- Schema changes: `git revert a5b69d6d` + update REQUIRED_SCHEMA_VERSION back
- Output diet: `git revert b5eb55c1`
- Bug fixes: `git revert 17848a5c`
- Full rollback to pre-session: `git reset --hard 73713a4e~1`

## Next Steps

1. Push and trigger smoke with all fixes including class scoring + MISMATCH + reindex scope
2. Verify pypsa edit-target now selects expanded_capacity (Class scoring fix)
3. Verify flexget MISMATCH false positive gone
4. Compare noise metrics against Phase 3 smoke baseline
