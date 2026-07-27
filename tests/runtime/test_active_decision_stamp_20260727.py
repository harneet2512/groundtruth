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
    """
    src = inspect.getsource(seam)
    sites = [i for i in range(len(src)) if src.startswith("prepare_next_inference(", i)]
    assert sites, "POSITIVE CONTROL: no prepare_next_inference call sites found at all"
    unpublished = []
    for idx in sites:
        window = src[max(0, idx - 3000):idx]
        if "_publish_active_decision(" not in window:
            line = src[:idx].count("\n") + 1
            unpublished.append(line)
    assert not unpublished, (
        f"prepare_next_inference site(s) at line(s) {unpublished} open a decision without "
        "publishing it -- rows written on that path lose gt_audit_active_decision silently"
    )


def test_the_seam_still_passes_OPEN_to_the_runtime():
    """STAGE-1 SCOPE FENCE. This change records a fact; it must not yet alter delivery.

    If this assertion ever fails, someone has begun Stage 2 (enforcing the window) inside a
    change that was only supposed to observe it. Stage 2 requires the recorded distribution to
    show DECISION_WINDOW_EXPIRED and READINESS_RULES_SATISFIED actually firing first -- a gate
    that never bites proves nothing, and a gate that bites WRONGLY suppresses real evidence.
    """
    src = inspect.getsource(seam)
    assert src.count("commitment_window=CommitmentWindowState.OPEN") == 3, (
        "the number of hardcoded-OPEN construction sites changed; if this is Stage 2, "
        "re-derive the enforcement from the recorded data and update this fence deliberately"
    )
