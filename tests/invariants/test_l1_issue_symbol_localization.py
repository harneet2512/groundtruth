"""Invariant tests for L1 localization — issue-symbol matching.

L1-INV-1: If issue text names a function in graph.db, that function's file
           MUST appear in the edit-target search space.
L1-INV-2: Edit target must prefer issue-relevant functions over high-caller hubs.
L1-INV-3: If no issue-relevant function exists, emit orientation, not edit target.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from conftest_l1 import (  # noqa: E402
    create_graph_db,
    extract_issue_symbol_files,
    score_edit_target_candidates,
)


# ---------------------------------------------------------------------------
# Fixtures — synthetic graph.dbs modeling real failure cases
# ---------------------------------------------------------------------------

@pytest.fixture
def pypsa_graph(tmp_path):
    """pypsa-1172: expanded_capacity in expressions.py, Network in networks.py."""
    db = tmp_path / "pypsa.db"
    nodes = [
        # High-degree hub (97 callers simulated with 5 for speed)
        ("Network", "Class", "pypsa/networks.py", "class Network:", 1, 500, True),
        ("add", "Method", "pypsa/networks.py", "def add(self, ...)", 50, 200, True),
        ("set_snapshots", "Method", "pypsa/networks.py", "def set_snapshots(self, ...)", 100, 150, True),
        # Actual target — low degree
        ("expanded_capacity", "Function", "pypsa/statistics/expressions.py", "def expanded_capacity(n, comps=None)", 10, 40, True),
        ("optimal_capacity", "Function", "pypsa/statistics/expressions.py", "def optimal_capacity(n, comps=None)", 42, 70, True),
        ("installed_capacity", "Function", "pypsa/statistics/expressions.py", "def installed_capacity(n, comps=None)", 75, 100, True),
        # Callers for Network to make it high-degree
        ("test_a", "Function", "test/test_components.py", "def test_a()", 1, 10, False),
        ("test_b", "Function", "test/test_bugs.py", "def test_b()", 1, 10, False),
        ("pf_func", "Function", "pypsa/pf.py", "def pf_func(n)", 1, 50, True),
        ("conftest_n", "Function", "test/conftest.py", "def conftest_n()", 1, 20, False),
        ("example_func", "Function", "pypsa/examples.py", "def example_func()", 1, 30, True),
    ]
    edges = [
        # 5 callers for Network
        ("test_a", "Network", "CALLS", 1.0),
        ("test_b", "Network", "CALLS", 1.0),
        ("pf_func", "Network", "CALLS", 1.0),
        ("conftest_n", "Network", "CALLS", 1.0),
        ("example_func", "Network", "CALLS", 1.0),
        # 1 caller for expanded_capacity
        ("test_a", "expanded_capacity", "CALLS", 0.9),
        # 1 caller for optimal_capacity
        ("test_b", "optimal_capacity", "CALLS", 0.9),
    ]
    create_graph_db(db, nodes, edges)
    return db


@pytest.fixture
def flexget_graph(tmp_path):
    """flexget-4306: add_entries in qbittorrent.py, Session in requests.py (246 cal)."""
    db = tmp_path / "flexget.db"
    nodes = [
        ("Session", "Class", "flexget/utils/requests.py", "class Session:", 1, 300, True),
        ("get", "Method", "flexget/utils/requests.py", "def get(self, url)", 50, 80, True),
        ("add_entries", "Method", "flexget/plugins/clients/qbittorrent.py", "def add_entries(self, task, config)", 200, 300, True),
        ("connect", "Method", "flexget/plugins/clients/qbittorrent.py", "def connect(self, config)", 100, 150, True),
        ("check_api_version", "Method", "flexget/plugins/clients/qbittorrent.py", "def check_api_version(self)", 150, 180, True),
        # Many callers for Session
        ("caller1", "Function", "flexget/task.py", "def caller1()", 1, 10, True),
        ("caller2", "Function", "flexget/api.py", "def caller2()", 1, 10, True),
        ("caller3", "Function", "flexget/manager.py", "def caller3()", 1, 10, True),
        ("caller4", "Function", "flexget/scheduler.py", "def caller4()", 1, 10, True),
        ("caller5", "Function", "flexget/ipc.py", "def caller5()", 1, 10, True),
    ]
    edges = [
        ("caller1", "Session", "CALLS", 1.0),
        ("caller2", "Session", "CALLS", 1.0),
        ("caller3", "Session", "CALLS", 1.0),
        ("caller4", "Session", "CALLS", 1.0),
        ("caller5", "Session", "CALLS", 1.0),
        ("connect", "add_entries", "CALLS", 1.0),
    ]
    create_graph_db(db, nodes, edges)
    return db


@pytest.fixture
def hub_vs_match_graph(tmp_path):
    """Generic: exact issue symbol with 2 callers vs hub with 500 callers."""
    db = tmp_path / "hub.db"
    nodes = [
        ("BigHub", "Class", "src/core/hub.py", "class BigHub:", 1, 500, True),
        ("my_func", "Function", "src/utils/helpers.py", "def my_func(x, y)", 10, 30, True),
        ("c1", "Function", "src/a.py", "def c1()", 1, 5, True),
        ("c2", "Function", "src/b.py", "def c2()", 1, 5, True),
        ("hc1", "Function", "src/h1.py", "def hc1()", 1, 5, True),
        ("hc2", "Function", "src/h2.py", "def hc2()", 1, 5, True),
        ("hc3", "Function", "src/h3.py", "def hc3()", 1, 5, True),
        ("hc4", "Function", "src/h4.py", "def hc4()", 1, 5, True),
        ("hc5", "Function", "src/h5.py", "def hc5()", 1, 5, True),
    ]
    edges = [
        ("c1", "my_func", "CALLS", 1.0),
        ("c2", "my_func", "CALLS", 1.0),
        ("hc1", "BigHub", "CALLS", 1.0),
        ("hc2", "BigHub", "CALLS", 1.0),
        ("hc3", "BigHub", "CALLS", 1.0),
        ("hc4", "BigHub", "CALLS", 1.0),
        ("hc5", "BigHub", "CALLS", 1.0),
    ]
    create_graph_db(db, nodes, edges)
    return db


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestIssueSymbolFileExtraction:
    """L1-INV-1: Issue-named symbols → their files must be in search space."""

    def test_pypsa_expanded_capacity_found(self, pypsa_graph):
        issue = "expanded_capacity(comps='Generator', groupby=False) returns empty"
        files = extract_issue_symbol_files(str(pypsa_graph), issue)
        assert "pypsa/statistics/expressions.py" in files

    def test_pypsa_optimal_capacity_found(self, pypsa_graph):
        issue = "optimal_capacity(comps='Generator') is missing from list"
        files = extract_issue_symbol_files(str(pypsa_graph), issue)
        assert "pypsa/statistics/expressions.py" in files

    def test_pypsa_network_not_primary(self, pypsa_graph):
        issue = "expanded_capacity(comps='Generator') returns empty since 0.32"
        files = extract_issue_symbol_files(str(pypsa_graph), issue)
        # expressions.py should appear before networks.py
        if "pypsa/networks.py" in files:
            assert files.index("pypsa/statistics/expressions.py") < files.index("pypsa/networks.py")

    def test_flexget_add_entries_when_named(self, flexget_graph):
        issue = "qbittorrent add_entries fails to set ratioLimit when adding torrent"
        files = extract_issue_symbol_files(str(flexget_graph), issue)
        assert any("qbittorrent" in f for f in files)

    def test_flexget_no_match_when_no_exact_name(self, flexget_graph):
        issue = "qbittorrent plugin fails to set ratioLimit when adding torrent"
        files = extract_issue_symbol_files(str(flexget_graph), issue)
        # No exact function name in issue → no symbol match (correct behavior)
        # File ranking falls back to v7.4 scorer in production
        assert "flexget/utils/requests.py" not in files


class TestEditTargetScoring:
    """L1-INV-2: Issue-relevant functions must outrank high-caller hubs."""

    def test_pypsa_expanded_capacity_beats_network(self, pypsa_graph):
        issue = "expanded_capacity(comps='Generator') returns empty"
        candidates = score_edit_target_candidates(str(pypsa_graph), issue)
        assert len(candidates) > 0
        best = candidates[0]
        assert best["func"] == "expanded_capacity"
        assert best["file"] == "pypsa/statistics/expressions.py"

    def test_hub_with_500_callers_loses_to_issue_match(self, hub_vs_match_graph):
        issue = "my_func(x, y) raises TypeError when y is None"
        candidates = score_edit_target_candidates(str(hub_vs_match_graph), issue)
        assert len(candidates) > 0
        best = candidates[0]
        assert best["func"] == "my_func"

    def test_no_issue_match_produces_no_authoritative_target(self, hub_vs_match_graph):
        issue = "something completely unrelated with no function names in graph"
        candidates = score_edit_target_candidates(str(hub_vs_match_graph), issue)
        # No candidate should have high issue-relevance
        for c in candidates:
            assert not c.get("direct"), f"Candidate {c['func']} marked direct without issue match"


class TestExactNameInIssue:
    """L1-INV-1 variant: exact function name in issue → file must appear."""

    def test_exact_name_match(self, pypsa_graph):
        issue = "When calling expanded_capacity the result is wrong"
        files = extract_issue_symbol_files(str(pypsa_graph), issue)
        assert "pypsa/statistics/expressions.py" in files

    def test_camel_case_match(self, hub_vs_match_graph):
        issue = "BigHub fails to initialize properly"
        files = extract_issue_symbol_files(str(hub_vs_match_graph), issue)
        assert "src/core/hub.py" in files
