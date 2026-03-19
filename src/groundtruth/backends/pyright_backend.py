"""Pyright backend — subprocess wrapper for type-checking diagnostics.

Runs Pyright on specified files and maps diagnostics to certainty lanes:
- green: closed-world facts (member access, missing imports)
- yellow: likely issues but not guaranteed
- red: unknown rules, ignored
"""
from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from typing import Any


@dataclass
class PyrightDiagnostic:
    """A single diagnostic from Pyright."""

    file: str
    line: int
    rule: str
    message: str
    severity: str  # error, warning, information
    certainty: str  # green, yellow, red


# Rules that Pyright has definitively enumerated the type's members for
_GREEN_RULES = frozenset({
    "reportAttributeAccessIssue",
    "reportMissingImports",
    "reportUndefinedVariable",
    "reportGeneralTypeIssues",
    "reportIndexIssue",
    "reportOperatorIssue",
    "reportArgumentType",
})

_YELLOW_RULES = frozenset({
    "reportMissingModuleSource",
    "reportMissingTypeStubs",
    "reportOptionalMemberAccess",
    "reportPossiblyUnbound",
})


def _classify_rule(rule: str) -> str:
    """Map a Pyright diagnostic rule to a certainty lane."""
    if rule in _GREEN_RULES:
        return "green"
    if rule in _YELLOW_RULES:
        return "yellow"
    return "red"


def run_pyright_on_files(
    files: list[str],
    cwd: str = "/testbed",
    timeout: int = 30,
) -> list[PyrightDiagnostic]:
    """Run Pyright on specified files and return diagnostics.

    Graceful degradation: returns [] if pyright is not found or times out.
    """
    if not files:
        return []

    try:
        result = subprocess.run(
            ["pyright", "--outputjson"] + files,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=cwd,
        )
    except FileNotFoundError:
        return []
    except subprocess.TimeoutExpired:
        return []
    except OSError:
        return []

    try:
        data = json.loads(result.stdout)
    except (json.JSONDecodeError, ValueError):
        return []

    diagnostics: list[PyrightDiagnostic] = []
    for diag in data.get("generalDiagnostics", []):
        rule = diag.get("rule", "")
        severity = diag.get("severity", "error")
        message = diag.get("message", "")
        file_path = diag.get("file", "")
        line = diag.get("range", {}).get("start", {}).get("line", 0)

        certainty = _classify_rule(rule)
        if certainty == "red":
            continue  # Skip unknown rules

        diagnostics.append(PyrightDiagnostic(
            file=file_path,
            line=line,
            rule=rule,
            message=message,
            severity=severity,
            certainty=certainty,
        ))

    return diagnostics


def run_pyright_on_files_stdlib(
    files: list[str],
    cwd: str = "/testbed",
    timeout: int = 30,
) -> list[dict[str, Any]]:
    """Stdlib-only version for Docker embedding. Returns list of plain dicts."""
    if not files:
        return []

    try:
        result = subprocess.run(
            ["pyright", "--outputjson"] + files,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=cwd,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return []

    try:
        data = json.loads(result.stdout)
    except (json.JSONDecodeError, ValueError):
        return []

    diagnostics: list[dict[str, Any]] = []
    for diag in data.get("generalDiagnostics", []):
        rule = diag.get("rule", "")
        certainty = _classify_rule(rule)
        if certainty == "red":
            continue

        diagnostics.append({
            "file": diag.get("file", ""),
            "line": diag.get("range", {}).get("start", {}).get("line", 0),
            "rule": rule,
            "message": diag.get("message", ""),
            "severity": diag.get("severity", "error"),
            "certainty": certainty,
        })

    return diagnostics
