# Replay fixture schema

Each fixture lives at `tests/kernel/fixtures/<scenario>/` and contains:

- `input.json` -- a `RunState` plus the function-specific input (e.g. `ToolCall` for `decide_pre_tool`, `Diff` for `validate_against_graph`).
- `expected.json` -- the expected return value (e.g. `Decision`, `Replan`, `ValidationResult`) and, where relevant, the expected `KernelEvent` written by `kernel.log`.

Fixtures must run without `graph.db`. When a fixture covers a function that touches the graph, the input includes a `graph_handle` block with the mocked node/edge data; tests inject this via the `MockGraphHandle` convention defined below.

## Top-level `input.json` schema

```json
{
  "scenario": "<scenario_name>",
  "kernel_function": "decide_pre_tool | detect_drift | validate_against_graph | replan",
  "run_state": { ... },
  "function_input": { ... },
  "graph_handle": { ... | null }
}
```

| Field | Required | Notes |
|---|---|---|
| `scenario` | yes | Must match the directory name. |
| `kernel_function` | yes | Identifies which kernel function the fixture targets. |
| `run_state` | yes | Full `RunState` JSON. |
| `function_input` | yes | The second argument to the kernel function (e.g. `ToolCall`, `Diff`, `ReplanTriggers`). For `detect_drift`, this is null because the function takes only `RunState`. |
| `graph_handle` | conditional | Required when `kernel_function == "validate_against_graph"`. Optional otherwise. |

## `run_state` schema

Mirrors `control.types.RunState` in JSON:

```json
{
  "task_id": "string",
  "plan": {
    "agent_focus_files": ["path/to/focus.py", ...],
    "cluster_files": ["path/to/cluster.py", ...],
    "contracts": [...],
    "constraints": [...]
  },
  "brief_result": {
    "brief_text": "...",
    "candidates": [{"path": "...", "score": 0.91}],
    "focus_files": ["..."],
    "cluster_files": ["..."],
    "contracts": [],
    "constraints": [],
    "confidence": 0.71,
    "plan": {...},
    "plan_path": "/tmp/plan.json"
  },
  "edit_history": [
    {"task_id": "...", "files_changed": ["..."], "diff_text": "...", "ts": "...", "source_tool": "file_editor"}
  ],
  "viewed_files": [],
  "warning_history": [],
  "capabilities": {"block": true, "visible": true, "audit": true, "mid_task_pull": true, "replan_inject": true},
  "model_hint": "claude-sonnet-4.5"
}
```

## `expected.json` schema

```json
{
  "expected_return": { ... },
  "expected_kernel_event": { ... | null }
}
```

`expected_return` matches the function's return type schema (see `docs/kernel/API.md`). `expected_kernel_event` matches the `gt_kernel_decision` schema in `docs/kernel/telemetry.md` and is required when the function emits a kernel event.

For decision fixtures, `expected_return` includes:

```json
{
  "action": "block | visible | audit | allow",
  "rule_id": "first_edit_root_scaffold | first_edit_missed_focus | ...",
  "min_confidence": 0.0,
  "max_confidence": 1.0,
  "reasons_must_include": ["first_edit_root_scaffold"],
  "evidence_must_have_node_ids": false
}
```

Tests assert that `Decision.confidence` falls within `[min_confidence, max_confidence]` to allow for formula refinement without rewriting fixtures.

## Mock graph handle convention

The kernel's `validate_against_graph` takes a `GraphHandle`. In tests, this is a Pydantic-modelled mock that exposes the same query surface as the real handle but reads from inline JSON. Schema:

```json
{
  "graph_handle": {
    "kind": "mock",
    "graph_db_sha": "test-sha-001",
    "nodes": [
      {"id": 4421, "qualified_name": "django.contrib.auth.models.User.has_perm", "file_path": "django/contrib/auth/models.py", "signature": "(self, perm, obj=None)"},
      {"id": 4423, "qualified_name": "django.contrib.auth.models.User", "file_path": "django/contrib/auth/models.py"}
    ],
    "edges": [
      {"id": 11890, "source_id": 4421, "target_id": 4423, "type": "CALLS", "resolution_method": "same_file", "confidence": 1.0}
    ],
    "callers_of": {
      "django.contrib.auth.models.User.has_perm": [
        {"qualified_name": "django.views.auth.login_required", "file_path": "django/views/auth.py", "line": 42}
      ]
    }
  }
}
```

The `MockGraphHandle` Pydantic class is defined in `tests/kernel/conftest.py` (Phase 1). Fixtures cite it by passing `graph_handle.kind = "mock"`.

## Run-without-graph rule

Tests that don't target `validate_against_graph` must run with `graph_handle = null` in the fixture. The kernel never opens a real `graph.db` during unit tests. `pull` fixtures use a pre-baked `PullResult` payload as if the MCP handler had already responded -- this is what `tests/kernel/fixtures/mid_task_pull_trace/` does.

## Six committed scenarios (Phase 0 list)

| Scenario | Kernel function | Notes |
|---|---|---|
| `first_edit_root_scaffold` | `decide_pre_tool` | First edit creates a root-level repro scaffold (`reproduce_bug.py`). Expect `block`, `rule_id="first_edit_root_scaffold"`. |
| `first_edit_misses_focus_high_confidence` | `decide_pre_tool` | Brief confidence is 0.8; first edit hits a non-focus file. Expect `block`, `rule_id="first_edit_missed_focus"`. |
| `first_edit_misses_focus_low_confidence` | `decide_pre_tool` | Same input but brief confidence is 0.4. Expect `visible`, NOT `block` (confidence-gated). |
| `cluster_drift_after_three` | `detect_drift` | Third edit moves outside the cluster. Expect `DriftSignals.edits_outside_cluster_count >= 1`. |
| `graph_validation_breaks_signature` | `validate_against_graph` | Diff changes a function signature; mock graph shows 4 callers. Expect `ValidationResult.ok=False`, `broken_signatures` populated. |
| `mid_task_pull_trace` | `pull` | `PullQuery(kind=trace, args={"symbol": "foo"})`. Expect `PullResult.kind="trace"`, evidence includes node_ids. |

Phase 1 may add scenarios; Phase 0 freezes these six as the gate.
