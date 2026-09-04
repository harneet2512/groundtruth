"""Oracle gate WRITER fires in the in-container (runtime-stub) shape.

TTD reproduction of the FAILED ARTIFACT (HEAD d29113cb, run that produced
``gt_oracle_events.jsonl`` with ``gt.oracle_event.v2 == 0`` on all 8 tasks while
the SEPARATE obligation SENSE sibling wrote 300-1062 records/task).

Observed failure chain (gt_gt.md §15.4 Stage-3 — the per-turn gate at the review
transition):
  - In the eval TASK CONTAINER ``groundtruth.runtime.*`` is NOT importable, so the
    shared import block (gt_mini_patch.py :56-69) falls back to inline stubs and
    ``_RUNTIME_AVAILABLE`` is False.
  - The runtime-FALLBACK stub ``_ProductHorizonThresholds`` (pre-fix: ``class … : pass``,
    no ctor) is then the LIVE class, but its call site
    ``verify_horizon_band(...)`` (the wrapper at :2998) constructs it with SIX kwargs
    (advisory_test_coverage / advisory_budget / urgent_test_coverage / urgent_budget /
    gate_test_coverage / gate_cycles).  -> ``TypeError: _ProductHorizonThresholds()
    takes no arguments``.
  - That TypeError is raised by the ``_verification_horizon_candidate()`` PRODUCER
    inside ``_augment_output`` and (pre-fix) propagates to the OUTER
    ``except Exception: pass`` BEFORE the gate ``_oracle_gate_blocks`` ever runs.
  - The gate's telemetry writer ``_oracle_telemetry_write`` (which emits the
    ``gt.oracle_event.v2`` record) therefore NEVER runs -> 0 v2 records (the silent
    WRITER the artifact proved).

This test reproduces the reader/writer contract by loading gt_mini_patch.py fresh
with the runtime imports POISONED (so ``_RUNTIME_AVAILABLE`` is False and the stub is
live — mirrors ``tests/test_b1_factfilter_single_source.py``), and proves:

  (1) UNIT — the active stub ``_ProductHorizonThresholds`` constructs with the 6
      kwargs WITHOUT raising (the ctor the call site needs).
  (2) BEHAVIOR (the artifact contract) — driving the per-turn path
      (GT_ORACLE_ROUTE=1, GT_STEP_LIMIT set, GT_ORACLE_EVENTS -> temp file, a real
      source edit + a persisted test failure) makes the gate WRITE >= 1
      ``gt.oracle_event.v2`` record to GT_ORACLE_EVENTS.  This is the writer the
      artifact proved was silent.

RED  (pre-fix): TypeError from the no-arg stub -> 0 v2 records  -> both asserts fail.
GREEN (post-fix): stub ctor accepts kwargs + the producer is individually guarded
      -> the gate runs -> >= 1 v2 record.
"""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GMP = ROOT / "artifact_deepswe" / "gt_mini_patch.py"


def _run_driver(
    body: str, env_events_name: str = "events.jsonl", *, tmp: Path, step_limit: str = "100"
) -> subprocess.CompletedProcess:
    """Load gt_mini_patch.py in a clean subprocess with the runtime modules
    POISONED (forces ``_RUNTIME_AVAILABLE`` False -> the stub is the live class —
    the in-container shape) and the oracle route armed, then run ``body``.

    The subprocess is the only reliable way to control the import-time globals
    (``_RUNTIME_AVAILABLE`` / ``_GT_STEP_LIMIT`` / ``_ORACLE_ROUTE`` /
    ``GT_ORACLE_EVENTS``), all of which gt_mini_patch reads at module import time.
    """
    events = tmp / env_events_name
    # Header at column 0 (no body interpolation — keeps dedent from breaking on the
    # already-dedented body block). The body is concatenated AFTER, also at column 0.
    header = textwrap.dedent(f"""\
        import sys, os, importlib.util

        # In-container shape: groundtruth.runtime.* not importable -> stub fallback.
        # Poison the FIRST import of the shared block so the whole block falls to
        # the inline stubs and _RUNTIME_AVAILABLE becomes False.
        for _m in (
            "groundtruth", "groundtruth.runtime",
            "groundtruth.runtime.action_translation",
            "groundtruth.runtime.context_budget",
            "groundtruth.runtime.ledger",
            "groundtruth.runtime.trajectory_state",
            "groundtruth.runtime.verification_horizon",
        ):
            sys.modules[_m] = None

        # Arm the per-turn oracle route BEFORE import (read at import time).
        os.environ["GT_ORACLE_ROUTE"] = "1"
        os.environ["GT_STEP_LIMIT"] = {step_limit!r}
        os.environ["GT_ORACLE_EVENTS"] = {str(events)!r}
        os.environ.pop("GT_BASELINE", None)
        os.environ.pop("GT_PROOF_MODE", None)

        spec = importlib.util.spec_from_file_location("gmp_oracle", {str(GMP)!r})
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)   # MUST NOT raise

        # The stub MUST be the live class (in-container shape) for this to be a
        # faithful reproduction.
        assert m._RUNTIME_AVAILABLE is False, "expected runtime stub fallback"
    """)
    script = header + "\n" + textwrap.dedent(body)
    return subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, cwd=str(ROOT)
    )


def test_stub_horizon_thresholds_constructs_with_six_kwargs():
    """UNIT: the active (in-container) stub _ProductHorizonThresholds accepts the
    exact 6 kwargs its call site (verify_horizon_band wrapper) constructs it with —
    pre-fix it was a no-arg ``class … : pass`` and this raised TypeError."""
    body = textwrap.dedent("""
        th = m._ProductHorizonThresholds(
            advisory_test_coverage=0.125,
            advisory_budget=0.11166666,
            urgent_test_coverage=0.125,
            urgent_budget=0.35333333,
            gate_test_coverage=1.0,
            gate_cycles=7.48,
        )
        # the kwargs are bound as attributes (the real dataclass shape)
        assert th.gate_cycles == 7.48
        # and _verification_horizon_candidate() must not RAISE (it constructs the
        # stub internally) — None is fine (dormant), a raise is the bug.
        m._verification_horizon_candidate()
        print("UNIT_CTOR_OK")
    """)
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        r = _run_driver(body, tmp=Path(d))
    assert "UNIT_CTOR_OK" in r.stdout, (
        f"stub ctor / producer raised\nstdout={r.stdout!r}\nstderr={r.stderr!r}"
    )


def test_oracle_gate_writes_v2_telemetry_in_container():
    """BEHAVIOR (the artifact contract): drive the per-turn _augment_output path in
    the in-container stub shape; the gate must WRITE >= 1 'gt.oracle_event.v2'
    record to GT_ORACLE_EVENTS.

    Drive: a real source edit (post_edit -> _source_edit_count, edited-set), then the
    SAME persisted test failure observed twice (the edit-bound l5.failure candidate —
    a non-graph, gate-surviving block). Every turn the verify.horizon producer also
    fires (GT_STEP_LIMIT set): pre-fix its TypeError kills _augment_output BEFORE the
    gate -> 0 v2 records; post-fix it is caught + the stub ctor works -> the gate runs
    and the winning candidate writes the v2 record.
    """
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        body = textwrap.dedent("""
            FAIL = ("============ FAILURES ============\\n"
                    "FAILED tests/test_app.py::test_x - assert 1 == 2\\n"
                    "1 failed in 0.10s")

            def drive(cmd, out_text=""):
                out = {"output": out_text}
                m._augment_output({"command": cmd}, out)
                return out

            # turn 1: a real source edit -> populates the edited-set + edit count
            drive("printf 'x = 1\\n' >> src/app.py")
            # turn 2 + 3: the SAME test failure persists across the edit (>=2 -> the
            # edit-bound l5.failure candidate fires, survives the gate as a winner)
            drive("pytest tests/test_app.py", FAIL)
            drive("pytest tests/test_app.py", FAIL)

            print("DRIVE_OK")
        """)
        r = _run_driver(body, tmp=tmp)
        assert "DRIVE_OK" in r.stdout, (
            f"drive did not complete\nstdout={r.stdout!r}\nstderr={r.stderr!r}"
        )

        events = tmp / "events.jsonl"
        v2 = []
        if events.exists():
            for line in events.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                rec = json.loads(line)
                if rec.get("schema") == "gt.oracle_event.v2":
                    v2.append(rec)

        assert len(v2) >= 1, (
            "the oracle gate WRITER was silent — 0 'gt.oracle_event.v2' records "
            "(reproduces the artifact: TypeError from the no-arg "
            "_ProductHorizonThresholds stub killed _augment_output before the gate).\n"
            f"events_file_exists={events.exists()} v2_count={len(v2)}\n"
            f"stdout={r.stdout!r}\nstderr={r.stderr!r}"
        )
