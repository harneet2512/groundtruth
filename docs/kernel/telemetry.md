# Kernel telemetry

All kernel telemetry is appended to `gt_runtime_telemetry.jsonl` in `GT_LOG_DIR` via `runtime.telemetry.append_block` (`src/groundtruth/runtime/telemetry.py:22`). The kernel does not invent a new transport. `control/decision_log.py` is a thin wrapper that takes a `KernelEvent` and routes it through `append_block` with the block name `gt_kernel_decision`.

Each line is a JSON object with the stable envelope `{timestamp, task_id, block, <block>: data}`.

## `gt_kernel_decision` schema

Canonical record. Implements the Decision Trace 7-element schema (see ADR 0003).

```json
{
  "timestamp": "2026-04-30T14:22:11Z",
  "task_id": "django__django-11099",
  "block": "gt_kernel_decision",
  "gt_kernel_decision": {
    "triggering_state": {
      "scaffold": "openhands",
      "event_kind": "pre_tool",
      "tool": "EDIT",
      "edit_index": 0
    },
    "context_evaluated": {
      "evidence": {
        "node_ids": [4421, 4423],
        "edge_ids": [11890, 11891],
        "candidates_top3": [
          {"path": "django/contrib/auth/models.py", "score": 0.91},
          {"path": "django/contrib/auth/__init__.py", "score": 0.62},
          {"path": "tests/auth_tests/test_models.py", "score": 0.58}
        ],
        "patch_shape": null,
        "rule_inputs": {"first_edit_target": "reproduce_bug.py"}
      },
      "provenance": {
        "graph_db_sha": "9c4f...",
        "plan_path": "/tmp/gt_plans/django__django-11099.json",
        "confidence_components": {
          "localization": 0.71,
          "drift": 0.4,
          "graph_validation": 1.0
        },
        "error_class": null
      }
    },
    "policy_applied": {
      "rule_id": "first_edit_root_scaffold",
      "rule_version": "kernel-0.1"
    },
    "alternatives_considered": [
      {"action": "visible", "rejected_because": "confidence>=0.6 and capabilities.block=true"},
      {"action": "audit",   "rejected_because": "drift_signal_present"}
    ],
    "confidence": 0.71,
    "action_selected": "block",
    "authority_exercised": {
      "adapter": "openhands",
      "actual_action": "block",
      "degraded_from": null
    }
  }
}
```

### Field-level contract

| Field | Type | Required | Notes |
|---|---|---|---|
| `triggering_state.scaffold` | string | yes | Adapter name, e.g. `"openhands"`, `"swe_agent"`. |
| `triggering_state.event_kind` | string | yes | One of `pre_tool`, `post_edit`, `drift_check`, `validate`, `pull`. |
| `triggering_state.tool` | string \| null | no | Adapter-normalized tool name. Null for `drift_check` / `validate`. |
| `triggering_state.edit_index` | int \| null | no | Zero-based count of edits seen so far in the run. |
| `context_evaluated.evidence` | object | yes | `Evidence` payload; never null, may have empty lists. |
| `context_evaluated.provenance.graph_db_sha` | string | yes | Hash of the `graph.db` content used. |
| `context_evaluated.provenance.plan_path` | string \| null | yes | Plan file path or null if no plan was loaded. |
| `context_evaluated.provenance.confidence_components` | object | yes | `{localization, drift, graph_validation}` floats in `[0, 1]`. |
| `context_evaluated.provenance.error_class` | string \| null | yes | See `error_class` enum below. Null on the success path. |
| `policy_applied.rule_id` | string | yes | Stable identifier, e.g. `first_edit_missed_focus`. |
| `policy_applied.rule_version` | string | yes | `kernel-X.Y`. |
| `alternatives_considered` | array | yes | At least one entry when an action other than `audit` is selected. |
| `confidence` | float | yes | Range `[0, 1]`. Composition formula in `docs/kernel/API.md`. |
| `action_selected` | string | yes | One of `allow`, `block`, `visible`, `audit`. |
| `authority_exercised.adapter` | string | yes | Same as `triggering_state.scaffold`. |
| `authority_exercised.actual_action` | string | yes | What the adapter actually did after capability check. |
| `authority_exercised.degraded_from` | string \| null | yes | Set iff capability degradation occurred (e.g. `block` -> `visible`). |

## `gt_pull` schema

Emitted on every successful or failed `kernel.pull(...)` call.

```json
{
  "timestamp": "2026-04-30T14:23:08Z",
  "task_id": "django__django-11099",
  "block": "gt_pull",
  "gt_pull": {
    "kind": "trace",
    "args": {"symbol": "User.has_perm"},
    "latency_ms": 14,
    "response_shape": {"node_count": 1, "caller_count": 6, "evidence_node_ids": 7},
    "telemetry_record_id": "01J0...",
    "error_class": null
  }
}
```

| Field | Notes |
|---|---|
| `kind` | One of `trace`, `impact`, `hotspots`, `validate`, `context`, `symbols`. |
| `args` | Same `dict` passed to `PullQuery.args`. |
| `latency_ms` | Wall clock of the routed handler call. |
| `response_shape` | Compact summary; not the full payload. The full payload is held in `PullResult.payload` and not duplicated here to keep telemetry small. |
| `telemetry_record_id` | Stable id correlating to `PullResult.telemetry_record_id`. |
| `error_class` | Null on success; otherwise one of the values below. |

## `error_class` enum

Adopted from the Cursor harness blog (Apr 30 2026). Used in `gt_kernel_decision.context_evaluated.provenance.error_class` and `gt_pull.error_class`. Surfaces in any KernelEvent that took the error path.

| Value | Meaning |
|---|---|
| `InvalidArguments` | Caller passed malformed inputs (bad path, wrong type, missing required field). Not the agent's fault per se -- the harness made a bad call. |
| `UnexpectedEnvironment` | Repo state, file system, or `graph.db` was not in a state the kernel can act on (missing file, stale graph, no plan). |
| `ProviderError` | An underlying provider raised (SQLite error, MCP handler crash, telemetry write failure). |
| `UserAborted` | Run was interrupted by the user / harness terminated the task. Distinguishable from a true error. |
| `Timeout` | A handler exceeded its timeout budget. |
| `Unknown` | Fallback. Per Cursor's framing, an `Unknown` classification = harness/adapter bug. Alertable separately from agent mistakes. |

## Block taxonomy

| Block | Source | Purpose | Status |
|---|---|---|---|
| `gt_brief_gen` | `kernel.brief` | Brief generation provenance | existing |
| `gt_patch_shape` | `kernel.observe_edit` | Patch audit output | existing |
| `gt_replan` | `kernel.replan` | Replan trigger evaluation | existing |
| `gt_usable_delivery` | adapter (existing) | Was the brief actually delivered | existing |
| `gt_kernel_decision` | `kernel.decide_pre_tool`, `kernel.detect_drift`, `kernel.validate_against_graph` | Decision Trace 7-element record | new (Phase 1) |
| `gt_pull` | `kernel.pull` | Mid-task pull record | new (Phase 1) |

The three-stream parity (AgentTrace mapping) is unchanged: `gt_runtime_telemetry.jsonl` is operational + cognitive; `gt_hook_telemetry.jsonl` (steering events) is contextual; `gt_report.csv` is reconciliation output. Resolves Open Question 2 in the handoff: the canonical kernel fields are everything inside `gt_kernel_decision`. Anything scaffold-specific stays inside the adapter's own logging surface.

## `verify_report.py` extensions

Add five new report-only gates to `scripts/swebench/verify_report.py:432`. The existing 13 strict-conjunctive gates stay unchanged; the new gates render in the run section but do not flip the PASS/FAIL verdict until calibration is established.

| Gate | What it measures | Source | Threshold (Phase 1) |
|---|---|---|---|
| `decision_block_rate` | Fraction of tasks where the kernel issued at least one `block`. Sanity ceiling. | Count of `gt_kernel_decision` with `action_selected="block"` per task / task count. | report-only; expected `<=0.30`. |
| `degraded_capability_rate` | Fraction of tasks where the adapter degraded a `block` to a lower action. | Count of `authority_exercised.degraded_from != null` per task / task count. | report-only; expected `0.0` for OH after Phase 1. Non-zero means an adapter capability assumption broke. |
| `drift_signal_to_replan_rate` | Fraction of tasks where a non-empty `DriftSignals` produced an actual `Replan` with stage != `stay_course`. | Cross-correlate `gt_kernel_decision` (drift_check) and `gt_replan` per task. | report-only; expected `>=0.50` (sanity floor). |
| `gt_keep_rate` | Fraction of GT-recommended files retained in the final patch (Cursor-style keep rate). | Final patch file list intersected with `BriefResult.candidates` per task. | report-only; baseline TBD by Phase 1 evidence. |
| `pull_error_rate_per_tool` | Per-`PullKind` error rate. Reliability SLO target is 2-3 nines (Cursor harness blog). | `gt_pull.error_class != null` count / total `gt_pull` for each `kind`. | report-only; expected `<=0.01` per kind. |

The `gt_keep_rate` and `pull_error_rate_per_tool` gates are added per Cursor harness alignment (`future_plan.md` §F.1).

Threshold overrides per-run via env: existing pattern uses `VERIFY_MIN_*` vars; new gates follow the same convention (`VERIFY_MAX_DECISION_BLOCK_RATE`, `VERIFY_MAX_DEGRADED_CAPABILITY_RATE`, `VERIFY_MIN_DRIFT_REPLAN_RATE`, `VERIFY_MIN_GT_KEEP_RATE`, `VERIFY_MAX_PULL_ERROR_RATE`). Implementation lands in Phase 1.
