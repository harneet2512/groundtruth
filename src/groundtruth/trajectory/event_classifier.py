"""Generalized event classifier — maps raw agent actions to event buckets, file kinds, check kinds.

Decision 34: All classification is framework-agnostic. No pytest/jest/cargo names in output.
"""

from __future__ import annotations

import os
import re

_SCAFFOLD_PREFIXES = (
    "reproduce", "repro_", "debug_", "tmp_", "test_fix", "scratch_",
    "temp_", "throwaway_", "local_", "hack_",
)

_SCAFFOLD_DIRS = {
    "reproduce", "debug", "scratch", "tmp", "temp", "throwaway",
}

_GENERATED_DIRS = {
    "gen", "generated", "__generated__", "_generated",
    "vendor", "node_modules", "dist", "build",
}

_CONFIG_EXTS = {
    ".yml", ".yaml", ".toml", ".json", ".cfg", ".ini", ".env",
    ".conf", ".config", ".properties", ".xml",
}

_SOURCE_EXTS = {
    ".py", ".js", ".ts", ".jsx", ".tsx", ".go", ".rs", ".java",
    ".c", ".cpp", ".h", ".hpp", ".rb", ".php", ".swift", ".kt",
    ".scala", ".cs", ".lua", ".zig", ".ex", ".exs", ".clj",
    ".ml", ".hs", ".erl", ".dart", ".r", ".jl", ".pl",
}

_TEST_PATTERNS = [
    re.compile(r"(?:^|/)tests?/"),
    re.compile(r"(?:^|/)test_\w+\."),
    re.compile(r"(?:^|/)\w+_test\."),
    re.compile(r"(?:^|/)spec/"),
    re.compile(r"\.(?:test|spec)\.\w+$"),
    re.compile(r"(?:^|/)conftest\.py$"),
    re.compile(r"(?:^|/)fixtures?/"),
]

_SEARCH_PATTERNS = [
    re.compile(r"\bgrep\b"),
    re.compile(r"\brg\b"),
    re.compile(r"\bag\b"),
    re.compile(r"\bfind\b.*-name"),
    re.compile(r"\bfind_file\b"),
    re.compile(r"\bsearch_dir\b"),
    re.compile(r"\bsearch_file\b"),
    re.compile(r"\bsearch\b"),
]


def classify_file_kind(path: str) -> str:
    """Classify a file path into a generalized file kind."""
    if not path:
        return "UNKNOWN_FILE"

    norm = path.replace("\\", "/").lower()
    fname = os.path.basename(norm)
    parts = set(norm.split("/"))

    if parts & _GENERATED_DIRS:
        return "GENERATED_FILE"

    if any(fname.startswith(p) for p in _SCAFFOLD_PREFIXES):
        return "SCAFFOLD_FILE"
    if parts & _SCAFFOLD_DIRS:
        return "SCAFFOLD_FILE"

    ext = os.path.splitext(fname)[1]

    if ext in _CONFIG_EXTS:
        return "CONFIG_FILE"

    for pat in _TEST_PATTERNS:
        if pat.search(norm):
            return "VALIDATION_FILE"

    if ext in _SOURCE_EXTS:
        return "DURABLE_PRODUCT_FILE"

    return "UNKNOWN_FILE"


def classify_check_kind(
    command: str,
    edited_files: list[str] | None = None,
) -> str:
    """Classify a verification command into a generalized check kind.

    Uses the existing classifier module for command kind detection,
    then maps to generalized check kinds.
    """
    from .classifier import (
        classify_command, classify_verification_targeting,
        is_verification_command, CommandKind,
    )

    if not command:
        return "NO_CHECK"

    cmd_kind = classify_command(command)

    if cmd_kind == CommandKind.INSTALL:
        return "SETUP_OR_INSTALL"

    if not is_verification_command(command):
        if cmd_kind == CommandKind.BUILD:
            return "STATIC_SANITY"
        return "NO_CHECK"

    if cmd_kind in (CommandKind.TYPECHECK, CommandKind.LINT):
        return "STATIC_SANITY"

    if not edited_files:
        return "BROAD_CHECK"

    targeting = classify_verification_targeting(command, edited_files)
    target_map = {
        "targeted_to_edited_symbol": "TARGETED_CHECK",
        "targeted_to_edited_file": "TARGETED_CHECK",
        "targeted_to_related_test": "TARGETED_CHECK",
        "broad_project_verification": "BROAD_CHECK",
        "irrelevant_verification": "IRRELEVANT_CHECK",
    }
    return target_map.get(targeting.value, "UNKNOWN_CHECK")


def classify_verification_strength(
    check_kind: str,
    structural_witness_followed: bool = False,
) -> str:
    """Classify the strength of a verification action."""
    if structural_witness_followed:
        return "STRONG"
    if check_kind == "TARGETED_CHECK":
        return "STRONG"
    if check_kind == "STATIC_SANITY":
        return "STRONG"
    if check_kind == "BROAD_CHECK":
        return "WEAK"
    if check_kind in ("IRRELEVANT_CHECK", "SETUP_OR_INSTALL", "UNKNOWN_CHECK"):
        return "WEAK"
    return "NONE"


def classify_event_bucket(
    action_type: str,
    command: str | None = None,
    is_finish: bool = False,
) -> str:
    """Classify an agent action into a generalized event bucket."""
    if is_finish:
        return "FINISH_TERMINAL"

    if action_type in ("edit_file", "write_file", "FileEditAction", "FileWriteAction"):
        return "EDIT_COMMITMENT"

    if action_type in ("read_file", "FileReadAction", "browse"):
        return "OPEN_INSPECT"

    if action_type in ("run_command", "CmdRunAction") and command:
        for pat in _SEARCH_PATTERNS:
            if pat.search(command):
                return "SEARCH"

        from .classifier import is_verification_command, classify_command, CommandKind
        if is_verification_command(command):
            return "VERIFICATION_CHECK"

        cmd_kind = classify_command(command)
        if cmd_kind == CommandKind.BUILD:
            return "VERIFICATION_CHECK"
        if cmd_kind == CommandKind.INSTALL:
            return "ENVIRONMENT"

        return "ORIENTATION"

    return "ORIENTATION"


def is_search_command(command: str) -> bool:
    """Check if a command is a search/grep type."""
    if not command:
        return False
    for pat in _SEARCH_PATTERNS:
        if pat.search(command):
            return True
    return False
