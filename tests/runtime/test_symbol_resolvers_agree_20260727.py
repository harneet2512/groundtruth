"""The two symbol resolvers must apply the SAME definition predicate.

FOUND BY ADVERSARIAL REVIEW of the commit that introduced them. GT's dominant defect class is
"two formulas that must agree by hand" — the runtime_evidence digests, PHASE_POLICY vs producer
names, the CanonicalEvent.from_json field list. The symbol-focus commit introduced a THIRD
instance, and the two copies ALREADY disagreed on the day they were written:

    gateway.defined_symbols_for_file   ... AND COALESCE(start_line,0)>0 ...   (view path)
    gt_mini_patch._resolved_search_symbols  (no start_line predicate)         (search path)

Consequence: a graph node with NULL/0 `start_line` VALIDATES a search operand into
`focused_symbols`, but that same symbol can NEVER be produced by the view path. Focus then
contains a name the definitional resolver considers unusable — and focus feeds the relevance
intersection, so it admits evidence on a symbol GT itself declines to resolve.

Both must gate on: non-test, a definition label, and a real start_line.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from artifact_deepswe import gt_mini_patch as seam  # noqa: E402
from groundtruth.runtime.gateway import defined_symbols_for_file  # noqa: E402
from groundtruth.runtime.reasoning_runtime import ActionOperation  # noqa: E402


FILE = "src/pkg/mod.py"


@pytest.fixture()
def graph(tmp_path, monkeypatch):
    """`good` is a real definition; `no_line` has start_line 0 — unusable to the view path."""
    db = tmp_path / "graph.db"
    con = sqlite3.connect(db)
    con.execute(
        "CREATE TABLE nodes (id INTEGER PRIMARY KEY, name TEXT, file_path TEXT, "
        "start_line INTEGER, label TEXT, is_test INTEGER)"
    )
    con.executemany(
        "INSERT INTO nodes VALUES (?,?,?,?,?,?)",
        [
            (1, "good_symbol", FILE, 10, "Function", 0),
            (2, "no_line_symbol", FILE, 0, "Function", 0),
        ],
    )
    con.commit()
    con.close()
    monkeypatch.setattr(seam, "_db_path", lambda: str(db), raising=False)
    monkeypatch.setattr(seam, "_root", lambda: str(tmp_path), raising=False)
    return str(db)


def test_positive_control_a_real_definition_resolves_on_BOTH_paths(graph):
    """Without this, the disagreement test below could pass because both paths are dead."""
    assert "good_symbol" in defined_symbols_for_file(graph, FILE)
    assert seam._resolved_search_symbols(
        ActionOperation.SEARCH, "grep -rn good_symbol ."
    ) == (f"{FILE}::good_symbol",)


def test_the_view_path_rejects_a_definition_with_no_start_line(graph):
    """PREMISE. This is the asymmetry that made the two resolvers disagree."""
    assert "no_line_symbol" not in defined_symbols_for_file(graph, FILE)


def test_the_search_path_rejects_it_TOO(graph):
    """THE FIX. A symbol the definitional resolver will not produce must not enter focus by the
    search route either — focus feeds the relevance intersection, so it would admit evidence
    about a symbol GT itself declines to resolve."""
    assert seam._resolved_search_symbols(
        ActionOperation.SEARCH, "grep -rn no_line_symbol ."
    ) == (), "the search path still validates a symbol the view path considers unusable"


def test_the_two_resolvers_agree_on_every_symbol_in_the_graph(graph):
    """THE INVARIANT, stated generally so a future divergence on ANY predicate fails here
    rather than surfacing as unexplained focus contents in a live run."""
    view_side = set(defined_symbols_for_file(graph, FILE))
    for name in ("good_symbol", "no_line_symbol"):
        search_side = bool(
            seam._resolved_search_symbols(ActionOperation.SEARCH, f"grep -rn {name} .")
        )
        assert search_side == (name in view_side), (
            f"resolvers disagree on {name!r}: view={name in view_side} search={search_side}"
        )
