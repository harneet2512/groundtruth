"""Layer 2: Hallucination Risk Scoring.

Predicts which parts of a codebase will cause hallucinations,
using only SQLite queries and Levenshtein distance. Zero AI.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass

from groundtruth.index.store import SymbolRecord, SymbolStore
from groundtruth.utils.levenshtein import levenshtein_distance
from groundtruth.utils.platform import paths_equal
from groundtruth.utils.result import Err, GroundTruthError, Ok, Result

# Weights for overall risk calculation
_WEIGHTS: dict[str, float] = {
    "naming_ambiguity": 0.25,
    "import_depth": 0.15,
    "convention_variance": 0.15,
    "overloaded_paths": 0.15,
    "parameter_complexity": 0.15,
    "isolation_score": 0.15,
}


@dataclass(frozen=True)
class RiskScore:
    """Risk score for a file."""

    file_path: str
    overall_risk: float
    factors: dict[str, float]


@dataclass(frozen=True)
class SymbolRiskScore:
    """Risk score for a symbol."""

    symbol_name: str
    file_path: str
    overall_risk: float
    factors: dict[str, float]


def _detect_naming_convention(name: str) -> str:
    """Detect the naming convention of a symbol name."""
    if "_" in name:
        return "snake_case"
    if name and name[0].isupper() and not any(c == "_" for c in name):
        return "PascalCase"
    if name and name[0].islower() and not any(c == "_" for c in name):
        return "camelCase"
    return "other"


class RiskScorer:
    """Scores files and symbols for hallucination risk. Fully deterministic."""

    def __init__(self, store: SymbolStore) -> None:
        self._store = store

    def score_file(self, file_path: str) -> Result[RiskScore, GroundTruthError]:
        """Score a file for hallucination risk."""
        symbols_result = self._store.get_symbols_in_file(file_path)
        if isinstance(symbols_result, Err):
            return Err(symbols_result.error)

        symbols = symbols_result.value
        if not symbols:
            return Ok(
                RiskScore(
                    file_path=file_path,
                    overall_risk=0.0,
                    factors={k: 0.0 for k in _WEIGHTS},
                )
            )

        factors: dict[str, float] = {
            "naming_ambiguity": self._compute_naming_ambiguity(symbols),
            "import_depth": self._compute_import_depth(symbols),
            "convention_variance": self._compute_convention_variance(symbols),
            "overloaded_paths": self._compute_overloaded_paths(file_path),
            "parameter_complexity": self._compute_parameter_complexity(symbols),
            "isolation_score": self._compute_isolation_score(symbols),
        }

        overall = sum(factors[k] * _WEIGHTS[k] for k in _WEIGHTS)

        return Ok(
            RiskScore(
                file_path=file_path,
                overall_risk=min(1.0, overall),
                factors=factors,
            )
        )

    def score_symbol(self, symbol_name: str) -> Result[list[SymbolRiskScore], GroundTruthError]:
        """Score all instances of a symbol for hallucination risk."""
        find_result = self._store.find_symbol_by_name(symbol_name)
        if isinstance(find_result, Err):
            return Err(find_result.error)

        symbols = find_result.value
        results: list[SymbolRiskScore] = []

        for sym in symbols:
            file_result = self.score_file(sym.file_path)
            if isinstance(file_result, Err):
                continue

            file_risk = file_result.value
            results.append(
                SymbolRiskScore(
                    symbol_name=sym.name,
                    file_path=sym.file_path,
                    overall_risk=file_risk.overall_risk,
                    factors=file_risk.factors,
                )
            )

        return Ok(results)

    def score_codebase(self, limit: int = 50) -> Result[list[RiskScore], GroundTruthError]:
        """Score all files in the codebase, ranked by risk."""
        try:
            cursor = self._store.connection.execute("SELECT DISTINCT file_path FROM symbols")
            file_paths = [row["file_path"] for row in cursor.fetchall()]
        except Exception as exc:
            return Err(
                GroundTruthError(
                    code="db_query_failed",
                    message=f"Failed to get file paths: {exc}",
                )
            )

        scores: list[RiskScore] = []
        for fp in file_paths:
            result = self.score_file(fp)
            if isinstance(result, Ok):
                scores.append(result.value)

        scores.sort(key=lambda s: s.overall_risk, reverse=True)
        return Ok(scores[:limit])

    def _compute_naming_ambiguity(self, symbols: list[SymbolRecord]) -> float:
        """Fraction of symbols that have a near-namesake elsewhere in the index."""
        if not symbols:
            return 0.0

        all_names_result = self._store.get_all_symbol_names()
        if isinstance(all_names_result, Err):
            return 0.0

        all_names = all_names_result.value
        ambiguous_count = 0

        for sym in symbols:
            # Pre-filter candidates by name length (±3 chars) to avoid O(n²)
            candidates = [
                n for n in all_names if n != sym.name and abs(len(n) - len(sym.name)) <= 3
            ]
            for candidate in candidates:
                if levenshtein_distance(sym.name, candidate) <= 3:
                    ambiguous_count += 1
                    break

        return min(1.0, ambiguous_count / len(symbols))

    def _compute_import_depth(self, symbols: list[SymbolRecord]) -> float:
        """Max re-export chain length for exported symbols in this file."""
        max_depth = 0
        for sym in symbols:
            if not sym.is_exported:
                continue
            try:
                cursor = self._store.connection.execute(
                    """SELECT COUNT(*) as cnt FROM exports
                       WHERE symbol_id = ?""",
                    (sym.id,),
                )
                row = cursor.fetchone()
                if row and row["cnt"] > 0:
                    max_depth = max(max_depth, row["cnt"])
            except Exception:
                continue

        # Normalize: depth of 3+ → 1.0
        return min(1.0, max_depth / 3.0)

    def _compute_convention_variance(self, symbols: list[SymbolRecord]) -> float:
        """How mixed are naming conventions in this file?"""
        if len(symbols) < 2:
            return 0.0

        conventions: set[str] = set()
        for sym in symbols:
            conv = _detect_naming_convention(sym.name)
            if conv != "other":
                conventions.add(conv)

        if len(conventions) <= 1:
            return 0.0

        # 2 conventions = 0.5, 3+ = 1.0
        return min(1.0, (len(conventions) - 1) / 2.0)

    def _compute_overloaded_paths(self, file_path: str) -> float:
        """How many other files share a similar module name?"""
        base_name = os.path.basename(file_path)
        name_no_ext = os.path.splitext(base_name)[0]

        if not name_no_ext:
            return 0.0

        try:
            cursor = self._store.connection.execute("SELECT DISTINCT file_path FROM symbols")
            all_paths = [row["file_path"] for row in cursor.fetchall()]
        except Exception:
            return 0.0

        similar_count = 0
        for p in all_paths:
            if paths_equal(p, file_path):
                continue
            other_base = os.path.splitext(os.path.basename(p))[0]
            if other_base == name_no_ext:
                similar_count += 1

        # 1 duplicate = 0.5, 2+ = 1.0
        return min(1.0, similar_count / 2.0)

    def _compute_parameter_complexity(self, symbols: list[SymbolRecord]) -> float:
        """Average parameter complexity of functions in this file."""
        param_counts: list[int] = []
        for sym in symbols:
            if sym.kind not in ("function", "method"):
                continue
            if sym.params:
                try:
                    params = json.loads(sym.params)
                    if isinstance(params, list):
                        param_counts.append(len(params))
                except (json.JSONDecodeError, TypeError):
                    pass
            elif sym.signature:
                # Count commas in signature as rough param count
                match = re.search(r"\(([^)]*)\)", sym.signature)
                if match:
                    inner = match.group(1).strip()
                    if inner:
                        param_counts.append(inner.count(",") + 1)
                    else:
                        param_counts.append(0)

        if not param_counts:
            return 0.0

        avg = sum(param_counts) / len(param_counts)
        # Normalize: 5+ params avg → 1.0
        return min(1.0, avg / 5.0)

    def _compute_isolation_score(self, symbols: list[SymbolRecord]) -> float:
        """Fraction of exported symbols with zero usage."""
        exported = [s for s in symbols if s.is_exported]
        if not exported:
            return 0.0

        unused = sum(1 for s in exported if s.usage_count == 0)
        return unused / len(exported)
