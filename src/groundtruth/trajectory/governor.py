"""L5 trajectory governor — single dispatch point for all L5 hooks."""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any

from .state import L5TrajectoryState, IterationBand, FailureSnapshot
from .classifier import (
    classify_observation,
    classify_command,
    classify_verification_targeting,
    is_verification_command,
    CommandKind,
    VerificationTarget,
)
from .parsers import parse_failures, FailureRecord
from . import hooks


@dataclass
class L5Decision:
    """Typed return from governor. Wrapper reads this to emit L5+L5b events."""

    hook_name: str = ""
    fired: bool = False
    suppressed: bool = False
    suppression_reason: str | None = None
    message: str | None = None
    next_action_type: str | None = None
    next_action_text: str | None = None
    next_action_file: str | None = None
    next_action_command: str | None = None
    next_action_test: str | None = None
    evidence_items: list[dict] = field(default_factory=list)
    trigger_reason: str = ""
    verification_kind: str | None = None
    edited_file: str | None = None
    command: str | None = None


def _is_source_edit(path: str) -> bool:
    if not path:
        return False
    ext = os.path.splitext(path)[1].lower()
    source_exts = {
        ".py", ".js", ".ts", ".jsx", ".tsx", ".go", ".rs", ".java",
        ".c", ".cpp", ".h", ".hpp", ".rb", ".php", ".swift", ".kt",
        ".scala", ".cs", ".yml", ".yaml", ".toml", ".json", ".cfg",
    }
    if ext not in source_exts:
        return False
    fname = os.path.basename(path).lower()
    scaffolds = ("reproduce", "debug_", "tmp_", "test_fix", "repro_")
    if any(fname.startswith(s) for s in scaffolds):
        return False
    return True


def _extract_command(action: Any) -> str:
    if hasattr(action, "command"):
        return str(action.command or "")
    if hasattr(action, "content"):
        return str(action.content or "")
    return ""


def _extract_observation_text(obs: Any) -> str:
    text = getattr(obs, "content", "") or ""
    if not text:
        text = getattr(obs, "stdout", "") or ""
    return str(text)


def _get_edited_path_from_action(action: Any) -> str:
    if hasattr(action, "path"):
        return str(action.path or "")
    text = _extract_command(action)
    m = re.search(r"str_replace_editor.*?path=\"([^\"]+)\"", text)
    if m:
        return m.group(1)
    m = re.search(r"(?:create|str_replace|insert|write)\s+(\S+\.(?:py|js|ts|go|rs|java|rb|c|cpp|h))", text)
    if m:
        return m.group(1)
    return ""


def _action_class_name(action: Any) -> str:
    return type(action).__name__


def _is_finish_action(action: Any) -> bool:
    cls = _action_class_name(action)
    return cls in ("AgentFinishAction", "FinishAction")


_NO_DECISION = L5Decision()


class L5Governor:
    """Trajectory governor — decides WHEN to intervene, calls L3/L3b for WHAT."""

    def __init__(self, instance_id: str, max_iter: int = 100) -> None:
        self.state = L5TrajectoryState.load_or_create(instance_id, max_iter)
        self._log_entries: list[dict[str, Any]] = []

    def after_interaction(
        self,
        action: Any,
        obs: Any,
        action_count: int,
        max_iter: int,
        *,
        edited_files: set[str] | None = None,
        brief_candidates: set[str] | None = None,
        viewed_files: set[str] | None = None,
        graph_db: str = "",
        workspace_root: str = "",
    ) -> L5Decision:
        self.state.update_iter(action_count, max_iter)

        if self.state._injection_disabled:
            self._log("disabled", "", suppressed=self.state._disable_reason)
            return _NO_DECISION

        # Early scaffold trap: 20+ actions with 0 source edits
        if (
            not self.state.edited_source_files
            and action_count >= 20
            and not getattr(self, "_scaffold_trap_fired", False)
            and action_count / max(max_iter, 1) >= 0.20
        ):
            self._scaffold_trap_fired = True
            msg = (
                f"[GT L5: No Source Edits]\n"
                f"Iteration: {action_count}/{max_iter}\n"
                f"You have run {action_count} actions with 0 source file edits.\n"
                f"Focus on identifying and editing the fix target directly.\n"
                f"Use `gt_query <symbol>` to find the right function."
            )
            self._log("scaffolding_trap_early", msg)
            return L5Decision(
                hook_name="scaffolding_trap_early",
                fired=True,
                message=msg,
                trigger_reason=f"no_source_edit_after_{action_count}_actions",
            )

        if _is_finish_action(action):
            return self._handle_finish()

        cls_name = _action_class_name(action)

        if cls_name == "CmdRunAction":
            return self._handle_command(action, obs, graph_db=graph_db)

        path = _get_edited_path_from_action(action)
        if cls_name in ("FileEditAction", "FileWriteAction") or (
            cls_name == "CmdRunAction" and path
        ):
            if _is_source_edit(path):
                self.state.record_source_edit(path)
                return self._handle_source_edit(
                    path,
                    edited_files=edited_files,
                    brief_candidates=brief_candidates,
                    viewed_files=viewed_files,
                    graph_db=graph_db,
                )
            elif path:
                return self._handle_non_source_edit(path)

        self.state.save()
        return _NO_DECISION

    def _build_decision(
        self, hook_name: str, msg: str | None, *,
        trigger_reason: str = "",
        verification_kind: str | None = None,
        edited_file: str | None = None,
        command: str | None = None,
        graph_db: str = "",
    ) -> L5Decision:
        """Build L5Decision from hook output, apply safety checker."""
        if msg is None:
            return _NO_DECISION

        from .hooks import L5bSafetyChecker
        ratio = self.state.current_iter / max(self.state.max_iter, 1)
        is_safe, reason = L5bSafetyChecker.validate(msg, ratio)

        next_action_text = self._extract_next_action(msg)
        next_action_type: str | None = None
        next_action_file: str | None = None
        next_action_test: str | None = None

        # Structural-first next_action (Decision 32)
        suggestions = self._get_structural_suggestions(graph_db)
        if suggestions.get("next_action_type"):
            next_action_type = suggestions["next_action_type"]
            next_action_file = suggestions.get("next_action_file")
        elif next_action_text:
            # Fallback: parse from rendered text (lowest priority)
            if "run" in next_action_text.lower() and "test" in next_action_text.lower():
                next_action_type = "RUN_TARGETED_TEST"
            elif "read" in next_action_text.lower() or "inspect" in next_action_text.lower():
                next_action_type = "READ_CALLER_CONTRACT"

        self.state.record_l5_emission(hook_name)
        self._log(hook_name, msg)

        if is_safe:
            return L5Decision(
                hook_name=hook_name, fired=True, suppressed=False,
                message=msg,
                next_action_type=next_action_type, next_action_text=next_action_text,
                next_action_file=next_action_file, next_action_test=next_action_test,
                trigger_reason=trigger_reason, verification_kind=verification_kind,
                edited_file=edited_file, command=command,
            )
        else:
            self._log("l5b_safety_blocked", "", suppressed=reason or "safety_check_failed")
            return L5Decision(
                hook_name=hook_name, fired=True, suppressed=True,
                suppression_reason=f"l5b_safety_check:{reason}",
                message=None,
                next_action_type=next_action_type, next_action_text=next_action_text,
                trigger_reason=trigger_reason, verification_kind=verification_kind,
                edited_file=edited_file, command=command,
            )

    def _handle_command(
        self,
        action: Any,
        obs: Any,
        *,
        graph_db: str = "",
    ) -> L5Decision:
        command = _extract_command(action)
        obs_text = _extract_observation_text(obs)

        if not is_verification_command(command):
            self.state.save()
            return _NO_DECISION

        classification = classify_observation(command, obs_text)

        if classification.is_env_failure:
            self._log("env_failure_suppressed", command)
            self.state.save()
            return _NO_DECISION

        passed = not classification.is_failure
        failure_record: FailureRecord | None = None

        targeting = classify_verification_targeting(
            command, list(self.state.edited_source_files),
        )
        target_level = targeting.value

        if not passed:
            records = parse_failures(command, obs_text)
            failure_record = records[0] if records else None

            snapshot = FailureSnapshot(
                command_kind=classification.command_kind,
                failure_kind=failure_record.failure_kind if failure_record else "unknown",
                failing_unit=failure_record.failing_unit if failure_record else "",
                assertion_or_error=failure_record.assertion_or_error if failure_record else "",
                expected=failure_record.expected if failure_record else "",
                actual=failure_record.actual if failure_record else "",
                exception_type=failure_record.exception_type if failure_record else "",
                top_project_frame=failure_record.top_project_frame if failure_record else "",
                raw_excerpt=failure_record.raw_excerpt[:300] if failure_record else obs_text[-300:],
                iter_observed=self.state.current_iter,
            )
            self.state.record_verification(False, snapshot, target_level=target_level)
        else:
            self.state.record_verification(True, target_level=target_level)

            if not targeting.is_targeted() and os.environ.get("GT_REBUILD_L5", "0") == "1":
                structural = self._get_structural_suggestions(graph_db)
                msg = hooks.hook_unverified_patch(
                    self.state,
                    test_file_suggestions=[structural.get("next_action_file", "")] if structural.get("next_action_file") else [],
                )
                if msg:
                    decision = self._build_decision(
                        "unverified_patch", msg,
                        trigger_reason="broad_pass_after_edit",
                        verification_kind=target_level,
                        command=command,
                        graph_db=graph_db,
                    )
                    self.state.save()
                    return decision

            self.state.save()
            return _NO_DECISION

        result = self._try_hooks_after_failure(failure_record, command=command, graph_db=graph_db)
        self.state.save()
        return result

    def _try_hooks_after_failure(
        self,
        failure_record: FailureRecord | None,
        *,
        command: str = "",
        graph_db: str = "",
    ) -> L5Decision:
        msg = hooks.hook_same_failure_persisted(self.state, failure_record)
        if msg:
            return self._build_decision(
                "same_failure_persisted", msg,
                trigger_reason="repeated_failure", command=command,
            )

        if self.state.has_source_edit_before_last_failure and failure_record:
            msg = hooks.hook_hypothesis_falsified(self.state, failure_record)
            if msg:
                self.state.has_source_edit_before_last_failure = False
                return self._build_decision(
                    "hypothesis_falsified", msg,
                    trigger_reason="test_failure_after_edit", command=command,
                )

        self.state.save()
        return _NO_DECISION

    def _handle_source_edit(
        self,
        path: str,
        *,
        edited_files: set[str] | None = None,
        brief_candidates: set[str] | None = None,
        viewed_files: set[str] | None = None,
        graph_db: str = "",
    ) -> L5Decision:
        confirming = 0
        if viewed_files and brief_candidates:
            for v in viewed_files:
                if any(bc in v for bc in brief_candidates):
                    confirming += 1

        msg = hooks.hook_premature_commitment(self.state, path, confirming)
        if msg:
            return self._build_decision(
                "premature_commitment", msg,
                trigger_reason="source_edit_before_confirming", edited_file=path,
            )

        self.state.save()
        return _NO_DECISION

    def _handle_non_source_edit(self, path: str) -> L5Decision:
        msg = hooks.hook_no_durable_source_progress(self.state, path)
        if msg:
            return self._build_decision(
                "no_durable_source_progress", msg,
                trigger_reason="non_source_edit", edited_file=path,
            )
        self.state.save()
        return _NO_DECISION

    def _handle_finish(self) -> L5Decision:
        msg = hooks.hook_unsafe_finish(self.state)
        if msg:
            return self._build_decision(
                "unsafe_finish", msg,
                trigger_reason="finish_with_unresolved_or_unverified",
            )
        # Mechanism #4: Multi-file edit warning
        # Check if edited files have high-confidence callers in files NOT edited
        scope_msg = self._check_multi_file_scope()
        if scope_msg:
            return L5Decision(
                hook_name="multi_file_scope_warning",
                fired=True,
                message=scope_msg,
                trigger_reason="caller_in_unedited_file",
            )
        self.state.save()
        return _NO_DECISION

    def _check_multi_file_scope(self) -> str:
        """Warn if edited files have callers in files the agent didn't edit."""
        edited = set(self.state.edited_source_files)
        if not edited:
            return ""
        graph_db = os.environ.get("GT_GRAPH_DB", "/tmp/gt_index.db")
        if not os.path.exists(graph_db):
            return ""
        try:
            import sqlite3
            conn = sqlite3.connect(graph_db)
            warnings = []
            for ef in list(edited)[:3]:
                rows = conn.execute(
                    """SELECT DISTINCT nsrc.file_path, COUNT(*) as cnt
                    FROM nodes nt
                    JOIN edges e ON e.target_id = nt.id AND e.type = 'CALLS'
                      AND COALESCE(e.confidence, 0.5) >= 0.9
                    JOIN nodes nsrc ON e.source_id = nsrc.id
                    WHERE nt.file_path = ? AND nsrc.file_path != ?
                      AND nsrc.is_test = 0
                    GROUP BY nsrc.file_path
                    HAVING cnt >= 2
                    ORDER BY cnt DESC LIMIT 3""",
                    (ef, ef),
                ).fetchall()
                for caller_file, cnt in rows:
                    if caller_file not in edited:
                        warnings.append(f"  {caller_file} ({cnt} calls into {os.path.basename(ef)})")
            conn.close()
            if warnings:
                return (
                    "[GT L5: Scope Check]\n"
                    "You edited files with callers in OTHER files you didn't touch:\n"
                    + "\n".join(warnings[:3])
                    + "\nVerify these callers still work with your changes."
                )
        except Exception:
            pass
        return ""

    def _get_structural_suggestions(self, graph_db: str) -> dict[str, str | None]:
        """Query graph.db for structural witnesses: callers first, then consumers, then tests.

        Returns dict with next_action_type and next_action_file.
        """
        result: dict[str, str | None] = {"next_action_type": None, "next_action_file": None}
        if not graph_db or not os.path.exists(graph_db):
            return result
        try:
            import sqlite3
            conn = sqlite3.connect(graph_db)
            for edited in self.state.edited_source_files[-2:]:
                norm = edited.replace("\\", "/")
                if norm.startswith("/"):
                    norm = norm.lstrip("/")
                # Priority 1: callers (files that CALL functions in the edited file)
                rows = conn.execute(
                    """SELECT DISTINCT nsrc.file_path
                       FROM nodes nt
                       JOIN edges e ON e.target_id = nt.id AND e.type = 'CALLS'
                         AND COALESCE(e.confidence, 0.5) >= 0.5
                       JOIN nodes nsrc ON e.source_id = nsrc.id
                       WHERE nt.file_path LIKE ? AND nsrc.file_path NOT LIKE ?
                       LIMIT 3""",
                    (f"%{norm}", f"%{norm}"),
                ).fetchall()
                if rows:
                    result["next_action_type"] = "READ_CALLER_CONTRACT"
                    result["next_action_file"] = rows[0][0]
                    conn.close()
                    return result
                # Priority 2: consumers/importers
                rows = conn.execute(
                    """SELECT DISTINCT nsrc.file_path
                       FROM nodes nt
                       JOIN edges e ON e.target_id = nt.id AND e.type = 'IMPORTS'
                       JOIN nodes nsrc ON e.source_id = nsrc.id
                       WHERE nt.file_path LIKE ? AND nsrc.file_path NOT LIKE ?
                       LIMIT 3""",
                    (f"%{norm}", f"%{norm}"),
                ).fetchall()
                if rows:
                    result["next_action_type"] = "READ_CONSUMER"
                    result["next_action_file"] = rows[0][0]
                    conn.close()
                    return result
                # Priority 3: test files
                rows = conn.execute(
                    """SELECT DISTINCT n2.file_path
                       FROM nodes n1
                       JOIN edges e ON (e.source_id = n1.id OR e.target_id = n1.id)
                       JOIN nodes n2 ON (
                           CASE WHEN e.source_id = n1.id THEN e.target_id ELSE e.source_id END = n2.id
                       )
                       WHERE n1.file_path LIKE ? AND n2.is_test = 1
                       LIMIT 3""",
                    (f"%{norm}",),
                ).fetchall()
                if rows:
                    result["next_action_type"] = "RUN_TARGETED_TEST"
                    result["next_action_file"] = rows[0][0]
                    conn.close()
                    return result
            conn.close()
        except Exception:
            pass
        return result

    # --- Decision 34: Generalized event-driven dispatch ---

    def goku_check(
        self,
        action: Any,
        obs: Any,
        action_count: int,
        max_iter: int,
        *,
        file_path: str | None = None,
        diff_size: int | None = None,
    ) -> L5Decision:
        """Generalized P0 event checks. Gated by GT_L5_GOKU_EVENTS=1.

        L5 decides WHEN. Uses latest known L3/L3b next_action from state.
        Does NOT query graph.db for new evidence.
        Populates its own state from the actions it sees.
        """
        if os.environ.get("GT_L5_GOKU_EVENTS", "0") != "1":
            return _NO_DECISION

        self.state.update_iter(action_count, max_iter)

        if self.state._injection_disabled:
            return _NO_DECISION

        # --- State population: goku_check must feed itself ---
        cls_name = _action_class_name(action)

        # Record source edits
        if cls_name in ("FileEditAction", "FileWriteAction") and file_path:
            if _is_source_edit(file_path):
                self.state.record_source_edit(file_path)

        # Record verification commands
        if cls_name == "CmdRunAction":
            from .classifier import is_verification_command, classify_verification_targeting
            command = _extract_command(action)
            if is_verification_command(command):
                obs_text = _extract_observation_text(obs)
                from .classifier import classify_observation
                classification = classify_observation(command, obs_text)
                passed = not classification.is_failure
                targeting = classify_verification_targeting(
                    command, list(self.state.edited_source_files),
                )
                self.state.record_verification(passed, target_level=targeting.value)

        # Track diff snapshots for patch collapse detection
        if diff_size is not None:
            self.state.record_diff_snapshot(diff_size)

        # Track action signatures for loop detection
        sig = f"{cls_name}:{file_path or ''}"
        self.state.record_action_signature(sig)

        # Track agent actions relative to structural witness
        if self.state.latest_gt_next_action_type:
            self.state.record_action_after_gt(file_path)

        # P0 checks in priority order

        # 1. Patch collapsed
        if self.state.patch_collapsed and not self.state.durable_edit_lost:
            decision = self._try_goku_emit(
                "PATCH_COLLAPSED_OR_LOST", "HIGH",
                hooks.hook_patch_collapsed_or_lost(self.state),
                trigger_reason="diff_nonzero_to_zero",
            )
            self.state.durable_edit_lost = True  # fire once
            if decision.fired:
                self.state.save()
                return decision

        # 2. Finish without structural witness
        if _is_finish_action(action):
            decision = self._try_goku_emit(
                "FINISH_WITH_UNVERIFIED_EDIT", "HIGH",
                hooks.hook_finish_without_structural_witness(self.state),
                trigger_reason="finish_no_witness",
            )
            if decision.fired:
                self.state.save()
                return decision

        # 3. Structural witness ignored (3+ actions without following)
        if (
            self.state.latest_gt_next_action_type
            and self.state.actions_since_gt_next_action >= 3
            and not self.state.structural_witness_followed
        ):
            decision = self._try_goku_emit(
                "STRUCTURAL_WITNESS_IGNORED", "HIGH",
                hooks.hook_structural_witness_ignored(
                    self.state,
                    witness_file=self.state.latest_gt_next_action_file,
                ),
                trigger_reason="witness_ignored_3_actions",
            )
            if decision.fired:
                self.state.save()
                return decision

        # 4. Weak verification after edit
        if self.state.has_unverified_patch():
            confidence = "HIGH" if self.state.band in (
                IterationBand.LATE_REPAIR, IterationBand.FINALIZATION,
            ) else "MEDIUM"
            decision = self._try_goku_emit(
                "WEAK_VERIFICATION_AFTER_EDIT", confidence,
                hooks.hook_weak_verification_after_edit(self.state),
                trigger_reason="broad_pass_no_targeted",
            )
            if decision.fired:
                self.state.save()
                return decision

        # 5. No durable progress (late/final only)
        if not self.state.edited_source_files and self.state.band in (
            IterationBand.LATE_REPAIR, IterationBand.FINALIZATION,
        ):
            decision = self._try_goku_emit(
                "NO_DURABLE_PROGRESS", "HIGH",
                hooks.hook_no_durable_progress_goku(self.state),
                trigger_reason="no_source_edit_late_band",
            )
            if decision.fired:
                self.state.save()
                return decision

        self.state.save()
        return _NO_DECISION

    def _try_goku_emit(
        self,
        event_type: str,
        confidence_level: str,
        message: str | None,
        *,
        trigger_reason: str = "",
    ) -> L5Decision:
        """Attempt to emit a Goku L5 event.

        Context budget rule (beets-5495 regression fix):
        L5b injections consume agent context window tokens.
        Most detections → structured-only (logged to JSONL, zero context cost).
        Only HIGH + LATE/FINAL + concrete next_action + max 2 injections → inject.
        """
        if message is None:
            return _NO_DECISION

        from ..telemetry.constants import L5_MAX_INJECTIONS_PER_TASK

        # Every detection is recorded as a structured event (fired=True)
        # but only some get message injected into agent context (suppressed=False)

        # Gate 1: confidence — only HIGH can ever inject
        if confidence_level != "HIGH":
            self._log(f"goku_{event_type}", "", suppressed=f"structured_only:confidence={confidence_level}")
            self.state.record_l5_goku_emission(event_type)
            return L5Decision(
                hook_name=f"goku_{event_type}", fired=True, suppressed=True,
                suppression_reason=f"structured_only:confidence={confidence_level}",
                trigger_reason=trigger_reason,
            )

        # Gate 2: band — only LATE_REPAIR or FINALIZATION can inject
        if self.state.band not in (IterationBand.LATE_REPAIR, IterationBand.FINALIZATION):
            self._log(f"goku_{event_type}", "", suppressed=f"structured_only:band={self.state.band.value}")
            self.state.record_l5_goku_emission(event_type)
            return L5Decision(
                hook_name=f"goku_{event_type}", fired=True, suppressed=True,
                suppression_reason=f"structured_only:band={self.state.band.value}",
                trigger_reason=trigger_reason,
            )

        # Gate 3: max injections (2, not 5 — context is expensive)
        injection_count = sum(
            1 for et, c in self.state.l5_emissions_by_type.items()
            if not et.startswith("structured_only")
        )
        if injection_count >= L5_MAX_INJECTIONS_PER_TASK:
            self._log(f"goku_{event_type}", "", suppressed=f"max_injections:{injection_count}>={L5_MAX_INJECTIONS_PER_TASK}")
            self.state.record_l5_goku_emission(event_type)
            return L5Decision(
                hook_name=f"goku_{event_type}", fired=True, suppressed=True,
                suppression_reason=f"max_injections:{injection_count}>={L5_MAX_INJECTIONS_PER_TASK}",
                trigger_reason=trigger_reason,
            )

        # Gate 4: debounce
        allowed, reason = self.state.can_emit_l5(event_type)
        if not allowed:
            self._log(f"goku_{event_type}", "", suppressed=reason)
            self.state.record_l5_goku_emission(event_type)
            return L5Decision(
                hook_name=f"goku_{event_type}", fired=True, suppressed=True,
                suppression_reason=reason,
                trigger_reason=trigger_reason,
            )

        # Gate 5: safety checker
        from .hooks import L5bSafetyChecker
        ratio = self.state.current_iter / max(self.state.max_iter, 1)
        is_safe, safety_reason = L5bSafetyChecker.validate(message, ratio)

        if not is_safe:
            self._log(f"goku_{event_type}", "", suppressed=f"l5b_safety:{safety_reason}")
            self.state.record_l5_goku_emission(event_type)
            return L5Decision(
                hook_name=f"goku_{event_type}", fired=True, suppressed=True,
                suppression_reason=f"l5b_safety:{safety_reason}",
                trigger_reason=trigger_reason,
            )

        # All 5 gates passed → inject into agent context
        self.state.record_l5_goku_emission(event_type)
        self.state.record_l5_emission(f"goku_{event_type}")
        self._log(f"goku_{event_type}", message)

        next_action_type = self.state.latest_gt_next_action_type
        next_action_file = self.state.latest_gt_next_action_file

        return L5Decision(
            hook_name=f"goku_{event_type}",
            fired=True,
            suppressed=False,
            message=message,
            next_action_type=next_action_type,
            next_action_file=next_action_file,
            trigger_reason=trigger_reason,
        )

    @staticmethod
    def _extract_next_action(message: str) -> str:
        for line in message.splitlines():
            stripped = line.strip()
            if stripped.startswith("Next action:"):
                return stripped[len("Next action:"):].strip()
        return ""

    def _log(self, hook_name: str, message: str, suppressed: str = "") -> dict:
        entry = {
            "timestamp": time.time(),
            "layer": "L5",
            "hook": hook_name,
            "iter": self.state.current_iter,
            "max_iter": self.state.max_iter,
            "band": self.state.band.value,
            "phase": self.state.phase.value,
            "fired": bool(message) and not suppressed,
            "suppressed_reason": suppressed,
            "l5_messages_total": self.state.l5_messages_emitted,
            "message_len": len(message),
            "message_text": message[:500] if message else "",
            "next_action": self._extract_next_action(message) if message else "",
        }
        self._log_entries.append(entry)
        if message and not suppressed:
            print(f"[GT_META] L5 {hook_name} fired at iter {self.state.current_iter}/{self.state.max_iter} band={self.state.band.value}", flush=True)
        try:
            with open("/tmp/gt_l5_telemetry.jsonl", "a") as f:
                f.write(json.dumps(entry) + "\n")
        except Exception:
            pass
        return entry
