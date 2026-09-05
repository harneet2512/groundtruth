"""Harness-agnostic source-edit predicates.

Extracted from the (retiring) OH wrapper at
``scripts/swebench/oh_gt_live_lite_v2_wrapper.py:438-489`` so that L3+L6 can
fire from any agent harness — SWE-agent, OpenHands, or a future runner — by
operating on ``(tool_name: str, args: dict)`` tuples instead of OH action
objects.

Two questions the L3+L6 site asks per agent action:

  1. Is this a source-file edit that should fire gt_hook + gt-index reindex?
     Answer via ``is_source_edit(tool_name, args) -> bool``.
  2. Which file did it edit? Answer via
     ``extract_edited_path(tool_name, args) -> str | None``.

Both questions are name- and shape-tolerant: they accept SWE-agent's
``str_replace_editor`` (the canonical post-OH editor name; verified on t0
at ``/home/ubuntu/SWE-agent/tools/edit_anthropic/config.yaml``) AND legacy
OH names (``FileEditAction``-style tool_name strings, ``CmdRunAction`` with
the editor invocation embedded in the bash command).

This module imports nothing from openhands or sweagent — it is pure stdlib.
"""

from __future__ import annotations

import re
from typing import Any, Mapping

# ---------------------------------------------------------------------------
# Verbs (kept verbatim from oh_gt_live_lite_v2_wrapper.py:438) plus the two
# verbs that SWE-agent's str_replace_editor adds: it has no `write` or `edit`
# verbs of its own, but bash redirection patterns produce them as effective
# verbs in the CmdRunAction path. Keep the OH set authoritative; treat the
# bash patterns separately below.
# ---------------------------------------------------------------------------
_MUTATING_EDITOR_CMDS: frozenset[str] = frozenset({"create", "str_replace", "insert"})

# Tool names that the editor may surface as. SWE-agent canonical:
# `str_replace_editor` (see /home/ubuntu/SWE-agent/tools/edit_anthropic/
# config.yaml). OH legacy: action class name (`FileEditAction`,
# `FileWriteAction`). The OH wrapper also handles `CmdRunAction` because the
# agent sometimes goes through a bash invocation for editor; we keep that
# path under the `bash` tool name below.
_EDITOR_TOOL_NAMES: frozenset[str] = frozenset(
    {
        # SWE-agent canonical names
        "str_replace_editor",
        "edit_anthropic",
        # OH legacy / direct file-mutation classes
        "FileEditAction",
        "FileWriteAction",
        "edit",
        "create",
        "write_file",
        "str_replace",
        "insert",
    }
)

# Bash-style tool names. When tool_name matches one of these, args["command"]
# is parsed for either an embedded `str_replace_editor`/`file_editor`
# invocation OR for shell redirection patterns that mutate a source file.
_BASH_TOOL_NAMES: frozenset[str] = frozenset({"bash", "execute_bash", "CmdRunAction", "shell"})

# Source-file extensions (verbatim from oh_gt_live_lite_v2_wrapper.py:484).
_SOURCE_EXTS: tuple[str, ...] = (".py", ".js", ".ts", ".go", ".java", ".rs", ".rb", ".php")

# Test-path regex (verbatim from oh_gt_live_lite_v2_wrapper.py:435).
_TEST_PATH_RE: re.Pattern[str] = re.compile(
    r"(^|/)(tests?|__tests__|spec|specs)/|(^|/)test_[^/]*\.py$|(^|/)[^/]*_test\.py$"
)

# Embedded editor invocation inside a bash command line. Mirrors the OH
# wrapper at oh_gt_live_lite_v2_wrapper.py:472. Captures (verb, path).
_EMBEDDED_EDITOR_RE: re.Pattern[str] = re.compile(
    r"(?:str_replace_editor|file_editor)\s+(\S+)\s+(\S+)"
)

# Shell redirection patterns hitting a source-extension file: `>`, `>>`, plus
# `tee FILE`, `dd of=FILE`. The OH wrapper does not handle bash redirection
# (it only matches embedded editor invocations), but the original code
# comment at line 425 contemplates it; we extend conservatively because the
# agent's tool-call distribution increasingly includes bash-only workflows.
# Keep this restrictive: must be a single-token redirect target with a
# source extension, not a heredoc / process-substitution sink.
_BASH_REDIRECT_RE: re.Pattern[str] = re.compile(
    r"(?:^|\s|;|&&|\|\|)"  # statement boundary
    r">>?\s*"  # > or >>
    r"([^\s|;&<>()]+\."  # path with extension (one path token)
    r"(?:py|js|ts|go|java|rs|rb|php))"
    r"(?=\s|$|;|&|\|)"  # statement terminator
)
_BASH_TEE_RE: re.Pattern[str] = re.compile(
    r"\btee\s+(?:-a\s+)?([^\s|;&<>()]+\."
    r"(?:py|js|ts|go|java|rs|rb|php))"
)


def _is_test_path(path: str) -> bool:
    """Return True iff the path matches a test-path pattern."""
    if not path:
        return False
    norm = path.replace("\\", "/")
    return bool(_TEST_PATH_RE.search(norm))


def _has_source_extension(path: str) -> bool:
    """Return True iff the path ends in a known source-file extension."""
    if not path:
        return False
    return path.endswith(_SOURCE_EXTS)


def _extract_path_from_editor_args(args: Mapping[str, Any]) -> str:
    """Pull a file path out of an editor-tool args dict.

    SWE-agent str_replace_editor uses ``args["path"]``. OH's FileEditAction
    used ``path`` or ``file_path``. We try both, then fall back to ``file``
    and ``filename`` for harness drift.
    """
    for key in ("path", "file_path", "file", "filename"):
        val = args.get(key)
        if isinstance(val, str) and val:
            return val
    return ""


def _editor_command_is_mutating(args: Mapping[str, Any]) -> bool:
    """For an editor tool, check if the ``command`` arg is a mutating verb.

    SWE-agent's str_replace_editor takes a ``command`` arg with allowed
    values {view, create, str_replace, insert, undo_edit}. Only
    create/str_replace/insert mutate. ``undo_edit`` and ``view`` do not.
    """
    cmd = args.get("command")
    if not isinstance(cmd, str):
        # If no command field, assume this is a direct-mutation tool
        # (FileWriteAction / write_file) where every invocation is a write.
        return True
    return cmd in _MUTATING_EDITOR_CMDS


def _extract_path_from_bash_command(command: str) -> tuple[str, bool]:
    """Pull an edited-file path out of a bash command line.

    Returns ``(path, is_mutating)``. ``is_mutating`` is False if the command
    is a known read-only invocation (e.g. `cat`, `ls`, `grep` against the
    file) even when the path itself looks edit-shaped. We only return a
    non-empty path when we can prove the command writes to it.
    """
    if not command:
        return "", False

    # 1. Embedded editor invocation: `str_replace_editor str_replace /foo/bar.py`
    m = _EMBEDDED_EDITOR_RE.search(command)
    if m:
        verb, candidate = m.group(1), m.group(2)
        if verb in _MUTATING_EDITOR_CMDS:
            return candidate, True
        # view/undo_edit/etc. — non-mutating.
        return candidate, False

    # 2. Shell redirection: `echo foo > bar.py`, `cat x >> bar.py`,
    #    `tee bar.py`. Must hit a source-extension file.
    m_redir = _BASH_REDIRECT_RE.search(command)
    if m_redir:
        return m_redir.group(1), True
    m_tee = _BASH_TEE_RE.search(command)
    if m_tee:
        return m_tee.group(1), True

    return "", False


def is_source_edit(tool_name: str, args: Mapping[str, Any]) -> bool:
    """Return True iff this (tool_name, args) call mutates a non-test source file.

    Mirrors the OH-wrapper logic at
    ``scripts/swebench/oh_gt_live_lite_v2_wrapper.py:445`` but operates on a
    harness-agnostic (tool_name, args) tuple. False on:

      - Read-only tool calls (view, read, ls, find_file, grep, cat, search_*)
      - Test-path edits (matches _TEST_PATH_RE)
      - Non-source-extension files (no .py/.js/.ts/.go/.java/.rs/.rb/.php)
      - Bash commands that don't write to a source file
      - editor view / undo_edit verbs

    True on:
      - str_replace_editor with command in {create, str_replace, insert}
      - FileEditAction / FileWriteAction class names with a path arg
      - bash with `>`/`>>`/`tee` redirection to a source file
    """
    if not tool_name:
        return False

    # Editor-class tools: check command verb (if any) and path.
    if tool_name in _EDITOR_TOOL_NAMES:
        if not _editor_command_is_mutating(args):
            return False
        path = _extract_path_from_editor_args(args)
        if not path:
            return False
        # Editor tools sometimes operate on non-source files (.md, .txt,
        # config). The OH wrapper allows that path for FileEdit/Write
        # (line 483 special-cases the non-CmdRun path), but we want L3 to
        # fire ONLY on graph-tracked source files — matches the rest of the
        # GT pipeline (gt-index parses only the source extensions above).
        if not _has_source_extension(path):
            return False
        if _is_test_path(path):
            return False
        return True

    # Bash-class tools: parse the command for editor invocations or
    # shell redirection writes.
    if tool_name in _BASH_TOOL_NAMES:
        command = args.get("command")
        if not isinstance(command, str):
            return False
        path, is_mutating = _extract_path_from_bash_command(command)
        if not path or not is_mutating:
            return False
        if not _has_source_extension(path):
            return False
        if _is_test_path(path):
            return False
        return True

    # Unknown tool name: conservatively say no. Read-only tools like
    # `view`, `read`, `find_file`, `search_dir`, `grep`, `ls`, `cat`,
    # browse-class tools, etc. all fall through here.
    return False


def extract_edited_path(tool_name: str, args: Mapping[str, Any]) -> str | None:
    """Return the (relative or absolute) path of the edited file.

    Returns None when the call isn't a source edit OR when the path can't
    be extracted. Callers should treat None as "no L3/L6 fire".

    Note: returns the path verbatim — no normalization to repo-relative.
    The caller (state command) is responsible for stripping any `/workspace`
    or `/testbed` prefix when invoking gt-index.
    """
    if not is_source_edit(tool_name, args):
        return None
    if tool_name in _EDITOR_TOOL_NAMES:
        path = _extract_path_from_editor_args(args)
        return path or None
    if tool_name in _BASH_TOOL_NAMES:
        command = args.get("command", "")
        if not isinstance(command, str):
            return None
        path, _ = _extract_path_from_bash_command(command)
        return path or None
    return None
