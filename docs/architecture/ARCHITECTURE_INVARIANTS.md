# Architecture Invariants

Derived from DOC_OF_HONOR.md intent + OpenHands integration reality.
These must hold for the architecture to work. Each is testable.

## Invariant 1 — Delivery Truth

gt_layer_events `emitted=true` implies output.jsonl contains agent-visible evidence for that layer,
UNLESS the event explicitly has `suppressed=true` with a `suppression_reason`.

Violation = G1 in failure taxonomy.

Test: `tests/invariants/test_delivery_truth.py`

## Invariant 2 — L6 Actionability

Pre-submit review evidence (caller contracts, test suggestions) must appear in the agent's observation
BEFORE AgentFinishAction. Content appended after state=FINISHED is a dead write.

Violation = F2 in failure taxonomy.

Test: `tests/invariants/test_l6_actionability.py`

## Invariant 3 — Edit Target Selection

If the issue text explicitly names a candidate function (e.g., "set_fields"),
that function must be selected over unrelated high-caller-count functions (e.g., Pipeline).
Caller count is a tie-breaker, not the primary signal.
All candidates must be evaluated before selection (no first-match-wins).

Violation = D5 in failure taxonomy.

Test: `tests/invariants/test_l1_visibility.py`

## Invariant 4 — Test Evidence Ranking

Helper/support files (_common.py, conftest.py, helper.py) must not outrank
direct relevant test files (test_set_fields.py) when assertion evidence links
the relevant test to the edited function.

Violation = D5 in failure taxonomy.

Test: `tests/invariants/test_l3_post_edit.py`

## Invariant 5 — Completeness Scope

[COMPLETENESS] evidence must be scoped to the edited function's shared state,
not the entire class. "ImportTask.chosen_info shares choice_flag with ImportTask.set_choice"
is class-level noise if the agent edited `set_fields`, not `chosen_info` or `set_choice`.

Violation = D2 in failure taxonomy.

Test: `tests/invariants/test_l3_post_edit.py`

## Invariant 6 — Pattern Evidence Hygiene

Dunder methods (__init__, __repr__, __str__, __eq__, __hash__, __del__)
must not appear as sibling-pattern examples in [PATTERN] evidence.
They are boilerplate, not behavioral references.

Violation = D2 in failure taxonomy.

Test: `tests/invariants/test_l3_post_edit.py`

## Invariant 7 — Path Resolution

All layer queries that look up file paths in graph.db must resolve
host/container/workspace paths consistently. Either:
- Use a single universal resolver function, OR
- Each LIKE suffix match must be proven safe (no false matches on partial paths)

Violation = B1/B2/B6 in failure taxonomy.

Test: `tests/invariants/test_path_resolution.py`

## Invariant 8 — Claim Ledger Truth

DOC_OF_HONOR claims marked WORKING or VERIFIED must have at least one of:
- Passing topology test
- Trajectory proof (output.jsonl contains expected marker)
- Replay proof (frozen artifact shows expected behavior)
- Graph proof (graph.db contains expected data)

Claims with only code_audit proof are UNVERIFIED, not VERIFIED.

Violation = claim checker failure.

Test: `tests/invariants/test_claim_truth.py`

## Invariant 9 — Vendor Exclusion

Vendor, static, minified, or generated JavaScript files must never appear
in caller/callee evidence in any layer (L3b, L5b scope, grep intercept).

Patterns: `/static/`, `/vendor/`, `/node_modules/`, `/dist/`, `.min.`, `/assets/`

Violation = D2 in failure taxonomy.

Test: `tests/invariants/test_vendor_filter.py`

## Invariant 10 — Baseline Isolation

When `GT_BASELINE=1`, the agent must receive zero GT evidence markers.
No `<gt-task-brief>`, no `[SIGNATURE]`, no `[GT_AUTO]`, no `Called by:`.
Baseline arm is the control group.

Violation = C6 in failure taxonomy.

Test: `tests/invariants/test_baseline_isolation.py`
