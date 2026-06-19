"""A2 — substrate handoff fail-loud guard (root cause A2, 2026-06-14).

Root cause: the DeepSWE/pier in-container runtime producers read the graph via
``_db_path() -> GT_HOST_GRAPH_DB``, which resolved to a PRESENT-but-0-BYTE handoff
copy at ``/gt_artifacts/graph.db`` (the real graph lived at ``<task>/graph.db``). The
0-byte file passed ``os.path.isfile()``, but ``_connect_ro``'s schema probe failed on
it -> every graph producer returned "" -> ZERO <gt-contract>/<gt-evidence>/<gt-scope>
ever reached the agent. A silently-dead runtime channel.

The fix makes the handoff fail-CLOSED on a PRESENT-but-EMPTY graph and correct-or-QUIET
on a legitimately-ABSENT graph:
  * PRESENT-but-EMPTY (exists, 0-byte / no populated `nodes` table) in SUBSTRATE/PROOF
    mode  -> ``_db_path``/``_connect_ro`` RAISE ``GTHandoffEmptyError`` (BaseException,
    so the Lane-A ``except Exception`` guards never swallow it) after printing a
    classified ``[GT_META] error=GT_HANDOFF_EMPTY`` line.
  * ABSENT (path unset, or the file genuinely does not exist) -> no raise; producers
    stay quiet (return "").

GENERALITY / HELD-OUT: the REAL-graph arm is proven on the **go** substrate
(``D:/tmp/gtsub_go/graph.db``) — a different repo/language than the python (fastapi)
substrate the rest of GT was tuned on. The guard is a structural property of the db
(schema + a populated ``nodes`` table), not keyed to any repo/task/language.
"""
from __future__ import annotations

import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import gt_mini_patch as g  # noqa: E402

# Two of the four on-disk substrates. PY = the tuned shape (fastapi); GO = the HELD-OUT
# shape (a different repo + language). Skip cleanly if a substrate is not present so the
# suite runs anywhere; the guard logic itself is exercised even without them.
_PY_DB = "D:/tmp/gtsub/graph.db"
_GO_DB = "D:/tmp/gtsub_go/graph.db"


def _has(db: str) -> bool:
    return bool(db) and os.path.isfile(db) and os.path.getsize(db) > 0


def _a_real_file_with_funcs(db: str) -> str:
    """A real source file_path in ``db`` that declares several functions (so the
    producers have something to emit). Pure read."""
    c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        row = c.execute(
            "SELECT file_path FROM nodes WHERE label IN ('Function','Method') "
            "AND COALESCE(is_test,0)=0 GROUP BY file_path "
            "ORDER BY COUNT(*) DESC LIMIT 1"
        ).fetchone()
        return row[0] if row else ""
    finally:
        c.close()


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch):
    """Clear the handoff env + the cached per-path verdict before every test so cases
    don't leak into each other."""
    for k in ("GT_HOST_GRAPH_DB", "GT_GRAPH_DB", "GT_CERT_DIR", "GT_PORTABLE_SUBSTRATE",
              "GT_PROOF_MODE"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(g, "_handoff_guard_state", {})
    monkeypatch.setattr(g, "_graph_probe_printed", False)
    monkeypatch.setattr(g, "_GT_BASELINE", False, raising=False)


# ─────────────────────── 1. REAL graph -> emit (no raise) ───────────────────────

def _real_graph_emits(db: str, monkeypatch) -> None:
    monkeypatch.setenv("GT_PROOF_MODE", "1")
    monkeypatch.setenv("GT_PORTABLE_SUBSTRATE", "1")
    monkeypatch.setenv("GT_HOST_GRAPH_DB", db)
    monkeypatch.setattr(g, "_contract_seen", set(), raising=False)
    monkeypatch.setattr(g, "_consensus_state", {}, raising=False)

    # _db_path returns the real db WITHOUT raising; the guard caches it as usable.
    assert g._db_path() == db
    assert g._handoff_guard_state.get(db) is True

    # _connect_ro opens it (no raise, real connection).
    con = g._connect_ro(db)
    assert con is not None
    con.close()

    # A producer emits non-empty on a real file with functions (the channel is LIVE).
    rel = _a_real_file_with_funcs(db)
    assert rel, f"no function-bearing file found in {db}"
    # repo root is irrelevant for the SQL-only contract pillar; use the file's dir's root.
    body = g._graph_contract_block(rel)
    # The contract pillar may be correct-or-quiet on a given file, so also try evidence;
    # at least ONE structural producer must emit on a real graph (the channel works).
    monkeypatch.setattr(g, "_contract_seen", set(), raising=False)
    ev = g._evidence_body("post_view", rel, os.path.dirname(db))
    assert (body.strip() or ev.strip()), (
        f"both producers empty on a REAL graph for {rel} in {db} — channel dead")


@pytest.mark.skipif(not _has(_GO_DB), reason="go substrate not on disk")
def test_real_go_graph_emits_held_out(monkeypatch):
    """HELD-OUT: a real GO graph (different repo+language than the tuned fastapi
    substrate) drives the producers to emit — proving the channel is live, not blind."""
    _real_graph_emits(_GO_DB, monkeypatch)


@pytest.mark.skipif(not _has(_PY_DB), reason="python substrate not on disk")
def test_real_py_graph_emits(monkeypatch):
    _real_graph_emits(_PY_DB, monkeypatch)


# ─────────────── 2. PRESENT-but-EMPTY handoff -> fail-CLOSED (raise) ───────────────

def test_empty_handoff_db_path_raises(monkeypatch, tmp_path):
    """A 0-byte handoff in substrate mode is a HARD error from _db_path()."""
    empty = tmp_path / "graph.db"
    empty.write_bytes(b"")  # PRESENT but 0 bytes — the A2 failure shape
    assert empty.exists() and empty.stat().st_size == 0
    monkeypatch.setenv("GT_PROOF_MODE", "1")
    monkeypatch.setenv("GT_PORTABLE_SUBSTRATE", "1")
    monkeypatch.setenv("GT_HOST_GRAPH_DB", str(empty))
    with pytest.raises(g.GTHandoffEmptyError):
        g._db_path()


def test_empty_handoff_connect_ro_raises(monkeypatch, tmp_path):
    """The second chokepoint: _connect_ro also fail-closes on a present-but-empty
    handoff (rather than silently returning None)."""
    empty = tmp_path / "graph.db"
    empty.write_bytes(b"")
    monkeypatch.setenv("GT_PROOF_MODE", "1")
    monkeypatch.setenv("GT_HOST_GRAPH_DB", str(empty))
    with pytest.raises(g.GTHandoffEmptyError):
        g._connect_ro(str(empty))


def test_garbage_handoff_raises(monkeypatch, tmp_path):
    """A non-empty but NON-SQLITE file (truncated/garbage copy) is also fail-closed."""
    junk = tmp_path / "graph.db"
    junk.write_bytes(b"this is not a sqlite database at all\x00\x01")
    monkeypatch.setenv("GT_PROOF_MODE", "1")
    monkeypatch.setenv("GT_HOST_GRAPH_DB", str(junk))
    with pytest.raises(g.GTHandoffEmptyError):
        g._db_path()


def test_schema_but_no_nodes_raises(monkeypatch, tmp_path):
    """A real sqlite file whose `nodes` table is EMPTY (0 rows) is treated as an
    unusable handoff — a schema'd-but-empty copy still blinds the channel."""
    db = tmp_path / "graph.db"
    c = sqlite3.connect(str(db))
    c.execute("CREATE TABLE nodes (id INTEGER PRIMARY KEY, name TEXT)")
    c.commit()
    c.close()
    monkeypatch.setenv("GT_PROOF_MODE", "1")
    monkeypatch.setenv("GT_HOST_GRAPH_DB", str(db))
    with pytest.raises(g.GTHandoffEmptyError):
        g._db_path()


def test_empty_handoff_raise_is_not_caught_by_except_exception(monkeypatch, tmp_path):
    """GTHandoffEmptyError subclasses BaseException ON PURPOSE so the Lane-A producers'
    `except Exception` guards cannot absorb it into another silent "". Prove a broad
    `except Exception` does NOT catch it (it would re-blind the channel)."""
    empty = tmp_path / "graph.db"
    empty.write_bytes(b"")
    monkeypatch.setenv("GT_PROOF_MODE", "1")
    monkeypatch.setenv("GT_HOST_GRAPH_DB", str(empty))
    caught_as_exception = False
    raised = False
    try:
        try:
            g._db_path()
        except Exception:  # noqa: BLE001 — simulates a Lane-A producer guard
            caught_as_exception = True
    except g.GTHandoffEmptyError:
        raised = True
    assert raised and not caught_as_exception


def test_empty_handoff_emits_classified_gt_meta(monkeypatch, tmp_path, capsys):
    """The fail-closed path prints a classified [GT_META] error=GT_HANDOFF_EMPTY line
    (to stderr, never the agent's stdout context) so the cause is always visible."""
    empty = tmp_path / "graph.db"
    empty.write_bytes(b"")
    monkeypatch.setenv("GT_PROOF_MODE", "1")
    monkeypatch.setenv("GT_HOST_GRAPH_DB", str(empty))
    with pytest.raises(g.GTHandoffEmptyError):
        g._db_path()
    err = capsys.readouterr().err
    assert "GT_HANDOFF_EMPTY" in err and "[GT_META]" in err


def test_empty_handoff_verdict_is_cached_and_reraises(monkeypatch, tmp_path):
    """The verdict is cached per path: once a handoff is judged empty, every later call
    re-raises (the channel can never silently recover into the half-blind state)."""
    empty = tmp_path / "graph.db"
    empty.write_bytes(b"")
    monkeypatch.setenv("GT_PROOF_MODE", "1")
    monkeypatch.setenv("GT_HOST_GRAPH_DB", str(empty))
    with pytest.raises(g.GTHandoffEmptyError):
        g._db_path()
    assert g._handoff_guard_state.get(str(empty)) is False
    # second call still raises (from cache)
    with pytest.raises(g.GTHandoffEmptyError):
        g._db_path()


# ──────────────── 3. legitimately-ABSENT graph -> correct-or-QUIET ────────────────

def test_absent_handoff_is_quiet_not_raise(monkeypatch, tmp_path):
    """A handoff path that names a NON-EXISTENT file is 'absent' (the substrate handed
    us nothing) — correct-or-quiet, NEVER a raise. The producers then go silent on the
    missing file via their own os.path.isfile check."""
    missing = str(tmp_path / "does_not_exist.db")
    assert not os.path.exists(missing)
    monkeypatch.setenv("GT_PROOF_MODE", "1")
    monkeypatch.setenv("GT_PORTABLE_SUBSTRATE", "1")
    monkeypatch.setenv("GT_HOST_GRAPH_DB", missing)
    # No raise — _db_path returns the (absent) path; the producer stays quiet.
    assert g._db_path() == missing
    assert g._evidence_body("post_view", "some/file.py", "/root") == ""
    assert g._graph_contract_block("some/file.py") == ""


def test_legacy_mode_empty_db_is_quiet(monkeypatch, tmp_path):
    """OUTSIDE substrate/proof mode (legacy /tmp path, OURS, L6 may rewrite it) a
    transient empty db is NOT a handoff bug — the guard is a no-op there (quiet)."""
    empty = tmp_path / "graph.db"
    empty.write_bytes(b"")
    # No substrate/proof env set -> legacy path.
    monkeypatch.setenv("GT_GRAPH_DB", str(empty))
    # _guard_handoff_db is a no-op outside substrate/proof; no raise.
    g._guard_handoff_db(str(empty))  # must not raise
    # _connect_ro on the legacy empty db must NOT raise (correct-or-quiet). sqlite treats
    # a 0-byte file as a valid empty db, so it may return a (functionally empty) handle —
    # the producers then find no rows and stay silent. The contract here is "no raise",
    # NOT a particular return value (legacy mode never fail-closes).
    con = g._connect_ro(str(empty))  # must not raise
    if con is not None:
        # Functionally empty: a node query returns nothing -> producers stay quiet.
        try:
            cur = con.execute("SELECT name FROM sqlite_master")
            assert cur.fetchall() == []
        finally:
            con.close()


def test_unset_handoff_is_quiet(monkeypatch):
    """No GT_HOST_GRAPH_DB at all in substrate mode -> empty path, no raise (degraded
    mode is correct-or-quiet; the guard only fires on a PRESENT-but-empty file)."""
    monkeypatch.setenv("GT_PROOF_MODE", "1")
    monkeypatch.setenv("GT_PORTABLE_SUBSTRATE", "1")
    # GT_HOST_GRAPH_DB unset; GT_GRAPH_DB unset -> _db_path returns "" without raising.
    assert g._db_path() == ""


# ───────────────────────── 4. A1 stub regression (confirm) ─────────────────────────

def test_a1_stub_has_init():
    """A1 (already fixed in HEAD): the in-container fallback stubs carry __init__ so the
    pre-CP011 fallback path never TypeErrors. Confirm the load-bearing stubs construct."""
    # These construct only when the real groundtruth.runtime import failed (stub path).
    # We assert the classes are constructible with the kwargs the real call sites pass.
    for cls in (g._ProductHorizonThresholds, g._ProductTrajectoryState,
                g._ProductLedgerEntry, g._ProductContextBudgeter):
        try:
            obj = cls(some_kw=1)  # type: ignore[call-arg]
        except TypeError:
            # Real (non-stub) classes may reject unknown kwargs — that's fine; the point
            # is the STUB path has __init__. If the real import succeeded, skip.
            if not g._RUNTIME_AVAILABLE:
                raise
        else:
            assert obj is not None
