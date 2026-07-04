"""Cross-platform utilities for path handling and subprocess resolution."""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path
from urllib.parse import unquote, urlparse


def is_windows() -> bool:
    """Check if running on Windows."""
    return sys.platform == "win32"


def resolve_command(cmd: list[str]) -> list[str]:
    """Resolve an executable command using shutil.which().

    On Windows, this finds .cmd/.bat/.exe shims (e.g. pyright-langserver.cmd).
    When the resolved path is a .cmd/.bat file, prepends cmd.exe /c so that
    asyncio.create_subprocess_exec can execute it (it cannot run shell scripts directly).

    Also handles the case where shutil.which() resolves to an extensionless Unix shell
    script (common with npm shims under MSYS/Git Bash on Windows) — if a .cmd sibling
    exists, uses that instead.

    Returns the original list if the executable cannot be resolved.
    """
    if not cmd:
        return cmd
    resolved = shutil.which(cmd[0])
    if resolved is not None:
        if is_windows():
            resolved_lower = resolved.lower()
            # Already a .cmd/.bat → wrap with cmd.exe /c
            if resolved_lower.endswith((".cmd", ".bat")):
                return ["cmd.exe", "/c", resolved] + cmd[1:]
            # Extensionless file (Unix shell script from npm) → check for .cmd sibling
            if not resolved_lower.endswith(".exe"):
                cmd_sibling = resolved + ".cmd"
                if os.path.isfile(cmd_sibling):
                    return ["cmd.exe", "/c", cmd_sibling] + cmd[1:]
        return [resolved] + cmd[1:]
    return cmd


def normalize_path(path: str) -> str:
    """Normalize a filesystem path: resolve . and .. components, convert backslashes to /."""
    return os.path.normpath(path).replace("\\", "/")


def path_to_uri(path: str) -> str:
    """Convert a filesystem path to a file:// URI."""
    return Path(os.path.abspath(path)).as_uri()


def uri_to_path(uri: str) -> str:
    """Convert a file:// URI to a normalized filesystem path.

    Handles Windows drive letters (file:///C:/foo) and encoded characters.
    If the input is not a file:// URI, returns it normalized as-is.
    """
    if not uri.startswith("file://"):
        return normalize_path(uri)

    parsed = urlparse(uri)
    # parsed.path for file:///C:/foo is /C:/foo — on Windows, strip leading /
    path = unquote(parsed.path)

    if is_windows():
        if parsed.netloc:
            # UNC path: file://server/share/file.py → //server/share/file.py
            path = "//" + parsed.netloc + path
        elif len(path) >= 3 and path[0] == "/" and path[2] == ":":
            path = path[1:]

    return normalize_path(path)


def is_macos() -> bool:
    """Check if running on macOS."""
    return sys.platform == "darwin"


def validate_path(file_path: str, root_path: str) -> tuple[bool, str]:
    """Validate that file_path is under root_path (no path traversal).

    Relative paths are resolved against root_path.
    Returns (ok, resolved_path). If ok is False, resolved_path contains an error message.
    """
    try:
        root = Path(root_path).resolve()
        p = Path(file_path)
        if not p.is_absolute():
            resolved = (root / p).resolve()
        else:
            resolved = p.resolve()
        if resolved.is_relative_to(root):
            return (True, str(resolved))
        return (False, f"Path escapes root: {file_path}")
    except (ValueError, OSError) as exc:
        return (False, f"Invalid path: {exc}")


def paths_equal(a: str, b: str) -> bool:
    """Compare two paths for equality after normalization.

    Case-insensitive on Windows and macOS (APFS/HFS+ default), case-sensitive elsewhere.
    """
    na = normalize_path(a)
    nb = normalize_path(b)
    if is_windows() or is_macos():
        return na.lower() == nb.lower()
    return na == nb
