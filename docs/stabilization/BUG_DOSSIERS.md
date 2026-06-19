# Bug Dossiers

## BUG-001: Finish handler events marked emitted=True despite being dead writes

Status: PROVEN
Layer: L5b, L6
Failure class: G1 (event says delivered but output lacks evidence)
Task/run: delgan__loguru-1306 / GHA run 26525222275
Git SHA: fd05bebf

### Expected behavior
When GT evidence is generated in the finish handler (after AgentState.FINISHED), telemetry events should be marked `emitted=false, suppressed=true, reason="finish_handler_dead_write"`. The agent cannot step after FINISHED, so any content appended to the observation is a dead write.

### Actual behavior
Telemetry events record `emitted=true, suppressed=false` for L5b Scope Check and L6 Pre-Submit Review content that is generated and appended in the finish handler. The full_run.log confirms the sequence:
1. Line 466: `Setting agent state from RUNNING to FINISHED`
2. Line 468: `[GT_DELIVERY] append_observation OK: +185 chars` — L5b Scope Check (AFTER FINISHED)
3. Line 475: `[GT_DELIVERY] append_observation OK: +454 chars` — L6 Pre-Submit (AFTER FINISHED)

The autopsy flags these as G1 divergences: "events say generated but output.jsonl lacks evidence."

### Full trajectory evidence
- output.jsonl entry [56] is `finish` action — last agent action
- No entries after [56] contain `[GT L5:` or `[PRE-SUBMIT]`
- gt_layer_events: L5b event has `emitted=true` at timestamp 1779900746
- full_run.log line 466: FINISHED at 16:52:26, L5b delivery at line 468 (same second)

### Agent-visible evidence
Agent never saw L5b Scope Check or L6 Pre-Submit Review. Confirmed by searching all output.jsonl observations.

### GT event evidence
- L5 event: `event_type=multi_file_scope_warning, emitted=true, suppressed=false`
- L5b event: `event_type=intervention_multi_file_scope_warning, emitted=true, suppressed=false`
- L6 event: `event_type=pre_submit, emitted=true, suppressed=false` (inferred from delivery log)

### Root cause
The finish handler in oh_gt_full_wrapper.py runs after OH sets `state=FINISHED`. The `_deliver_or_trace()` function appends content to the observation and logs `DELIVERED`, but the agent never takes another step. The telemetry writer records `emitted=True` based on `_deliver_or_trace()` succeeding, not based on the agent actually reading the content.

DOC_OF_HONOR section 2.8 already documents this as BROKEN for L6. But the telemetry still lies about it, and L5b scope warnings have the same problem.

### Research fit check
ENGINEERING_INVARIANT — this is a telemetry truthfulness bug, not a heuristic or ranking question. The fix is to mark dead writes honestly in the event stream.

### Patch plan
In the telemetry writer call sites within the finish handler, set `emitted=False, suppressed=True, suppression_reason="finish_handler_dead_write"`. This affects:
1. L5b scope warning event in finish handler
2. L6 pre-submit review event in finish handler
3. Any other `_deliver_or_trace()` calls in the finish handler path

### Regression test
Test that events written during finish handler have `emitted=false`.

### Before result
Pre-fix artifacts prove the bug exists:
- TestPreFixLoguru::test_l5b_scope_warning_has_emitted_true_before_fix — PASS (confirms emitted=True in old artifact)
- TestPreFixBeets::test_l5_scope_warning_has_emitted_true_before_fix — PASS (confirms emitted=True in old artifact)

### After result
Synthetic post-fix artifact proves the fix works (6/6 tests pass):
- TestPostFixSynthetic::test_l5_scope_warning_emitted_false — PASS (emitted=False)
- TestPostFixSynthetic::test_l6_pre_submit_emitted_false — PASS (emitted=False)
- TestPostFixSynthetic::test_non_finish_events_still_emitted_true — PASS (non-finish events unchanged)

Code fix in oh_gt_full_wrapper.py (5 sites):
- Line 4751: L5 governor finish event: `emitted=False, suppressed=True, suppression_reason="finish_handler_dead_write"`
- Line 4753: L5b intervention finish event: same
- Line 4795: Goku L5 finish event: same
- Line 4798: Goku L5b intervention finish event: same
- Line 4930: L6 pre-submit review event: same

### Replay/runtime proof
- Synthetic fixture: `tests/topology/fixtures/post_fix_finish_events/gt_layer_events_synthetic.jsonl`
- All 6 tests pass against fixture: pre-fix artifacts confirm the bug, post-fix fixture confirms the fix
- topology_result.json shows G1 FAIL for L5B_REMINDER on loguru before fix

### Remaining risk
If other layers also emit events in the finish handler, they need the same fix. The telemetry schema should distinguish "tried to deliver" from "agent received."

---

## BUG-002: L1_KEY_CONTRACTS marker missing despite contract data

Status: PROVEN
Layer: L1+
Failure class: E6 (payload generated but marker absent)
Task/run: beancount__beancount-931 / GHA run 26532251352
Git SHA: fd05bebf

### Expected behavior
When properties table has qualifying entries (guard_clause, conditional_return, side_effect, exception_handler) for the edit-target function, a separate `[GT KEY CONTRACTS]` marker should appear in the agent's observation.

### Actual behavior
Contract data IS queried (line 5799-5803), compiled into `_contract_lines` (line 5827-5828), and merged into `<gt-edit-target>` tags (line 5847). But no `[GT KEY CONTRACTS]` text marker is ever emitted. Autopsy and claim checker look for this marker and never find it.

### Full trajectory evidence
- full_run.log beancount: `[GT_META] l1_enhanced: edit_target=check contracts=1`
- output.jsonl entry 1: `<gt-edit-target>` contains `Preserve: exception_handler: except KeyError -> handles`
- output.jsonl: `[GT KEY CONTRACTS]` absent (confirmed by autopsy 0/3)
- beets/loguru: contracts=0 (no qualifying properties for Pipeline/info functions)

### Root cause
`oh_gt_full_wrapper.py` lines 5845-5859 merge `_contract_lines` into `<gt-edit-target>` or `<gt-orientation>` without a separate `[GT KEY CONTRACTS]` marker. The marker was documented in DOC_OF_HONOR but never implemented as a standalone block.

### Research fit check
ENGINEERING_INVARIANT — missing text marker, not a heuristic.

### Patch plan
After line 5859 (after `_l1_extra` is built), if `_contract_lines` non-empty, append `\n[GT KEY CONTRACTS]\n` + joined lines.

### Regression test
`tests/topology/test_l1_key_contracts_delivery.py` — 6 tests: marker present with properties, absent without, orientation mode, property extraction, artifact evidence.

### Before result
beancount fresh run: contracts=1 in log, `[GT KEY CONTRACTS]` absent from output.jsonl

### After result
6/6 regression tests pass. Fix adds marker emission. Claim changed to CONDITIONAL (fires when properties exist).

### Remaining risk
Claim is now CONDITIONAL. Tasks where edit-target function lacks qualifying properties won't see contracts. This is correct behavior, not a bug.

---

## PRIOR-001: Edit-target points at Pipeline()

Status: PRIOR_KNOWN_UNVERIFIED
Layer: L1+ Edit-Target
Failure class: D5 wrong ranking
Task/run: beetbox__beets-5495 / canary run pre-Phase 4
Git SHA: fd05bebf

### Expected behavior
Edit-target should point to the function most relevant to the issue, not the function with the most callers.

### Actual behavior
Stacktrace bonus picks crash site (Pipeline()), not root cause function (set_fields in importer.py).

### Full trajectory evidence
Not yet reproduced against current code.

### Root cause (suspected)
Edit-target ranking uses caller count as primary signal. Stacktrace bonus (+5) added but "direct" keyword match (+2) inflates common verbs.

### Research fit check
PENDING — ranking/localization bug requires research check before heuristic changes.

---

## PRIOR-002: [PEER] twin absent

Status: PRIOR_KNOWN_UNVERIFIED
Layer: L3 Post-Edit
Failure class: B1/B5 path-node resolution or D6 missing evidence
Task/run: beetbox__beets-5495
Git SHA: fd05bebf

### Expected behavior
Cross-class twin detection should find SingletonImportTask as peer of ImportTask when editing set_fields.

### Actual behavior
[PEER] evidence absent from L3 post-edit output.

### Root cause (suspected)
Cross-class query did not fire; graph path not resolved or node_id resolution returned wrong node.

### Research fit check
PENDING — path resolution is ENGINEERING_INVARIANT if the bug is in SQL; research-sensitive if the twin detection heuristic is wrong.

---

## PRIOR-003: [TEST] still _common.py

Status: PRIOR_KNOWN_UNVERIFIED
Layer: L3 Post-Edit
Failure class: D5/A3 wrong test ranking or assertion linking
Task/run: beetbox__beets-5495
Git SHA: fd05bebf

### Expected behavior
[TEST] should show test_set_fields assertions, not _common.py helpers.

### Actual behavior
Name-match fallback finds _common.py instead of test_set_fields.py due to naming convention mismatch.

### Root cause (suspected)
3-hop query doesn't reach test_set_fields through test framework indirection. Name-match fallback picks conftest/_common.py.

### Research fit check
PENDING — test selection/assertion linking is research-sensitive (TCTracer ICSE 2020).

---

## PRIOR-004: [COMPLETENESS] still noisy

Status: PRIOR_KNOWN_UNVERIFIED
Layer: L3 Post-Edit
Failure class: D2/D5 wrong extraction
Task/run: beetbox__beets-5495
Git SHA: fd05bebf

### Expected behavior
[COMPLETENESS] should be scoped to edited function's shared state only.

### Actual behavior
Extracts class name from diff hunk headers instead of function names, producing noisy evidence.

### Root cause (suspected)
--edited-functions extraction gets class name from `@@ -N,M +N,M @@ class ImportTask` instead of the function name.

### Research fit check
PENDING — extraction logic is ENGINEERING_INVARIANT; scoping heuristic may need research check.

---

## PRIOR-005: jquery.js still present

Status: PROVEN_AND_FIXED
Layer: L3b Post-View, L5b Scope
Failure class: D2 wrong evidence / caller rendering
Task/run: beetbox__beets-5495 / GHA 26525222275
Git SHA: bd5f8880

### Expected behavior
Vendor/static JS files (jquery.js, lodash.min.js, node_modules/) should be filtered from caller evidence.

### Actual behavior
No vendor path exclusion filter existed in any caller query path:
- post_view.py graph_navigation() returned jquery.js in "Called by:" lines
- governor.py _check_multi_file_scope() included jquery.js in scope warnings
- governor.py _get_structural_suggestions() suggested jquery.js as next_action

### Full trajectory evidence
gt_layer_events beets: `beetsplug/web/static/jquery.js:8547` in L3b caller text (5 events).
L5b scope warning: "beetsplug/web/static/jquery.js (1 calls into importer.py)".

### Root cause
No _is_vendor_path() filter existed. SOURCE_EXTS includes .js. Caller queries return all non-self callers.

### Research fit check
ENGINEERING_INVARIANT — vendor file exclusion is a filter correctness bug.

### Patch
Added _is_vendor_path() with patterns (/static/, /vendor/, /node_modules/, /dist/, .min., /assets/) to post_view.py and governor.py.

### Regression test
test_vendor_js_exclusion.py — 4 tests: jquery excluded, lodash excluded, bench.py preserved, scope warnings filtered.

### Before result
jquery.js and lodash.min.js appeared in "Called by:" output (reproduced in test).

### After result
4/4 tests pass. Vendor JS excluded, legitimate callers preserved.

---

## PRIOR-008: [PATTERN] shows __init__

Status: PRIOR_KNOWN_UNVERIFIED
Layer: L3 Post-Edit
Failure class: D2 wrong evidence / missing dunder filter
Task/run: beetbox__beets-5495
Git SHA: fd05bebf

### Expected behavior
Dunder methods (__init__, __repr__, etc.) should be filtered from sibling pattern evidence.

### Actual behavior
__init__ appears in [PATTERN] output.

### Root cause (suspected)
Dunder filter not applied in the sibling rendering code path.

### Research fit check
ENGINEERING_INVARIANT — filter gap.

---

## PRIOR-010: L6 never fired on beets

Status: PRIOR_KNOWN_UNVERIFIED
Layer: L6 Pre-Submit
Failure class: F2/C4 finish evidence too late / finish trigger
Task/run: beetbox__beets-5495
Git SHA: fd05bebf

### Expected behavior
L6 pre-submit review should fire before agent finishes, giving the agent a chance to act on it.

### Actual behavior
Agent finished at iter 54. L6 early review condition not met. L6 runs in finish handler AFTER state=FINISHED — agent never sees it.

### Root cause (suspected)
OH sets state=FINISHED before calling run_action. L6 review in finish handler is dead code for agent interaction. DOC_OF_HONOR already marks this BROKEN.

### Research fit check
PENDING — timing/actionability question. May need agent-workflow literature if proposing pre-finish intercept.

---

## PRIOR-012: Scope bare __init__.py

Status: PRIOR_KNOWN_UNVERIFIED
Layer: Consensus
Failure class: D2 formatting/path display
Task/run: beetbox__beets-5495
Git SHA: fd05bebf

### Expected behavior
Scope display should show `beets/__init__.py` not bare `__init__.py`.

### Actual behavior
_short_path not applied in gt-scope block rendering.

### Root cause (suspected)
Consensus/scope rendering uses basename or raw path without normalization.

### Research fit check
ENGINEERING_INVARIANT — display formatting bug.

---

## PRIOR-013: importer.py not in top 3 brief

Status: PRIOR_KNOWN_UNVERIFIED
Layer: L1 Brief
Failure class: D5 brief ranking
Task/run: beetbox__beets-5495
Git SHA: fd05bebf

### Expected behavior
importer.py (the file containing set_fields, the bug-relevant function) should appear in top 3 brief candidates.

### Actual behavior
Issue-keyword boost does not overcome structural ranking (caller count dominates).

### Root cause (suspected)
Brief ranking in graph_map.py prioritizes caller count over issue-keyword relevance.

### Research fit check
PENDING — ranking/localization is research-sensitive (SweRank ICLR 2025, fault localization literature).
