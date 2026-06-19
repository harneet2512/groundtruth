# OpenHands Integration Reality

How GT evidence actually reaches (or fails to reach) the agent at runtime.

## The Lifecycle

```
Agent decides action
    ↓
OH controller sets state (RUNNING or FINISHED)
    ↓
OH calls runtime.run_action(action)
    ↓
GT's patched_run_action intercepts
    ↓
orig_run_action(action) → OH processes action → returns obs
    ↓
GT modifies obs (append/prepend evidence)
    ↓
return obs → agent reads it on next step
    ↓
UNLESS state == FINISHED → agent never steps again
```

## Critical Fact

OH's controller sets `AgentState.FINISHED` BEFORE calling `runtime.run_action()` for finish actions. Any content appended to the observation in `patched_run_action` for a finish event is a dead write — the agent never reads it.

Evidence: oh_gt_full_wrapper.py lines 3013-3024, 3020-3024 (comments documenting this constraint).

## Where GT Can Inject (Agent Will See)

| Injection point | Line | Layer | Agent sees? | Condition |
|-----------------|------|-------|-------------|-----------|
| L1 brief prepend | ~5900 | L1 | YES | Task start, not baseline |
| L5 governor append | 3142 | L5 | YES | Scaffold detected, not finish |
| L5 Goku append | 3186 | L5 | YES | Goku fires, not finish |
| Grep intercept append | 3277/3316 | Grep | YES | grep command, not baseline |
| L6 early review append | 4356 | L6 | YES | edit_count >= 1, graph has callers |
| L3b/L4a via _deliver_or_trace | 4760 | L3/L3b/L4a | YES | Evidence passes marker check |
| Consensus scope | ~3466 | Consensus | YES | Candidate file viewed before edits |

## Where GT Injects But Agent Never Sees (Dead Writes)

| Injection point | Line | Layer | Why dead |
|-----------------|------|-------|----------|
| L5 finish handler | 4789 | L5 | state=FINISHED before run_action |
| L5b Goku finish | 4831 | L5b | state=FINISHED before run_action |
| L6 pre-submit finish | 4950 | L6 | state=FINISHED before run_action |

These are now marked `emitted=False, suppressed=True, suppression_reason="finish_handler_dead_write"` (BUG-001 fix).

## Stuck Detector Interaction

At line 3067-3091: if the same (action_class+action_text, md5(raw_obs[:8000])) pair appears in the last 8 entries, ALL GT injection is skipped. Early return at line 3091.

Exceptions:
- FinishAction never skipped (checked at line 3067)
- Baseline never triggers stuck skip
- Event classification and telemetry still run

## Baseline Gate

`_GT_BASELINE = os.environ.get("GT_BASELINE", "0") == "1"` (line 5686)

Gates: L5 governor, L5 Goku, grep intercept, L6 pre-submit, stuck compat logic.
Does NOT gate: L1 brief, L3 post-edit, L3b post-view (these run regardless).

## Delivery Ledger: _deliver_or_trace()

Defined at lines 1281-1327. Three outcomes:
1. **ROUTER_EMIT_HOOK_EMPTY** — payload empty, obs unmodified
2. **ROUTER_EMIT_MARKER_MISMATCH** — payload lacks GT markers, obs unmodified
3. **DELIVERED** — payload appended/prepended, `agent_visible=true` logged

Does NOT distinguish DEAD_WRITE at this level — that's tracked separately in `_emit_structured_event()`.

## Observation Types

`append_observation(obs, text)` and `prepend_observation(obs, text)` modify the observation object. The observation is whatever OH's `orig_run_action()` returned — typically CmdOutputObservation, FileEditObservation, or NullObservation.

Agent sees the full observation text on its next step, including GT-appended content.

## Hidden Prefix Filtering

`_is_hidden_line()` (line 64-67) filters lines starting with `[GT_META]`, `[GT_STATUS]`, `[GT_TRACE]`, `[GT_DELIVERY]`, etc. from the observation text. This ensures telemetry lines don't reach the agent.

## Action Classification

`classify_tool_event(action)` at line 788-811 classifies actions into:
- `post_edit` — file editor edit
- `post_view` — file editor view
- `finish` — AgentFinishAction/FinishAction
- `skip` — commands, navigation, etc.

This determines which GT layer fires.

## Main Dispatch Structure

```
patched_run_action(action)
  ├── pre-processing (action tracking, telemetry)
  ├── obs = orig_run_action(action)
  ├── stuck compat check → early return if repeated
  ├── event = classify_tool_event(action)
  ├── if event.kind != "finish":
  │   ├── L5 governor check
  │   ├── Grep intercept
  │   ├── if post_view: L3b + L4a + Consensus
  │   ├── if post_edit: L3 + L6 early review
  │   └── return obs (with GT appended)
  └── if event.kind == "finish":
      ├── L5 finish check (DEAD WRITE)
      ├── L6 pre-submit (DEAD WRITE)
      └── return obs (agent never reads)
```

## Architectural Constraints Derived

1. **GT can only affect agent behavior if it injects into a non-finish observation.** Anything in the finish handler is telemetry only.
2. **Stuck detector can suppress all GT injection.** If GT makes every observation unique (different evidence), the stuck detector becomes blind and the agent may loop.
3. **Baseline gate is incomplete.** L3 evidence still runs on baseline, which may contaminate baseline measurements unless the evidence queries fail gracefully.
4. **Delivery ledger tracks success/failure but not dead writes.** The structured event system handles dead write marking separately.
5. **One wrapper, many paths.** The wrapper is 6139 lines with at least 3 major dispatch branches (post_edit, post_view, finish). Each branch has its own evidence assembly logic.
