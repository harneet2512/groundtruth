"""A live PID is not proof that the run-lock's owner still exists.

MEASURED 2026-07-28, not hypothesised.  ``/tmp/ssr_replay_oracle.lock`` records a BARE
PID.  Both readers -- ``ss_replay_oracle.acquire_run_lock`` and
``ss_gate._oracle_lock_holder`` -- asked only ``_pid_alive(pid)``.  PIDs are recycled,
aggressively so on Windows.

What actually happened: a lock written at 19:15:38 by an oracle that was later killed
named pid 46120.  By 20:39 that pid belonged to an unrelated ``full_suite_sweep.py``.
``ss_gate`` aborted with::

    [ss_gate] ABORT: run-lock held by oracle run 46120 (\\tmp\\ssr_replay_oracle.lock).

and would have kept aborting indefinitely -- a dead oracle permanently locking out the
gate, while the abort message instructs the operator *never to delete the lock*.  The
failure is silent in the worst way: it looks exactly like correct mutual exclusion.

THE FIX.  Ask whether the holder IS an oracle, not whether something with that number
exists.  ``_pid_holds_oracle`` fails safe in BOTH directions, which is the property
these tests pin:

  * identity unobtainable  -> treated as HELD.  Never steal a lock on a guess; the
    conservative "never run concurrently" default is preserved.
  * identity positively NOT an oracle -> treated as FREE.  Never block forever on a
    recycled pid.

Only the second is new behaviour; the first is what the old code did for every case.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "swebench"))

import ss_replay_oracle as o  # noqa: E402


_ORACLE_CMD = (
    r"C:\Python312\python.exe -u scripts/swebench/ss_replay_oracle.py --tasks 29"
)
_NOT_ORACLE_CMD = (
    r"C:\Python312\python.exe -u scripts/swebench/full_suite_sweep.py --save out.json"
)


def test_a_recycled_pid_running_something_else_does_NOT_hold_the_lock(monkeypatch):
    """THE BUG. This is the exact observed case: pid alive, but it is a sweep."""
    monkeypatch.setattr(o, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(o, "_pid_cmdline", lambda pid: _NOT_ORACLE_CMD)
    assert o._pid_holds_oracle(46120) is False


def test_a_live_oracle_still_holds_the_lock(monkeypatch):
    """NEAR-NEGATIVE. The mutual exclusion this lock exists for must be untouched."""
    monkeypatch.setattr(o, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(o, "_pid_cmdline", lambda pid: _ORACLE_CMD)
    assert o._pid_holds_oracle(1234) is True


def test_unobtainable_identity_is_treated_as_HELD(monkeypatch):
    """FAIL-SAFE. Never steal a lock because we could not read a command line.

    This is the direction that protects the drive-global mirrors: a wrong answer
    here means two runs cross-contaminate \\testbed / \\gt_artifacts / \\opt\\gt.
    """
    monkeypatch.setattr(o, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(o, "_pid_cmdline", lambda pid: None)
    assert o._pid_holds_oracle(1234) is True


def test_a_dead_pid_never_holds_the_lock(monkeypatch):
    """Liveness is still necessary -- it merely stopped being sufficient."""
    monkeypatch.setattr(o, "_pid_alive", lambda pid: False)
    monkeypatch.setattr(o, "_pid_cmdline", lambda pid: _ORACLE_CMD)
    assert o._pid_holds_oracle(1234) is False


def test_the_gate_reader_uses_the_same_identity_rule(tmp_path, monkeypatch):
    """INTEGRATION. ss_gate must not keep its own weaker rule.

    The two readers previously each had their own ``_pid_alive`` and no shared
    notion of ownership, which is how they could disagree about the same file.
    """
    ss_gate = pytest.importorskip("ss_gate")
    lock = tmp_path / "ssr_replay_oracle.lock"
    lock.write_text("46120", encoding="utf-8")
    monkeypatch.setattr(ss_gate, "_ORACLE_RUN_LOCK", lock)
    monkeypatch.setattr(o, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(o, "_pid_cmdline", lambda pid: _NOT_ORACLE_CMD)
    assert ss_gate._oracle_lock_holder() is None, (
        "the gate still treats a recycled pid as an oracle and will abort forever"
    )
