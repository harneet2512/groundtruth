"""Patch-derived edit-truth authority for the localization edit metrics.

Confirmed measurement bug (frozen mini-swe baseline): the command-inference edit
detector (`_extract_edited_file`) misses edits applied via heredoc / `git apply` /
multi-file patches and attributes at most ONE file per action, so on 71/75 tasks it
reported ``n_edited=0`` while the agent had SUBMITTED a real source patch. That nulled
``false_file_rate`` / ``localization_precision`` and starved ``localization_recall`` /
``steps_to_gold_edit``.

Fix: the SUBMITTED PATCH is the strongest edit-truth authority (task_truth doctrine).
``extract_edit_targets`` parses every edited file from a unified diff; the localization
edit metrics use that set as the AUTHORITY when a submission diff is present and fall
back to command inference only when it is absent. A predeclared, ARM-SYMMETRIC
test/junk filter drops non-source noise from ``n_edited`` (gold is source-only).

RED-first: every metric-level test here fails against pristine HEAD (no
``extract_edit_targets``; ``_compute_localization`` never sees the submission, so
``n_edited=0`` -> precision/false_file_rate are null).

Loaded by path — scripts/ is not a package.
"""
from __future__ import annotations

import importlib.util
import json
import os
import tempfile
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_PM = _ROOT / "scripts" / "swebench" / "gt_performance_metrics.py"
_spec = importlib.util.spec_from_file_location("gt_performance_metrics", _PM)
assert _spec and _spec.loader
pm = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pm)


# --------------------------------------------------------------------------- helpers
def _bash(cmd: str) -> dict:
    """A mini-swe-agent assistant turn: a single bash tool call (the real shape)."""
    return {
        "role": "assistant",
        "content": "step",
        "tool_calls": [{"function": {"name": "bash",
                                     "arguments": json.dumps({"command": cmd})}}],
    }


def _run(traj: dict, gold: list[str]) -> dict:
    with tempfile.TemporaryDirectory() as tmp:
        tp = os.path.join(tmp, "mini-swe-agent.trajectory.json")
        with open(tp, "w", encoding="utf-8") as f:
            json.dump(traj, f)
        return pm.compute_performance_metrics(tp, tmp, gold_files=gold)


# =========================================================================== extractor
def test_extract_edit_targets_multi_file_diff_yields_all_files() -> None:
    diff = (
        "diff --git a/pkg/a.py b/pkg/a.py\n"
        "--- a/pkg/a.py\n+++ b/pkg/a.py\n@@ -1 +1 @@\n-x\n+y\n"
        "diff --git a/pkg/b.py b/pkg/b.py\n"
        "--- a/pkg/b.py\n+++ b/pkg/b.py\n@@ -1 +1 @@\n-x\n+y\n"
    )
    assert pm.extract_edit_targets(diff) == {"pkg/a.py", "pkg/b.py"}


def test_extract_edit_targets_plain_unified_diff_via_plus_header() -> None:
    """A plain (non-git) unified diff carries only ``+++`` headers; still recovered."""
    diff = "--- pkg/c.py\n+++ pkg/c.py\n@@ -1 +1 @@\n-x\n+y\n"
    assert pm.extract_edit_targets(diff) == {"pkg/c.py"}


def test_extract_edit_targets_skips_dev_null_on_delete_and_create() -> None:
    created = (
        "diff --git a/pkg/new.py b/pkg/new.py\n"
        "new file mode 100644\n--- /dev/null\n+++ b/pkg/new.py\n@@ -0,0 +1 @@\n+z\n"
    )
    assert pm.extract_edit_targets(created) == {"pkg/new.py"}
    deleted = (
        "diff --git a/pkg/old.py b/pkg/old.py\n"
        "deleted file mode 100644\n--- a/pkg/old.py\n+++ /dev/null\n@@ -1 +0,0 @@\n-z\n"
    )
    # the delete's post-image is /dev/null (skipped); the file name still lands via
    # the `diff --git ... b/pkg/old.py` post-image name — /dev/null itself never leaks.
    got = pm.extract_edit_targets(deleted)
    assert "/dev/null" not in got
    assert got == {"pkg/old.py"}


def test_extract_edit_targets_empty_and_garbage_are_safe() -> None:
    assert pm.extract_edit_targets("") == set()
    assert pm.extract_edit_targets("no diff here at all") == set()


def test_extract_edited_file_still_misses_git_apply_heredoc() -> None:
    """The command-inference detector genuinely cannot attribute a git-apply heredoc
    (that is WHY the patch must be the authority)."""
    cmd = ("git apply <<'EOF'\n"
           "diff --git a/pkg/mod.py b/pkg/mod.py\n"
           "--- a/pkg/mod.py\n+++ b/pkg/mod.py\n@@ -1 +1 @@\n-old\n+new\nEOF")
    assert pm._extract_edited_file("", cmd) is None


# =========================================================================== junk filter
def test_junk_filter_predeclared_rule() -> None:
    # legit source with 'backup' in the stem is NOT junk
    assert pm._is_junk_edit_path("pkg/foo.backup.py") is False
    assert pm._is_junk_edit_path("pkg/mod.py") is False
    # editor / patch backups ARE junk
    assert pm._is_junk_edit_path("pkg/mod.py.orig") is True
    assert pm._is_junk_edit_path("pkg/mod.py.bak") is True
    assert pm._is_junk_edit_path("pkg/mod.py.rej") is True
    assert pm._is_junk_edit_path("pkg/.mod.py.swp") is True
    # data / log spew
    assert pm._is_junk_edit_path("out/data.csv") is True
    assert pm._is_junk_edit_path("run.log") is True
    # bare console sink + timestamped generated files
    assert pm._is_junk_edit_path("console") is True
    assert pm._is_junk_edit_path("reports/20260721-153000_summary.py") is True


# =========================================================================== metric wiring
def test_patch_authority_recovers_git_apply_heredoc_edit() -> None:
    """THE bug: agent edits gold via git-apply heredoc (command inference -> None),
    but the submission diff proves the edit. Patch authority makes the edit metrics
    correct instead of null/zero."""
    apply_cmd = ("git apply <<'EOF'\n"
                 "diff --git a/pkg/mod.py b/pkg/mod.py\n"
                 "--- a/pkg/mod.py\n+++ b/pkg/mod.py\n@@ -1 +1 @@\n-old\n+new\nEOF")
    traj = {
        "messages": [
            _bash("cat pkg/mod.py"),
            {"role": "tool", "content": "old"},
            _bash(apply_cmd),
            {"role": "tool", "content": "Applied."},
        ],
        "info": {"submission": (
            "diff --git a/pkg/mod.py b/pkg/mod.py\n"
            "--- a/pkg/mod.py\n+++ b/pkg/mod.py\n@@ -1 +1 @@\n-old\n+new\n")},
    }
    loc = _run(traj, ["pkg/mod.py"])["localization"]
    assert loc["_unique_edited"] == 1                 # was 0 (command inference blind)
    assert loc["_edit_authority"] == "submission_patch"
    assert loc["localization_precision"] == 1.0       # was null
    assert loc["localization_recall"] == 1.0
    assert loc["false_file_rate"] == 0.0              # was null


def test_patch_authority_false_file_rate_with_non_gold_edit() -> None:
    """Two source files in the patch, only one gold -> precision 0.5, false_file 0.5."""
    traj = {
        "messages": [_bash("git apply patch.diff"), {"role": "tool", "content": "ok"}],
        "info": {"submission": (
            "diff --git a/pkg/gold.py b/pkg/gold.py\n"
            "--- a/pkg/gold.py\n+++ b/pkg/gold.py\n@@ -1 +1 @@\n-a\n+b\n"
            "diff --git a/pkg/wrong.py b/pkg/wrong.py\n"
            "--- a/pkg/wrong.py\n+++ b/pkg/wrong.py\n@@ -1 +1 @@\n-a\n+b\n")},
    }
    loc = _run(traj, ["pkg/gold.py"])["localization"]
    assert loc["_unique_edited"] == 2
    assert loc["localization_precision"] == 0.5
    assert loc["false_file_rate"] == 0.5


def test_symmetric_test_and_junk_filter_excludes_from_n_edited() -> None:
    """Gold is source-only, so a test edit and a .orig backup in the SAME patch are
    dropped from n_edited symmetrically, and the excluded counts are reported."""
    traj = {
        "messages": [_bash("git apply patch.diff"), {"role": "tool", "content": "ok"}],
        "info": {"submission": (
            "diff --git a/src/mod.py b/src/mod.py\n"
            "--- a/src/mod.py\n+++ b/src/mod.py\n@@ -1 +1 @@\n-a\n+b\n"
            "diff --git a/tests/test_mod.py b/tests/test_mod.py\n"
            "--- a/tests/test_mod.py\n+++ b/tests/test_mod.py\n@@ -1 +1 @@\n-a\n+b\n"
            "diff --git a/src/mod.py.orig b/src/mod.py.orig\n"
            "--- a/src/mod.py.orig\n+++ b/src/mod.py.orig\n@@ -1 +1 @@\n-a\n+b\n")},
    }
    loc = _run(traj, ["src/mod.py"])["localization"]
    assert loc["_unique_edited"] == 1                 # only src/mod.py survives
    assert loc["_n_excluded_test_edits"] == 1         # tests/test_mod.py
    assert loc["_n_excluded_junk_edits"] == 1         # src/mod.py.orig
    assert loc["localization_precision"] == 1.0
    assert loc["false_file_rate"] == 0.0


def test_command_inference_fallback_when_no_submission() -> None:
    """No submission diff -> authority falls back to command inference (str_replace)."""
    traj = {
        "messages": [
            {"role": "assistant", "content": "edit", "tool_calls": [{"function": {
                "name": "editor", "arguments": json.dumps({
                    "command": "str_replace", "path": "pkg/mod.py",
                    "old_str": "a", "new_str": "b"})}}]},
            {"role": "tool", "content": "File updated."},
        ],
        "info": {"submission": ""},
    }
    loc = _run(traj, ["pkg/mod.py"])["localization"]
    assert loc["_edit_authority"] == "command_inference"
    assert loc["_unique_edited"] == 1
    assert loc["localization_precision"] == 1.0
