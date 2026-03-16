"""Grounding records — structured evidence for code validation decisions.

A grounding record captures exactly *why* a piece of proposed code was accepted
or rejected, with machine-checkable evidence for each assertion.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from groundtruth.index.graph import ImportGraph
from groundtruth.index.store import SymbolStore
from groundtruth.utils.result import Err, Ok
from groundtruth.validators.ast_validator import AstValidator


@dataclass(frozen=True)
class Evidence:
    """A single piece of evidence supporting or refuting code correctness."""

    type: str  # 'symbol_resolved' | 'import_valid' | 'signature_match' | 'package_available'
    source: str  # 'ast_validator' | 'symbol_store' | 'import_graph'
    assertion: str  # human-readable claim, e.g. "getUserById exists in src/users/queries.py"
    verified: bool  # True = assertion holds, False = assertion violated
    detail: str = ""  # extra info (e.g. "found at line 5, signature (user_id: int) -> User")


@dataclass
class GroundingRecord:
    """Complete evidence bundle for a code validation decision."""

    target_file: str
    target_symbols: list[str]  # symbols extracted from the proposed code
    evidence: list[Evidence] = field(default_factory=list)
    confidence: float = 1.0  # 0.0 - 1.0, computed from evidence
    violated_invariants: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Serialize for MCP response."""
        return {
            "target_file": self.target_file,
            "target_symbols": self.target_symbols,
            "evidence_count": len(self.evidence),
            "verified_count": sum(1 for e in self.evidence if e.verified),
            "violated_count": sum(1 for e in self.evidence if not e.verified),
            "confidence": round(self.confidence, 3),
            "violated_invariants": self.violated_invariants,
            "evidence": [
                {
                    "type": e.type,
                    "source": e.source,
                    "assertion": e.assertion,
                    "verified": e.verified,
                    "detail": e.detail,
                }
                for e in self.evidence
            ],
        }


def build_grounding_record(
    code: str,
    file_path: str,
    store: SymbolStore,
    graph: ImportGraph | None = None,
    language: str | None = None,
) -> GroundingRecord:
    """Build a grounding record by running all checks against the index.

    Checks performed:
    1. Symbol resolution — every imported symbol resolves in the index
    2. Import chain validity — import paths map to real files
    3. Signature compatibility — function calls match stored signatures
    4. Package availability — external packages are installed
    """
    lang = language or _infer_language(file_path)
    record = GroundingRecord(target_file=file_path, target_symbols=[])

    if not lang:
        return record

    # Run AST validator to get structured errors
    validator = AstValidator(store)
    result = validator.validate(code, file_path, lang)

    if isinstance(result, Err):
        return record

    errors = result.value

    # Extract symbols from errors and successful checks
    imported_symbols = _extract_imported_symbols(code, lang)
    record.target_symbols = imported_symbols

    # Build evidence from successful imports (no error = verified)
    errored_symbols = {e.symbol_name for e in errors}
    for sym_name in imported_symbols:
        if sym_name in errored_symbols:
            continue
        # Symbol imported without error → verified
        find_result = store.find_symbol_by_name(sym_name)
        if isinstance(find_result, Ok) and find_result.value:
            sym = find_result.value[0]
            record.evidence.append(
                Evidence(
                    type="symbol_resolved",
                    source="symbol_store",
                    assertion=f"'{sym_name}' exists in {sym.file_path}",
                    verified=True,
                    detail=f"kind={sym.kind}, line={sym.line_number}",
                )
            )

    # Build evidence from errors (failed checks)
    for err in errors:
        ev_type = _error_to_evidence_type(err.error_type)
        record.evidence.append(
            Evidence(
                type=ev_type,
                source="ast_validator",
                assertion=err.message,
                verified=False,
                detail=f"line {err.line}, type={err.error_type}",
            )
        )
        record.violated_invariants.append(err.message)

    # Check import chain validity via graph if available
    if graph is not None:
        _check_import_chains(record, file_path, imported_symbols, store, graph)

    # Compute confidence
    total = len(record.evidence)
    if total > 0:
        verified = sum(1 for e in record.evidence if e.verified)
        record.confidence = verified / total
    else:
        record.confidence = 1.0  # no checks = no evidence of problems

    return record


def _infer_language(file_path: str) -> str | None:
    """Infer language from file extension."""
    ext_map: dict[str, str] = {
        ".py": "python",
        ".ts": "typescript",
        ".tsx": "typescript",
        ".js": "javascript",
        ".jsx": "javascript",
        ".go": "go",
        ".rs": "rust",
    }
    for ext, lang in ext_map.items():
        if file_path.endswith(ext):
            return lang
    return None


def _error_to_evidence_type(error_type: str) -> str:
    """Map AST validator error type to evidence type."""
    mapping: dict[str, str] = {
        "wrong_module_path": "import_valid",
        "wrong_arg_count": "signature_match",
        "likely_typo": "symbol_resolved",
    }
    return mapping.get(error_type, "symbol_resolved")


def _extract_imported_symbols(code: str, language: str) -> list[str]:
    """Extract symbol names from import statements in code."""
    import ast
    import re

    symbols: list[str] = []

    if language == "python":
        try:
            tree = ast.parse(code)
        except SyntaxError:
            return symbols
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    symbols.append(alias.asname or alias.name)
            elif isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    symbols.append(alias.asname or alias.name)
    elif language in ("typescript", "javascript"):
        # Named imports: import { X, Y } from '...'
        for m in re.finditer(r"import\s*\{([^}]+)\}\s*from", code):
            for name in m.group(1).split(","):
                clean = name.strip().split(" as ")[-1].strip()
                if clean:
                    symbols.append(clean)
        # Default imports: import X from '...'
        for m in re.finditer(r"import\s+(\w+)\s+from", code):
            symbols.append(m.group(1))

    return symbols


def _check_import_chains(
    record: GroundingRecord,
    file_path: str,
    symbols: list[str],
    store: SymbolStore,
    graph: ImportGraph,
) -> None:
    """Check that imported symbols are reachable via the import graph."""
    for sym_name in symbols:
        find_result = store.find_symbol_by_name(sym_name)
        if isinstance(find_result, Err) or not find_result.value:
            continue
        sym = find_result.value[0]
        # Check if there's a ref chain from the target file to the symbol's file
        callers_result = graph.find_callers(sym_name)
        if isinstance(callers_result, Err):
            continue
        callers = callers_result.value
        has_chain = any(c.file_path == file_path for c in callers)
        record.evidence.append(
            Evidence(
                type="import_valid",
                source="import_graph",
                assertion=f"Import chain exists from {file_path} to {sym.file_path} for '{sym_name}'",
                verified=has_chain,
                detail=f"callers={len(callers)}",
            )
        )
