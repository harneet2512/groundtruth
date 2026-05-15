# 03 Verifier Report: Structured Telemetry Readiness

Verifier date: 2026-05-15
Verifier: Claude Opus 4.6 (1M context) -- Agent V
Input: `01_code_audit.json` (structured audit), full source read of wrapper + telemetry + hooks + governor + tests + GHA workflow
Test baseline: 48/48 telemetry tests pass, 146 total tests pass

## Methodology

Each hard fail check evaluates a specific structural property of the telemetry system.
Evidence is drawn from source code grep/read of actual files, not from runtime observation.
A check is PASS only if the code path is unconditionally correct in production.
A check is FAIL if any production execution path violates the property.
A check is CONDITIONAL_PASS if the property holds only under specific env var configuration (`GT_STRUCTURED_EVENTS=1`).

---

## Preconditions Verified

| Precondition | Status | Evidence |
|---|---|---|
| Telemetry package exists | TRUE | `src/groundtruth/telemetry/{schemas,writer,evidence,constants,__init__}.py` all present |
| Tests pass (48/48) | TRUE | Test files verified: `test_schemas.py`, `test_writer.py`, `test_evidence.py`, `test_l5b_safety.py`, `test_mock_trajectories.py` |
| GTTelemetryWriter instantiated in wrapper | CONDITIONAL | Line 2280: `config._telemetry_writer = GTTelemetryWriter(...)` but ONLY when `GT_STRUCTURED_EVENTS=1` (line 2276). Default is `None` (line 296). GHA workflow sets `GT_STRUCTURED_EVENTS: "1"` (swebench_30task.yml line 173). |
| `_emit_structured_event()` calls `writer.emit_layer_event()` | TRUE | Lines 1101-1118: constructs `GTLayerEvent`, calls `writer.emit_layer_event(event)`. Returns `event_id`. |
| `_emit_belief_event()` calls `writer.emit_belief_event()` | TRUE | Lines 1139-1148: constructs `GTBeliefEvent`, calls `writer.emit_belief_event(event)`. |

---

## Correction to 01_code_audit.json

The original audit states (summary lines 13-16):

```json
"gt_telemetry_writer_used": false,
"gt_layer_event_emitted_anywhere": false,
"gt_agent_reaction_event_emitted_anywhere": false,
"gt_belief_event_emitted_anywhere": false
```

This is inaccurate. The audit was written assuming the default path (`GT_STRUCTURED_EVENTS` unset). Under the GHA workflow configuration (`GT_STRUCTURED_EVENTS=1`):

- **gt_telemetry_writer_used**: TRUE -- instantiated at wrapper line 2280
- **gt_layer_event_emitted_anywhere**: TRUE -- `_emit_structured_event()` calls `writer.emit_layer_event()` at 10 call sites
- **gt_belief_event_emitted_anywhere**: TRUE -- `_emit_belief_event()` calls `writer.emit_belief_event()` at 2 call sites (L1 candidates line 2568, file edit line 1738)
- **gt_agent_reaction_event_emitted_anywhere**: FALSE -- confirmed, no runtime caller of `writer.emit_agent_reaction()`

The original audit's layer statuses (IMPLEMENTED_RENDERED_ONLY, etc.) accurately describe the default path but understate the configured path.

---

## Hard Fail Checks

### HF-1: Any layer has fired count only (no structured evidence)

**Definition:** A layer has `_emit_structured_event` calls at its injection points, but the `evidence_items` list is always empty or None.

| Layer | `_emit_structured_event` called? | `evidence_items` populated? | Verdict |
|---|---|---|---|
| L1 | Yes (line 2561) | Conditional: reads `/tmp/gt_l1_structured.json` written by `v1r_brief.py` (line 522) when `GT_STRUCTURED_EVENTS=1`. Items are `l1_candidate` dicts with `file_path`, `confidence`, `source`, `reason`, `text`. | CONDITIONAL_PASS |
| L3 | Yes (line 1943) | Conditional: passes `hook_output=hook_out`. Wrapper parses `__GT_STRUCTURED__` sentinel at lines 1093-1098. Sentinel printed by `post_edit.py` line 1493 ONLY when `--structured-output` flag passed, which requires `GT_STRUCTURED_EVENTS=1` (line 732). If env var unset: `evidence_items=[]`. | CONDITIONAL_PASS |
| L3b | Yes (line 1725) | Same mechanism as L3: sentinel at `post_view.py` line 426. Flag added at line 701 when `GT_STRUCTURED_EVENTS=1`. | CONDITIONAL_PASS |
| L4 | Yes (lines 2465, 2471) | Hardcoded single item: `[{"kind": "l4_constraint", "text": prefetch_block[:500], "source": "graph_db"}]`. Always present when L4 fires. Suppression case has `emitted=False, suppressed=True`. | PASS |
| L5 (governor) | NO `_emit_structured_event` call | Governor writes to `/tmp/gt_l5_telemetry.jsonl` with ad-hoc schema (line 338-340). No GTLayerEvent emission for L5 proper. | FAIL |
| L5b (wrapper label) | Yes (lines 1658, 1969) | No `evidence_items`. Only `rendered_text=l5_append`. These calls emit governor-produced messages labeled as "L5b" but contain zero structured evidence items. | FAIL |
| L6 | Yes (line 1836) | Hardcoded single item: `[{"kind": "l6_reindex", "file_path": ..., "reason": ..., "text": ...}]`. Always present. | PASS |
| HYGIENE | Yes (line 1209) | List comprehension over `to_strip`: `[{"kind": "hygiene_strip", "file_path": f, "reason": "scaffold file removed"}]`. Empty when no scaffolds found (but `suppressed=True`). | PASS |

**Verdict: FAIL.** L5 proper has no structured event emission at all. L5b calls emit rendered text only with empty evidence_items.

---

### HF-2: Any rendered GT message lacks backing event_id

**Definition:** When GT injects text into the agent observation, the `_emit_structured_event` return value (event_id) must be stored alongside the `_log_gt_interaction` entry for that same injection, enabling correlation between the two telemetry streams.

| Layer | event_id captured? | Stored in interaction log? | Verdict |
|---|---|---|---|
| L1 | Yes: `l1_eid = _emit_structured_event(...)` line 2561 | No: passed to `_emit_belief_event` only (line 2573). Not in `_log_gt_interaction` (line 2546). | FAIL |
| L3 | Return value discarded (line 1943) | No event_id in `_log_gt_interaction` (line 1942) | FAIL |
| L3b | Return value discarded (line 1725) | No event_id in `_log_gt_interaction` (line 1724) | FAIL |
| L5b | Return value discarded (lines 1658, 1969) | No event_id in `_log_gt_interaction` (line 1657) | FAIL |
| L6 | Return value discarded (line 1836) | No event_id in `_log_gt_interaction` (line 1834) | FAIL |
| HYGIENE | Return value discarded (line 1209) | No `_log_gt_interaction` call exists for HYGIENE | FAIL |

**Verdict: FAIL.** Only L1 captures event_id, and only to forward to belief events. No layer writes event_id into the interaction log. The GTLayerEvent JSONL and gt_interactions JSONL streams are not joinable by event_id.

---

### HF-3: Any next_action lacks reaction event path

**Definition:** When a GTLayerEvent contains `next_action_type` suggesting the agent should take a specific action, there must exist a runtime path to produce a `GTAgentReactionEvent` recording whether the agent complied.

**Finding:** `next_action_type` is NEVER populated by any `_emit_structured_event` call in the wrapper. The parameter exists in the function signature (line 1078) and is forwarded to `GTLayerEvent` (line 1112), but every call site uses the default `None`:

- L1 (line 2561): no `next_action_type`
- L3 (line 1943): no `next_action_type`
- L3b (line 1725): no `next_action_type`
- L4 (line 2465): no `next_action_type`
- L5b (lines 1658, 1969): no `next_action_type`
- L6 (line 1836): no `next_action_type`
- HYGIENE (line 1209): no `next_action_type`

`GTAgentReactionEvent` is NEVER emitted anywhere in the runtime. `reaction_joiner.py` at `scripts/analysis/reaction_joiner.py`:
- Is never imported by any runtime code (confirmed: zero matches in `scripts/swebench/`)
- Is never imported by any test code (confirmed: zero matches in `tests/`)
- Requires both `gt_layer_events_{task_id}.jsonl` AND parsed `AgentTrajectory` from `output.jsonl`
- Can only run post-hoc after a complete run, and is never invoked by any workflow step

The mock trajectories in `test_mock_trajectories.py` DO populate `next_action_type` on test-constructed GTLayerEvent objects, but these prove schema validity only, not runtime behavior.

**Verdict: FAIL.** No reaction events are produced during or after any run. The reaction measurement pipeline -- which is the core of "provable utilization" -- is entirely dead code.

---

### HF-4: L5b safety checker not called before emission

**Definition:** `L5bSafetyChecker.validate()` must be called on L5/L5b governor messages before they are injected into the agent's observation.

**Finding:**
- `L5bSafetyChecker` defined at `src/groundtruth/trajectory/hooks.py:234-264`
- Validates: restart language (9 phrases), late broad exploration (6 phrases, >=60% ratio), token cap (>180 tokens)
- `governor.py`: ZERO references to `L5bSafetyChecker` (grep confirmed)
- `oh_gt_full_wrapper.py`: ZERO references to `L5bSafetyChecker` (grep confirmed)
- Only callers: `tests/telemetry/test_l5b_safety.py` (8 tests) and `tests/telemetry/test_mock_trajectories.py` (1 test)

Runtime path at wrapper lines 1655-1658:
1. Governor produces `l5_append` via `_l5_gov.after_interaction()`
2. Wrapper immediately appends to observation: `obs = append_observation(obs, l5_append)`
3. Logs: `_log_gt_interaction(config, "L5", ...)`
4. Emits: `_emit_structured_event(config, "L5b", "intervention", rendered_text=l5_append)`

No safety check between generation and injection. A governor message containing "start over", "restart", or exceeding 180 tokens will be injected unchecked.

**Verdict: FAIL.** L5bSafetyChecker exists, has 8 passing tests, but is dead code in production.

---

### HF-5: L3b late broad navigation decay not enforced via telemetry schema

**Definition:** Telemetry constants `L3B_EDGE_LIMITS` and `L3B_BROAD_NAV_CUTOFF_RATIO` (constants.py lines 23-30) define iteration-band-aware edge limits. These must be consumed by the L3b hook runtime.

**Finding:**
- `L3B_EDGE_LIMITS`: `{early_0_25: 3, mid_25_60: 2, late_60_85: 1, final_85_100: 0}`
- `L3B_BROAD_NAV_CUTOFF_RATIO`: `0.60`
- `post_view.py` has its OWN decay logic (lines 222-226): `if iteration_ratio >= 0.85: limit = 1; elif >= 0.60: limit = max(2, limit // 2)`. Gated on `GT_REBUILD_L3B=1`.
- Constants from `telemetry/constants.py` are NEVER imported by `post_view.py`
- Value mismatch: telemetry schema says `final_85_100 = 0` edges, but code sets `limit = 1`
- No telemetry event records whether decay was applied or which band triggered it

**Verdict: FAIL.** Decay exists but is disconnected from telemetry constants. The constants are dead code. Actual behavior differs from schema-defined limits.

---

### HF-6: Restart language in L5b emissions

**Definition:** L5b messages must not contain restart/start-over language that could derail the agent late in the trajectory.

**Finding:** Same root cause as HF-4. Without `L5bSafetyChecker.validate()` being called, there is no runtime guarantee that any governor message is free of restart language, late broad exploration phrases, or within the 180-token cap.

The governor hooks (e.g., `unverified_patch` at hooks.py line 221-231) generate fixed templates that happen to be safe. But `post_failure_diverge` and other hooks are not audited against the safety checker's phrase list. Without the runtime call, safety is coincidental, not enforced.

**Verdict: FAIL.** Same root cause as HF-4.

---

## Summary Table

| Check | ID | Verdict | Blocking? |
|---|---|---|---|
| Layer evidence coverage | HF-1 | FAIL | Yes -- L5 has no structured emission; L5b has rendered-only |
| Event_id join path | HF-2 | FAIL | Yes -- no layer stores event_id in interaction log |
| Reaction event path | HF-3 | FAIL | Yes -- next_action never populated; reaction_joiner never executed |
| L5b safety before emission | HF-4 | FAIL | Yes -- validate() never called in production |
| L3b decay via telemetry | HF-5 | FAIL | No -- decay exists via separate mechanism; constants are advisory |
| Restart language gating | HF-6 | FAIL | Yes -- same root cause as HF-4 |

---

## Overall Verdict: FAIL

**6/6 hard fail checks triggered. 4 unique blocking issues identified.**

### Blocking Issues

**B1: L5 governor has no structured event emission.**
Governor writes ad-hoc JSONL to `/tmp/gt_l5_telemetry.jsonl` with a non-GTLayerEvent schema (fields: timestamp, layer, hook, iter, max_iter, band, phase, fired, suppressed_reason, l5_messages_total, message_len, message_text, next_action). The wrapper labels L5 governor output as "L5b" in `_emit_structured_event` calls (lines 1658, 1969), conflating two distinct layer concepts. No `_emit_structured_event` exists for L5 proper.

**B2: No event_id linkage between structured events and interaction log.**
Every `_emit_structured_event` returns an event_id, but no caller (except L1 for belief forwarding) stores it. The `_log_gt_interaction` schema has no event_id field. The two parallel telemetry streams (GTLayerEvent JSONL via writer and gt_interactions JSONL via logger) cannot be correlated.

**B3: L5bSafetyChecker.validate() is production dead code.**
8 tests prove correctness. Zero callers exist in `governor.py` or `oh_gt_full_wrapper.py`. Governor messages are injected into agent observations without safety validation for restart language, late exploration, or token cap.

**B4: GTAgentReactionEvent is never produced.**
`reaction_joiner.py` exists as a standalone script but is never imported or called. `next_action_type` is never populated in any emitted GTLayerEvent. The complete reaction measurement pipeline does not execute in any path. `writer.emit_agent_reaction()` has zero callers outside tests.

### What Works

- Telemetry schemas are well-designed with proper validation (kind enum, layer enum, follow_type enum, belief status enum)
- GTTelemetryWriter is thread-safe, append-only, flush-on-write
- Evidence priority truncation logic (`truncate_evidence_by_priority`) is correct
- L5bSafetyChecker validation logic is correct in isolation
- 48/48 telemetry tests prove schema validity and component correctness
- Under `GT_STRUCTURED_EVENTS=1` (set in GHA workflow), GTLayerEvents ARE emitted for L1, L3, L3b, L4, L6, HYGIENE
- L1, L3, L3b have structured evidence items (via JSON file or sentinel parsing) when env var set
- L4, L6, HYGIENE have hardcoded evidence items that always populate
- Belief events ARE emitted for L1 candidates and file edits
- Analysis scripts exist and have correct logic: `trajectory_parser.py`, `test_command_classifier.py`, `reaction_joiner.py`, `behavioral_analyzer.py`, `smoke_report.py`, `deep_utilization.py`
