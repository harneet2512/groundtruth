"""Every ledger row must record WHICH DECISION WAS OPEN when it was written.

WHY THIS EXISTS (link 3 — "did the feature deliver AT THE CONTRACTED TIME?").

The first two attempts at this question were both wrong, and instructively so:

  1. Compare the ledger's `event_type` to the registry's `commitment_boundary` as STRINGS.
     Measured on a real 52,609-row ledger, those two vocabularies never match on a delivered
     row -- file_view/post_edit 58, search_result/post_view 20, failure_obs/test_result 17.
     A naive equality check reports a FALSE 0/N. They are the same moments under two namings,
     and reconciling labels was solving the wrong problem entirely.

  2. Read the attestation's `open_event` vs `required_event`. But `gt_mini_patch` computes the
     observed event as `lineage.actual_event OR required_event(evidence_type)` -- when lineage
     carries no observed event the observed boundary is DEFAULTED TO THE CONTRACTED ONE. That
     comparison returns ON_TIME by construction: a FALSE 100%.

The runtime already answers this SEMANTICALLY and needs no vocabulary at all.
`reasoning_runtime` gates release on the decision's commitment window (4676/4812):
COMMITTED or CLOSED -> EXPIRED (too late); OPEN -> RELEASED (on time); NOT_OPEN -> held (too
early). And every evidence record already carries its own `decision_context` from its feature
contract.

So the ONLY missing fact is which decision was OPEN at the moment a row was written. With it,
link 3 is a semantic comparison per row:
    row's active decision == evidence's decision_context  -> ON TIME
    evidence's context already passed                     -> LATE
    evidence's context not yet reached                    -> EARLY
The ORDERING of contexts deliberately lives in the analysis, not here. This seam records a
FACT; it does not adjudicate.

SCOPE -- STAGE 1, RECORD ONLY. This does NOT change what is delivered. The seam still passes
`CommitmentWindowState.OPEN` to `prepare_next_inference`, which is itself the bug behind link 3
being unfalsifiable (all three construction sites hardcode OPEN and assert the
COMMITMENT_WINDOW_OPEN predicate, so the 4812 gate can never be false). That is fixed in STAGE 2
-- and only once the data recorded here proves the derivation is right. The current defect is
BLINDNESS, which delivers too much; a wrong commitment model would SUPPRESS good evidence, which
is strictly worse. Recording before enforcing is the whole point.
"""

from __future__ import annotations

import inspect

from artifact_deepswe import gt_mini_patch as seam


def test_the_helper_exists_and_reports_unknown_when_nothing_is_attached():
    """Correct-or-quiet: with no canonical runtime attached there IS no open decision, and
    the seam must say so rather than inventing one."""
    helper = getattr(seam, "_current_active_decision", None)
    assert helper is not None, "no _current_active_decision helper"
    # No attachment in a bare unit context -> empty, never a guessed context.
    assert helper() == "", "an active decision was reported with nothing attached"


def test_the_helper_never_raises():
    """Telemetry must never break the agent loop -- the established rule for
    `_current_iteration` and every other observability helper in this seam."""
    seam._current_active_decision()  # must not raise


def test_rows_carry_the_active_decision_when_one_is_open(monkeypatch, tmp_path):
    """THE FIX, behaviourally. A row written while a decision is open records WHICH one."""
    ledger = tmp_path / "gt_runtime_ledger.jsonl"
    monkeypatch.setenv("GT_RUNTIME_LEDGER", str(ledger))
    monkeypatch.setattr(
        seam, "_current_active_decision", lambda: "PATCH_CONSTRUCTION", raising=False)

    seam._runtime_ledger_record(
        kind="l3.contract", outcome="delivered", reason="probe", chars=42)

    import json
    rows = [json.loads(x) for x in ledger.read_text(encoding="utf-8").splitlines() if x]
    assert len(rows) == 1, f"expected exactly 1 row, got {len(rows)}"
    assert rows[0].get("gt_audit_active_decision") == "PATCH_CONSTRUCTION", (
        "the row does not record which decision was open -- link 3 stays uncomputable"
    )


def test_rows_omit_the_key_entirely_when_no_decision_is_open(monkeypatch, tmp_path):
    """CORRECT-OR-QUIET, and the anti-tautology guard.

    An unknown window must be ABSENT, never defaulted to a plausible value. Defaulting is
    exactly how the attestation path produced a false 100% on-time: it substituted the
    CONTRACTED event when the observed one was missing. An absent key reads as
    NOT-EVALUABLE downstream; a defaulted key reads as proof.
    """
    ledger = tmp_path / "gt_runtime_ledger.jsonl"
    monkeypatch.setenv("GT_RUNTIME_LEDGER", str(ledger))
    monkeypatch.setattr(seam, "_current_active_decision", lambda: "", raising=False)

    seam._runtime_ledger_record(
        kind="l3.contract", outcome="delivered", reason="probe", chars=42)

    import json
    rows = [json.loads(x) for x in ledger.read_text(encoding="utf-8").splitlines() if x]
    assert len(rows) == 1
    assert "gt_audit_active_decision" not in rows[0], (
        "an unknown active decision was stamped anyway -- a defaulted value would make the "
        "on-time check self-confirming, the exact defect this replaces"
    )


def test_the_stamp_lives_in_the_audit_namespace():
    """It must NOT be a graded field. `attestation_join._row_has_registered_lineage` seats a
    truth join on `fact_class`, and that join feeds the promotion authority -- a diagnostic
    field that a grader reads can inflate a proof number. Same reasoning as the existing
    `gt_audit_fact_class` / `gt_audit_contracted_boundary` split."""
    src = inspect.getsource(seam._runtime_ledger_record)
    assert "gt_audit_active_decision" in src
    for graded in ("fact_class\"", "lineage_schema"):
        assert f"_row[\"{graded}\"] = " not in src


def test_every_site_that_opens_a_decision_also_publishes_it():
    """ADDED BECAUSE A MUTATION SURVIVED.

    Deleting the `_publish_active_decision(self, active)` call from the per-observation site
    left all six other tests GREEN: they pin the helper and the stamping, but nothing pinned
    the PUBLISHERS. With that call gone, every ledger row written on the observation path --
    the busiest path in the run -- silently loses its stamp, and link 3 goes quietly
    unmeasurable on exactly the observations that matter most. A missing publisher produces no
    error, no empty value, and no failing test: it produces an ABSENT key, which by design
    reads as NOT-EVALUABLE. That is the worst possible failure shape, because it is
    indistinguishable from the honest unknown.

    So the invariant is structural: every call that opens a decision must publish it first.

    REWRITTEN 2026-07-27 (SEV-2): the first version anchored on `prepare_next_inference(`
    call sites with a 3000-char look-behind window. But there are MORE opening sites than
    prepare sites, so one opener/publisher pair was covered by NO window: deleting the
    UNCONDITIONAL publisher in `_commitment_context` left the pre-commit prepare site's
    window satisfied by the submit-boundary publisher instead -- a publish that sits inside
    `if submit_envelopes:` inside a SUBMIT-only branch and may never execute at runtime. The
    mutation this test exists to kill survived it (proven by deleting that publisher and
    watching this test stay green). So the anchors are now the OPENING constructions
    themselves -- every `active = self._active_decision(...)` / `active = ActiveDecision(...)`
    -- and each opener must be followed by its OWN publisher before the next opener begins.
    Window contamination between sites is structurally impossible.
    """
    import re

    src = inspect.getsource(seam)
    openers = [
        match.start()
        for match in re.finditer(
            r"\bactive = (?:self\._active_decision|ActiveDecision)\(", src
        )
    ]
    assert openers, "POSITIVE CONTROL: no decision-opening construction sites found at all"
    # EXACT CENSUS, updated deliberately or not at all: 5 openers =
    #   _commitment_context (pre-execution) + _commitment_context (submit re-open) +
    #   observe_action_result (row-time, pre-producer) + observe_action_result
    #   (post-ingestion) + _stage_initial_canonical_evidence (task start).
    # A recount here means an opening site was added or removed -- re-verify each has its
    # own publisher, then update this number in the same change.
    assert len(openers) == 5, (
        f"expected exactly 5 decision-opening sites, found {len(openers)} at lines "
        f"{[src[:i].count(chr(10)) + 1 for i in openers]} -- the opener census changed"
    )
    publishers = [
        match.start() for match in re.finditer(r"_publish_active_decision\(", src)
        if not src.startswith("def _publish_active_decision(", match.start() - 4)
    ]
    unpublished = []
    for index, opener in enumerate(openers):
        boundary = openers[index + 1] if index + 1 < len(openers) else len(src)
        if not any(opener < site < boundary for site in publishers):
            unpublished.append(src[:opener].count("\n") + 1)
    assert not unpublished, (
        f"decision-opening site(s) at line(s) {unpublished} construct an active decision "
        "without publishing it before the next opening site -- every ledger row written on "
        "that path loses gt_audit_active_decision silently (an ABSENT key, indistinguishable "
        "from the honest unknown)"
    )


def test_deep_reactive_rows_are_stamped_with_the_row_time_decision(monkeypatch, tmp_path):
    """DEFECT (SEV-1, found 2026-07-27): OFF-BY-ONE ON-TIME STAMP.

    In `observe_action_result`, the producer rows written by `gateway_produce_raw` and
    `_deep_reactive_envelopes` (covering_red / syntax / recovery -- the busiest producer rows
    in the run) stamp `gt_audit_active_decision` AT WRITE TIME via `_current_active_decision`.
    But the publish for this observation used to happen only AFTER those calls, so every
    producer row carried the decision published at the PREVIOUS boundary. Edit and test
    boundaries -- where phase transitions concentrate -- were exactly the rows misdated.
    A wrong stamp is worse than an absent one: absent reads NOT-EVALUABLE, wrong reads as a
    measurement.

    THE ORACLE IS THE SEAM'S OWN SINGLE FORMULA, not a second phase->context table: after the
    observation, `_active_decision` (static, derives the open decision from work_state which
    `append_event` advanced BEFORE the producers ran, never from the records) is called on the
    same work state the producers saw. The stamp captured DURING deep-reactive must equal it.
    """
    import json

    ledger = tmp_path / "gt_runtime_ledger.jsonl"
    (tmp_path / "src").mkdir()
    source = tmp_path / "src" / "api.py"
    source.write_text("def get_user(uid):\n    return uid\n", encoding="utf-8")

    monkeypatch.setenv("GT_RUNTIME_LEDGER", str(ledger))
    monkeypatch.setattr(seam, "_CANONICAL_RUNTIME_ATTACHMENT", None)
    monkeypatch.setattr(seam, "_root", lambda: str(tmp_path))
    monkeypatch.setattr(seam, "_db_path", lambda: str(tmp_path / "graph.db"))

    class _Model:
        def _prepare_messages_for_api(self, messages):
            return messages

        def _query(self, messages, **kwargs):
            raise AssertionError("this test verifies stamping, not provider dispatch")

        def _gt_exact_provider_payload(self, messages, kwargs):
            return {"model": "fixture", "messages": messages, **kwargs}

    class _Agent:
        def add_messages(self, *messages):
            return list(messages)

        def execute_actions(self, _message):
            return []

    attachment = seam.install_canonical_runtime(
        model=_Model(),
        agent=_Agent(),
        env={
            "GT_ATTEMPT_ID": "attempt-row-time-stamp",
            "GT_RUNTIME_LEDGER": str(ledger),
            "GT_BRIEF_FILE": str(tmp_path / "missing-brief.txt"),
        },
        task="repair get_user",
    )
    assert attachment.attached, "positive control: the canonical runtime did not attach"
    try:
        # Simulate the publish from the PREVIOUS boundary: a real context, but not the one
        # open at this observation. Pre-fix, the deep-reactive rows inherit exactly this.
        attachment._active_decision_context = "FAILURE_RECOVERY"

        captured = {}
        original = attachment._deep_reactive_envelopes

        def probe(*, changed_files, revision, graph_fresh=True):
            captured["stamp"] = seam._current_active_decision()
            # Write a REAL row through the production stamping path at the exact moment
            # the deep-reactive producers write theirs.
            seam._runtime_ledger_record(
                kind="l3.contract",
                outcome="delivered",
                reason="row-time probe",
                chars=7,
            )
            return original(
                changed_files=changed_files,
                revision=revision,
                graph_fresh=graph_fresh,
            )

        monkeypatch.setattr(attachment, "_deep_reactive_envelopes", probe)

        view = {"command": "view", "path": "src/api.py"}
        attachment.observe_action_proposal(view)
        attachment.observe_action_result(
            view,
            {"output": source.read_text(encoding="utf-8"), "returncode": 0},
        )

        assert captured, "positive control: deep-reactive was never reached"
        work_state = attachment.attempt_runtime.work_state
        expected = seam.CanonicalRuntimeAttachment._active_decision(
            (),
            work_state,
            work_state.revision,
        ).context.name
        # Positive control on the probe design itself: the seeded stale context must differ
        # from the decision genuinely open at this observation, or the test proves nothing.
        assert expected != "FAILURE_RECOVERY", (
            "probe design broken: the seeded stale context equals the true one"
        )
        assert captured["stamp"] == expected, (
            f"deep-reactive rows are stamped {captured['stamp']!r} -- the decision published "
            f"at the PREVIOUS boundary -- not {expected!r}, the decision open when this "
            "observation's rows were written. Link 3 misdates every producer row at a phase "
            "transition."
        )
        rows = [
            json.loads(line)
            for line in ledger.read_text(encoding="utf-8").splitlines()
            if line
        ]
        probe_rows = [row for row in rows if row.get("reason") == "row-time probe"]
        assert len(probe_rows) == 1, "positive control: the probe row was not written"
        assert probe_rows[0].get("gt_audit_active_decision") == expected, (
            "the durable ledger row carries the previous boundary's decision"
        )
    finally:
        attachment.attempt_runtime.journal.close()


def test_the_seam_does_not_impose_one_global_commitment_window():
    """The scheduler derives OPEN versus HELD for each record/current decision."""
    src = inspect.getsource(seam)
    assert "commitment_window=CommitmentWindowState.OPEN" not in src
    assert "TemporalPredicate.COMMITMENT_WINDOW_OPEN" not in src
