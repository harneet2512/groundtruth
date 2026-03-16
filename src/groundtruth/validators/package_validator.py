"""Validates package imports using LSP diagnostics.

Default-allow: surfaces compiler diagnostic messages directly instead of
claiming packages are "not installed" based on index absence.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from groundtruth.index.store import SymbolStore
from groundtruth.lsp.config import DiagnosticCodeConfig
from groundtruth.lsp.protocol import Diagnostic
from groundtruth.utils.result import Err, GroundTruthError, Ok, Result

_QUOTED_NAME_RE = re.compile(r'["\']([^"\']+)["\']')

_IMPORT_KEYWORDS = (
    "cannot find module",
    "unresolved import",
    "no module named",
    "could not import",
    "could not be resolved",
    "module not found",
)


@dataclass
class PackageError:
    """A detected package error."""

    package_name: str
    message: str
    suggestion: str | None = None
    evidence_type: str = "compiler_diagnostic"


def _is_import_diagnostic(
    diag: Diagnostic,
    config: DiagnosticCodeConfig | None,
) -> bool:
    """Check if a diagnostic represents an unresolved import error."""
    if config is not None:
        return diag.code in config.unresolved_import
    if diag.severity != 1:
        return False
    msg_lower = diag.message.lower()
    return any(kw in msg_lower for kw in _IMPORT_KEYWORDS)


class PackageValidator:
    """Checks package imports using LSP diagnostics.

    Surfaces compiler diagnostics directly. Does not claim packages are
    "not installed" based solely on their absence from the packages table.
    """

    def __init__(self, store: SymbolStore) -> None:
        self._store = store

    def _is_known_local_module(self, module_path: str) -> bool:
        """Check if a module path corresponds to a local project module in the store."""
        candidates = [module_path, module_path.replace(".", "/")]

        for candidate in candidates:
            result = self._store.get_exports_by_module(candidate)
            if isinstance(result, Ok) and result.value:
                return True

        for candidate in candidates:
            slash_candidate = candidate.replace(".", "/")
            for suffix in [".py", "/__init__.py"]:
                result = self._store.get_symbols_in_file(slash_candidate + suffix)
                if isinstance(result, Ok) and result.value:
                    return True

        return False

    def validate(
        self,
        diagnostics: list[Diagnostic],
        file_path: str,
        language: str,
        diagnostic_config: DiagnosticCodeConfig | None = None,
    ) -> Result[list[PackageError], GroundTruthError]:
        """Validate package imports from diagnostics.

        Surfaces compiler diagnostic messages directly as positive evidence.
        """
        _ = file_path
        errors: list[PackageError] = []

        for diag in diagnostics:
            if not _is_import_diagnostic(diag, diagnostic_config):
                continue

            m = _QUOTED_NAME_RE.search(diag.message)
            if m is None:
                continue

            module_name = m.group(1)

            # Derive package name (top-level)
            if module_name.startswith("@"):
                parts = module_name.split("/")
                pkg_name = "/".join(parts[:2]) if len(parts) >= 2 else module_name
            elif "/" in module_name:
                pkg_name = module_name.split("/")[0]
            else:
                pkg_name = module_name.split(".")[0]

            # Skip if this is a known local module
            if self._is_known_local_module(module_name):
                continue

            # Skip if package is known-installed (compiler may just lack type stubs)
            pkg_result = self._store.get_package(pkg_name)
            if isinstance(pkg_result, Ok) and pkg_result.value is not None:
                continue

            # Surface the compiler diagnostic directly — this IS positive evidence
            errors.append(
                PackageError(
                    package_name=pkg_name,
                    message=f"Compiler reports unresolved import: {diag.message}",
                    evidence_type="compiler_diagnostic",
                )
            )

        return Ok(errors)
