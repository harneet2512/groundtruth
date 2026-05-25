"""Unit tests for src/groundtruth/edit_predicates.py.

Covers the OH-wrapper-extracted predicate behavior plus SWE-agent's
str_replace_editor canonical names. All tests are pure / synthetic — no
SWE-agent / OH imports.
"""

from __future__ import annotations

import pytest

from groundtruth.edit_predicates import (
    extract_edited_path,
    is_source_edit,
)


# ---------------------------------------------------------------------------
# True cases — should fire L3+L6
# ---------------------------------------------------------------------------

def test_str_replace_editor_str_replace_python_file_is_edit() -> None:
    """SWE-agent canonical: str_replace_editor with command=str_replace on .py."""
    args = {
        "command": "str_replace",
        "path": "/workspace/repo/src/foo.py",
        "old_str": "old",
        "new_str": "new",
    }
    assert is_source_edit("str_replace_editor", args) is True
    assert extract_edited_path("str_replace_editor", args) == "/workspace/repo/src/foo.py"


def test_str_replace_editor_create_python_file_is_edit() -> None:
    """SWE-agent: command=create on .py."""
    args = {
        "command": "create",
        "path": "/workspace/repo/src/new.py",
        "file_text": "x = 1\n",
    }
    assert is_source_edit("str_replace_editor", args) is True


def test_str_replace_editor_insert_typescript_file_is_edit() -> None:
    """SWE-agent: command=insert on .ts."""
    args = {
        "command": "insert",
        "path": "/workspace/repo/src/index.ts",
        "insert_line": 10,
        "new_str": "const x = 1;",
    }
    assert is_source_edit("str_replace_editor", args) is True


def test_oh_legacy_file_edit_action_python_is_edit() -> None:
    """OH legacy class name FileEditAction with path arg."""
    args = {"path": "src/foo.py", "command": "str_replace"}
    assert is_source_edit("FileEditAction", args) is True
    assert extract_edited_path("FileEditAction", args) == "src/foo.py"


def test_bash_redirect_python_file_is_edit() -> None:
    """Bash with `> file.py` should fire."""
    args = {"command": "echo 'hello' > /workspace/repo/src/x.py"}
    assert is_source_edit("bash", args) is True
    assert extract_edited_path("bash", args) == "/workspace/repo/src/x.py"


def test_bash_append_redirect_go_file_is_edit() -> None:
    """Bash with `>> file.go`."""
    args = {"command": "cat patch.txt >> main.go"}
    assert is_source_edit("bash", args) is True
    assert extract_edited_path("bash", args) == "main.go"


def test_bash_embedded_str_replace_editor_invocation() -> None:
    """Bash command containing `str_replace_editor str_replace path` — OH legacy."""
    args = {
        "command": "str_replace_editor str_replace /workspace/foo.py --old_str a --new_str b"
    }
    assert is_source_edit("bash", args) is True
    assert extract_edited_path("bash", args) == "/workspace/foo.py"


# ---------------------------------------------------------------------------
# False cases — should NOT fire L3+L6
# ---------------------------------------------------------------------------

def test_str_replace_editor_view_python_file_is_not_edit() -> None:
    """View is read-only."""
    args = {"command": "view", "path": "/workspace/repo/src/foo.py"}
    assert is_source_edit("str_replace_editor", args) is False
    assert extract_edited_path("str_replace_editor", args) is None


def test_str_replace_editor_undo_edit_python_file_is_not_edit() -> None:
    """undo_edit reverts but per the OH wrapper is treated as non-mutating
    for hook purposes (gt_hook is post-edit; we don't want to fire after
    undo)."""
    args = {"command": "undo_edit", "path": "/workspace/repo/src/foo.py"}
    assert is_source_edit("str_replace_editor", args) is False


def test_bash_cat_is_not_edit() -> None:
    """Reading a file via cat — read-only."""
    args = {"command": "cat /workspace/repo/src/foo.py"}
    assert is_source_edit("bash", args) is False
    assert extract_edited_path("bash", args) is None


def test_bash_ls_is_not_edit() -> None:
    """ls — read-only."""
    args = {"command": "ls /workspace/repo/src/"}
    assert is_source_edit("bash", args) is False


def test_bash_grep_is_not_edit() -> None:
    """grep — read-only."""
    args = {"command": "grep -r 'foo' /workspace/repo/src/"}
    assert is_source_edit("bash", args) is False


def test_str_replace_editor_test_path_is_not_edit() -> None:
    """Edits on test paths are excluded per OH wrapper line 487."""
    args = {
        "command": "str_replace",
        "path": "/workspace/repo/tests/test_foo.py",
        "old_str": "x",
        "new_str": "y",
    }
    assert is_source_edit("str_replace_editor", args) is False


def test_str_replace_editor_underscore_test_path_is_not_edit() -> None:
    """`tests/` prefix; both leading and embedded."""
    args = {
        "command": "str_replace",
        "path": "src/__tests__/component.test.ts",
        "old_str": "x",
        "new_str": "y",
    }
    assert is_source_edit("str_replace_editor", args) is False


def test_str_replace_editor_markdown_file_is_not_edit() -> None:
    """Non-source extension (.md) — no graph node, no L3 fire."""
    args = {
        "command": "create",
        "path": "/workspace/repo/README.md",
        "file_text": "# new",
    }
    assert is_source_edit("str_replace_editor", args) is False


def test_str_replace_editor_no_path_is_not_edit() -> None:
    """Editor invocation with no path arg."""
    args = {"command": "str_replace", "old_str": "x", "new_str": "y"}
    assert is_source_edit("str_replace_editor", args) is False
    assert extract_edited_path("str_replace_editor", args) is None


def test_unknown_tool_name_is_not_edit() -> None:
    """Tools we don't know about (browse, search, etc.) default to False."""
    args = {"path": "src/foo.py", "command": "str_replace"}
    assert is_source_edit("browse", args) is False
    assert is_source_edit("find_file", args) is False
    assert is_source_edit("search_dir", args) is False


def test_empty_tool_name_is_not_edit() -> None:
    """Defensive: empty/None tool_name."""
    assert is_source_edit("", {"path": "x.py", "command": "str_replace"}) is False


def test_bash_python_command_no_redirect_is_not_edit() -> None:
    """`python script.py` — runs a script, doesn't edit it."""
    args = {"command": "python /workspace/repo/src/script.py"}
    assert is_source_edit("bash", args) is False


# ---------------------------------------------------------------------------
# Path-extraction edge cases
# ---------------------------------------------------------------------------

def test_extract_edited_path_uses_file_path_alias() -> None:
    """OH legacy used `file_path`; should still resolve."""
    args = {"file_path": "src/bar.py", "command": "str_replace"}
    assert extract_edited_path("FileEditAction", args) == "src/bar.py"


def test_str_replace_editor_no_command_falls_back_to_mutating() -> None:
    """If args has no `command` key (e.g. FileWriteAction direct write),
    treat as mutating per the OH wrapper convention."""
    args = {"path": "src/baz.py"}
    assert is_source_edit("FileWriteAction", args) is True
