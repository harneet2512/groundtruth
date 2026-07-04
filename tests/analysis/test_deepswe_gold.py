"""Pins for the host-only TRUE-gold resolver (``scripts/analysis/deepswe_gold.py``).

Hermetic: builds a synthetic Harbor task dir in ``tmp_path`` so the test does not
depend on the local ``deepswe-bench`` checkout being present. Guards the two
things that make gold trustworthy: (1) gold comes from ``solution.patch``, never
``test.patch``; (2) a new-file gold path (``--- /dev/null``) survives while a
deletion target (``+++ /dev/null``) is dropped.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

_MOD = Path(__file__).resolve().parents[2] / "scripts" / "analysis" / "deepswe_gold.py"
_spec = importlib.util.spec_from_file_location("deepswe_gold", _MOD)
assert _spec and _spec.loader
g = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(g)


_SOLUTION = """\
diff --git a/pkg/core.py b/pkg/core.py
--- a/pkg/core.py
+++ b/pkg/core.py
@@ -1 +1 @@
-old
+new
diff --git a/pkg/newmod.py b/pkg/newmod.py
--- /dev/null
+++ b/pkg/newmod.py
@@ -0,0 +1 @@
+brand new file
diff --git a/pkg/removed.py b/pkg/removed.py
--- a/pkg/removed.py
+++ /dev/null
@@ -1 +0,0 @@
-gone
"""

_TESTPATCH = """\
diff --git a/tests/test_core.py b/tests/test_core.py
--- /dev/null
+++ b/tests/test_core.py
@@ -0,0 +1 @@
+assert True
"""

_TASK_TOML = """\
version = "1.0"
[metadata]
task_id = "synth-task"
language = "python"
base_commit_hash = "deadbeefcafe"
repository_url = "https://example.invalid/synth"
"""


def _build_bench(tmp_path: Path) -> Path:
    root = tmp_path / "tasks"
    d = root / "synth-task"
    (d / "solution").mkdir(parents=True)
    (d / "tests").mkdir(parents=True)
    (d / "solution" / "solution.patch").write_text(_SOLUTION, encoding="utf-8")
    (d / "tests" / "test.patch").write_text(_TESTPATCH, encoding="utf-8")
    (d / "task.toml").write_text(_TASK_TOML, encoding="utf-8")
    return root


def test_gold_files_new_file_kept_devnull_dropped(tmp_path: Path) -> None:
    root = _build_bench(tmp_path)
    gold = g.gold_files_for_task("synth-task", root)
    # core.py edited + newmod.py added (the stratum-D new-file gold) both kept;
    # the /dev/null deletion target is dropped.
    assert gold == ["pkg/core.py", "pkg/newmod.py"]
    assert "/dev/null" not in gold


def test_test_patch_is_separate_from_gold(tmp_path: Path) -> None:
    root = _build_bench(tmp_path)
    gold = set(g.gold_files_for_task("synth-task", root))
    tests = set(g.test_patch_files_for_task("synth-task", root))
    assert tests == {"tests/test_core.py"}
    assert gold.isdisjoint(tests)  # verifier files never enter gold


def test_meta_and_slug(tmp_path: Path) -> None:
    root = _build_bench(tmp_path)
    m = g.task_meta("deepswe-full-synth-task", root)  # tape prefix stripped
    assert m["language"] == "python"
    assert m["base_commit"] == "deadbeefcafe"
    assert g.task_id_from_tape_dir("deepswe-full-synth-task") == "synth-task"


def test_unknown_task_is_quiet(tmp_path: Path) -> None:
    root = _build_bench(tmp_path)
    assert g.gold_files_for_task("nope", root) == []
    assert g.gold_patch_for_task("nope", root) is None
    assert g.task_meta("nope", root) == {}
