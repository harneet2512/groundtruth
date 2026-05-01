"""RED tests for kernel.validate_against_graph.

Pin layers:
    1. Happy -- signature change with mock graph showing 4 callers.
    2. Boundary -- empty diff, diff with no callers, single-symbol-change.
    3. Adversarial -- diff with circular calls, diff that adds a symbol with no callers,
       graph with stale ``graph_db_sha``.
    4. Mutation -- documented per test.

Tests do not open ``graph.db``. ``MockGraphHandle`` is injected from conftest.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from groundtruth.control import kernel
from groundtruth.control.types import Diff


# Phase 1 implementation landed -- tests are now expected-pass.


# Layer 1: Happy -- canonical fixture, signature break with 4 callers.
# Mutation pin: caller-detection that ignores the qualified-name parameter list.
def test_signature_break_flags_orphans(fixture_loader, mock_graph_factory):
    input_data, expected = fixture_loader("graph_validation_breaks_signature")
    diff = Diff.model_validate(input_data["function_input"])
    graph = mock_graph_factory(input_data["graph_handle"])
    result = kernel.validate_against_graph(diff, graph)
    assert result.ok is False
    assert any("User.has_perm" in s for s in result.broken_signatures)
    assert len(result.orphaned_callers) >= expected["expected_return"]["orphaned_callers_min"]


# Layer 2: Boundary -- empty diff is a no-op, returns ok=True.
# Mutation pin: any branch that treats empty diff as failure.
def test_empty_diff_passes(mock_graph_factory):
    diff = Diff(diff_text="", files_changed=[])
    graph = mock_graph_factory({"graph_db_sha": "x"})
    result = kernel.validate_against_graph(diff, graph)
    assert result.ok is True
    assert result.broken_signatures == []
    assert result.orphaned_callers == []


# Layer 2: Boundary -- diff touches a symbol with zero callers in the graph.
# Mutation pin: orphan detection that reports symbol-itself instead of callers.
def test_no_callers_no_orphans(mock_graph_factory):
    diff = Diff(
        diff_text="diff --git a/src/a.py b/src/a.py\n--- a/src/a.py\n+++ b/src/a.py\n@@\n-def foo(x):\n+def foo(x, y):\n",
        files_changed=[Path("src/a.py")],
    )
    graph = mock_graph_factory({
        "graph_db_sha": "x",
        "callers_of": {"src.a.foo": []},
    })
    result = kernel.validate_against_graph(diff, graph)
    # Signature changed but no callers -- broken_signatures may still fire
    # but orphaned_callers must be empty.
    assert result.orphaned_callers == []


# Layer 3: Adversarial -- circular calls (A calls B, B calls A) must not infinite-loop.
# Mutation pin: missing cycle break in BFS.
def test_circular_calls_terminates(mock_graph_factory):
    diff = Diff(
        diff_text="diff --git a/src/a.py b/src/a.py\n--- a/src/a.py\n+++ b/src/a.py\n@@\n-def a():\n+def a(x):\n",
        files_changed=[Path("src/a.py")],
    )
    graph = mock_graph_factory({
        "graph_db_sha": "x",
        "callers_of": {
            "src.a.a": [{"qualified_name": "src.b.b", "file_path": "src/b.py", "line": 1}],
            "src.b.b": [{"qualified_name": "src.a.a", "file_path": "src/a.py", "line": 1}],
        },
    })
    # Must complete in bounded time.
    result = kernel.validate_against_graph(diff, graph)
    assert result is not None


# Layer 3: Adversarial -- diff adds a brand-new symbol; not a break.
# Mutation pin: false-positive on adds.
def test_pure_add_does_not_break(mock_graph_factory):
    diff = Diff(
        diff_text="diff --git a/src/a.py b/src/a.py\n--- a/src/a.py\n+++ b/src/a.py\n@@\n+def new_helper():\n+    return 1\n",
        files_changed=[Path("src/a.py")],
    )
    graph = mock_graph_factory({"graph_db_sha": "x"})
    result = kernel.validate_against_graph(diff, graph)
    assert result.ok is True


# Layer 3: Adversarial -- evidence must include node_ids when broken_signatures
# is non-empty so the proof layer can attribute the failure.
# Mutation pin: returning empty Evidence on failure.
def test_evidence_populated_on_break(fixture_loader, mock_graph_factory):
    input_data, _ = fixture_loader("graph_validation_breaks_signature")
    diff = Diff.model_validate(input_data["function_input"])
    graph = mock_graph_factory(input_data["graph_handle"])
    result = kernel.validate_against_graph(diff, graph)
    assert len(result.evidence.node_ids) >= 1


# Layer 2: Boundary -- diff with binary-only changes (no symbols) returns ok.
# Mutation pin: signature-extraction crashing on non-source diffs.
def test_binary_only_diff_handled(mock_graph_factory):
    diff = Diff(
        diff_text="Binary files a/img.png and b/img.png differ\n",
        files_changed=[Path("img.png")],
    )
    graph = mock_graph_factory({"graph_db_sha": "x"})
    result = kernel.validate_against_graph(diff, graph)
    assert result.ok is True
