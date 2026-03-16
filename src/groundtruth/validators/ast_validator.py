"""AST-based validation — positive evidence only.

Default-allow: only emit findings backed by positive evidence (a concrete
contradiction between the code and known index data). If the validator
doesn't know, it stays silent.

Supports Python (via LanguageAdapter). TS/JS/Go use stub adapters that return
empty results → validation stays silent until adapters are fully implemented.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from groundtruth.index.store import SymbolStore
from groundtruth.utils.levenshtein import levenshtein_distance
from groundtruth.utils.result import Err, Ok, Result, GroundTruthError
from groundtruth.validators.language_adapter import (
    LanguageAdapter,
    ParsedCall,
    ParsedImport,
    get_adapter,
)


@dataclass(frozen=True)
class AstValidationError:
    """A single AST-based validation error."""

    error_type: str  # 'wrong_module_path' | 'wrong_arg_count' | 'likely_typo'
    message: str
    symbol_name: str
    line: int
    module_path: str | None = None
    evidence_type: str = "unknown"  # 'positive_contradiction' | 'close_typo' | 'arity_mismatch'


# Minimum number of known symbols in a module before we trust our index
# enough to claim a symbol is missing. Below this, the index is too sparse.
_MIN_COVERAGE_THRESHOLD = 5


def _normalize_file_path(path: str) -> str:
    """Normalize a file path for comparison."""
    return path.replace("\\", "/").lstrip("./")


def _module_to_paths(module: str) -> list[str]:
    """Convert a dotted module path to candidate file paths."""
    base = module.replace(".", "/")
    candidates = [base + ".py", base + "/__init__.py"]
    if len(module) >= 2 and module[1] == ":":
        win_base = module[0] + ":" + module[2:].replace(".", "/")
        candidates.append(win_base + ".py")
        candidates.append(win_base + "/__init__.py")
    return candidates


def _find_matching_file(candidate: str, normalized_files: dict[str, str]) -> str | None:
    """Find a matching file in the index, trying exact then suffix match."""
    norm = _normalize_file_path(candidate)
    if norm in normalized_files:
        return normalized_files[norm]
    suffix = "/" + norm
    candidate_depth = norm.count("/")
    for stored_norm, orig in normalized_files.items():
        if stored_norm == norm:
            return orig
        if stored_norm.endswith(suffix):
            if candidate_depth == 0:
                stored_depth = stored_norm.count("/")
                if stored_depth > candidate_depth + 1:
                    continue
            return orig
    return None


def _module_dir_exists(module: str, normalized_files: dict[str, str]) -> bool:
    """Check if a module directory exists by looking for files under it."""
    dir_suffix = "/" + module.replace(".", "/") + "/"
    for stored_norm in normalized_files:
        if dir_suffix in stored_norm:
            return True
    return False


class AstValidator:
    """Validates code using positive evidence only.

    Only emits findings when there is concrete proof that something is wrong:
    - wrong_module_path: symbol exists at a different path (positive contradiction)
    - likely_typo: close Levenshtein match in the same module (positive near-match)
    - wrong_arg_count: arity provably outside allowed range (positive mismatch)
    """

    def __init__(self, store: SymbolStore, adapter: LanguageAdapter | None = None) -> None:
        self._store = store
        self._adapter = adapter

    def validate(
        self, code: str, file_path: str, language: str
    ) -> Result[list[AstValidationError], GroundTruthError]:
        """Validate imports and calls in the given code.

        Uses the language adapter if provided, otherwise looks one up by language.
        Returns empty list (no findings) for unsupported languages.
        """
        adapter = self._adapter or get_adapter(language)
        if adapter is None:
            return Ok([])

        errors: list[AstValidationError] = []

        # Import validation
        imports = adapter.parse_imports(code)
        if imports:
            import_errors = self._validate_imports(imports, adapter)
            errors.extend(import_errors)

        # Signature validation
        calls = adapter.parse_calls(code)
        if calls:
            sig_errors = self._validate_signatures(calls, adapter)
            errors.extend(sig_errors)

        return Ok(errors)

    def _validate_imports(
        self, imports: list[ParsedImport], adapter: LanguageAdapter
    ) -> list[AstValidationError]:
        """Validate imports using positive-evidence-only logic.

        For `from M import X`:
          1. Is M in index? NO → SILENT (unknown module = no opinion)
          2. Does M have ≥5 symbols? NO → SILENT (insufficient coverage)
          3. Does M have dynamic exports? YES → SILENT
          4. Is X among M's known symbols? YES → VALID
          5. Does X exist at a DIFFERENT module? YES → wrong_module_path
          6. Does Levenshtein match ≤2 exist in M? YES → likely_typo
          7. Otherwise → SILENT

        For `import M`: Always SILENT from AST validator.
        """
        errors: list[AstValidationError] = []
        builtins = adapter.get_builtins()

        # Get all files for path matching
        all_files_result = self._store.get_all_files()
        if isinstance(all_files_result, Err):
            return []
        normalized_files = {_normalize_file_path(f): f for f in all_files_result.value}

        for imp in imports:
            # Skip bare imports (import M) — always silent
            if not imp.is_from:
                continue

            # Skip stdlib/builtins
            top_module = imp.module.split(".")[0]
            if top_module in builtins:
                continue

            # Step 1: Is M in the index?
            matched_path = self._find_module_file(imp.module, normalized_files)
            if matched_path is None:
                # Also check if module directory exists
                if not _module_dir_exists(imp.module, normalized_files):
                    # Module not in index → SILENT (no opinion)
                    continue

                # Module directory exists but no specific file found
                # Check module_coverage for dynamic exports
                dynamic_result = self._store.module_has_dynamic_exports(imp.module)
                if isinstance(dynamic_result, Ok) and dynamic_result.value:
                    continue  # Dynamic exports → SILENT

                # Check if symbol exists elsewhere (positive evidence)
                err = self._check_cross_index(imp)
                if err is not None:
                    errors.append(err)
                continue

            # Step 2: Get symbols in this module file
            symbols_result = self._store.get_symbols_in_file(matched_path)
            if isinstance(symbols_result, Err):
                continue

            known_symbols = {s.name for s in symbols_result.value}

            # Check module_coverage threshold using actual symbol count in file
            symbol_count = len(known_symbols)
            if symbol_count < _MIN_COVERAGE_THRESHOLD:
                # Insufficient coverage → SILENT
                continue

            # Step 3: Dynamic exports?
            dynamic_result = self._store.module_has_dynamic_exports(imp.module)
            if isinstance(dynamic_result, Ok) and dynamic_result.value:
                continue  # SILENT

            # Check for __init__.py re-export patterns in the file content
            if matched_path.endswith("__init__.py"):
                # __init__.py files commonly re-export — be conservative
                continue

            # Step 4: Is X among known symbols?
            if imp.name in known_symbols:
                continue  # VALID

            # Step 5: Does X exist at a DIFFERENT module? (positive evidence)
            cross_err = self._check_cross_index(imp)
            if cross_err is not None:
                errors.append(cross_err)
                continue

            # Step 6: Levenshtein match ≤ 2 in same module? (positive evidence)
            typo_err = self._check_typo(imp, known_symbols)
            if typo_err is not None:
                errors.append(typo_err)
                continue

            # Step 7: Otherwise → SILENT

        return errors

    def _find_module_file(
        self, module: str, normalized_files: dict[str, str]
    ) -> str | None:
        """Find the file corresponding to a module path."""
        for candidate in _module_to_paths(module):
            match = _find_matching_file(candidate, normalized_files)
            if match is not None:
                return match
        return None

    def _check_cross_index(self, imp: ParsedImport) -> AstValidationError | None:
        """Check if the symbol exists at a different module path."""
        find_result = self._store.find_symbol_by_name(imp.name)
        if isinstance(find_result, Ok) and find_result.value:
            actual_file = find_result.value[0].file_path
            return AstValidationError(
                error_type="wrong_module_path",
                message=f"'{imp.name}' not found in '{imp.module}' (exists in {actual_file})",
                symbol_name=imp.name,
                line=imp.line,
                module_path=imp.module,
                evidence_type="positive_contradiction",
            )
        return None

    def _check_typo(
        self, imp: ParsedImport, known_symbols: set[str]
    ) -> AstValidationError | None:
        """Check if the symbol name is a close typo of a known symbol."""
        best_match: str | None = None
        best_dist = 3  # threshold

        for sym_name in known_symbols:
            dist = levenshtein_distance(imp.name, sym_name)
            if dist <= 2 and dist < best_dist:
                best_dist = dist
                best_match = sym_name

        if best_match is not None:
            return AstValidationError(
                error_type="likely_typo",
                message=(
                    f"'{imp.name}' not found in '{imp.module}'. "
                    f"Did you mean '{best_match}'? (edit distance: {best_dist})"
                ),
                symbol_name=imp.name,
                line=imp.line,
                module_path=imp.module,
                evidence_type="close_typo",
            )
        return None

    def _validate_signatures(
        self, calls: list[ParsedCall], adapter: LanguageAdapter
    ) -> list[AstValidationError]:
        """Validate function call signatures using positive-evidence-only logic.

        For f(a, b, c):
          1. f resolves to exactly ONE symbol? NO → SILENT (ambiguous)
          2. Signature has variadic params? YES → SILENT
          3. Compute effective arity via adapter (subtract self/cls, count defaults)
          4. Arg count outside [min_required, max_allowed]? YES → wrong_arg_count
          5. Otherwise → SILENT
        """
        errors: list[AstValidationError] = []

        for call in calls:
            # Step 1: Look up function — must resolve to exactly one
            find_result = self._store.find_symbol_by_name(call.function_name)
            if isinstance(find_result, Err) or not find_result.value:
                continue

            # Ambiguous — multiple matches
            if len(find_result.value) > 1:
                continue

            sym = find_result.value[0]
            if not sym.signature:
                continue
            if sym.kind not in ("function", "method"):
                continue

            # Step 2-3: Compute effective arity
            is_method = call.is_method_call or sym.kind == "method"
            min_required, max_allowed = adapter.resolve_effective_arity(
                sym.signature, is_method
            )

            # Step 2: Variadic → SILENT
            if max_allowed == math.inf:
                continue

            # Step 4: Check bounds
            if call.arg_count < min_required or call.arg_count > max_allowed:
                if min_required == max_allowed:
                    expected_str = str(int(min_required))
                else:
                    expected_str = f"{int(min_required)}-{int(max_allowed)}"
                errors.append(
                    AstValidationError(
                        error_type="wrong_arg_count",
                        message=(
                            f"'{call.function_name}' expects {expected_str} arg(s) "
                            f"but called with {call.arg_count}. "
                            f"Signature: {sym.signature}"
                        ),
                        symbol_name=call.function_name,
                        line=call.line,
                        evidence_type="arity_mismatch",
                    )
                )

        return errors
