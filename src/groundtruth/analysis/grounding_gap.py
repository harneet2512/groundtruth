"""Layer 1: Grounding Gap Measurement.

Measures how reliably agents use correct context from briefings.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from typing import Any

from groundtruth.index.store import BriefingLogRecord, SymbolStore
from groundtruth.utils.result import Err, GroundTruthError, Ok, Result


@dataclass(frozen=True)
class GroundingResult:
    """Result of comparing a briefing to the agent's output."""

    briefing_log_id: int
    briefing_symbols: list[str]
    output_symbols: list[str]
    correct_usages: int
    ignored_symbols: int
    hallucinated_despite_briefing: int
    compliance_rate: float


@dataclass(frozen=True)
class GroundingReport:
    """Aggregate compliance statistics across briefings."""

    total_briefings: int
    total_with_validation: int
    mean_compliance_rate: float
    median_compliance_rate: float


class GroundingGapAnalyzer:
    """Compares briefing context to agent output to measure compliance."""

    def __init__(self, store: SymbolStore) -> None:
        self._store = store

    def compare_briefing_to_output(
        self,
        briefing_log: BriefingLogRecord,
        validation_errors: list[dict[str, Any]],
        proposed_code: str,
    ) -> Result[GroundingResult, GroundTruthError]:
        """Compare what the briefing told the agent vs what the agent produced.

        Args:
            briefing_log: The briefing log entry to compare against.
            validation_errors: Error dicts from validation result.
            proposed_code: The code the agent generated.

        Returns:
            A GroundingResult with compliance metrics.
        """
        briefing_symbols = briefing_log.briefing_symbols
        if not briefing_symbols:
            result = GroundingResult(
                briefing_log_id=briefing_log.id,
                briefing_symbols=[],
                output_symbols=[],
                correct_usages=0,
                ignored_symbols=0,
                hallucinated_despite_briefing=0,
                compliance_rate=1.0,
            )
            return Ok(result)

        # Build set of symbols that have errors in validation
        error_symbols: set[str] = set()
        for err in validation_errors:
            sym_name = err.get("symbol_name") or err.get("function_name") or ""
            if sym_name:
                error_symbols.add(sym_name)

        correct: list[str] = []
        ignored: list[str] = []
        hallucinated: list[str] = []
        output_symbols: list[str] = []

        for sym in briefing_symbols:
            in_code = sym in proposed_code
            has_error = sym in error_symbols

            if in_code:
                output_symbols.append(sym)
                if has_error:
                    hallucinated.append(sym)
                else:
                    correct.append(sym)
            else:
                ignored.append(sym)

        compliance_rate = len(correct) / len(briefing_symbols)

        # Persist compliance data
        update_result = self._store.update_briefing_compliance(
            log_id=briefing_log.id,
            compliance_rate=compliance_rate,
            symbols_used_correctly=correct,
            symbols_ignored=ignored,
            hallucinated_despite_briefing=hallucinated,
        )
        if isinstance(update_result, Err):
            return Err(update_result.error)

        return Ok(
            GroundingResult(
                briefing_log_id=briefing_log.id,
                briefing_symbols=briefing_symbols,
                output_symbols=output_symbols,
                correct_usages=len(correct),
                ignored_symbols=len(ignored),
                hallucinated_despite_briefing=len(hallucinated),
                compliance_rate=compliance_rate,
            )
        )

    def aggregate_compliance(self, limit: int = 100) -> Result[GroundingReport, GroundTruthError]:
        """Compute aggregate compliance stats from recent briefing logs."""
        logs_result = self._store.get_recent_briefing_logs(limit)
        if isinstance(logs_result, Err):
            return Err(logs_result.error)

        logs = logs_result.value
        with_validation = [log for log in logs if log.compliance_rate is not None]

        if not with_validation:
            return Ok(
                GroundingReport(
                    total_briefings=len(logs),
                    total_with_validation=0,
                    mean_compliance_rate=0.0,
                    median_compliance_rate=0.0,
                )
            )

        rates = [log.compliance_rate for log in with_validation if log.compliance_rate is not None]

        return Ok(
            GroundingReport(
                total_briefings=len(logs),
                total_with_validation=len(with_validation),
                mean_compliance_rate=statistics.mean(rates),
                median_compliance_rate=statistics.median(rates),
            )
        )
