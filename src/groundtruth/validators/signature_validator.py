"""Validates function call signatures against the index using LSP diagnostics."""

from __future__ import annotations

import re
from dataclasses import dataclass

from groundtruth.index.store import SymbolStore
from groundtruth.lsp.config import DiagnosticCodeConfig
from groundtruth.lsp.protocol import Diagnostic
from groundtruth.utils.result import Err, GroundTruthError, Ok, Result

# Regex patterns for extracting arg counts from diagnostic messages.
_EXPECTED_ARGS_RE = re.compile(r"[Ee]xpected\s+(\d+)\s+argument")
_GOT_ARGS_RE = re.compile(r"got\s+(\d+)")
# Extract function name from messages like "Expected 2 arguments, but got 3" — try to find
# a function name mentioned. Common patterns:
# Pyright: 'No overloads for "foo" match the provided arguments'
# tsserver: 'Expected 2 arguments, but got 3.'
_FUNC_NAME_RE = re.compile(r'["\'](\w+)["\']')

_ARG_COUNT_KEYWORDS = (
    "expected",
    "argument",
    "too many",
    "too few",
    "no overload",
)


@dataclass
class SignatureError:
    """A detected signature mismatch."""

    function_name: str
    expected_params: int
    actual_params: int
    message: str


def _is_arg_count_diagnostic(
    diag: Diagnostic,
    config: DiagnosticCodeConfig | None,
) -> bool:
    """Check if a diagnostic represents an argument count mismatch."""
    if config is not None:
        return diag.code in config.wrong_arg_count
    # Fallback: severity=Error + keyword heuristic
    if diag.severity != 1:
        return False
    msg_lower = diag.message.lower()
    return any(kw in msg_lower for kw in _ARG_COUNT_KEYWORDS)


class SignatureValidator:
    """Checks function call signatures using LSP diagnostics."""

    def __init__(self, store: SymbolStore) -> None:
        self._store = store

    def validate(
        self,
        diagnostics: list[Diagnostic],
        file_path: str,
        language: str,
        diagnostic_config: DiagnosticCodeConfig | None = None,
    ) -> Result[list[SignatureError], GroundTruthError]:
        """Validate function call signatures from diagnostics."""
        _ = (file_path, language)
        errors: list[SignatureError] = []

        for diag in diagnostics:
            if not _is_arg_count_diagnostic(diag, diagnostic_config):
                continue

            msg = diag.message

            # Extract expected and actual arg counts
            expected_match = _EXPECTED_ARGS_RE.search(msg)
            got_match = _GOT_ARGS_RE.search(msg)

            expected_count = int(expected_match.group(1)) if expected_match else 0
            actual_count = int(got_match.group(1)) if got_match else 0

            # Extract function name
            func_match = _FUNC_NAME_RE.search(msg)
            func_name = func_match.group(1) if func_match else ""

            # Cross-reference against store for enrichment
            if func_name:
                find_result = self._store.find_symbol_by_name(func_name)
                if isinstance(find_result, Err):
                    return Err(find_result.error)

            errors.append(
                SignatureError(
                    function_name=func_name,
                    expected_params=expected_count,
                    actual_params=actual_count,
                    message=msg,
                )
            )

        return Ok(errors)
