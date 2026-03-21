"""Canonical edit-site resolver — find WHERE to edit a symbol's definition."""

from __future__ import annotations

from dataclasses import dataclass

from groundtruth.index.graph import ImportGraph
from groundtruth.index.store import SymbolStore
from groundtruth.utils.result import Err, Ok, Result, GroundTruthError


@dataclass
class EditSiteCandidate:
    """A candidate location for editing a symbol's canonical definition."""

    file_path: str
    line_number: int | None
    symbol_name: str
    score: float  # 0.0–1.0
    reason: str  # why this is canonical
    is_ambiguous: bool  # True if close to other candidates


_PENALTY_PATHS = ("docs/", "migrations/", "config/")


def _is_test_file(path: str) -> bool:
    normalized = path.replace("\\", "/")
    return "test_" in normalized or "/tests/" in normalized or "\\tests\\" in path


def _is_init_file(path: str) -> bool:
    return path.replace("\\", "/").endswith("__init__.py")


def _is_penalty_path(path: str) -> bool:
    normalized = path.replace("\\", "/")
    return any(seg in normalized for seg in _PENALTY_PATHS)


class EditSiteResolver:
    """Resolves the canonical edit site for a symbol using the symbol graph."""

    def __init__(self, store: SymbolStore, graph: ImportGraph) -> None:
        self._store = store
        self._graph = graph

    def resolve(
        self, symbol_name: str, max_candidates: int = 5
    ) -> Result[list[EditSiteCandidate], GroundTruthError]:
        """Find and rank candidate edit sites for a symbol."""
        symbols_result = self._store.find_symbol_by_name(symbol_name)
        if isinstance(symbols_result, Err):
            return Err(symbols_result.error)

        symbols = symbols_result.value
        if not symbols:
            return Ok([])

        candidates: list[EditSiteCandidate] = []
        for sym in symbols:
            raw_score = 0.0
            reasons: list[str] = []

            # +0.3 definition (has actual body — line range > 1 line, or kind is
            # function/class/method which implies a body)
            if sym.kind in ("function", "class", "method"):
                raw_score += 0.3
                reasons.append("defines the symbol body")

            # +0.2 high fan-in — many files reference symbols in this file
            fan_in = self._fan_in(sym.file_path)
            if fan_in > 0:
                # Cap contribution: 0.2 at fan_in >= 5, scaled linearly below
                raw_score += min(fan_in / 5.0, 1.0) * 0.2
                reasons.append(f"fan-in={fan_in}")

            # +0.1 exported
            if sym.is_exported:
                raw_score += 0.1
                reasons.append("exported")

            # -0.3 test file
            if _is_test_file(sym.file_path):
                raw_score -= 0.3
                reasons.append("test file (penalty)")

            # -0.2 __init__.py (likely re-export)
            if _is_init_file(sym.file_path):
                raw_score -= 0.2
                reasons.append("__init__.py re-export (penalty)")

            # -0.1 docs/migrations/config
            if _is_penalty_path(sym.file_path):
                raw_score -= 0.1
                reasons.append("non-source path (penalty)")

            # Clamp to [0, 1]
            score = max(0.0, min(1.0, raw_score))

            candidates.append(
                EditSiteCandidate(
                    file_path=sym.file_path,
                    line_number=sym.line_number,
                    symbol_name=sym.name,
                    score=round(score, 3),
                    reason="; ".join(reasons),
                    is_ambiguous=False,
                )
            )

        # Sort descending by score
        candidates.sort(key=lambda c: -c.score)

        # Mark ambiguity: top-2 within 0.1
        if len(candidates) >= 2 and (candidates[0].score - candidates[1].score) <= 0.1:
            candidates[0].is_ambiguous = True
            candidates[1].is_ambiguous = True

        return Ok(candidates[:max_candidates])

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _fan_in(self, file_path: str) -> int:
        """Count how many distinct files import from *file_path*."""
        result = self._store.get_importers_of_file(file_path)
        if isinstance(result, Err):
            return 0
        return len(result.value)
