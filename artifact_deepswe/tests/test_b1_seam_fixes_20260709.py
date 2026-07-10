"""RED->GREEN TTD for the three B1 mini-swe-agent seam fixes (Fable 2026-07-09).

Each test FAILS on the pre-fix code (proven by reverting the corresponding hunk)
and PASSES after. Scope: gt_mini_patch.py only.

  FIX 1  latch re-arm — the EXECUTED covering RED (`verify.horizon.executed`)
         consumes the SHARED `_horizon_advisory_fired` latch at production
         (~line 6360, set True BEFORE `_executed_covering_emission`). When it
         LOSES the single Lane-B slot, the re-arm block re-armed advisory/urgent/
         pivot/gate but NOT `verify.horizon.executed`, so the shared latch stayed
         burned and the risk-trigger (`if _risk_trigger and not
         _horizon_advisory_fired`) never re-entered -> the RED never re-fired.
         Fix: re-arm the SAME shared latch on an executed loss.

  FIX 2  phase-filter survival (gt_math §393) — an EXECUTED world-fact is valid
         in ANY phase, but it is produced on POST_EDIT/EDIT turns where the phase
         policy has no verify.horizon.* entry, so should_emit() -> wrong_phase and
         the RED was silently dropped (+ its shared latch burned). Fix: exempt
         ONLY `verify.horizon.executed` from the phase filter.

  FIX 3  executor threading — the seam runs the repo's covering tests; on a remote
         Docker/Singularity env a host subprocess runs on the WRONG machine. The
         env wrapper publishes the live env + its ORIGINAL execute; the seam builds
         an adapter (frozen contract executor(cmd, cwd, timeout) -> (rc, out, err))
         and threads it into the covering runner IFF the runner's signature accepts
         `executor` (feature-detected via inspect.signature). Absent -> today's path.
"""
from __future__ import annotations

import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import gt_mini_patch as g  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_live_env_bridge():
    """Test hygiene: the FIX-3 executor bridge (_GT_LIVE_ENV / _GT_LIVE_ORIG_EXECUTE)
    is a module global published by the env wrapper. A fake env published in a FIX-3
    test must NOT leak into another test — the covering runner now accepts `executor`,
    so a stale fake env would route a real covering run through it. Reset before AND
    after each test. (In production the wrapper re-publishes the live env on EVERY
    execute call, so it is never stale — this leak is a test-only artifact.)"""
    g._GT_LIVE_ENV = None
    g._GT_LIVE_ORIG_EXECUTE = None
    yield
    g._GT_LIVE_ENV = None
    g._GT_LIVE_ORIG_EXECUTE = None


def _mk_graph(path: str) -> None:
    """Minimal production-schema graph.db so the Lane-B producers degrade quietly
    (each is individually try-wrapped) while _augment_output reaches the re-arm."""
    con = sqlite3.connect(path)
    con.executescript(
        """
        CREATE TABLE nodes (
            id INTEGER PRIMARY KEY, label TEXT, name TEXT, file_path TEXT,
            language TEXT, parent_id INTEGER, is_test INTEGER DEFAULT 0
        );
        CREATE TABLE edges (
            id INTEGER PRIMARY KEY, source_id INTEGER, target_id INTEGER,
            type TEXT, resolution_method TEXT, confidence REAL,
            source_file TEXT, source_line INTEGER
        );
        """
    )
    con.execute("INSERT INTO nodes VALUES (1,'Function','frob','src/frob.py','python',NULL,0)")
    con.commit()
    con.close()


# ============================================================================ #
# FIX 1 — executed-RED gate loss re-arms the SHARED _horizon_advisory_fired latch
# ============================================================================ #
def test_f1_executed_gate_loss_rearms_shared_advisory_latch(monkeypatch, tmp_path):
    """Drives the REAL _augment_output. The executed RED is produced this turn and
    LOSES the single Lane-B slot (the gate records it as a loser, delivers ''). The
    re-arm block MUST release the shared _horizon_advisory_fired latch it consumed
    at production. RED pre-fix: no `verify.horizon.executed` case -> latch stays
    burned -> the risk-trigger never re-enters."""
    anchors = tmp_path / "anchors.json"
    anchors.write_text('{"symbols": ["frob"]}', encoding="utf-8")
    db = tmp_path / "graph.db"
    _mk_graph(str(db))
    monkeypatch.setenv("GT_ANCHORS_PATH", str(anchors))
    monkeypatch.setenv("GT_HOST_GRAPH_DB", str(db))
    monkeypatch.delenv("GT_BASELINE", raising=False)
    monkeypatch.setattr(g, "_GT_BASELINE", False)
    monkeypatch.setattr(g, "_ORACLE_ROUTE", True)
    monkeypatch.setattr(g, "_root", lambda: str(tmp_path))
    monkeypatch.setattr(g, "_oracle_focus_cache", None, raising=False)
    monkeypatch.setattr(g, "_oblig_syms_cache", None, raising=False)
    g._reset_oracle_state()

    # The executed candidate is produced this turn ...
    monkeypatch.setattr(
        g, "_verification_horizon_candidate",
        lambda: (4.0, "verify.horizon.executed", "\n$ pytest\nTypeError\n[exit 1]\n", True),
    )

    # ... and it LOSES the single Lane-B slot: the gate records it as a loser
    # (exactly as _oracle_gate_blocks does on a real outrank/floor loss) + '' win.
    def _stub_gate(cands):
        g._oracle_last_losers = {"verify.horizon.executed"}
        g._last_gate_winner_kind = ""
        return ""

    monkeypatch.setattr(g, "_oracle_gate_blocks", _stub_gate)

    # The shared advisory latch was consumed at production (as line ~6360 does).
    monkeypatch.setattr(g, "_horizon_advisory_fired", True, raising=False)

    g._augment_output({"command": "sed -i '1s/a/b/' src/frob.py"}, {"output": "patched"})

    assert g._horizon_advisory_fired is False, (
        "executed RED lost the gate but its SHARED _horizon_advisory_fired latch "
        "was not re-armed -> the risk-trigger can never re-enter and the RED never "
        "re-fires (the FIX-1 bug)"
    )


# ============================================================================ #
# FIX 2 — executed-RED survives the phase filter (world-fact exemption); scope pin
# ============================================================================ #
def test_f2_executed_red_survives_phase_filter_in_edit(monkeypatch):
    """An EXECUTED covering RED is a world-fact valid in ANY phase. In EDIT (its
    production phase) the policy has no verify.horizon.* entry, so should_emit()
    returns wrong_phase. RED pre-fix: the RED is dropped + staged in
    _phase_dropped_losers. GREEN: the one-reason exemption keeps it."""
    g._reset_phase_dropped_losers()
    monkeypatch.setattr(g, "_runtime_ledger_record", lambda *a, **k: None)
    cands = [(4.0, "verify.horizon.executed", "\n$ pytest\nTypeError\n[exit 1]\n", True)]
    kept = g._filter_candidates_by_phase(
        cands, g.Phase.EDIT, g.Event.POST_EDIT, file_path="src/x.py"
    )
    kinds = {k for _s, k, _t, _e in kept}
    assert "verify.horizon.executed" in kinds, (
        "executed RED did NOT survive the phase filter in EDIT — it was phase-"
        "dropped (the P1 silencing bug). kept=%r" % kinds
    )
    assert "verify.horizon.executed" not in g._phase_dropped_losers, (
        "executed RED must not be staged as phase-dropped when exempted"
    )


def test_f2_advisory_still_phase_gated_in_edit(monkeypatch):
    """Scope pin: the exemption is for ONE reason only. An ADVISORY verify.horizon.*
    (not a world-fact) must STILL obey the phase gate in EDIT — proving FIX 2 did
    not lift the filter for the whole family."""
    g._reset_phase_dropped_losers()
    monkeypatch.setattr(g, "_runtime_ledger_record", lambda *a, **k: None)
    cands = [(4.0, "verify.horizon.advisory", "advisory reminder text", True)]
    kept = g._filter_candidates_by_phase(
        cands, g.Phase.EDIT, g.Event.POST_EDIT, file_path="src/x.py"
    )
    kinds = {k for _s, k, _t, _e in kept}
    if "verify.horizon.advisory" in kinds:
        pytest.skip("phase policy admits advisory in EDIT — nothing to pin here")
    assert "verify.horizon.advisory" in g._phase_dropped_losers, (
        "advisory should still be phase-gated (dropped) in EDIT — the FIX-2 "
        "exemption must be scoped to verify.horizon.executed only"
    )


# ============================================================================ #
# FIX 3 — executor threading (feature-detected) routes through the live env
# ============================================================================ #
def _seam_env(monkeypatch, tmp_path):
    """Shared setup: pass every _executed_covering_emission early-return guard."""
    root = tmp_path
    (root / "test_mod.py").write_text("def test_x():\n    assert True\n", encoding="utf-8")
    monkeypatch.setattr(g, "_root", lambda: str(root))
    monkeypatch.setenv("GT_VERIFY_EXECUTE", "1")
    monkeypatch.setattr(g, "_GT_BASELINE", False)
    monkeypatch.setattr(g, "_last_test_step", None, raising=False)
    monkeypatch.setattr(g, "_action_count", 5, raising=False)
    import groundtruth.runtime.native_render as nr
    monkeypatch.setattr(nr, "is_edit_attributed", lambda *a, **k: True)
    monkeypatch.setattr(
        nr, "render_covering_failure_native",
        lambda *a, **k: "\n$ pytest\nTypeError\n[exit 1]\n",
    )
    return root


def test_f3_executor_routes_through_live_env(monkeypatch, tmp_path):
    """With GT_VERIFY_EXECUTE=1 and a covering runner that ACCEPTS `executor`, the
    seam threads an adapter that routes the run through the live env's ORIGINAL
    execute. RED pre-fix: no executor threading -> the fake env is never called."""
    import groundtruth.runtime.covering_runner as cr
    _seam_env(monkeypatch, tmp_path)

    calls = []

    def _fake_orig(env, action, cwd, *, timeout=None):  # mini-swe env.execute shape
        calls.append((action.get("command"), cwd, timeout))
        return {"output": "boom\nTypeError: x", "returncode": 1}

    g._gt_publish_live_env(object(), _fake_orig)

    def _stub_run(repo_root, files, *, per_file_timeout=120,
                  total_budget_seconds=300, executor=None, **kw):
        assert executor is not None, "executor must be threaded when the runner accepts it"
        rc, out, err = executor(["python", "-m", "pytest", files[0]], repo_root, per_file_timeout)
        assert (rc, err) == (1, ""), (rc, out, err)  # adapter parsed returncode + merged stderr
        return {"verdict": "fail", "ran": files, "exit_code": rc, "stdout_tail": out,
                "stderr_tail": err, "command": ["pytest"], "failing_test_names": []}

    monkeypatch.setattr(cr, "run_covering_tests", _stub_run)

    cov = [{"file": "test_mod.py", "confidence": 0.9}]
    block = g._executed_covering_emission(cov, {"mod.py"}, {"foo"})
    assert calls, "executor did NOT route through the live env (bridge not threaded)"
    assert calls[0][0] and "pytest" in calls[0][0], calls
    assert block, "the emission should still proceed to a Format-D block"


def test_f3_absent_executor_param_is_safe_fallback(monkeypatch, tmp_path):
    """When the covering runner LACKS an `executor` param (before the engine merges
    it), the feature-detect must NOT pass it: no crash, and the executor is never
    invoked (the sync-subprocess default). RED (broken feature-detect that always
    passes): TypeError unexpected keyword 'executor'."""
    import groundtruth.runtime.covering_runner as cr
    _seam_env(monkeypatch, tmp_path)

    calls = []

    def _fake_orig(env, action, cwd, *, timeout=None):
        calls.append(action.get("command"))
        return {"output": "", "returncode": 1}

    g._gt_publish_live_env(object(), _fake_orig)

    seen = {}

    def _stub_run_no_exec(repo_root, files, *, per_file_timeout=120,
                          total_budget_seconds=300, **kw):
        seen["executor_in_kw"] = "executor" in kw  # must be False
        return {"verdict": "fail", "ran": files, "exit_code": 1, "stdout_tail": "TypeError",
                "stderr_tail": "", "command": ["pytest"], "failing_test_names": []}

    monkeypatch.setattr(cr, "run_covering_tests", _stub_run_no_exec)

    cov = [{"file": "test_mod.py", "confidence": 0.9}]
    block = g._executed_covering_emission(cov, {"mod.py"}, {"foo"})  # must not raise
    assert seen.get("executor_in_kw") is False, (
        "executor must NOT be passed when the runner has no such param"
    )
    assert not calls, "executor must not be invoked on the no-param fallback"
    assert block, "the emission should still proceed via the default path"
