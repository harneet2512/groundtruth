"""A dead timing authority must cost GT its TIMING, not its EVIDENCE.

THE GAP.  `_augment_output` chose its route on identity alone::

    attachment = _CANONICAL_RUNTIME_ATTACHMENT
    if attachment is None:
        _augment_output_legacy(action, out); return
    observer(action, out)

So the legacy path was reachable only when the canonical runtime had never been built. Once
built, it owned every observation for the rest of the attempt -- including after it went
dark. And `observe_action_result` returns silently when `_component_available` is False, with
no ledger row. One unexpected exception anywhere in the observer therefore took a task's GT
delivery to ZERO for every remaining step, invisibly.

That is strictly worse than the route it replaced. Before the oracle hook existed, that same
task shipped evidence through the legacy path with no timing guarantee. After, it can ship
nothing at all. Measured shape of the old route on run 30229092179: 81 delivered payloads.

WHY THIS IS NOT A WEAKENED BAR.  The exclusivity invariant -- "canonical live attempts have
one environment route; no legacy submit gate, lane arbiter, appender or ledger may run
alongside it" -- exists to stop DOUBLE delivery, which would break dose<=1. That invariant is
about two routes being live at the same time.

A dark runtime is not live. `_component_available` False means the canonical observer emits
nothing, for any observation, permanently. So the two routes are mutually exclusive by
construction here: for a given observation exactly one of them can produce bytes, and which
one is decided before either runs. Dose<=1 is preserved, and the tests below pin that as a
property rather than an argument -- including the case that would actually break it (a
merely-ISOLATED observer whose runtime is still emitting must NOT get a legacy shadow).

Correct-or-quiet is preserved too: falling back does not invent evidence, it re-enables the
producer set that was already the shipping default. What is lost is the release-timing
guarantee, which is exactly what died, and the fallback is recorded so an audit can subtract
those observations from any timing claim instead of silently counting them.
"""

from __future__ import annotations

import inspect
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from artifact_deepswe import gt_mini_patch as seam  # noqa: E402


class _FailureState:
    def __init__(self, emission: bool, isolated: tuple[str, ...] = ()):
        self.gt_emission_enabled = emission
        self.isolated_components = isolated


class _Runtime:
    def __init__(self, state):
        self.failure_state = state


def _attachment(*, emission=True, isolated=(), record=None):
    """A stand-in with the SAME shape the real attachment exposes to _augment_output."""
    att = types.SimpleNamespace()
    att.attempt_runtime = _Runtime(_FailureState(emission, isolated))
    sink = record if record is not None else []
    att.observe_action_result = lambda a, o: sink.append("canonical")
    return att


@pytest.fixture()
def routed(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(
        seam, "_augment_output_legacy", lambda a, o: calls.append("legacy")
    )
    return calls


def test_a_live_runtime_keeps_the_canonical_route(monkeypatch, routed):
    """CONTROL. The fallback must not fire on a healthy runtime, or every attempt gets the
    legacy path and the oracle is dead again for a different reason."""
    monkeypatch.setattr(
        seam, "_CANONICAL_RUNTIME_ATTACHMENT", _attachment(record=routed), raising=False
    )
    seam._augment_output("cmd", {"output": "x"})
    assert routed == ["canonical"]


def test_no_attachment_still_takes_the_legacy_route(monkeypatch, routed):
    """CONTROL. The pre-existing behaviour is unchanged."""
    monkeypatch.setattr(seam, "_CANONICAL_RUNTIME_ATTACHMENT", None, raising=False)
    seam._augment_output("cmd", {"output": "x"})
    assert routed == ["legacy"]


def test_a_quarantined_runtime_falls_back_instead_of_shipping_nothing(monkeypatch, routed):
    """THE FIX. gt_emission_enabled=False is a dead runtime -- deliver via legacy."""
    monkeypatch.setattr(
        seam,
        "_CANONICAL_RUNTIME_ATTACHMENT",
        _attachment(emission=False, record=routed),
        raising=False,
    )
    seam._augment_output("cmd", {"output": "x"})
    assert routed == ["legacy"], (
        "a quarantined runtime still owns the route -- this task ships ZERO GT bytes for "
        "every remaining observation"
    )


def test_an_isolated_canonical_observer_falls_back(monkeypatch, routed):
    """The other way the observer goes dark: isolation without full quarantine.

    `observe_action_result` returns silently when `canonical_observer` is isolated, so this
    is the same total-silence outcome by a different route.
    """
    monkeypatch.setattr(
        seam,
        "_CANONICAL_RUNTIME_ATTACHMENT",
        _attachment(isolated=("canonical_observer",), record=routed),
        raising=False,
    )
    seam._augment_output("cmd", {"output": "x"})
    assert routed == ["legacy"]


def test_isolation_of_an_UNRELATED_component_does_not_trigger_fallback(monkeypatch, routed):
    """THE DOSE-SAFETY CASE, and the one that would actually break the bar.

    If some other component is isolated but the canonical observer is still emitting, then
    BOTH routes would produce bytes for the same observation -- a double dose. The fallback
    must key on the observer's own availability, never on "something, somewhere, degraded".
    """
    monkeypatch.setattr(
        seam,
        "_CANONICAL_RUNTIME_ATTACHMENT",
        _attachment(isolated=("provider:OBSERVATION_JOIN",), record=routed),
        raising=False,
    )
    seam._augment_output("cmd", {"output": "x"})
    assert routed == ["canonical"], (
        "fell back while the canonical observer was still emitting -- the same observation "
        "would receive two doses, breaking dose<=1"
    )


def test_exactly_one_route_runs_per_observation(monkeypatch):
    """dose<=1 stated directly: never both, for any health state."""
    for emission, isolated in (
        (True, ()),
        (False, ()),
        (True, ("canonical_observer",)),
        (False, ("canonical_observer",)),
        (True, ("something_else",)),
    ):
        calls: list[str] = []
        monkeypatch.setattr(
            seam, "_augment_output_legacy", lambda a, o: calls.append("legacy")
        )
        monkeypatch.setattr(
            seam,
            "_CANONICAL_RUNTIME_ATTACHMENT",
            _attachment(emission=emission, isolated=isolated, record=calls),
            raising=False,
        )
        seam._augment_output("cmd", {"output": "x"})
        assert len(calls) == 1, (
            f"emission={emission} isolated={isolated} produced {calls} -- exactly one route "
            f"must own an observation"
        )


def test_a_health_probe_that_raises_does_not_break_the_agent(monkeypatch, routed):
    """Correct-or-quiet. If the probe itself explodes, keep the canonical route (the status
    quo) rather than raising into the agent's execute path."""

    att = _attachment(record=routed)

    class _Boom:
        @property
        def failure_state(self):
            raise RuntimeError("probe exploded")

    att.attempt_runtime = _Boom()
    monkeypatch.setattr(seam, "_CANONICAL_RUNTIME_ATTACHMENT", att, raising=False)
    seam._augment_output("cmd", {"output": "x"})
    assert routed in (["canonical"], ["legacy"]), "the probe raised into the agent"


def test_the_fallback_is_recorded_so_a_timing_claim_can_subtract_it(monkeypatch):
    """A silent fallback would let a run report canonical delivery counts while some of the
    bytes came from the untimed route. It must be visible in the ledger."""
    rows: list[dict] = []
    monkeypatch.setattr(seam, "_augment_output_legacy", lambda a, o: None)
    monkeypatch.setattr(
        seam, "_runtime_ledger_record", lambda **kw: rows.append(kw)
    )
    monkeypatch.setattr(
        seam,
        "_CANONICAL_RUNTIME_ATTACHMENT",
        _attachment(emission=False),
        raising=False,
    )
    seam._augment_output("cmd", {"output": "x"})
    assert rows, "the fallback left no trace"
    assert any("dark" in str(r.get("reason", "")) for r in rows), (
        f"no reason naming the darkness: {rows}"
    )
    assert all(r.get("chars", 0) == 0 for r in rows), "a telemetry row must ship no bytes"


def test_the_fallback_is_reached_from_augment_output_itself():
    """Source-level anchor: the routing decision must live where the route is chosen, not in
    a caller that a future refactor can bypass."""
    src = inspect.getsource(seam._augment_output)
    assert "_augment_output_legacy" in src
    assert "gt_emission_enabled" in src or "_canonical_observer_is_dark" in src, (
        "the route is still chosen on identity alone"
    )
