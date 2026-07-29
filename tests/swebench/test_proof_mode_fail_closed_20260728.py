"""A dark canonical observer must be able to FAIL CLOSED, and say so in the artifact.

THE GAP.  `_augment_output` (`artifact_deepswe/gt_mini_patch.py`) probes
`_canonical_observer_is_dark` and, when the observer is dark, writes ONE host-side ledger row
(`kind="canonical_runtime.dark_fallback"`, `outcome="suppressed_internal_only"`,
`reason="canonical_observer_dark:legacy_delivery_resumed"`) and then resumes LEGACY delivery by
calling `_augment_output_legacy(action, out)`, which mutates `out["output"]`.

That is the right default for a PAID run: a dead timing authority should cost GT its timing, not
its evidence (`tests/runtime/test_dark_runtime_legacy_fallback_20260727.py` pins exactly that, and
this file must not weaken it).  It is the WRONG default for a PROOF run, and on live run
30390877219 the fallback fired 59x / 38x / 12x / 6x across four tasks.  Two things are missing:

  1.  The bytes the model saw on those observations came from the untimed route, but no artifact
      field says the canonical claim for that task is void.  `_canonical_observer_is_dark` reads
      only `gt_emission_enabled` and `isolated_components`; it never touches `AssuranceStatus`,
      which `_quarantine` (`reasoning_runtime.py:3498-3515`) has ALREADY set to UNASSURED.
  2.  Neither grader knows the row exists.  `scripts/swebench/gt_feature_metrics.py` and
      `scripts/swebench/live_evidence.py` contain no reference to `dark_fallback` at all.  They
      happen to read only DELIVERED rows, so today they report nothing rather than something
      false -- but that safety is ACCIDENTAL.  Any future numerator that counts a suppressed or
      internal row inherits a canonical-delivery claim for a task whose observer was dead.

THE CONTRACT PINNED HERE.  An env flag `GT_CANONICAL_PROOF_MODE`, DEFAULT OFF.  Off: byte-
identical to today (test 1, which passes today and must keep passing -- it is what makes the
change safe to land on the paid path).  On, and dark: emit the row and RETURN without touching
`out`, so the model sees its native bytes and nothing else; the row carries the assurance READ
FROM the runtime's own failure state; and both graders treat any such row as a hard precondition
forcing `canonical_assurance = "UNASSURED"` with the task excluded from every canonical-delivery
numerator but retained in the denominator.

WHY TEST 5 EXISTS (anti-fabrication).  `"UNASSURED"` is the assurance of a QUARANTINED runtime,
so a hardcoded literal would satisfy test 3 while proving nothing.  An observer isolated WITHOUT
quarantine is dark too (`_canonical_observer_is_dark` second clause) and its assurance is
DEGRADED, not UNASSURED -- `apply_failure_policy` on a non-core fault sets
`assurance=AssuranceStatus.DEGRADED` and leaves `gt_emission_enabled` True
(`reasoning_runtime.py:3530-3539`).  Test 5 constructs that state and demands the row report
DEGRADED.  A hardcoded string passes test 3 and fails test 5.

WHAT IS *NOT* ASSERTED.  Nothing here claims the fallback is rare, or that proof mode should be
the default.  Flipping the default is a separate decision with a paid-run cost.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
for _p in (str(_ROOT), str(_ROOT / "src"), str(_ROOT / "scripts" / "swebench")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from artifact_deepswe import gt_mini_patch as seam  # noqa: E402
from groundtruth.runtime import reasoning_runtime as rr  # noqa: E402
from groundtruth.runtime.reasoning_runtime import (  # noqa: E402
    AssuranceStatus,
    FailurePolicyState,
    FaultCode,
    RuntimeFault,
    RuntimeHealthState,
)

PROOF_MODE_ENV = "GT_CANONICAL_PROOF_MODE"
DARK_ROW_KIND = "canonical_runtime.dark_fallback"
LEGACY_REASON = "canonical_observer_dark:legacy_delivery_resumed"
PROOF_REASON = "canonical_observer_dark:proof_mode_fail_closed"
NATIVE = "native command bytes\nsecond line\n"


# --------------------------------------------------------------------------------------
# Attachment fixtures.  The failure states below are REAL `FailurePolicyState` values --
# not hand-rolled stand-ins -- so `assurance` cannot drift away from what the runtime sets.
# The attachment shape (attempt_runtime.failure_state) is copied from
# `tests/runtime/test_dark_runtime_legacy_fallback_20260727.py::_attachment`, which is the
# established way to force an observer dark.
# --------------------------------------------------------------------------------------


class _Runtime:
    def __init__(self, state):
        self.failure_state = state


def _quarantined_state() -> FailurePolicyState:
    """Dark by clause 1: `gt_emission_enabled` False.  Assurance is UNASSURED."""
    return rr._quarantine(
        FailurePolicyState.initial(attempt_id="attempt-proof-mode"),
        RuntimeFault(
            code=FaultCode.STATE_HASH_MISMATCH,
            component="canonical_observer",
            signature="sig-quarantine",
            event_id="ev-1",
        ),
        attempted_signatures=("sig-quarantine",),
    )


def _isolated_degraded_state() -> FailurePolicyState:
    """Dark by clause 2: the observer is ISOLATED but the attempt is not quarantined.

    Built through the real policy so the DEGRADED assurance is the runtime's verdict, not the
    test's assertion.  `EVIDENCE_PRODUCER_FAILED` is not in `CORE_CORRUPTION_CODES`, so
    `apply_failure_policy` takes the isolation branch and never calls `recover`.
    """
    state = rr.apply_failure_policy(
        FailurePolicyState.initial(attempt_id="attempt-proof-mode"),
        RuntimeFault(
            code=FaultCode.EVIDENCE_PRODUCER_FAILED,
            component="canonical_observer",
            signature="sig-isolate",
            event_id="ev-2",
        ),
        recovery_input=None,
        recover=None,
    )
    assert state.health is RuntimeHealthState.DEGRADED
    assert "canonical_observer" in state.isolated_components
    return state


def _attachment(state):
    class _Attachment:
        def __init__(self):
            self.attempt_runtime = _Runtime(state)
            self.observed: list = []

        def observe_action_result(self, action, out):
            self.observed.append((action, out))

    return _Attachment()


@pytest.fixture()
def seam_env(monkeypatch):
    """Install a dark attachment + spies, and hand back the recorded route/ledger.

    `_augment_output_legacy` is SPIED, not replaced: the spy delegates to the real function so
    the byte assertions below are about real behaviour rather than about a stub the test wrote.
    """

    class Harness:
        def __init__(self):
            self.legacy_calls: list = []
            self.rows: list[dict] = []

        def install(self, state, *, proof_mode: bool):
            real_legacy = seam._augment_output_legacy

            def spy(action, out):
                self.legacy_calls.append(action)
                return real_legacy(action, out)

            monkeypatch.setattr(seam, "_augment_output_legacy", spy)
            monkeypatch.setattr(
                seam,
                "_runtime_ledger_record",
                lambda **kw: bool(self.rows.append(kw)) or True,
            )
            monkeypatch.setattr(
                seam, "_CANONICAL_RUNTIME_ATTACHMENT", _attachment(state), raising=False
            )
            if proof_mode:
                monkeypatch.setenv(PROOF_MODE_ENV, "1")
            else:
                monkeypatch.delenv(PROOF_MODE_ENV, raising=False)

        def dark_rows(self) -> list[dict]:
            return [r for r in self.rows if r.get("kind") == DARK_ROW_KIND]

        def run(self, action="grep -rn foo src/"):
            out = {"output": NATIVE, "returncode": 0}
            seam._augment_output(action, out)
            return out

    return Harness()


# --------------------------------------------------------------------------------------
# 1.  DEFAULT OFF -- byte identity with today.  PASSES TODAY and must keep passing.
# --------------------------------------------------------------------------------------


def test_1_default_off_is_byte_identical_to_todays_fallback(seam_env):
    """The regression guard for the PAID path.

    With the flag unset, a dark observer must still resume legacy delivery and must still record
    the legacy reason string verbatim.  The expected bytes are produced by running the REAL
    `_augment_output_legacy` on an identical input, so this stays a true byte-identity check even
    if the legacy producers start appending (they append nothing in a hermetic process, which is
    precisely why the route assertion carries the weight -- see test 2's docstring).
    """
    expected = {"output": NATIVE, "returncode": 0}
    seam._augment_output_legacy("grep -rn foo src/", expected)

    seam_env.install(_quarantined_state(), proof_mode=False)
    out = seam_env.run()

    assert seam_env.legacy_calls, (
        "default-off no longer resumes legacy delivery -- a dark observer now ships ZERO GT "
        "bytes for every remaining observation on the paid path"
    )
    assert out["output"] == expected["output"], "default-off model bytes changed"
    rows = seam_env.dark_rows()
    assert len(rows) == 1, f"expected exactly one dark_fallback row, got {rows}"
    assert rows[0]["reason"] == LEGACY_REASON
    assert rows[0]["outcome"] == "suppressed_internal_only"
    assert rows[0].get("chars", 0) == 0, "a telemetry row must ship no bytes"


# --------------------------------------------------------------------------------------
# 2.  PROOF MODE (a) -- the native observation is preserved untouched.
# --------------------------------------------------------------------------------------


def test_2_proof_mode_leaves_the_native_observation_untouched(seam_env):
    """RED TODAY.  In proof mode a dark observer must produce NO model-facing bytes.

    Two assertions, deliberately:
      * the byte contract -- `out` is identical to the native input;
      * the MECHANISM -- `_augment_output_legacy` is never entered.
    The second is load-bearing.  In a hermetic process the legacy producers happen to append
    nothing, so the byte assertion alone would pass today by accident; the route assertion fails
    deterministically because today's code calls the legacy path unconditionally when dark.
    """
    seam_env.install(_quarantined_state(), proof_mode=True)
    native = {"output": NATIVE, "returncode": 0}
    out = seam_env.run()

    assert seam_env.legacy_calls == [], (
        "proof mode re-entered the legacy delivery route -- the run can still emit untimed "
        "bytes while claiming canonical delivery"
    )
    assert out == native, f"proof mode mutated the native observation: {out!r}"


# --------------------------------------------------------------------------------------
# 3.  PROOF MODE (b) -- exactly one row, proof reason, POPULATED assurance.
# --------------------------------------------------------------------------------------


def test_3_proof_mode_row_carries_the_runtime_assurance(seam_env):
    """RED TODAY.  The row exists but carries no assurance and the wrong reason."""
    state = _quarantined_state()
    seam_env.install(state, proof_mode=True)
    seam_env.run()

    rows = seam_env.dark_rows()
    assert len(rows) == 1, f"expected exactly one dark_fallback row, got {rows}"
    row = rows[0]
    assert row["reason"] == PROOF_REASON, (
        f"proof-mode row must be distinguishable from the legacy-resume row: {row['reason']!r}"
    )
    assert row["outcome"] == "suppressed_internal_only"
    assert row.get("chars", 0) == 0
    extra = row.get("extra") or {}
    assert extra.get("assurance"), (
        f"no assurance on the fail-closed row -- the artifact cannot say the canonical claim is "
        f"void: {row!r}"
    )
    assert extra["assurance"] == state.assurance.value
    assert extra.get("proof_mode") is True
    assert extra.get("canonical_delivery_claimed") is False
    assert extra.get("native_output_preserved") is True
    assert "isolated_components" in extra
    assert "quarantine_reason" in extra
    assert "fault_code" in extra


# --------------------------------------------------------------------------------------
# 4.  PROOF MODE (c) -- the grader must fail closed on that row.
# --------------------------------------------------------------------------------------


def _ledger_line(row_kwargs: dict) -> dict:
    """Serialize captured `_runtime_ledger_record` kwargs the way the durable sink does.

    Mirrors `gt_mini_patch._runtime_ledger_record` (`:9951-9978`): canonical schema fields first,
    then `extra` merged with `setdefault` so a side-car key can never clobber one.
    """
    line = {
        "layer": row_kwargs.get("kind", ""),
        "event_type": "",
        "file_path": "",
        "outcome": row_kwargs.get("outcome", ""),
        "reason": row_kwargs.get("reason", ""),
        "chars_delivered": row_kwargs.get("chars", 0),
        "iteration": 7,
    }
    for key, value in (row_kwargs.get("extra") or {}).items():
        line.setdefault(key, value)
    return line


def test_4_gt_feature_metrics_fails_closed_on_a_dark_fallback_row(seam_env, tmp_path):
    """RED TODAY.  `collect_task` has no `canonical_assurance` field at all.

    The ledger fed to the grader is the row the SEAM actually emitted, not one the test invented,
    so this cannot pass by grading a fixture that production never writes.
    """
    seam_env.install(_quarantined_state(), proof_mode=True)
    seam_env.run()
    rows = seam_env.dark_rows()
    assert rows, "the seam emitted no dark_fallback row to grade"

    ledger = tmp_path / "gt_runtime_ledger_proofmode.jsonl"
    ledger.write_text(
        "".join(json.dumps(_ledger_line(r)) + "\n" for r in rows), encoding="utf-8"
    )

    import gt_feature_metrics as gfm

    record = gfm.collect_task("proof/mode-1", str(tmp_path))

    assert record.get("canonical_assurance") == "UNASSURED", (
        f"a task whose canonical observer went dark is not marked UNASSURED: "
        f"{record.get('canonical_assurance', '<ABSENT>')!r}"
    )
    attest = record["ss_integrity"].get("canonical_runtime_attestation") or {}
    assert attest.get("delivered_count", 0) == 0, (
        "a dark-observer task contributed to the canonical delivery numerator"
    )
    assert record["ss_integrity"].get("canonical_delivery_proven") is False, (
        "the task must be excluded from every 'canonical delivery proven' numerator while "
        "remaining in the denominator"
    )


def test_4b_absent_evidence_reads_UNKNOWN_never_ASSURED(tmp_path):
    """RED TODAY.  An OLD artifact with no `dark_fallback` evidence at all is UNKNOWN.

    The dangerous default is the silent one: a grader that omits the field lets a downstream
    reader supply `"ASSURED"`.  A legacy ledger with an ordinary row must therefore read
    `"UNKNOWN"` -- an explicit absence of proof, never proof of absence.
    """
    ledger = tmp_path / "gt_runtime_ledger_legacy.jsonl"
    ledger.write_text(
        json.dumps(
            {
                "layer": "l3.contract",
                "event_type": "",
                "file_path": "",
                "outcome": "delivered",
                "reason": "",
                "chars_delivered": 412,
                "iteration": 2,
            }
        )
        + "\n",
        encoding="utf-8",
    )

    import gt_feature_metrics as gfm

    record = gfm.collect_task("proof/legacy-1", str(tmp_path))
    assert record.get("canonical_assurance") == "UNKNOWN", (
        f"a legacy artifact carrying no canonical evidence reported "
        f"{record.get('canonical_assurance', '<ABSENT>')!r}; absent evidence must never read "
        f"as ASSURED"
    )


# --------------------------------------------------------------------------------------
# 5.  ANTI-FABRICATION -- the assurance is READ, not asserted.
# --------------------------------------------------------------------------------------


def test_5_assurance_is_read_from_the_runtime_not_hardcoded(seam_env):
    """RED TODAY, and RED for a hardcoded `"UNASSURED"` too.

    An isolated-but-not-quarantined observer is dark (clause 2 of `_canonical_observer_is_dark`)
    and its assurance is DEGRADED.  If the row reports UNASSURED here, the field is a literal and
    the artifact is fabricating the runtime's own verdict.
    """
    state = _isolated_degraded_state()
    assert state.assurance is AssuranceStatus.DEGRADED, (
        "precondition drifted: the isolation branch no longer yields DEGRADED"
    )
    assert seam._canonical_observer_is_dark(_attachment(state)), (
        "precondition drifted: an isolated canonical_observer is no longer dark"
    )

    seam_env.install(state, proof_mode=True)
    seam_env.run()

    rows = seam_env.dark_rows()
    assert len(rows) == 1, f"expected exactly one dark_fallback row, got {rows}"
    extra = rows[0].get("extra") or {}
    assert extra.get("assurance") == "DEGRADED", (
        f"row reported {extra.get('assurance', '<ABSENT>')!r} for a DEGRADED runtime -- the "
        f"assurance is being asserted, not read from attachment.attempt_runtime.failure_state"
    )


# test_5b_neither_grader_reads_dark_fallback_today was DELETED on 2026-07-28, per its own
# instruction, when the precondition landed in `gt_feature_metrics._apply_canonical_runtime_
# attestation` and `live_evidence.validate_live_evidence`. It was the CALIBRATION for tests
# 4/4b -- it asserted that no precondition existed anywhere, so that a green 4/4b could only
# come from new grader code and never from a fixture that happened to satisfy an old one. It
# had done that job by the time it went red; weakening it instead of deleting it would have
# left a test that asserts the fix is absent.
