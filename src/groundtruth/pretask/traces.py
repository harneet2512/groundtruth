"""Module 2 — Stack-trace parser.

Per-language regex registry that turns stack traces in an issue body into
structured ``StackFrame`` records, then filters frames whose paths fall
outside the repository (drops stdlib / site-packages noise).

Source: arxiv 2412.03905 — the deepest in-repo frame correlates with bug
location at 98.3% on real-world failures.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass


@dataclass(frozen=True)
class StackFrame:
    """A single resolved stack-trace frame.

    Attributes:
        file: Path as it appeared in the trace (raw — orchestrator can
            ``os.path.realpath`` it later).
        line: Line number in ``file``.
        func: Function name, or empty string when the language's frame
            format does not surface one (Go is the common case).
        lang: Source-language tag produced by the matching regex.
    """

    file: str
    line: int
    func: str
    lang: str


# ----------------------------------------------------------------- regexes
# Each entry: (lang, compiled_regex, (file_group, line_group, func_group)).
# func_group may be 0 if the format does not surface a function name.
_REGISTRY: tuple[tuple[str, re.Pattern[str], tuple[int, int, int]], ...] = (
    # Python: ``File "path", line N, in func``
    (
        "python",
        re.compile(r'File "([^"]+)", line (\d+), in ([A-Za-z_][A-Za-z0-9_]*)'),
        (1, 2, 3),
    ),
    # JavaScript / TypeScript V8 format: ``at fn (path:line:col)``
    (
        "javascript",
        re.compile(
            r"at\s+([A-Za-z_$][A-Za-z0-9_$.<>]*)\s+\(([^()\s]+):(\d+):\d+\)"
        ),
        (2, 3, 1),
    ),
    # Java: ``at pkg.Class.method(File.java:line)``
    (
        "java",
        re.compile(r"at\s+([\w.$]+)\(([\w$.-]+\.java):(\d+)\)"),
        (2, 3, 1),
    ),
    # Go runtime: ``\tpath/file.go:line +0xNN``
    (
        "go",
        re.compile(r"([^\s:]+\.go):(\d+)(?:\s+\+0x[0-9a-fA-F]+)?"),
        (1, 2, 0),
    ),
    # Rust: ``at path/to/file.rs:line``
    (
        "rust",
        re.compile(r"at\s+([^\s:]+\.rs):(\d+)"),
        (1, 2, 0),
    ),
    # C / C++ gdb: ``#N 0xADDR in func at path:line``
    (
        "c",
        re.compile(
            r"#\d+\s+0x[0-9a-fA-F]+\s+in\s+([A-Za-z_][A-Za-z0-9_]*)\s+at\s+([^\s:]+):(\d+)"
        ),
        (2, 3, 1),
    ),
)


def _frames_from_text(text: str) -> list[StackFrame]:
    """Run every regex in the registry over ``text`` and return raw frames.

    Frames are returned in the order they appear in the text, per regex.
    Deduplication is applied at the end (same file/line/func collapses).
    """
    seen: set[tuple[str, int, str, str]] = set()
    out: list[StackFrame] = []
    for lang, pattern, (gf, gl, gfn) in _REGISTRY:
        for match in pattern.finditer(text):
            try:
                file_ = match.group(gf)
                line_ = int(match.group(gl))
            except (ValueError, IndexError):
                continue
            func_ = match.group(gfn) if gfn else ""
            key = (file_, line_, func_, lang)
            if key in seen:
                continue
            seen.add(key)
            out.append(StackFrame(file=file_, line=line_, func=func_, lang=lang))
    return out


def _is_in_repo(path: str, repo_root: str) -> bool:
    """True if ``path`` resolves under ``repo_root``.

    Uses ``os.path.realpath`` to handle symlinks; falls back to a string
    prefix check when realpath fails (e.g. the path is relative and the
    file does not exist on the running host).
    """
    if not path or not repo_root:
        return False
    raw_norm = path.replace("\\", "/")
    bad_markers = (
        "site-packages",
        "dist-packages",
        "/usr/",
        "/lib/",
        "node_modules/",
        "vendor/",
        "target/",
        ".cargo/",
        ".gem/",
        ".pub-cache/",
    )
    if raw_norm.startswith(("node:", "internal/")):
        return False
    if any(m in raw_norm for m in bad_markers):
        return False
    try:
        rp = os.path.realpath(path)
        rr = os.path.realpath(repo_root)
    except (OSError, ValueError):
        rp, rr = path, repo_root

    rp_norm = rp.replace("\\", "/")
    rr_norm = rr.replace("\\", "/").rstrip("/")
    if rp_norm.startswith(rr_norm + "/") or rp_norm == rr_norm:
        return True

    # Fallback for relative paths that came in as ``patroni/watchdog.py``:
    # treat them as in-repo if they don't reference a vendored / system
    # path (common stdlib + per-language third-party markers).
    if not os.path.isabs(path):
        return True

    return False


def parse_stack_traces(
    issue_text: str,
    repo_root: str,
) -> list[StackFrame]:
    """Return in-repo stack frames, deepest-first.

    "Deepest" means last in textual order — Python tracebacks list the
    failing frame at the bottom, JavaScript at the top, but for our
    purpose the textual ordering already biases toward the in-repo frame
    nearest the failure for Python (which is the primary target language
    for SWE-bench-style issues). The orchestrator can re-rank if needed.

    Args:
        issue_text: Raw issue body that may contain one or more tracebacks.
        repo_root: Filesystem path of the repository being analyzed.
            Frames whose file does not resolve under this root are dropped.

    Returns:
        List of StackFrame, in source-text order, repo-filtered.
    """
    if not issue_text:
        return []
    raw = _frames_from_text(issue_text)
    in_repo = [fr for fr in raw if _is_in_repo(fr.file, repo_root)]

    # Language-aware ordering. In Python tracebacks the failing frame is
    # printed LAST (deepest at bottom). In V8 / JavaScript, Java, and
    # most C-style traces it is printed FIRST (deepest at top). We want
    # the deepest in-repo frame FIRST in the returned list either way.
    bottom_deepest = {"python"}
    py_frames = [fr for fr in in_repo if fr.lang in bottom_deepest]
    other_frames = [fr for fr in in_repo if fr.lang not in bottom_deepest]
    return list(reversed(py_frames)) + other_frames
