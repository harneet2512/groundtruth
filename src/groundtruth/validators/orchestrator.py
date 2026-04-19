"""Orchestrates all validators and merges results."""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from groundtruth.ai.semantic_resolver import SemanticResolver
from groundtruth.index.store import SymbolStore
from groundtruth.lsp.config import get_diagnostic_config, get_language_id
from groundtruth.lsp.manager import LSPManager
from groundtruth.lsp.protocol import Diagnostic
from groundtruth.utils.levenshtein import suggest_alternatives
from groundtruth.utils.symbol_components import suggest_by_components
from groundtruth.utils.logger import get_logger
from groundtruth.utils.result import Err, GroundTruthError, Ok, Result
from groundtruth.validators.ast_validator import AstValidator
from groundtruth.validators.import_validator import ImportValidator
from groundtruth.validators.language_adapter import get_adapter
from groundtruth.validators.package_validator import PackageValidator
from groundtruth.validators.signature_validator import SignatureValidator

log = get_logger("validators.orchestrator")

# Map file extensions to language names (still needed for language_id inference)
_EXTENSION_MAP: dict[str, str] = {
    ".py": "python",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".go": "go",
    ".rs": "rust",
    ".java": "java",
    ".kt": "kotlin",
    ".kts": "kotlin",
    ".scala": "scala",
    ".groovy": "groovy",
    ".gradle": "groovy",
    ".cs": "csharp",
    ".php": "php",
    ".swift": "swift",
    ".c": "c",
    ".h": "c",
    ".cpp": "cpp",
    ".cc": "cpp",
    ".cxx": "cpp",
    ".hpp": "cpp",
    ".hxx": "cpp",
    ".rb": "ruby",
    ".rake": "ruby",
    ".ex": "elixir",
    ".exs": "elixir",
    ".lua": "lua",
    ".ml": "ocaml",
    ".mli": "ocaml",
    ".elm": "elm",
}


@dataclass
class ValidationResult:
    """Merged result from all validators.

    ``valid`` is tri-state:
    - ``True``  — validation ran and found no errors
    - ``False`` — validation ran and found errors
    - ``None``  — validation was degraded (e.g. LSP unavailable); result is unknown

    When ``degraded`` is True, callers must not interpret ``valid=None`` as "pass".
    """

    valid: bool | None
    errors: list[dict[str, Any]] = field(default_factory=list)
    ai_used: bool = False
    latency_ms: int = 0
    degraded: bool = False
    degraded_reason: str = ""


def compute_confidence(error: dict[str, Any], store: SymbolStore) -> float:
    """Compute evidence-based confidence for a validation error.

    Confidence is derived from the type of evidence, not assigned by error type.
    """
    evidence = error.get("evidence_type", "unknown")

    if evidence == "compiler_diagnostic":
        base = 0.95
    elif evidence == "positive_contradiction":  # wrong_module_path with cross-index proof
        base = 0.90
    elif evidence == "close_typo":  # Levenshtein ≤ 2
        base = 0.85
    elif evidence == "arity_mismatch":  # provable arity violation
        base = 0.80
    else:
        base = 0.30

    if error.get("ambiguous_match"):
        base *= 0.6

    return min(1.0, max(0.0, base))


class ValidationOrchestrator:
    """Runs all validators and merges results."""

    def __init__(
        self,
        store: SymbolStore,
        lsp_manager: LSPManager | None = None,
        api_key: str | None = None,
    ) -> None:
        self._store = store
        self._lsp_manager = lsp_manager
        self._api_key = api_key
        self._import_validator = ImportValidator(store)
        self._package_validator = PackageValidator(store)
        self._signature_validator = SignatureValidator(store)
        self._resolver = SemanticResolver(store, api_key)

    def _infer_language(self, file_path: str) -> str | None:
        """Infer language from file extension."""
        for ext, lang in _EXTENSION_MAP.items():
            if file_path.endswith(ext):
                return lang
        return None

    def _get_extension(self, file_path: str) -> str:
        """Get file extension from path."""
        return os.path.splitext(file_path)[1]

    def _make_virtual_uri(self, file_path: str) -> str:
        """Construct a virtual URI for validation to avoid collision with real files."""
        dirname = os.path.dirname(file_path)
        basename = os.path.basename(file_path)
        virtual_name = f"__gt_validate_{basename}"
        virtual_path = os.path.join(dirname, virtual_name) if dirname else virtual_name
        return Path(os.path.abspath(virtual_path)).as_uri()

    def _enrich_with_suggestions(self, errors: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Add Levenshtein and cross-index suggestions to positive-evidence errors."""
        names_result = self._store.get_all_symbol_names()
        if isinstance(names_result, Err):
            return errors

        all_names = names_result.value

        for error in errors:
            if error.get("suggestion") is not None:
                continue

            # Get the symbol name to search for
            search_name = error.get("symbol_name") or error.get("function_name") or ""
            if not search_name:
                continue

            # Try Levenshtein match
            alternatives = suggest_alternatives(search_name, all_names)
            if alternatives:
                best_name, dist = alternatives[0]
                # Look up where the best match lives
                find_result = self._store.find_symbol_by_name(best_name)
                if isinstance(find_result, Ok) and find_result.value:
                    sym = find_result.value[0]
                    error["suggestion"] = {
                        "source": "deterministic",
                        "fix": f"Did you mean '{best_name}' from {sym.file_path}?",
                        "confidence": max(0.0, 1.0 - dist * 0.2),
                        "reason": f"Levenshtein distance {dist}",
                    }
                    continue

            # Component matching
            component_matches = suggest_by_components(search_name, all_names)
            if component_matches:
                best_name, score = component_matches[0]
                comp_find = self._store.find_symbol_by_name(best_name)
                if isinstance(comp_find, Ok) and comp_find.value:
                    comp_sym = comp_find.value[0]
                    error["suggestion"] = {
                        "source": "deterministic",
                        "fix": f"Did you mean '{best_name}' from {comp_sym.file_path}?",
                        "confidence": min(0.85, score),
                        "reason": f"Component match (score {score:.2f})",
                    }
                    continue

            # Module export listing
            module_path = error.get("module_path")
            if module_path:
                exports_result = self._store.get_exports_by_module(module_path)
                if isinstance(exports_result, Ok) and exports_result.value:
                    export_names = [s.name for s in exports_result.value]
                    error["suggestion"] = {
                        "source": "deterministic",
                        "fix": (
                            f"'{search_name}' not found in {module_path}. "
                            f"Available exports: {', '.join(export_names[:5])}"
                        ),
                        "confidence": 0.7,
                        "reason": "Module export listing",
                    }
                    continue

            # Cross-index: check if the symbol exists at a different path
            find_result = self._store.find_symbol_by_name(search_name)
            if isinstance(find_result, Ok) and find_result.value:
                sym = find_result.value[0]
                error["suggestion"] = {
                    "source": "deterministic",
                    "fix": f"import from {sym.file_path}",
                    "confidence": 0.9,
                    "reason": f"{search_name} exists in {sym.file_path}",
                }

        return errors

    async def _get_diagnostics(self, code: str, file_path: str, ext: str) -> list[Diagnostic]:
        """Get LSP diagnostics for the given code. Returns empty if LSP unavailable."""
        if self._lsp_manager is None:
            return []

        server_result = await self._lsp_manager.ensure_server(ext)
        if isinstance(server_result, Err):
            log.warning("lsp_unavailable", ext=ext, error=server_result.error.message)
            return []

        client = server_result.value
        lang_result = get_language_id(ext)
        if isinstance(lang_result, Err):
            return []

        language_id = lang_result.value
        virtual_uri = self._make_virtual_uri(file_path)

        try:
            diagnostics = await client.open_and_get_diagnostics(virtual_uri, language_id, code)
        finally:
            await client.did_close(virtual_uri)
            client.clear_diagnostics(virtual_uri)

        return diagnostics

    async def validate(
        self, code: str, file_path: str, language: str | None = None
    ) -> Result[ValidationResult, GroundTruthError]:
        """Run all validators on proposed code using LSP diagnostics."""
        start = time.monotonic_ns()

        # Infer language if not provided
        lang = language or self._infer_language(file_path)
        if lang is None:
            return Ok(
                ValidationResult(
                    valid=None,
                    latency_ms=0,
                    degraded=True,
                    degraded_reason=(
                        f"Language not recognized for '{file_path}'; "
                        "no validation was performed"
                    ),
                )
            )

        ext = self._get_extension(file_path)

        all_errors: list[dict[str, Any]] = []

        # AST-based fast-path (no LSP needed) — uses language adapter
        adapter = get_adapter(lang)
        if adapter is not None:
            ast_validator = AstValidator(self._store, adapter)
            ast_result = ast_validator.validate(code, file_path, lang)
            if isinstance(ast_result, Ok):
                for ast_err in ast_result.value:
                    all_errors.append(
                        {
                            "type": ast_err.error_type,
                            "message": ast_err.message,
                            "symbol_name": ast_err.symbol_name,
                            "module_path": ast_err.module_path,
                            "evidence_type": ast_err.evidence_type,
                            "suggestion": None,
                        }
                    )

        diagnostics = await self._get_diagnostics(code, file_path, ext)
        diag_config = get_diagnostic_config(ext)

        # Run import validator
        import_result = self._import_validator.validate(diagnostics, file_path, lang, diag_config)
        if isinstance(import_result, Err):
            return Err(import_result.error)
        for err in import_result.value:
            error_dict: dict[str, Any] = {
                "type": err.error_type,
                "message": err.message,
                "symbol_name": err.symbol_name,
                "evidence_type": err.evidence_type,
                "suggestion": None,
            }
            if err.suggestion:
                error_dict["suggestion"] = {
                    "source": "deterministic",
                    "fix": err.suggestion,
                    "confidence": 0.9,
                    "reason": f"{err.symbol_name} found at different path",
                }
            all_errors.append(error_dict)

        # Run package validator
        pkg_result = self._package_validator.validate(diagnostics, file_path, lang, diag_config)
        if isinstance(pkg_result, Err):
            return Err(pkg_result.error)
        for pkg_err in pkg_result.value:
            pkg_dict: dict[str, Any] = {
                "type": "compiler_diagnostic",
                "message": pkg_err.message,
                "package_name": pkg_err.package_name,
                "evidence_type": pkg_err.evidence_type,
                "suggestion": None,
            }
            if pkg_err.suggestion:
                pkg_dict["suggestion"] = {
                    "source": "deterministic",
                    "fix": pkg_err.suggestion,
                    "confidence": 0.8,
                    "reason": "Package suggestion",
                }
            all_errors.append(pkg_dict)

        # Run signature validator
        sig_result = self._signature_validator.validate(diagnostics, file_path, lang, diag_config)
        if isinstance(sig_result, Err):
            return Err(sig_result.error)
        for sig_err in sig_result.value:
            all_errors.append(
                {
                    "type": "wrong_arg_count",
                    "message": sig_err.message,
                    "function_name": sig_err.function_name,
                    "evidence_type": "compiler_diagnostic",
                    "suggestion": None,
                }
            )

        # De-duplicate: keep first error per symbol_name
        seen_symbols: set[str] = set()
        deduped: list[dict[str, Any]] = []
        for err_dict in all_errors:
            key = err_dict.get("symbol_name") or err_dict.get("function_name") or ""
            if key and key in seen_symbols:
                continue
            if key:
                seen_symbols.add(key)
            deduped.append(err_dict)
        all_errors = deduped

        # Enrich errors without suggestions
        all_errors = self._enrich_with_suggestions(all_errors)

        elapsed_ns = time.monotonic_ns() - start
        latency_ms = max(1, elapsed_ns // 1_000_000)

        # If LSP was unavailable and AST found no errors, we cannot claim the code is valid.
        # AST checks catch syntax errors and some obvious import issues, but cannot verify
        # import correctness, type compatibility, or runtime semantics.
        lsp_ran = self._lsp_manager is not None
        if not lsp_ran and len(all_errors) == 0:
            return Ok(
                ValidationResult(
                    valid=None,
                    errors=[],
                    ai_used=False,
                    latency_ms=latency_ms,
                    degraded=True,
                    degraded_reason=(
                        "LSP unavailable — AST-only validation ran. "
                        "Import correctness and type errors cannot be verified without a language server."
                    ),
                )
            )

        return Ok(
            ValidationResult(
                valid=len(all_errors) == 0,
                errors=all_errors,
                ai_used=False,
                latency_ms=latency_ms,
            )
        )

    async def validate_with_ai(
        self, code: str, file_path: str, language: str | None = None
    ) -> Result[ValidationResult, GroundTruthError]:
        """Run deterministic validation, then AI resolution for unresolved errors."""
        det_result = await self.validate(code, file_path, language)
        if isinstance(det_result, Err):
            return det_result

        vr = det_result.value
        if not vr.errors or self._api_key is None:
            return det_result

        ai_used = False
        for error in vr.errors:
            if error.get("suggestion") is not None:
                continue

            error_msg = error.get("message", "")
            resolve_result = await self._resolver.resolve(
                error_message=error_msg,
                code_context=code,
                file_path=file_path,
            )
            if isinstance(resolve_result, Ok):
                res = resolve_result.value
                error["suggestion"] = {
                    "source": "ai",
                    "fix": res.suggested_fix,
                    "confidence": res.confidence,
                    "reason": res.reasoning,
                }
                ai_used = True

        vr.ai_used = ai_used
        return Ok(vr)
