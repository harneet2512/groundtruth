"""mini-swe-agent seam adapter for the GT Gateway (W2 — the one-call wiring).

The mini seam (``artifact_deepswe/gt_mini_patch.py``) completes one tool
observation with deterministic FACTS by making ONE ``gateway.augment()`` call and
appending the result BYTE-PRESERVING into the SAME observation the model already
reads. This module owns every decision-shaped step of that pipeline so it is
unit-testable in isolation, WITHOUT importing gt_mini_patch (importing it runs
runtime monkeypatches):

    normalize_event  raw (cmd, output, returncode, index) -> gateway.ToolEvent
    augment          (gateway.augment — the ONE call, done by the caller)
    arbitrate        many envelopes -> at most ONE (the per-turn dose, ABI §2)
    render_envelope  one envelope -> model-facing bytes (tagged default / native)
    fits_budget      TITO law 8 — refuse a partial/over-budget delta whole
    seal_delivery    stamp TITO metadata + extend the per-episode hash-chain
    update_receipts  TITO law 7 — promote receipt_state (delivered->referenced->acted)

PURE · DETERMINISTIC · LLM-FREE · stdlib + gateway/evidence_envelope/native_render only.
No time, no randomness, no I/O. The observation splice (mutating the mini's ``out`` dict) and
the leak-guard on the rendered bytes stay on the SEAM side — this module never
touches a harness object and never renders a ``<gt-*>`` tag it cannot leak-check.

SM-2b (2026-07-11): :func:`render_envelope` NATIVE dispatch. Under ``native=True`` a live
gateway fact class routes to its SM-1 per-class native renderer (``native_render``) — a real
tool-channel grammar (a CPython traceback frame, a compiler/type-checker CONTRACT diagnostic)
— instead of tag-free English narration. ``_render_generic`` stays the final fallback for a
class with no native renderer. The SM-1 renderers were BUILT but never wired (SM-2 LIPI
Finding 2); this closes that seam.

TITO CONTRACT (evidence_envelope §"TITO"): every delivery is byte/prefix-preserving.
:func:`seal_delivery` records ``rendered_bytes_hash`` = sha256 of the bytes ACTUALLY
appended and threads ``parent_observation_hash`` through
``evidence_envelope.chain_hash`` over the episode's delivery sequence, so a replayed
trajectory reproduces the exact appended bytes (law 5/6). The chain is PER-EPISODE:
a fresh attempt starts at genesis and re-sees facts (ABI dedup keys are
episode-invariant by design; the chain head is not).
"""
from __future__ import annotations

import dataclasses
import hashlib
import re
from collections.abc import Callable, Mapping

from groundtruth.runtime.evidence_envelope import (
    ObservationBinding,
    RECEIPT_ACTED,
    RECEIPT_CAUSAL,
    RECEIPT_DELIVERED,
    RECEIPT_NONE,
    RECEIPT_RANK,
    RECEIPT_REFERENCED,
    RECEIPT_RESOLVED_STATE,
    EvidenceEnvelope,
    chain_hash,
)
from groundtruth.runtime.evidence_envelope import (
    HYPOTHESIS,
    INFO,
    VERIFIED,
    WARNING,
)
from groundtruth.runtime.gateway import (
    KIND_EDIT, KIND_SEARCH, KIND_SUBMIT, KIND_TEST, KIND_VIEW,
    CoveringResult, ToolEvent, classify_command)
from groundtruth.runtime.native_render import (
    render_body_concept_native,
    render_caller_contract_native,
    render_caller_usage_native,
    render_def_rows_native,
    render_note_rows_native,
    render_ranked_list_native,
    render_registration_native,
    render_scope_constraint_native,
    render_signature_delta_native,
    render_trace_frame_native,
)

__all__ = [
    "normalize_event",
    "canonicalize_action_proposal",
    "canonicalize_tool_result",
    "canonicalize_tool_event",
    "canonical_test_failure_fingerprint",
    "arbitrate",
    "render_envelope",
    "fits_budget",
    "seal_delivery",
    "update_receipts",
    "DEF_FACTS_KINDS",
    "DEFAULT_MAX_DELTA_CHARS",
]


def canonicalize_action_proposal(
    action,
    *,
    event_id: str,
    attempt_id: str,
    sequence: int,
    model_turn_id: str,
    observation_id: str,
    revision,
    previous_event_hash: str,
):
    """Commit one structured Mini-SWE action before native execution."""
    from groundtruth.runtime.reasoning_runtime import (
        ActionOperation,
        Authority,
        CanonicalEvent,
        CausalRef,
        CausalRefKind,
        EventKind,
        SemanticKind,
        SemanticOutcome,
    )

    proposal_kind = {
        ActionOperation.SEARCH: SemanticKind.SEARCH_REQUESTED,
        ActionOperation.VIEW_SOURCE: SemanticKind.SOURCE_READ_REQUESTED,
        ActionOperation.VIEW_SYMBOL: SemanticKind.SOURCE_READ_REQUESTED,
        ActionOperation.EDIT: SemanticKind.EDIT_PROPOSED,
        ActionOperation.TEST: SemanticKind.TEST_REQUESTED,
        ActionOperation.SIGNATURE_CHANGE: SemanticKind.EDIT_PROPOSED,
        ActionOperation.FILE_CREATE: SemanticKind.EDIT_PROPOSED,
        ActionOperation.FILE_DELETE: SemanticKind.EDIT_PROPOSED,
        ActionOperation.FILE_RENAME: SemanticKind.EDIT_PROPOSED,
        ActionOperation.SUBMIT: SemanticKind.SUBMIT_PROPOSED,
    }.get(action.operation)
    outcomes = (
        (
            SemanticOutcome(
                kind=proposal_kind,
                subject=action.subject,
                metadata=tuple(
                    (key, value)
                    for key, value in (
                        ("query", action.query),
                        ("targets", "|".join(action.targets)),
                    )
                    if value
                ),
                authority=Authority.STRUCTURED,
                provenance=("canonical_action",),
            ),
        )
        if proposal_kind is not None
        else ()
    )
    parents = (
        CausalRef(CausalRefKind.ACTION, action.action_id),
        CausalRef(CausalRefKind.MODEL_CALL, model_turn_id),
        CausalRef(CausalRefKind.OBSERVATION, observation_id),
    )
    return CanonicalEvent(
        event_id=event_id,
        attempt_id=attempt_id,
        sequence=sequence,
        kind=EventKind.ACTION_PROPOSED,
        authority=Authority.STRUCTURED,
        outcomes=outcomes,
        revision_before=revision,
        revision_after=revision,
        previous_event_hash=previous_event_hash,
        action_id=action.action_id,
        model_turn_id=model_turn_id,
        observation_id=observation_id,
        carrier=action.raw_command,
        parents=parents,
        action=action,
    )


def canonicalize_tool_result(
    carrier_event: ToolEvent,
    *,
    proposal,
    result,
    event_id: str,
    sequence: int,
    observation_id: str,
    revision_after,
    previous_event_hash: str,
):
    """Commit structured native outcome truth without parsing carrier bytes."""
    from groundtruth.runtime.reasoning_runtime import (
        ActionOperation,
        Authority,
        CanonicalEvent,
        CausalRef,
        CausalRefKind,
        EventKind,
        SemanticKind,
        SemanticOutcome,
    )

    if proposal.kind is not EventKind.ACTION_PROPOSED or proposal.action is None:
        raise ValueError("tool result requires a canonical action proposal")
    if previous_event_hash != proposal.content_hash:
        raise ValueError("tool result previous hash does not bind its proposal")
    action = proposal.action
    status = result.status.lower()
    provenance = ("canonical_action", "canonical_result")

    def outcome(
        kind,
        *,
        metadata=(),
        changed=None,
        subject=None,
    ):
        # `subject` defaults to the action's own subject (the path/target). A symbol-level
        # outcome must override it: the reducer files SYMBOL_VIEWED into `focused_symbols` by
        # subject, and the relevance intersection compares `subject:` tokens literally, so a
        # symbol outcome carrying the FILE path would populate focus with paths and still fail
        # to intersect symbol-named evidence.
        return SemanticOutcome(
            kind=kind,
            subject=action.subject if subject is None else subject,
            status=result.status,
            changed=changed,
            failure_fingerprint=result.failure_fingerprint,
            metadata=tuple(metadata),
            authority=Authority.STRUCTURED,
            provenance=provenance,
        )

    outcomes = ()
    if action.operation is ActionOperation.SEARCH:
        if status == "failed":
            kind = SemanticKind.SEARCH_FAILED
        elif result.hit_count == 0:
            kind = SemanticKind.SEARCH_EMPTY
        else:
            kind = SemanticKind.SEARCH_RESULT
        metadata = (
            ("query", action.query),
            *((("hit_count", str(result.hit_count)),)
              if result.hit_count is not None else ()),
            *((("files_hit", "|".join(result.files_hit)),)
              if result.files_hit else ()),
            # The graph-validated operand symbol. Deliberately metadata on SEARCH_RESULT and
            # NOT a SYMBOL_VIEWED outcome: that kind's reducer branch sets
            # phase = UNDERSTANDING, and a search must not advance the trajectory out of
            # DISCOVERY/LOCALIZATION -- `_active_decision` derives the open decision from the
            # phase, so a false advance would make the oracle reason about the wrong moment.
            # NOT on SEARCH_FAILED: that command did not run (bad flag, unreadable path),
            # so its operand is not evidence of what the agent is working on. SEARCH_EMPTY
            # IS kept -- a search that ran and found nothing is real interest, and those
            # are precisely the searches GT has most to add to.
            *((("resolved_symbols", "|".join(result.resolved_symbols)),)
              if result.resolved_symbols
              and kind is not SemanticKind.SEARCH_FAILED else ()),
        )
        outcomes = (outcome(kind, metadata=metadata),)
    elif action.operation is ActionOperation.VIEW_SOURCE:
        # The file AND the symbols the view actually showed. One action may emit several
        # outcomes -- EDIT already does exactly this (EDIT_EXECUTED + DIFF_CREATED below).
        #
        # `focused_symbols` has no other way to be populated on a shell harness: SYMBOL_VIEWED
        # is reachable only from ActionOperation.VIEW_SYMBOL, and no shell command can be
        # classified into that operation deterministically without semantic reading, which GT
        # forbids itself (LLM-free). So the symbols must arrive as authoritative RESULT data,
        # resolved against graph.db by the operation, not parsed out of the rendered output.
        #
        # Correct-or-quiet: an empty `viewed_symbols` emits nothing extra. A GUESSED symbol
        # would be worse than none -- it would admit unrelated evidence through the relevance
        # intersection, which is the precise failure mode correct-or-quiet exists to prevent.
        outcomes = (
            (
                outcome(SemanticKind.SOURCE_VIEWED),
                *(
                    outcome(
                        SemanticKind.SYMBOL_VIEWED,
                        subject=symbol,
                    )
                    for symbol in result.viewed_symbols
                ),
            )
            if action.subject
            else ()
        )
    elif action.operation is ActionOperation.VIEW_SYMBOL:
        outcomes = (outcome(SemanticKind.SYMBOL_VIEWED),)
    elif action.operation is ActionOperation.EDIT:
        outcomes = (
            (
                outcome(SemanticKind.EDIT_EXECUTED, changed=True),
                outcome(SemanticKind.DIFF_CREATED, changed=True),
            )
            if status == "success" and result.changed is True
            else (outcome(SemanticKind.EDIT_FAILED, changed=False),)
        )
    elif action.operation is ActionOperation.TEST:
        if status == "executed_no_tests":
            outcomes = (outcome(SemanticKind.TEST_EXECUTED_NO_TESTS),)
        elif status == "unobserved":
            outcomes = ()
        else:
            verdict = {
                "pass": SemanticKind.TEST_PASS,
                "success": SemanticKind.TEST_PASS,
                "fail": SemanticKind.TEST_FAIL,
                "failed": SemanticKind.TEST_FAIL,
                "env_fail": SemanticKind.TEST_ENV_FAIL,
            }.get(status)
            outcomes = (outcome(SemanticKind.TEST_RESULT),) + (
                (outcome(verdict),) if verdict is not None else ()
            )
    elif action.operation is ActionOperation.COMPILE:
        outcomes = (outcome(SemanticKind.COMPILE_RESULT),)
    elif action.operation is ActionOperation.SIGNATURE_CHANGE:
        outcomes = (
            outcome(SemanticKind.EDIT_EXECUTED, changed=True),
            outcome(
                SemanticKind.SIGNATURE_CHANGED,
                metadata=(
                    ("signature_before", result.signature_before),
                    ("signature_after", result.signature_after),
                ),
                changed=True,
            ),
        )
    elif action.operation is ActionOperation.FILE_CREATE:
        outcomes = (outcome(SemanticKind.FILE_CREATED, changed=True),)
    elif action.operation is ActionOperation.FILE_DELETE:
        outcomes = (outcome(SemanticKind.FILE_DELETED, changed=True),)
    elif action.operation is ActionOperation.FILE_RENAME:
        outcomes = (outcome(SemanticKind.FILE_RENAMED, changed=True),)
    elif action.operation is ActionOperation.SUBMIT:
        outcomes = (
            outcome(
                SemanticKind.SUBMIT_ACCEPTED
                if status == "accepted"
                else SemanticKind.SUBMIT_BLOCKED
            ),
        )

    parents = (
        CausalRef(CausalRefKind.EVENT, proposal.event_id),
        CausalRef(CausalRefKind.ACTION, action.action_id),
        CausalRef(CausalRefKind.MODEL_CALL, proposal.model_turn_id),
        CausalRef(CausalRefKind.OBSERVATION, observation_id),
    )
    return CanonicalEvent(
        event_id=event_id,
        attempt_id=proposal.attempt_id,
        sequence=sequence,
        kind=EventKind.ACTION_RESULT,
        authority=Authority.STRUCTURED,
        outcomes=outcomes,
        revision_before=proposal.revision_after,
        revision_after=revision_after,
        previous_event_hash=previous_event_hash,
        action_id=action.action_id,
        model_turn_id=proposal.model_turn_id,
        observation_id=observation_id,
        carrier=carrier_event.carrier_kind or carrier_event.kind or "",
        parents=parents,
        action=action,
        result=result,
    )


def canonicalize_tool_event(
    event: ToolEvent,
    *,
    event_id: str,
    attempt_id: str,
    sequence: int,
    action_id: str,
    model_turn_id: str,
    observation_id: str,
    revision_before,
    revision_after,
    previous_event_hash: str,
    parent_event_ids: "tuple[str, ...]" = (),
):
    """Translate an already-normalized ToolEvent without reparsing its carrier.

    Outcome authority is assigned per evidence surface: repository byte deltas
    own mutation claims, while structured result fields own test verdicts.
    """
    from groundtruth.runtime.reasoning_runtime import (
        Authority,
        CanonicalEvent,
        CausalRef,
        CausalRefKind,
        EventKind,
        SemanticKind,
        SemanticOutcome,
    )

    outcomes: list[SemanticOutcome] = []
    semantic_events = tuple(event.semantic_events or ())
    repository_changed = (
        revision_before.repository_content
        != revision_after.repository_content
    )
    failure_fingerprint = canonical_test_failure_fingerprint(event)
    for semantic_event in semantic_events:
        if semantic_event == "edit_result":
            if not repository_changed:
                continue
            for file_path in tuple(event.changed_files or ()):
                outcomes.extend(
                    (
                        SemanticOutcome(
                            kind=SemanticKind.EDIT_EXECUTED,
                            subject=file_path,
                            changed=True,
                            authority=Authority.REPOSITORY_DELTA,
                            provenance=("changed_files",),
                        ),
                        SemanticOutcome(
                            kind=SemanticKind.DIFF_CREATED,
                            subject=file_path,
                            changed=True,
                            authority=Authority.REPOSITORY_DELTA,
                            provenance=("changed_files",),
                        ),
                    )
                )
        elif semantic_event == "test_executed_no_tests":
            if event.test_outcome == "executed_no_tests":
                outcomes.append(
                    SemanticOutcome(
                        kind=SemanticKind.TEST_EXECUTED_NO_TESTS,
                        status=event.test_outcome,
                        authority=Authority.RESULT_DERIVED,
                        provenance=(
                            "test_outcome",
                            "test_protocol",
                            "exit_status",
                        ),
                    )
                )
        elif semantic_event == "test_result":
            if event.test_outcome in {"executed_no_tests", "unobserved"}:
                continue
            provenance = ("test_outcome", "test_protocol", "exit_status")
            outcomes.append(
                SemanticOutcome(
                    kind=SemanticKind.TEST_RESULT,
                    status=event.test_outcome or "",
                    authority=Authority.RESULT_DERIVED,
                    provenance=provenance,
                )
            )
            verdict_kind = {
                "pass": SemanticKind.TEST_PASS,
                "fail": SemanticKind.TEST_FAIL,
                "env_fail": SemanticKind.TEST_ENV_FAIL,
            }.get(event.test_outcome or "")
            if verdict_kind is not None:
                outcomes.append(
                    SemanticOutcome(
                        kind=verdict_kind,
                        status=event.test_outcome or "",
                        failure_fingerprint=failure_fingerprint,
                        authority=Authority.RESULT_DERIVED,
                        provenance=provenance,
                    )
                )
        elif semantic_event == "file_view":
            viewed_files = tuple(event.viewed_files or ())
            if viewed_files:
                outcomes.extend(
                    SemanticOutcome(
                        kind=SemanticKind.SOURCE_VIEWED,
                        subject=file_path,
                        authority=Authority.STRUCTURED,
                        provenance=("viewed_files",),
                    )
                    for file_path in viewed_files
                )
            elif not event.semantics_authoritative:
                outcomes.append(
                    SemanticOutcome(
                        kind=SemanticKind.SOURCE_VIEWED,
                        authority=Authority.RESULT_DERIVED,
                        provenance=("semantic_events",),
                    )
                )
        elif semantic_event == "search_result":
            outcomes.append(
                SemanticOutcome(
                    kind=SemanticKind.SEARCH_RESULT,
                    status="success",
                    authority=Authority.RESULT_DERIVED,
                    provenance=("semantic_events", "exit_status"),
                )
            )
        elif semantic_event == "failed_search":
            outcomes.append(
                SemanticOutcome(
                    kind=SemanticKind.SEARCH_EMPTY,
                    status="empty",
                    authority=Authority.RESULT_DERIVED,
                    provenance=("semantic_events", "exit_status"),
                )
            )
        elif semantic_event == "submit":
            outcomes.append(
                SemanticOutcome(
                    kind=SemanticKind.SUBMIT_PROPOSED,
                    authority=Authority.STRUCTURED,
                    provenance=("semantic_events",),
                )
            )

    parents = tuple(
        CausalRef(CausalRefKind.EVENT, parent_id)
        for parent_id in parent_event_ids
    ) + (
        CausalRef(CausalRefKind.ACTION, action_id),
        CausalRef(CausalRefKind.MODEL_CALL, model_turn_id),
        CausalRef(CausalRefKind.OBSERVATION, observation_id),
    )
    return CanonicalEvent(
        event_id=event_id,
        attempt_id=attempt_id,
        sequence=sequence,
        kind=EventKind.OBSERVATION_COMMITTED,
        authority=(
            Authority.STRUCTURED
            if event.semantics_authoritative
            else Authority.RESULT_DERIVED
        ),
        outcomes=tuple(outcomes),
        revision_before=revision_before,
        revision_after=revision_after,
        previous_event_hash=previous_event_hash,
        action_id=action_id,
        model_turn_id=model_turn_id,
        observation_id=observation_id,
        carrier=event.carrier_kind or event.kind or "",
        parents=parents,
    )

# The def-facts kinds ROUTED to the mini's own ``_fmt_def_facts`` /
# ``_fmt_def_facts_native`` via the injected ``def_facts_renderer`` (never
# re-implemented here). P5 honesty note (bounce 2026-07-10): the reuse HOLDS for
# ``def_ref_partition`` (fact_id = the resolved symbol -> the renderer re-resolves
# it). For ``name_fold`` the fact_id is the ORIGINAL unresolved spelling, so the
# injected renderer abstains ('' -> generic fallback — correct-or-quiet, the fold
# note survives in the payload). For ``wrong_surface`` the renderer emits the plain
# def-facts block (the def sites), dropping the wrong-surface note — acceptable:
# the def site IS the actionable fact. Absent a renderer (host/unit tests without
# the mini loaded), all three render generic.
DEF_FACTS_KINDS = frozenset({"def_ref_partition", "name_fold", "wrong_surface"})

# TITO law 8 default budget: a delta longer than this is DROPPED WHOLE (never a
# partial/fabricated splice). The seam may pass a tighter remaining budget.
DEFAULT_MAX_DELTA_CHARS = 4000


def canonical_test_failure_fingerprint(event: ToolEvent) -> str:
    """Return a stable host-only identity for one observed failing test result.

    ``test_outcome`` is the authority that says the observation is a failure.
    Output bytes supply only the repeat identity.  Volatile paths, addresses and
    numbers are removed so an unchanged failure remains comparable across runs.
    The value is never rendered or treated as a repository fact.
    """
    if event.test_outcome not in {"fail", "env_fail"}:
        return ""
    markers = (
        "error",
        "failed",
        "failure",
        "exception",
        "traceback",
        "assert",
        "fatal",
        "not found",
        "cannot",
        "no such",
        "panic",
    )
    significant = tuple(
        line.strip()
        for line in (event.output or "").splitlines()
        if any(marker in line.lower() for marker in markers)
    )
    if not significant:
        return ""
    signature = "\n".join(significant[-8:])
    signature = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", signature)
    signature = re.sub(r"0x[0-9a-fA-F]+|\d+", "", signature)
    signature = re.sub(r"[\w.]*[/\\][\w./\\-]+", "", signature)
    signature = " ".join(signature.split())
    if not signature:
        return ""
    return hashlib.sha256(
        signature.encode("utf-8", "replace")
    ).hexdigest()[:16]


# --------------------------------------------------------------------------- #
# 1. normalize — raw seam values -> the gateway's ONE interception unit
# --------------------------------------------------------------------------- #
def normalize_event(
    command: str,
    output: str,
    returncode: "int | None",
    action_index: int,
    *,
    cwd: str = "",
    changed_files: "tuple[str, ...]" = (),
    viewed_files: "tuple[str, ...]" = (),
    edit_before_after: "Mapping[str, tuple[str | None, str]] | None" = None,
    covering: "CoveringResult | None" = None,
    semantic_events: "tuple[str, ...] | None" = None,
    primary_boundary: str = "",
    test_outcome: str = "",
    test_protocol: str = "",
    state_revision: str = "",
) -> ToolEvent:
    """Build the gateway :class:`ToolEvent` from the mini's raw per-turn values.

    The kind is classified by ``gateway.classify_command`` (the segment-head rule
    proven byte-equivalent to the mini's own parser), so the seam and a direct
    ``augment`` call agree on the event shape."""
    carrier = classify_command(command or "")
    observed = tuple(dict.fromkeys(semantic_events or ()))
    exact_viewed = tuple(dict.fromkeys(viewed_files or ()))
    if semantic_events is None and exact_viewed:
        observed = ("file_view",)
    authoritative = semantic_events is not None or bool(exact_viewed)
    if not test_outcome:
        try:
            from groundtruth.runtime.patterns import classify_test_observation
            test_outcome, test_protocol = classify_test_observation(
                command or "", output or "", returncode)
        except Exception:  # noqa: BLE001 -- normalization stays correct-or-quiet
            test_outcome = test_protocol = ""
    if not authoritative:
        if test_outcome in {"pass", "fail", "env_fail"}:
            observed = ("test_result",)
        elif test_outcome == "executed_no_tests":
            observed = ("test_executed_no_tests",)
        elif carrier == KIND_EDIT and (changed_files or edit_before_after):
            observed = ("edit_result",)
        elif exact_viewed or carrier == KIND_VIEW:
            observed = ("file_view",)
        elif carrier == KIND_SUBMIT:
            observed = ("submit",)
        elif carrier == KIND_SEARCH:
            # Outcome-specific search semantics are completed in gateway where
            # the canonical grep-result parser lives.
            observed = ()
    if not primary_boundary and observed:
        primary_boundary = observed[0]
    return ToolEvent(
        kind=carrier,
        command=command or "",
        output=output or "",
        exit_status=(None if returncode is None else int(returncode)),
        cwd=cwd or "",
        changed_files=tuple(changed_files or ()),
        viewed_files=exact_viewed,
        action_index=int(action_index),
        edit_before_after=edit_before_after,
        covering=covering,
        carrier_kind=carrier,
        semantic_events=observed,
        primary_boundary=primary_boundary,
        test_outcome=test_outcome,
        test_protocol=test_protocol,
        state_revision=state_revision,
        semantics_authoritative=authoritative,
    )


# --------------------------------------------------------------------------- #
# 2. arbitrate — the per-turn dose (<= 1 steer), deterministic
# --------------------------------------------------------------------------- #
# Severity of a produced FACT class (ABI §2 ordering, projected onto the
# envelope's evidence_type). Higher wins the single dose. An unknown kind gets a
# low floor so a new producer never silently out-ranks a verified world-fact.
_EVIDENCE_TYPE_RANK: dict[str, int] = {
    "covering_verdict": 60,       # executed world-fact (the repo's own RED)
    "signature_mismatch": 50,     # edit-bound contract break (patch_delta, Python-only)
    "caller_break": 48,           # cross-language caller-contract break (below the narrower
    "caller_contract_view": 48,   # the same contract, attached before mutation on source view
    #                               signature_mismatch — an arity diagnostic is more specific —
    #                               but above wrong_surface/trace so it wins an edit turn)
    "wrong_surface": 45,          # every hit is a test/vendored copy
    "trace_frame": 40,            # deepest in-repo stack frame
    # T0->T2 re-slot (2026-07-12): GT's ranked localization ANSWER at post-search. ABOVE
    # def_ref_partition (a ranked FILE answer wins the FIRST search dose, before the agent
    # has a bare symbol to complete), BELOW trace_frame/wrong_surface (a concrete crash
    # frame / "wrong copy" still out-doses a ranked hypothesis). ~37 is a MEASUREMENT-GATED
    # starting point (tunable via measure_brief on the stratum-B testset), NOT a fixture
    # constant / a ranking weight (localize()'s ranking is untouched).
    "localization": 37,
    "def_ref_partition": 35,      # def/ref partition on an ambiguous/flood grep
    "name_fold": 34,              # indexed under a fold variant
    "companion_surface": 30,      # registration/companion gap
    "missing_role": 22,           # change_surface missing role (hypothesis)
    "missing_role_postcreate": 22,  # same fact, honest post-creation edit boundary
    "body_concept": 20,           # vocabulary-gap function bodies
    "new_file_destination": 15,   # feature-add destination (hypothesis)
    "cochange_partner": 12,       # co-change partner
}
_TIER_RANK: dict[str, int] = {VERIFIED: 3, WARNING: 2, INFO: 1, HYPOTHESIS: 0}


def _xsession_boost(env: EvidenceEnvelope) -> int:
    """The cross-session rank-up boost the Gateway stamped onto ``env.native_args`` for a
    historically-CONSUMED class (reserved key ``_xsession_boost``; SM-9c winner promotion,
    ``gateway._apply_xsession_rankup``). ``0`` when absent / malformed / negative — so an
    envelope the Gateway did NOT stamp (feature off, empty policy, non-policy class) leaves
    ``_priority`` byte-identical. Clamped ``>= 0``: a memory signal only RANKS a fact UP among
    already-eligible candidates, never demotes one."""
    na = env.native_args
    if not na:
        return 0
    try:
        return max(0, int(na.get("_xsession_boost") or 0))
    except (TypeError, ValueError):
        return 0


# WS-1 (2026-07-23) rotation: a fact class that ALREADY delivered earlier in THIS attempt is
# demoted below every not-yet-delivered class, so the next observation's <=1 dose rotates to a
# starved class (def_partition after localization) instead of the same argmax winner every turn.
# Large enough to sink any delivered class beneath any fresh one across the whole rank span
# (12..60); the seam passes the accumulated delivered evidence_types, default empty => no decay
# => byte-identical to the pre-WS-1 order.
_ROTATION_DECAY = 1000


# EXECUTED world-facts: the repo's own toolchain actually RAN and produced this. They are exempt
# from boundary matching for the same reason `_filter_candidates_by_phase` exempts them from the
# phase gate — a verified world-fact is valid whenever it exists, not only at one boundary. Kept as
# an explicit frozenset (not a heuristic) so adding one is a deliberate, reviewable act.
_WORLD_FACT_EVIDENCE_TYPES = frozenset({"covering_verdict", "syntax_result", "edit_syntax"})


def _boundary_match(env: EvidenceEnvelope, observed_event: "str | None") -> int:
    """1 iff this fact's REGISTERED delivery boundary is the observation that just occurred.

    GT_BOUNDARY_SPECIFICITY (default off => byte-identical: returns 0 without an observed_event).

    WHY LEXICOGRAPHIC AND NOT AN ADDITIVE BOOST. `_EVIDENCE_TYPE_RANK` gaps are small (60, 50, 48,
    ... 22, 15) and the covering-RED lead over the next class is only 10. Letting a contracted fact
    win its OWN boundary would need a boost of 26+ (missing_role 22 vs caller_break 48), which would
    also let an advisory out-dose an EXECUTED world-fact — the exact thing `_xsession_boost` is
    capped to prevent. Returning a separate leading tuple element avoids the conflict entirely:
    matched candidates sort above unmatched ones, and the existing severity order still decides
    WITHIN the matched set, so covering_verdict keeps its dominance wherever it legitimately matches.

    The policy is NOT invented here. `fact_registry.required_event()` resolves the boundary from the
    envelope's own `evidence_type`, per a contract written and reviewed independently of any
    benchmark — which is what keeps this out of hand-tuned/benchmaxx territory. An unregistered or
    unresolvable type scores 0 (correct-or-quiet): it competes exactly as it does today."""
    if not observed_event:
        return 0
    et = env.evidence_type or ""
    # EXECUTED WORLD-FACTS ARE BOUNDARY-EXEMPT. An executed covering-RED is the repo's OWN test
    # having run and failed on the agent's edit — it is not an advisory owed at a boundary, it is
    # a fact about the world that is valid whenever it exists. It is PRODUCED post-EDIT (the
    # producer even steps aside when the agent just ran a test) while its registration names
    # `test_result`, so a naive boundary match would score it 0 and let a caller/signature advisory
    # out-dose it on an edit turn — the exact inversion the lexicographic design exists to prevent.
    # This is not a new carve-out: `_filter_candidates_by_phase` already exempts the SAME class of
    # fact from the phase gate for the same reason ("a verified WORLD-FACT ... valid in ANY phase").
    if et in _WORLD_FACT_EVIDENCE_TYPES:
        return 1
    try:
        from groundtruth.runtime.fact_registry import required_event
        if required_event(et) == observed_event:
            return 1
    except Exception:  # noqa: BLE001 -- unregistered/unresolvable type falls through
        pass
    # STEERS declare their boundary in a DIFFERENT table. `fact_registry` covers the 11 FACT
    # classes; the verify.horizon.* steers (GT_HYPOTHESIS owns verify.horizon.pivot) are
    # event-bound in `context_policy.EVENT_BOUND_PAYLOADS` instead — pivot to TEST_RESULT,
    # advisory/urgent/gate to REVIEW_TRANSITION, gate also to PRE_SUBMIT. Consulting only the
    # fact registry scored every steer 0, so a contracted FACT would always out-rank a steer that
    # was in fact contracted for this very observation. Two contract tables, one question: is this
    # payload owed HERE? Both are declarative and reviewed; neither is invented here.
    try:
        from groundtruth.runtime.context_policy import EVENT_BOUND_PAYLOADS
        for event, payloads in EVENT_BOUND_PAYLOADS.items():
            if et in payloads and getattr(event, "value", None) == observed_event:
                return 1
    except Exception:  # noqa: BLE001 -- unresolvable binding ranks as today
        pass
    return 0


# The ONLY boundaries `boundary_for_event` can derive from a tool event. Expiry may fire only on a
# contract inside this set: a fact contracted elsewhere can never be OBSERVED at its own boundary, so
# treating "not equal" as expired would delete it at every observation. Keep in sync with
# boundary_for_event — a test asserts they agree.
_DERIVABLE_BOUNDARIES = frozenset({
    "edit_result", "test_result", "file_view", "search_result", "failed_search"})

BOUNDARY_MATCH = "match"
BOUNDARY_MISMATCH = "mismatch"
BOUNDARY_UNKNOWN = "unknown"


def boundary_verdict(env: EvidenceEnvelope, observed_event: "str | None") -> str:
    """THREE-state boundary judgement for the EXPIRY gate (PURE).

    `_boundary_match` collapses to 0/1 because RANKING only needs "is this the contracted fact".
    An EXPIRY gate cannot use that: 0 conflates "contracted elsewhere, so this is expired" with
    "no contract found, so we know nothing". Blocking the second would DELETE evidence on an
    unknown — the worst failure mode available, and strictly worse than the ranking bugs #29
    already produced. So UNKNOWN is a distinct verdict and never blocks.

    `match`    — the fact is contracted for this observation (or is an executed world-fact, which
                 is valid whenever it exists — same exemption `_filter_candidates_by_phase` makes).
    `mismatch` — a contract EXISTS and names a DIFFERENT boundary. `deliver_by` is documented as
                 the "LAST-useful boundary; deliver later => expire", so this is expired evidence.
    `unknown`  — no resolvable contract in either table. Correct-or-quiet: deliver as today."""
    if not observed_event:
        return BOUNDARY_UNKNOWN
    et = env.evidence_type or ""
    if et in _WORLD_FACT_EVIDENCE_TYPES:
        return BOUNDARY_MATCH
    # STEERS ARE PHASE-GOVERNED, NOT REGISTRY-GOVERNED — do not expire them here.
    # GT_VERIFY_IN_EDIT deliberately admits verify.horizon.* into Phase.EDIT on measured evidence
    # (121 wrong_phase suppressions; they are PRODUCED post-edit while EVENT_BOUND_PAYLOADS binds
    # advisory/urgent to REVIEW_TRANSITION and pivot to TEST_RESULT). Judging them against the
    # event table would mark them `mismatch` at edit_result and DROP exactly what that lever exists
    # to admit — the two levers would silently cancel out when both are enabled.
    # Two governance lanes, and expiry belongs to the FACT lane: `context_policy` decides when a
    # steer may speak, `fact_registry` decides when a fact has expired. Ranking may still use the
    # event binding as a preference (it only reorders); expiry must not, because it deletes.
    if et.startswith("verify.horizon."):
        return BOUNDARY_UNKNOWN
    # EXPIRY IS A FACT-LANE RULE. If `fact_registry` does not resolve this evidence_type, expiry
    # ABSTAINS — it must never fall back to the event-binding table the way RANKING does.
    #
    # Found by testing GT_SCOPE_AT_SEARCH together with expiry: `consensus.scope` does not resolve
    # in the fact registry, so the fallback judged it against EVENT_BOUND_PAYLOADS (REVIEW_TRANSITION)
    # and returned `mismatch` at EVERY boundary — including `search_result`, the boundary its own
    # fact class (def_partition) is contracted to. Expiry would have deleted def_partition outright,
    # and its ledger row would read `boundary_expired`, i.e. exactly like the gate working.
    #
    # The asymmetry, stated once: RANKING may consult both tables because a preference only
    # REORDERS; EXPIRY may consult only the registry because it DELETES. A signal good enough to
    # prefer with is not automatically good enough to erase with.
    contracted = None
    try:
        from groundtruth.runtime.fact_registry import required_event
        contracted = required_event(et)
    except Exception:  # noqa: BLE001
        contracted = None
    if contracted:
        if contracted == observed_event:
            return BOUNDARY_MATCH
        # UNOBSERVABLE CONTRACT => ABSTAIN. `boundary_for_event` can only derive the five kinds a
        # tool event carries (edit_result / test_result / file_view / search_result /
        # failed_search). A fact contracted to a boundary OUTSIDE that set — task_start,
        # failure_obs, submit, first_view_edit — can therefore NEVER be observed at its own
        # boundary, so every observation would read `mismatch` and expiry would DELETE it
        # EVERYWHERE. Measured: trace_frame (failure_obs) was dropped at all 7 boundaries;
        # submit_refusal, obligations/brief_localization (task_start) and cochange_prior
        # (first_view_edit) are in the same class — four of the 17 DIRECT features, permanently
        # silenced by an INCOMPLETENESS in the derivation rather than by anything about the fact.
        # Expiry may only fire on a contract this seam can actually witness.
        if contracted not in _DERIVABLE_BOUNDARIES:
            return BOUNDARY_UNKNOWN
        return BOUNDARY_MISMATCH
    return BOUNDARY_UNKNOWN


def boundary_for_event(event: "ToolEvent | None", *, zero_results: bool = False) -> "str | None":
    """Derive the fine §1 observation boundary from the tool event (PURE, no I/O).

    The §1 vocabulary `fact_registry` ranks against is FINER than the event kind alone, and the
    difference is load-bearing: `newfile_precedent` is contracted to `failed_search`, NOT
    `search_result`. A search that RETURNED hits and one that came back EMPTY are different
    decision moments, and only the empty one owes a new-file precedent. So the caller must pass
    ``zero_results`` — it owns the outcome classification; this module stays pure.

    Returns None when the boundary cannot be determined, which leaves ranking exactly as it is
    today (correct-or-quiet: an undetermined boundary must never invent a match)."""
    if event is None:
        return None
    primary = getattr(event, "primary_boundary", "") or ""
    if primary:
        return primary
    semantic_events = tuple(getattr(event, "semantic_events", ()) or ())
    if semantic_events:
        return semantic_events[0]
    kind = getattr(event, "kind", None)
    if kind == KIND_EDIT:
        return "edit_result"
    if kind == KIND_TEST:
        return "test_result"
    if kind == KIND_VIEW:
        return "file_view"
    if kind == KIND_SEARCH:
        return "failed_search" if zero_results else "search_result"
    return None


def _priority(
    env: EvidenceEnvelope,
    recently_delivered: "frozenset[str]" = frozenset(),
    observed_event: "str | None" = None,
) -> tuple[int, int, int, float, str]:
    """A TOTAL deterministic order over envelopes (higher = deliver first).

    Ranked by evidence-type severity, then tier, then confidence, then the stable
    ``dedup_key`` so two facts with identical severity resolve deterministically
    (no set-iteration / no time).

    SM-9c (2026-07-12): a cross-session rank-up boost (``_xsession_boost`` on ``native_args``,
    stamped by ``gateway._apply_xsession_rankup`` for a historically-CONSUMED class behind
    ``GT_XSESSION_RANKUP``) is ADDED to the severity rank, so a memory-consumed already-eligible
    fact climbs the ladder and can WIN the ``<= 1`` dose. ``0`` (absent) ⇒ byte-identical. It is
    ADDED to ``base`` (bounded + capped below the covering-RED gap in ``xsession_memory``), never
    a leading override — so it re-ranks WITHIN the severity space and never lets a learned fact
    out-dose an executed world-fact."""
    et = env.evidence_type or ""
    base = _EVIDENCE_TYPE_RANK.get(et)
    if base is None:  # family prefix (e.g. "missing_role:registry")
        base = _EVIDENCE_TYPE_RANK.get(et.split(":", 1)[0], 10)
    base += _xsession_boost(env)  # SM-9c rank-up (0 when unstamped -> byte-identical)
    if recently_delivered and (
            et in recently_delivered or et.split(":", 1)[0] in recently_delivered):
        base -= _ROTATION_DECAY  # WS-1: already delivered this attempt -> yield the next dose
    return (_boundary_match(env, observed_event), base,
            _TIER_RANK.get(env.tier, 1), float(env.confidence), env.dedup_key)


def arbitrate(
    envelopes: "list[EvidenceEnvelope]",
    recently_delivered: "frozenset[str]" = frozenset(),
    observed_event: "str | None" = None,
) -> "EvidenceEnvelope | None":
    """The per-turn dose: at most ONE envelope (ABI §2 — <=1 steer/turn). Returns
    the highest-priority envelope, or ``None`` when the list is empty. augment()
    already dedup-suppressed repeats via the per-episode chain, so this only picks
    the winner among the survivors. ``recently_delivered`` (WS-1, default empty =>
    byte-identical) demotes already-delivered classes so the dose rotates.

    ``observed_event`` (default None => byte-identical) is the fine §1 boundary the current
    observation carries. When supplied, a fact whose REGISTERED ``deliver_by`` is that boundary
    sorts above one whose is not — so the dose goes to the fact that answers THIS observation
    rather than to whichever class sits highest in the static severity table. Severity still
    orders the matched set, so an executed covering-RED is never out-dosed by an advisory."""
    if not envelopes:
        return None
    return max(envelopes, key=lambda e: _priority(e, recently_delivered, observed_event))


def select(
    envelopes: "list[EvidenceEnvelope]",
    *,
    max_doses: int = 1,
    multidose: bool = False,
    recently_delivered: "frozenset[str]" = frozenset(),
    observed_event: "str | None" = None,
) -> "list[EvidenceEnvelope]":
    """WS-1 (2026-07-23) — the dose SET, superset of :func:`arbitrate`.

    ``multidose=False`` (default) is BYTE-IDENTICAL to ``arbitrate``: returns
    ``[winner]`` (or ``[]``), i.e. the ABI §2 ``<=1`` dose. This keeps the whole seam
    byte-identical until the seam explicitly opts in.

    ``multidose=True`` (the seam reads ``GT_MULTIDOSE`` and passes the decision +
    ``max_doses`` here — this module stays PURE / I-O-free) admits a RANKED, dedup'd
    SET: envelopes in ``_priority`` order, distinct ``dedup_key``, capped at
    ``max_doses`` — so several high-priority, non-redundant facts co-deliver at one
    observation instead of the winner-take-all ``argmax`` starving every runner-up
    (SGR coverage-max under a budget — El-Yaniv/Wiener 2010, SelectiveNet ICML'19).
    The per-class calibrated threshold + freshness + marginal-Δ gate is applied by the
    seam BEFORE this call (it owns those runtime signals); ``select`` only does the
    deterministic ranked-set-under-cap. ``max_doses<=1`` collapses to the ``<=1`` dose."""
    if not envelopes:
        return []
    if not multidose or max_doses <= 1:
        w = arbitrate(envelopes, recently_delivered, observed_event)
        return [w] if w is not None else []
    out: "list[EvidenceEnvelope]" = []
    seen: "set[str]" = set()
    for env in sorted(
            envelopes, key=lambda e: _priority(e, recently_delivered, observed_event),
            reverse=True):
        if env.dedup_key in seen:
            continue
        out.append(env)
        seen.add(env.dedup_key)
        if len(out) >= max_doses:
            break
    return out


# --------------------------------------------------------------------------- #
# 3. render — one envelope -> model-facing bytes
# --------------------------------------------------------------------------- #
def render_envelope(
    env: EvidenceEnvelope,
    *,
    native: bool,
    def_facts_renderer: "Callable[[EvidenceEnvelope], str] | None" = None,
) -> str:
    """Render ONE envelope to the model-facing string that will be appended.

    Def-facts kinds ROUTE to the mini's own formatter (``def_facts_renderer`` — the
    injected ``_fmt_def_facts``, which itself dispatches to ``_fmt_def_facts_native``
    under ``GT_POST_SEARCH_NATIVE``), so the gateway's post_search bytes equal the
    pre-wired mini's — never a re-implementation. The byte-equal reuse is PROVEN for
    ``def_ref_partition``; ``name_fold`` falls back generic (the renderer abstains on
    the unresolved spelling — see ``DEF_FACTS_KINDS``). Other kinds render generically:
    tag-free under ``native`` (world-fact voice), a single ``<gt-fact>`` wrapper by
    default. The leak-guard on the result lives on the seam (it owns the
    native_render predicates); this function only assembles deterministic bytes."""
    if env.evidence_type in DEF_FACTS_KINDS and def_facts_renderer is not None:
        try:
            rendered = def_facts_renderer(env)
        except Exception:  # noqa: BLE001 — a renderer fault falls back to generic
            rendered = ""
        if rendered:
            return rendered
    # SM-2b: NATIVE per-class dispatch (Finding 2). Under `native`, a live gateway class
    # renders in its SM-1 tool-channel grammar instead of tag-free English. `_render_generic`
    # stays the fallback (a class with no native renderer, or a renderer that abstains). The
    # TAGGED form is UNCHANGED (still `_render_generic`'s <gt-fact> wrapper) — dispatch is
    # native-only, so the default-off byte-identity of the tagged path is preserved.
    if native:
        nat = _render_native_class(env)
        if nat:
            return nat if nat.endswith("\n") else nat + "\n"
    return _render_generic(env, native=native)


# --------------------------------------------------------------------------- #
# SM-2b native per-class dispatch — envelope -> SM-1 renderer arg mapping.
# The gateway producers build TEXT payloads (the envelope has no structured numeric fields),
# so each adapter below reconstructs the SM-1 renderer's structured args from the envelope's
# target / provenance / fact_id. A renderer that abstains ("") falls back to _render_generic.
# --------------------------------------------------------------------------- #
_IDENT_RE = re.compile(r"^[A-Za-z_]\w*$")


def _prov_first_line(env: EvidenceEnvelope) -> "int | None":
    for f, ln in env.provenance:
        if f:
            try:
                return int(ln)
            except (TypeError, ValueError):
                return None
    return None


def _func_of(env: EvidenceEnvelope) -> "str | None":
    """The envelope's ``fact_id`` when it is a bare symbol (a function name), else None — a
    path-shaped ``fact_id`` (trace with no func) must not become a ``in <fn>`` continuation."""
    fid = (env.fact_id or "").strip()
    return fid if _IDENT_RE.match(fid) else None


def _distinct_prov_files(env: EvidenceEnvelope) -> "list[str]":
    return sorted({f for f, _ in env.provenance if f})


def _render_trace(env: EvidenceEnvelope) -> str:
    """`trace_frame` -> a native CPython/compiler traceback FRAME (the where-to-fix), NOT the
    ``deepest in-repo frame: …`` GT narration."""
    return render_trace_frame_native(env.target, _prov_first_line(env), _func_of(env))


def _render_caller_break(env: EvidenceEnvelope) -> str:
    """`caller_break` (caller_contract) -> a native compiler CONTRACT diagnostic. N callers /
    M files are recovered from the provenance (one caller row per distinct caller by producer
    construction); the edited symbol is ``fact_id`` and its file is ``target``."""
    n_callers = len(env.provenance)
    n_files = len(_distinct_prov_files(env))
    if n_callers <= 0:
        return ""
    args = env.native_args or {}
    before = args.get("before_parameters")
    after = args.get("after_parameters")
    delta = ""
    if (
        isinstance(before, tuple) and isinstance(after, tuple)
        and before != after
        and all(isinstance(item, str) and item for item in (*before, *after))
    ):
        delta = f"{', '.join(before)} -> {', '.join(after)}"
    diagnostic = render_caller_contract_native(
        env.fact_id, n_callers, n_files, def_file=env.target, sig_delta=delta)
    if not diagnostic:
        return ""
    caller_rows = render_def_rows_native(args.get("caller_rows") or ())
    usage_lines: list[str] = []
    for row in args.get("caller_usage_rows") or ():
        try:
            caller_file, caller_line, symbol, usage_kind = row
        except (TypeError, ValueError):
            continue
        rendered = render_caller_usage_native(
            caller_file, caller_line, symbol, usage_kind,
        )
        if rendered and rendered not in usage_lines:
            usage_lines.append(rendered)
    return "\n".join(
        part for part in (diagnostic, caller_rows, *usage_lines) if part
    )


def _render_caller_contract_view(env: EvidenceEnvelope) -> str:
    """`caller_contract_view` (FACT-tier contracts attached to a pure source VIEW) -> the
    TRUTHFUL view-contract form. The viewed file was NEVER edited, so the caller_break
    change-claim ("<file>: error: <sym>() signature changed; ...") is a fabricated post-edit
    fact here (wrong-info at VERIFIED tier; a multi-symbol view even surfaced the placeholder
    ``fact_id`` "viewed_file_contract" as the changed symbol). Fixed 2026-07-29 by narrowing
    the ROUTING - the break renderer stays exclusively on ``caller_break``.

    Composition, all EXISTING primitives fed from what the producer already attaches:
      * the producer's own payload head ("<sym>() has N production caller(s) across M
        file(s)" per contract - the who-calls-it statement, leak-filtered at _mk_add);
      * :func:`render_note_rows_native` over ``native_args['caller_rows']`` - the
        relationship-agnostic compiler ``note:`` row per caller site, documented as never
        overclaiming "signature" (correct-or-quiet by design);
      * the typed :func:`render_caller_usage_native` lines (same loop as the break form).
    Correct-or-quiet: no signal-bearing caller row -> "" -> _render_generic fallback (which
    ships the same truthful payload tag-free in native mode)."""
    args = env.native_args or {}
    note_rows = render_note_rows_native(args.get("caller_rows") or ())
    if not note_rows:
        return ""
    head = [ln for ln in env.payload if ln]
    usage_lines: list[str] = []
    for row in args.get("caller_usage_rows") or ():
        try:
            caller_file, caller_line, symbol, usage_kind = row
        except (TypeError, ValueError):
            continue
        rendered = render_caller_usage_native(
            caller_file, caller_line, symbol, usage_kind,
        )
        if rendered and rendered not in usage_lines:
            usage_lines.append(rendered)
    return "\n".join(part for part in (*head, note_rows, *usage_lines) if part)


def _render_signature_delta(env: EvidenceEnvelope) -> str:
    """`signature_mismatch` (patch_delta arity break) -> the SM-1 mypy/gopls ARITY DIAGNOSTIC.
    The structured arity fields ride ``env.native_args`` (attached by the gateway producer);
    absent/empty -> "" -> _render_generic fallback (byte-identical for a native_args-less
    envelope). The renderer itself is identity-firewalled + scrubbed."""
    a = env.native_args or {}
    if not a:
        return ""
    caller_file = a.get("caller_file")
    symbol = a.get("symbol")
    if not isinstance(caller_file, str) or not isinstance(symbol, str):
        return ""
    return render_signature_delta_native(
        caller_file, a.get("caller_line"), symbol,
        expected_min=a.get("new_min_params"), expected_max=a.get("new_max_params"),
        given=a.get("positional_args"))


def _render_registration(env: EvidenceEnvelope) -> str:
    """`companion_surface` (registers siblings X, Y but not Z) -> the SM-1 linter REGISTRATION
    WARNING. The structured ``siblings`` LIST rides ``env.native_args`` (the companion producer
    owns it — no prose re-parse); a missing file/line/siblings -> "" -> generic fallback."""
    a = env.native_args or {}
    if not a:
        return ""
    file_path = a.get("file")
    symbol = a.get("symbol")
    if not isinstance(file_path, str) or not isinstance(symbol, str):
        return ""
    return render_registration_native(
        file_path, a.get("line"), symbol, a.get("siblings"))


def _render_def_rows(env: EvidenceEnvelope) -> str:
    """`def_ref_partition` -> the SM-1 native RIPGREP ROW block (``path:line:sym`` per def
    site). The def sites ARE the envelope provenance (each ``(file, line)`` is a def site
    ``_resolve_symbol_defs`` emitted — ``_produce_def_ref_partition`` passes
    ``evidence=info["def_sites"]``) and the queried symbol is ``fact_id``, so the SM-1
    ``grep-native`` primitive (the registry-DECLARED renderer for the ``def_partition`` class)
    is fed DIRECTLY from the envelope — no reconstruction from prose. This is the gateway's
    OWN native ``def_partition`` form for the STANDALONE path (when the mini's ``_fmt_def_facts``
    is not injected); in the seam production path the injected ``def_facts_renderer`` runs FIRST
    (``DEF_FACTS_KINDS``) and wins, so this is byte-identical there. Correct-or-quiet: no symbol
    or no clean (non-test) def row -> "" (render_def_rows_native drops test rows) -> generic."""
    sym = (env.fact_id or "").strip()
    if not sym:
        return ""
    rows = [(f, ln, sym) for f, ln in env.provenance if f]
    return render_def_rows_native(rows)


def _render_ranked_list(env: EvidenceEnvelope) -> str:
    """`localization` (T0->T2 re-slot) -> GT's RANKED candidate files as the native RIPGREP
    ROW block (``path:line:sym`` per candidate). The (path,line,sym) triples ride
    ``env.native_args['rows']`` (the ``_produce_ranked_localization`` producer OWNS them — one
    row per ranked candidate, DISTINCT per-file symbol; no prose re-parse), mirroring
    signature_mismatch/companion_surface. Absent/empty rows -> "" -> _render_generic fallback
    (byte-identical for a native_args-less envelope). Hedge-free + identity-firewalled by
    :func:`render_ranked_list_native` (a test-file candidate is dropped, every row scrubbed)."""
    rows = (env.native_args or {}).get("rows") or []
    if not rows:
        return ""
    return render_ranked_list_native(rows)


def _render_body_concept(env: EvidenceEnvelope) -> str:
    """`body_concept` -> the SM-1 native RIPGREP ROW block for the concept-hit function bodies
    (``render_body_concept_native``). The (path,line,sym) triples ride ``env.native_args['rows']``
    (the ``_produce_body`` gateway producer OWNS them — one row per concept-bearing function, its
    NAME as the symbol; no prose re-parse), mirroring def_ref_partition / localization. Absent /
    empty rows -> "" -> _render_generic fallback (byte-identical for a native_args-less envelope).
    Identity-firewalled + scrubbed by the renderer (a test-file row is dropped)."""
    rows = (env.native_args or {}).get("rows") or []
    if not rows:
        return ""
    return render_body_concept_native(rows)


def _render_scope(env: EvidenceEnvelope) -> str:
    """`scope` -> the SM-1 native compiler ``note:`` constraint via the BUILT-but-formerly-DARK
    :func:`render_scope_constraint_native` (the envelope's ``target`` is the must-ALSO-touch
    file). WIRED (2026-07-12), NOT rebuilt.

    DORMANT in production: no LIVE gateway producer emits a ``scope`` evidence_type today (the
    only scope surface is the mini seam's Lane-A ``_consensus_scope_block`` <gt-scope> tag, which
    is NOT routed through this adapter, and ``scope`` is not a registered fact class), so this
    dispatch entry is exercised by the unit suite but never fires on a live run until a registered
    scope producer exists — mirroring the ``localization`` entry's inert-until-its-producer
    status. Correct-or-quiet + identity-firewalled (a test-file target -> "")."""
    return render_scope_constraint_native(env.target)


# evidence_type (or its ``base:suffix`` base) -> the native-arg-mapping renderer. cochange is
# INTENTIONALLY absent (it is internal-only — the gateway never delivers a cochange_partner
# envelope, so it never reaches here; its SM-1 renderer always returns "").
# SM-10 (2026-07-12): signature_mismatch + companion_surface are WIRED — the gateway producer
# attaches the structured fields the SM-1 renderer needs on ``env.native_args`` (the arity
# tuple / the siblings LIST), so the adapter feeds the renderer DIRECTLY (no re-parse of GT's
# own prose). A native_args-less envelope of either class renders "" -> _render_generic
# (byte-identical); the dispatch is native-only, so the tagged/default path is unchanged.
# SM (2026-07-12): the LAST two named seams are now WIRED — a conscious, tested choice
# (test_scope_body_concept_native):
#   * body_concept — LIVE. The ``_produce_body`` producer attaches ``native_args['rows']``, so
#     the grep-native ROW block renders DIRECTLY. A native_args-less envelope -> "" -> generic.
#   * scope — WIRED but DORMANT. render_scope_constraint_native was BUILT-but-dark; it is wired
#     here (not rebuilt), but no live gateway producer emits a ``scope`` evidence_type yet (see
#     _render_scope), so the entry is unit-tested and inert on a live run until a registered
#     scope producer exists (do NOT fake fields — the target must be a real must-touch file).
_NATIVE_CLASS_RENDERERS: dict[str, Callable[[EvidenceEnvelope], str]] = {
    "trace_frame": _render_trace,
    "caller_break": _render_caller_break,
    # 2026-07-29: the VIEW alias renders the truthful pre-edit contract form - routing it
    # through _render_caller_break shipped a false "signature changed" claim for a file the
    # agent merely read (see _render_caller_contract_view).
    "caller_contract_view": _render_caller_contract_view,
    "def_ref_partition": _render_def_rows,
    "signature_mismatch": _render_signature_delta,
    "companion_surface": _render_registration,
    # newfile_precedent (`new_file_destination` / `missing_role:*`) is deliberately
    # ABSENT: a ripgrep row for a file that does not exist yet fabricates a world-fact
    # in the grep grammar, and change_surface envelopes carry no native_args for the
    # registration renderer. Its native FORM needs a real design (a suggestion is not
    # a search hit) — until then the tagged/generic form is the honest carrier.
    # T0->T2 re-slot (2026-07-12): the ranked localization ANSWER renders native at D2. A
    # native_args-less envelope maps to "" -> _render_generic (byte-identical); the dispatch
    # is native-only, so the tagged/default path is unchanged. Only the GT_LOC_RESLOT
    # producer ever emits a "localization" evidence_type, so this entry is inert when off.
    "localization": _render_ranked_list,
    "body_concept": _render_body_concept,
    "scope": _render_scope,
}


def _render_native_class(env: EvidenceEnvelope) -> str:
    """Route ``env`` to its SM-1 native renderer, or "" (fall back to _render_generic) when the
    class has no native mapping or its renderer abstains. Never raises."""
    et = env.evidence_type or ""
    fn = _NATIVE_CLASS_RENDERERS.get(et) or _NATIVE_CLASS_RENDERERS.get(et.split(":", 1)[0])
    if fn is None:
        return ""
    try:
        return fn(env) or ""
    except Exception:  # noqa: BLE001 — a native renderer fault falls back to generic
        return ""


def _render_generic(env: EvidenceEnvelope, *, native: bool) -> str:
    body = [ln for ln in env.payload if ln]
    prov = [f"{f}:{ln}" for f, ln in env.provenance if f]
    # P3 (bounce 2026-07-10): BOTH branches dedup provenance rows already embedded
    # in a payload line — the tagged form no longer repeats `a/x.py:7` when the
    # body says "deepest in-repo frame: a/x.py:7 in f" (parity with native).
    extra = [p for p in prov if not any(p in b for b in body)]
    if native:
        # world-fact voice: tag-free. Payload lines first (already human-readable),
        # then any provenance row not already embedded in a payload line.
        lines = body + extra
        return ("\n".join(lines) + "\n") if lines else ""
    if not body and not extra:
        return ""
    head = f'<gt-fact kind="{env.evidence_type}">'
    lines = [head, *body, *extra, "</gt-fact>"]
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- #
# 4. TITO law 8 — never append a partial/over-budget delta
# --------------------------------------------------------------------------- #
def fits_budget(delta: str, *, max_delta_chars: int = DEFAULT_MAX_DELTA_CHARS) -> bool:
    """True iff ``delta`` is non-empty and fits the delivery budget WHOLE. Law 8: a
    delta that would be truncated at the observation boundary is dropped ENTIRELY —
    a fabricated/partial splice is never appended. The seam passes the remaining
    observation budget so a near-full observation refuses the delta rather than
    clipping it."""
    return bool(delta) and len(delta) <= int(max_delta_chars)


# --------------------------------------------------------------------------- #
# 5. seal — stamp TITO metadata + extend the per-episode hash-chain
# --------------------------------------------------------------------------- #
def seal_delivery(
    env: EvidenceEnvelope,
    *,
    episode_id: str,
    event_id: str,
    parent_hash: str,
    rendered_bytes: bytes,
    renderer_id: str,
    tool_output_bytes: bytes = b"",
    boundary: bytes = b"",
    dedup_chain: "set[str] | None" = None,
    observation_binding: "ObservationBinding | None" = None,
) -> "tuple[EvidenceEnvelope, str]":
    """Stamp a DELIVERED copy of ``env`` and return ``(sealed_copy, new_chain_head)``.

    ``rendered_bytes`` are the bytes ACTUALLY appended into the observation:
    ``rendered_bytes_hash`` = their sha256 (law 6, the replay check) and the chain
    advances via ``chain_hash(parent_hash, rendered_bytes, tool_output_bytes=...,
    boundary=...)`` (law 5). The copy is a ``dataclasses.replace`` — fact identity
    (producer/type/target/fact_id/payload/provenance and thus ``dedup_key``) is
    UNTOUCHED, so the sealed copy still passes ``evidence_envelope.validate``.
    ``receipt_state`` starts at ``delivered`` (law 7, level 1) with the
    delivery-evidence hash validate requires.

    FULL-OBSERVATION CHAIN (B-32, this fix): the chain now commits the base
    observation, not just the GT delta. ``tool_output_bytes`` are the EXACT base
    tool-output bytes of the observation this delta is spliced into, and ``boundary``
    is the message-boundary metadata; both are folded into ``chain_hash`` so two
    DIFFERENT observations carrying identical GT deltas produce DIFFERENT chain heads
    (observation-prefix / TITO-replay integrity). They default to ``b""`` — the
    DOCUMENTED "seam not yet wired" state: until the seam supplies the base
    observation the chain degrades EXPLICITLY to the GT-delta-only commitment. This is
    NOT a model-facing byte change (the chain head is audit metadata, never appended).
    THE SEAM WIRING (Fable) must pass ``tool_output_bytes`` = the exact base tool
    output the delta is appended into, and ``boundary`` = the message-boundary
    descriptor for that splice.

    THE DELIVERY COMMIT (F1, bounce 2026-07-10): when ``dedup_chain`` (the episode's
    ``delivered_dedup`` set) is passed, the fact's ``dedup_key`` is stamped HERE — at
    the seal, the seam-visible commit — never at production. ``gateway.augment`` only
    READS the chain, so a produced-but-dropped fact (arbitration loss, leak-guard
    drop, law-8 budget drop) is deferred and re-offered, never destroyed."""
    rbh = hashlib.sha256(rendered_bytes).hexdigest()
    new_head = chain_hash(
        parent_hash, rendered_bytes,
        tool_output_bytes=tool_output_bytes, boundary=boundary,
    )
    if dedup_chain is not None:
        dedup_chain.add(env.dedup_key)
    sealed = dataclasses.replace(
        env,
        episode_id=str(episode_id or ""),
        event_id=str(event_id or ""),
        parent_observation_hash=parent_hash or "",
        rendered_bytes_hash=rbh,
        renderer_id=str(renderer_id or ""),
        receipt_state=RECEIPT_DELIVERED,
        delivery_reason="gateway_augment",
        suppression_reason="",
        observation_binding=observation_binding,
    )
    return sealed, new_head


# --------------------------------------------------------------------------- #
# 6. receipts — TITO law 7 reward-attribution ladder (levels 1-3 live)
# --------------------------------------------------------------------------- #
# The ladder's ORDER is declared once, in `evidence_envelope.RECEIPT_RANK` (2026-07-28).
# This was a frozen literal that agreed with `receipt_sidecar._TRANSITION_RANK` by hand; the
# alias is safe because all three uses below are read-only (`.get`, `__getitem__`) and
# `RECEIPT_RANK` is a `MappingProxyType`, so this reader cannot mutate the shared authority.
_RECEIPT_ORDER: Mapping[str, int] = RECEIPT_RANK


def _delivered_entities(env: EvidenceEnvelope) -> "list[tuple[str, str]]":
    """The typed entities a later message/action can REFERENCE or ACT on:
    ``("path", ...)`` for the target and each provenance file (full repo-relative
    path — a bare basename is NEVER derived from a pathed value, F2), and
    ``("symbol", fact_id)`` for the producer-natural symbol (a path-shaped fact_id
    is typed path). Deterministic, de-duplicated, non-empty values only."""
    ents: list[tuple[str, str]] = []

    def add(kind: str, v: str) -> None:
        v = (v or "").strip()
        if v and (kind, v) not in ents:
            ents.append((kind, v))

    add("path", env.target)
    fid = (env.fact_id or "").strip()
    add("path" if "/" in fid.replace("\\", "/") else "symbol", fid)
    for f, _ln in env.provenance:
        add("path", f)
    return ents


# Path-shaped tokens in free text/commands: slash-joined runs (an optional
# leading '/' is CAPTURED — R2: absolute vs relative prefix must be
# distinguishable), or a bare dotted filename (util.py). Used to compare
# DELIVERED path entities against the turn's text on token boundaries, never
# raw substring (F2).
_TEXT_PATH_TOKEN_RE = re.compile(r"/?[\w.+\-]+(?:/[\w.+\-]+)+|[\w+\-]+\.[A-Za-z0-9_]+")


def _path_entity_matches(entity: str, text: str) -> bool:
    """True iff the DELIVERED path ``entity`` matches a path token of ``text``:
    exact relpath; a suffix form ending in ``/<entity>`` ONLY for an ABSOLUTE
    token (R2, micro-bounce 2026-07-10: a container/absolute prefix like
    ``/testbed/`` legitimately re-roots the SAME repo-relative file, but a
    sibling-RELATIVE prefix like ``backup/`` names a DIFFERENT file — a clone —
    and must not match); or — ONLY when the delivered entity is basename-only
    (carries no directory to contradict) — basename equality. A pathed entity
    never matches a same-basename file in a DIFFERENT directory (F2)."""
    ent = (entity or "").replace("\\", "/").strip().lstrip("./")
    if not ent:
        return False
    basename_only = "/" not in ent
    for tok in _TEXT_PATH_TOKEN_RE.findall((text or "").replace("\\", "/")):
        absolute = tok.startswith("/")
        tok = tok.lstrip("./")
        if tok == ent or (absolute and tok.endswith("/" + ent)):
            return True
        if basename_only and tok.rsplit("/", 1)[-1] == ent:
            return True
    return False


def _symbol_entity_matches(entity: str, text: str) -> bool:
    """True iff ``entity`` appears in ``text`` as a WHOLE identifier token —
    word-boundary on identifier chars, so ``get`` never matches inside ``target``
    and ``run`` never matches inside ``running`` (F2). The boundary class is
    ``\\w`` — UNICODE-aware (R1, micro-bounce 2026-07-10): the ASCII class let
    ``café`` match inside ``écafé`` (é was not "identifier" to the lookaround),
    inflating receipts on unicode identifiers; ``\\w`` is the strict superset."""
    ent = (entity or "").strip()
    if len(ent) < 2:
        return False  # degenerate 1-char "symbols" are noise, never a receipt
    pat = re.compile(r"(?<!\w)" + re.escape(ent) + r"(?!\w)")
    return bool(pat.search(text or ""))


def _entity_matches(kind: str, entity: str, text: str) -> bool:
    if kind == "path":
        return _path_entity_matches(entity, text)
    return _symbol_entity_matches(entity, text)


def update_receipts(
    deliveries: "list[EvidenceEnvelope]",
    *,
    policy_text: str = "",
    next_action_cmd: str = "",
    env_output: str = "",
    observed_text: str = "",
) -> "list[EvidenceEnvelope]":
    """Promote each recorded delivery's ``receipt_state`` from THIS turn's evidence
    (law 7, levels 1-3):

      * level 2 REFERENCED — the POLICY's own message NAMES a delivered entity
        (``policy_text`` — the agent's assistant/reasoning turn).
      * level 3 ACTED — the POLICY's next tool CALL TARGETS a delivered entity
        (``next_action_cmd`` — the command the agent chose to run).

    B-13 (this fix): a receipt may be earned ONLY from POLICY-PRODUCED evidence — the
    agent's own message (``policy_text``) or its own tool call (``next_action_cmd``) —
    NEVER from environment/tool OUTPUT. Previously the seam fed tool output into the
    referencing channel, so an unrelated ``ls``/``grep`` result that merely NAMES a
    delivered path false-promoted the receipt to REFERENCED without the policy ever
    referencing or acting on the evidence — a spurious reward signal. ``env_output``
    is accepted so the seam can pass the environment's tool output EXPLICITLY, but it
    is structurally NEVER consulted for promotion. ``observed_text`` is the DEPRECATED
    alias the pre-fix seam used to pass tool output; it is likewise treated as
    environment output and NEVER promotes, so an un-migrated seam degrades EXPLICITLY
    to the correct (no-false-promotion) behavior rather than crashing. THE SEAM WIRING
    (Fable) must migrate ``observed_text=<tool output>`` to ``env_output=<tool
    output>`` and pass ``policy_text=<the agent's own assistant message>`` for the
    REFERENCED signal.

    ACTED dominates REFERENCED. Monotone (never downgrades). Cheap and deterministic
    — no graph, no time — but NEVER raw substring (F2, bounce 2026-07-10): symbols
    match on identifier word boundaries; paths match whole path tokens (full relpath
    or a ``/``-prefixed form; basename equality only for a basename-only entity), so
    ``get``⊂``target``, ``run``⊂``running``, and a same-basename file in a different
    directory can no longer inflate a receipt. Returns a NEW list of copies
    (``dataclasses.replace``); the input is not mutated."""
    ptext = policy_text or ""
    cmd = next_action_cmd or ""
    # B-13: environment/tool OUTPUT is NEVER a promotion source. env_output and the
    # deprecated observed_text alias are accepted (so the seam can pass tool output
    # explicitly / an un-migrated seam does not crash) but are intentionally NOT read
    # for promotion — only ptext (policy message) and cmd (policy action) are.
    _ = (env_output, observed_text)
    out: list[EvidenceEnvelope] = []
    for env in deliveries:
        cur = _RECEIPT_ORDER.get(env.receipt_state, 0)
        ents = _delivered_entities(env)
        acted = any(_entity_matches(k, e, cmd) for k, e in ents)
        referenced = any(_entity_matches(k, e, ptext) for k, e in ents)
        new_state = env.receipt_state
        if acted and cur < _RECEIPT_ORDER[RECEIPT_ACTED]:
            new_state = RECEIPT_ACTED
        elif referenced and cur < _RECEIPT_ORDER[RECEIPT_REFERENCED]:
            new_state = RECEIPT_REFERENCED
        if new_state != env.receipt_state:
            env = dataclasses.replace(env, receipt_state=new_state)
        out.append(env)
    return out
