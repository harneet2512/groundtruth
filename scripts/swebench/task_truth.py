#!/usr/bin/env python3
"""Per-task truth ledger — reconciles certs, runtime witness, deep metrics, outcome.

Writes ``task_truth.json`` beside DeepSWE outcome artifacts. Witness-over-cert
rules follow gt_gt.md §12 (GRAPH_FAIL_MISSING_HANDOFF reconciled when runtime
witness holds).
"""
from __future__ import annotations

import glob
import json
import os
import re
import sqlite3
import sys
import tempfile
from typing import Any

# deepswe_outcome is imported lazily in build_task_truth to avoid circular imports
# when tests load via importlib.

_SCRIPTS = os.path.dirname(__file__)
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)
_SRC = os.path.abspath(os.path.join(_SCRIPTS, "..", "..", "src"))
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from artifact_resolver import (  # noqa: E402
    TrialArtifacts,
    brief_provenance,
    resolve_trial_artifacts,
)
from reconcile import reconcile_graph_handoff as reconcile_graph_handoff  # noqa: E402
from groundtruth.runtime.context_policy import POLICY_VERSION as PHASE_POLICY_VERSION  # noqa: E402
from groundtruth.runtime.obligations import OBLIGATION_VERSION  # noqa: E402
from groundtruth.runtime.trajectory_state import Turn, derive_phase, derive_state, is_source_path  # noqa: E402
from groundtruth.runtime.verification_horizon import HORIZON_VERSION  # noqa: E402

__all__ = ["build_task_truth", "write_task_truth", "reconcile_graph_handoff"]


def _load_json(path: str) -> dict | None:
    if not path or not os.path.isfile(path):
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def _load_jsonl(path: str) -> list[dict[str, Any]]:
    if not path or not os.path.isfile(path):
        return []
    rows: list[dict[str, Any]] = []
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if isinstance(row, dict):
                    rows.append(row)
    except OSError:
        return []
    return rows


def _find_trial_artifacts(jobs_dir: str, *, instance_id: str | None = None) -> dict[str, str | None]:
    """Locate per-trial paths under jobs_dir (P1-29 artifact_resolver)."""
    arts = resolve_trial_artifacts(
        jobs_dir, instance_id=instance_id, strict_task_match=bool(instance_id)
    )
    return arts.as_dict()


def _trajectory_integrity(artifacts: dict[str, str | None]) -> dict[str, Any]:
    canon = artifacts.get("canonical_trajectory")
    mini = artifacts.get("mini_trajectory")
    canon_bytes = os.path.getsize(canon) if canon and os.path.isfile(canon) else None
    mini_bytes = os.path.getsize(mini) if mini and os.path.isfile(mini) else None
    return {
        "canonical_path": canon,
        "canonical_bytes": canon_bytes,
        "mini_path": mini,
        "mini_bytes": mini_bytes,
        "mini_fallback": bool(canon_bytes == 0 and mini_bytes and mini_bytes > 0),
    }


def _load_deepswe_outcome():
    import importlib.util

    path = os.path.join(os.path.dirname(__file__), "..", "verify", "deepswe_outcome.py")
    spec = importlib.util.spec_from_file_location("deepswe_outcome_tt", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _load_patch_hygiene():
    import importlib.util

    path = os.path.join(os.path.dirname(__file__), "patch_hygiene.py")
    spec = importlib.util.spec_from_file_location("patch_hygiene_tt", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _patch_hygiene_from_artifacts(artifacts: dict[str, str | None]) -> dict:
    """Classify model patch for task_truth (P1-28)."""
    patch = ""
    if artifacts.get("result_json"):
        d = _load_json(artifacts["result_json"]) or {}
        patch = str((d.get("info") or {}).get("submission") or "")
    if not patch and artifacts.get("mini_trajectory"):
        d = _load_json(artifacts["mini_trajectory"]) or {}
        patch = str((d.get("info") or {}).get("submission") or "")
    ph = _load_patch_hygiene()
    return ph.classify_patch(patch)


def _first_str(*values: Any) -> str:
    for value in values:
        if isinstance(value, str) and value:
            return value
        if isinstance(value, dict):
            for key in ("command", "cmd", "content", "text", "output", "observation"):
                v = value.get(key)
                if isinstance(v, str) and v:
                    return v
    return ""


def _tool_call_command(call: Any) -> str:
    """Extract a command from one native chat-completions tool call."""
    if not isinstance(call, dict):
        raise ValueError("native trajectory tool call is not an object")
    function = call.get("function")
    if not isinstance(function, dict):
        raise ValueError("native trajectory tool call has no function object")
    arguments = function.get("arguments")
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except (TypeError, ValueError):
            raise ValueError("native trajectory tool arguments are malformed") from None
    command = _first_str(arguments)
    if not command:
        raise ValueError("native trajectory tool call has no command")
    return command


def _turns_from_native_messages(messages: list[Any]) -> list[Turn]:
    """Pair assistant tool calls with their tool-result observations.

    System/user instruction text is not an action observation and must never be
    scanned for delivery markers.  The call id is the lossless join key used by
    mini-swe-agent's stored chat-completions trajectory.
    """
    pending: dict[str, str] = {}
    seen_call_ids: set[str] = set()
    turns: list[Turn] = []
    for item in messages:
        if not isinstance(item, dict):
            raise ValueError("native trajectory message is not an object")
        role = item.get("role")
        if role == "assistant":
            calls = item.get("tool_calls")
            if calls is None:
                # Narrative assistant messages are valid and are not actions.
                continue
            if not isinstance(calls, list):
                raise ValueError("native trajectory assistant tool_calls is not a list")
            for call in calls:
                if not isinstance(call, dict):
                    raise ValueError("native trajectory tool call is not an object")
                call_id = call.get("id")
                command = _tool_call_command(call)
                if not isinstance(call_id, str) or not call_id:
                    raise ValueError("native trajectory tool call has no id")
                if call_id in seen_call_ids:
                    raise ValueError("native trajectory has duplicate tool-call id")
                seen_call_ids.add(call_id)
                pending[call_id] = command
        elif role == "tool":
            call_id = item.get("tool_call_id")
            if not isinstance(call_id, str) or call_id not in pending:
                raise ValueError("native trajectory has unmatched tool result")
            observation = _first_str(item.get("content"), item.get("output"))
            turns.append(Turn(
                command=pending.pop(call_id),
                observation=observation,
                full_observation=observation,
            ))
        elif role == "exit":
            if not pending:
                # Older mini-swe-agent artifacts append terminal framing after
                # the final tool result.  It is not a second action.
                continue
            if len(pending) != 1:
                raise ValueError("native trajectory exit does not identify one pending call")
            _call_id, command = pending.popitem()
            observation = _first_str(item.get("content"), item.get("output"))
            turns.append(Turn(
                command=command,
                observation=observation,
                full_observation=observation,
            ))
        elif role in {"system", "user"}:
            # Instruction/narrative records are valid but never observations.
            continue
        else:
            raise ValueError(f"native trajectory has unsupported message role: {role!r}")
    if pending:
        raise ValueError("native trajectory ends with unmatched tool calls")
    return turns


def _turns_from_mini_trajectory(path: str | None) -> list[Turn]:
    if not path or not os.path.isfile(path):
        return []
    data = _load_json(path)
    if data is None:
        raise ValueError("native trajectory document is malformed or not an object")
    raw = data.get("messages") or data.get("trajectory") or data.get("steps") or []
    if not isinstance(raw, list):
        raise ValueError("native trajectory document has no message list")
    if any(not isinstance(item, dict) for item in raw):
        raise ValueError("native trajectory message is not an object")
    if data.get("messages") is raw and any(
        isinstance(item, dict) and isinstance(item.get("role"), str)
        for item in raw
    ):
        return _turns_from_native_messages(raw)
    turns: list[Turn] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        action = item.get("action") or item.get("tool_call") or {}
        result = item.get("result") or item.get("observation") or item.get("response") or {}
        command = _first_str(
            item.get("command"),
            item.get("cmd"),
            action,
            item.get("content") if item.get("role") == "assistant" else "",
        )
        observation = _first_str(
            item.get("observation"),
            item.get("output"),
            result,
            item.get("content") if item.get("role") != "assistant" else "",
        )
        full = _first_str(item.get("full_observation"), item.get("content"), observation)
        if command or observation or full:
            turns.append(Turn(command=command, observation=observation, full_observation=full))
    return turns


def _source_write_truth(path: str | None) -> dict[str, Any] | None:
    """Read exact chronological source-write truth from the producer ledger.

    ``source_write_proof`` is an internal producer receipt, not a delivery. Its
    exact post-command byte comparison is stronger than parsing shell syntax.
    Whole-task edit truth retains every successful byte-changing write, while a
    separate coherence episode resets after each passing ``test_proof``. Once a
    source receipt is present, malformed proof rows fail closed rather than
    silently reverting to weaker command inference.
    """
    proof_rows = [
        row for row in _load_jsonl(path or "")
        if row.get("layer") == "ss.coherence.proof"
        and row.get("event_type") in {"source_write_proof", "test_proof"}
    ]
    source_count = sum(
        row.get("event_type") == "source_write_proof" for row in proof_rows
    )
    if source_count == 0:
        return None

    successful_all: list[tuple[int, str]] = []
    successful_since_pass: list[tuple[int, str]] = []
    seen: set[tuple[str, int, str]] = set()
    step_kinds: dict[int, str] = {}
    previous_step = -1
    test_count = 0
    latest_pass: int | None = None
    for row in proof_rows:
        event_type = row.get("event_type")
        step = row.get("action_step")
        iteration = row.get("iteration")
        rel = row.get("file_path")
        normalized = _normalize_repo_path(rel) if isinstance(rel, str) else ""
        valid_path = bool(
            normalized
            and not normalized.startswith("/")
            and not re.match(r"^[A-Za-z]:/", normalized)
            and ".." not in normalized.split("/")
        )
        common_valid = (
            row.get("outcome") == "suppressed_internal_only"
            and type(row.get("chars_delivered")) is int
            and row.get("chars_delivered") == 0
            and isinstance(step, int) and not isinstance(step, bool) and step >= 0
            and isinstance(iteration, int) and not isinstance(iteration, bool)
            and iteration == step
            and step >= previous_step
        )
        if event_type == "source_write_proof":
            event_valid = (
                row.get("reason") == "exact_post_command_write_result"
                and valid_path
                and type(row.get("write_ok")) is bool
                and type(row.get("bytes_changed")) is bool
            )
            identity = (event_type, step, normalized)
        else:
            event_valid = (
                row.get("reason") == "classified_test_result"
                and normalized == ""
                and type(row.get("passed")) is bool
            )
            identity = (event_type, step, "")
        same_step_kind = step_kinds.get(step) if isinstance(step, int) else None
        if not (
            common_valid and event_valid and identity not in seen
            and (same_step_kind is None or same_step_kind == event_type)
        ):
            return {
                "valid": False,
                "count": source_count,
                "test_count": sum(
                    row.get("event_type") == "test_proof" for row in proof_rows
                ),
                "successful_all": [],
                "successful_since_pass": [],
                "latest_pass": None,
                "error": "malformed_or_nonchronological_producer_proof",
            }
        previous_step = step
        seen.add(identity)
        step_kinds[step] = event_type
        if event_type == "test_proof":
            test_count += 1
            if row["passed"]:
                successful_since_pass.clear()
                latest_pass = step
        elif row["write_ok"] and row["bytes_changed"]:
            successful_all.append((step, normalized))
            successful_since_pass.append((step, normalized))
    return {
        "valid": True,
        "count": source_count,
        "test_count": test_count,
        "successful_all": successful_all,
        "successful_since_pass": successful_since_pass,
        "latest_pass": latest_pass,
        "error": None,
    }


# ── Edit-truth reconciliation (DEFECT-1) ─────────────────────────────────────────────
# Shell-command inference (derive_state / command_event) only recognizes a NARROW set of
# write verbs (apply_patch / python -c / cat> / >>). It MISSES edits applied via ``sed -i``
# (which it classifies as a VIEW), ``patch`` / ``git apply``, ``tee``, and heredocs — the
# tell of which is a ``.orig`` backup file. When the run's APPLIED patch demonstrably
# changed a source file, that file WAS edited regardless of how the shell syntax parsed, so
# the patch is an edit-truth authority at least as strong as command inference (command
# inference stays the fallback). These helpers locate the applying step generically — no
# task/repo-specific keys.
_EDIT_SHAPE_RE = re.compile(
    r"(?:^|[;&|]\s*)(?:sed\s+-i|patch(?:\s|$)|git\s+apply|tee(?:\s|$)|apply_patch|dd\s)"
    r"|>>?\s*[^>\s&|]"            # shell redirect into a file
    r"|<<-?\s*[\"']?[A-Za-z_]"    # heredoc
    r"|\.orig\b",                # sed -i.orig / patch backup-file tell
    re.I,
)


def _looks_like_edit_command(command: str) -> bool:
    """True when a command has the shape of a file WRITE/patch (broad, generic)."""
    return bool(_EDIT_SHAPE_RE.search(command or ""))


def _first_edit_step_for_paths(turns: list[Turn], target_paths: set[str]) -> int | None:
    """1-indexed step of the first edit-shaped command that names a target source path.

    Generic: matches a target's normalized path OR its basename as a substring of the
    command. Returns None when no applying step can be located (honest unknown — never a
    fabricated step), even when the patch itself proves an edit occurred.
    """
    if not target_paths:
        return None
    needles: set[str] = set()
    for path in target_paths:
        if path:
            needles.add(path)
            needles.add(path.rsplit("/", 1)[-1])
    for index, turn in enumerate(turns, 1):
        command = turn.command or ""
        if not _looks_like_edit_command(command):
            continue
        norm = command.replace("\\", "/")
        if any(needle and needle in norm for needle in needles):
            return index
    return None


def _trajectory_state_summary(
    artifacts: dict[str, str | None],
    *,
    changed_source_paths: set[str] | None = None,
    has_patch: bool = False,
) -> dict[str, Any]:
    step_limit_raw = os.environ.get("GT_STEP_LIMIT")
    try:
        step_limit = int(step_limit_raw) if step_limit_raw else None
    except ValueError:
        step_limit = None
    turns = _turns_from_mini_trajectory(artifacts.get("mini_trajectory"))
    state = derive_state(turns, step_limit=step_limit)
    write_truth = _source_write_truth(artifacts.get("runtime_ledger"))
    if write_truth is not None and write_truth["valid"]:
        successful_all = write_truth["successful_all"]
        state.edited_files = {path for _step, path in successful_all}
        state.source_edit_count = len(successful_all)
        state.nonedit_streak = (
            max(0, state.action_count - successful_all[-1][0]) if successful_all else 0
        )
    if write_truth is None:
        edit_authority = "trajectory_command_inference"
        edit_valid = True
        proof_count = 0
        test_proof_count = 0
        latest_passing_test_step = None
        coherence_write_count = None
        coherence_edited_files: list[str] = []
        # Reconcile weak command inference against the APPLIED patch (DEFECT-1): a source
        # file the diff demonstrably changed WAS edited even when the write was applied via
        # sed -i / patch / heredoc (missed by shell-command inference). The patch is an
        # authority at least as strong as inference; inference remains the fallback.
        inferred_files = set(state.edited_files)
        patch_source = {
            norm
            for p in (changed_source_paths or set())
            if p
            for norm in (_normalize_repo_path(p),)
            if is_source_path(norm)
        }
        reconciled_files = set(inferred_files)
        reconciled_count = state.source_edit_count
        if has_patch and patch_source:
            reconciled_files |= patch_source
            reconciled_count = max(reconciled_count, len(patch_source))
            if (reconciled_files - inferred_files) or reconciled_count > state.source_edit_count:
                edit_authority = "applied_patch_reconciled_command_inference"
        source_edit_count: int | None = reconciled_count
        edited_files = sorted(reconciled_files)[:20]
        edited_source_set = {f for f in reconciled_files if is_source_path(f)}
        edit_error = None
    elif write_truth["valid"]:
        edit_authority = "ss.coherence.proof/source_write_proof"
        edit_valid = True
        proof_count = write_truth["count"]
        test_proof_count = write_truth["test_count"]
        latest_passing_test_step = write_truth["latest_pass"]
        coherence_successful = write_truth["successful_since_pass"]
        coherence_write_count = len(coherence_successful)
        coherence_edited_files = sorted({path for _step, path in coherence_successful})[:20]
        source_edit_count = state.source_edit_count
        edited_files = sorted(state.edited_files)[:20]
        edited_source_set = {f for f in state.edited_files if is_source_path(f)}
        edit_error = None
    else:
        edit_authority = "invalid_ss.coherence.proof/source_write_proof"
        edit_valid = False
        proof_count = write_truth["count"]
        test_proof_count = write_truth["test_count"]
        latest_passing_test_step = None
        coherence_write_count = None
        coherence_edited_files = []
        source_edit_count = None
        edited_files = []
        edited_source_set = set()
        edit_error = write_truth["error"]
    # First applied source-edit step (DEFECT-1): the localization-efficacy witness
    # (steps-to-first-correct-edit). One turn-indexed basis across all authorities;
    # honest None when no applying step can be located.
    first_source_edit_step = _first_edit_step_for_paths(turns, edited_source_set)
    return {
        "phase_policy_version": PHASE_POLICY_VERSION,
        "turns_observed": len(turns),
        "phase": derive_phase(state).value,
        "action_count": state.action_count,
        "step_limit": state.step_limit,
        "budget_fraction": round(state.budget_fraction, 8),
        "viewed_files": sorted(state.viewed_files)[:20],
        "edited_files": edited_files,
        "source_edit_count": source_edit_count,
        "first_source_edit_step": first_source_edit_step,
        "edit_truth_authority": edit_authority,
        "edit_truth_valid": edit_valid,
        "edit_truth_error": edit_error,
        "source_write_proof_count": proof_count,
        "test_proof_count": test_proof_count,
        "latest_passing_test_step": latest_passing_test_step,
        "coherence_write_count": coherence_write_count,
        "coherence_edited_files": coherence_edited_files,
        "test_count": state.test_count,
        "test_evidence_seen": state.test_evidence_seen,
        "last_test_failed": state.last_test_failed,
        "delivered_markers": [
            {"turn": turn, "marker": marker, "reason": reason}
            for turn, marker, reason in state.delivered_markers[-20:]
        ],
    }


def _obligation_lifecycle_summary(obligation_status: dict | None) -> dict[str, Any]:
    data = obligation_status or {}
    records = data.get("obligations") or data.get("records") or data.get("snapshot") or []
    counts: dict[str, int] = {}
    if isinstance(records, list):
        for rec in records:
            if isinstance(rec, dict):
                status = str(rec.get("status") or "unknown")
                counts[status] = counts.get(status, 0) + 1
    return {
        "version": OBLIGATION_VERSION,
        "source_present": bool(data),
        "count": sum(counts.values()),
        "status_counts": counts,
    }


def _consumption_summary(deep: dict) -> dict[str, Any]:
    return {
        "delivered": deep.get("gt_blocks_delivered"),
        "consumed": deep.get("gt_blocks_consumed"),
        "verification_followup": deep.get("gt_blocks_verification_followup"),
        "hard_enforced": deep.get("gt_blocks_hard_enforced"),
        "enforced": deep.get("gt_blocks_enforced"),
        "semantics": deep.get("gt_enforcement_semantics"),
    }


def _verification_horizon_summary(deep: dict, verifier_semantics: dict) -> dict[str, Any]:
    semantics = deep.get("gt_enforcement_semantics") or {}
    return {
        "version": HORIZON_VERSION,
        "summary": deep.get("verification_horizon") or {},
        "pre_submit_intervention": True,
        "hard_enforced": bool(semantics.get("hard_enforced")) if isinstance(semantics, dict) else False,
        "official_verifier_repair": bool(verifier_semantics.get("official_verifier_repair")),
        "self_verifier_retry": bool(verifier_semantics.get("self_verifier_retry")),
    }


def _runtime_ledger_summary(path: str | None) -> dict[str, Any]:
    rows = _load_jsonl(path or "")
    outcome_counts: dict[str, int] = {}
    reason_counts: dict[str, int] = {}
    for row in rows:
        outcome = str(row.get("outcome") or "unknown")
        outcome_counts[outcome] = outcome_counts.get(outcome, 0) + 1
        reason = str(row.get("reason") or "")
        if reason:
            reason_counts[reason] = reason_counts.get(reason, 0) + 1
    return {
        "path": path,
        "present": bool(path and os.path.isfile(path)),
        "entry_count": len(rows),
        "outcome_counts": outcome_counts,
        "reason_counts": reason_counts,
    }


def _truth_authority_map() -> dict[str, str]:
    """Product-facing authority contract for the one-surface truth ledger."""
    return {
        "outcome": "task_truth.outcome",
        "substrate": "reconciled_substrate_verdict.json",
        "runtime_witness": "task_truth.runtime_witness",
        "brief_delivery": "task_truth.brief_provenance",
        "verifier_semantics": "task_truth.verifier_semantics",
        "verifier_truth": "official report.json tests_status.PASS_TO_PASS",
        "obligations": "task_truth.obligation_status",
        "patch_hygiene": "task_truth.patch_hygiene",
        "trajectory_integrity": "task_truth.trajectory_integrity",
        "consumption": "gt_consumption_ledger.json via task_truth.deep_metrics",
    }


_FACT_EDGE_METHODS = frozenset({
    "same_file", "import", "verified_unique", "type_flow", "lsp",
})


def _normalize_repo_path(path: str) -> str:
    return path.replace("\\", "/").removeprefix("./")


def _join_failed_test_callers(
    failures: list[str], graph_path: str, changed_paths: set[str],
) -> tuple[int | None, int, bool]:
    """Join evaluator-only failed probes to graph-proven callers of edited files.

    No test identifier is returned.  Any missing/ambiguous probe or candidate-only
    edge makes the count unmeasured instead of manufacturing caller attribution.
    """
    changed = {_normalize_repo_path(path) for path in changed_paths if path}
    if not failures or not changed or not os.path.isfile(graph_path):
        return (0, 0, True) if not failures else (None, 0, False)
    joined_node_ids: set[int] = set()
    try:
        with sqlite3.connect(f"file:{graph_path}?mode=ro", uri=True) as connection:
            for identifier in failures:
                parts = identifier.split("::")
                if len(parts) < 2:
                    return None, len(joined_node_ids), False
                test_path = _normalize_repo_path(parts[0])
                test_name = parts[-1].split("[", 1)[0]
                nodes = connection.execute(
                    "SELECT id FROM nodes "
                    "WHERE is_test = 1 AND file_path = ? AND name = ?",
                    (test_path, test_name),
                ).fetchall()
                if len(nodes) != 1:
                    return None, len(joined_node_ids), False
                node_id = int(nodes[0][0])
                targets = connection.execute(
                    "SELECT t.file_path, e.resolution_method, e.confidence "
                    "FROM edges e JOIN nodes t ON t.id = e.target_id "
                    "WHERE e.source_id = ?",
                    (node_id,),
                ).fetchall()
                reaches_edit = any(
                    _normalize_repo_path(str(path)) in changed
                    and str(method) in _FACT_EDGE_METHODS
                    and isinstance(confidence, (int, float))
                    and float(confidence) >= 0.9
                    for path, method, confidence in targets
                )
                if not reaches_edit:
                    return None, len(joined_node_ids), False
                joined_node_ids.add(node_id)
    except (OSError, sqlite3.DatabaseError, TypeError, ValueError):
        return None, len(joined_node_ids), False
    return len(joined_node_ids), len(failures), True


def _build_verifier_truth(
    report: dict | None,
    instance_id: str | None,
    *,
    graph_path: str | None = None,
    changed_paths: set[str] | None = None,
) -> dict[str, Any]:
    """Build count-only interface truth from the official evaluator report.

    The report is evaluator-only data.  Test identifiers never leave this
    function: downstream metric producers receive only a validated denominator
    and failure count.  PASS_TO_PASS failures prove regressions, but they do not
    identify distinct production callers by itself.  Failed probes count only
    after a graph-proven join to an edited production file; zero failures proves
    the empty caller-breakage set directly.
    """
    base: dict[str, Any] = {
        "schema": "gt.verifier_truth.v1",
        "authority": "official_swebench_report.tests_status.PASS_TO_PASS",
        "source_present": False,
        "valid": False,
        "p2p_total": None,
        "p2p_failed": None,
        "caller_breakage_count": None,
        "caller_breakage_unmeasured_reason": "caller_aware_verifier_join_absent",
    }
    if not isinstance(report, dict) or not isinstance(instance_id, str) or not instance_id:
        return base
    entry = report.get(instance_id)
    if not isinstance(entry, dict):
        return base
    base["source_present"] = True
    tests_status = entry.get("tests_status")
    p2p = tests_status.get("PASS_TO_PASS") if isinstance(tests_status, dict) else None
    if not isinstance(p2p, dict):
        return base
    success = p2p.get("success")
    failure = p2p.get("failure")
    if not isinstance(success, list) or not isinstance(failure, list):
        return base
    if any(not isinstance(item, str) or not item for item in success + failure):
        return base
    # Duplicate or contradictory identifiers make the denominator ambiguous.
    if len(set(success)) != len(success) or len(set(failure)) != len(failure):
        return base
    if set(success).intersection(failure):
        return base
    base.update({
        "valid": True,
        "p2p_total": len(success) + len(failure),
        "p2p_failed": len(failure),
    })
    caller_count, joined_failures, join_complete = _join_failed_test_callers(
        failure, graph_path or "", changed_paths or set(),
    )
    base["caller_breakage_count"] = caller_count
    base["caller_joined_failures"] = joined_failures
    base["caller_join_complete"] = join_complete
    if join_complete:
        base.pop("caller_breakage_unmeasured_reason", None)
    return base


def _augment_artifacts_root(jobs_dir: str, artifacts: dict[str, str | None]) -> dict[str, str | None]:
    """Mini-path fallback for the INFRA-stub defect (healthy run mislabelled INFRA).

    ``resolve_trial_artifacts`` globs the pier layout ``jobs/*/*__*/agent/`` for the
    trajectory + patch. On the mini-swe-agent path those sit at the TASK-DIR ROOT (a
    sibling of the ``jobs`` dir passed on the CLI as ``task_truth.py jobs ...``), so the
    glob misses and the truth artifact fails closed to INFRA_MISSING_ARTIFACT even though a
    full-run trajectory (+ patch + reward) is right there. When the resolver missed the mini
    trajectory, look for it at the task root and fill it in so trajectory_integrity /
    trajectory_state / patch_hygiene read the REAL run instead of an all-null stub."""
    roots: list[str] = []
    for r in (jobs_dir, os.path.dirname(os.path.abspath(jobs_dir)), os.getcwd()):
        if r and r not in roots:
            roots.append(r)
    for root in roots:
        cand = os.path.join(root, "mini-swe-agent.trajectory.json")
        if os.path.isfile(cand):
            artifacts["mini_trajectory"] = cand
            if not artifacts.get("trial_dir"):
                artifacts["trial_dir"] = root
            break
    if not artifacts.get("deep_metrics"):
        # The mini workflow writes final deep metrics directly into the explicit
        # task root. Never search sibling task roots: accept the instance-bound
        # name when present, otherwise a unique task-root candidate only.
        task_root = os.path.abspath(jobs_dir)
        instance_id = (
            os.environ.get("GT_INSTANCE_ID")
            or os.environ.get("GT_MATRIX_TASK")
            or ""
        )
        preferred = (
            os.path.join(task_root, f"gt_deep_metrics_{instance_id}.json")
            if instance_id
            else ""
        )
        if preferred and os.path.isfile(preferred):
            artifacts["deep_metrics"] = preferred
        else:
            candidates = sorted(
                glob.glob(os.path.join(task_root, "gt_deep_metrics_*.json"))
            )
            if len(candidates) == 1:
                artifacts["deep_metrics"] = candidates[0]
    return artifacts


def _task_graph_path(jobs_dir: str) -> str | None:
    """Locate only the task-scoped graph; never search sibling task roots."""
    task_root = os.path.abspath(jobs_dir)
    candidate = os.path.join(task_root, "graph.db")
    return candidate if os.path.isfile(candidate) else None


def _root_reward_fallback(jobs_dir: str) -> float | None:
    """The task-root ``reward.txt`` (mini path), parsed as a float. None when absent/blank
    (missing-data law) — used only when the pier ``result.json`` carried no reward."""
    for r in (jobs_dir, os.path.dirname(os.path.abspath(jobs_dir)), os.getcwd()):
        cand = os.path.join(r, "reward.txt") if r else ""
        if cand and os.path.isfile(cand):
            try:
                txt = open(cand, encoding="utf-8").read().strip()
                return float(txt) if txt else None
            except (OSError, ValueError):
                return None
    return None


def build_task_truth(
    jobs_dir: str,
    *,
    instance_id: str | None = None,
    trial_log: str = "",
    cert_dir: str | None = None,
    patch_hygiene: dict | None = None,
    grade_outcome: bool = True,
) -> dict:
    """Assemble reconciled per-task truth record.

    grade_outcome=False is the OH / non-DeepSWE path: grade ONLY the substrate
    (graph handoff via the runtime witness) and DEFER outcome to eval_result.json.
    The DeepSWE outcome classifier (detect_infra_subtype / _detect_eval_no_report)
    globs the pier layout jobs/*/*__*/agent/*.trajectory.json, which is absent on an
    OpenHands results dir (output.jsonl / test_result.git_patch); left on, it fails
    closed to INFRA_MISSING_ARTIFACT and mislabels a real agent run as infra. The
    substrate reconcile is unaffected — reconcile_graph_handoff reads only the
    witness fields, populated regardless of grade_outcome.
    """
    do = _load_deepswe_outcome()

    artifacts = _find_trial_artifacts(jobs_dir, instance_id=instance_id)
    # Mini-path fallback: fill in the task-root mini trajectory when the pier glob missed it.
    artifacts = _augment_artifacts_root(jobs_dir, artifacts)
    reward: float | None = None
    n_agent_steps: int | None = None
    exit_status: str | None = None
    iid = instance_id

    if artifacts.get("result_json"):
        d = _load_json(artifacts["result_json"]) or {}
        info = d.get("info") or {}
        vr = d.get("verifier_result") or {}
        reward = (vr.get("rewards") or {}).get("reward")
        n_agent_steps = d.get("n_agent_steps")
        exit_status = info.get("exit_status")
        if not iid:
            iid = do.extract_instance_id(d, info, trial_dir=artifacts.get("trial_dir"))
    # Mini path: no pier result.json → read reward/exit_status/instance_id from the mini
    # trajectory itself so the truth artifact grades the REAL run, not an INFRA stub.
    if artifacts.get("mini_trajectory"):
        md = _load_json(artifacts["mini_trajectory"]) or {}
        minfo = md.get("info") or {}
        if exit_status is None:
            exit_status = minfo.get("exit_status")
        if not iid:
            iid = minfo.get("instance_id") or iid
        # The mini path carries the agent step count as info.model_stats.api_calls (never
        # a pier n_agent_steps). Map it so a report-graded reward=0 run classifies as a
        # genuine AGENT/GT non-resolve (IN the denominator), not UNKNOWN-excluded.
        if n_agent_steps is None:
            api_calls = (minfo.get("model_stats") or {}).get("api_calls")
            if isinstance(api_calls, int) and api_calls > 0:
                n_agent_steps = api_calls
            else:
                _msgs = md.get("messages") or []
                _assist = sum(
                    1 for m in _msgs if isinstance(m, dict)
                    and (m.get("role") == "assistant"
                         or (m.get("role") is None and isinstance(m.get("output"), list)))
                )
                if _assist > 0:
                    n_agent_steps = _assist

    # ── Harness-truth binding (run 29236533134 defect-1) ────────────────────
    # The pier result.json/instance_id is absent on the mini path → instance_id
    # came through null on 29/29 tasks and every task was auto-stamped INFRA even
    # though a report.json + reward + trajectory sat at the task root. Recover the
    # identity from the report key / matrix env / task dir name, and take the
    # reward from the task-root reward.txt or the report's resolved verdict.
    report_path, report = do.find_task_root_report(jobs_dir)
    iid = do.bind_instance_identity(jobs_dir, current=iid, report=report)
    if reward is None:
        reward = _root_reward_fallback(jobs_dir)
    if reward is None:
        reward = do.root_reward(jobs_dir)
    report_resolved = do.report_resolved(report, iid)
    if reward is None and report_resolved is not None:
        reward = 1.0 if report_resolved else 0.0

    if not trial_log:
        log_path = os.environ.get("GT_TRIAL_LOG")
        if log_path and os.path.isfile(log_path):
            trial_log = do._read_trial_log(log_path)  # noqa: SLF001

    if not cert_dir:
        cert_dir = os.environ.get("GT_CERT_DIR")
        if not cert_dir or not os.path.isdir(cert_dir):
            cert_dir = "/tmp/gt" if os.path.isdir("/tmp/gt") else None

    _mini_turns = _turns_from_mini_trajectory(artifacts.get("mini_trajectory"))
    truth_reconciliation: dict[str, Any] | None = None
    if grade_outcome:
        eval_no_report = do._detect_eval_no_report(jobs_dir)  # noqa: SLF001
        infra_subtype = do.detect_infra_subtype(jobs_dir, trial_log)
        # CONTRADICTION GUARD (defect-1): the outcome classifier globs the pier layout and, on
        # the mini path, calls a HEALTHY run INFRA_MISSING_ARTIFACT even though a report.json
        # (the eval VERDICT) sits at the task root. The eval report is the grading authority:
        # when it is present, grade from report.json/reward — never INFRA. A genuine resolve
        # (reward>=1) also disproves INFRA. But a task with NO report AND reward<1 (the eval
        # produced no verdict, e.g. checkov-6893) stays true INFRA — trajectory presence alone
        # does NOT manufacture an eval verdict. The reconciliation is recorded, not silent.
        _traj_present = len(_mini_turns) > 0 or bool(artifacts.get("mini_trajectory"))
        _report_present = report is not None
        _genuine_resolve = reward is not None and float(reward) >= 1.0
        if (infra_subtype and "MISSING" in str(infra_subtype).upper()
                and (_report_present or _genuine_resolve)):
            _reason = (
                f"infra_missing_artifact contradicted by the eval verdict — report.json "
                f"present={_report_present} (resolved={report_resolved}), reward={reward}, "
                f"trajectory_present={_traj_present}"
            )
            truth_reconciliation = {
                "reconciled": True,
                "original_infra_subtype": infra_subtype,
                "reason": _reason,
                "report_json": report_path,
                "mini_trajectory": artifacts.get("mini_trajectory"),
            }
            infra_subtype = None
    else:
        # OH path: never run the DeepSWE-shaped outcome classifier (it would
        # fail closed to INFRA_MISSING_ARTIFACT on an OH results dir). Outcome
        # is deferred to eval_result.json; only the substrate is graded here.
        eval_no_report = False
        infra_subtype = None

    signal = do.build_signal_record(
        instance_id=iid,
        reward=reward,
        n_agent_steps=n_agent_steps,
        exit_status=exit_status,
        trial_log=trial_log,
        cert_dir=cert_dir,
        eval_no_report=eval_no_report,
        infra_subtype=infra_subtype,
    )

    deep = _load_json(artifacts.get("deep_metrics") or "") or {}
    traj_int = _trajectory_integrity(artifacts)
    reconciled = reconcile_graph_handoff(signal)
    # Preserve the single artifact selection made at function entry. Re-resolving
    # with the normalized result identity can reject a valid pier directory whose
    # basename carries an attempt suffix (for example task__abc), silently dropping
    # its runtime ledger and producing a cross-surface partial truth record.
    arts = TrialArtifacts(**{
        field: artifacts.get(field)
        for field in TrialArtifacts.__dataclass_fields__
    })
    brief_prov = brief_provenance(arts)
    retry_n = int(os.environ.get("GT_RETRY_ON_VERIFIER_FAIL") or "0")
    verifier_semantics = {
        "self_verifier_retry": retry_n > 0,
        "official_verifier_repair": False,
        "retry_count_configured": retry_n,
        "note": (
            "self_verifier_retry = in-loop GT_RETRY harness (agent re-run with test feedback); "
            "official_verifier_repair = pier/SWE-bench post-submit verifier only"
        ),
    }

    obl_path = os.environ.get("GT_OBLIGATION_STATUS", "/tmp/gt/obligation_status.json")
    if not os.path.isfile(obl_path):
        cand = os.path.join(os.path.dirname(artifacts.get("trial_dir") or ""), "obligation_status.json")
        if cand and os.path.isfile(cand):
            obl_path = cand
    obligation_status = _load_json(obl_path) if obl_path else None

    if patch_hygiene is None:
        patch_hygiene = _patch_hygiene_from_artifacts(artifacts) if grade_outcome else {
            "classification": "deferred_to_eval",
            "files": [],
            "summary": {"source_fix": 0, "lockfile": 0, "generated": 0, "noise": 0},
            "note": "patch graded by eval_result.json (OH/non-DeepSWE path)",
        }

    # Applied-patch edit-truth authority (DEFECT-1): the diff's changed SOURCE paths + a
    # non-empty model patch. Reconciles source_edit_count / first_source_edit_step for edits
    # applied via sed/patch/heredoc that shell-command inference misses.
    _changed_source_paths = {
        str(row.get("path"))
        for row in (patch_hygiene or {}).get("files", [])
        if isinstance(row, dict)
        and row.get("class") == "source_fix"
        and isinstance(row.get("path"), str)
        and row.get("path")
    }
    _has_patch = bool(patch_hygiene) and (patch_hygiene.get("classification")
                                          not in (None, "missing_model_patch", "deferred_to_eval"))
    trajectory_summary = _trajectory_state_summary(
        artifacts,
        changed_source_paths=_changed_source_paths,
        has_patch=_has_patch,
    )
    obligation_summary = _obligation_lifecycle_summary(obligation_status)
    consumption_summary = _consumption_summary(deep)
    horizon_summary = _verification_horizon_summary(deep, verifier_semantics)
    runtime_ledger_summary = _runtime_ledger_summary(arts.runtime_ledger)
    structured_adapter_witness = _load_json(arts.adapter_witness or "") or {}

    if grade_outcome:
        outcome_block = {
            "reward": signal.get("reward"),
            "resolved": signal.get("failure_class") == "RESOLVED",
            "failure_class": signal.get("failure_class"),
            "infra_subtype": signal.get("infra_subtype"),
            "in_resolved_denominator": signal.get("in_resolved_denominator"),
        }
    else:
        outcome_block = {
            "reward": None,
            "resolved": None,
            "failure_class": None,
            "infra_subtype": None,
            "in_resolved_denominator": None,
            "note": (
                "outcome authority = eval_result.json (OH/non-DeepSWE path); "
                "DeepSWE outcome classifier not run"
            ),
        }

    return {
        "schema": "gt.task_truth.v1",
        "authority": _truth_authority_map(),
        "instance_id": iid,
        "certs": signal.get("cert_verdicts") or {},
        "runtime_witness": {
            "gt_prebuilt_active": signal.get("gt_prebuilt_active"),
            "hook_hash_match": signal.get("hook_hash_match"),
            "gt_meta_present": signal.get("gt_meta_present"),
            "cert_fail_reconciled": signal.get("cert_fail_reconciled"),
        },
        "deep_metrics": {
            "path": artifacts.get("deep_metrics"),
            "outcome": deep.get("outcome"),
            "resolved": deep.get("resolved"),
            "gt_delivery": deep.get("gt_delivery"),
            "gt_blocks_delivered": deep.get("gt_blocks_delivered"),
            "gt_blocks_consumed": deep.get("gt_blocks_consumed"),
            "gt_blocks_verification_followup": deep.get(
                "gt_blocks_verification_followup"
            ),
            "gt_blocks_hard_enforced": deep.get("gt_blocks_hard_enforced"),
            "gt_blocks_enforced": deep.get("gt_blocks_enforced"),
            "gt_enforcement_semantics": deep.get("gt_enforcement_semantics"),
        },
        "trajectory_state": trajectory_summary,
        "runtime_control": {
            "phase_policy_version": PHASE_POLICY_VERSION,
            "trajectory_state_summary": trajectory_summary,
            "obligation_lifecycle_summary": obligation_summary,
            "verification_horizon_summary": horizon_summary,
            "runtime_ledger_summary": runtime_ledger_summary,
            "consumption_summary": consumption_summary,
            "enforcement_semantics": {
                "pre_submit_intervention": horizon_summary["pre_submit_intervention"],
                "hard_enforced": horizon_summary["hard_enforced"],
                "official_verifier_repair": horizon_summary["official_verifier_repair"],
                "self_verifier_retry": horizon_summary["self_verifier_retry"],
            },
            "adapter_witness": {
                "gt_prebuilt_active": signal.get("gt_prebuilt_active"),
                "hook_hash_match": signal.get("hook_hash_match"),
                "gt_meta_present": signal.get("gt_meta_present"),
                "structured_path": arts.adapter_witness,
                "structured": structured_adapter_witness,
            },
        },
        "outcome": outcome_block,
        "truth_reconciliation": truth_reconciliation,
        "trajectory_integrity": traj_int,
        "patch_hygiene": patch_hygiene or {},
        "reconciled": reconciled,
        "brief_provenance": brief_prov,
        "verifier_truth": _build_verifier_truth(
            report,
            iid,
            graph_path=_task_graph_path(jobs_dir),
            changed_paths={
                str(row.get("path"))
                for row in (patch_hygiene or {}).get("files", [])
                if isinstance(row, dict)
                and row.get("class") == "source_fix"
                and isinstance(row.get("path"), str)
            },
        ),
        "verifier_semantics": verifier_semantics,
        "oracle_events_status": {
            "path": arts.oracle_events,
            "present": bool(arts.oracle_events and os.path.isfile(arts.oracle_events)),
        },
        "obligation_status": obligation_status or {},
        "signals": signal,
    }


def write_task_truth(jobs_dir: str, out_path: str | None = None, **kwargs: Any) -> str:
    """Build and persist task_truth.json; return output path."""
    truth = build_task_truth(jobs_dir, **kwargs)
    if not out_path:
        trial_dir = _find_trial_artifacts(jobs_dir).get("trial_dir")
        out_path = os.path.join(trial_dir or jobs_dir, "task_truth.json")
    out_dir = os.path.dirname(os.path.abspath(out_path))
    temp_path = ""
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=out_dir, prefix=".task_truth.",
            suffix=".tmp", delete=False,
        ) as fh:
            temp_path = fh.name
            json.dump(truth, fh, indent=2)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(temp_path, out_path)
        temp_path = ""
    finally:
        if temp_path:
            try:
                os.unlink(temp_path)
            except OSError:
                pass
    write_reconciled_substrate_verdict(truth, os.path.dirname(out_path))
    return out_path


def build_reconciled_substrate_verdict(truth: dict[str, Any]) -> dict[str, Any]:
    """Single dashboard-facing substrate verdict after witness reconciliation (P0-07)."""
    certs = truth.get("certs") or {}
    graph_cert = certs.get("graph_certificate.json") or {}
    lsp_cert = certs.get("lsp_certificate.json") or {}
    reconciled = truth.get("reconciled") or {}
    graph_handoff = reconciled.get("graph_handoff", "unproven")
    return {
        "schema": "gt.reconciled_substrate_verdict.v1",
        "instance_id": truth.get("instance_id"),
        "graph_handoff": graph_handoff,
        "witness_holds": reconciled.get("witness_holds"),
        "contradictions": reconciled.get("contradictions") or [],
        "graph_certificate_verdict": graph_cert.get("verdict"),
        "lsp_certificate_verdict": lsp_cert.get("verdict"),
        "outcome_failure_class": (truth.get("outcome") or {}).get("failure_class"),
        "in_resolved_denominator": (truth.get("outcome") or {}).get("in_resolved_denominator"),
        "authority": "task_truth.json",
        "authority_map": truth.get("authority") or _truth_authority_map(),
    }


def write_reconciled_substrate_verdict(
    truth: dict[str, Any], out_dir: str
) -> str:
    """Write reconciled_substrate_verdict.json beside task_truth."""
    path = os.path.join(out_dir, "reconciled_substrate_verdict.json")
    payload = build_reconciled_substrate_verdict(truth)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2)
        fh.write("\n")
    return path


def main(argv: list[str] | None = None) -> int:
    import sys

    args = (argv or sys.argv)[1:]
    jobs_dir = args[0] if args else "jobs"
    out_path = args[1] if len(args) > 1 else None
    if not os.path.isdir(jobs_dir):
        print(f"skip: jobs dir missing: {jobs_dir}", file=sys.stderr)
        return 0
    path = write_task_truth(jobs_dir, out_path=out_path)
    print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
