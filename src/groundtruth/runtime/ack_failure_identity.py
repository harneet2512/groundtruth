"""Exact identity for source-state-bound acknowledged failures.

This module is producer-neutral: it knows neither seam kinds nor arbitration
classes.  It accepts only repository source identity, exact source bytes, and a
native syntax diagnostic.  Missing or ambiguous evidence returns ``None``.
"""

from __future__ import annotations

import posixpath
import re
from dataclasses import dataclass

__all__ = [
    "FailureIdentity",
    "build_syntax_failure_identity",
    "implicated_source_path",
    "syntax_payload_is_exhausted",
]

_SHA256_RE = re.compile(r"[0-9a-f]{64}")
# Colorized toolchain output must still yield exact identities: every anchored
# recognizer below is defeated by a leading CSI sequence, so raw diagnostics are
# de-colorized at the two public ingestion points before any matching.
_ANSI_CSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
_PY_LOCATION_RE = re.compile(
    r'^\s*File\s+["\'](?P<path>[^"\']+)["\'],\s*line\s+(?P<line>\d+)'
    r"(?:,\s*(?:column|col)\s+(?P<column>\d+))?",
    re.IGNORECASE,
)
_FRAME_RE = re.compile(
    r"^\s*(?P<path>[^\s:\r\n][^:\r\n]*?):(?P<line>\d+)"
    r"(?::(?P<column>\d+))?:\s*(?P<message>.*)$"
)
_PY_SYNTAX_RE = re.compile(
    r"^(?P<error>SyntaxError|IndentationError|TabError):\s*(?P<message>.+)$",
    re.IGNORECASE,
)
_SYNTAX_SIGNAL_RE = re.compile(
    r"\b(?:syntaxerror|indentationerror|taberror|invalid\s+syntax|syntax\s+error|"
    r"unexpected\s+(?:eof|token|end\s+of\s+input|identifier|keyword|indent|"
    r"newline|character)|unterminated|was\s+never\s+closed|parse\s+error|"
    r"missing\s+[)\]}]|unexpected\s+end|expected\b.*\bfound\b)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class FailureIdentity:
    source_path: str
    source_state_sha256: str
    diagnostic_category: str
    location: str
    diagnostic: str


def _repo_path(path: str, repo_root: str = "") -> str:
    value = (path or "").replace("\\", "/").strip()
    if not value:
        return ""
    normalized = posixpath.normpath(value)
    absolute = normalized.startswith("/") or bool(re.match(r"^[A-Za-z]:/", normalized))
    if absolute:
        root = posixpath.normpath((repo_root or "").replace("\\", "/").strip())
        root_absolute = root.startswith("/") or bool(re.match(r"^[A-Za-z]:/", root))
        if not root_absolute:
            return ""
        drive_path = bool(re.match(r"^[A-Za-z]:/", normalized))
        compare_path = normalized.casefold() if drive_path else normalized
        compare_root = root.casefold() if drive_path else root
        prefix = compare_root.rstrip("/") + "/"
        if not compare_path.startswith(prefix):
            return ""
        normalized = normalized[len(root.rstrip("/")) + 1 :]
    if normalized in {"", ".", ".."} or normalized.startswith("../"):
        return ""
    return normalized


def _locations(text: str, repo_root: str = "") -> list[tuple[str, str, str]]:
    rows: list[tuple[str, str, str]] = []
    for line in (text or "").splitlines():
        py = _PY_LOCATION_RE.match(line)
        if py:
            location = py.group("line")
            if py.group("column"):
                location += ":" + py.group("column")
            rows.append((_repo_path(py.group("path"), repo_root), location, ""))
            continue
        frame = _FRAME_RE.match(line)
        if frame:
            location = frame.group("line")
            if frame.group("column"):
                location += ":" + frame.group("column")
            rows.append(
                (
                    _repo_path(frame.group("path"), repo_root),
                    location,
                    frame.group("message").strip(),
                )
            )
    return [row for row in rows if row[0]]


def implicated_source_path(
    diagnostic: str, candidates: set[str], *, repo_root: str = ""
) -> str | None:
    """Return the one candidate named by an exact diagnostic location."""
    diagnostic = _ANSI_CSI_RE.sub("", diagnostic or "")
    normalized_candidates = {_repo_path(path, repo_root) for path in candidates or set()}
    normalized_candidates.discard("")
    matches = {
        path
        for path, _location, _message in _locations(diagnostic, repo_root)
        if path in normalized_candidates
    }
    return next(iter(matches)) if len(matches) == 1 else None


def _normalized_syntax_diagnostic(text: str, source_path: str, repo_root: str = "") -> str:
    candidates: list[str] = []
    for line in (text or "").splitlines():
        stripped = " ".join(line.strip().split())
        py = _PY_SYNTAX_RE.match(stripped)
        if py:
            candidates.append(f"{py.group('error')}:{py.group('message')}")
    for path, _location, message in _locations(text, repo_root):
        if path == source_path and message and _SYNTAX_SIGNAL_RE.search(message):
            candidates.append(message)
    normalized = {
        re.sub(r"\s*:\s*", ":", " ".join(value.split())).casefold() for value in candidates if value
    }
    return next(iter(normalized)) if len(normalized) == 1 else ""


def build_syntax_failure_identity(
    source_path: str,
    source_state_sha256: str,
    diagnostic: str,
    *,
    repo_root: str = "",
) -> FailureIdentity | None:
    """Build a complete exact identity, or ``None`` on missing/ambiguous truth."""
    diagnostic = _ANSI_CSI_RE.sub("", diagnostic or "")
    path = _repo_path(source_path, repo_root)
    digest = (source_state_sha256 or "").strip().lower()
    if not path or not _SHA256_RE.fullmatch(digest):
        return None
    matching_locations = {
        location
        for found_path, location, _message in _locations(diagnostic, repo_root)
        if found_path == path and location
    }
    if len(matching_locations) != 1:
        return None
    normalized_diagnostic = _normalized_syntax_diagnostic(diagnostic, path, repo_root)
    if not normalized_diagnostic:
        return None
    return FailureIdentity(
        source_path=path,
        source_state_sha256=digest,
        diagnostic_category="syntax_error",
        location=next(iter(matching_locations)),
        diagnostic=normalized_diagnostic,
    )


def syntax_payload_is_exhausted(identity: FailureIdentity | None, semantic_payload: str) -> bool:
    """Whether every rendered semantic line is already contained in ``identity``.

    The caller removes producer framing first. This kernel admits only the exact
    normalized diagnostic and exact source/location; any additional rendered
    fact makes the payload novel and therefore ineligible for whole suppression.
    """
    if identity is None or not semantic_payload:
        return False
    exact_location = f"{identity.source_path}:{identity.location}".casefold()
    exact_diagnostic = identity.diagnostic
    observed = False
    for line in semantic_payload.splitlines():
        normalized = " ".join(line.strip().split()).casefold()
        if not normalized:
            continue
        normalized = re.sub(r"\s*:\s*", ":", normalized)
        if normalized in {exact_location, "at " + exact_location, exact_diagnostic}:
            observed = True
            continue
        return False
    return observed
