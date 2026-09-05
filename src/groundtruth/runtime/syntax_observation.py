"""Immutable producer inputs for an ``edit.syntax`` truth attestation.

The syntax checker owns these facts.  This module converts its structured result
and the exact checked source bytes into deterministic host-side data; it performs
no I/O and emits no model-facing bytes.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Mapping

_PY_FILE_RE = re.compile(r'^\s*File "(?P<path>[^"]+)", line (?P<line>\d+)', re.MULTILINE)
_GENERIC_LOCATION_RE = re.compile(
    r"^\s*(?P<path>[^\r\n]+?):(?P<line>\d+):(?P<column>\d+):",
    re.MULTILINE,
)
_CATEGORY_RE = re.compile(
    r"\b(SyntaxError|IndentationError|TabError|Parse\s+Error)\b", re.IGNORECASE
)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _normalize_diagnostic(value: object) -> str:
    if not isinstance(value, str):
        return ""
    lines = [line.rstrip() for line in value.replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    while lines and not lines[0]:
        lines.pop(0)
    while lines and not lines[-1]:
        lines.pop()
    return "\n".join(lines)


def _snake_category(value: str) -> str:
    compact = re.sub(r"\s+", "", value)
    return re.sub(r"(?<!^)(?=[A-Z])", "_", compact).lower()


@dataclass(frozen=True)
class SyntaxDiagnosticLocation:
    path: str
    line: int
    column: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"path": self.path, "line": self.line, "column": self.column}


@dataclass(frozen=True)
class SyntaxObservation:
    file_path: str
    source_sha256: str
    source_bytes_length: int
    diagnostic_sha256: str
    diagnostic_category: str | None
    diagnostic_location: SyntaxDiagnosticLocation | None
    rendered_sha256_16: str
    rendered_chars: int
    rendered_bytes_length: int
    checker: tuple[str, ...]
    language: str
    verdict: str
    reason: str
    actual_event: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "file_path": self.file_path,
            "source_sha256": self.source_sha256,
            "source_bytes_length": self.source_bytes_length,
            "diagnostic_sha256": self.diagnostic_sha256,
            "diagnostic_category": self.diagnostic_category,
            "diagnostic_location": (
                self.diagnostic_location.to_dict() if self.diagnostic_location else None
            ),
            "rendered_sha256_16": self.rendered_sha256_16,
            "rendered_chars": self.rendered_chars,
            "rendered_bytes_length": self.rendered_bytes_length,
            "checker": list(self.checker),
            "language": self.language,
            "verdict": self.verdict,
            "reason": self.reason,
            "actual_event": self.actual_event,
        }

    def canonical_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))


def _diagnostic_location(diagnostic: str) -> SyntaxDiagnosticLocation | None:
    py_matches = list(_PY_FILE_RE.finditer(diagnostic))
    if py_matches:
        match = py_matches[-1]
        column: int | None = None
        following = diagnostic[match.end() :].lstrip("\n").splitlines()
        # Python's native diagnostic prefixes both source and caret with four
        # display spaces. Convert the caret back to a one-based source column.
        if len(following) >= 2 and "^" in following[1]:
            column = max(1, following[1].find("^") - 4 + 1)
        return SyntaxDiagnosticLocation(
            path=match.group("path").replace("\\", "/"),
            line=int(match.group("line")),
            column=column,
        )
    match = _GENERIC_LOCATION_RE.search(diagnostic)
    if match:
        return SyntaxDiagnosticLocation(
            path=match.group("path").strip().replace("\\", "/"),
            line=int(match.group("line")),
            column=int(match.group("column")),
        )
    return None


def build_syntax_observation(
    *,
    file_path: str,
    source_bytes: bytes,
    check_result: Mapping[str, Any],
    actual_event: str,
    rendered_block: str,
) -> SyntaxObservation:
    """Build deterministic producer truth from exact checked bytes and result."""
    if not isinstance(source_bytes, bytes):
        raise TypeError("source_bytes must be bytes")
    diagnostic = _normalize_diagnostic(check_result.get("diagnostic"))
    category_match = _CATEGORY_RE.search(diagnostic)
    verdict = str(check_result.get("verdict") or "unavailable")
    category = (
        _snake_category(category_match.group(1))
        if category_match
        else ("syntax_error" if verdict == "syntax_error" else None)
    )
    checker_raw = check_result.get("checker")
    checker = (
        tuple(item for item in checker_raw if isinstance(item, str) and item)
        if isinstance(checker_raw, (list, tuple))
        else ()
    )
    rendered_bytes = rendered_block.encode("utf-8", "surrogatepass")
    return SyntaxObservation(
        file_path=str(file_path or "").replace("\\", "/"),
        source_sha256=_sha256(source_bytes),
        source_bytes_length=len(source_bytes),
        diagnostic_sha256=_sha256(diagnostic.encode("utf-8", "surrogatepass")),
        diagnostic_category=category,
        diagnostic_location=_diagnostic_location(diagnostic),
        rendered_sha256_16=_sha256(rendered_bytes)[:16],
        rendered_chars=len(rendered_block),
        rendered_bytes_length=len(rendered_bytes),
        checker=checker,
        language=str(check_result.get("language") or ""),
        verdict=verdict,
        reason=str(check_result.get("reason") or ""),
        actual_event=str(actual_event or ""),
    )
