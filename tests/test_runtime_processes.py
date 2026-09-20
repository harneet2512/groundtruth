"""Capability tests for groundtruth.runtime.processes — flow detection on ground truth.

The fixture builds a real graph.db-shaped SQLite database with a known call
graph, then asserts the detector produces exactly the expected flows:
correct membership, correct step order, correct entry selection, min-steps
filtering, prefix dedup, provenance tiers, and honest truncation reporting.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from groundtruth.runtime.processes import (
    detect_processes,
    find_entry_points,
    render_process_block,
)


def _build_graph_db(path: Path) -> None:
    """Fixture graph:

        main -> app.start -> handler.process -> validate -> check
                           |                 -> transform
                           -> db.connect
        main -> util.helper
        test_x (is_test) -> validate   (tests excluded from entries)
        orphan_a -> orphan_b           (2-node flow, below min_steps)
    """
    db = sqlite3.connect(path)
    db.executescript(
        """
        CREATE TABLE nodes (
            id INTEGER PRIMARY KEY, label TEXT, name TEXT,
            qualified_name TEXT, file_path TEXT, start_line INTEGER,
            end_line INTEGER, signature TEXT, return_type TEXT,
            is_exported INTEGER, is_test INTEGER, language TEXT,
            parent_id INTEGER
        );
        CREATE TABLE edges (
            id INTEGER PRIMARY KEY, source_id INTEGER, target_id INTEGER,
            type TEXT, source_line INTEGER, source_file TEXT,
            resolution_method TEXT, confidence REAL, metadata TEXT,
            trust_tier TEXT, candidate_count INTEGER, evidence_type TEXT,
            verification_status TEXT
        );
        """
    )
    nodes = [
        # id, label, name, file, line, is_test
        (1, "Function", "main", "main.py", 10, 0),
        (2, "Function", "start", "app.py", 5, 0),
        (3, "Function", "process", "handler.py", 20, 0),
        (4, "Function", "validate", "validate.py", 8, 0),
        (5, "Function", "check", "check.py", 3, 0),
        (6, "Function", "transform", "transform.py", 12, 0),
        (7, "Function", "connect", "db.py", 4, 0),
        (8, "Function", "helper", "util.py", 15, 0),
        (9, "Function", "test_validate", "test_validate.py", 1, 1),
        (10, "Function", "orphan_a", "orphan.py", 1, 0),
        (11, "Function", "orphan_b", "orphan.py", 9, 0),
        (12, "Class", "Unused", "unused.py", 1, 0),
    ]
    db.executemany(
        "INSERT INTO nodes (id,label,name,qualified_name,file_path,start_line,"
        "end_line,signature,return_type,is_exported,is_test,language,parent_id)"
        " VALUES (?,?,?,?,?,?,?,?,'',1,?, 'python',NULL)",
        [
            (i, label, name, name, fp, line, line + 5, f"{name}()", is_test)
            for i, label, name, fp, line, is_test in nodes
        ],
    )
    edges = [
        # (src, tgt, method, conf, tier, cands)
        (1, 2, "same_file", 1.0, "CERTIFIED", 1),
        (2, 3, "same_file", 1.0, "CERTIFIED", 1),
        (3, 4, "same_file", 1.0, "CERTIFIED", 1),
        (4, 5, "same_file", 1.0, "CERTIFIED", 1),
        (3, 6, "same_file", 1.0, "CERTIFIED", 1),
        (2, 7, "same_file", 1.0, "CERTIFIED", 1),
        (1, 8, "same_file", 1.0, "CERTIFIED", 1),
        (9, 4, "same_file", 1.0, "CERTIFIED", 1),
        (10, 11, "name_match", 0.5, "CANDIDATE", 1),
    ]
    db.executemany(
        "INSERT INTO edges (source_id,target_id,type,resolution_method,"
        "confidence,trust_tier,candidate_count) VALUES (?,?,'CALLS',?,?,?,?)",
        edges,
    )
    db.commit()
    db.close()


@pytest.fixture()
def graph_db(tmp_path: Path) -> Path:
    path = tmp_path / "graph.db"
    _build_graph_db(path)
    return path


def test_detects_expected_flows(graph_db: Path) -> None:
    result = detect_processes(graph_db)
    chains = {tuple(n.name for n in p.nodes) for p in result.processes}
    assert ("main", "start", "process", "validate", "check") in chains
    assert ("main", "start", "process", "transform") in chains


def test_min_steps_filters_short_flows(graph_db: Path) -> None:
    result = detect_processes(graph_db)
    for p in result.processes:
        assert p.step_count >= 3
    chains = {tuple(n.name for n in p.nodes) for p in result.processes}
    assert ("main", "start", "connect") not in chains  # 2 edges
    assert ("orphan_a", "orphan_b") not in chains  # 1 edge


def test_entry_points_exclude_tests_and_callees(graph_db: Path) -> None:
    result = detect_processes(graph_db)
    entries = {p.entry.name for p in result.processes}
    assert "test_validate" not in entries  # is_test excluded
    assert "start" not in entries  # has a caller (main)
    assert "process" not in entries
    assert entries == {"main"}


def test_prefixes_deduplicated(graph_db: Path) -> None:
    result = detect_processes(graph_db)
    chains = [tuple(n.name for n in p.nodes) for p in result.processes]
    # No flow may be a strict prefix of another returned flow.
    for chain in chains:
        for other in chains:
            if chain != other:
                assert chain[: len(chain)] != other[: len(chain)] or chain == other
                assert not (len(other) > len(chain) and other[: len(chain)] == chain)


def test_labels_and_certified_ratio(graph_db: Path) -> None:
    result = detect_processes(graph_db)
    by_label = {p.label: p for p in result.processes}
    long_flow = by_label["main -> check"]
    assert long_flow.certified_ratio == 1.0
    assert long_flow.entry_kind == "declared_main"
    assert "lower bound" not in long_flow.rendered


def test_uncertified_flow_marked_lower_bound(tmp_path: Path) -> None:
    """A flow containing CANDIDATE hops renders as a lower bound."""
    path = tmp_path / "g.db"
    db = sqlite3.connect(path)
    db.executescript(
        "CREATE TABLE nodes (id INTEGER PRIMARY KEY, label TEXT, name TEXT,"
        " file_path TEXT, start_line INTEGER, is_test INTEGER, signature TEXT);"
        "CREATE TABLE edges (id INTEGER PRIMARY KEY, source_id INTEGER,"
        " target_id INTEGER, type TEXT, resolution_method TEXT,"
        " confidence REAL, trust_tier TEXT, candidate_count INTEGER);"
    )
    db.executemany(
        "INSERT INTO nodes (id,label,name,file_path,start_line,is_test,"
        "signature) VALUES (?,?,?,?,?,0,'')",
        [(i, "Function", n, f"{n}.py", i) for i, n in enumerate(
            ("a", "b", "c", "d"), start=1)],
    )
    db.executemany(
        "INSERT INTO edges (source_id,target_id,type,resolution_method,"
        "confidence,trust_tier,candidate_count) VALUES (?,?,'CALLS',?,?,?,?)",
        [
            (1, 2, "same_file", 1.0, "CERTIFIED", 1),
            (2, 3, "name_match", 0.6, "CANDIDATE", 1),
            (3, 4, "same_file", 1.0, "CERTIFIED", 1),
        ],
    )
    db.commit()
    db.close()
    result = detect_processes(path)
    assert len(result.processes) == 1
    proc = result.processes[0]
    assert proc.certified_ratio < 1.0
    assert "lower bound" in proc.rendered


def test_determinism(graph_db: Path) -> None:
    first = detect_processes(graph_db)
    second = detect_processes(graph_db)
    assert [p.process_id for p in first.processes] == [
        p.process_id for p in second.processes
    ]


def test_truncation_stats_present(graph_db: Path) -> None:
    result = detect_processes(graph_db)
    stats = result.stats
    assert stats.entry_candidates_dropped >= 0
    assert isinstance(stats.truncated, bool)
    assert result.node_count == 12
    assert result.edge_count == 9


def test_render_block_bounded(graph_db: Path) -> None:
    result = detect_processes(graph_db)
    text = render_process_block(result.processes, max_processes=1)
    assert "main -> check" in text
    assert "main -> transform" not in text


def test_empty_graph(tmp_path: Path) -> None:
    path = tmp_path / "empty.db"
    db = sqlite3.connect(path)
    db.executescript(
        "CREATE TABLE nodes (id INTEGER PRIMARY KEY, label TEXT, name TEXT,"
        " file_path TEXT, start_line INTEGER, is_test INTEGER, signature TEXT);"
        "CREATE TABLE edges (id INTEGER PRIMARY KEY, source_id INTEGER,"
        " target_id INTEGER, type TEXT, resolution_method TEXT,"
        " confidence REAL, trust_tier TEXT, candidate_count INTEGER);"
    )
    db.close()
    result = detect_processes(path)
    assert result.processes == []
    assert result.edge_count == 0


def _add_persisted_process_tables(db: sqlite3.Connection) -> None:
    """The Go process layer's certified/witnessed surface (persist.go Schema)."""
    db.executescript(
        """
        CREATE TABLE processes (
            id TEXT PRIMARY KEY, entry_stable_id TEXT NOT NULL,
            terminal_stable_id TEXT NOT NULL,
            witness_assertion_id INTEGER NOT NULL,
            test_stable_id TEXT NOT NULL, kind TEXT NOT NULL DEFAULT '',
            depth INTEGER NOT NULL, trust_floor TEXT NOT NULL
        );
        CREATE TABLE process_steps (
            process_id TEXT NOT NULL, ordinal INTEGER NOT NULL,
            stable_id TEXT NOT NULL,
            PRIMARY KEY(process_id, ordinal)
        );
        CREATE TABLE resolution_symbols (
            stable_id TEXT, native_id TEXT
        );
        """
    )


def test_persisted_processes_preferred_and_witnessed(tmp_path: Path) -> None:
    """A test-witnessed persisted flow outranks the heuristic reconstruction
    of the same path — same nodes, stronger provenance — and surfaces once."""
    path = tmp_path / "g.db"
    _build_graph_db(path)
    db = sqlite3.connect(path)
    _add_persisted_process_tables(db)
    # stable ids via resolution_symbols.native_id -> nodes.id
    db.executemany(
        "INSERT INTO resolution_symbols (stable_id, native_id) VALUES (?,?)",
        [(f"sid_{i}", str(i)) for i in range(1, 13)],
    )
    # Witnessed flow: main -> start -> process -> transform (heuristic also
    # traces this exact path; the persisted copy must win).
    db.execute(
        "INSERT INTO processes (id,entry_stable_id,terminal_stable_id,"
        "witness_assertion_id,test_stable_id,kind,depth,trust_floor)"
        " VALUES ('p1','sid_1','sid_6',7,'sid_9','test_witnessed',3,'CERTIFIED')"
    )
    db.executemany(
        "INSERT INTO process_steps (process_id,ordinal,stable_id) VALUES (?,?,?)",
        [("p1", 0, "sid_1"), ("p1", 1, "sid_2"), ("p1", 2, "sid_3"),
         ("p1", 3, "sid_6")],
    )
    db.commit()
    db.close()

    result = detect_processes(path)
    witnessed = [p for p in result.processes if p.witnessed]
    assert len(witnessed) == 1
    proc = witnessed[0]
    assert [n.name for n in proc.nodes] == ["main", "start", "process", "transform"]
    assert proc.witness_test == "sid_9"
    # Edge provenance re-joined from CALLS adjacency — all certified.
    assert proc.certified_ratio == 1.0
    assert all(e.trust_tier == "CERTIFIED" for e in proc.edges)
    # Dedup: the same node path must not appear twice (heuristic copy dropped).
    chains = [tuple(n.node_id for n in p.nodes) for p in result.processes]
    assert len(chains) == len(set(chains))
    # Witnessed flow sorts first.
    assert result.processes[0].witnessed


def test_persisted_hop_without_calls_edge_is_tierless(tmp_path: Path) -> None:
    """A persisted step pair with no CALLS edge cannot claim certification."""
    path = tmp_path / "g.db"
    db = sqlite3.connect(path)
    db.executescript(
        "CREATE TABLE nodes (id INTEGER PRIMARY KEY, label TEXT, name TEXT,"
        " file_path TEXT, start_line INTEGER, is_test INTEGER, signature TEXT,"
        " stable_id TEXT);"
        "CREATE TABLE edges (id INTEGER PRIMARY KEY, source_id INTEGER,"
        " target_id INTEGER, type TEXT, resolution_method TEXT,"
        " confidence REAL, trust_tier TEXT, candidate_count INTEGER);"
    )
    db.executemany(
        "INSERT INTO nodes (id,label,name,file_path,start_line,is_test,"
        "signature,stable_id) VALUES (?,?,?,?,?,0,'',?)",
        [(i, "Function", n, f"{n}.py", i, f"sid_{i}") for i, n in enumerate(
            ("a", "b", "c", "d"), start=1)],
    )
    # a->b and b->c certified; the persisted path skips to d with no c->d edge.
    db.executemany(
        "INSERT INTO edges (source_id,target_id,type,resolution_method,"
        "confidence,trust_tier,candidate_count) VALUES (?,?,'CALLS',?,?,?,?)",
        [(1, 2, "same_file", 1.0, "CERTIFIED", 1),
         (2, 3, "same_file", 1.0, "CERTIFIED", 1)],
    )
    _add_persisted_process_tables(db)
    db.execute(
        "INSERT INTO processes (id,entry_stable_id,terminal_stable_id,"
        "witness_assertion_id,test_stable_id,kind,depth,trust_floor)"
        " VALUES ('p1','sid_1','sid_4',1,'sid_t','test_witnessed',3,'CERTIFIED')"
    )
    db.executemany(
        "INSERT INTO process_steps (process_id,ordinal,stable_id) VALUES (?,?,?)",
        [("p1", 0, "sid_1"), ("p1", 1, "sid_2"), ("p1", 2, "sid_3"),
         ("p1", 3, "sid_4")],
    )
    db.commit()
    db.close()

    result = detect_processes(path)
    witnessed = [p for p in result.processes if p.witnessed]
    assert len(witnessed) == 1
    proc = witnessed[0]
    # The c->d hop has no CALLS edge: tierless, so the flow is honestly
    # below 1.0 certified despite trust_floor='CERTIFIED'.
    assert proc.edges[-1].trust_tier == ""
    assert proc.edges[-1].resolution_method == "persisted_no_calls_edge"
    assert proc.certified_ratio < 1.0


def test_persisted_tables_absent_falls_back(graph_db: Path) -> None:
    """No processes/process_steps tables -> pure heuristic behavior."""
    result = detect_processes(graph_db)
    assert all(not p.witnessed for p in result.processes)
    assert len(result.processes) > 0
