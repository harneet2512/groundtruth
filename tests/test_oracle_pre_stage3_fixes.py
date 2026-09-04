"""Oracle pre-Stage-3 corrections (red->green) — the 5 adversarial-debug fixes.

Per `ORACLE_RESEARCH_VERIFICATION.md` "Required before Stage 3" + the user
directive.  This file gates the CODE half of the 5 fixes:

  fix 3 — NO invented constants: the hardcoded `confidence=0.90` is gone;
          every candidate confidence is PROVENANCE-DERIVED (a pure function of
          the sensed trajectory evidence), and the confidence gate is a
          DISTRIBUTION-DERIVED floor (median + 1*MAD over the candidate pool's
          own scores — the gt_gt §15.2 mechanism, now implemented).
  fix 4 — spec.py's obligations are WIRED to their consumer: the oracle's
          candidate pool includes the spec-extracted obligations (loaded from
          the same gt_issue_anchors.json channel v1r_brief.py writes), with
          full 8-dp suppression telemetry even on silence (plan §1.4).
  fixes 1/2/5 (doc) — gt_gt.md §15 citation honesty (LitM recency-only,
          REAgent/CodeScout effect sizes detached from our mechanism,
          TRAJEVAL cited for the C1 channel) — asserted as doc contracts.

LEG 1: floors are functions of the data (no absolute thresholds, no task IDs,
no gold).  LEG 2: parity with the live governor is PRESERVED (zero behavior
drift on the frozen corpus) while the spec->oracle plumbing that Stage 3's
flip-lever candidates require becomes real.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_SENSE_PATH = _ROOT / "artifact_deepswe" / "gt_oracle_sense.py"
_ORACLE_PATH = _ROOT / "artifact_deepswe" / "gt_oracle.py"


def _load(path: Path, name: str):
    # GT_BASELINE=1 only WHILE loading (the module captures the flag at import);
    # restore afterwards so this suite never poisons later suites' env (the
    # pre-existing pollution broke test_deepswe_xlang_evidence in suite order).
    prev = os.environ.get("GT_BASELINE")
    os.environ["GT_BASELINE"] = "1"
    try:
        spec = importlib.util.spec_from_file_location(name, path)
        assert spec and spec.loader
        mod = importlib.util.module_from_spec(spec)
        sys.modules[name] = mod
        spec.loader.exec_module(mod)
        return mod
    finally:
        if prev is None:
            os.environ.pop("GT_BASELINE", None)
        else:
            os.environ["GT_BASELINE"] = prev


@pytest.fixture(scope="module")
def sense_mod():
    return _load(_SENSE_PATH, "gt_oracle_sense_ps3")


@pytest.fixture(scope="module")
def oracle_mod():
    return _load(_ORACLE_PATH, "gt_oracle_ps3")


def _turn(sense_mod, cmd: str, obs: str = ""):
    return sense_mod.Turn(index=0, command=cmd, raw_obs=obs, full_obs=obs)


# ---------------------------------------------------------------------------
# FIX 3a — the invented constant is gone from the source.
# ---------------------------------------------------------------------------
def test_no_invented_confidence_constant_in_source():
    src = _ORACLE_PATH.read_text(encoding="utf-8")
    assert "confidence=0.90" not in src, (
        "gt_oracle.py still ships the invented 0.90 constant the plan bans "
        "(§2.1: 'provenance-derived, never invented')"
    )


# ---------------------------------------------------------------------------
# FIX 3b — candidate confidence is provenance-derived (a pure function of the
# sensed evidence), not a constant.
# ---------------------------------------------------------------------------
def test_loop_confidence_is_window_saturation(sense_mod, oracle_mod):
    """loop confidence == count(sig in window) / len(window) — recomputed from
    the trajectory, 8dp-exact."""
    # 8 distinct commands then 4 identical (cmd, obs) pairs -> window of 12,
    # 4 occurrences of the repeated signature at the firing turn.
    turns = [_turn(sense_mod, f"ls dir{i}", f"out{i}") for i in range(8)]
    turns += [_turn(sense_mod, "grep -r foo .", "nothing") for _ in range(4)]
    decisions = oracle_mod.replay(turns)
    fired = [d for d in decisions if d.emission is not None]
    assert fired and fired[0].emission.reason_string == "loop"
    conf = fired[0].telemetry[0].confidence
    assert conf == pytest.approx(4.0 / 12.0, abs=1e-9), (
        f"loop confidence {conf!r} is not the window-saturation ratio 4/12"
    )


def test_scaffold_confidence_is_nonedit_fraction(sense_mod, oracle_mod):
    """scaffold_trap confidence == (action_count - source_edit_count) /
    action_count — at fire that is 25/25 == 1.0, derived not asserted."""
    turns = [_turn(sense_mod, f"cat file{i}.txt", "x") for i in range(25)]
    decisions = oracle_mod.replay(turns)
    fired = [d for d in decisions if d.emission is not None]
    assert fired and fired[0].emission.reason_string == "scaffold_trap"
    st = sense_mod.sense(turns[: fired[0].turn_index + 1])
    expect = (st.action_count - st.source_edit_count) / st.action_count
    assert fired[0].telemetry[0].confidence == pytest.approx(expect, abs=1e-9)


# ---------------------------------------------------------------------------
# FIX 3c — the confidence floor is DISTRIBUTION-DERIVED (median + 1*MAD over
# the pool's own scores): a function of the data, never a constant.
# ---------------------------------------------------------------------------
def test_distribution_floor_is_median_plus_mad(oracle_mod):
    import statistics

    pool = [0.9, 0.9, 0.5]
    med = statistics.median(pool)
    mad = statistics.median(abs(x - med) for x in pool)
    assert oracle_mod.distribution_floor(pool) == pytest.approx(med + mad)
    # singleton pool: floor == the value itself -> the candidate passes (>=).
    assert oracle_mod.distribution_floor([0.33333333]) == pytest.approx(0.33333333)
    # empty pool: floor 0.0 (nothing to gate).
    assert oracle_mod.distribution_floor([]) == 0.0


def test_gate_pool_suppresses_low_tail_below_floor(oracle_mod):
    """In a multi-candidate pool the low-tail candidate is suppressed with
    reason below_floor; the winner is emitted; telemetry covers BOTH."""
    mk = oracle_mod.Candidate
    strong = mk(
        id="a",
        layer="L5",
        kind="x",
        content="A",
        confidence=0.9,
        severity="verification_gap",
        trigger="t",
    )
    weak = mk(
        id="b",
        layer="L5",
        kind="y",
        content="B",
        confidence=0.2,
        severity="verification_gap",
        trigger="t",
    )
    winner, telemetry = oracle_mod.gate_pool([strong, weak])
    assert winner is not None and winner.id == "a"
    by_id = {t.candidate_id: t for t in telemetry}
    assert by_id["b"].suppressed_reason == "below_floor"
    assert by_id["a"].suppressed_reason is None


def test_gate_pool_singleton_passes(oracle_mod):
    c = oracle_mod.Candidate(
        id="solo",
        layer="L5",
        kind="x",
        content="A",
        confidence=0.33333333,
        severity="stuck",
        trigger="t",
    )
    winner, telemetry = oracle_mod.gate_pool([c])
    assert winner is not None and winner.id == "solo"


def test_gate_pool_ranks_by_severity_then_confidence(oracle_mod):
    mk = oracle_mod.Candidate
    obligation = mk(
        id="o",
        layer="spec",
        kind="obligation",
        content="O",
        confidence=0.8,
        severity="obligation",
        trigger="t",
    )
    nudge = mk(
        id="n", layer="L5", kind="x", content="N", confidence=0.8, severity="stuck", trigger="t"
    )
    winner, telemetry = oracle_mod.gate_pool([nudge, obligation])
    assert winner is not None and winner.id == "o"
    by_id = {t.candidate_id: t for t in telemetry}
    assert by_id["n"].suppressed_reason == "outranked"


# ---------------------------------------------------------------------------
# FIX 4 — spec.py obligations reach the oracle's candidate pool.
# ---------------------------------------------------------------------------
_OBLIGATIONS = [
    {
        "verbatim_text": "The capture_snapshot method should be async.",
        "kind": "behavior",
        "symbols": ["capture_snapshot"],
        "keywords": ["async", "should"],
        "checkable_forms": ["async"],
    },
    {
        "verbatim_text": "get_user must return Optional[User].",
        "kind": "behavior",
        "symbols": ["get_user", "Optional.User"],
        "keywords": ["must", "returns"],
        "checkable_forms": [],
    },
]


def test_load_obligations_reads_anchors_artifact(oracle_mod, tmp_path):
    """The oracle reads obligations from the SAME gt_issue_anchors.json channel
    v1r_brief.py writes (F1 plumbing, now with a reader)."""
    p = tmp_path / "gt_issue_anchors.json"
    p.write_text(json.dumps({"symbols": [], "obligations": _OBLIGATIONS}), encoding="utf-8")
    obls = oracle_mod.load_obligations(str(p))
    assert len(obls) == 2
    assert obls[0]["symbols"] == ["capture_snapshot"]
    # missing file / no obligations key -> empty list, never raises.
    assert oracle_mod.load_obligations(str(tmp_path / "missing.json")) == []


def test_obligations_enter_candidate_pool_with_telemetry(sense_mod, oracle_mod):
    """Wired: obligation candidates appear in the oracle's per-turn telemetry
    (suppressed no_trigger until Stage 3 lands their trigger), 8-dp scores."""
    turns = [_turn(sense_mod, "cat README.md", "hello")]
    d = oracle_mod.oracle_decide(turns, 0, obligations=_OBLIGATIONS)
    spec_records = [t for t in d.telemetry if t.candidate_id.startswith("spec.")]
    assert len(spec_records) == 2, (
        "spec obligations did not reach the oracle candidate pool "
        f"(telemetry ids: {[t.candidate_id for t in d.telemetry]})"
    )
    for rec in spec_records:
        assert rec.suppressed_reason == "no_trigger"
        assert isinstance(rec.confidence, float)
    assert d.emission is None  # silence preserved; telemetry still recorded


def test_obligations_do_not_change_parity_emissions(sense_mod, oracle_mod):
    """Wiring the pool must NOT change Stage-2 parity: the emitted nudges are
    identical with and without obligations supplied."""
    turns = [_turn(sense_mod, f"cat f{i}.txt", "x") for i in range(25)]
    without = [d.emission.reason_string for d in oracle_mod.replay(turns) if d.emission]
    withobl = [
        d.emission.reason_string
        for d in oracle_mod.replay(turns, obligations=_OBLIGATIONS)
        if d.emission
    ]
    assert without == withobl == ["scaffold_trap"]


# ---------------------------------------------------------------------------
# Plan §1.4 — telemetry ALWAYS (even on silence) + the JSONL writer, 8dp.
# ---------------------------------------------------------------------------
def test_silence_still_records_suppression_telemetry(sense_mod, oracle_mod):
    turns = [_turn(sense_mod, "ls", "ok")]
    d = oracle_mod.oracle_decide(turns, 0, obligations=_OBLIGATIONS)
    assert d.emission is None
    assert len(d.telemetry) >= 2  # the obligation candidates, suppressed


def test_oracle_events_jsonl_writer_8dp(sense_mod, oracle_mod, tmp_path):
    turns = [_turn(sense_mod, f"cat f{i}.txt", "x") for i in range(25)]
    decisions = oracle_mod.replay(turns, obligations=_OBLIGATIONS)
    out = tmp_path / "gt_oracle_events_test.jsonl"
    oracle_mod.write_oracle_events(decisions, str(out))
    lines = [json.loads(x) for x in out.read_text(encoding="utf-8").splitlines()]
    assert len(lines) == len(turns)
    # every numeric score serialized at 8-dp precision (the deep-logging rule).
    saw_score = False
    for rec in lines:
        for t in rec.get("telemetry", []):
            for k in ("confidence", "severity_score", "relevance_overlap"):
                v = t.get(k)
                assert v is not None
                assert float(f"{float(v):.8f}") == float(v)
                saw_score = True
    assert saw_score


# ---------------------------------------------------------------------------
# FIXES 1/2/5 — the gt_gt.md §15 citation-honesty doc contracts.
# ---------------------------------------------------------------------------
