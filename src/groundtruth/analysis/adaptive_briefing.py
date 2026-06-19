"""Layer 3: Adaptive Briefing.

Tailors briefing context based on risk scores and past failure history.
Does NOT call AI — all enhancements are deterministic.
"""

from __future__ import annotations

from groundtruth.ai.briefing import BriefingResult
from groundtruth.analysis.risk_scorer import RiskScorer
from groundtruth.index.store import SymbolStore
from groundtruth.utils.result import Err, GroundTruthError, Ok, Result


class AdaptiveBriefing:
    """Enhances briefings based on risk scores and past failures."""

    def __init__(self, store: SymbolStore, risk_scorer: RiskScorer) -> None:
        self._store = store
        self._risk_scorer = risk_scorer

    def enhance_briefing(
        self, base_briefing: BriefingResult, target_file: str
    ) -> Result[BriefingResult, GroundTruthError]:
        """Enhance a briefing based on file risk and history.

        Returns a new BriefingResult with additional context appended.
        Does NOT call AI — all additions are deterministic text.
        """
        risk_result = self._risk_scorer.score_file(target_file)
        if isinstance(risk_result, Err):
            return Err(risk_result.error)

        risk = risk_result.value
        additions: list[str] = []
        extra_warnings: list[str] = list(base_briefing.warnings)

        # High naming ambiguity: include exact import paths
        if risk.factors.get("naming_ambiguity", 0.0) > 0.5:
            path_lines: list[str] = []
            for sym_info in base_briefing.relevant_symbols:
                name = sym_info.get("name", "")
                file = sym_info.get("file", "")
                if name and file:
                    path_lines.append(f"  {name} -> {file}")
            if path_lines:
                additions.append(
                    "Exact import paths (high naming ambiguity):\n" + "\n".join(path_lines)
                )
            extra_warnings.append("This file has high naming ambiguity — use exact import paths.")

        # Deep import chains: warn about re-exports
        if risk.factors.get("import_depth", 0.0) > 0.4:
            additions.append(
                "This file has deep re-export chains. "
                "Import from the defining module, not re-exports."
            )

        # Overloaded paths: warn about confusable modules
        if risk.factors.get("overloaded_paths", 0.0) > 0.4:
            extra_warnings.append(
                "Multiple modules share similar names — double-check import paths."
            )

        # Check past failures for this file
        logs_result = self._store.get_briefing_logs_for_file(target_file)
        if isinstance(logs_result, Ok):
            past_failures = [
                log
                for log in logs_result.value
                if log.hallucinated_despite_briefing and len(log.hallucinated_despite_briefing) > 0
            ]
            if past_failures:
                failed_symbols: set[str] = set()
                for log in past_failures[:5]:
                    if log.hallucinated_despite_briefing:
                        failed_symbols.update(log.hallucinated_despite_briefing)
                if failed_symbols:
                    additions.append(
                        "Previously hallucinated symbols for this file: "
                        + ", ".join(sorted(failed_symbols))
                        + ". Pay extra attention to these."
                    )

        has_new_warnings = len(extra_warnings) > len(base_briefing.warnings)
        if not additions and not has_new_warnings:
            return Ok(base_briefing)

        enhanced_text = base_briefing.briefing
        if additions:
            enhanced_text = enhanced_text + "\n\n" + "\n\n".join(additions)

        return Ok(
            BriefingResult(
                briefing=enhanced_text,
                relevant_symbols=list(base_briefing.relevant_symbols),
                warnings=extra_warnings,
            )
        )
