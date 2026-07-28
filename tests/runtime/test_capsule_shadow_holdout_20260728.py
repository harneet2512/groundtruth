"""#30 step 3 — capsule-level shadow holdout: the arm that makes the state reachable.

The holdout compiles a capsule and then deliberately does NOT send it, so the coin's decision
becomes measurable. Everything here is about getting the RANDOMIZATION honest, because a
randomized arm whose recorded propensity does not match its actual draw is worse than no arm:
it produces confident numbers that are quietly biased.

TWO PROPERTIES CARRY THE WHOLE DESIGN:

1. SAFETY. `submit_refusal` and `syntax_result` are SAFETY_EXCLUDED — withholding "your patch is
   broken" would actively harm the agent. A capsule carries SEVERAL evidence records and
   withholding it withholds ALL of them, so ONE safety member means the WHOLE capsule ships.

2. PROPENSITY. The obvious composition — "HOLDOUT iff assign() says HOLDOUT for every member" —
   is conservative and WRONG. With rate=0.5 and three members, P(all HOLDOUT) is about 0.125,
   so the arm would record 0.5 while actually drawing 0.125 and every weighted estimate built
   on it would be biased. The capsule is ONE dose, so it gets ONE draw keyed on capsule_hash;
   participation is checked for all members FIRST, so the class argument only gates
   participation, which is already established.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

from groundtruth.runtime import miniswe_provider_boundary as mpb
from groundtruth.runtime.shadow_holdout import DELIVER, HOLDOUT


def _capsule(*fact_classes: str, capsule_hash: str = "c" * 64) -> SimpleNamespace:
    return SimpleNamespace(
        capsule_hash=capsule_hash,
        capsule_text="CAPSULE",
        model_call_id="call-abc",
        observation_id="obs-1",
        evidence_ids=tuple(f"GT-E-{c}" for c in fact_classes),
        member_fact_classes=tuple(fact_classes),
    )


# --------------------------------------------------------------------------- #
# OFF BY DEFAULT. A knob that withholds evidence must never act on a default.
# --------------------------------------------------------------------------- #
def test_off_by_default_always_delivers(monkeypatch) -> None:
    monkeypatch.delenv("GT_SS_SHADOW", raising=False)
    monkeypatch.delenv("GT_SS_SHADOW_RATE", raising=False)
    assert mpb._capsule_holdout_verdict("task-1", _capsule("localization")) == DELIVER


def test_flag_on_but_zero_rate_still_delivers(monkeypatch) -> None:
    monkeypatch.setenv("GT_SS_SHADOW", "1")
    monkeypatch.setenv("GT_SS_SHADOW_RATE", "0")
    assert mpb._capsule_holdout_verdict("task-1", _capsule("localization")) == DELIVER


def test_rate_without_the_flag_still_delivers(monkeypatch) -> None:
    """Both the flag AND a rate are required — neither alone may withhold evidence."""
    monkeypatch.delenv("GT_SS_SHADOW", raising=False)
    monkeypatch.setenv("GT_SS_SHADOW_RATE", "1")
    assert mpb._capsule_holdout_verdict("task-1", _capsule("localization")) == DELIVER


# --------------------------------------------------------------------------- #
# SAFETY. One excluded member protects the entire capsule.
# --------------------------------------------------------------------------- #
def test_one_safety_member_ships_the_whole_capsule(monkeypatch) -> None:
    monkeypatch.setenv("GT_SS_SHADOW", "1")
    monkeypatch.setenv("GT_SS_SHADOW_RATE", "1")
    # Alone, an advisory class at rate 1 is held out...
    assert mpb._capsule_holdout_verdict("task-1", _capsule("localization")) == HOLDOUT
    # ...but pairing it with a SAFETY class must ship BOTH, because withholding the capsule
    # would withhold the safety evidence too.
    assert (
        mpb._capsule_holdout_verdict(
            "task-1", _capsule("localization", "submit_refusal")
        )
        == DELIVER
    )
    assert (
        mpb._capsule_holdout_verdict(
            "task-1", _capsule("localization", "syntax_result")
        )
        == DELIVER
    )


def test_unknown_member_class_ships_the_capsule(monkeypatch) -> None:
    """An unresolvable class is NOT known to be advisory, so it is never withheld."""
    monkeypatch.setenv("GT_SS_SHADOW", "1")
    monkeypatch.setenv("GT_SS_SHADOW_RATE", "1")
    assert (
        mpb._capsule_holdout_verdict("task-1", _capsule("localization", "nonsuch"))
        == DELIVER
    )


def test_empty_capsule_delivers(monkeypatch) -> None:
    monkeypatch.setenv("GT_SS_SHADOW", "1")
    monkeypatch.setenv("GT_SS_SHADOW_RATE", "1")
    assert mpb._capsule_holdout_verdict("task-1", _capsule()) == DELIVER


# --------------------------------------------------------------------------- #
# PROPENSITY. ONE draw per capsule, keyed on capsule_hash.
# --------------------------------------------------------------------------- #
def test_verdict_is_deterministic_for_the_same_capsule(monkeypatch) -> None:
    monkeypatch.setenv("GT_SS_SHADOW", "1")
    monkeypatch.setenv("GT_SS_SHADOW_RATE", "0.5")
    cap = _capsule("localization", "caller_contract")
    first = mpb._capsule_holdout_verdict("task-1", cap)
    for _ in range(5):
        assert mpb._capsule_holdout_verdict("task-1", cap) == first


def test_member_count_does_not_shrink_the_holdout_share(monkeypatch) -> None:
    """THE PROPENSITY TRAP, pinned.

    An all-members-must-agree composition would make a 3-member capsule far less likely to be
    held out than a 1-member capsule at the same rate, while both recorded the same propensity.
    Here the draw is on the CAPSULE, so adding advisory members must not move the verdict.
    """
    monkeypatch.setenv("GT_SS_SHADOW", "1")
    monkeypatch.setenv("GT_SS_SHADOW_RATE", "0.5")
    for h in (f"{i:064x}" for i in range(60)):
        one = mpb._capsule_holdout_verdict(
            "task-1", _capsule("localization", capsule_hash=h)
        )
        many = mpb._capsule_holdout_verdict(
            "task-1",
            _capsule(
                "localization", "caller_contract", "def_partition", capsule_hash=h
            ),
        )
        assert one == many, (
            f"capsule {h[:8]} flipped when advisory members were added — the draw is "
            "leaking into per-member composition and the recorded propensity is a lie"
        )


def test_rate_one_holds_out_and_rate_zero_never_does(monkeypatch) -> None:
    monkeypatch.setenv("GT_SS_SHADOW", "1")
    cap = _capsule("localization")
    monkeypatch.setenv("GT_SS_SHADOW_RATE", "1")
    assert mpb._capsule_holdout_verdict("task-1", cap) == HOLDOUT
    monkeypatch.setenv("GT_SS_SHADOW_RATE", "0")
    assert mpb._capsule_holdout_verdict("task-1", cap) == DELIVER


# --------------------------------------------------------------------------- #
# THE ASSIGNMENT ROW — written on BOTH arms, or the propensity is unusable.
# --------------------------------------------------------------------------- #
def _row(tmp_path, rate: str, monkeypatch, *classes: str) -> dict | None:
    monkeypatch.setenv("GT_SS_SHADOW", "1")
    monkeypatch.setenv("GT_SS_SHADOW_RATE", rate)
    sink = tmp_path / "receipts.jsonl"
    boundary = SimpleNamespace(_receipt_sink_path=str(sink))
    mpb._record_shadow_assignment_row(
        boundary, "task-1", _capsule(*classes), HOLDOUT if rate == "1" else DELIVER
    )
    if not sink.exists():
        return None
    lines = [
        line
        for line in sink.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    return json.loads(lines[0]) if lines else None


def test_assignment_row_is_written_on_the_DELIVER_arm_too(
    tmp_path, monkeypatch
) -> None:
    """Recording only holdouts makes the propensity uncomputable: you cannot estimate a
    probability from the numerator alone."""
    row = _row(tmp_path, "0.5", monkeypatch, "localization")
    assert row is not None
    assert row["schema"] == "gt.shadow_assignment.v1"
    assert row["arm"] == DELIVER
    assert row["propensity"] == 0.5


def test_assignment_row_records_the_configured_rate_as_the_propensity(
    tmp_path, monkeypatch
) -> None:
    row = _row(tmp_path, "1", monkeypatch, "localization", "caller_contract")
    assert row is not None
    assert row["arm"] == HOLDOUT
    assert row["propensity"] == 1.0
    # The withheld render is accounted by SEAL, with zero model-facing bytes.
    assert row["withheld_capsule_hash"] == "c" * 64
    assert row["chars_delivered"] == 0
    assert row["outcome"] == "measurement_only"
