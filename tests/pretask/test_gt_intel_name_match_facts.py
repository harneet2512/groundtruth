"""Behavior tests for gt_intel L1 (IMPORT family) + L2 (directive briefing) name_match
suppression — deeper-LIPI A-batch fixes.

Builds a synthetic graph.db matching the real schema (nodes/edges) and asserts:

  L1 (IMPORT family):
    - get_callees() returns ONLY deterministic-resolution callees (import/same_file/...),
      NEVER a name_match callee — a guessed import path is maximally harmful.
    - the deterministic callee carries its resolution_method so the calibration marker
      can fire on the emitted IMPORT line.

  L2 (directive briefing — generate_pretask_briefing):
    - a name_match-only incoming caller does NOT surface as a "CALLERS:" fact;
    - a deterministic incoming caller DOES surface;
    - FIX-HERE disambiguates by the resolved target node id (the same-named node with a
      real deterministic incoming edge wins) instead of an arbitrary LOWER(name) LIMIT 2.

Red-before-green: with name_match admitted into the fact set (the pre-fix behavior),
the L1 assertion (no name_match callee) and the L2 caller assertion both fail.
"""
from __future__ import annotations

import importlib.util
import os
import sqlite3
import sys

import pytest

# gt_intel.py is a standalone benchmark script (not a package module). Load it by path.
_GT_INTEL_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "benchmarks",
    "swebench",
    "gt_intel.py",
)
_MODNAME = "gt_intel_under_test"
_spec = importlib.util.spec_from_file_location(_MODNAME, _GT_INTEL_PATH)
assert _spec and _spec.loader
gt = importlib.util.module_from_spec(_spec)
# Register before exec so @dataclass can resolve cls.__module__ via sys.modules.
sys.modules[_MODNAME] = gt
_spec.loader.exec_module(gt)


# Full nodes schema (13 columns, matching the real graph.db so _row_to_node maps cleanly).
_SCHEMA = """
CREATE TABLE nodes (
    id INTEGER PRIMARY KEY, label TEXT, name TEXT, qualified_name TEXT,
    file_path TEXT, start_line INTEGER, end_line INTEGER, signature TEXT,
    return_type TEXT, is_exported INTEGER, is_test INTEGER, language TEXT,
    parent_id INTEGER
);
CREATE TABLE edges (
    id INTEGER PRIMARY KEY, source_id INTEGER, target_id INTEGER, type TEXT,
    source_line INTEGER, source_file TEXT, resolution_method TEXT,
    confidence REAL, metadata TEXT
);
"""


def _node(nid, name, fpath, sline=10, qname=None, sig="", is_test=0, label="Function"):
    return (
        nid, label, name, qname or name, fpath, sline, sline + 5, sig, "",
        0, is_test, "python", 0,
    )


@pytest.fixture(autouse=True)
def _reset_active_resolutions():
    """Ensure the active set is the immutable default each test (gate-narrowing from a
    prior DB never leaks). Default ∩ deterministic = {import, same_file}."""
    gt._active_resolutions = gt.VERIFIED_RESOLUTIONS
    yield
    gt._active_resolutions = gt.VERIFIED_RESOLUTIONS


def _conn(rows_nodes, rows_edges):
    conn = sqlite3.connect(":memory:")
    conn.executescript(_SCHEMA)
    conn.executemany(
        "INSERT INTO nodes (id,label,name,qualified_name,file_path,start_line,end_line,"
        "signature,return_type,is_exported,is_test,language,parent_id) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        rows_nodes,
    )
    conn.executemany(
        "INSERT INTO edges (source_id,target_id,type,source_line,source_file,"
        "resolution_method,confidence,metadata) VALUES (?,?,?,?,?,?,?,?)",
        rows_edges,
    )
    conn.commit()
    return conn


# ── L1: IMPORT family / get_callees ─────────────────────────────────────────

def test_l1_get_callees_excludes_name_match_keeps_deterministic():
    """get_callees: target -> det callee (import) kept; target -> name_match callee dropped.

    RED before fix: the old query admitted name_match, so the name_match callee would be
    returned and the IMPORT family would emit a guessed `from core import walk`.
    """
    nodes = [
        _node(1, "validate", "app.py", sig="def validate(data):"),
        _node(2, "_check", "util.py", sig="def _check(x):"),   # deterministic callee
        _node(3, "walk", "core.py", sig="def walk(root):"),    # name_match callee (drop)
    ]
    edges = [
        (1, 2, "CALLS", 11, "app.py", "import", 1.0, None),        # det -> kept
        (1, 3, "CALLS", 12, "app.py", "name_match", 0.9, None),    # guess -> dropped
    ]
    conn = _conn(nodes, edges)

    callees = gt.get_callees(conn, 1)
    names = {c.name for c, _rm in callees}
    methods = {rm for _c, rm in callees}

    assert "_check" in names, "deterministic (import) callee must survive"
    assert "walk" not in names, "name_match callee must NEVER surface (guessed import)"
    assert methods == {"import"}, f"only deterministic methods expected, got {methods}"


def test_l1_import_family_threads_resolution_method_and_suppresses_name_match():
    """compute_evidence IMPORT family: emits the import line for the deterministic callee
    only, and the EvidenceNode carries resolution_method so the [VERIFIED: import] marker
    can fire. A name_match callee produces NO import line."""
    nodes = [
        _node(1, "validate", "app.py", sig="def validate(data):"),
        _node(2, "_check", "util.py", sig="def _check(x):"),
        _node(3, "walk", "core.py", sig="def walk(root):"),
    ]
    edges = [
        (1, 2, "CALLS", 11, "app.py", "import", 1.0, None),
        (1, 3, "CALLS", 12, "app.py", "name_match", 0.9, None),
    ]
    conn = _conn(nodes, edges)
    target = gt.get_target_node(conn, "app.py", "validate")
    assert target is not None

    # Restrict to the IMPORT family to isolate L1.
    os.environ["GT_EVIDENCE_FAMILIES"] = "IMPORT"
    try:
        ev = gt.compute_evidence(conn, root="/nonexistent", target=target)
    finally:
        del os.environ["GT_EVIDENCE_FAMILIES"]

    imports = [e for e in ev if e.family == "IMPORT"]
    names = {e.name for e in imports}
    assert "_check" in names, "deterministic callee import must be emitted"
    assert "walk" not in names, "name_match callee import (a guess) must be suppressed"

    check_node = next(e for e in imports if e.name == "_check")
    assert check_node.resolution_method == "import", (
        "resolution_method must be threaded so the calibration marker can fire"
    )
    # The marker helper must now render a deterministic tag (not blank, not [POSSIBLE]).
    assert gt._resolution_suffix(check_node) == " [VERIFIED: import]"


# ── L2: directive briefing (generate_pretask_briefing) ──────────────────────

def test_l2_top_caller_suppresses_name_match_phantom():
    """A name_match-only incoming caller is a phantom (same-name guess) and must NOT be
    rendered as a 'CALLERS:' fact. A deterministic caller is rendered.

    RED before fix: the top-caller query used the full active set (incl. name_match) with
    no confidence floor, so a phantom caller surfaced as a fact.
    """
    # phantom case: target `parse` has ONLY a name_match incoming caller.
    nodes_phantom = [
        _node(1, "parse", "p.py", sig="def parse(s):"),
        _node(9, "phantom_caller", "x.py"),
    ]
    edges_phantom = [
        (9, 1, "CALLS", 3, "x.py", "name_match", 0.9, None),
    ]
    conn = _conn(nodes_phantom, edges_phantom)
    out = gt.generate_pretask_briefing(conn, root="/nonexistent", identifiers=["parse"])
    assert "FIX HERE" in out  # the node still localizes
    assert "CALLERS:" not in out, "name_match caller must not surface as a fact"

    # deterministic case: same shape but the caller edge is `import`.
    edges_det = [
        (9, 1, "CALLS", 3, "x.py", "import", 1.0, None),
    ]
    conn2 = _conn(nodes_phantom, edges_det)
    out2 = gt.generate_pretask_briefing(conn2, root="/nonexistent", identifiers=["parse"])
    assert "CALLERS: phantom_caller" in out2, "deterministic caller must surface"


def test_l2_fix_here_disambiguates_by_resolved_target_id():
    """Two same-named nodes: the one with a real deterministic incoming edge (the resolved
    target) must be the FIX-HERE the briefing points at — not an arbitrary name match.

    RED before fix: LOWER(name)=? LIMIT 2 (ordered by rowid) would emit the wrong node
    (id=1, the shadow) first.
    """
    nodes = [
        # id=1 is a same-named shadow with NO real incoming edges (sorts first by rowid).
        _node(1, "handle", "shadow/dup.py", sline=5, qname="shadow.handle"),
        # id=2 is the genuinely-referenced target (has a deterministic incoming caller).
        _node(2, "handle", "core/real.py", sline=40, qname="core.handle"),
        _node(7, "router", "web.py"),
    ]
    edges = [
        (7, 2, "CALLS", 8, "web.py", "import", 1.0, None),  # real -> id=2 resolved
    ]
    conn = _conn(nodes, edges)
    out = gt.generate_pretask_briefing(conn, root="/nonexistent", identifiers=["handle"])
    # The resolved target (core/real.py:40) must appear; ranking puts det-target first.
    assert "core.handle()" in out, "resolved target must be surfaced as FIX HERE"
    # And its real caller surfaces as a deterministic fact.
    assert "CALLERS: router" in out
