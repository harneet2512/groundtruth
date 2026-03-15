"""Validates imports against the symbol index using LSP diagnostics."""

from __future__ import annotations

import re
from dataclasses import dataclass

from groundtruth.index.store import SymbolStore
from groundtruth.lsp.config import DiagnosticCodeConfig
from groundtruth.lsp.protocol import Diagnostic
from groundtruth.utils.result import Err, GroundTruthError, Ok, Result

# Regex to extract quoted names from diagnostic messages.
# Works for Pyright, tsserver, gopls — they all quote the problematic name.
_QUOTED_NAME_RE = re.compile(r'["\']([^"\']+)["\']')

# Heuristic keywords for import-related errors when no diagnostic config is available.
_IMPORT_KEYWORDS = (
    "cannot find module",
    "unresolved import",
    "no module named",
    "could not import",
    "could not be resolved",
    "module not found",
)


@dataclass
class ImportValidationError:
    """A detected import error."""

    import_path: str
    symbol_name: str
    error_type: str  # 'wrong_module_path' | 'symbol_not_found'
    message: str
    suggestion: str | None = None


def _is_import_diagnostic(
    diag: Diagnostic,
    config: DiagnosticCodeConfig | None,
) -> bool:
    """Check if a diagnostic represents an unresolved import error."""
    if config is not None:
        return diag.code in config.unresolved_import
    # Fallback: severity=Error + keyword heuristic
    if diag.severity != 1:
        return False
    msg_lower = diag.message.lower()
    return any(kw in msg_lower for kw in _IMPORT_KEYWORDS)


def _extract_name(message: str) -> str | None:
    """Extract the quoted module/symbol name from a diagnostic message."""
    m = _QUOTED_NAME_RE.search(message)
    return m.group(1) if m else None


class ImportValidator:
    """Checks imports using LSP diagnostics against the symbol index."""

    def __init__(self, store: SymbolStore) -> None:
        self._store = store

    def validate(
        self,
        diagnostics: list[Diagnostic],
        file_path: str,
        language: str,
        diagnostic_config: DiagnosticCodeConfig | None = None,
    ) -> Result[list[ImportValidationError], GroundTruthError]:
        """Validate import diagnostics against the symbol index."""
        _ = file_path
        errors: list[ImportValidationError] = []

        for diag in diagnostics:
            if not _is_import_diagnostic(diag, diagnostic_config):
                continue

            name = _extract_name(diag.message)
            if name is None:
                # Can't parse — surface as generic error
                errors.append(
                    ImportValidationError(
                        import_path="",
                        symbol_name="",
                        error_type="symbol_not_found",
                        message=diag.message,
                    )
                )
                continue

            # Extract the symbol name (last segment after . or /)
            parts = re.split(r"[./]", name)
            symbol_name = parts[-1] if parts else name
            module_path = name

            # Look up in store to see if symbol exists elsewhere
            find_result = self._store.find_symbol_by_name(symbol_name)
            if isinstance(find_result, Err):
                return Err(find_result.error)

            if find_result.value:
                actual_file = find_result.value[0].file_path
                errors.append(
                    ImportValidationError(
                        import_path=module_path,
                        symbol_name=symbol_name,
                        error_type="wrong_module_path",
                        message=f"{symbol_name} not found in {module_path}",
                        suggestion=f"import from {actual_file}",
                    )
                )
            else:
                errors.append(
                    ImportValidationError(
                        import_path=module_path,
                        symbol_name=symbol_name,
                        error_type="symbol_not_found",
                        message=f"{symbol_name} does not exist in the codebase",
                    )
                )

        return Ok(errors)
