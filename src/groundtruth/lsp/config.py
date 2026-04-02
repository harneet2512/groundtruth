"""LSP server configuration — the only language-aware file."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from groundtruth.utils.result import Err, GroundTruthError, Ok, Result


class LSPServerConfig(BaseModel):
    """Configuration for an LSP server."""

    command: list[str]
    initialization_options: dict[str, Any] | None = None
    settings: dict[str, Any] | None = None


# The only language-aware mapping. Adding a new language = one entry.
LSP_SERVERS: dict[str, LSPServerConfig] = {
    ".py": LSPServerConfig(command=["pyright-langserver", "--stdio"]),
    ".ts": LSPServerConfig(command=["typescript-language-server", "--stdio"]),
    ".tsx": LSPServerConfig(command=["typescript-language-server", "--stdio"]),
    ".js": LSPServerConfig(command=["typescript-language-server", "--stdio"]),
    ".jsx": LSPServerConfig(command=["typescript-language-server", "--stdio"]),
    ".go": LSPServerConfig(command=["gopls", "serve", "-stdio"]),
    ".rs": LSPServerConfig(command=["rust-analyzer"]),
    ".java": LSPServerConfig(command=["jdtls"]),
}

LANGUAGE_IDS: dict[str, str] = {
    ".py": "python",
    ".ts": "typescript",
    ".tsx": "typescriptreact",
    ".js": "javascript",
    ".jsx": "javascriptreact",
    ".go": "go",
    ".rs": "rust",
    ".java": "java",
}


def get_server_config(ext: str) -> Result[LSPServerConfig, GroundTruthError]:
    """Get the LSP server config for a file extension."""
    config = LSP_SERVERS.get(ext)
    if config is None:
        return Err(
            GroundTruthError(
                code="unsupported_language",
                message=f"No LSP server configured for extension: {ext}",
            )
        )
    return Ok(config)


def get_language_id(ext: str) -> Result[str, GroundTruthError]:
    """Get the LSP language identifier for a file extension."""
    lang_id = LANGUAGE_IDS.get(ext)
    if lang_id is None:
        return Err(
            GroundTruthError(
                code="unsupported_language",
                message=f"No language ID for extension: {ext}",
            )
        )
    return Ok(lang_id)


class DiagnosticCodeConfig(BaseModel):
    """Maps LSP diagnostic codes to error categories for a specific server."""

    unresolved_import: list[str | int]
    wrong_arg_count: list[str | int]
    source: str


DIAGNOSTIC_CODES: dict[str, DiagnosticCodeConfig] = {
    ".py": DiagnosticCodeConfig(
        unresolved_import=["reportMissingImports", "reportMissingModuleSource"],
        wrong_arg_count=["reportCallIssue", "reportGeneralClassIssue"],
        source="Pyright",
    ),
    ".ts": DiagnosticCodeConfig(
        unresolved_import=[2307, 2305],
        wrong_arg_count=[2554, 2555],
        source="typescript",
    ),
    ".tsx": DiagnosticCodeConfig(
        unresolved_import=[2307, 2305],
        wrong_arg_count=[2554, 2555],
        source="typescript",
    ),
    ".js": DiagnosticCodeConfig(
        unresolved_import=[2307, 2305],
        wrong_arg_count=[2554, 2555],
        source="typescript",
    ),
    ".jsx": DiagnosticCodeConfig(
        unresolved_import=[2307, 2305],
        wrong_arg_count=[2554, 2555],
        source="typescript",
    ),
    ".go": DiagnosticCodeConfig(
        unresolved_import=["UndeclaredImportedName"],
        wrong_arg_count=["WrongArgCount"],
        source="gopls",
    ),
    ".rs": DiagnosticCodeConfig(
        unresolved_import=["E0432", "E0433"],
        wrong_arg_count=["E0061"],
        source="rust-analyzer",
    ),
    ".java": DiagnosticCodeConfig(
        unresolved_import=["268435846"],  # jdt.ls unresolved import
        wrong_arg_count=["67108964"],  # jdt.ls wrong arg count
        source="jdt.ls",
    ),
}


def get_diagnostic_config(ext: str) -> DiagnosticCodeConfig | None:
    """Get diagnostic code config for a file extension. Returns None for unknown servers."""
    return DIAGNOSTIC_CODES.get(ext)
