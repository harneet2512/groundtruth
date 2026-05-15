"""L5 trajectory governor — single dispatch point for all L5 hooks."""

from __future__ import annotations

import json
import os
import re
import time
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
    ) -> str | None:
        self.state.update_iter(action_count, max_iter)

        if self.state._injection_disabled:
            self._log("disabled", "", suppressed=self.state._disable_reason)
            return None

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
        return None

    def _handle_command(
        self,
        action: Any,
        obs: Any,
        *,
        graph_db: str = "",
    ) -> str | None:
        command = _extract_command(action)
        obs_text = _extract_observation_text(obs)

        if not is_verification_command(command):
            self.state.save()
            return None

        classification = classify_observation(command, obs_text)

        if classification.is_env_failure:
            self._log("env_failure_suppressed", command)
            self.state.save()
            return None

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
                test_suggestions = self._get_test_suggestions(graph_db)
                msg = hooks.hook_unverified_patch(
                    self.state,
                    test_file_suggestions=test_suggestions,
                )
                if msg:
                    self.state.record_l5_emission("unverified_patch")
                    self._log("unverified_patch", msg)
                    self.state.save()
                    return f"\n\n{msg}\n"

            self.state.save()
            return None

        result = self._try_hooks_after_failure(failure_record, graph_db=graph_db)
        self.state.save()
        return result

    def _try_hooks_after_failure(
        self,
        failure_record: FailureRecord | None,
        *,
        graph_db: str = "",
    ) -> str | None:
        # Priority 2: Same Failure Persisted
        msg = hooks.hook_same_failure_persisted(
            self.state, failure_record,
        )
        if msg:
            self.state.record_l5_emission("same_failure_persisted")
            self._log("same_failure_persisted", msg)
            self.state.save()
            return f"\n\n{msg}\n"

        # Priority 3: Hypothesis Falsified (THE KEY HOOK)
        if self.state.has_source_edit_before_last_failure and failure_record:
            msg = hooks.hook_hypothesis_falsified(
                self.state, failure_record,
            )
            if msg:
                self.state.record_l5_emission("hypothesis_falsified")
                self.state.has_source_edit_before_last_failure = False
                self._log("hypothesis_falsified", msg)
                self.state.save()
                return f"\n\n{msg}\n"

        self.state.save()
        return None

    def _handle_source_edit(
        self,
        path: str,
        *,
        edited_files: set[str] | None = None,
        brief_candidates: set[str] | None = None,
        viewed_files: set[str] | None = None,
        graph_db: str = "",
    ) -> str | None:
        confirming = 0
        if viewed_files and brief_candidates:
            for v in viewed_files:
                if any(bc in v for bc in brief_candidates):
                    confirming += 1

        # Premature Commitment
        msg = hooks.hook_premature_commitment(
            self.state, path, confirming,
        )
        if msg:
            self.state.record_l5_emission("premature_commitment")
            self._log("premature_commitment", msg)
            self.state.save()
            return f"\n\n{msg}\n"

        self.state.save()
        return None

    def _handle_non_source_edit(self, path: str) -> str | None:
        msg = hooks.hook_no_durable_source_progress(self.state, path)
        if msg:
            self.state.record_l5_emission("no_durable_source_progress")
            self._log("no_durable_source_progress", msg)
            self.state.save()
            return f"\n\n{msg}\n"
        self.state.save()
        return None

    def _handle_finish(self) -> str | None:
        msg = hooks.hook_unsafe_finish(self.state)
        if msg:
            self.state.record_l5_emission("unsafe_finish")
            self._log("unsafe_finish", msg)
            self.state.save()
            return f"\n\n{msg}\n"
        self.state.save()
        return None

    def _get_test_suggestions(self, graph_db: str) -> list[str]:
        """Query graph.db for test files connected to recently edited source files."""
        if not graph_db or not os.path.exists(graph_db):
            return []
        try:
            import sqlite3
            conn = sqlite3.connect(graph_db)
            suggestions: list[str] = []
            for edited in self.state.edited_source_files[-2:]:
                norm = edited.replace("\\", "/")
                if norm.startswith("/"):
                    norm = norm.lstrip("/")
                rows = conn.execute(
                    """SELECT DISTINCT n2.file_path
                       FROM nodes n1
                       JOIN edges e ON (e.source_id = n1.id OR e.target_id = n1.id)
                       JOIN nodes n2 ON (
                           CASE WHEN e.source_id = n1.id THEN e.target_id ELSE e.source_id END = n2.id
                       )
                       WHERE n1.file_path LIKE ? AND n2.is_test = 1
                       LIMIT 5""",
                    (f"%{norm}",),
                ).fetchall()
                for row in rows:
                    if row[0] not in suggestions:
                        suggestions.append(row[0])
            conn.close()
            return suggestions[:3]
        except Exception:
            return []

    def _log(self, hook_name: str, message: str, suppressed: str = "") -> None:
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
        }
        self._log_entries.append(entry)
        if message and not suppressed:
            print(f"[GT_META] L5 {hook_name} fired at iter {self.state.current_iter}/{self.state.max_iter} band={self.state.band.value}", flush=True)
        try:
            path = f"/tmp/gt_l5_telemetry.jsonl"
            with open(path, "a") as f:
                f.write(json.dumps(entry) + "\n")
        except Exception:
            pass
