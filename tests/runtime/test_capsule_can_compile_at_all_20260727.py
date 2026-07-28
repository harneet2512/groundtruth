"""C14 — the POSITIVE CONTROL for the whole delivery chain: can a capsule compile AT ALL?

WHY THIS FILE EXISTS. Every run's fingerprint is `store=1, coalition=0` and every compilation row
reads `FAILED:DECISION_INCOMPLETE` (`reasoning_runtime.py:5428-5441`). That was recorded as C14 —
"DECISION_INCOMPLETE is the real killer" — implying the compiler itself refuses to serve.

**That framing is wrong, and this test is the artifact that shows it.** Given ONE record that clears
the temporal gate, `select_evidence_coalition` completes and `compile_observation_capsule` returns
`COMPILED` with real bytes and no failure code. The gate -> coalition -> compiler path WORKS.

So `DECISION_INCOMPLETE` is not a defect in the compiler; it is the compiler *correctly reporting*
that nothing upstream ever became relevant. The blocker is C12 (the possession test) plus store
population — not this stage. Fixing the compiler would have been wasted work, and "harden the
compiler" is exactly the plausible-but-wrong task this control prevents.

WITHOUT THIS CONTROL the `DECISION_INCOMPLETE` zero is unreadable: it cannot distinguish "the
compiler refuses" from "nothing was ever offered to it". Reading that zero as the former is how a
day gets spent on the wrong stage — the same mistake as the acquisition-counter zero (C15) and the
observation-binding zero (C13), both of which were also unreadable for the same reason.

NOTE ON A NEAR-MISS IN WRITING THIS FILE: the first probe read the capsule size via
`getattr(cap, "rendered", "")` and got 0. `rendered` is not a field — the text lives in
`capsule_text`. A zero from a misspelled attribute looks exactly like a zero from an empty capsule.
The field list is asserted below so this cannot silently regress.
"""

from __future__ import annotations

import dataclasses

from artifact_deepswe import gt_mini_patch as seam
from groundtruth.runtime import reasoning_runtime as rr


REVISION = rr.RevisionVector(
    repository_content="repo-1", graph="graph-1", lsp="lsp-1", runtime_evidence="runtime-1"
)

SATISFIED = frozenset(
    {
        rr.TemporalPredicate.PRODUCER_COMPUTATION_COMPLETE,
        rr.TemporalPredicate.REVISION_DEPENDENCIES_CAPTURED,
        rr.TemporalPredicate.ACTIVE_DECISION_CONTEXT_MATCHES,
        rr.TemporalPredicate.ACTIVE_DECISION_ID_MATCHES,
        rr.TemporalPredicate.REASONING_GRAPH_CONNECTED,
        rr.TemporalPredicate.COMMITMENT_WINDOW_OPEN,
    }
)

OPENED = "src/pkg/opened.py"


def _decision():
    work_state = dataclasses.replace(
        rr.WorkState.initial(attempt_id="attempt-1", revision=REVISION),
        focused_files=(OPENED,),
    )
    return seam.CanonicalRuntimeAttachment._active_decision((), work_state, REVISION, ())


def _record(feature_id: str, subject: str) -> rr.EvidenceRecord:
    contract = rr.feature_contract_for(feature_id)
    return rr.EvidenceRecord(
        evidence_id=f"ev-{feature_id}",
        feature_id=feature_id,
        decision_context=contract.decision_context,
        roles=contract.roles,
        subject=subject,
        claim=f"{feature_id} claim about {subject}",
        actionable_consequence=f"edit {subject}",
        provenance=(f"{subject}:7",),
        grade=rr.EvidenceGrade.VERIFIED,
        revision=REVISION,
        causal_neighborhood=(
            f"decision:{contract.decision_context.value}",
            f"fact:{feature_id}",
            f"subject:{subject}",
        ),
        lifecycle=rr.EvidenceLifecycle.READY,
        fresh=True,
        already_visible=False,
        superseded=False,
        mandatory_reason=None,
        revision_dependencies=contract.revision_dependencies,
        token_cost=120,
        failure_prevention=3,
        causal_value=3,
        contradiction_resolution=0,
        anchoring_risk=0,
        observed_substrates=tuple(
            sorted(
                set(contract.fallback_policy.preferred_substrates)
                or set(contract.fallback_policy.fallback_substrates)
            )
        ),
    )


def _stage(record, decision):
    """Reproduce the runtime's real order: temporal gate, then coalition."""
    contract = rr.feature_contract_for(record.feature_id)
    evaluation = rr.evaluate_feature_contract(
        contract,
        record,
        rr.TemporalRuntimeContext(
            active_decision=decision,
            satisfied_predicates=SATISFIED,
            commitment_window=rr.CommitmentWindowState.OPEN,
            current_revision=REVISION,
            available_substrates=contract.fallback_policy.preferred_substrates,
        ),
    )
    return dataclasses.replace(
        record,
        lifecycle=(
            rr.EvidenceLifecycle.READY
            if evaluation.release_allowed
            else rr.EvidenceLifecycle.HELD
        ),
    )


def _compile(decision, staged, observation_id="obs-1", model_call_id="mc-1"):
    oracle = rr.select_evidence_coalition(decision, staged)
    return oracle, rr.compile_observation_capsule(
        native_observation="ls -la\n",
        decision=oracle,
        observation_id=observation_id,
        source_model_call_id="mc-0",
        model_call_id=model_call_id,
        enabled=True,
    )


def test_the_capsule_text_field_is_named_capsule_text():
    """Pins the near-miss described in the docstring: a zero read off a misspelled attribute is
    indistinguishable from a zero read off an empty capsule."""
    names = {f.name for f in dataclasses.fields(rr.CapsuleCompilation)}
    assert "capsule_text" in names
    assert "rendered" not in names, "if this field is renamed, fix the readers, not this test"


def test_POSITIVE_CONTROL_a_capsule_does_compile_from_one_relevant_record():
    """THE CONTROL C14 WAS MISSING. One record that clears the gate is sufficient: the coalition
    completes and the compiler emits COMPILED with no failure code. Therefore
    FAILED:DECISION_INCOMPLETE in production is the compiler correctly reporting empty input, NOT
    a compiler defect."""
    decision = _decision()
    staged = [_stage(_record("localization", OPENED), decision)]

    oracle, capsule = _compile(decision, staged)

    assert oracle.decision_complete is True
    assert oracle.release_allowed is True
    assert not oracle.unresolved_roles
    assert len(oracle.coalition) == 1

    assert capsule.state is rr.CapsuleCompilationState.COMPILED
    assert not capsule.failure_code
    assert capsule.capsule_text, "compiled capsule carries no bytes"
    assert capsule.capsule_hash


def test_the_NEGATIVE_state_is_reachable_too_so_the_control_means_something():
    """A control that can only ever pass proves nothing. Same pipeline, one variable changed —
    the record's subject is a file the agent never opened, so the C12 possession gate holds it —
    and the compiler now reports exactly the production failure code."""
    decision = _decision()
    staged = [_stage(_record("localization", "src/pkg/never_opened.py"), decision)]

    oracle, capsule = _compile(decision, staged)

    assert oracle.decision_complete is False
    assert capsule.state is not rr.CapsuleCompilationState.COMPILED
    assert capsule.failure_code == "DECISION_INCOMPLETE"


def test_SOURCE_TARGET_SELECTION_depends_on_localization_class_evidence():
    """Pins the structural coupling C14 records, which contradicts CLAUDE.md §6.1's claim that all
    17 features are independent. The open decision at a view boundary REQUIRES TARGET_IDENTITY, and
    only localization / def_partition / newfile_precedent carry that role — so every other feature's
    delivery at this boundary has a hard dependency on localization-class evidence being present."""
    decision = _decision()
    assert decision.context is rr.DecisionContext.SOURCE_TARGET_SELECTION

    required = set(decision.required_roles)
    carriers = {
        fid
        for fid in ("localization", "def_partition", "newfile_precedent", "obligations",
                    "caller_contract", "covering_red", "signature_delta", "recovery",
                    "syntax_result", "submit_refusal")
        if set(rr.feature_contract_for(fid).roles) & required
    }
    assert carriers == {"localization", "def_partition", "newfile_precedent"}, (
        f"the set of TARGET_IDENTITY carriers changed: {sorted(carriers)}"
    )
