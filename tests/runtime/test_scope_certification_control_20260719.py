"""RED-first: the GT_SCOPE_NATIVE scope-certification NO_EFFECT receipt.

Dark behaviour (pre-fix, proven against run4 art 29694879462 where GT_SCOPE_NATIVE
had ZERO control.participation rows and graded UNAVAILABLE/HOLD): the gateway's
``_certified_search_scope`` decides whether a certified scope constraint exists for the
ranked localization anchor, but recorded NOTHING when it correctly found no qualifying
cross-file neighbour. The mini-seam splice only ever records the APPLIED/delivered half,
so the far-more-common correctly-SILENT decision was invisible to the REFEREE-3 terminal.

These tests pin the fix: a correctly-silent certification emits ONE typed GT_SCOPE_NATIVE
NO_EFFECT ``control.participation`` decision through the seam-wired recorder, keyed on the
GENERAL condition (an empty certified selection), byte-preserving, and never competing with
the splice's APPLIED (which only fires on a shaped, non-empty selection).
"""

from __future__ import annotations

import sqlite3

import pytest

from groundtruth.runtime.control_participation import build_control_participation
from groundtruth.runtime.gateway import GatewayState, _certified_search_scope


def _graph(path: str, *, anchor_has_certified_neighbour: bool) -> None:
    con = sqlite3.connect(path)
    con.executescript(
        """
        CREATE TABLE nodes(id INTEGER PRIMARY KEY, file_path TEXT, is_test INTEGER DEFAULT 0);
        CREATE TABLE edges(id INTEGER PRIMARY KEY, source_id INTEGER, target_id INTEGER,
          type TEXT, resolution_method TEXT, confidence REAL);
        CREATE TABLE project_meta(key TEXT PRIMARY KEY, value TEXT);
        INSERT INTO project_meta VALUES('post_revision', 'graph-rev-9');
        INSERT INTO nodes VALUES(1, 'src/anchor.py', 0);
        INSERT INTO nodes VALUES(2, 'src/other.py', 0);
        INSERT INTO nodes VALUES(3, 'tests/test_anchor.py', 1);
        -- name_match (non-deterministic) and a below-floor edge: neither is certifiable,
        -- so the anchor certifies an EMPTY selection = correctly silent.
        INSERT INTO edges VALUES(10, 1, 2, 'CALLS', 'name_match', 1.0);
        INSERT INTO edges VALUES(11, 1, 3, 'CALLS', 'import', 1.0);
        """
    )
    if anchor_has_certified_neighbour:
        # one high-confidence import edge to a real non-test file => a shaped selection.
        con.execute(
            "INSERT INTO edges VALUES(12, 1, 2, 'CALLS', 'import', 1.0)"
        )
    con.commit()
    con.close()


def _recorder():
    calls: list[tuple] = []

    def rec(feature_id, decision_site, decision, **extra):
        calls.append((feature_id, decision_site, decision, extra))

    return rec, calls


def test_silent_certification_emits_typed_no_effect_scope_receipt(tmp_path, monkeypatch):
    """The dark->live pin: an empty certified selection records one schema-valid
    GT_SCOPE_NATIVE NO_EFFECT control.participation decision. FAILS pre-fix (no emit)."""
    monkeypatch.setenv("GT_SCOPE_NATIVE", "1")
    db = tmp_path / "graph.db"
    _graph(str(db), anchor_has_certified_neighbour=False)
    rec, calls = _recorder()
    state = GatewayState(
        graph_db=str(db), repo_root=str(tmp_path), issue_text="x", control_recorder=rec,
    )

    selection = _certified_search_scope(state, "src/anchor.py")

    # behaviour preserved: certification still ran and found nothing certifiable.
    assert selection.graph_revision == "graph-rev-9"
    assert selection.relations == ()

    # exactly one typed GT_SCOPE_NATIVE NO_EFFECT decision was recorded.
    assert len(calls) == 1, calls
    feature_id, decision_site, decision, extra = calls[0]
    assert feature_id == "GT_SCOPE_NATIVE"
    assert decision_site == "mini_seam.scope.native_render"
    assert decision == "NO_EFFECT"
    assert extra["fact_class"] == "localization"
    assert extra["candidate_id"] == "localization:src/anchor.py"
    assert extra["candidate_bytes"] == ""
    assert extra["reason"]

    # the emitted arguments form a REAL, schema-valid typed receipt (not just a log line):
    # zero-byte NO_EFFECT mediator with concrete registered fact class + canonical id.
    record = build_control_participation(
        feature_id=feature_id, decision_site=decision_site, decision=decision,
        iteration=0, candidate_bytes=extra["candidate_bytes"],
        fact_class=extra["fact_class"], candidate_id=extra["candidate_id"],
        reason=extra["reason"],
    )
    assert record.role == "mediator"
    assert record.candidate_chars == 0 and record.candidate_sha256_16 == ""


def test_shaped_certification_stays_quiet_at_gateway(tmp_path, monkeypatch):
    """A NON-empty (shaped) selection is the splice's APPLIED/delivered half: the gateway
    emits NOTHING, so the two authorities never compete for one certification."""
    monkeypatch.setenv("GT_SCOPE_NATIVE", "1")
    db = tmp_path / "graph.db"
    _graph(str(db), anchor_has_certified_neighbour=True)
    rec, calls = _recorder()
    state = GatewayState(
        graph_db=str(db), repo_root=str(tmp_path), issue_text="x", control_recorder=rec,
    )

    selection = _certified_search_scope(state, "src/anchor.py")

    assert selection.relations  # shaped
    assert calls == []          # gateway silent; the delivery-time splice owns APPLIED


def test_flag_off_records_nothing(tmp_path, monkeypatch):
    """Arm off (GT_SCOPE_NATIVE unset) => no participation emit even when silent."""
    monkeypatch.delenv("GT_SCOPE_NATIVE", raising=False)
    db = tmp_path / "graph.db"
    _graph(str(db), anchor_has_certified_neighbour=False)
    rec, calls = _recorder()
    state = GatewayState(
        graph_db=str(db), repo_root=str(tmp_path), issue_text="x", control_recorder=rec,
    )

    selection = _certified_search_scope(state, "src/anchor.py")

    assert selection.relations == ()
    assert calls == []


def test_no_recorder_is_exact_no_op(tmp_path, monkeypatch):
    """No seam-wired recorder => emit is a total no-op and never raises."""
    monkeypatch.setenv("GT_SCOPE_NATIVE", "1")
    db = tmp_path / "graph.db"
    _graph(str(db), anchor_has_certified_neighbour=False)
    state = GatewayState(graph_db=str(db), repo_root=str(tmp_path), issue_text="x")

    selection = _certified_search_scope(state, "src/anchor.py")
    assert selection.relations == ()


def test_leaky_anchor_stays_quiet(tmp_path, monkeypatch):
    """A test/vendored anchor never earns a scope receipt (identity firewall)."""
    monkeypatch.setenv("GT_SCOPE_NATIVE", "1")
    db = tmp_path / "graph.db"
    _graph(str(db), anchor_has_certified_neighbour=False)
    rec, calls = _recorder()
    state = GatewayState(
        graph_db=str(db), repo_root=str(tmp_path), issue_text="x", control_recorder=rec,
    )

    # tests/test_anchor.py is a single unique anchor with no certifiable neighbour,
    # but it is leaky => no receipt.
    _certified_search_scope(state, "tests/test_anchor.py")
    assert calls == []
