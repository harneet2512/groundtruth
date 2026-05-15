# L5 Current Audit — 2026-05-15

## Critical Findings

| # | Finding | Label | File:Lines |
|---|---------|-------|------------|
| 1 | hypothesis_falsified dead — 0/29 agent-visible test failures | FIRED_ONLY_FAKE_METRIC | hooks.py:90-112 |
| 2 | same_failure_persisted dead — same root cause | FIRED_ONLY_FAKE_METRIC | hooks.py:115-135 |
| 3 | L5 queries graph.db directly (_get_structural_suggestions) | SAFETY_RISK | governor.py:357-423 |
| 4 | Dual logging: ad-hoc JSONL (no event_id) + wrapper GTLayerEvent | IMPLEMENTED_NOT_WIRED | governor.py:433-457 |
| 5 | No diff/patch tracking — cannot detect collapse/revert | MISSING | state.py |
| 6 | No file-read observation — L5 blind to agent exploration | MISSING | governor.py:111-155 |
| 7 | extract_exit_code dependency — silent failure detection failure | WIRED_BUT_NOT_MEASURABLE | classifier.py:143-150 |
| 8 | L5Decision.evidence_items always empty list | WIRED_BUT_NOT_MEASURABLE | governor.py:39 |
| 9 | 2 deprecated hooks retained (patch_hypothesis, symptom_convergence) | IMPLEMENTED_NOT_WIRED | hooks.py:71-87, 138-152 |
| 10 | /tmp/gt_l5_state.json shared across workers | SAFETY_RISK | state.py:31 |

## Label Distribution

| Label | Count |
|---|---|
| IMPLEMENTED_AND_WIRED | 26 |
| IMPLEMENTED_NOT_WIRED | 3 |
| WIRED_BUT_NOT_MEASURABLE | 4 |
| FIRED_ONLY_FAKE_METRIC | 1 |
| TEST_SPECIFIC | 3 |
| GENERALIZED_EVENT_DRIVEN | 3 |
| SAFETY_RISK | 4 |

## What Works

- State machine: bands, edit tracking, verification targeting, failure dedup, persistence, reset detector
- Command classification: multi-ecosystem regex (pytest/jest/go test/cargo test/tsc/mypy/eslint/ruff)
- L5bSafetyChecker: restart language blocking, late broad exploration, 180-token cap
- Online next_action tracker: 3-action window, L5-L5b chain on ignore
- Wrapper integration: governor init, structured event emission, parent/child linking

## What's Broken

- hypothesis_falsified: precondition (agent-visible test failure) doesn't hold in 29/29 tasks
- same_failure_persisted: same root cause
- No diff tracking: cannot detect patch collapse, revert, expanding/shrinking diff
- No file-read observation: premature_commitment's confirming edge count is wrapper-dependent

## What's Missing

- Diff/patch content tracking
- Go/Rust/Java/Ruby test parsers (only Python + TypeScript dedicated)
- evidence_items population in L5Decision
- Cross-L5-event correlation / escalation logic
