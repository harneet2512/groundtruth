# Generalization 6-Task Audit

GHA Run: 26542650701
Commit: 271a54ff (gt-architecture-rebuild)
Date: 2026-05-27

## Per-Task Architecture Matrix

| Task | Repo | Resolved | Edit target | Test evidence | Completeness | Pattern | Vendor | GT_AUTO | L6 review | L5 scaffold | Tier-0 | Verdict |
|------|------|----------|-------------|---------------|--------------|---------|--------|---------|-----------|-------------|--------|---------|
| weasyprint-2300 | kozea/weasyprint | YES | flex_layout() | OK (no _common) | OK | OK (no dunder) | OK (no vendor JS) | OK (visible) | YES | N/A | none | PASS |
| flexget-4306 | flexget/flexget | NO | Session() | OK (no _common) | CLASS-WIDE | OK | OK | OK | YES | N/A | none | PARTIAL |
| pypsa-1172 | pypsa/pypsa | N/A* | Network() | OK | OK | OK | OK | OK | NO (no callers) | N/A | none | PASS |
| cfn-lint-3875 | aws/cfn-lint | N/A* | Properties() | OK | OK | OK | OK | OK | NO (no callers) | N/A | none | PASS |
| sh-744 | amoffat/sh | YES | bake() | OK | CLASS-WIDE | OK (wait(), no dunder) | OK | OK | YES | YES (iter 20) | L5B G1 | PARTIAL |
| arviz-2413 | arviz-devs/arviz | NO | plot_hdi() | OK | OK | OK | OK | OK | NO | N/A | L5B G1 | PARTIAL |

*N/A = eval result not available in artifacts (may have timed out or eval step skipped)

## Bug Class Recurrence

| Bug class | Fixed in session | Recurs on 6-task? | Tasks affected | Verdict |
|-----------|-----------------|-------------------|----------------|---------|
| PRIOR-003 (_common.py test ranking) | YES (helper deprioritization) | NO — 0/6 tasks show _common.py in [TEST] | none | GENERALIZED |
| PRIOR-004 (class-wide completeness) | YES (graph.db fallback) | YES — 2/6 tasks (flexget, sh-744) | flexget-4306, sh-744 | PARTIAL FIX |
| PRIOR-005 (vendor JS in callers) | YES (vendor filter) | NO — 0/6 tasks show vendor JS in GT evidence | none | GENERALIZED |
| PRIOR-008 (__init__ in pattern) | YES (dunder filter) | NO — 0/6 tasks show __init__ in [PATTERN] | none | GENERALIZED |
| BUG-001 (emitted=True dead write) | YES (telemetry truth) | Known — L5B G1 on 2/6 (finish handler timing) | sh-744, arviz-2413 | KNOWN RESIDUAL |
| GT_AUTO hidden leak | NOT A BUG (visible by design) | N/A | none | N/A |

## Architecture Quality Table

| Metric | 6-task unseen |
|--------|---------------|
| Resolve rate | 2/4 evaluated (weasyprint YES, sh-744 YES, flexget NO, arviz NO; pypsa/cfn-lint N/A) |
| Edit-target failures | 0 clear failures (all selected high-caller functions; correctness depends on issue) |
| Test-evidence failures | 0/6 — no _common.py/conftest outranking direct tests |
| Completeness failures | 2/6 — class-wide noise on flexget + sh-744 |
| Pattern leaks | 0/6 — no __init__ in [PATTERN] |
| Vendor/static leaks | 0/6 — no vendor JS in GT evidence |
| GT_AUTO leaks | 0/6 — [GT_AUTO] is visible by design |
| Tier-0 truth bugs | 0 new — L5B G1 is known residual (finish handler timing) |
| Claim contradictions | pending claim checker run |

## Interpretation

### GENERALIZED (3 bug classes fixed across unseen repos):
- **PRIOR-003 test ranking:** _common.py deprioritization works on all 6 repos. No helper file outranks direct tests.
- **PRIOR-005 vendor filter:** Zero vendor JS in caller evidence across 6 different repos.
- **PRIOR-008 dunder filter:** Zero __init__ in [PATTERN] across 6 repos.

### PARTIAL FIX (1 bug class partially fixed):
- **PRIOR-004 completeness scope:** Graph.db fallback for function name extraction works when graph.db is available AND changed lines fall within a function's start_line..end_line range. Fails on 2/6 tasks (flexget, sh-744) — likely because either graph.db wasn't available for the fallback query, or the changed lines didn't match a function range.

### KNOWN RESIDUAL (1 existing issue):
- **L5B G1:** L5B scope check fires in finish handler on some tasks, marked emitted=True in events but agent never sees it. BUG-001 fix marks these as `emitted=False, suppressed=True` but the autopsy event-to-layer mapping still picks up L5 events with `emitted=True` that are NOT finish-handler events. This is an autopsy classification issue, not a production bug.

## Remaining Work

1. **PRIOR-004:** Investigate why completeness scope fallback fails on flexget/sh-744. May need to check whether graph.db is available to the wrapper during obligation check, and whether changed line ranges match function boundaries.
2. **L5B autopsy mapping:** Refine event-to-layer classification so L5 `ignored_next_action` events (which ARE visible) are not confused with L5b finish-handler scope checks (which are dead writes).
3. **BUG-003 edit target:** Remains CONDITIONAL — the selected function depends on which files are in the brief, which is limited by graph connectivity ranking. No semantic localization without LLM.
