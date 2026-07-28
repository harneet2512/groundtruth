"""#31 / L2 — the cheapest REAL causal signal: a same-state counterfactual at the boundary.

WHAT THIS BUYS. Every other GT signal is observational: GT delivered, the agent did something,
and we argue about whether the two are related. Here the state is IDENTICAL — same messages, same
kwargs, same provider, same turn — and the ONLY difference is whether the staged capsule is
present. That is a genuine counterfactual contrast, and it is available without a paired run.

WHAT IT IS NOT. It does not SIGN itself. Deciding whether the GT-arm action was BETTER needs an
anchor this layer does not have and must not invent, so the boundary records the PAIR and leaves
signing to offline analysis where a measurement-only anchor exists. Recording an unsigned pair is
honest; inventing a direction here would be the "presents telemetry as evidence" failure.

NON-NEGOTIABLES PINNED BELOW:
  * OFF BY DEFAULT and byte-identical when off — no extra provider call, no row.
  * The counterfactual response is DISCARDED. The agent must receive the REAL response.
  * The counterfactual call must NOT enter the delivery state machine: it has no capsule bound to
    it, so a row that could be mistaken for a delivery would be worse than no measurement at all.
  * Sampling is DETERMINISTIC (hash of the model-call id), never random — a random probe would
    make replay unfaithful, and SS-10 replay already suffers from stale recordings.
  * A fault in the probe can never break the agent's turn.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from groundtruth.runtime import miniswe_provider_boundary as mpb


def _resp(rid: str, text: str) -> SimpleNamespace:
    return SimpleNamespace(
        id=rid,
        status="completed",
        choices=[
            SimpleNamespace(
                finish_reason="stop",
                message=SimpleNamespace(content=text),
            )
        ],
    )


# --------------------------------------------------------------------------- #
# Text / action extraction — a PROXY for the action, and labelled as one.
# --------------------------------------------------------------------------- #
def test_response_text_is_extracted_defensively() -> None:
    assert mpb._response_text(_resp("r1", "hello")) == "hello"
    # Never raises on a malformed provider object; absence reads as "".
    assert mpb._response_text(SimpleNamespace()) == ""
    assert mpb._response_text(SimpleNamespace(choices=[])) == ""
    assert mpb._response_text(None) == ""


def test_first_action_extracts_the_fenced_command_block() -> None:
    text = "I will look.\n\n```bash\ngrep -rn foo src/\n```\n"
    assert mpb._first_action(text) == "grep -rn foo src/"
    # No fence => no action proxy, not a guess at one.
    assert mpb._first_action("just prose") == ""
    assert mpb._first_action("") == ""


# --------------------------------------------------------------------------- #
# DETERMINISTIC sampling. A random probe makes replay unfaithful.
# --------------------------------------------------------------------------- #
def test_probe_selection_is_deterministic_and_rate_bounded() -> None:
    assert mpb._l2_probe_selected("call-1", 0.0) is False
    assert mpb._l2_probe_selected("call-1", 1.0) is True
    first = mpb._l2_probe_selected("call-abc", 0.5)
    for _ in range(5):
        assert mpb._l2_probe_selected("call-abc", 0.5) is first
    # Some keys in, some out — otherwise the sampler is a constant and unusable.
    selected = [
        mpb._l2_probe_selected(f"call-{i}", 0.5) for i in range(40)
    ]
    assert any(selected) and not all(selected)


def test_probe_rate_defaults_to_off(monkeypatch) -> None:
    monkeypatch.delenv("GT_L2_PROBE_RATE", raising=False)
    assert mpb._l2_probe_rate() == 0.0
    monkeypatch.setenv("GT_L2_PROBE_RATE", "not-a-number")
    assert mpb._l2_probe_rate() == 0.0
    monkeypatch.setenv("GT_L2_PROBE_RATE", "0.25")
    assert mpb._l2_probe_rate() == 0.25
    # Clamped, never trusted blindly.
    monkeypatch.setenv("GT_L2_PROBE_RATE", "5")
    assert mpb._l2_probe_rate() == 1.0
    monkeypatch.setenv("GT_L2_PROBE_RATE", "-2")
    assert mpb._l2_probe_rate() == 0.0


# --------------------------------------------------------------------------- #
# The recorded row: OUT of the delivery namespace, and the pair is unsigned.
# --------------------------------------------------------------------------- #
def _run_probe(tmp_path: Path, rate: str, monkeypatch) -> list[dict]:
    monkeypatch.setenv("GT_L2_PROBE_RATE", rate)
    sink = tmp_path / "receipts.jsonl"
    calls: list[list] = []

    def fake_query(msgs, **kwargs):
        calls.append(msgs)
        return _resp("counterfactual-1", "no-gt\n\n```bash\nls\n```\n")

    boundary = SimpleNamespace(
        _receipt_sink_path=str(sink),
        _original_query=fake_query,
        _without_staged_capsule=lambda msgs, active: [
            m for m in msgs if m.get("role") != "gt-capsule"
        ],
    )
    active = SimpleNamespace(
        model_call_id="call-abc",
        observation_id="obs-1",
        capsule_hash="c" * 64,
        capsule_text="CAPSULE",
    )
    messages = [
        {"role": "user", "content": "task"},
        {"role": "gt-capsule", "content": "CAPSULE"},
    ]
    real = _resp("real-1", "with-gt\n\n```bash\ngrep -rn foo src/\n```\n")

    mpb._maybe_record_counterfactual_pair(
        boundary, messages, active, real, {}
    )
    if not sink.exists():
        return []
    return [
        json.loads(line)
        for line in sink.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def test_off_by_default_issues_no_call_and_writes_no_row(
    tmp_path, monkeypatch
) -> None:
    assert _run_probe(tmp_path, "0", monkeypatch) == []


def test_pair_row_is_outside_the_delivery_namespace(
    tmp_path, monkeypatch
) -> None:
    rows = _run_probe(tmp_path, "1", monkeypatch)
    assert len(rows) == 1
    row = rows[0]

    # The whole point: this must never be mistaken for a delivery.
    assert row["schema"] == "gt.counterfactual_pair.v1"
    assert row["layer"] == "measurement.counterfactual_pair"
    assert row["outcome"] == "measurement_only"
    assert row["chars_delivered"] == 0
    assert "delivery_attempt_id" not in row
    assert "content_sha256_16" not in row

    # Both arms recorded, and the contrast is explicit.
    assert row["treatment_action"] == "grep -rn foo src/"
    assert row["control_action"] == "ls"
    assert row["actions_differ"] is True

    # UNSIGNED on purpose: this layer has no anchor and must not invent a direction.
    assert "effect_sign" not in row
    assert "improved" not in row
    assert row["signed"] is False


def test_probe_is_actually_wired_into_the_dispatch_path() -> None:
    """STRUCTURAL, and labelled as such: the helper being green proves nothing about it
    running in production. Every other test here drives the helper directly, so without
    this one the probe could be deleted from the dispatch path and the suite would stay
    green — the exact "wired is not working" failure.

    It is a source assertion because a behavioural test would need a live provider round
    trip reaching DISPATCHED; that is worth building, but its absence must not leave the
    call site unpinned in the meantime.
    """
    import inspect

    source = inspect.getsource(mpb)
    assert "_maybe_record_counterfactual_pair(" in source
    # Called, not merely defined.
    assert source.count("_maybe_record_counterfactual_pair(") >= 2
    # And positioned after a real dispatch, not before one.
    call_index = source.rindex("_maybe_record_counterfactual_pair(\n")
    dispatch_index = source.index(
        "response = boundary._original_query(messages, **kwargs)"
    )
    assert call_index > dispatch_index, (
        "the counterfactual must be issued AFTER the real dispatch; issuing it first "
        "would change what the agent receives"
    )


def test_probe_fault_never_breaks_the_turn(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("GT_L2_PROBE_RATE", "1")

    def exploding_query(msgs, **kwargs):
        raise RuntimeError("provider exploded")

    boundary = SimpleNamespace(
        _receipt_sink_path=str(tmp_path / "r.jsonl"),
        _original_query=exploding_query,
        _without_staged_capsule=lambda msgs, active: msgs,
    )
    active = SimpleNamespace(
        model_call_id="call-abc",
        observation_id="obs-1",
        capsule_hash="c" * 64,
        capsule_text="CAPSULE",
    )
    # Must return None rather than propagate — the agent's turn is not ours to break.
    assert (
        mpb._maybe_record_counterfactual_pair(
            boundary, [{"role": "user", "content": "t"}], active,
            _resp("real-1", "x"), {},
        )
        is None
    )
