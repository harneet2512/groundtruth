# 07 Verification Plan -- Per-Layer Test Specifications

**Date:** 2026-05-15
**Depends on:** `06_implementation_plan.md` (what to build), `01_layer_audit.md` (current state)
**Principle:** Every test uses in-memory SQLite with synthetic graph.db fixtures. No network calls, no containers, no paid LLM invocations. Tests run in < 5 seconds total.

---

## L3 Tests: Structural next_action hierarchy

All tests target the `next_action_type` population logic added by implementation plan item 1. Test fixture: in-memory graph.db with 3 files (source A, source B, test T), edges A->B (CALLS, confidence 0.9), T->A (CALLS, is_test=1, confidence 1.0).

### Test 1: caller exists, no tests --> emits READ_CALLER_CONTRACT

**Setup:** Agent edits file B. File B has 1 caller (file A, line 145, `a_func()` calls `b_func()`). File B has 0 test edges (no test file imports or calls B).
**Action:** Run L3 evidence generation with `GT_STRUCTURAL_NEXT_ACTION=1`.
**Assert:** `next_action_type == "READ_CALLER_CONTRACT"`, `next_action_file == "src/a.py"`.
**Why:** Callers are the highest-priority structural witness. When present, the agent should read the caller to understand the contract it must preserve. This is the most common case on real repos with non-trivial call graphs.

### Test 2: consumer exists --> emits READ_CONSUMER

**Setup:** Agent edits file A. File A has 0 same-repo callers (it is a top-level entry point). File A has 1 consumer: file C imports a class from A and uses it as a type annotation (edge type IMPORTS, confidence 1.0).
**Action:** Run L3 evidence generation with `GT_STRUCTURAL_NEXT_ACTION=1`.
**Assert:** `next_action_type == "READ_CONSUMER"`, `next_action_file == "src/c.py"`.
**Why:** When no callers exist but consumers do (import-only edges), the consumer is the next best structural witness. The agent should verify its edit does not break the consumer's type expectations.

### Test 3: no witness --> RUN_STATIC_SANITY or NONE

**Setup:** Agent edits file D. File D has 0 callers, 0 consumers, 0 test edges. File D has a signature in graph.db (`def isolated_func(x: int) -> str`).
**Action:** Run L3 evidence generation with `GT_STRUCTURAL_NEXT_ACTION=1`.
**Assert:** `next_action_type == "RUN_STATIC_SANITY"` when signature exists; `next_action_type == "NONE"` when even signature is absent.
**Why:** With no structural witnesses, the hierarchy bottoms out. `RUN_STATIC_SANITY` is a weak directive (check types/lint) that costs nothing. `NONE` means the reaction joiner will skip this event cleanly without producing a misleading `IGNORED` reaction.

### Test 4: mapped test --> RUN_TARGETED_TEST as optional bonus

**Setup:** Agent edits file A. File A has 1 caller (file B) AND 1 mapped test (file T, `test_a_func` calls `a_func`).
**Action:** Run L3 evidence generation with `GT_STRUCTURAL_NEXT_ACTION=1`.
**Assert:** `next_action_type == "READ_CALLER_CONTRACT"` (callers take priority), `next_action_test == "tests/test_a.py::test_a_func"` (test is a bonus, not the primary directive).
**Why:** The hierarchy is strict: callers > consumers > signature > tests. Tests are valuable but secondary. The agent should read the caller first, then optionally run the targeted test. This ensures the reaction joiner classifies by the primary action, not the test.

### Test 5: output <= 300 tokens

**Setup:** File B has 10 callers (each with a 120-char code line), 5 test assertions, a signature, and 3 siblings. Maximum possible evidence.
**Action:** Run L3 evidence generation with any flags.
**Assert:** `len(output) <= 1200` chars (the `_MAX_EVIDENCE_CHARS` hard cap from post_edit.py line 54). Token estimate `len(output) // 4 + 1 <= 301`.
**Why:** The structural next_action addition must not increase the rendered output size. The hierarchy only affects `next_action_type` metadata, not the evidence text. This test guards against accidental injection of hierarchy text into the agent-visible output.

---

## L3b Tests: Primary-edge selection + rendered pruning

All tests target the pruning logic added by implementation plan item 2. Test fixture: in-memory graph.db with a hub file (20 callers), a focused file (2 callers), and issue keywords "authentication login".

### Test 1: multiple edges --> selects one primary

**Setup:** File F has 5 caller edges. Caller C1 has file content matching issue keyword "authentication" (relevance score 0.8). Callers C2-C5 have no keyword overlap (relevance score 0.0).
**Action:** Run `graph_navigation()` with `GT_L3B_PRIMARY_EDGE=1`.
**Assert:** Output contains exactly 1 fully-rendered caller line (C1 with function name and call count). Output contains a count line: `+4 more callers`. No other caller file paths appear in the rendered text.
**Why:** SWE-Pruner (A3) proves goal-conditioned selection outperforms full dumps. The primary edge is the one most relevant to the task, not the one with the most calls.

### Test 2: hub file --> no dump

**Setup:** File H is a hub file with in-degree 50 (above p90 threshold). Agent views file H.
**Action:** Run `graph_navigation()` with `GT_L3B_PRIMARY_EDGE=1`.
**Assert:** Output contains at most 1 primary caller + count line, OR output is `[GT_STATUS] no_evidence:hub_file_suppressed`. The output does NOT contain 5+ caller lines (the existing `limit=5` default without primary-edge selection).
**Why:** Hub files are the worst bloat offenders. The existing hub penalty reduces scores but still renders multiple edges. Primary-edge selection + hub detection should produce minimal or zero output for hubs.

### Test 3: visited suppressed

**Setup:** File F has 3 caller edges (C1, C2, C3). Agent has already viewed C1 and C2 (present in `/tmp/gt_viewed.txt`).
**Action:** Run `graph_navigation()` with `GT_L3B_PRIMARY_EDGE=1`.
**Assert:** Primary edge is C3 (the only non-visited caller). C1 and C2 do not appear in rendered output or count line. If C3 is also visited, output is empty or `[GT_STATUS] no_evidence:all_visited`.
**Why:** Visited-file suppression is an existing feature (audit L3b.2, lines 291-293). Primary-edge selection must respect it: the primary edge must be a non-visited file.

### Test 4: late output <= 80 tokens or silent

**Setup:** Iteration ratio = 0.90 (FINALIZATION band). File F has 3 caller edges.
**Action:** Run `graph_navigation()` with `GT_L3B_PRIMARY_EDGE=1`.
**Assert:** `len(output) <= 320` chars (~80 tokens). If no non-visited, non-hub primary edge exists, output is empty (silent).
**Why:** At finalization, the agent should be converging. L3b output at this stage is noise unless it names a specific file the agent has not seen. The 80-token target is a 5.6x reduction from the current 452-token average.

### Test 5: finalization 0 broad edges

**Setup:** Iteration ratio = 0.87 (FINALIZATION band). File F has 5 caller edges. 2 callers are in the brief candidate set. 3 callers are not in the brief candidate set and not in the visited set (broad edges).
**Action:** Run `graph_navigation()` with `GT_L3B_PRIMARY_EDGE=1`.
**Assert:** Output contains 0 broad edges. Only the 2 brief-candidate callers are eligible for primary selection. If neither is relevant, output is silent.
**Why:** Implementation plan item 2 specifies "at finalization band, suppress all broad edges." Broad edges are edges to files not in the brief candidate set or visited set. At finalization, the agent should not be exploring new territory.

---

## Reaction Tests: Structural follow classification

All tests target the reaction joiner extensions from implementation plan item 3. Test fixture: synthetic GTLayerEvent list + synthetic trajectory action list.

### Test 1: opening caller --> FOLLOWED_RELATED_FILE or FOLLOWED_EXACT

**Setup:** L3 event with `next_action_type="READ_CALLER_CONTRACT"`, `next_action_file="src/auth/login.py"`. Agent's next action is `read_file("src/auth/login.py")`.
**Action:** Run `compute_follow_type()`.
**Assert:** `follow_type == "FOLLOWED_EXACT"`.
**Variant:** Agent's next action is `read_file("src/auth/session.py")` (same directory, different file). Assert: `follow_type == "FOLLOWED_RELATED_FILE"`.
**Why:** Exact file match is the strongest signal. Same-directory match is weaker but still indicates the agent engaged with the structural evidence.

### Test 2: broad test --> FOLLOWED_BROAD_ONLY

**Setup:** L3 event with `next_action_type="READ_CALLER_CONTRACT"`, `next_action_file="src/auth/login.py"`. Agent's next action is `run_command("pytest tests/")` (broad test run, not a file read).
**Action:** Run `compute_follow_type()`.
**Assert:** `follow_type == "FOLLOWED_BROAD_ONLY"`.
**Why:** Running a broad test instead of reading the suggested caller means the agent did not engage with the structural evidence. It is doing something reasonable (testing) but not what GT directed. This is not `IGNORED` (the agent is working) but not `FOLLOWED_EXACT` either.

### Test 3: ignoring --> IGNORED

**Setup:** L3 event with `next_action_type="READ_CALLER_CONTRACT"`, `next_action_file="src/auth/login.py"`. Agent's next action is `edit_file("reproduce_bug.py")` (editing a scaffold file unrelated to the suggested caller).
**Action:** Run `compute_follow_type()`.
**Assert:** `follow_type == "IGNORED"`.
**Why:** The agent is doing something completely unrelated to the structural evidence. This is the signal that triggers L5 `ignored_next_action` detection (item 4).

### Test 4: insufficient trace --> NOT_MEASURABLE

**Setup:** L3 event with `next_action_type="READ_CALLER_CONTRACT"`, `next_action_file="src/auth/login.py"`. Trajectory has no subsequent actions (task ended immediately after L3 fired, e.g., max_iter reached).
**Action:** Run `compute_follow_type()`.
**Assert:** `follow_type == "NOT_MEASURABLE"`.
**Why:** If the trajectory ends before the agent could act, the reaction is not measurable. This must not be classified as `IGNORED` (which would false-trigger L5).

### Test 5: every next_action has exactly one reaction

**Setup:** 5 L3 events with various `next_action_type` values. Trajectory with 20 actions.
**Action:** Run `join_gt_to_agent()`.
**Assert:** Exactly 5 reaction records produced. Each reaction has a unique `gt_event_id` matching one of the 5 L3 events. No duplicates. No missing.
**Why:** The 1:1 invariant between next_action events and reaction records is critical for all downstream metrics (follow rates, ignore rates). If an event produces 0 or 2 reactions, the rates are wrong.

---

## L5 Tests: Structural detection patterns

All tests target the governor extensions from implementation plan item 4. Test fixture: `L5TrajectoryState` with configurable `structural_witness_count` and iteration history.

### Test 1: ignored structural witness fires

**Setup:** L3 emitted `next_action_type="READ_CALLER_CONTRACT"` on iteration 5. Reaction joiner classified it as `IGNORED` (agent edited an unrelated file on iteration 6). `GT_L5_STRUCTURAL_UNVERIFIED=1`.
**Action:** Call `governor.after_interaction()` with the iteration-6 action.
**Assert:** Governor fires `ignored_next_action` detection. L5 event has `event_type="ignored_next_action"`, `parent_event_id` links to the L3 event.
**Why:** This is the core new detection. The agent received structural evidence (a caller to read) and ignored it. The governor should catch this pattern.

### Test 2: broad-only does not fire structural_unverified_patch

**Setup:** Agent has run 3 broad test commands (`pytest tests/`) but opened 0 caller/consumer files. Reaction classifications are all `FOLLOWED_BROAD_ONLY`. `structural_witness_count == 0`. Iteration ratio = 0.90 (FINALIZATION). Agent calls finish.
**Action:** Call `governor._handle_finish()`.
**Assert:** `structural_unverified_patch` fires (agent never verified structurally, only ran broad tests).
**Variant:** `structural_witness_count == 1` (agent opened 1 caller file at some point). Assert: `structural_unverified_patch` does NOT fire.
**Why:** Broad testing is not structural verification. The agent may pass all tests but still violate a caller contract. `structural_unverified_patch` catches this.

### Test 3: collapsed diff triggers no false L5

**Setup:** Agent's diff is empty (behavior_class=collapsed). Agent calls finish. `structural_witness_count == 0`.
**Action:** Call `governor._handle_finish()`.
**Assert:** `structural_unverified_patch` fires (correct: 0 witnesses). The HYGIENE layer detects the collapse separately (item 6). L5 and HYGIENE both fire but with different event_types.
**Why:** A collapsed diff is the worst outcome. L5 should still detect the structural verification gap. HYGIENE detects the empty diff. Both signals are valid and non-redundant.

### Test 4: no failing tests needed for structural detection

**Setup:** All test commands passed. Agent opened 0 caller/consumer files. `structural_witness_count == 0`. No `repeated_failure_count`.
**Action:** Call `governor._handle_finish()` at FINALIZATION band.
**Assert:** `structural_unverified_patch` fires. It does NOT require test failures as a precondition.
**Why:** The existing `unverified_patch` hook (hooks.py line 155) checks for "broad tests pass but no targeted test run." The new `structural_unverified_patch` is orthogonal: it checks "no structural witnesses read." Both can fire independently. Test pass/fail is irrelevant to structural verification.

---

## L5b Tests: One-action structural interventions

All tests target the new hook functions from implementation plan item 5.

### Test 1: one action only

**Setup:** `hook_ignored_next_action()` called with `file="src/auth/login.py"`, `caller_count=3`.
**Action:** Render the hook message.
**Assert:** Output is exactly 1 line. No bullet points, no multi-paragraph advice. Contains the file path and a directive verb ("Read", "Check", "Verify").
**Why:** Existing hooks (e.g., `hook_hypothesis_falsified`) are multi-line with structured sections (Iteration, Evidence, Next action). The structural interventions must be single-line because they name one specific action, not a diagnosis.

### Test 2: no restart language

**Setup:** All possible inputs to `hook_ignored_next_action()` and `hook_structural_unverified_patch()`.
**Action:** Render both hook messages for 10 different file paths and caller counts.
**Assert:** No output contains "start over", "restart", "begin again", "try a different approach", "start from scratch" or any phrase in the L5bSafetyChecker blocklist (hooks.py lines 253-255).
**Why:** The L5bSafetyChecker blocks messages with restart language. If the hook templates accidentally contain such phrases, the intervention is silently suppressed and the agent never sees it. This test ensures the templates are clean by construction, not by safety-checker rescue.

### Test 3: safety checker passes in production path

**Setup:** `hook_ignored_next_action()` output for a file path with 150 characters (long path).
**Action:** Pass the output through `L5bSafetyChecker.check()`.
**Assert:** Returns `True` (message allowed). Token estimate of the message is under 180 (the `_MAX_L5_TOKENS` cap).
**Variant:** File path with 500 characters (pathologically long). Assert: message is truncated or checker returns `False` with `suppression_reason="exceeds_token_cap"`.
**Why:** The safety checker is the last gate before emission. This test verifies the hooks produce messages that survive the checker under normal and edge-case inputs.

### Test 4: blocked message not appended to agent observation

**Setup:** `L5bSafetyChecker.check()` returns `False` for a message (e.g., because it exceeds 180 tokens).
**Action:** Wrapper processes the L5b decision.
**Assert:** The wrapper emits a `GTLayerEvent` with `layer="L5b"`, `suppressed=True`, `suppression_reason` populated. The wrapper does NOT append the message to the agent's observation. The `agent_action_after` field on the interaction log entry is NOT influenced by a blocked message.
**Why:** Audit section L5b.4 documents this behavior (wrapper lines 1694-1700). The test verifies the wrapper correctly gates on safety-checker output. A blocked message that leaks into the observation is a bug.

---

## 5-Smoke Pass Gates

These are the acceptance criteria for a 5-task smoke test after implementing Phases 1-3. All must pass before scaling to the frozen 30-task validation set.

### Gate 1: next_action > 0 even with 0 test edges

**Check:** Across all 5 tasks, count L3 events where `next_action_type` is non-empty.
**Pass:** Count > 0.
**Why:** The structural hierarchy (item 1) should populate `next_action_type` from callers/consumers/signatures, not just tests. If the graph has edges but no test files, `next_action_type` must still be populated. A count of 0 means the hierarchy is not firing.

### Gate 2: structural reactions produced

**Check:** Across all 5 tasks, count reaction records with `structural_follow=True`.
**Pass:** Count > 0.
**Why:** Item 3 extends the reaction joiner to classify structural follow-through. If 0 structural reactions are produced, either: (a) no L3 events had `next_action_type` populated (Gate 1 would also fail), or (b) the joiner extension is not working.

### Gate 3: L3b chars reduced

**Check:** Compute average L3b chars/fire across all 5 tasks.
**Pass:** Average <= 600 chars (from current 1810, a 3x reduction minimum).
**Why:** Item 2 introduces primary-edge selection. If average chars/fire is still above 600, the pruning is not aggressive enough or is not activating.

### Gate 4: no restart language

**Check:** Grep all L5b messages emitted across all 5 tasks for restart-language phrases.
**Pass:** 0 matches.
**Why:** Item 5 specifies single-line interventions without restart language. This gate catches template bugs that the unit tests (L5b Test 2) should also catch, but verified in a real smoke context.

### Gate 5: all event_ids present

**Check:** Every GTLayerEvent in the JSONL output has a non-null, unique `event_id`.
**Pass:** 0 events with null or duplicate `event_id`.
**Why:** The reaction joiner joins on `event_id`. If any event lacks an ID, the join is broken and reactions are lost. This is a data-integrity gate, not a behavioral gate.

### Gate 6: every next_action has a reaction

**Check:** Count L3/L3b/L5b events with non-empty `next_action_type`. Count reaction records. The two counts must be equal.
**Pass:** `event_count == reaction_count` (1:1 invariant).
**Why:** Reaction Test 5 verifies this in unit tests. The smoke gate verifies it end-to-end with real trajectories. Any mismatch means the joiner is dropping events or double-counting.

---

## Test Infrastructure Notes

### Fixture: minimal graph.db

All L3, L3b, L5, and L8 tests use a shared `create_test_graph()` helper that builds an in-memory SQLite database matching the graph.db schema (nodes + edges tables, v16+ with assertions). The fixture contains:

- 5 source files (A, B, C, D, H) with 2-3 functions each
- 1 test file (T) with 2 test functions
- 10 edges: A->B (CALLS), B->C (CALLS), T->A (CALLS, is_test=1), A->D (IMPORTS), etc.
- H is a hub file with 20 incoming edges
- Confidence values: same_file=1.0, import=1.0, name_match with varying candidates (0.2-0.9)
- 3 assertions in the assertions table (T file)

### Fixture: synthetic trajectory

All reaction tests use a `create_test_trajectory()` helper that builds a list of action dicts matching the OpenHands action schema:
- `{"action": "read_file", "args": {"path": "src/auth/login.py"}, "iteration": 6}`
- `{"action": "edit_file", "args": {"path": "reproduce_bug.py"}, "iteration": 6}`
- `{"action": "run_command", "args": {"command": "pytest tests/"}, "iteration": 6}`

### Fixture: synthetic GTLayerEvent list

All reaction tests use a `create_test_events()` helper that builds a list of GTLayerEvent dicts:
- `{"event_id": "evt_001", "layer": "L3", "iteration": 5, "next_action_type": "READ_CALLER_CONTRACT", "next_action_file": "src/auth/login.py"}`

### Running tests

```bash
pytest tests/test_structural_next_action.py -v      # L3 tests (item 1)
pytest tests/test_l3b_primary_edge.py -v             # L3b tests (item 2)
pytest tests/test_reaction_structural.py -v          # Reaction tests (item 3)
pytest tests/test_l5_structural_detection.py -v      # L5 tests (item 4)
pytest tests/test_l5b_structural_intervention.py -v  # L5b tests (item 5)
pytest tests/ -k "structural" -v                     # All structural tests
```

All tests must pass with `GT_STRUCTURAL_NEXT_ACTION=1`, `GT_L3B_PRIMARY_EDGE=1`, `GT_L5_STRUCTURAL_UNVERIFIED=1` set as environment variables. All tests must also pass with all flags set to 0 (verifying no regression to existing behavior).
