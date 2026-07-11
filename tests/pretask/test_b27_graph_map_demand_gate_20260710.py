"""B-27 — the ``<gt-graph-map>`` is DEMAND-PAGED, not a step-0 narration dose.

FINDING (verifyandobserve.md B-27): ``gt_layers.md`` retired ``<gt-graph-map>`` but
``gt_gt.md`` still listed it at task start and ``_with_graph_map`` still EMITTED it at
step-0 (4/5 live shapes carried it) — an anti-narration doctrine violation.

FIX: demand-gate the map. At step-0 (not demand-triggered) the brief carries NO
``<gt-graph-map>`` by default; the map is emitted only when an explicit demand signal
(``GT_GRAPH_MAP_DEMAND``) requests it, and the demand-path bytes are byte-identical to
the historical step-0 emission.

RED→GREEN + MUTATION
--------------------
RED (pre-fix): ``_with_graph_map`` always emitted the map, so the default-path
assertions here FAIL. GREEN (post-fix): the demand gate suppresses it by default.
BITING: each test first proves — with the flag ON — that the fixture IS capable of
producing a map, so the flag-OFF suppression assertion is a real, non-vacuous guard.
Removing the gate (``if not _graph_map_demand_on(): return brief``) re-reddens the
OFF-path assertions.

HERMETIC: a tiny synthetic graph.db (gold ``apply_defaults`` + caller ``load_config``
via a deterministic ``import`` edge). No embedder / ONNX for the direct-boundary
tests; the embedder is forced OFF for the end-to-end render.
"""
from __future__ import annotations

import sqlite3

import pytest

import groundtruth.pretask.graph_localizer as GL
import groundtruth.pretask.v7_4_brief as V74
from groundtruth.pretask.v1r_brief import (
    FileEntry,
    _graph_map_demand_on,
    _with_graph_map,
    generate_v1r_brief,
)


# --------------------------------------------------------------------------- #
# hermetic fixture graph: apply_defaults(config) <- load_config (import edge)
# --------------------------------------------------------------------------- #
def _build_graph(db: str) -> None:
    con = sqlite3.connect(db)
    con.executescript(
        """CREATE TABLE nodes(id INTEGER PRIMARY KEY, label TEXT, name TEXT,
             qualified_name TEXT, file_path TEXT, start_line INTEGER, end_line INTEGER,
             signature TEXT, return_type TEXT, is_exported INTEGER, is_test INTEGER,
             language TEXT, parent_id INTEGER);
           CREATE TABLE edges(id INTEGER PRIMARY KEY, source_id INTEGER, target_id INTEGER,
             type TEXT, source_line INTEGER, source_file TEXT, resolution_method TEXT,
             confidence REAL, metadata TEXT);"""
    )
    con.execute(
        "INSERT INTO nodes(id,label,name,file_path,start_line,end_line,signature,is_test,language)"
        " VALUES(1,'Function','apply_defaults','pkg/config.py',1,3,'apply_defaults(cfg)',0,'python')"
    )
    con.execute(
        "INSERT INTO nodes(id,label,name,file_path,start_line,end_line,is_test,language)"
        " VALUES(2,'Function','load_config','pkg/loader.py',1,3,0,'python')"
    )
    con.execute(
        "INSERT INTO edges(id,source_id,target_id,type,source_line,resolution_method,confidence)"
        " VALUES(1,2,1,'CALLS',2,'import',1.0)"
    )
    con.commit()
    con.close()


def _write_tree(root) -> None:
    (root / "pkg").mkdir(parents=True, exist_ok=True)
    (root / "pkg" / "config.py").write_text(
        "def apply_defaults(cfg):\n    return cfg\n", encoding="utf-8"
    )
    (root / "pkg" / "loader.py").write_text(
        "def load_config():\n    return apply_defaults({})\n", encoding="utf-8"
    )


_ISSUE = "apply_defaults mutates the caller mapping; correct apply_defaults in config"

_BRIEF = "<gt-task-brief>\nsome evidence line\n</gt-task-brief>"
_FILES = [FileEntry(path="pkg/config.py", score=1.0, function_names=["apply_defaults"])]


# --------------------------------------------------------------------------- #
# the flag parser
# --------------------------------------------------------------------------- #
def test_flag_default_off(monkeypatch):
    monkeypatch.delenv("GT_GRAPH_MAP_DEMAND", raising=False)
    assert _graph_map_demand_on() is False
    for off in ("", "0", "false", "no", "NO", "False"):
        monkeypatch.setenv("GT_GRAPH_MAP_DEMAND", off)
        assert _graph_map_demand_on() is False, off
    for on in ("1", "true", "yes", "on"):
        monkeypatch.setenv("GT_GRAPH_MAP_DEMAND", on)
        assert _graph_map_demand_on() is True, on


# --------------------------------------------------------------------------- #
# direct boundary — _with_graph_map (fast, no embedder)
# --------------------------------------------------------------------------- #
def test_with_graph_map_suppressed_by_default(tmp_path, monkeypatch):
    """Default (no demand signal): _with_graph_map returns the brief BYTE-IDENTICAL,
    no <gt-graph-map> — even though the fixture IS capable of producing one (proven
    by the flag-ON branch)."""
    db = str(tmp_path / "g.db")
    _build_graph(db)

    # Precondition: the fixture CAN produce a map on the demand path (proves the
    # OFF-path suppression below is a real, biting guard — not a vacuous pass).
    monkeypatch.setenv("GT_GRAPH_MAP_DEMAND", "1")
    on = _with_graph_map(_BRIEF, _FILES, db)
    assert "<gt-graph-map>" in on, "fixture incapable of a map — suppression test vacuous"
    # the demand path places the map right after the open tag (historical placement).
    assert on.startswith("<gt-task-brief>\n<gt-graph-map>")

    # Default (flag OFF): suppressed + byte-identical passthrough.
    monkeypatch.delenv("GT_GRAPH_MAP_DEMAND", raising=False)
    off = _with_graph_map(_BRIEF, _FILES, db)
    assert "<gt-graph-map>" not in off
    assert off == _BRIEF, "OFF path must return the brief unchanged (byte-identical)"


# --------------------------------------------------------------------------- #
# end-to-end — generate_v1r_brief (embedder OFF for determinism)
# --------------------------------------------------------------------------- #
@pytest.fixture()
def _semantic_off(monkeypatch):
    monkeypatch.setattr(GL, "_get_embedder", lambda: None)

    def _zero_model():
        class _Z:
            dim = 8

            def encode(self, texts, **_):
                return [[0.0] * self.dim for _ in texts]

        return _Z()

    monkeypatch.setattr(V74, "_get_model", _zero_model)


def test_step0_brief_omits_graph_map_by_default(tmp_path, monkeypatch, _semantic_off):
    """TTD: a brief rendered at step-0 does NOT contain <gt-graph-map> under the new
    default; opting into the demand path restores the (historical) emission."""
    db = str(tmp_path / "g.db")
    _build_graph(db)
    root = tmp_path / "repo"
    _write_tree(root)

    monkeypatch.delenv("GT_GRAPH_MAP_DEMAND", raising=False)
    default_bt = generate_v1r_brief(_ISSUE, str(root), db).brief_text
    assert "<gt-graph-map>" not in default_bt, (
        "B-27: step-0 brief must NOT carry the graph-map by default"
    )

    monkeypatch.setenv("GT_GRAPH_MAP_DEMAND", "1")
    demand_bt = generate_v1r_brief(_ISSUE, str(root), db).brief_text
    assert "<gt-graph-map>" in demand_bt, (
        "demand path must still be able to emit the map"
    )
