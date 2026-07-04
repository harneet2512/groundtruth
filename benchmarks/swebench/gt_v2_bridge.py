"""V2 Pull Architecture — Bridge wrapping gt_intel.py graph queries into 3 focused tools.

Tools:
  gt_locate  — "Where should I look?" → ranked files from issue description
  gt_context — "What do I need to know?" → callers, siblings, tests, types for a file/function
  gt_impact  — "What could break?" → downstream callers, must-pass tests, related changes
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from . import gt_intel

# Confidence threshold for gt_locate — below this, stay silent
LOCATE_CONFIDENCE_THRESHOLD = 0.3

# Token caps (approximate, measured in chars / 4)
MAX_LOCATE_TOKENS = 200
MAX_CONTEXT_TOKENS = 300
MAX_IMPACT_TOKENS = 200


def _estimate_tokens(text: str) -> int:
    """Rough token estimate: chars / 4."""
    return len(text) // 4


def _truncate_to_tokens(text: str, max_tokens: int) -> str:
    """Truncate text to approximate token limit."""
    max_chars = max_tokens * 4
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "\n... (truncated)"


@dataclass
class LocateCandidate:
    file: str
    score: float
    reason: str
    callers: int = 0
    confidence: str = "MED"  # HIGH, MED, LOW


class GTV2Bridge:
    """Bridge for v2 pull architecture. Wraps gt_intel.py graph DB queries."""

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
        self._tool_log: list[dict] = []

    def set_task_id(self, task_id: str) -> None:
        self._task_id = task_id
        self._tool_log = []

    def connect(self) -> bool:
        """Open connection to graph.db."""
        if not os.path.exists(self.db_path):
            return False
        try:
            self._conn = sqlite3.connect(self.db_path)
            gt_intel.verify_admissibility_gate(self._conn)
            return True
        except Exception:
            return False

    def shutdown(self) -> None:
        """Close DB and flush logs."""
        self._flush_log()
        if self._conn:
            self._conn.close()
            self._conn = None

    async def call_tool(self, name: str, arguments: dict) -> str:
        """Route tool call to handler. Returns result string."""
        if self._conn is None:
            return "GroundTruth not available — graph DB not connected."

        start = time.monotonic()
        try:
            if name == "gt_locate":
                result = self._gt_locate(arguments.get("issue_description", ""))
            elif name == "gt_context":
                result = self._gt_context(
                    arguments.get("file_path", ""),
                    arguments.get("function_name"),
                )
            elif name == "gt_impact":
                result = self._gt_impact(
                    arguments.get("file_path", ""),
                    arguments.get("function_name"),
                )
            else:
                result = f"Unknown tool: {name}"
        except Exception as e:
            result = f"Error: {e}"

        latency_ms = int((time.monotonic() - start) * 1000)
        self._log_tool_call(name, arguments, result, latency_ms)
        return result

    # ── gt_locate ──────────────────────────────────────────────────────────

    def _gt_locate(self, issue_description: str) -> str:
        """Find 3-5 files most likely to need changes."""
        conn = self._conn
        assert conn is not None

        identifiers = gt_intel.extract_identifiers_from_issue(issue_description)
        if not identifiers:
            return "No strong signal found. Recommend manual exploration."

        targets = gt_intel.resolve_briefing_targets(conn, identifiers, max_targets=5)
        if not targets:
            return "No strong signal found. Recommend manual exploration."

        # Build ranked file list from targets + their callers
        candidates: list[LocateCandidate] = []
        seen_files: set[str] = set()

        for target in targets:
            if target.file_path in seen_files:
                continue
            seen_files.add(target.file_path)

            total_callers, unique_files = gt_intel.get_all_callers_count(conn, target.id)
            qname = target.qualified_name or target.name
            loc = f"{target.file_path}:{target.start_line}" if target.start_line else target.file_path

            # Score: direct symbol match = high confidence
            score = 1.0
            confidence = "HIGH"
            reason = f"defines {qname}(), {total_callers} callers"

            candidates.append(LocateCandidate(
                file=target.file_path,
                score=score,
                reason=reason,
                callers=total_callers,
                confidence=confidence,
            ))

            # Add caller files as medium-confidence candidates
            callers = gt_intel.get_callers(conn, target.id, target.file_path)
            for caller_node, call_line, source_file, _res in callers[:3]:
                if source_file in seen_files:
                    continue
                seen_files.add(source_file)
                candidates.append(LocateCandidate(
                    file=source_file,
                    score=0.6,
                    reason=f"imports {qname}() from {os.path.basename(target.file_path)}",
                    callers=0,
                    confidence="MED",
                ))

            # Add test files
            tests = gt_intel.get_tests(conn, target.id)
            for test_node in tests[:2]:
                if test_node.file_path in seen_files:
                    continue
                seen_files.add(test_node.file_path)
                candidates.append(LocateCandidate(
                    file=test_node.file_path,
                    score=0.5,
                    reason=f"tests {qname}()",
                    confidence="MED",
                ))

        if not candidates:
            return "No strong signal found. Recommend manual exploration."

        # Confidence gate
        if candidates[0].score < LOCATE_CONFIDENCE_THRESHOLD:
            return "No strong signal found. Recommend manual exploration."

        # Format response (max 5 files)
        lines = ["Files most likely to need changes:"]
        for i, c in enumerate(candidates[:5], 1):
            lines.append(f"{i}. [{c.confidence}] {c.file} — {c.reason}")

        response = "\n".join(lines)
        return _truncate_to_tokens(response, MAX_LOCATE_TOKENS)

    # ── gt_context ─────────────────────────────────────────────────────────

    def _gt_context(self, file_path: str, function_name: str | None = None) -> str:
        """Get structural context for a file/function."""
        conn = self._conn
        assert conn is not None

        # Normalize path
        if os.path.isabs(file_path):
            file_path = os.path.relpath(file_path, self.repo_path)
        file_path = file_path.replace("\\", "/")

        target = gt_intel.get_target_node(conn, file_path, function_name or "")
        if not target:
            return "No structural context found for this file."

        sections: list[str] = []
        qname = target.qualified_name or target.name
        loc = f"{file_path}:{target.start_line}" if target.start_line else file_path
        sections.append(f"## {file_path} :: {qname}()")

        # Signature
        if target.signature:
            sections.append(f"Signature: {target.signature[:120]}")

        # Return type
        if target.return_type:
            sections.append(f"Returns: {target.return_type}")

        # Callers (max 5)
        callers = gt_intel.get_callers(conn, target.id, target.file_path)
        if callers:
            caller_strs = []
            for caller_node, call_line, source_file, _res in callers[:5]:
                score, summary = gt_intel.classify_caller_usage(
                    self.repo_path, source_file, call_line,
                )
                caller_strs.append(f"{caller_node.name}() in {os.path.basename(source_file)} — {summary}")
            sections.append(f"Callers ({len(callers)}): " + ", ".join(caller_strs))

        # Siblings (max 3)
        siblings = gt_intel.get_siblings(conn, target.id)
        if siblings:
            sib_names = [s.name for s in siblings[:3]]
            sections.append(f"Siblings: {', '.join(sib_names)} follow same pattern")

        # Tests (max 3)
        tests = gt_intel.get_tests(conn, target.id)
        if tests:
            test_strs = []
            for t in tests[:3]:
                assertions = gt_intel.extract_assertions(self.repo_path, t)
                if assertions:
                    test_strs.append(f"{t.file_path}::{t.name} ({len(assertions)} assertions)")
                else:
                    test_strs.append(f"{t.file_path}::{t.name}")
            sections.append(f"Tests: " + ", ".join(test_strs))

        if len(sections) <= 1:
            return "No structural context found for this file."

        response = "\n".join(sections)
        return _truncate_to_tokens(response, MAX_CONTEXT_TOKENS)

    # ── gt_impact ──────────────────────────────────────────────────────────

    def _gt_impact(self, file_path: str, function_name: str | None = None) -> str:
        """Check downstream impact of changing a file/function."""
        conn = self._conn
        assert conn is not None

        # Normalize path
        if os.path.isabs(file_path):
            file_path = os.path.relpath(file_path, self.repo_path)
        file_path = file_path.replace("\\", "/")

        target = gt_intel.get_target_node(conn, file_path, function_name or "")
        if not target:
            return "No downstream impact detected."

        sections: list[str] = []
        qname = target.qualified_name or target.name
        sections.append(f"## Impact of changing {qname}()")

        # Caller count + signature constraint
        total_callers, unique_files = gt_intel.get_all_callers_count(conn, target.id)
        if total_callers > 0:
            constraint = ""
            if target.return_type:
                constraint = f" — do not change {target.return_type} return type"
            sections.append(
                f"Downstream: {total_callers} callers in {unique_files} files depend on this{constraint}"
            )

        # Critical path check
        if gt_intel.is_critical_path(target.file_path):
            sections.append("WARNING: This is on a critical path (auth/security/payment)")

        # Must-pass tests
        tests = gt_intel.get_tests(conn, target.id)
        if tests:
            test_names = [f"{t.name}" for t in tests[:5]]
            sections.append(f"Must-pass tests: {', '.join(test_names)}")

        # Related siblings that may need matching changes
        siblings = gt_intel.get_siblings(conn, target.id)
        if siblings:
            sib_names = [s.name for s in siblings[:3]]
            sections.append(f"Related: {', '.join(sib_names)} may need matching changes")

        if len(sections) <= 1:
            return "No downstream impact detected."

        response = "\n".join(sections)
        return _truncate_to_tokens(response, MAX_IMPACT_TOKENS)

    # ── Logging ────────────────────────────────────────────────────────────

    def _log_tool_call(self, tool: str, args: dict, result: str, latency_ms: int) -> None:
        entry = {
            "event_type": "tool_call",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "task_id": self._task_id,
            "tool": tool,
            "input": args,
            "response_tokens": _estimate_tokens(result),
            "latency_ms": latency_ms,
            "confidence": "SILENT" if "No strong signal" in result or "No structural" in result or "No downstream" in result else "RESPONDED",
        }
        self._tool_log.append(entry)

    def _flush_log(self) -> None:
        if not self.log_dir or not self._tool_log:
            return
        log_dir = Path(self.log_dir)
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / f"{self._task_id}.v2.jsonl"
        try:
            with open(log_path, "a", encoding="utf-8") as f:
                for entry in self._tool_log:
                    f.write(json.dumps(entry, default=str) + "\n")
        except Exception:
            pass

    def get_tool_log(self) -> list[dict]:
        return list(self._tool_log)
