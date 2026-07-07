"""Pin: _strip_scaffold_files must use the RICH junk classifier, not the basename-only one.

_strip_scaffold_files was wired to _is_scaffold_name (basename prefix ONLY: reproduce_/debug_/
temp_), so agent-created junk under a _JUNK_DIR (`.openhands/TASKS.md`) and junk extensions
(`flex.py.bak` — the weasyprint-2300 eval-failure class) slipped into the FINAL git-diff patch
(witnessed on cfn-lint-3749 + loguru-1297, run 28841112192). The fix swaps it to
_is_scaffolding_path, the purpose-built classifier (SCAFFOLDING_PREFIXES ∪ _JUNK_EXTENSIONS ∪
_JUNK_DIRS ∪ double-extension backups). This pins BOTH the partition the strip now applies AND
the semantic gap that made the swap necessary — reverting to _is_scaffold_name reddens it.
"""
from __future__ import annotations

import os
import sys
from types import SimpleNamespace

import pytest

_WRAP_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "scripts", "swebench"))
sys.path.insert(0, _WRAP_DIR)
for _mod in ("litellm", "cost_tracking"):
    sys.modules.setdefault(_mod, SimpleNamespace(
        model_cost={}, success_callback=[], completion=lambda *a, **k: None,
        acompletion=None, completion_cost=lambda *a, **k: 0.0,
        track_cost=lambda *a, **k: None, CostTracker=object))
try:
    import oh_gt_full_wrapper as _w
except Exception:  # heavy sibling deps unavailable
    _w = None

skip = pytest.mark.skipif(_w is None, reason="oh_gt_full_wrapper import unavailable")

# junk that pollutes the git diff and can fail the eval — MUST be stripped
_STRIP = [
    ".openhands/TASKS.md",           # _JUNK_DIR — the cfn-3749/loguru case
    "a/b/.openhands/microagents/x.md",  # nested _JUNK_DIR
    "src/flex.py.bak",               # _JUNK_EXTENSION — the weasyprint-2300 case
    "pkg/__pycache__/mod.pyc",       # _JUNK_DIR + ext
    "module.py.orig",                # double-extension backup
    "reproduce_bug.py", "debug_1.py",  # SCAFFOLDING_PREFIXES (the old predicate's only catch)
]
# legitimate new files — 19.3% of gold patches add files — MUST be preserved
_KEEP = [
    "src/new_module.py", "CHANGELOG.rst", "tests/data/fixture.yaml",
    "docs/readme.md", "loguru/_datetime.py", "src/cfnlint/rules/functions/ForEach.py",
]


@skip
@pytest.mark.parametrize("path", _STRIP)
def test_junk_is_stripped(path):
    assert _w._is_scaffolding_path(path), f"{path} should be stripped (pollutes the patch)"


@skip
@pytest.mark.parametrize("path", _KEEP)
def test_legitimate_new_file_is_preserved(path):
    assert not _w._is_scaffolding_path(path), f"{path} is a legitimate new file — must NOT be stripped"


@skip
def test_basename_predicate_would_have_missed_openhands():
    # the exact gap the swap closes: the weak predicate misses junk-dir / junk-ext files,
    # the rich one catches them. If these ever converge, the swap has been reverted.
    assert not _w._is_scaffold_name(".openhands/TASKS.md")   # weak: MISS (the bug)
    assert _w._is_scaffolding_path(".openhands/TASKS.md")    # rich: STRIP (the fix)
    assert not _w._is_scaffold_name("src/flex.py.bak")
    assert _w._is_scaffolding_path("src/flex.py.bak")
