"""Wave 16 RED-first: the gate must exercise the canonical runtime, not only
the legacy ``_augment_output`` seam, and must not call a ledger row "delivery".

WHY (LIPI / Integration).  ``scripts/swebench/ss_gate.py`` drives the legacy
seam at exactly one chokepoint -- ``_drive_event`` -> ``g._augment_output``
(``ss_gate.py:430``) -- and neither ``ss_gate.py`` nor
``scripts/swebench/ss_replay_oracle.py`` (``:614``, ``:2005``) contains a single
reference to ``install_canonical_runtime`` or
``_CANONICAL_RUNTIME_ATTACHMENT``.  A green gate therefore proves legacy-seam
regression coverage and nothing about the deterministic runtime.

WHY IT MATTERS EMPIRICALLY.  ``SeamResult.delivered_rows()`` defines delivery as
``outcome == "delivered" and chars_delivered > 0``.  That is the same predicate
GT's runtime ledger used in paid run ``30169158187``, where all 80 rows matching
it failed ``ss_proof_manifest``'s byte join against the model-visible
trajectory (0/80 located by ``_sealed_window``).  A ledger row is a local
routing record, not evidence that bytes reached the provider.

Handoff invariants under test (§9): ``COMPILED`` != ``JOINED`` != ``DISPATCHED``
!= ``PROVIDER_ACCEPTED`` != ``DELIVERED``; ``DELIVERED`` requires provider
terminal inference over the exact joined payload.

These assertions are about the gate's ARCHITECTURE, so they cannot be satisfied
by component-level work on the runtime itself.
"""

from __future__ import annotations

import pathlib
import sys
from types import SimpleNamespace


_SS_DIR = pathlib.Path(__file__).resolve().parents[2] / "scripts" / "swebench"
if str(_SS_DIR) not in sys.path:
    sys.path.insert(0, str(_SS_DIR))

import ss_gate as G  # noqa: E402


def _canonical_driver_types() -> list[type]:
    """Any gate driver that declares it installs the canonical runtime."""
    return [
        obj
        for obj in vars(G).values()
        if isinstance(obj, type)
        and getattr(obj, "installs_canonical_runtime", False) is True
    ]


def test_gate_exposes_a_driver_that_installs_the_canonical_runtime() -> None:
    """RED until a canonical driver exists alongside ``RealSeamDriver``."""
    assert _canonical_driver_types(), (
        "ss_gate exposes no driver declaring installs_canonical_runtime=True; "
        "every scenario still runs through _augment_output, so a green gate "
        "cannot speak to the deterministic runtime"
    )


def test_canonical_driver_does_not_fabricate_provider_terminal_delivery() -> None:
    """RED until the canonical driver exists -- and the core honesty invariant.

    The gate is HERMETIC: it fabricates the repo, the graph, and every
    observation.  There is no provider.  A hermetic driver can therefore prove
    canonical events -> reducer -> lifecycle -> coalition -> compiler -> exact
    join -> DISPATCHED, and that the delivery state machine refuses illegal
    transitions.  It CANNOT prove DELIVERED, because ``DELIVERED`` is defined
    (handoff §9) as provider-terminal inference over the exact joined payload,
    and a double's "terminal inference" is fabricated by construction.

    Letting a provider double stamp DELIVERED would recreate, one layer up,
    exactly the defect Wave 16 exists to fix: certifying delivery from a
    self-generated record.  So the hermetic driver must report ZERO
    provider-terminal deliveries -- the honest answer, not the flattering one.
    """
    drivers = _canonical_driver_types()
    assert drivers, "no canonical driver yet (see the preceding test)"
    driver = drivers[0]

    assert getattr(driver, "proves_provider_terminal_delivery", None) is False, (
        "a hermetic gate driver must declare that it does NOT prove "
        "provider-terminal delivery; that bit requires a paid run"
    )


def test_legacy_real_seam_driver_is_labelled_honestly() -> None:
    """The legacy driver must not silently pass for canonical coverage.

    Keeping ``RealSeamDriver`` is correct -- it is real regression evidence.
    What is not correct is leaving it indistinguishable from a canonical
    driver, which is how "gate green" got read as "runtime proven".
    """
    assert hasattr(G, "RealSeamDriver"), "legacy regression driver disappeared"
    assert getattr(G.RealSeamDriver, "installs_canonical_runtime", False) is False, (
        "RealSeamDriver drives _augment_output; it must not claim canonical "
        "runtime coverage"
    )


def test_seam_result_separates_provider_terminal_delivery_from_ledger_rows() -> None:
    """RED until delivery proof requires more than a ledger row.

    ``delivered_rows()`` may remain (legacy view), but a distinct
    provider-terminal view must exist so no caller can treat a routing record
    as proof that bytes reached the model.
    """
    assert hasattr(G.SeamResult, "delivered_rows"), "legacy view disappeared"
    assert hasattr(G.SeamResult, "provider_terminal_deliveries"), (
        "SeamResult has no provider-terminal delivery view; delivered_rows() "
        "(outcome=delivered AND chars_delivered>0) is the exact predicate that "
        "failed the byte join 80/80 in run 30169158187"
    )


def test_canonical_driver_runs_but_emits_no_observation_delta() -> None:
    """MEASURED 2026-07-26. Documents why Wave 16 is not a driver swap.

    Driving the gate's OWN ``scenario_s1`` through both drivers:

      RealSeamDriver      -> PASS  (suppressed_observation_deliveries=1)
      CanonicalSeamDriver -> SKIP:flag-not-built  ("byte-identical no-op")

    The canonical runtime is not broken here.  It delivers through the
    PROVIDER PAYLOAD (staged capsule -> exact join -> model call), whereas
    ``_augment_output`` delivers by MUTATING THE TOOL OBSERVATION.  Every gate
    scenario asserts on observation deltas and runtime-ledger rows, so it is
    measuring a surface the canonical path deliberately does not touch.

    Consequence for Wave 16: the scenarios need canonical-native assertions
    (staged capsule identity, one-coalition-per-decision, exact join), not a
    driver substitution.  This test pins the CURRENT honest state so the gap
    cannot be silently rediscovered; update it when those assertions land.
    """
    driver = G.CanonicalSeamDriver()

    result = G.scenario_s1(driver)

    assert result.verdict.startswith("SKIP"), (
        "canonical driver now produces observation-surface deliveries; the "
        "Wave 16 scenario vocabulary assumption above needs revisiting"
    )


def _probe_events() -> list:
    """Two observations the fixture graph can actually answer."""
    return [
        G.Event(action={"command": "grep -rn alpha ."}, output="pkg/a.py:3:def alpha(x):", rc=0),
        G.Event(action={"command": "cat pkg/a.py"}, output="def alpha(x):\n    return x\n", rc=0),
    ]


def _install_fixture_localization(monkeypatch) -> None:
    """Supply deterministic rows without restoring the product embedding import.

    The production gateway deliberately leaves its historical embedding-backed
    localizer unregistered.  These canonical-chain tests are about the evidence
    and compilation boundaries, so they inject the already-resolved fixture rows
    at the narrow producer seam instead of depending on that comparison control.
    """
    from groundtruth.runtime import gateway

    monkeypatch.setattr(
        gateway,
        "_ranked_localization_rows",
        lambda _state, _audit=None: [("pkg/a.py", 3, "alpha")],
    )


def test_seam_result_carries_canonical_runtime_facts() -> None:
    """RED until SeamResult publishes what the canonical runtime actually does.

    MEASURED: after driving the gate's scenario_s1 through CanonicalSeamDriver
    the runtime holds real state -- ``work_state.sequence == 4``,
    ``work_state.phase == Phase.UNDERSTANDING``, and 4 committed canonical
    events in the journal.  None of it reaches ``SeamResult``, so no scenario
    can assert on it and the canonical driver reads as a "byte-identical
    no-op".  This is the concrete blocker behind #42.
    """
    result = G.CanonicalSeamDriver().run(_probe_events(), ss_env={})

    facts = getattr(result, "canonical", None)
    assert facts is not None, (
        "SeamResult exposes no canonical runtime facts; gate scenarios can "
        "only see observation deltas and ledger rows, which the canonical "
        "path deliberately never touches"
    )
    assert facts.committed_events >= len(_probe_events())
    assert facts.sequence >= len(_probe_events())
    assert facts.phase


def test_canonical_facts_expose_where_the_chain_actually_stops(monkeypatch) -> None:
    """RED until the facts record carries evidence + compilation counts.

    MEASURED on the gate's own ``scenario_s1``:

        committed events                 = 4   (reducer runs)
        evidence_records_for_attempt     = 1   (fixture producer runs)
        compilations_for_attempt         = 0   (chain stops here)
        provider_boundary.records        = 0

    The canonical runtime observes, reduces, and produces evidence in the
    hermetic gate. Compilation remains correct-quiet because the deliberately
    small fixture does not close every required decision role. The compilation
    ledger must expose that decision-incomplete outcome explicitly.

    NOTE: ``journal.evidence_history()`` returns 0 here while
    ``evidence_records_for_attempt()`` returns 1 -- two different views. Assert
    on the latter; reading the former is how this was nearly misdiagnosed as
    "no evidence produced".
    """
    _install_fixture_localization(monkeypatch)
    result = G.CanonicalSeamDriver().run(_probe_events(), ss_env={})
    facts = getattr(result, "canonical", None)
    assert facts is not None

    # Behavioural, not hasattr: producers DID run (measured 1 record for these
    # two events, 2 for scenario_s1), and compilation did NOT.
    assert facts.evidence_records >= 1, (
        "no evidence produced; the chain would be stopping earlier than the "
        "inference boundary and #44's premise would be wrong"
    )
    # The current completeness contract requires decision-role closure.  This
    # two-observation fixture supplies localization evidence but no certified
    # TARGET_IDENTITY or BEHAVIORAL_CONTRACT witness, so compiling a capsule
    # would be a false-completeness regression.  The adjacent test requires the
    # resulting DECISION_INCOMPLETE outcome to be explicit rather than silent.
    assert facts.compilations == 0, (
        "the canonical chain compiled decision-incomplete fixture evidence; "
        "missing decision roles must remain held and correct-quiet"
    )


def test_failed_capsule_compilation_is_recorded_not_silent(monkeypatch) -> None:
    """RED until a FAILED compilation leaves a durable trace.

    MEASURED: every turn produces ``compilation.state=FAILED`` with
    ``failure_code=DECISION_INCOMPLETE`` and ``held_evidence_ids=1``.  The seam
    stages only on a truthy ``plan.delivery_attempt_id``
    (``gt_mini_patch.py:22389``) and writes NO ledger row otherwise, so
    downstream "compilation failed" is indistinguishable from "GT had nothing
    to say".

    That ambiguity is precisely what kept 0/17 unfalsifiable for months, now
    reappearing one layer deeper in the canonical architecture.  The quiet
    itself may well be CORRECT (correct-or-quiet on a thin fixture); an
    unexplained quiet is not.

    This asserts observability ONLY.  It must stay green whether the coalition
    later ships a capsule or keeps declining to -- never weaken the
    decision-completeness bar to satisfy it.
    """
    _install_fixture_localization(monkeypatch)
    result = G.CanonicalSeamDriver().run(_probe_events(), ss_env={})

    rows = [
        row for row in result.ledger
        if "compil" in f"{row.get('layer', '')}{row.get('reason', '')}".lower()
    ]
    assert rows, (
        "no ledger row explains the capsule compilation outcome; a silent "
        "compilation is indistinguishable from correct-or-quiet"
    )
    # UPDATED 2026-07-27 (C12), and this is the assertion the docstring above always
    # described. The original required the reason to contain DECISION_INCOMPLETE -- which
    # silently assumed compilation would always FAIL. It did, until the C12 openness fix let
    # a coalition form on this very fixture; the capsule then compiled and the file went red
    # with ZERO rows, because the seam only ever explained FAILURES.
    #
    # That was a real hole, and it was fixed in the SEAM, not here: the success branch of
    # `MiniSweProviderBoundary.observe_action_result` now writes its own row. The bar is
    # UNCHANGED -- every compilation outcome must still leave a durable, self-describing
    # trace. Only the assumption that the outcome is always a failure is gone, exactly as
    # "must stay green whether the coalition later ships a capsule or keeps declining to"
    # instructed. The decision-completeness bar itself was never touched.
    _explained = ("DECISION_INCOMPLETE", "COMPILED", "FAILED", "staged")
    assert all(
        any(token in str(row.get("reason", "")) for token in _explained)
        for row in rows
    ), f"a compilation row explains nothing about its outcome: {rows[:2]}"


def test_legacy_driver_reports_no_canonical_facts() -> None:
    """Correct-or-quiet: the legacy seam has no canonical runtime to report.

    Biting half of the pair -- an implementation that fabricates facts for
    every driver passes the previous test and fails this one.
    """
    result = G.RealSeamDriver().run(_probe_events(), ss_env={})

    assert getattr(result, "canonical", "missing") is None, (
        "the legacy driver must report canonical facts as None, never "
        "synthesise them"
    )


def test_gate_exposes_a_canonical_chain_scenario(monkeypatch) -> None:
    """RED until the gate has a scenario written in canonical vocabulary.

    Every existing scenario (s1..s11) asserts on observation deltas and
    runtime-ledger rows -- the surface the canonical path deliberately never
    touches, which is why CanonicalSeamDriver reads SKIP:flag-not-built on all
    of them.  A canonical scenario must instead assert on ``SeamResult
    .canonical``: canonical events committed, evidence produced, and the
    compilation outcome reported honestly rather than inferred from silence.
    """
    assert hasattr(G, "scenario_canonical_chain"), (
        "ss_gate has no canonical-vocabulary scenario; every scenario still "
        "asserts on observation deltas the canonical path never produces"
    )

    _install_fixture_localization(monkeypatch)
    result = G.scenario_canonical_chain(G.CanonicalSeamDriver())

    assert result.verdict == "PASS", f"{result.verdict}: {result.detail}"
    # The detail must SHOW the chain, not just assert it.
    for token in ("events=", "evidence=", "compilations="):
        assert token in result.detail, f"missing {token!r} in {result.detail!r}"


def test_gate_grades_quarantine_never_pretends_delivery() -> None:
    """RED until the gate covers handoff invariant #17.

    "GT quarantine never mutates native tool output or pretends delivery
    occurred."  No gate scenario asserts this today: s1..s11 grade SS referees
    on the LEGACY delivery lane, so a quarantined canonical runtime that kept
    emitting would pass every one of them.

    Deliberately NOT a translation of any legacy scenario.  The canonical path
    produces no observation deltas, so translating s1..s11 mechanically would
    yield scenarios that assert on a surface that is silent by design.  The
    real gap is the §9 invariants that have zero gate coverage.
    """
    assert hasattr(G, "scenario_canonical_quarantine"), (
        "ss_gate has no scenario covering invariant #17; a quarantined "
        "runtime that kept emitting would pass every existing scenario"
    )

    result = G.scenario_canonical_quarantine(G.CanonicalSeamDriver())

    assert result.verdict == "PASS", f"{result.verdict}: {result.detail}"
    for token in ("health=", "emission=", "native="):
        assert token in result.detail, f"missing {token!r} in {result.detail!r}"


class _FailureState:
    """A synthetic failure state for driving the invariant evaluator."""

    def __init__(self, **kw):
        self.health = SimpleNamespace(name=kw.get("health", "QUARANTINED"))
        self.assurance = SimpleNamespace(name=kw.get("assurance", "UNASSURED"))
        self.gt_emission_enabled = kw.get("emission", False)
        self.gt_interruption_enabled = kw.get("interruption", False)
        self.gt_certification_enabled = kw.get("certification", False)
        self.native_path_enabled = kw.get("native", True)


def _verdicts(state, terminal=0) -> dict:
    return dict(G.quarantine_invariant_subchecks(state, terminal))


def test_chain_invariant_checks_actually_discriminate() -> None:
    """Each canonical-chain subcheck must FAIL on its own violation.

    Same structural cure as the quarantine evaluator: drive the pure function
    with violating facts rather than trusting a happy-path scenario run.
    """
    healthy = SimpleNamespace(committed_events=4, evidence_records=1)

    def verdicts(facts, terminal=0):
        return dict(G.chain_invariant_subchecks(facts, terminal))

    assert all(v == "PASS" for v in verdicts(healthy).values())

    assert verdicts(SimpleNamespace(committed_events=0, evidence_records=1))[
        "reducer committed canonical events"] == "FAIL"
    assert verdicts(SimpleNamespace(committed_events=4, evidence_records=0))[
        "producers manufactured evidence"] == "FAIL"
    assert verdicts(healthy, terminal=1)[
        "provider-terminal delivery not fabricated"] == "FAIL"


def test_quarantine_invariant_checks_actually_discriminate() -> None:
    """Each invariant-#17 subcheck must FAIL on its own violation.

    Without this, weakening any single subcheck still yields a PASSing
    scenario -- measured: a mutation forcing the emission check to PASS
    survived a test that only asserted the happy path.  A scenario that never
    sees a violating state cannot show that it checks anything.
    """
    assert all(v == "PASS" for v in _verdicts(_FailureState()).values())

    assert _verdicts(_FailureState(health="HEALTHY"))[
        "runtime is QUARANTINED/UNASSURED"] == "FAIL"
    assert _verdicts(_FailureState(assurance="ASSURED"))[
        "runtime is QUARANTINED/UNASSURED"] == "FAIL"
    assert _verdicts(_FailureState(emission=True))[
        "GT emission disabled"] == "FAIL"
    assert _verdicts(_FailureState(interruption=True))[
        "GT emission disabled"] == "FAIL"
    assert _verdicts(_FailureState(certification=True))[
        "GT emission disabled"] == "FAIL"
    assert _verdicts(_FailureState(native=False))[
        "native agent path preserved"] == "FAIL"
    assert _verdicts(_FailureState(), terminal=1)[
        "no delivery claimed while quarantined"] == "FAIL"


def test_canonical_chain_scenario_skips_on_the_legacy_driver() -> None:
    """Correct-or-quiet: the legacy driver has no canonical runtime to grade.

    Biting half of the pair -- a scenario that PASSes for any driver would be
    asserting nothing.
    """
    assert hasattr(G, "scenario_canonical_chain"), "see the preceding test"

    result = G.scenario_canonical_chain(G.RealSeamDriver())

    assert result.verdict.startswith("SKIP"), (
        "the legacy driver installs no canonical runtime, so a canonical "
        f"scenario must SKIP, not {result.verdict}"
    )


class _Attempt:
    """Minimal stand-in carrying the DeliveryAttempt fields the view reads."""

    def __init__(self, state, joined="j1", payload="p1", call="c1", terminal=object()):
        self.state = state
        self.joined_capsule_hash = joined
        self.provider_payload_hash = payload
        self.model_call_id = call
        self.terminal_kind = terminal


class _State:
    def __init__(self, name):
        self.name = name


def test_provider_terminal_view_requires_a_terminal_state() -> None:
    """COMPILED/JOINED/DISPATCHED/PROVIDER_ACCEPTED are not delivery (§9)."""
    result = G.SeamResult(
        delivery_attempts=[
            _Attempt(_State("COMPILED")),
            _Attempt(_State("JOINED")),
            _Attempt(_State("DISPATCHED")),
            _Attempt(_State("PROVIDER_ACCEPTED")),
            _Attempt(_State("PROVIDER_REJECTED")),
            _Attempt(_State("INFERENCE_FAILED")),
        ]
    )

    assert result.provider_terminal_deliveries() == []


def test_provider_terminal_view_accepts_delivered_and_response_committed() -> None:
    """RESPONSE_COMMITTED strictly follows DELIVERED, so it also counts."""
    result = G.SeamResult(
        delivery_attempts=[
            _Attempt(_State("DELIVERED")),
            _Attempt(_State("RESPONSE_COMMITTED")),
        ]
    )

    assert len(result.provider_terminal_deliveries()) == 2


def test_provider_terminal_view_requires_exact_join_identity() -> None:
    """A terminal state without the joined/payload hashes is not delivery."""
    result = G.SeamResult(
        delivery_attempts=[
            _Attempt(_State("DELIVERED"), joined=""),
            _Attempt(_State("DELIVERED"), payload=""),
        ]
    )

    assert result.provider_terminal_deliveries() == []


def test_provider_terminal_view_requires_a_provider_call_record() -> None:
    """Without model_call_id + terminal_kind there is no provider terminal."""
    result = G.SeamResult(
        delivery_attempts=[
            _Attempt(_State("DELIVERED"), call=""),
            _Attempt(_State("DELIVERED"), terminal=None),
        ]
    )

    assert result.provider_terminal_deliveries() == []


def test_zero_character_rows_are_never_delivery() -> None:
    """Guard (currently GREEN): keep zero-byte instruments out of delivery.

    GT's ack lane writes ``event_type='ack'`` rows with ``chars_delivered=0``
    that ride the same ledger view as the delivery they annotate.  Counting
    them would double-count every delivery.
    """
    result = G.SeamResult(
        ledger=[
            {"outcome": "delivered", "chars_delivered": 0, "event_type": "ack"},
            {"outcome": "delivered", "chars_delivered": 12, "layer": "l3.contract"},
        ]
    )

    rows = result.delivered_rows()

    assert len(rows) == 1
    assert rows[0].get("chars_delivered") == 12


def test_shadow_holdout_rows_are_never_delivery() -> None:
    """Guard: a shadow/holdout row is a suppressed candidate, not a delivery."""
    result = G.SeamResult(
        ledger=[
            {"outcome": "suppressed_hidden_only", "chars_delivered": 40},
            {"outcome": "suppressed_internal_only", "chars_delivered": 40},
            {"outcome": "delivered", "chars_delivered": 40, "layer": "l3.contract"},
        ]
    )

    assert len(result.delivered_rows()) == 1
