# Kernel API

Module location: `src/groundtruth/control/`. Canonical types live in `control/types.py`. Pure logic lives in `control/kernel.py`. The only side effect kernel functions are permitted is telemetry, routed through `control/decision_log.py`.

The kernel is scaffold-agnostic. Adapters (`src/groundtruth/adapters/<scaffold>.py`) translate scaffold events into kernel-canonical types and translate kernel decisions back into scaffold actions. Adapters import the kernel; the kernel never imports adapter code.

## Public surface

```python
def brief(task: TaskInput) -> BriefResult: ...
def observe_edit(edit: EditEvent, run_state: RunState) -> EditObservation: ...
def decide_pre_tool(tool_call: ToolCall, run_state: RunState) -> Decision: ...
def pull(query: PullQuery, run_state: RunState) -> PullResult: ...
def detect_drift(run_state: RunState) -> DriftSignals: ...
def validate_against_graph(diff: Diff, graph: GraphHandle) -> ValidationResult: ...
def replan(triggers: ReplanTriggers, run_state: RunState) -> Replan: ...
def log(event: KernelEvent) -> None: ...
```

### Pure vs I/O split

| Function | Class | Notes |
|---|---|---|
| `decide_pre_tool` | pure | Inputs fully determine output. Replay-testable. |
| `detect_drift` | pure | Signals derived from `RunState` history only. |
| `validate_against_graph` | pure given `GraphHandle` | Treats `GraphHandle` as an opaque read-only port; mockable. |
| `replan` | pure | Triggers + state -> next actions. |
| `brief` | read-only I/O | Reads `graph.db`, plan files. |
| `observe_edit` | read-only I/O | Reads diff text, repo working tree (read-only). |
| `pull` | read-only I/O | Routes to existing MCP handlers. |
| `log` | telemetry write | Only function permitted to write. |

## Canonical types

All types are Pydantic v2 `BaseModel` unless noted. Paths are `pathlib.Path`. Timestamps are ISO 8601 UTC strings.

### `TaskInput`

```python
class TaskInput(BaseModel):
    task_id: str
    repo_root: Path
    issue_text: str
    base_commit: str
    language_hint: str | None = None
```

JSON:

```json
{
  "task_id": "django__django-11099",
  "repo_root": "/repo",
  "issue_text": "...",
  "base_commit": "abc123",
  "language_hint": "python"
}
```

### `EditEvent`

```python
class EditEvent(BaseModel):
    task_id: str
    files_changed: list[Path]
    diff_text: str | None = None
    diff_handle: str | None = None  # opaque ref when diff is large
    ts: str
    source_tool: str  # adapter-normalized: "file_editor", "str_replace_editor", "aider_edit", ...
```

Exactly one of `diff_text` or `diff_handle` must be set.

### `ToolCall`

```python
class ToolIntent(StrEnum):
    READ = "read"
    EDIT = "edit"
    SHELL = "shell"
    MCP_PULL = "mcp_pull"
    OTHER = "other"


class ToolCall(BaseModel):
    task_id: str
    tool_name: str
    args: dict[str, Any]
    ts: str
    intent: ToolIntent
```

### `Capabilities`

```python
class Capabilities(BaseModel):
    block: bool
    visible: bool
    audit: bool
    mid_task_pull: bool
    replan_inject: bool
```

Adapter declares this once at construction. `base.Adapter.__init__` validates that `audit` is `True` (every adapter must support audit-only logging as the floor) and that no capability is `None`.

### `RunState`

```python
class RunState(BaseModel):
    task_id: str
    plan: dict[str, Any]                # v7 plan dict (cluster_files, agent_focus_files, ...)
    brief_result: BriefResult | None
    edit_history: list[EditEvent]
    viewed_files: list[Path]
    warning_history: list[str]
    capabilities: Capabilities
    model_hint: str | None = None
```

`model_hint` is set by the adapter from scaffold metadata (e.g. `"claude-sonnet-4.5"`, `"gpt-5"`). The kernel uses it to pick directive-vs-suggestive thresholds per backing model. See ADR 0003 and Cursor harness alignment in `future_plan.md` §F.1.

### `PullQuery`

```python
class PullKind(StrEnum):
    TRACE = "trace"
    IMPACT = "impact"
    HOTSPOTS = "hotspots"
    VALIDATE = "validate"
    CONTEXT = "context"
    SYMBOLS = "symbols"


class PullQuery(BaseModel):
    kind: PullKind
    args: dict[str, Any]
```

### `BriefResult`

```python
class Candidate(BaseModel):
    path: Path
    score: float


class BriefResult(BaseModel):
    brief_text: str
    candidates: list[Candidate]
    focus_files: list[Path]            # max 3
    cluster_files: list[Path]
    contracts: list[str]
    constraints: list[str]
    confidence: float                  # 0.0 - 1.0
    plan: dict[str, Any]
    plan_path: Path | None
```

Absorbs `pretask.v7_brief.generate_brief` (`src/groundtruth/pretask/v7_brief.py:459`). `confidence` is already gated by `HIGH_CONFIDENCE_MIN = 0.6` (`v7_brief.py:43`); the kernel exposes the numeric value and lets adapters render directive vs suggestive framing.

### `EditObservation`

```python
class EditObservation(BaseModel):
    patch_shape: dict[str, Any]
    focus_hit_at_1: bool
    focus_hit_at_3: bool
    cluster_touch_rate: float
    root_scaffold_files_added: list[Path]
    warnings: list[str]
    expected_side_files_missing: list[Path]
```

Absorbs `runtime.patch_auditor.audit_patch` (`src/groundtruth/runtime/patch_auditor.py:149`).

### `Evidence`

```python
class Evidence(BaseModel):
    node_ids: list[int] = []
    edge_ids: list[int] = []
    candidates_top3: list[Candidate] = []
    patch_shape: dict[str, Any] | None = None
    rule_inputs: dict[str, Any] = {}
```

### `Decision`

```python
class DecisionAction(StrEnum):
    ALLOW = "allow"
    BLOCK = "block"
    VISIBLE = "visible"
    AUDIT = "audit"


class Decision(BaseModel):
    action: DecisionAction
    severity: str                      # "pass" | "warn" | "block" | "audit"
    reasons: list[str]
    message: str
    evidence: Evidence
    confidence: float                  # 0.0 - 1.0
    rule_id: str
    rule_version: str                  # e.g. "kernel-0.1"
```

Extends `runtime.control_policy.decide_control_action` (`src/groundtruth/runtime/control_policy.py:16`) with `confidence`, `evidence`, `rule_id`, `rule_version`.

#### `Decision.confidence` composition

`Decision.confidence` is a multiplicative composition of three signals, each in `[0, 1]`:

```
confidence = clamp(0, 1, localization_confidence
                       * (1 - drift_strength)
                       * graph_validation_score)
```

Where:

- `localization_confidence` is `BriefResult.confidence` (range `[0, 1]`).
- `drift_strength` is a normalized scalar in `[0, 1]` derived from `DriftSignals`. Mapping (canonical, applied by `kernel.replan` when composing): `0.0` if no drift signals; `0.4` if `first_edit_misses_focus`; `0.6` if `root_scaffold_added`; `0.8` if `edits_outside_cluster_count >= 3`; `1.0` if any of the recompute-class triggers fire (`no_focus_file_after_three_edits`, `no_cluster_file_after_five_edits`). When multiple apply, take the max.
- `graph_validation_score` is `1.0` if `validate_against_graph` has not yet been called for the current diff, `1.0` if `ValidationResult.ok == True`, and `0.0` if `ok == False`.

**Phase 1 special case:** `kernel.decide_pre_tool` for the first edit has neither drift evidence (drift=0) nor accumulated validation evidence (validation=1.0). Both factors collapse to 1.0 and `Decision.confidence == BriefResult.confidence` exactly. The composition formula activates from Phase 4 onward when drift and validation signals populate.

Multiplicative form is chosen over weighted-sum because any single low signal must be able to suppress the decision; a 0.0 graph validation must yield a 0.0 decision confidence even when localization is high. A weighted sum cannot express that. Multiplicative also matches the test fixtures (`first_edit_misses_focus_low_confidence` expects decision confidence in `[0.0, 0.6]` when brief confidence is `0.40`).

The `confidence` value is logged in `gt_kernel_decision.context_evaluated.provenance.confidence_components` with all three components broken out so the proof layer can attribute decisions back to which signal dominated.

### `control.paths.normalize`

Single-source path normalization for kernel and adapters. Replaces ad-hoc `_norm` helpers across the codebase. Pure function, no I/O.

```python
def normalize(path: str) -> str: ...
```

Rules (in order):
1. Backslashes become forward slashes (Windows safety).
2. Exactly one leading `/` is removed (absolute-path tolerance).
3. The literal prefix `workspace/` or `testbed/` is removed iff it is a full path component at position 0 (regex anchored). `workspaces/x.py` is NOT a match — the boundary is the trailing slash.
4. Nothing else is stripped. Leading dots in filenames are preserved (`..foo.py` stays `..foo.py`).

This module is the single funnel for path normalization in the kernel. The buggy `pretask.v7_brief._norm` (which calls `lstrip("./")` — a CHARSET strip, not a prefix strip) is wrapped, never called directly.

### `PullResult`

```python
class PullResult(BaseModel):
    kind: PullKind
    payload: dict[str, Any]
    evidence: Evidence
    telemetry_record_id: str
```

The kernel routes `pull` to existing MCP tool handlers in `src/groundtruth/mcp/tools.py` (`handle_trace`, `handle_impact`, `handle_hotspots`, `handle_validate`, `handle_context`, `handle_symbols`). The kernel does not duplicate logic; it is a router with telemetry.

### `DriftSignals`

```python
class DriftSignals(BaseModel):
    first_edit_misses_focus: bool
    root_scaffold_added: bool
    graph_distance_growth: float       # BFS distance from latest edit to focus_files[0], monotonically tracked
    edits_outside_cluster_count: int
    repeated_warnings: list[str]
```

Derived from `runtime.replan.evaluate_replan_triggers` reasons (`src/groundtruth/runtime/replan.py:11`), plus a new `graph_distance_growth` term that tracks BFS distance in `graph.db` from each successive edit to `focus_files[0]`.

### `ValidationResult`

```python
class ValidationResult(BaseModel):
    ok: bool
    broken_signatures: list[str] = []
    orphaned_callers: list[str] = []
    undefined_symbols: list[str] = []
    evidence: Evidence
```

New. Reads diff, queries graph for callers/callees, reports breakage. **Replaces test-based validation** per locked decision 3 (no test reliance). See ADR 0002.

### `Replan`

```python
class ReplanStage(StrEnum):
    STAY_COURSE = "stay_course"
    CORRECTIVE = "corrective"
    RECOMPUTE = "recompute"


class Replan(BaseModel):
    stage: ReplanStage
    message: str
    next_actions: list[str]            # max 3
    new_candidates: list[Candidate] | None = None
    agent_focus_files: list[Path]      # max 3
```

Absorbs `runtime.replan.evaluate_replan_triggers`.

### `ReplanTriggers`

```python
class ReplanTriggers(BaseModel):
    drift: DriftSignals
    validation: ValidationResult | None = None
    failing_tests_after_edit: bool = False
```

### `KernelEvent` (Decision Trace 7-element)

```python
class TriggeringState(BaseModel):
    scaffold: str
    event_kind: str                    # "pre_tool" | "post_edit" | "drift_check" | "validate" | "pull"
    tool: str | None = None
    edit_index: int | None = None


class ContextEvaluated(BaseModel):
    evidence: Evidence
    provenance: dict[str, Any]         # graph_db_sha, plan_path, confidence_components, error_class
    
    
class PolicyApplied(BaseModel):
    rule_id: str
    rule_version: str


class AuthorityExercised(BaseModel):
    adapter: str
    actual_action: DecisionAction
    degraded_from: DecisionAction | None = None


class KernelEvent(BaseModel):
    timestamp: str
    task_id: str
    triggering_state: TriggeringState
    context_evaluated: ContextEvaluated
    policy_applied: PolicyApplied
    alternatives_considered: list[dict[str, Any]]
    confidence: float
    action_selected: DecisionAction
    authority_exercised: AuthorityExercised
```

This is the canonical record. See `docs/kernel/telemetry.md` for the on-disk JSON schema and the block taxonomy.

## Existing-module factoring map

| Existing | Becomes |
|---|---|
| `pretask/v7_brief.py:generate_brief` | internal of `kernel.brief` |
| `runtime/patch_auditor.py:audit_patch` | internal of `kernel.observe_edit` |
| `runtime/control_policy.py:decide_control_action` | internal of `kernel.decide_pre_tool` (extended w/ confidence + evidence) |
| `runtime/replan.py:evaluate_replan_triggers` | internal of `kernel.replan`; signal source for `kernel.detect_drift` |
| `runtime/plan_surface.py` | internal helpers |
| `runtime/repo_adapters.py` | language-`RepoAdapter` registry; documented as "language profile" to disambiguate from "scaffold adapter" |
| `runtime/telemetry.py:append_block` | underneath `kernel.log` (via `decision_log.append_decision`) |
| `mcp/tools.py:handle_*` | called by `kernel.pull` router |

In Phase 1 the kernel is a thin facade over these modules. Deprecation of direct imports is Phase 8+.
