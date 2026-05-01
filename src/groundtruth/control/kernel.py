"""GT control kernel: scaffold-agnostic deterministic decision surface.

Each function wraps an existing ``runtime``/``pretask``/``mcp`` module and
projects the internal return into a canonical ``control.types`` model. The
projection drops every field not in the model -- this is Boundary 1 of the
plan's anti-leakage contract.

Hard rule: kernel functions construct canonical types by NAMED fields, never
``**dict_unpack``. ``**unpack`` is the leakage pattern that bit v7.2.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from groundtruth.control.paths import normalize as _norm_path
from groundtruth.control.types import (
    BriefResult,
    Candidate,
    Decision,
    Diff,
    DriftSignals,
    EditEvent,
    EditObservation,
    GraphHandle,
    KernelEvent,
    PullQuery,
    PullResult,
    Replan,
    ReplanTriggers,
    RunState,
    TaskInput,
    ToolCall,
    ValidationResult,
)


def _to_path(p: Any) -> Path:
    """Normalize then wrap as Path. Single funnel for all kernel-side paths."""
    return Path(_norm_path(str(p)))


def brief(task: TaskInput) -> BriefResult:
    """Generate the pre-task brief.

    Wraps ``pretask.v7_brief.generate_brief`` (v7_brief.py:459). Boundary 1
    filter: drops ``telemetry`` (internal module scores) and ``plan_path``
    (host-only path); projects ``pretask.render.Candidate`` (which carries
    ``tags`` and ``is_test`` provenance) into ``control.types.Candidate``
    keeping only ``path`` + ``score``.
    """
    from groundtruth.pretask.v7_brief import V7BriefResult, generate_brief

    raw = generate_brief(
        issue_text=task.issue_text,
        repo_root=str(task.repo_root),
        graph_db=None,
        task_id=task.task_id,
        return_telemetry=True,
    )
    if not isinstance(raw, V7BriefResult):
        raise RuntimeError(
            "generate_brief returned str despite return_telemetry=True; "
            "internal contract drift -- investigate v7_brief.py"
        )

    plan = raw.plan
    focus_raw = plan.get("agent_focus_files") or []
    cluster_raw = raw.cluster_files

    candidates_filtered = [
        Candidate(path=_to_path(c.file), score=float(c.score))
        for c in raw.candidates
        if getattr(c, "file", None)
    ]

    return BriefResult(
        brief_text=raw.brief,
        candidates=candidates_filtered,
        focus_files=[_to_path(p) for p in focus_raw[:3]],
        cluster_files=[_to_path(p) for p in cluster_raw],
        contracts=[str(line) for line in plan.get("contract_lines", [])],
        constraints=[str(line) for line in plan.get("constraints", [])],
        confidence=float(plan.get("confidence", 0.0)),
        plan=plan,
        plan_path=None,
    )


_RULE_VERSION = "kernel-0.1"
HIGH_CONFIDENCE_MIN = 0.6  # mirrors pretask.v7_brief.HIGH_CONFIDENCE_MIN

# Root-scaffold patterns from the v7 brief constraints. Match only at the
# repo root (no path separator) per the existing constraint line:
#   "Do not add throwaway scaffolding at the repo root: ..."
import re as _re

_ROOT_SCAFFOLD_RE = _re.compile(
    r"^("
    r"reproduce[^/]*\.py|repro[^/]*\.py|test_[^/]*\.py|[^/]*_test\.py|"
    r"[^/]*\.test\.(?:js|ts|jsx|tsx)|[^/]*\.spec\.(?:js|ts|jsx|tsx)|"
    r"[^/]*Test\.java|Repro[^/]*\.java|"
    r"[^/]*_test\.go|repro[^/]*\.go|"
    r"repro[^/]*\.rs"
    r")$"
)


def _is_root_scaffold(norm_path: str) -> bool:
    return bool(_ROOT_SCAFFOLD_RE.match(norm_path))


def _allow_decision(*, confidence: float) -> Decision:
    from groundtruth.control.types import DecisionAction, Evidence

    return Decision(
        action=DecisionAction.ALLOW,
        severity="pass",
        reasons=[],
        message="",
        evidence=Evidence(),
        confidence=confidence,
        rule_id="default_allow",
        rule_version=_RULE_VERSION,
    )


def observe_edit(edit: EditEvent, run_state: RunState) -> EditObservation:
    """Record one post-edit observation.

    Wraps ``runtime.patch_auditor.audit_patch`` (patch_auditor.py:149).
    The kernel synthesises ``name_status`` from ``edit.files_changed`` so the
    auditor never shells out to git -- pre-tool decisions cannot rely on a
    real diff existing yet.

    Boundary 1 filter:
      - drops ``test_execution.failing_test_names`` and any ``error_traces``
      - coerces every ``warnings`` entry to ``str``
      - the auditor's ``recommendation`` and other narrative fields stay in
        ``patch_shape`` (kernel-internal) but are not surfaced as canonical
        EditObservation fields.
    """
    from groundtruth.runtime.patch_auditor import audit_patch

    name_status: list[tuple[str, str]] = [
        ("M", str(p)) for p in edit.files_changed
    ]
    plan = run_state.plan or (
        run_state.brief_result.plan if run_state.brief_result else {}
    )

    repo_root = "."
    if run_state.brief_result and run_state.brief_result.plan_path:
        repo_root = str(run_state.brief_result.plan_path)

    raw = audit_patch(
        repo_root=repo_root,
        plan=plan,
        name_status=name_status,
    )

    warnings = [str(w) for w in (raw.get("warnings") or [])]
    root_scaffolds = [_to_path(p) for p in (raw.get("root_scaffold_files_added") or [])]
    expected_missing = [_to_path(p) for p in (raw.get("expected_side_files_missing") or [])]

    # Boundary 1: strip non-counter fields from test_execution before letting
    # patch_shape escape to telemetry.
    safe_patch_shape = dict(raw)
    te = dict(safe_patch_shape.get("test_execution") or {})
    te.pop("failing_test_names", None)
    te.pop("error_traces", None)
    safe_patch_shape["test_execution"] = te

    return EditObservation(
        patch_shape=safe_patch_shape,
        focus_hit_at_1=bool(raw.get("focus_hit_at_1", False)),
        focus_hit_at_3=bool(raw.get("focus_hit_at_3", False)),
        cluster_touch_rate=float(raw.get("cluster_touch_rate", 0.0)),
        root_scaffold_files_added=root_scaffolds,
        warnings=warnings,
        expected_side_files_missing=expected_missing,
    )


def decide_pre_tool(tool_call: ToolCall, run_state: RunState) -> Decision:
    """Decide allow / block / visible / audit before a tool fires.

    Pre-tool rules (kernel-0.1):
      1. EDIT to a root-scaffold file at first edit -> block
      2. EDIT to a file outside ``brief.focus_files`` at first edit:
         confidence >= HIGH_CONFIDENCE_MIN -> block; else visible
      3. Repeated identical drift warning -> visible
      4. Otherwise -> allow

    ``Decision.confidence`` inherits ``brief.confidence`` at this stage. The
    composition formula (localization * (1 - drift) * graph_validation)
    activates in ``replan`` once drift and validation evidence accumulate.

    Boundary 1 filter:
      - ``reasons`` are rule-id format only (regex ``^[a-z0-9_]+$``)
      - ``message`` truncated via ``runtime.control_policy.format_intervention``
      - ``evidence.rule_inputs`` whitelist: ``edit_path``, ``focus_files``
    """
    from groundtruth.control.types import (
        DecisionAction,
        Evidence,
        ToolIntent,
    )
    from groundtruth.runtime.control_policy import format_intervention

    if tool_call.intent != ToolIntent.EDIT:
        return _allow_decision(
            confidence=run_state.brief_result.confidence if run_state.brief_result else 1.0,
        )

    raw_path = tool_call.args.get("path") if isinstance(tool_call.args, dict) else None
    if not raw_path or not isinstance(raw_path, str):
        # B1 / Cursor error taxonomy: malformed args route to audit, never crash.
        return Decision(
            action=DecisionAction.AUDIT,
            severity="audit",
            reasons=["malformed_tool_args"],
            message="",
            evidence=Evidence(rule_inputs={"error_class": "InvalidArguments"}),
            confidence=run_state.brief_result.confidence if run_state.brief_result else 0.0,
            rule_id="malformed_tool_args",
            rule_version=_RULE_VERSION,
        )

    norm_path = _norm_path(raw_path)
    is_first_edit = len(run_state.edit_history) == 0
    brief = run_state.brief_result
    confidence = brief.confidence if brief else 0.0
    focus_set = (
        {_norm_path(str(p)) for p in brief.focus_files} if brief else set()
    )

    # Rule 1: root-scaffold at first edit -> block
    if is_first_edit and _is_root_scaffold(norm_path):
        reasons = ["first_edit_root_scaffold"]
        message = format_intervention(
            {
                "hook_visible_to_agent": True,
                "message": (
                    "GT runtime intervention [block]\n"
                    "Reasons: first_edit_root_scaffold\n"
                    "Next actions:\n"
                    f"1. Edit ranked targets first; do not add throwaway scaffolding at the repo root ({raw_path})."
                ),
            }
        )
        return Decision(
            action=DecisionAction.BLOCK,
            severity="block",
            reasons=reasons,
            message=message,
            evidence=Evidence(
                rule_inputs={"edit_path": norm_path, "focus_files": sorted(focus_set)},
            ),
            confidence=confidence,
            rule_id="first_edit_root_scaffold",
            rule_version=_RULE_VERSION,
        )

    # Rule 2: missed-focus at first edit, confidence-gated
    if is_first_edit and brief is not None and norm_path not in focus_set and focus_set:
        if confidence >= HIGH_CONFIDENCE_MIN:
            action = DecisionAction.BLOCK
            severity = "block"
        else:
            action = DecisionAction.VISIBLE
            severity = "warn"
        reasons = ["first_edit_missed_focus"]
        message = format_intervention(
            {
                "hook_visible_to_agent": True,
                "message": (
                    f"GT runtime intervention [{severity}]\n"
                    "Reasons: first_edit_missed_focus\n"
                    "Next actions:\n"
                    f"1. Edit ranked targets first: {sorted(focus_set)[:3]}; verify before editing {raw_path}."
                ),
            }
        )
        return Decision(
            action=action,
            severity=severity,
            reasons=reasons,
            message=message,
            evidence=Evidence(
                rule_inputs={"edit_path": norm_path, "focus_files": sorted(focus_set)},
            ),
            confidence=confidence,
            rule_id="first_edit_missed_focus",
            rule_version=_RULE_VERSION,
        )

    # Rule 3: repeated drift warning -> visible (do not allow)
    if run_state.warning_history and brief is not None and norm_path not in focus_set and focus_set:
        return Decision(
            action=DecisionAction.VISIBLE,
            severity="warn",
            reasons=["repeated_drift_warning"],
            message="GT runtime intervention [warn]\nReasons: repeated_drift_warning",
            evidence=Evidence(
                rule_inputs={"edit_path": norm_path, "focus_files": sorted(focus_set)},
            ),
            confidence=confidence,
            rule_id="repeated_drift_warning",
            rule_version=_RULE_VERSION,
        )

    return _allow_decision(confidence=confidence)


_PULL_WHITELIST: dict[str, set[str]] = {
    "trace": {"symbol", "callers", "callees"},
    "impact": {"direct_callers", "impact_summary"},
    "hotspots": {"hotspots"},
    "validate": {"valid", "errors"},
    "context": {"usages"},
    "symbols": {"symbols", "imports_from", "imported_by"},
}


def pull(query: PullQuery, run_state: RunState) -> PullResult:
    """Route a mid-task pull to the MCP handler matching ``query.kind``.

    Boundary 1 filter: per-tool whitelist drops handler internals (intervention
    timing, reasoning_guidance narrative, tracker IDs) before they cross the
    kernel boundary. The whitelist is the contract -- adding a new key in a
    handler does NOT auto-expose it.

    Plumbing safety (B1): if the MCP handler returns an ``error`` key, the
    kernel surfaces it as a typed ``ErrorClass`` rather than dropping it
    silently.

    The async/sync boundary lives here: MCP handlers are async; the kernel is
    sync-by-design per ``control.types``. ``asyncio.run`` is OK because each
    pull is a one-shot routed call, not part of a long-lived event loop.
    """
    import asyncio
    import uuid

    from groundtruth.control.types import Evidence, PullKind

    handler_payload = _invoke_mcp_handler(query, run_state, asyncio.run)
    error_value = handler_payload.get("error") if isinstance(handler_payload, dict) else None
    kind_str = (
        query.kind.value if isinstance(query.kind, PullKind) else str(query.kind)
    )

    whitelist = _PULL_WHITELIST.get(kind_str, set())
    if isinstance(handler_payload, dict):
        filtered = {k: v for k, v in handler_payload.items() if k in whitelist}
    else:
        filtered = {}

    # Evidence assembly: collect node_ids embedded in the handler payload.
    node_ids: list[int] = []
    if isinstance(handler_payload, dict):
        for k in ("callers", "callees", "direct_callers", "indirect_dependents"):
            for item in handler_payload.get(k, []) or []:
                if isinstance(item, dict):
                    nid = item.get("node_id") or item.get("id")
                    if isinstance(nid, int):
                        node_ids.append(nid)
        sym = handler_payload.get("symbol")
        if isinstance(sym, dict):
            sid = sym.get("node_id") or sym.get("id")
            if isinstance(sid, int):
                node_ids.append(sid)

    record_id = uuid.uuid4().hex
    if error_value is not None:
        # Surface handler-level error class without crashing.
        filtered["_error"] = str(error_value)

    return PullResult(
        kind=PullKind(kind_str),
        payload=filtered,
        evidence=Evidence(node_ids=node_ids, rule_inputs={"pull_kind": kind_str}),
        telemetry_record_id=record_id,
    )


def _invoke_mcp_handler(
    query: PullQuery,
    run_state: RunState,
    runner: Any,
) -> dict[str, Any]:
    """Dispatch ``query.kind`` to the matching MCP handler.

    The runner is injected (``asyncio.run``) so tests can substitute a
    synchronous fake without touching ``asyncio``.
    """
    del run_state  # not yet used; reserved for handler dependency injection.
    from groundtruth.mcp import tools as mcp_tools

    kind = query.kind.value if hasattr(query.kind, "value") else str(query.kind)
    args = dict(query.args)
    handler_name = f"handle_{kind}"
    handler = getattr(mcp_tools, handler_name, None)
    if handler is None:
        return {"error": f"unknown_pull_kind:{kind}"}

    coro = handler(**args)
    return runner(coro) if hasattr(coro, "__await__") else coro


def detect_drift(run_state: RunState) -> DriftSignals:
    """Compute drift signals from edit history vs the plan.

    Pure function. Reads ``RunState.edit_history``, ``brief_result.focus_files``,
    ``brief_result.cluster_files``, ``warning_history``.

    Signals computed:
      - ``first_edit_misses_focus``: first edit's first file outside focus_files
      - ``root_scaffold_added``: any edit touched a root-scaffold path
      - ``edits_outside_cluster_count``: edits whose first file is outside cluster
      - ``repeated_warnings``: warning strings that appear >=2 times in history
      - ``graph_distance_growth``: deferred to Phase 4 (needs graph.db wiring);
        emitted as 0.0 here -- the field is non-negative-only by contract.
    """
    brief = run_state.brief_result
    focus = {_norm_path(str(p)) for p in (brief.focus_files if brief else [])}
    cluster = {_norm_path(str(p)) for p in (brief.cluster_files if brief else [])}

    history = run_state.edit_history
    first_misses = False
    root_added = False
    outside_cluster = 0

    if history:
        first_edit_files = history[0].files_changed
        if first_edit_files:
            first_path = _norm_path(str(first_edit_files[0]))
            if focus and first_path not in focus:
                first_misses = True

    for ev in history:
        if not ev.files_changed:
            continue
        for fp in ev.files_changed:
            np = _norm_path(str(fp))
            if _is_root_scaffold(np):
                root_added = True
            if cluster and np not in cluster:
                outside_cluster += 1
                break  # one outside-cluster strike per edit, not per file

    # repeated warnings: same string appearing >=2 times
    seen: dict[str, int] = {}
    for w in run_state.warning_history:
        seen[w] = seen.get(w, 0) + 1
    repeated = sorted(w for w, n in seen.items() if n >= 2)

    return DriftSignals(
        first_edit_misses_focus=first_misses,
        root_scaffold_added=root_added,
        graph_distance_growth=0.0,
        edits_outside_cluster_count=outside_cluster,
        repeated_warnings=repeated,
    )


_DEF_LINE_RE = _re.compile(
    r"^([+-])\s*(?:async\s+)?def\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(([^)]*)\)\s*(?:->\s*[^:]+)?:\s*$"
)


def _file_module(file_path: str) -> str:
    norm = _norm_path(file_path)
    if norm.endswith(".py"):
        norm = norm[:-3]
    return norm.replace("/", ".")


def validate_against_graph(diff: Diff, graph: GraphHandle) -> ValidationResult:
    """Structural validation: signature break, orphaned caller.

    Parses the diff for removed-vs-added ``def name(args):`` pairs. For each
    function whose signature changed, looks up its callers in the graph
    handle. If callers exist, the change is a *break* and the callers are
    *orphaned*.

    No test execution. Per ADR 0002 (locked decision 3, SWE-Bench Pro
    spec/interface augmentation evidence).
    """
    from groundtruth.control.types import Evidence

    if not diff.diff_text or not diff.files_changed:
        return ValidationResult(ok=True, evidence=Evidence())

    removed: dict[str, tuple[str, str]] = {}  # name -> (sig, file_path)
    added: dict[str, str] = {}  # name -> sig

    current_file: str | None = None
    for line in diff.diff_text.splitlines():
        if line.startswith("+++ b/"):
            current_file = line[len("+++ b/") :]
            continue
        if line.startswith("--- a/"):
            current_file = line[len("--- a/") :]
            continue
        m = _DEF_LINE_RE.match(line)
        if not m:
            continue
        sign, name, args = m.group(1), m.group(2), m.group(3).strip()
        if sign == "-":
            removed[name] = (args, current_file or (str(diff.files_changed[0]) if diff.files_changed else ""))
        else:
            added[name] = args

    broken_signatures: list[str] = []
    orphaned_callers: list[str] = []
    node_ids: list[int] = []

    callers_of: dict[str, list[dict[str, Any]]] = getattr(graph, "callers_of", {}) or {}
    nodes: list[dict[str, Any]] = getattr(graph, "nodes", []) or []

    for name, (old_args, file_path) in removed.items():
        new_args = added.get(name)
        if new_args is None or new_args == old_args:
            continue  # pure removal or unchanged signature -- not a break
        # Find the qualified-name lookup key.
        file_mod = _file_module(file_path)
        direct_key = f"{file_mod}.{name}"
        chosen_key: str | None = None
        chosen_node_id: int | None = None
        # Prefer matching node by file_path + name suffix.
        for node in nodes:
            qn = str(node.get("qualified_name") or "")
            if qn.endswith(f".{name}") and _norm_path(str(node.get("file_path") or "")) == _norm_path(file_path):
                chosen_key = qn
                nid = node.get("id")
                if isinstance(nid, int):
                    chosen_node_id = nid
                break
        if chosen_key is None:
            # Fallback: direct module key, then suffix-scan.
            if direct_key in callers_of:
                chosen_key = direct_key
            else:
                for k in callers_of:
                    if k.startswith(f"{file_mod}.") and k.endswith(f".{name}"):
                        chosen_key = k
                        break
        callers = callers_of.get(chosen_key, []) if chosen_key else []
        symbol_label = chosen_key or direct_key
        broken_signatures.append(f"{symbol_label}: ({old_args}) -> ({new_args})")
        for caller in callers:
            qn = str(caller.get("qualified_name") or "")
            fp = str(caller.get("file_path") or "")
            line_no = caller.get("line", "?")
            orphaned_callers.append(f"{qn} @ {fp}:{line_no}")
        if chosen_node_id is not None:
            node_ids.append(chosen_node_id)

    ok = not broken_signatures
    return ValidationResult(
        ok=ok,
        broken_signatures=broken_signatures,
        orphaned_callers=orphaned_callers,
        undefined_symbols=[],
        evidence=Evidence(node_ids=node_ids),
    )


_RECOMPUTE_WARNINGS = frozenset(
    {
        "missing_or_empty_plan_cluster",
        "no_focus_file_after_three_edits",
        "no_cluster_file_after_five_edits",
    }
)


def replan(triggers: ReplanTriggers, run_state: RunState) -> Replan:
    """Generate a corrective replan from drift + validation triggers.

    Pure-deterministic mapping (Boundary 1):
      - any drift OR validation OR failing_tests_after_edit fires -> CORRECTIVE
      - any recompute-class warning OR validation with >=2 broken signatures
        OR edits_outside_cluster_count >= 5 -> RECOMPUTE (escalates from
        corrective)
      - none of the above -> STAY_COURSE

    Caps applied:
      - ``next_actions``: <=3 entries, each <=200 chars (per future_plan.md
        operational specs)
      - ``agent_focus_files``: <=3 paths, drawn from run_state.brief_result
    """
    from groundtruth.control.types import ReplanStage

    drift = triggers.drift
    validation = triggers.validation

    fired = (
        drift.first_edit_misses_focus
        or drift.root_scaffold_added
        or drift.edits_outside_cluster_count > 0
        or bool(drift.repeated_warnings)
        or (validation is not None and not validation.ok)
        or triggers.failing_tests_after_edit
    )

    if not fired:
        return Replan(
            stage=ReplanStage.STAY_COURSE,
            message="Continue with the current focused edit path.",
            next_actions=[],
            agent_focus_files=_focus_files_capped(run_state),
        )

    recompute_warning_hit = any(w in _RECOMPUTE_WARNINGS for w in drift.repeated_warnings)
    many_validation_breaks = validation is not None and len(validation.broken_signatures) >= 2
    deep_outside_cluster = drift.edits_outside_cluster_count >= 5
    stage = (
        ReplanStage.RECOMPUTE
        if (recompute_warning_hit or many_validation_breaks or deep_outside_cluster)
        else ReplanStage.CORRECTIVE
    )

    next_actions = _build_next_actions(triggers, run_state)
    next_actions = [a[:200] for a in next_actions[:3]]

    if stage == ReplanStage.RECOMPUTE:
        message = (
            "GT replan [recompute]: structural signals exceed corrective threshold; "
            "recompute the plan from the original issue + observed edits."
        )
    else:
        reasons_summary = _summarise_triggers(triggers)
        message = f"GT replan [corrective]: {reasons_summary}"

    return Replan(
        stage=stage,
        message=message,
        next_actions=next_actions,
        agent_focus_files=_focus_files_capped(run_state),
    )


def _focus_files_capped(run_state: RunState) -> list[Path]:
    brief = run_state.brief_result
    if brief and brief.focus_files:
        return [_to_path(str(p)) for p in brief.focus_files[:3]]
    raw = (run_state.plan or {}).get("agent_focus_files") or []
    out: list[Path] = []
    for item in raw:
        if isinstance(item, dict):
            value = item.get("file") or item.get("path")
        else:
            value = item
        if value:
            out.append(_to_path(str(value)))
        if len(out) >= 3:
            break
    return out


def _build_next_actions(triggers: ReplanTriggers, run_state: RunState) -> list[str]:
    actions: list[str] = []
    drift = triggers.drift
    focus = _focus_files_capped(run_state)
    if drift.root_scaffold_added:
        actions.append("Remove root-level repro/scaffold files from the patch.")
    if drift.first_edit_misses_focus or drift.edits_outside_cluster_count > 0:
        if focus:
            actions.append(f"Open and edit ranked focus file first: {focus[0]}.")
        else:
            actions.append("Recompute the v7 plan before continuing.")
    if triggers.validation and not triggers.validation.ok:
        broken = triggers.validation.broken_signatures
        if broken:
            actions.append(f"Restore signature compatibility for: {broken[0]}.")
    if triggers.failing_tests_after_edit:
        actions.append("Inspect the visible failing test before expanding the patch.")
    if not actions:
        actions.append("Re-check localization before continuing outside the candidate cluster.")
    return actions


def _summarise_triggers(triggers: ReplanTriggers) -> str:
    parts: list[str] = []
    drift = triggers.drift
    if drift.first_edit_misses_focus:
        parts.append("first_edit_missed_focus")
    if drift.root_scaffold_added:
        parts.append("root_scaffold_added")
    if drift.edits_outside_cluster_count > 0:
        parts.append(f"edits_outside_cluster={drift.edits_outside_cluster_count}")
    if triggers.validation and not triggers.validation.ok:
        parts.append(f"validation_broken={len(triggers.validation.broken_signatures)}")
    if triggers.failing_tests_after_edit:
        parts.append("failing_tests_after_edit")
    return ", ".join(parts) if parts else "drift detected"


def log(event: KernelEvent) -> None:
    """Append one ``KernelEvent`` to the unified telemetry stream.

    Routes through ``control.decision_log.append_decision`` which in turn
    calls ``runtime.telemetry.append_block`` with the canonical block name
    ``gt_kernel_decision``.
    """
    from groundtruth.control.decision_log import append_decision

    append_decision(event)
