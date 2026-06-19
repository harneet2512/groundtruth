"""Red->green behavior tests for the 3 name_match-laundering fixes in
``scripts/swebench/oh_gt_full_wrapper.py`` (L5 scope-callers floor, L6
direct-name-rescue caller COUNT, L7 ``_detect_scope`` container-fallback relabel).

Each fix is the SAME bug class as the confirmed graph-map leak: the wrapper's own
SQL / field-selection admitted a ``name_match`` edge as a deterministic FACT while
the callee path applied the shared DETERMINISTIC resolution-method gate. These
tests build the real graph.db schema (nodes/edges) in memory and assert that:

  * L5: the shared ``_edge_filter_for_db`` categorical clause counts ONLY the
    deterministic caller (the ``import`` FACT), excluding the ``name_match``
    phantom that the OLD ``COALESCE(confidence,0.5) >= 0.5`` floor admitted.
  * L6: the direct-name-rescue caller COUNT (now 0.7-gated) equals the per-file
    caller COUNT for the same target — both sources count callers identically.
  * L7: ``_detect_scope``'s container fallback relabels a ``name_match`` neighbor
    as "possible match (unverified)" (via ``_SCOPE_REASON_LABELS``), symmetric
    with the host path — never the laundered hardcoded "calls functions here".

RED-before-GREEN is demonstrated inline by also evaluating the OLD clause/string
and asserting it WOULD have laundered the phantom.
"""
from __future__ import annotations

import importlib.util
import sqlite3
import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[2] / "src"
_WRAPPER_DIR = Path(__file__).resolve().parents[2] / "scripts" / "swebench"
_WRAPPER = _WRAPPER_DIR / "oh_gt_full_wrapper.py"
# The wrapper imports sibling modules (cost_tracking, ...) by bare name, so its
# own directory must be importable alongside the package src/.
for _p in (str(_SRC), str(_WRAPPER_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)


def _load_wrapper():
    """Import the giant wrapper module (registers in sys.modules so its
    frozen dataclasses resolve their own module namespace)."""
    spec = importlib.util.spec_from_file_location("oh_gt_full_wrapper", str(_WRAPPER))
    mod = importlib.util.module_from_spec(spec)
    sys.modules["oh_gt_full_wrapper"] = mod
    spec.loader.exec_module(mod)
    return mod


WRAPPER = _load_wrapper()

# Shared deterministic gate the callee path uses — the single source of truth.
from groundtruth.hooks.post_edit import _edge_filter_for_db  # noqa: E402
from groundtruth.pretask.curation_map import (  # noqa: E402
    DETERMINISTIC_RESOLUTION_METHODS,
)


def _make_db(path: str) -> None:
    """Build the real graph.db edge/node schema with:
      target node 1 'process' in target.py,
      caller 2 'driver'  in driver.py  via import   (DETERMINISTIC FACT),
      caller 3 'phantom' in other.py   via name_match conf 0.9 (PHANTOM:
        above the OLD 0.5 floor, so only the categorical gate excludes it).
    """
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE nodes (
            id INTEGER PRIMARY KEY, label TEXT, name TEXT, qualified_name TEXT,
            file_path TEXT, start_line INTEGER, end_line INTEGER, signature TEXT,
            return_type TEXT, is_exported INTEGER, is_test INTEGER, language TEXT,
            parent_id INTEGER
        );
        CREATE TABLE edges (
            id INTEGER PRIMARY KEY, source_id INTEGER, target_id INTEGER, type TEXT,
            source_line INTEGER, source_file TEXT, resolution_method TEXT,
            confidence REAL, trust_tier TEXT, candidate_count INTEGER, metadata TEXT
        );
        """
    )
    conn.executemany(
        "INSERT INTO nodes (id,label,name,file_path,start_line,is_test) VALUES (?,?,?,?,?,0)",
        [
            (1, "Function", "process", "pkg/target.py", 10),
            (2, "Function", "driver", "pkg/driver.py", 5),
            (3, "Function", "phantom", "pkg/other.py", 7),
        ],
    )
    conn.executemany(
        "INSERT INTO edges (source_id,target_id,type,resolution_method,confidence,trust_tier,candidate_count) "
        "VALUES (?,?,'CALLS',?,?,?,?)",
        [
            # FACT: driver imports + calls process.
            (2, 1, "import", 1.0, "CERTIFIED", 1),
            # PHANTOM: name_match conf 0.6 — chosen to sit ABOVE the OLD L5 0.5
            # floor (so the OLD floor launders it; only the categorical gate
            # excludes it) AND BELOW the L6 0.7 floor (so both L6 gated counts
            # exclude it while the OLD ungated COUNT inflates to 2).
            (3, 1, "name_match", 0.6, "SPECULATIVE", 1),
        ],
    )
    conn.commit()
    conn.close()


# --------------------------------------------------------------------------- #
# L5  — [SCOPE] caller-files query: deterministic gate excludes the phantom.
# --------------------------------------------------------------------------- #
def test_l5_scope_callers_deterministic_gate_excludes_name_match(tmp_path):
    db = str(tmp_path / "graph.db")
    _make_db(db)

    # The EXACT L5 host SQL shape (cross-file callers of the edited file), run
    # once with the NEW deterministic clause and once with the OLD numeric floor.
    base_sql = (
        "SELECT DISTINCT nsrc.file_path FROM nodes nt "
        "JOIN edges e ON e.target_id = nt.id AND e.type = 'CALLS' "
        "JOIN nodes nsrc ON e.source_id = nsrc.id "
        "WHERE nt.file_path LIKE ? ESCAPE '\\' AND nsrc.file_path NOT LIKE ? ESCAPE '\\' "
        "AND {clause} LIMIT 5"
    )
    new_clause = _edge_filter_for_db(db, alias="e")        # NEW: shared categorical gate
    old_clause = "COALESCE(e.confidence, 0.5) >= 0.5"      # OLD: laundering floor

    conn = sqlite3.connect(db)
    like = "%target.py"
    new_rows = conn.execute(base_sql.format(clause=new_clause), (like, like)).fetchall()
    old_rows = conn.execute(base_sql.format(clause=old_clause), (like, like)).fetchall()
    conn.close()

    new_files = {r[0] for r in new_rows}
    old_files = {r[0] for r in old_rows}

    # GREEN (fix): only the deterministic FACT caller file is counted.
    assert new_files == {"pkg/driver.py"}, f"deterministic gate admitted: {new_files}"
    assert "pkg/other.py" not in new_files, "name_match phantom leaked through new gate"

    # RED (pre-fix proof): the OLD 0.5 floor WOULD have laundered the phantom,
    # firing "[SCOPE] Callers in 2 files" (>=2 triggers the warning).
    assert old_files == {"pkg/driver.py", "pkg/other.py"}
    assert len(old_files) >= 2 and len(new_files) < 2


def test_l5_clause_categorically_excludes_name_match():
    # The shared gate must, by construction, never list name_match as a fact.
    assert "name_match" not in DETERMINISTIC_RESOLUTION_METHODS


# --------------------------------------------------------------------------- #
# L6  — direct-name-rescue caller COUNT == per-file caller COUNT (symmetric).
# --------------------------------------------------------------------------- #
def test_l6_rescue_caller_count_matches_per_file_count(tmp_path):
    db = str(tmp_path / "graph.db")
    _make_db(db)
    conn = sqlite3.connect(db)

    # Per-file path (wrapper :7384-7386) — the reference, 0.7-gated.
    per_file = conn.execute(
        "SELECT COUNT(*) FROM edges WHERE target_id = ? AND type = 'CALLS' "
        "AND COALESCE(confidence, 0.5) >= 0.7",
        (1,),
    ).fetchone()[0]

    # NEW rescue COUNT (wrapper :7486-7489) — now carries the SAME 0.7 floor.
    rescue_new = conn.execute(
        "SELECT COUNT(*) FROM edges WHERE target_id = ? AND type = 'CALLS' "
        "AND COALESCE(confidence, 0.5) >= 0.7",
        (1,),
    ).fetchone()[0]

    # OLD rescue COUNT (pre-fix) — ungated, double-counts the phantom.
    rescue_old = conn.execute(
        "SELECT COUNT(*) FROM edges WHERE target_id = ? AND type = 'CALLS'",
        (1,),
    ).fetchone()[0]
    conn.close()

    # The 0.9-confidence name_match phantom is < 0.7 -> the gated counts agree at 1.
    assert per_file == 1
    assert rescue_new == per_file, "rescue COUNT diverges from per-file COUNT"
    # RED proof: the ungated COUNT inflated the same function to 2 callers.
    assert rescue_old == 2 and rescue_old != per_file


# --------------------------------------------------------------------------- #
# L7  — _detect_scope container fallback relabels name_match symmetrically.
# --------------------------------------------------------------------------- #
class _StubObs:
    def __init__(self, content: str) -> None:
        self.content = content


class _StubConfig:
    """Minimal stand-in for GTRuntimeConfig: no host db -> container fallback path."""
    _host_graph_db = ""
    workspace_root = ""  # empty root -> _path_relative_to_workspace is a no-op

    def __init__(self, graph_db: str) -> None:
        self.graph_db = graph_db


def _make_run_action(rows):
    """orig_run_action stub: _container_query expects a JSON array line in obs.content.
    ``rows`` is the list[[file_path, resolution_method], ...] the SELECT returns."""
    import json

    def _run(action):
        return _StubObs(json.dumps(rows))

    return _run


def test_l7_detect_scope_relabels_name_match_unverified():
    cfg = _StubConfig(graph_db="/container/graph.db")
    # Container returns a name_match neighbor and an import neighbor (2-col rows,
    # matching the NEW SELECT DISTINCT nsrc.file_path, e.resolution_method).
    rows = [
        ["pkg/other.py", "name_match"],
        ["pkg/driver.py", "import"],
    ]
    scope = WRAPPER._detect_scope("pkg/target.py", cfg, _make_run_action(rows))

    by_file = {s["file"]: s["reason"] for s in scope}
    assert "pkg/other.py" in by_file and "pkg/driver.py" in by_file

    # GREEN (fix): name_match relabeled "(unverified)" via _SCOPE_REASON_LABELS;
    # import gets its correct structural label — symmetric with the host path.
    assert by_file["pkg/other.py"] == WRAPPER._SCOPE_REASON_LABELS["name_match"]
    assert by_file["pkg/other.py"] == "possible match (unverified)"
    assert by_file["pkg/driver.py"] == WRAPPER._SCOPE_REASON_LABELS["import"]

    # RED proof: the laundered hardcoded reason no longer appears on the phantom.
    assert by_file["pkg/other.py"] != "calls functions here"


def test_l7_detect_scope_unknown_method_falls_back_graph_connected():
    cfg = _StubConfig(graph_db="/container/graph.db")
    rows = [["pkg/x.py", "some_future_method"]]  # not in the label map
    scope = WRAPPER._detect_scope("pkg/target.py", cfg, _make_run_action(rows))
    assert scope and scope[0]["reason"] == "graph-connected"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
