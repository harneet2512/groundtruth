"""Logs every intervention to SQLite."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from groundtruth.index.store import SymbolStore
from groundtruth.utils.result import Err, GroundTruthError, Ok, Result


@dataclass
class InterventionStats:
    """Aggregated intervention statistics."""

    total: int
    hallucinations_caught: int
    ai_calls: int
    tokens_used: int


@dataclass(frozen=True)
class SessionSummary:
    """Summary of the current session's tool usage."""

    tools_called: dict[str, int]
    files_referenced: list[str]
    validations_run: int
    errors_found: int
    errors_fixed: int
    total_calls: int


class InterventionTracker:
    """Records and queries intervention data."""

    def __init__(self, store: SymbolStore) -> None:
        self._store = store
        self._session_log: list[dict[str, Any]] = []

    def record(
        self,
        tool: str,
        phase: str,
        outcome: str,
        file_path: str | None = None,
        language: str | None = None,
        errors_found: int = 0,
        errors_fixed: int = 0,
        error_types: list[str] | None = None,
        ai_called: bool = False,
        ai_model: str | None = None,
        latency_ms: int = 0,
        tokens_used: int = 0,
        fix_accepted: bool | None = None,
        run_id: str | None = None,
    ) -> Result[None, GroundTruthError]:
        """Record an intervention."""
        error_types_json: str | None = None
        if error_types is not None:
            error_types_json = json.dumps(error_types)

        # Append to session log
        self._session_log.append(
            {
                "tool": tool,
                "phase": phase,
                "outcome": outcome,
                "file_path": file_path,
                "errors_found": errors_found,
                "errors_fixed": errors_fixed,
            }
        )

        result = self._store.log_intervention(
            tool=tool,
            phase=phase,
            outcome=outcome,
            file_path=file_path,
            language=language,
            run_id=run_id,
            errors_found=errors_found,
            errors_fixed=errors_fixed,
            error_types=error_types_json,
            ai_called=ai_called,
            ai_model=ai_model,
            latency_ms=latency_ms,
            tokens_used=tokens_used,
            fix_accepted=fix_accepted,
        )
        if isinstance(result, Err):
            return result
        return Ok(None)

    def get_stats(self) -> Result[InterventionStats, GroundTruthError]:
        """Get aggregated intervention stats."""
        result = self._store.get_stats()
        if isinstance(result, Err):
            return result

        raw = result.value

        def _int(val: object) -> int:
            if isinstance(val, int):
                return val
            return int(str(val)) if val is not None else 0

        return Ok(
            InterventionStats(
                total=_int(raw.get("total_interventions", 0)),
                hallucinations_caught=_int(raw.get("hallucinations_caught", 0)),
                ai_calls=_int(raw.get("ai_calls", 0)),
                tokens_used=_int(raw.get("tokens_used", 0)),
            )
        )

    def get_session_summary(self) -> SessionSummary:
        """Aggregate session log into a summary."""
        tools_called: dict[str, int] = {}
        files_seen: list[str] = []
        validations_run = 0
        total_errors_found = 0
        total_errors_fixed = 0

        for entry in self._session_log:
            tool = entry["tool"]
            tools_called[tool] = tools_called.get(tool, 0) + 1

            fp = entry.get("file_path")
            if fp is not None and fp not in files_seen:
                files_seen.append(fp)

            if entry.get("phase") == "validate":
                validations_run += 1

            total_errors_found += entry.get("errors_found", 0)
            total_errors_fixed += entry.get("errors_fixed", 0)

        return SessionSummary(
            tools_called=tools_called,
            files_referenced=files_seen,
            validations_run=validations_run,
            errors_found=total_errors_found,
            errors_fixed=total_errors_fixed,
            total_calls=len(self._session_log),
        )
