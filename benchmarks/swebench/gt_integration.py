"""Passive GroundTruth integration for SWE-bench V2 mode.

The agent never sees GT tools. Instead:
1. Context is injected into the system prompt before task starts.
2. Edits are validated post-edit and feedback appended to tool results.

This file contains:
- Intelligence layer (host-agnostic): symbol search, contract extraction, blast radius
- Delivery layer (host-specific): prompt enrichment, post-edit validation, reporting
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field

from groundtruth.analysis.contracts import Contract, extract_contracts
from groundtruth.index.ast_parser import ASTSymbol, parse_python_file
from groundtruth.index.store import SymbolRecord, SymbolStore
from groundtruth.utils.result import Err, Ok
from groundtruth.validators.ast_validator import AstValidationError, AstValidator

logger = logging.getLogger(__name__)

GT_ARTIFACT_VERSION = "0.5.0"
HIGH_CONFIDENCE_THRESHOLD = 0.85
POST_EDIT_TIMEOUT_SECONDS = 2.0


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ValidationFinding:
    """Wraps an AstValidationError with confidence and severity."""

    error: AstValidationError
    confidence: float
    severity: str  # "high" | "medium" | "low"


# ---------------------------------------------------------------------------
# GTIntegration class
# ---------------------------------------------------------------------------


class GTIntegration:
    """Passive GroundTruth integration — invisible to the agent."""

    def __init__(
        self,
        store: SymbolStore,
        repo_path: str,
    ) -> None:
        self.store = store
        self.repo_path = repo_path
        self._validator = AstValidator(store)
        self._instrumentation: dict[str, object] = {
            "gt_available": False,
            "index_time_seconds": 0.0,
            "index_symbols": 0,
            "context_tokens_injected": 0,
            "contracts_extracted": 0,
            "edits_total": 0,
            "validations_fired": 0,
            "validations_true_positive": 0,
            "validations_false_positive": 0,
            "validations_likely_fp_reexport": 0,
            "validations_skipped_low_confidence": 0,
            "validation_timeouts": 0,
            "validation_latency_ms": [],
            "agent_fixed_after_validation": 0,
        }
        self._validation_log: list[dict[str, object]] = []
        self._last_findings_by_file: dict[str, list[ValidationFinding]] = {}
        self._injected_symbol_names: list[str] = []

    def mark_index_complete(self, elapsed: float, symbol_count: int) -> None:
        """Record that indexing completed."""
        self._instrumentation["gt_available"] = True
        self._instrumentation["index_time_seconds"] = round(elapsed, 2)
        self._instrumentation["index_symbols"] = symbol_count

    # ------------------------------------------------------------------
    # Intelligence layer (host-agnostic)
    # ------------------------------------------------------------------

    def reindex_single_file(self, file_path: str) -> None:
        """Re-parse a single Python file and update the store incrementally."""
        if not file_path.endswith(".py"):
            return

        symbols = parse_python_file(file_path)
        if not symbols:
            return

        # Delete old symbols for this file
        self.store.delete_symbols_in_file(file_path)

        now = int(time.time())
        for sym in symbols:
            self.store.insert_symbol(
                name=sym.name,
                kind=sym.kind,
                language="python",
                file_path=file_path,
                line_number=sym.line,
                end_line=sym.end_line,
                is_exported=sym.is_exported,
                signature=sym.signature,
                params=None,
                return_type=sym.return_type,
                documentation=sym.documentation,
                last_indexed_at=now,
            )
            # Also index children (methods inside classes)
            for child in sym.children:
                self.store.insert_symbol(
                    name=child.name,
                    kind=child.kind,
                    language="python",
                    file_path=file_path,
                    line_number=child.line,
                    end_line=child.end_line,
                    is_exported=child.is_exported,
                    signature=child.signature,
                    params=None,
                    return_type=child.return_type,
                    documentation=child.documentation,
                    last_indexed_at=now,
                )

    def _search_relevant_symbols(
        self, problem_statement: str, max_results: int = 15
    ) -> list[SymbolRecord]:
        """Extract symbol names from a problem statement and look them up."""
        # Extract likely symbol names: camelCase, snake_case, dotted paths
        candidates: set[str] = set()

        # Match identifiers that look like code: camelCase, snake_case, Class.method
        patterns = [
            r"`([a-zA-Z_]\w+(?:\.\w+)*)`",  # backtick-quoted
            r"\b([A-Z][a-z]+(?:[A-Z][a-z]+)+)\b",  # CamelCase
            r"\b([a-z]+_[a-z_]+)\b",  # snake_case
            r"\b([A-Z][a-zA-Z]+)\b",  # PascalCase class names
        ]
        for pat in patterns:
            for m in re.finditer(pat, problem_statement):
                name = m.group(1)
                # Skip very short or very common words
                if len(name) >= 3 and name.lower() not in _STOP_WORDS:
                    candidates.add(name)
                    # Also try the part after the last dot
                    if "." in name:
                        candidates.add(name.rsplit(".", 1)[-1])

        # Look up each candidate
        found: list[SymbolRecord] = []
        seen_ids: set[int] = set()
        for name in candidates:
            result = self.store.find_symbol_by_name(name)
            if isinstance(result, Ok):
                for sym in result.value:
                    if sym.id not in seen_ids:
                        seen_ids.add(sym.id)
                        found.append(sym)

        # Also try FTS search with key terms
        words = [w for w in problem_statement.split() if len(w) >= 4 and w.lower() not in _STOP_WORDS]
        if words:
            fts_query = " OR ".join(words[:5])
            fts_result = self.store.search_symbols_fts(fts_query, limit=max_results)
            if isinstance(fts_result, Ok):
                for sym in fts_result.value:
                    if sym.id not in seen_ids:
                        seen_ids.add(sym.id)
                        found.append(sym)

        # Sort by usage_count descending, limit
        found.sort(key=lambda s: s.usage_count, reverse=True)
        return found[:max_results]

    def _compute_blast_radius(self, symbol: SymbolRecord) -> int:
        """Count how many files reference a symbol."""
        result = self.store.get_refs_for_symbol(symbol.id)
        if isinstance(result, Err):
            return 0
        files = {ref.referenced_in_file for ref in result.value}
        return len(files)

    def _find_ambiguous_symbols(self, symbols: list[SymbolRecord]) -> list[str]:
        """Find symbol names that appear in multiple files (ambiguous)."""
        name_files: dict[str, set[str]] = {}
        for sym in symbols:
            name_files.setdefault(sym.name, set()).add(sym.file_path)

        warnings: list[str] = []
        for name, files in name_files.items():
            if len(files) > 1:
                short_files = [_short_path(f) for f in sorted(files)]
                warnings.append(
                    f"{len(files)} different {name}() exist: {', '.join(short_files[:4])}"
                )
        return warnings

    def _extract_contracts(self, file_path: str | None = None) -> list[Contract]:
        """Extract behavioral contracts from the index."""
        contracts = extract_contracts(self.store, file_path=file_path)
        self._instrumentation["contracts_extracted"] = len(contracts)
        return contracts

    def classify_task(self, problem_statement: str) -> str:
        """Classify the task type from the problem statement."""
        text = problem_statement.lower()
        if any(w in text for w in ("crash", "error", "exception", "traceback", "bug", "fix")):
            return "bugfix"
        if any(w in text for w in ("add", "implement", "feature", "new", "support")):
            return "feature"
        if any(w in text for w in ("refactor", "clean", "rename", "move")):
            return "refactor"
        return "unknown"

    @staticmethod
    def sanitize_docstring(doc: str | None, max_len: int = 100) -> str:
        """Truncate and clean a docstring for context injection."""
        if not doc:
            return ""
        # First line only, stripped
        line = doc.split("\n")[0].strip()
        if len(line) > max_len:
            line = line[: max_len - 3] + "..."
        return line

    # ------------------------------------------------------------------
    # Delivery layer (host-specific)
    # ------------------------------------------------------------------

    def enrich_system_prompt(
        self, problem_statement: str, base_prompt: str
    ) -> str:
        """Append GT context to the system prompt. Returns enriched prompt."""
        symbols = self._search_relevant_symbols(problem_statement)
        if not symbols:
            return base_prompt

        stats_result = self.store.get_stats()
        file_count = 0
        symbol_count = 0
        if isinstance(stats_result, Ok):
            stats = stats_result.value
            file_count = int(stats.get("files", 0))  # type: ignore[arg-type]
            symbol_count = int(stats.get("symbols", 0))  # type: ignore[arg-type]

        # Build context block
        lines: list[str] = [
            "",
            "## Codebase Context (auto-generated)",
            f"Indexed: {file_count} Python files, {symbol_count} symbols.",
            "",
            "### Relevant symbols:",
        ]

        for sym in symbols[:10]:
            sig = f"({sym.signature})" if sym.signature else "()"
            ret = f" → {sym.return_type}" if sym.return_type else ""
            short = _short_path(sym.file_path)
            lines.append(f"- `{sym.name}{sig}{ret}` in {short}")

        # Key relationships: blast radius for high-usage symbols
        relationships: list[str] = []
        for sym in symbols[:5]:
            if sym.usage_count > 3:
                radius = self._compute_blast_radius(sym)
                if radius > 0:
                    short = _short_path(sym.file_path)
                    relationships.append(
                        f"- {sym.name} has {sym.usage_count} usages across {radius} files ({short})"
                    )
        if relationships:
            lines.append("")
            lines.append("### Key relationships:")
            lines.extend(relationships[:5])

        # Warnings: ambiguous names
        warnings = self._find_ambiguous_symbols(symbols)
        if warnings:
            lines.append("")
            lines.append("### Warnings:")
            for w in warnings[:3]:
                lines.append(f"- {w}")

        # Contracts
        contracts = self._extract_contracts()
        if contracts:
            lines.append("")
            lines.append("### Behavioral contracts:")
            for c in contracts[:5]:
                lines.append(f"- `{c.symbol_name}`: {c.description}")

        context_block = "\n".join(lines)
        token_estimate = len(context_block) // 4  # rough chars-to-tokens
        self._instrumentation["context_tokens_injected"] = token_estimate

        # Track injected symbol names for context utilization analysis
        self._injected_symbol_names = [s.name for s in symbols[:10]]

        return base_prompt + "\n" + context_block

    def post_edit_validate(self, file_path: str, content: str) -> str | None:
        """Validate an edited file. Returns feedback string or None if clean.

        Has a hard timeout of POST_EDIT_TIMEOUT_SECONDS.
        """
        self._instrumentation["edits_total"] = int(self._instrumentation["edits_total"]) + 1  # type: ignore[arg-type]
        start = time.monotonic()

        try:
            # Step 1: Re-index the edited file
            self.reindex_single_file(file_path)
            if _timed_out(start):
                self._instrumentation["validation_timeouts"] = int(self._instrumentation["validation_timeouts"]) + 1  # type: ignore[arg-type]
                return None

            # Step 2: Run AST validation
            result = self._validator.validate(content, file_path, "python")
            if _timed_out(start):
                self._instrumentation["validation_timeouts"] = int(self._instrumentation["validation_timeouts"]) + 1  # type: ignore[arg-type]
                return None

            if isinstance(result, Err):
                logger.debug("Validation error for %s: %s", file_path, result.error)
                return None

            errors = result.value
            if not errors:
                # Check if previous findings for this file were fixed
                if file_path in self._last_findings_by_file:
                    prev = self._last_findings_by_file.pop(file_path)
                    self._instrumentation["agent_fixed_after_validation"] = (
                        int(self._instrumentation["agent_fixed_after_validation"]) + len(prev)  # type: ignore[arg-type]
                    )
                return None

            # Step 3: Wrap in ValidationFinding with confidence
            findings = [_wrap_finding(e, file_path) for e in errors]

            # Step 4: Filter by confidence
            high_conf = [f for f in findings if f.confidence >= HIGH_CONFIDENCE_THRESHOLD]
            low_conf_count = len(findings) - len(high_conf)
            self._instrumentation["validations_skipped_low_confidence"] = (
                int(self._instrumentation["validations_skipped_low_confidence"]) + low_conf_count  # type: ignore[arg-type]
            )

            if not high_conf:
                return None

            # Detect likely false positives from re-exports (__init__.py barrel files)
            real_findings: list[ValidationFinding] = []
            for f in high_conf:
                if f.error.error_type == "wrong_module_path" and _is_likely_reexport(
                    f.error.symbol_name or "", f.error.module_path or "", self.store
                ):
                    self._instrumentation["validations_likely_fp_reexport"] = (
                        int(self._instrumentation["validations_likely_fp_reexport"]) + 1  # type: ignore[arg-type]
                    )
                else:
                    real_findings.append(f)

            if not real_findings:
                return None

            self._instrumentation["validations_fired"] = (
                int(self._instrumentation["validations_fired"]) + 1  # type: ignore[arg-type]
            )

            # Per-validation latency
            elapsed_ms = round((time.monotonic() - start) * 1000)
            latencies = self._instrumentation["validation_latency_ms"]
            if isinstance(latencies, list):
                latencies.append(elapsed_ms)

            # Track for later fix detection
            self._last_findings_by_file[file_path] = real_findings

            # Log
            self._validation_log.append({
                "file_path": file_path,
                "timestamp": time.time(),
                "latency_ms": elapsed_ms,
                "findings": [
                    {
                        "error_type": f.error.error_type,
                        "message": f.error.message,
                        "symbol": f.error.symbol_name,
                        "confidence": f.confidence,
                        "severity": f.severity,
                    }
                    for f in real_findings
                ],
            })

            return format_validation_feedback(real_findings)

        except Exception:
            logger.exception("post_edit_validate failed for %s", file_path)
            return None

    def compute_context_utilization(self, patch: str | None) -> dict[str, object]:
        """Measure how many injected symbols appear in the agent's patch."""
        if not patch or not self._injected_symbol_names:
            return {
                "injected_symbols": self._injected_symbol_names,
                "symbols_used_in_patch": [],
                "utilization_rate": 0.0,
            }
        used = [s for s in self._injected_symbol_names if s in patch]
        rate = len(used) / len(self._injected_symbol_names) if self._injected_symbol_names else 0.0
        return {
            "injected_symbols": self._injected_symbol_names,
            "symbols_used_in_patch": used,
            "utilization_rate": round(rate, 3),
        }

    def final_report(self) -> dict[str, object]:
        """Return instrumentation data for the run metadata."""
        return {
            "artifact_version": GT_ARTIFACT_VERSION,
            "instrumentation": dict(self._instrumentation),
            "validation_log": list(self._validation_log),
        }


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------


def format_validation_feedback(findings: list[ValidationFinding]) -> str:
    """Format validation findings as feedback for the agent."""
    lines = ["⚠️ GroundTruth validation found issues:"]
    for f in findings:
        icon = "🔴" if f.severity == "high" else "🟡" if f.severity == "medium" else "⚪"
        lines.append(f"  {icon} [{f.error.error_type}] {f.error.message}")
        if f.error.module_path:
            lines.append(f"     → Check: {f.error.module_path}")
    lines.append("")
    lines.append("Please fix these issues before proceeding.")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _wrap_finding(error: AstValidationError, file_path: str) -> ValidationFinding:
    """Wrap an AstValidationError with confidence and severity."""
    confidence = _compute_confidence(error, file_path)
    severity = "high" if confidence >= 0.85 else "medium" if confidence >= 0.70 else "low"
    return ValidationFinding(error=error, confidence=confidence, severity=severity)


def _compute_confidence(error: AstValidationError, file_path: str) -> float:
    """Compute evidence-based confidence for a validation error.

    Confidence is derived from the type of evidence backing the finding,
    not from the error type itself.
    """
    evidence = getattr(error, "evidence_type", "unknown")

    if evidence == "compiler_diagnostic":
        base = 0.95
    elif evidence == "positive_contradiction":  # wrong_module_path with cross-index proof
        base = 0.90
    elif evidence == "close_typo":  # Levenshtein ≤ 2
        base = 0.85
    elif evidence == "arity_mismatch":  # provable arity violation
        base = 0.70
    else:
        base = 0.30  # No positive evidence → low confidence

    # Test files: reduce confidence
    basename = file_path.rsplit("/", 1)[-1] if "/" in file_path else file_path
    if basename.startswith("test_") or basename.startswith("tests"):
        base -= 0.15

    return max(0.0, min(1.0, base))


def _is_likely_reexport(symbol_name: str, module_path: str, store: SymbolStore) -> bool:
    """Check if a symbol is likely re-exported via __init__.py or __all__.

    If the symbol exists in a sub-module of the stated import path, it's likely
    re-exported through the package's __init__.py — a common Python pattern that
    our AST-based index can't fully resolve.
    """
    if not symbol_name or not module_path:
        return False

    # Convert dotted module path to path fragment
    path_fragment = module_path.replace(".", "/")

    # Look up where the symbol actually lives
    result = store.find_symbol_by_name(symbol_name)
    if isinstance(result, Err):
        return False

    for sym in result.value:
        normalized = sym.file_path.replace("\\", "/")
        # Symbol lives somewhere under the same package tree
        if path_fragment in normalized:
            return True

    return False


def _timed_out(start: float) -> bool:
    """Check if we've exceeded the post-edit timeout."""
    return (time.monotonic() - start) > POST_EDIT_TIMEOUT_SECONDS


def _short_path(path: str) -> str:
    """Shorten a file path for display."""
    # Keep last 3 path components
    parts = path.replace("\\", "/").split("/")
    if len(parts) > 3:
        return "/".join(parts[-3:])
    return path


_STOP_WORDS: frozenset[str] = frozenset({
    "the", "this", "that", "with", "from", "have", "been", "should",
    "could", "would", "will", "when", "where", "what", "which",
    "about", "after", "before", "into", "through", "during", "each",
    "does", "doesn", "don", "didn", "also", "more", "most", "some",
    "than", "then", "them", "they", "their", "there", "here", "just",
    "only", "very", "other", "over", "under", "again", "once",
    "because", "while", "until", "both", "between", "same", "such",
    "like", "make", "made", "using", "used", "None", "none", "True",
    "true", "False", "false", "self", "class", "return", "import",
    "file", "line", "code", "issue", "error", "test",
})
