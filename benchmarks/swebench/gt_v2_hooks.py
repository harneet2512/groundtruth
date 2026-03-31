"""V2 Pull Architecture — Lifecycle hooks for targeted context injection.

Hooks fire at specific moments in the agent's workflow, NOT at task start.
Every hook is conditional, capped, and defaults to silence.

Hooks:
  on_file_open — fires when agent opens a file for editing (after turn 2)
  on_edit      — fires when agent is about to apply an edit (constraints, not context)
  on_submit    — fires when agent submits patch (quick sanity check)
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from . import gt_intel

# Hard caps
MAX_HOOKS_PER_TASK = 3
MIN_TURN_FOR_HOOKS = 2
MAX_CONTEXT_TOKENS = 300
MAX_IMPACT_TOKENS = 200
MAX_SUBMIT_TOKENS = 150


def _estimate_tokens(text: str) -> int:
    return len(text) // 4


def _truncate(text: str, max_tokens: int) -> str:
    max_chars = max_tokens * 4
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "\n... (truncated)"


class GTV2Hooks:
    """Lifecycle hooks for v2 pull architecture.

    Fires ONLY at specific moments. Never at task start.
    Every hook is conditional and capped.
    """

    def __init__(
        self,
        db_path: str,
        repo_path: str,
        log_dir: str | None = None,
    ) -> None:
        self.db_path = db_path
        self.repo_path = repo_path
        self.log_dir = log_dir
        self._conn: sqlite3.Connection | None = None
        self._task_id: str = ""
        self._files_contextualized: set[str] = set()
        self._file_summaries: dict[str, str] = {}
        self._hook_count: int = 0
        self._hook_log: list[dict] = []

    def set_task_id(self, task_id: str) -> None:
        self._task_id = task_id
        self._files_contextualized = set()
        self._file_summaries = {}
        self._hook_count = 0
        self._hook_log = []

    def connect(self) -> bool:
        if not os.path.exists(self.db_path):
            return False
        try:
            self._conn = sqlite3.connect(self.db_path)
            gt_intel.verify_admissibility_gate(self._conn)
            return True
        except Exception:
            return False

    def shutdown(self) -> None:
        self._flush_log()
        if self._conn:
            self._conn.close()
            self._conn = None

    # ── Hook 1: on_file_open ───────────────────────────────────────────────

    def _count_connections(self, target: gt_intel.GraphNode) -> int:
        """Count total graph connections: callers + callees + tests + siblings."""
        conn = self._conn
        assert conn is not None
        total_callers, _ = gt_intel.get_all_callers_count(conn, target.id)
        callees = gt_intel.get_callees(conn, target.id)
        tests = gt_intel.get_tests(conn, target.id)
        siblings = gt_intel.get_siblings(conn, target.id)
        return total_callers + len(callees) + len(tests) + len(siblings)

    def on_file_open(self, file_path: str, turn_number: int) -> str | None:
        """Post-localization hook. Fires when agent opens a file for editing.

        Only fires once per file. Only fires after turn MIN_TURN_FOR_HOOKS.
        Stage-aware: early turns get brief signal, later turns get constraints.
        Confidence gate: requires signal strength >= 3.
        Returns context string or None (silent).
        """
        if self._conn is None:
            return None

        if turn_number < MIN_TURN_FOR_HOOKS:
            self._log("on_file_open", file_path, f"SKIP — too early (turn {turn_number} < {MIN_TURN_FOR_HOOKS})")
            return None

        norm_path = self._normalize_path(file_path)

        if norm_path in self._files_contextualized:
            self._log("on_file_open", file_path, "SKIP — already contextualized")
            return None

        if self._hook_count >= MAX_HOOKS_PER_TASK:
            self._log("on_file_open", file_path, "SKIP — hook limit reached")
            return None

        target = gt_intel.get_target_node(self._conn, norm_path)
        if not target:
            self._log("on_file_open", file_path, "SILENT — no target node found")
            return None

        # Confidence gate: require meaningful graph signal
        signal = self._count_connections(target)
        if signal < 3:
            self._log("on_file_open", file_path, f"SKIP — weak signal ({signal} connections)")
            return None

        qname = target.qualified_name or target.name
        total_callers, _ = gt_intel.get_all_callers_count(self._conn, target.id)
        tests = gt_intel.get_tests(self._conn, target.id)

        if turn_number < 8:
            # Early turns (exploring): brief signal only (~20 tokens)
            parts: list[str] = []
            if total_callers:
                parts.append(f"{total_callers} callers")
            if tests:
                parts.append(f"tested by {tests[0].name}")
            if target.return_type:
                parts.append(f"returns {target.return_type}")

            if not parts:
                self._log("on_file_open", file_path, "SILENT — no brief signal")
                return None

            self._files_contextualized.add(norm_path)
            self._hook_count += 1
            summary = f"{qname}(): {', '.join(parts)}"
            self._file_summaries[norm_path] = summary
            context = f"[GT] {summary}"
            self._log("on_file_open", file_path, f"INJECTED-BRIEF — {_estimate_tokens(context)} tokens")
            return context

        else:
            # Later turns (repairing): interface constraints (~60 tokens)
            if total_callers >= 3 and target.return_type:
                self._files_contextualized.add(norm_path)
                self._hook_count += 1
                summary = f"{qname}(): {total_callers} callers, {target.return_type}"
                self._file_summaries[norm_path] = summary
                context = f"[GT] Don't change return type of {qname}() → {target.return_type}; {total_callers} callers depend on it."
                self._log("on_file_open", file_path, f"INJECTED-CONSTRAINT — {_estimate_tokens(context)} tokens")
                return context

            # Fallback: brief signal even in late turns
            parts = []
            if total_callers:
                parts.append(f"{total_callers} callers")
            if tests:
                parts.append(f"tested by {tests[0].name}")
            if not parts:
                self._log("on_file_open", file_path, "SILENT — no context for late turn")
                return None

            self._files_contextualized.add(norm_path)
            self._hook_count += 1
            summary = f"{qname}(): {', '.join(parts)}"
            self._file_summaries[norm_path] = summary
            context = f"[GT] {summary}"
            self._log("on_file_open", file_path, f"INJECTED-BRIEF — {_estimate_tokens(context)} tokens")
            return context

    # ── Hook 2: on_edit ────────────────────────────────────────────────────

    def on_edit(self, file_path: str, function_name: str | None = None) -> str | None:
        """Pre-patch hook. Fires when agent is about to write/apply an edit.

        Shows constraints (impact), not context. Max MAX_IMPACT_TOKENS tokens.
        Returns constraint string or None (silent).
        """
        if self._conn is None:
            return None

        if self._hook_count >= MAX_HOOKS_PER_TASK:
            self._log("on_edit", file_path, "SKIP — hook limit reached")
            return None

        norm_path = self._normalize_path(file_path)
        target = gt_intel.get_target_node(self._conn, norm_path, function_name or "")
        if not target:
            self._log("on_edit", file_path, "SILENT — no target node")
            return None

        warnings: list[str] = []

        # Check caller count
        total_callers, unique_files = gt_intel.get_all_callers_count(self._conn, target.id)
        if total_callers >= 3:
            constraint = ""
            if target.return_type:
                constraint = f" (return type: {target.return_type})"
            warnings.append(f"{total_callers} callers depend on current interface{constraint}")

        # Critical path
        if gt_intel.is_critical_path(target.file_path):
            warnings.append("CRITICAL PATH — auth/security/payment code")

        # Tests that must pass
        tests = gt_intel.get_tests(self._conn, target.id)
        if tests:
            test_names = [t.name for t in tests[:3]]
            warnings.append(f"Must-pass: {', '.join(test_names)}")

        # Consolidation: remind about previously viewed files
        other_files = [
            f"{f}: {s}" for f, s in self._file_summaries.items()
            if f != norm_path
        ]
        if other_files:
            warnings.append("Also viewed: " + "; ".join(other_files[:3]))

        if not warnings:
            self._log("on_edit", file_path, "SILENT — no impact")
            return None

        self._hook_count += 1

        qname = target.qualified_name or target.name
        impact = f"[GroundTruth] Before editing {qname}():\n" + "\n".join(f"- {w}" for w in warnings)
        impact = _truncate(impact, MAX_IMPACT_TOKENS)

        self._log("on_edit", file_path, f"INJECTED — constraints ({len(warnings)} items)")
        return impact

    # ── Hook 3: on_submit ──────────────────────────────────────────────────

    def on_submit(self, patch_text: str) -> str | None:
        """Post-patch validation hook. Fires when agent is about to submit.

        Quick sanity check on changed files. Max MAX_SUBMIT_TOKENS tokens.
        Returns warning string or None (silent).
        """
        if self._conn is None:
            return None

        if self._hook_count >= MAX_HOOKS_PER_TASK:
            self._log("on_submit", "patch", "SKIP — hook limit reached")
            return None

        changed_files = self._extract_files_from_patch(patch_text)
        if not changed_files:
            self._log("on_submit", "patch", "SILENT — no files in patch")
            return None

        warnings: list[str] = []
        for fpath in changed_files[:5]:
            norm_path = self._normalize_path(fpath)
            target = gt_intel.get_target_node(self._conn, norm_path)
            if not target:
                continue

            total_callers, _ = gt_intel.get_all_callers_count(self._conn, target.id)
            if total_callers >= 3 and target.return_type:
                warnings.append(
                    f"Check: {norm_path}::{target.name} has {total_callers} callers "
                    f"depending on {target.return_type} return type"
                )

        if not warnings:
            self._log("on_submit", "patch", "SILENT — no warnings")
            return None

        self._hook_count += 1
        warning_text = "\n".join(f"- {w}" for w in warnings[:3])
        result = f"[GroundTruth] Pre-submit check:\n{warning_text}"
        result = _truncate(result, MAX_SUBMIT_TOKENS)

        self._log("on_submit", "patch", f"WARNED — {len(warnings)} items")
        return result

    # ── Helpers ─────────────────────────────────────────────────────────────

    def _normalize_path(self, file_path: str) -> str:
        if os.path.isabs(file_path):
            file_path = os.path.relpath(file_path, self.repo_path)
        return file_path.replace("\\", "/")

    @staticmethod
    def _extract_files_from_patch(patch_text: str) -> list[str]:
        """Extract changed file paths from a unified diff."""
        files = []
        for match in re.finditer(r'^diff --git a/(.*?) b/', patch_text, re.MULTILINE):
            files.append(match.group(1))
        if not files:
            # Fallback: look for +++ lines
            for match in re.finditer(r'^\+\+\+ b/(.*?)$', patch_text, re.MULTILINE):
                files.append(match.group(1))
        return files

    # ── Logging ────────────────────────────────────────────────────────────

    def _log(self, hook_name: str, target: str, action: str) -> None:
        entry = {
            "event_type": "hook_fire" if "INJECTED" in action or "WARNED" in action else "hook_skip",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "task_id": self._task_id,
            "hook": hook_name,
            "target": target,
            "action": action,
            "hook_count": self._hook_count,
        }
        self._hook_log.append(entry)
        # Write immediately (crash-safe)
        if self.log_dir:
            log_dir = Path(self.log_dir)
            log_dir.mkdir(parents=True, exist_ok=True)
            log_path = log_dir / f"{self._task_id}.hooks.jsonl"
            try:
                with open(log_path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(entry, default=str) + "\n")
            except Exception:
                pass

    def _flush_log(self) -> None:
        """Write summary entry at task end."""
        if not self.log_dir:
            return
        log_dir = Path(self.log_dir)
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / f"{self._task_id}.hooks.jsonl"

        summary = {
            "event_type": "task_end",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "task_id": self._task_id,
            "total_hooks_fired": self._hook_count,
            "total_hooks_skipped": len(self._hook_log) - self._hook_count,
            "files_contextualized": sorted(self._files_contextualized),
        }
        try:
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(summary, default=str) + "\n")
        except Exception:
            pass

    def get_hook_log(self) -> list[dict]:
        return list(self._hook_log)

    def get_summary(self) -> dict:
        return {
            "hooks_fired": self._hook_count,
            "hooks_skipped": len(self._hook_log) - self._hook_count,
            "files_contextualized": sorted(self._files_contextualized),
        }
