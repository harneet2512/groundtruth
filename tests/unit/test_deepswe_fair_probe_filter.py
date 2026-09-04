"""Tests for the DeepSWE self-localization (fair-probe) pre-filter (CLUSTER E / E3).

These lock the held-out invariants:
  - a task whose instruction.md pre-names the gold public surface (files + public
    symbols) scores HIGH and flags self-localizing;
  - a task whose instruction describes behavior but never names the gold scores LOW
    and stays a fair probe;
  - private/internal helpers do NOT inflate coverage (the fastapi dilution bug);
  - the scorer is language-agnostic (go/python/rust/ts synthetic shapes).

They use SYNTHETIC fixtures (no benchmark coupling) plus, when present, the real
fastapi-implicit-head-options task as a held-out integration check.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = REPO_ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import deepswe_fair_probe_filter as F  # noqa: E402


def _make_task(tmp_path: Path, *, instruction: str, patch: str, language: str) -> str:
    td = tmp_path / "task"
    (td / "solution").mkdir(parents=True)
    (td / "instruction.md").write_text(instruction, encoding="utf-8")
    (td / "solution" / "solution.patch").write_text(patch, encoding="utf-8")
    (td / "task.toml").write_text(f'language = "{language}"\n', encoding="utf-8")
    return str(td)


# --------------------------------------------------------------------------------------
# Symbol extraction is language-aware
# --------------------------------------------------------------------------------------
def test_extract_python_def_and_class():
    patch = (
        "diff --git a/a.py b/a.py\n+++ b/a.py\n"
        "+class WidgetTracker:\n+    pass\n"
        "+def compute_total(x):\n+    return x\n"
        "+def _private_helper():\n+    pass\n"
    )
    syms = F._extract_added_symbols(patch, "python")
    assert "WidgetTracker" in syms
    assert "compute_total" in syms
    assert "_private_helper" in syms  # extracted, but excluded from PUBLIC surface below


def test_extract_go_func_and_type():
    patch = (
        "diff --git a/a.go b/a.go\n+++ b/a.go\n"
        "+func ResolveModulePath(base string) string {\n"
        "+type EvaluationState struct {\n"
    )
    syms = F._extract_added_symbols(patch, "go")
    assert "ResolveModulePath" in syms
    assert "EvaluationState" in syms


def test_extract_rust_fn_struct_trait():
    patch = (
        "diff --git a/a.rs b/a.rs\n+++ b/a.rs\n"
        "+pub fn evaluate_handle() -> Self {}\n"
        "+pub struct EvaluationHandle {}\n"
        "+trait Finalize {}\n"
    )
    syms = F._extract_added_symbols(patch, "rust")
    assert {"evaluate_handle", "EvaluationHandle", "Finalize"} <= syms


def test_extract_ts_export_const_and_class():
    patch = (
        "diff --git a/a.ts b/a.ts\n+++ b/a.ts\n"
        "+export const parseArrayJsonSchema = (x) => x\n"
        "+export class JsonSchemaParser {}\n"
        "+export type NestedParser = (j) => Type\n"
    )
    syms = F._extract_added_symbols(patch, "typescript")
    assert {"parseArrayJsonSchema", "JsonSchemaParser", "NestedParser"} <= syms


# --------------------------------------------------------------------------------------
# Public-surface filter — the fastapi dilution fix
# --------------------------------------------------------------------------------------
def test_private_symbols_excluded_from_public_surface():
    assert F._is_public_symbol("get_stats") is True
    assert F._is_public_symbol("ImplicitMethodTrackingMiddleware") is True
    assert F._is_public_symbol("_resolve_auto_head") is False
    assert F._is_public_symbol("__call__") is False


def test_private_helpers_do_not_dilute_coverage(tmp_path):
    """A spec that names the PUBLIC class but none of the 5 private helpers is still
    self-localizing — private helpers must not drag coverage below threshold."""
    patch = (
        "diff --git a/m.py b/m.py\n+++ b/m.py\n"
        "+class PublicWidget:\n+    pass\n"
        "+def _h1():\n    pass\n+def _h2():\n    pass\n"
        "+def _h3():\n    pass\n+def _h4():\n    pass\n+def _h5():\n    pass\n"
    )
    instr = "Define the `PublicWidget` in `m.py`. Internal helpers are implementation details."
    td = _make_task(tmp_path, instruction=instr, patch=patch, language="python")
    rec = F.score_task(td)
    # public surface = {m.py file, PublicWidget} both named -> coverage 1.0
    assert rec["gold_name_coverage"] == 1.0
    # all-symbol coverage is much lower (1 file + 1 class named of 7 names)
    assert rec["gold_name_coverage_all_symbols"] < 0.5


# --------------------------------------------------------------------------------------
# High-coverage self-localizing vs low-coverage fair-probe (the core discriminator)
# --------------------------------------------------------------------------------------
def test_self_localizing_when_gold_named(tmp_path):
    patch = (
        "diff --git a/routing.py b/routing.py\n+++ b/routing.py\n"
        "+def build_router(cfg):\n    pass\n"
        "diff --git a/methods.py b/methods.py\n+++ b/methods.py\n"
        "+class MethodTracker:\n    pass\n"
    )
    instr = "Add `build_router` to `routing.py` and define `MethodTracker` in `methods.py`."
    td = _make_task(tmp_path, instruction=instr, patch=patch, language="python")
    rec = F.score_task(td)
    rec["self_localizing"] = rec["gold_name_coverage"] >= 0.70
    assert rec["gold_name_coverage"] == 1.0
    assert rec["self_localizing"] is True


def test_fair_probe_when_behavior_only(tmp_path):
    patch = (
        "diff --git a/cache.go b/cache.go\n+++ b/cache.go\n"
        "+func resolveCandidate(p string) string {\n"
        "+func ParseSearchPaths(env string) []string {\n"
    )
    instr = (
        "Equivalent paths that point to the same module file should reuse a single "
        "cache entry. Candidate lookup order is base directory first. The exact helper "
        "layout is an implementation detail."
    )
    td = _make_task(tmp_path, instruction=instr, patch=patch, language="go")
    rec = F.score_task(td)
    rec["self_localizing"] = rec["gold_name_coverage"] >= 0.70
    # no gold file basename or public symbol appears in the behavior-only instruction
    assert rec["gold_name_coverage"] < 0.30
    assert rec["self_localizing"] is False


def test_token_boundary_no_false_substring(tmp_path):
    """`get` must NOT count as naming `get_stats` (bounded-token match)."""
    patch = "diff --git a/x.py b/x.py\n+++ b/x.py\n+def get_stats():\n    pass\n"
    instr = "The endpoint should get the data and return it."  # 'get' but not 'get_stats'
    td = _make_task(tmp_path, instruction=instr, patch=patch, language="python")
    rec = F.score_task(td)
    assert "get_stats" not in rec["gold_symbols_named"]


# --------------------------------------------------------------------------------------
# Held-out integration: the real fastapi task MUST flag self-localizing
# --------------------------------------------------------------------------------------
_FASTAPI = Path("D:/Groundtruth/deepswe-bench/tasks/fastapi-implicit-head-options")


@pytest.mark.skipif(not _FASTAPI.is_dir(), reason="deepswe-bench fastapi task not present")
def test_fastapi_implicit_head_options_is_self_localizing():
    rec = F.score_task(str(_FASTAPI))
    assert rec is not None
    # 3/4 gold files named in instruction.md
    assert rec["file_coverage"] == pytest.approx(0.75)
    # public-surface coverage clears the 0.70 self-localizing threshold
    assert rec["gold_name_coverage"] >= 0.70
    # the named public symbols include the spec-named middleware + stats methods
    assert "ImplicitMethodTrackingMiddleware" in rec["gold_symbols_named"]
    assert "get_stats" in rec["gold_symbols_named"]
    assert "reset_stats" in rec["gold_symbols_named"]


@pytest.mark.skipif(not _FASTAPI.parent.is_dir(), reason="deepswe-bench tasks dir not present")
def test_low_coverage_fair_probes_exist():
    """At least a few genuinely behavior-described (fair-probe) tasks must surface
    with ~0 coverage — otherwise the filter has nothing fair to recommend."""
    tasks_dir = _FASTAPI.parent
    zero = 0
    for name in os.listdir(tasks_dir):
        td = tasks_dir / name
        if not td.is_dir():
            continue
        rec = F.score_task(str(td))
        if rec is not None and rec["gold_name_coverage"] == 0.0:
            zero += 1
    assert zero >= 5  # several fully-behavioral fair probes exist
