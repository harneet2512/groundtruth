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
from .gt_hook import format_gt_evidence, check_staleness

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
        self._hook_count: int = 0
        self._hook_log: list[dict] = []

    def set_task_id(self, task_id: str) -> None:
        self._task_id = task_id
        self._files_contextualized = set()
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

    def on_file_open(self, file_path: str, turn_number: int) -> str | None:
        """Post-localization hook. Fires when agent opens a file for editing.

        Only fires once per file. Only fires after turn MIN_TURN_FOR_HOOKS.
        Returns context string or None (silent).
        """
        if self._conn is None:
            return None

        if turn_number < MIN_TURN_FOR_HOOKS:
            self._log("on_file_open", file_path, f"SKIP — too early (turn {turn_number} < {MIN_TURN_FOR_HOOKS})")
            return None

        # Normalize path
        norm_path = self._normalize_path(file_path)

        if norm_path in self._files_contextualized:
            self._log("on_file_open", file_path, "SKIP — already contextualized")
            return None

        if self._hook_count >= MAX_HOOKS_PER_TASK:
            self._log("on_file_open", file_path, "SKIP — hook limit reached")
            return None

        # Build context using gt_intel
        target = gt_intel.get_target_node(self._conn, norm_path)
        if not target:
            self._log("on_file_open", file_path, "SILENT — no target node found")
            return None

        sections: list[str] = []
        qname = target.qualified_name or target.name

        # Callers
        callers = gt_intel.get_callers(self._conn, target.id, target.file_path)
        if callers:
            caller_strs = []
            for caller_node, call_line, source_file, _res in callers[:5]:
                score, summary = gt_intel.classify_caller_usage(
                    self.repo_path, source_file, call_line,
                )
                caller_strs.append(f"{caller_node.name}() — {summary}")
            sections.append(f"Callers ({len(callers)}): " + ", ".join(caller_strs))

        # Return type
        if target.return_type:
            sections.append(f"Returns: {target.return_type}")

        # Siblings
        siblings = gt_intel.get_siblings(self._conn, target.id)
        if siblings:
            sib_names = [s.name for s in siblings[:3]]
            sections.append(f"Pattern: siblings {', '.join(sib_names)} follow same signature")

        # Tests
        tests = gt_intel.get_tests(self._conn, target.id)
        if tests:
            test_strs = [f"{t.file_path}::{t.name}" for t in tests[:3]]
            sections.append(f"Tests: " + ", ".join(test_strs))

        if not sections:
            self._log("on_file_open", file_path, "SILENT — no context available")
            return None

        self._files_contextualized.add(norm_path)
        self._hook_count += 1

        # Build lightweight evidence items for unified formatter
        from dataclasses import dataclass as _dc

        @_dc
        class _CtxItem:
            message: str
            confidence: float
            kind: str = "context"
            family: str = "context"

        items = [_CtxItem(message=s, confidence=0.80) for s in sections]

        stale = 0
        if self.db_path:
            stale = check_staleness(self.db_path, [os.path.join(self.repo_path, norm_path)])

        context = format_gt_evidence(
            evidence_items=items,
            stale_files=max(0, stale),
        )
        context = _truncate(context, MAX_CONTEXT_TOKENS)

        self._log("on_file_open", file_path, f"INJECTED — {_estimate_tokens(context)} tokens")
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

        if not warnings:
            self._log("on_edit", file_path, "SILENT — no impact")
            return None

        self._hook_count += 1

        from dataclasses import dataclass as _dc

        @_dc
        class _ConstraintItem:
            message: str
            confidence: float
            kind: str = "constraint"
            family: str = "structural"

        items = [_ConstraintItem(message=w, confidence=0.85) for w in warnings]

        stale = 0
        if self.db_path:
            stale = check_staleness(self.db_path, [os.path.join(self.repo_path, norm_path)])

        impact = format_gt_evidence(
            evidence_items=items,
            stale_files=max(0, stale),
        )
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

        from dataclasses import dataclass as _dc

        @_dc
        class _SubmitItem:
            message: str
            confidence: float
            kind: str = "caller_dependency"
            family: str = "contract"

        items = [_SubmitItem(message=w, confidence=0.85) for w in warnings[:3]]

        result = format_gt_evidence(evidence_items=items)
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
