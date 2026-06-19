"""HYBRID LANE-SPLIT — data-plane / control-plane bulkhead (Nygard, *Release It!*).

These tests DRIVE the REAL ``_augment_output`` oracle route (NO mocking of the
path under test) and prove the three contract guarantees of the lane split:

  (a) FAULT INJECTION — monkeypatch the oracle stateless decision
      (``_oracle_gate_blocks``) to RAISE.  Drive a real post-edit turn and
      assert the CONTRACT block STILL reached ``out['output']``.  This is the
      0/8 stub-crash failure mode: the old monolith delivered NOTHING when the
      gate died (the contract was computed + queued but never appended).  Under
      the lane split, Lane A (data plane) appended BEFORE Lane B (the gate) ran,
      so the control-plane crash loses ONLY the steer, never the data plane.

  (b) NO DOUBLE SHIP — identical content reaching the agent via Lane A and then
      Lane B in the SAME behavioral state is delivered EXACTLY ONCE (the shared
      ``_oracle_delivered_hashes`` content+state ledger dedups it).

  (c) NO FLOOD — a turn that triggers BOTH a context producer (Lane A) AND a
      steer (Lane B) delivers at most 1 context + 1 steer, not N of either.

GENERALIZED (not benchmaxx): the test graph is a tiny synthetic SQLite (no task
IDs / gold labels / repo-specific logic).  The edit shape (``sed -i`` on a repo
source file) is a universal POSIX edit.  The contract / co-change producers are
pure-SQL cross-language.
"""
from __future__ import annotations

import os
import re
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import gt_mini_patch as g  # noqa: E402


# ---------------------------------------------------------------------------
# Synthetic graph: an edited file with a function + a caller in a SECOND file
# (so the contract block has a real [CALLERS] fact) + a co-change sibling.
# ---------------------------------------------------------------------------
def _build_graph(path, nodes, edges=()):
    con = sqlite3.connect(path)
    con.execute(
        "CREATE TABLE nodes (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "label TEXT, name TEXT, qualified_name TEXT, file_path TEXT, "
        "start_line INTEGER, end_line INTEGER, signature TEXT, "
        "return_type TEXT, is_exported INTEGER DEFAULT 0, is_test INTEGER "
        "DEFAULT 0, language TEXT, parent_id INTEGER)"
    )
    con.execute(
        "CREATE TABLE edges (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "source_id INTEGER, target_id INTEGER, type TEXT, source_line INTEGER, "
        "source_file TEXT, resolution_method TEXT, confidence REAL DEFAULT 0.0, "
        "metadata TEXT)"
    )
    # the contract producer queries `properties` for the top function's
    # behavioural facts (guard_clause / return_shape) — the table must exist or
    # the producer's outer try swallows the missing-table error and returns "".
    con.execute(
        "CREATE TABLE properties (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "node_id INTEGER, kind TEXT, value TEXT)"
    )
    for nid, (name, fp, label, sl, el, sig, lang) in enumerate(nodes, start=1):
        con.execute(
            "INSERT INTO nodes (id, label, name, qualified_name, file_path, "
            "start_line, end_line, signature, return_type, is_exported, "
            "is_test, language, parent_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (nid, label, name, name, fp, sl, el, sig, "", 1, 0, lang, None),
        )
    for src, tgt, typ, method, conf in edges:
        con.execute(
            "INSERT INTO edges (source_id, target_id, type, resolution_method, "
            "confidence) VALUES (?,?,?,?,?)",
            (src, tgt, typ, method, conf),
        )
    con.commit()
    con.close()


def _tmpdb():
    """A graph where set_fields (node 1, src/importer.py) is CALLED by run
    (node 2, src/cli.py) via a high-confidence import edge -> the contract block
    renders a real [CALLERS] fact."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    _build_graph(
        path,
        nodes=[
            ("set_fields", "src/importer.py", "Function", 40, 55,
             "def set_fields(self, fields)", "python"),
            ("run", "src/cli.py", "Function", 10, 30,
             "def run(self)", "python"),
            ("sibling_fn", "src/importer.py", "Function", 60, 80,
             "def sibling_fn(self)", "python"),
        ],
        edges=[
            # cli.run() -> importer.set_fields()  (verified import, conf 1.0)
            (2, 1, "CALLS", "import", 1.0),
        ],
    )
    return path


def _write_anchors():
    """gt_issue_anchors.json naming set_fields as the focus symbol."""
    fd, path = tempfile.mkstemp(suffix=".json")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write('{"symbols": ["set_fields"], '
                '"obligations": [{"symbols": ["set_fields"]}]}')
    return path


def _write_root():
    fd, path = tempfile.mkstemp(suffix=".txt")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write("/")  # repo-relative paths pass through _to_repo_rel unchanged
    return path


class _WiredOracle:
    """Context manager: wires the full oracle env (graph + anchors + root +
    ORACLE route) and FULLY resets oracle/delivery state, then restores
    everything on exit.  Mirrors the proven _drive_post_edit_live harness."""

    def __init__(self, db, anchors, rootf):
        self.db, self.anchors, self.rootf = db, anchors, rootf

    def __enter__(self):
        self._saved_env = {k: os.environ.get(k) for k in
                           ("GT_HOST_GRAPH_DB", "GT_ANCHORS_PATH", "GT_BASELINE")}
        self._saved = (g._GT_BASELINE, g._ORACLE_ROUTE, g._oblig_syms_cache,
                       g._ROOT_FILE, g._oracle_focus_cache)
        os.environ["GT_HOST_GRAPH_DB"] = self.db
        os.environ["GT_ANCHORS_PATH"] = self.anchors
        os.environ.pop("GT_BASELINE", None)
        g._GT_BASELINE = False
        g._ORACLE_ROUTE = True
        g._ROOT_FILE = self.rootf
        g._oblig_syms_cache = None
        g._oracle_focus_cache = None  # force re-read of the anchors focus
        g._reset_oracle_state()
        return self

    def __exit__(self, *exc):
        (g._GT_BASELINE, g._ORACLE_ROUTE, g._oblig_syms_cache,
         g._ROOT_FILE, g._oracle_focus_cache) = self._saved
        for k, v in self._saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        return False


def _cleanup(*paths):
    for p in paths:
        try:
            os.unlink(p)
        except OSError:
            pass


# ===========================================================================
# (a) FAULT INJECTION — control-plane crash, data plane survives.
# ===========================================================================
def test_hybrid_fault_injection(monkeypatch):
    """Reproduces the 0/8 stub-crash: monkeypatch _oracle_gate_blocks (the Lane B
    stateless decider) to RAISE, drive a REAL post-edit turn, and assert the
    Lane A CONTRACT block STILL reached out['output'].  In the old monolith the
    sole delivery site was DOWNSTREAM of the gate, so a gate crash delivered
    nothing; the lane split appends Lane A first, so only the steer is lost."""
    db, anchors, rootf = _tmpdb(), _write_anchors(), _write_root()
    try:
        with _WiredOracle(db, anchors, rootf):
            # The control plane explodes — this is the stub-crash fault.
            def _boom(_cands):
                raise RuntimeError("oracle gate stub crash (fault injection)")
            monkeypatch.setattr(g, "_oracle_gate_blocks", _boom)

            out: dict = {"output": ""}
            # A real repo-source edit at set_fields' span -> post_edit -> Lane A
            # contract/cochange/evidence produced & delivered BEFORE the gate.
            g._augment_output(
                {"command": "sed -i '42s/old/new/' src/importer.py"}, out)

        delivered = out["output"]
        # DATA PLANE SURVIVED: the contract block reached the agent even though
        # the control-plane gate raised.
        assert "<gt-contract" in delivered, (
            "FAULT-INJECTION FAILED: the contract (Lane A data plane) did NOT "
            "reach the agent after the gate crashed — the 0/8 stub-crash mode "
            f"is NOT fixed. Delivered: {delivered!r}")
        # And it carries a real fact (the verified caller), not a placeholder.
        assert "set_fields" in delivered
    finally:
        _cleanup(db, anchors, rootf)


def test_hybrid_fault_injection_phase_filter_crash(monkeypatch):
    """Same guarantee, different control-plane fault: crash the phase filter
    (also Lane B glue).  Lane A's contract must STILL be delivered."""
    db, anchors, rootf = _tmpdb(), _write_anchors(), _write_root()
    try:
        with _WiredOracle(db, anchors, rootf):
            def _boom(*a, **k):
                raise RuntimeError("phase filter crash (fault injection)")
            monkeypatch.setattr(g, "_filter_candidates_by_phase", _boom)

            out: dict = {"output": ""}
            g._augment_output(
                {"command": "sed -i '42s/old/new/' src/importer.py"}, out)

        assert "<gt-contract" in out["output"], (
            "phase-filter crash undid Lane A delivery — bulkhead leak")
    finally:
        _cleanup(db, anchors, rootf)


def test_hybrid_fault_injection_negative_control(monkeypatch):
    """NON-VACUITY of the bulkhead (the regression guard the positive test lacks).
    NEUTER the data plane (_lane_a_deliver -> no-op) AND crash the control plane
    (_oracle_gate_blocks -> raise): the contract MUST NOT reach the agent. This
    reproduces the 0/8 stub-crash EXACTLY — without Lane A's early append, a gate
    crash loses everything. Paired with test_hybrid_fault_injection (Lane A intact
    -> contract SURVIVES), it proves the contract survives BECAUSE of the lane
    ordering, not incidentally: a silent revert of the ordering FAILS HERE, even
    though the positive test (gate not crashed) would still pass."""
    db, anchors, rootf = _tmpdb(), _write_anchors(), _write_root()
    try:
        with _WiredOracle(db, anchors, rootf):
            # data plane neutered: Lane A delivers NOTHING.
            monkeypatch.setattr(g, "_lane_a_deliver", lambda *a, **k: None)
            # control plane explodes: the stub-crash fault.
            def _boom(_cands):
                raise RuntimeError("oracle gate stub crash (fault injection)")
            monkeypatch.setattr(g, "_oracle_gate_blocks", _boom)

            out: dict = {"output": ""}
            g._augment_output(
                {"command": "sed -i '42s/old/new/' src/importer.py"}, out)

        # 0/8 MODE REPRODUCED: with no data plane and a dead control plane, the
        # agent gets nothing. If the contract appears here, the bulkhead proof is
        # VACUOUS (the contract is surviving via some path OTHER than Lane A).
        assert "<gt-contract" not in out["output"], (
            "NEGATIVE CONTROL FAILED: contract reached the agent with Lane A "
            "neutered + gate crashed — the fault-injection proof is vacuous. "
            f"Delivered: {out['output']!r}")
    finally:
        _cleanup(db, anchors, rootf)


# ===========================================================================
# (b) NO DOUBLE SHIP — Lane A then Lane B identical content -> one delivery.
# ===========================================================================
def test_hybrid_no_double_ship():
    """Identical content delivered by Lane A must NOT be re-shipped by Lane B in
    the SAME behavioral state.  We deliver a block through Lane A, then force the
    Lane B gate to 'win' the BYTE-IDENTICAL block: the shared content+state
    ledger (_oracle_delivered_hashes) suppresses the re-send -> the block appears
    exactly once in out['output']."""
    db, anchors, rootf = _tmpdb(), _write_anchors(), _write_root()
    try:
        with _WiredOracle(db, anchors, rootf):
            # 1) deliver a known block via the REAL Lane A path.
            block = '\n<gt-contract file="src/importer.py">[SIGNATURE] x</gt-contract>'
            out: dict = {"output": ""}
            g._lane_a_deliver(out, "cmd", [("l3.contract", block)],
                              krel="src/importer.py", event=None)
            assert out["output"].count(block) == 1, "Lane A did not deliver once"
            assert g._oracle_content_hash(block) in g._oracle_delivered_hashes

            # 2) the SAME block competes in the Lane B gate (state unchanged).
            #    The gate's content-hash dedup sees it in _oracle_delivered_hashes
            #    -> suppresses -> returns '' -> nothing re-appended.
            cands = [(g._SEV_STUCK, "l5.stuck", block, True)]
            win = g._oracle_gate_blocks(cands)
            if win:
                out["output"] += win

            # EXACTLY ONE copy in the agent's view.
            assert out["output"].count(block) == 1, (
                f"DOUBLE-SHIP: block delivered "
                f"{out['output'].count(block)} times across both lanes")
    finally:
        _cleanup(db, anchors, rootf)


def test_hybrid_no_double_ship_lane_a_self_dedup():
    """Lane A's own correct-or-quiet gate: the SAME block enqueued twice in one
    Lane A pass delivers once (idempotent within the lane)."""
    db, anchors, rootf = _tmpdb(), _write_anchors(), _write_root()
    try:
        with _WiredOracle(db, anchors, rootf):
            block = "\n<gt-evidence>dup</gt-evidence>"
            out: dict = {"output": ""}
            g._lane_a_deliver(
                out, "cmd",
                [("l3b.evidence", block), ("l3b.evidence", block)],
                krel="src/x.py", event=None)
            assert out["output"].count(block) == 1
    finally:
        _cleanup(db, anchors, rootf)


# ===========================================================================
# (c) NO FLOOD — at most 1 context + 1 steer per turn.
# ===========================================================================
def _count_blocks(text: str) -> dict:
    """Count delivered GT block kinds in the agent-visible output."""
    return {
        "contract": len(re.findall(r"<gt-contract\b", text)),
        "evidence": len(re.findall(r"<gt-evidence\b", text)),
        "scope": len(re.findall(r"<gt-scope\b", text)),
        # steer blocks the oracle emits (l5/verify/obligation/detect) carry a
        # leading marker; count any <gt-*> that is NOT a Lane A context kind.
        "all_gt": len(re.findall(r"<gt-[a-z]", text)),
    }


def test_hybrid_no_flood():
    """A single turn that triggers a Lane A context producer (post_edit ->
    contract) MUST NOT emit N contracts.  Lane A delivers each context kind at
    most once (its one-shot latch + content-hash dedup), and Lane B's gate emits
    at most ONE steer.  Net: <=1 contract + <=1 cochange + <=1 evidence from
    Lane A, and <=1 steer from Lane B — never a flood."""
    db, anchors, rootf = _tmpdb(), _write_anchors(), _write_root()
    try:
        with _WiredOracle(db, anchors, rootf):
            out: dict = {"output": ""}
            g._augment_output(
                {"command": "sed -i '42s/old/new/' src/importer.py"}, out)
            counts = _count_blocks(out["output"])
            # Lane A context: at most one of each kind this turn.
            assert counts["contract"] <= 1, f"flood: {counts['contract']} contracts"
            assert counts["evidence"] <= 1, f"flood: {counts['evidence']} evidence"
            # The whole turn produced a BOUNDED number of GT blocks — the data
            # plane (<=3: contract+cochange+evidence) plus at most one steer.
            assert counts["all_gt"] <= 4, (
                f"FLOOD: {counts['all_gt']} GT blocks in one turn "
                f"(expected <=3 context + <=1 steer): {out['output']!r}")
            # And the contract DID reach the agent (the data plane is alive).
            assert counts["contract"] == 1, (
                "expected the post-edit contract to deliver exactly once")
    finally:
        _cleanup(db, anchors, rootf)


def test_hybrid_lane_b_single_steer():
    """Lane B gate emits AT MOST ONE steer even when many steer candidates pass:
    the <=1 emission/turn rank/budget rule.  Drive the gate directly with two
    distinct, focus-relevant steer blocks -> exactly one is returned."""
    db, anchors, rootf = _tmpdb(), _write_anchors(), _write_root()
    try:
        with _WiredOracle(db, anchors, rootf):
            s1 = "\n[STEER-A] set_fields verify before ship"
            s2 = "\n[STEER-B] set_fields review the scope"
            cands = [
                (g._SEV_STUCK, "l5.stuck", s1, True),
                (g._SEV_NUDGE_VERIFY, "l5.no_test", s2, True),
            ]
            win = g._oracle_gate_blocks(cands)
            # exactly one of the two distinct steers wins; the other is deferred.
            assert win in (s1, s2)
            assert not (s1 in win and s2 in win)
    finally:
        _cleanup(db, anchors, rootf)


if __name__ == "__main__":
    import types

    class _MP:
        """Minimal monkeypatch shim for the python -c / __main__ driver."""

        def __init__(self):
            self._undo = []

        def setattr(self, obj, name, val):
            self._undo.append((obj, name, getattr(obj, name)))
            setattr(obj, name, val)

        def undo(self):
            for obj, name, old in reversed(self._undo):
                setattr(obj, name, old)
            self._undo = []

    fns = [(k, v) for k, v in sorted(globals().items())
           if k.startswith("test_") and isinstance(v, types.FunctionType)]
    for name, fn in fns:
        mp = _MP()
        try:
            if "monkeypatch" in fn.__code__.co_varnames[:fn.__code__.co_argcount]:
                fn(mp)
            else:
                fn()
            print(f"PASS {name}")
        finally:
            mp.undo()
    print(f"ALL {len(fns)} HYBRID LANE-SPLIT TESTS PASSED")
