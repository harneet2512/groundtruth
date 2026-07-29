"""C35 probe — does the global arbiter deliver on a scaffold with NO batch-commit handshake?

WHY THIS FILE EXISTS. Run 30390877219 fails `dose_lte_one`: 4 observations carried TWO GT blocks
at back-to-back spans (`consensus.scope_map` + `l3b.evidence` on one `post_view`). The cause is
`gt_mini_patch.py:20121`:

    if _ga_on and not _batch_commit_installed:
        _ga_on = False        # FAIL-OPEN (2026-07-22)

whose stated reason is "with the arbiter ON and no flush hook, the per-turn pool would never be
committed -> silent 0-delivery of EVERY reactive FACT on every observation".

THAT REASON IS IN TENSION WITH THE CODE. `_batch_defer = _batch_state is not None`, and with no
handshake there is no batch state, so `_ga_pool` becomes a fresh `[]` (line 20137-20138) and the
flush at line 21000 runs INLINE because `not _batch_defer` is True. The inline flush landed
2026-07-14 (5324d11d0); the fail-open landed 2026-07-22 (15847661e) — eight days LATER, so
"the comment is merely stale" is not established either. Archaeology cannot settle it.

AND THE EXISTING SUITE CANNOT SETTLE IT EITHER. `test_sm5_global_arbiter_20260711.py:65` does
`monkeypatch.setattr(g, "_batch_commit_installed", True)`. Every arbiter test therefore runs in a
configuration mini-swe NEVER reaches, so `test_off_is_byte_identical_and_on_single_plane_matches`
proves nothing about production: with the real value (False) the fail-open silently disables the
very mechanism under test.

So this file asks the question in the PRODUCTION posture — `_batch_commit_installed` False, which
is what mini-swe always has — and records the answer as an artifact rather than an argument. It
asserts only what it observes; it does not presume the fail-open is wrong.
"""

from __future__ import annotations

import sqlite3

import pytest

import gt_mini_patch as g


def _mk_graph(tmp_path):
    db = str(tmp_path / "graph.db")
    con = sqlite3.connect(db)
    con.executescript(
        "CREATE TABLE nodes(id INTEGER PRIMARY KEY, label TEXT, name TEXT,"
        " qualified_name TEXT, file_path TEXT, start_line INTEGER, end_line INTEGER,"
        " signature TEXT, return_type TEXT, is_exported INTEGER, is_test INTEGER,"
        " language TEXT, parent_id INTEGER);"
        "CREATE TABLE edges(id INTEGER PRIMARY KEY, source_id INTEGER, target_id INTEGER,"
        " type TEXT, source_line INTEGER, source_file TEXT, resolution_method TEXT,"
        " confidence REAL, metadata TEXT);")
    for node_id, name, path, line in (
        (1, "run", "a/x.py", 10),
        (2, "run", "b/y.py", 20),
    ):
        con.execute(
            "INSERT INTO nodes(id,label,name,file_path,start_line,end_line,is_test,language)"
            " VALUES(?,?,?,?,?,?,?,?)",
            (node_id, "Function", name, path, line, line + 5, 0, "python"))
    con.commit()
    con.close()
    return db


@pytest.fixture()
def production_seam(tmp_path, monkeypatch):
    """The mini-swe posture: no batch-commit handshake installed, and none attempted."""
    db = _mk_graph(tmp_path)
    monkeypatch.setattr(g, "_db_path", lambda: db)
    monkeypatch.setattr(g, "_root", lambda: str(tmp_path))
    monkeypatch.setattr(g, "_POST_SEARCH_ON", False)
    monkeypatch.delenv("GT_POST_SEARCH_NATIVE", raising=False)
    # THE WHOLE POINT: the real production values, not the suite's forced True.
    monkeypatch.setattr(g, "_batch_commit_installed", False)
    monkeypatch.setattr(g, "_batch_install_failed", False)
    g._reset_oracle_state()
    yield db
    g._reset_oracle_state()


def _run(cmd, output, *, rc=0):
    action, out = {"command": cmd}, {"output": output, "returncode": rc}
    g._augment_output(action, out)
    return out["output"]


_GREP = "a/x.py:10: run\nb/y.py:20: run"


def test_the_probe_can_produce_a_non_zero(production_seam, monkeypatch):
    """CALIBRATION. If the seam delivered nothing in ANY posture, every result below is
    unreadable — a zero would mean 'the harness is inert', not 'the arbiter is silent'."""
    monkeypatch.setenv("GT_GATEWAY", "1")
    monkeypatch.setenv("GT_GLOBAL_ARBITER", "0")
    out = _run("grep -rn run .", _GREP)
    assert out.startswith(_GREP)
    assert len(out) > len(_GREP), "arbiter-OFF must deliver, or this file measures nothing"


def test_arbiter_on_without_a_batch_handshake_still_delivers(production_seam, monkeypatch):
    """THE QUESTION. With `_batch_commit_installed` False — mini-swe's only posture — does
    turning the arbiter ON keep the evidence flowing?

    If this passes, the fail-open at gt_mini_patch:20121 is not load-bearing for DELIVERY, and
    the `dose_lte_one` violations in run 30390877219 are the avoidable cost of a guard that is
    protecting against a condition the inline flush already handles.

    If it FAILS, the fail-open IS load-bearing, C35 must be fixed at `_lane_a_deliver` instead,
    and this test is the record of why.
    """
    monkeypatch.setenv("GT_GATEWAY", "1")

    monkeypatch.setenv("GT_GLOBAL_ARBITER", "0")
    off = _run("grep -rn run .", _GREP)
    g._reset_oracle_state()

    monkeypatch.setenv("GT_GLOBAL_ARBITER", "1")
    on = _run("grep -rn run .", _GREP)

    assert on.startswith(_GREP), "TITO: GT must remain a pure suffix on the agent's own output"
    assert len(on) > len(_GREP), (
        "arbiter ON with no batch handshake delivered NOTHING — the fail-open is load-bearing"
    )
    assert on == off, "single plane: the pool thunk must equal the inline delivery"


def test_the_fail_open_is_what_disables_the_arbiter(production_seam, monkeypatch):
    """Pin the MECHANISM, not just the symptom: with no handshake the flag is on and the
    effective arbiter is off. This is the line that has to change if the fix lands here."""
    monkeypatch.setenv("GT_GLOBAL_ARBITER", "1")
    assert g._global_arbiter_on() is True
    assert g._batch_commit_installed is False
