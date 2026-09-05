"""fact_registry — the fact-to-decision registry (the spine; verifyandobserve.md §1).

RL-adherent delivery has ONE unifying rule that must hold BEFORE any producer renders a
byte: **every model-facing fact class is statically registered, and an UNREGISTERED fact
must never render.** This module is that registry — a pure, deterministic, LLM-free table
mapping each of the 11 §1 FACT inventory rows to the decision it is meant to change, the
observation boundary at which it is still useful, the surface + native renderer that carry
it, its dose ceiling, its freshness dependencies, and how a receipt / causal contribution
is graded for it.

WHY A REGISTRY (correct-or-quiet, gt_gt §24-§26 + the delivery law of §0):
    A GT fact is valuable only when it is CORRECT, ON-TIME (delivered at the last
    observation boundary where it can still change its intended decision), and RL-ADHERED
    (native channel). A fact with no registration has no declared decision, no deliver-by
    boundary, and no dose — so the delivery kernel cannot deliver it on-time or dose it.
    The safe default for such a fact is SILENCE: :func:`renderable` returns ``False`` for
    any class not in :data:`REGISTRY`, so the kernel's render path is gated
    (``if not renderable(fc): return``). This mirrors the fail-closed leak law of
    :mod:`groundtruth.runtime.evidence_envelope` — wrong/undeclared evidence is worse than
    none.

SCOPE: the registry is a static declaration of the fact-class contract. It does NOT render,
deliver, dedup, or enforce timing — those are the delivery kernel's (Gateway's) job. The
render GATE is now WIRED (Graph-F2, bounce 2026-07-10): :func:`groundtruth.runtime.gateway.
augment` drops any addition whose ``evidence_type`` is not :func:`renderable`. Because the
Gateway emits a FINER vocabulary than the 11 §1 keys, :data:`_EVIDENCE_TYPE_ALIASES` bridges
each shipped ``evidence_type`` to its canonical class so :func:`renderable` passes every
legit fact and blocks only a truly-unregistered type. Timing/dose/surface consumption of
:attr:`FactRegistration.deliver_by` / :attr:`~FactRegistration.max_dose` /
:attr:`~FactRegistration.surface` remains future work.

PURE · DETERMINISTIC · LLM-FREE · stdlib-only. No time, no randomness, no I/O.

EVENT VOCABULARY. The §1 table names the fine observation boundaries at which each fact is
still decision-useful (``task_start``, ``search_result``, ``file_view``, ``edit_result``,
``test_result``, ``submit``, and the composite boundaries ``first_view_edit``,
``failed_search``, ``failure_obs``). This is a FINER vocabulary than
:mod:`evidence_envelope`'s coarse ``preferred_event`` (search/view/edit/test/submit/step0);
the correspondence is noted per registration. For every §1 fact, ``earliest_event`` and
``deliver_by`` are the SAME boundary — these are facts ABOUT an action, useful only in the
observation that carries the action's result (there is no later boundary at which the fact
still changes the decision), so the last-useful boundary == the earliest one. The §1
deliver_by QUALIFIERS ("pre-edit", "interception", "transactional", "same X") are recorded
in per-registration comments; they refine the SAME canonical event.
"""

from __future__ import annotations

from dataclasses import dataclass, field

__all__ = [
    "FactRegistration",
    "REGISTRY",
    "registration",
    "registration_for",
    "producer_matches",
    "required_event",
    "earliest_event_for",
    "required_renderer",
    "ack_expected",
    "is_reactive",
    "freshness_surfaces",
    "registry_graph_surfaces",
    "is_patch_bound",
    "is_registered",
    "renderable",
    "assert_registered",
    "all_fact_classes",
    "fact_role_for",
    "UnregisteredFactError",
    # executable-registry structures (SM-0 Super Mode spine)
    "FRESHNESS_SURFACES",
    "PATCH_BOUND_FACTCLASSES",
    # controlled vocabularies (the only legal values for the enum-ish fields)
    "EVENTS",
    "SURFACES",
    "RENDERERS",
    "FACT_ROLE_DELIVERY",
    "FACT_ROLE_INTERNAL_SUPPORT",
    "FACT_ROLES",
    # event names (§1 fine observation boundaries)
    "EVENT_TASK_START",
    "EVENT_SEARCH_RESULT",
    "EVENT_FILE_VIEW",
    "EVENT_EDIT_RESULT",
    "EVENT_TEST_RESULT",
    "EVENT_SUBMIT",
    "EVENT_FIRST_VIEW_EDIT",
    "EVENT_FAILED_SEARCH",
    "EVENT_FAILURE_OBS",
]


# --------------------------------------------------------------------------- #
# controlled vocabularies
# --------------------------------------------------------------------------- #
# Event names = the §1 fine observation boundaries (earliest_event / deliver_by must be one
# of these). Kept distinct from evidence_envelope._EVENTS on purpose: that is the coarse
# preferred_event; these are the finer §1 boundaries. The comment on each maps it back.
EVENT_TASK_START = "task_start"            # step-0 brief boundary (≈ envelope step0)
EVENT_SEARCH_RESULT = "search_result"      # a search/grep result observation (≈ search)
EVENT_FILE_VIEW = "file_view"              # a file-view observation (≈ view)
EVENT_EDIT_RESULT = "edit_result"          # an edit-result observation (≈ edit)
EVENT_TEST_RESULT = "test_result"          # a test-run result observation (≈ test)
EVENT_SUBMIT = "submit"                    # the submit interception boundary (≈ submit)
EVENT_FIRST_VIEW_EDIT = "first_view_edit"  # §1 "first view/edit" (companion prior boundary)
EVENT_FAILED_SEARCH = "failed_search"      # §1 "failed_search/ADD" (missing-role boundary)
EVENT_FAILURE_OBS = "failure_obs"          # §1 "failure/loop obs" (stuck/pivot boundary)

EVENTS: frozenset[str] = frozenset(
    {
        EVENT_TASK_START,
        EVENT_SEARCH_RESULT,
        EVENT_FILE_VIEW,
        EVENT_EDIT_RESULT,
        EVENT_TEST_RESULT,
        EVENT_SUBMIT,
        EVENT_FIRST_VIEW_EDIT,
        EVENT_FAILED_SEARCH,
        EVENT_FAILURE_OBS,
    }
)

# Surfaces = the §1 surface column (which delivery channel carries the fact).
SURFACES: frozenset[str] = frozenset(
    {"brief", "post_search", "post_view", "post_edit", "post_test", "submit", "steer"}
)

# Native renderers = the §1 native_renderer column (the native FORM the fact takes so it
# rides the channel the model already reads — never a bespoke <gt-*> tag).
RENDERERS: frozenset[str] = frozenset(
    {
        "plan",
        "ranked-list",
        "grep-native",
        "contract",
        "compiler-native",
        "diff-native",
        "test-native",
        "list",
        "imperative",
    }
)

# Proof/measurement role of a FACT inventory row. This is deliberately separate
# from render routing: all 11 rows remain in the canonical FACT inventory, while
# an internal producer input cannot borrow model-facing delivery gates.
FACT_ROLE_DELIVERY = "fact_delivery"
FACT_ROLE_INTERNAL_SUPPORT = "internal_support"
FACT_ROLES: frozenset[str] = frozenset(
    {FACT_ROLE_DELIVERY, FACT_ROLE_INTERNAL_SUPPORT}
)

# max_dose sentinel for the ONE unbounded fact class (obligations = the whole plan, not a
# single dose). §1 renders this as "—"; kept as the literal §1 cell value for fidelity.
_DOSE_UNBOUNDED = "—"


class UnregisteredFactError(LookupError):
    """Raised by :func:`assert_registered` for a fact class not in :data:`REGISTRY`.

    ``LookupError`` (not ``KeyError``) so the message is the exact string passed, not the
    repr-mangled form ``KeyError`` produces — a corrupt/unknown fact identity fails loudly
    and legibly (parity with the fail-loud construction guards in ``evidence_envelope``)."""


# --------------------------------------------------------------------------- #
# THE REGISTRATION
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class FactRegistration:
    """One immutable FACT-inventory registration and its typed proof contract.

    Frozen so the table is a constant (no producer can mutate a registration at runtime).
    ``freshness_deps`` is a ``tuple`` (the immutable form of §1's list) so the dataclass is
    hashable and the table round-trips byte-stably.
    """

    fact_class: str            # the stable fact-class identity (== its REGISTRY key)
    producer: str              # the engine that produces it (§1 producer column)
    target_decision: str       # the EXACT agent decision it is meant to change (§1)
    earliest_event: str        # first observation boundary it may deliver at (in EVENTS)
    deliver_by: str            # LAST-useful boundary; deliver later => expire (in EVENTS)
    surface: str               # delivery channel (§1 surface; in SURFACES)
    native_renderer: str       # native FORM (§1 native_renderer; in RENDERERS)
    max_dose: str              # dose ceiling per the §1 max_dose column ("1"/"small"/"—")
    freshness_deps: tuple[str, ...] = field(default=())  # revisions that stale the fact
    receipt_predicate: str = ""  # NAME of the non-reacquisition receipt predicate (for now)
    causal_eval: str = ""        # the paired/counterfactual method that grades causality
    fact_role: str = FACT_ROLE_DELIVERY  # terminal proof role; inventory membership unchanged
    # SS-1 (2026-07-13) — METADATA ONLY (consumed by telemetry; NO behavior change). Whether a
    # consumption ACK is expected for this class. Internal support rows set this false even
    # when they retain a downstream-effect predicate. Read by ack-metrics telemetry; the
    # delivery kernel never branches on it, so it cannot alter a single delivered byte.
    ack_expected: bool = True


def _reg(
    fact_class: str,
    *,
    producer: str,
    target_decision: str,
    earliest_event: str,
    deliver_by: str,
    surface: str,
    native_renderer: str,
    max_dose: str,
    freshness_deps: tuple[str, ...],
    receipt_predicate: str,
    causal_eval: str,
    fact_role: str = FACT_ROLE_DELIVERY,
    ack_expected: bool = True,
) -> FactRegistration:
    """Build one registration (keyword-only so a column is never mis-positioned)."""
    return FactRegistration(
        fact_class=fact_class,
        producer=producer,
        target_decision=target_decision,
        earliest_event=earliest_event,
        deliver_by=deliver_by,
        surface=surface,
        native_renderer=native_renderer,
        max_dose=max_dose,
        freshness_deps=freshness_deps,
        receipt_predicate=receipt_predicate,
        causal_eval=causal_eval,
        fact_role=fact_role,
        ack_expected=ack_expected,
    )


# --------------------------------------------------------------------------- #
# THE REGISTRY — the 11 FACT inventory rows of verifyandobserve.md §1.
#
# Every row is verbatim from the §1 spine table. earliest_event == deliver_by for all (a
# fact about an action is useful only in the observation carrying that action's result);
# the §1 deliver_by qualifier is noted in the trailing comment. receipt_predicate is the
# per-fact NON-REACQUISITION check name (the agent used the fact rather than re-deriving
# it); causal_eval is the paired/counterfactual metric that alone may grade CAUSAL
# (§0 gate item 12: CAUSAL only from a paired / fixed-history counterfactual).
# --------------------------------------------------------------------------- #
_REGISTRATIONS: tuple[FactRegistration, ...] = (
    _reg(
        "obligations",
        producer="spec",
        target_decision="initial plan",
        earliest_event=EVENT_TASK_START,
        deliver_by=EVENT_TASK_START,          # §1: task_start
        surface="brief",
        native_renderer="plan",
        max_dose=_DOSE_UNBOUNDED,             # §1: "—" (the whole plan, not one dose)
        freshness_deps=("issue",),            # §1: issue
        receipt_predicate="plan_reflects_obligations",
        causal_eval="paired_plan_coverage_delta",
    ),
    _reg(
        "localization",
        producer="v1r_brief",
        target_decision="which file to open",
        # T0->T2 re-slot (2026-07-12, design C1): the localization DECISION ("which file to
        # open") is made at D2/post-search, so the HONEST declared boundary is search_result,
        # NOT the T0 task_start self-cert. This unblocks the reactive ranked-localization
        # producer (gateway._produce_ranked_localization): under GT_REGISTRY_ENFORCE a
        # search-fired localization envelope now routes DELIVER (cur=search==want) instead of
        # EXPIRED_LATE (cur=search > task_start). trace_frame (aliased to this class) is
        # UNAFFECTED — its own _EVIDENCE_TYPE_DELIVER_BY=failure_obs override + reactive status
        # win. The T0 baked brief tag is NOT registry-routed (gt_agent._substrate_brief reads
        # brief.txt directly), so this data change does not disturb the baked-brief delivery.
        earliest_event=EVENT_SEARCH_RESULT,
        deliver_by=EVENT_SEARCH_RESULT,       # §1 re-slot: task_start -> search_result (D2)
        surface="brief",
        native_renderer="ranked-list",
        max_dose="small",                     # §1: small
        freshness_deps=("nodes", "edges", "content_rev"),  # §1: nodes+edges+content_rev
        receipt_predicate="opened_ranked_file_without_search",
        causal_eval="paired_steps_to_first_correct_edit",
    ),
    _reg(
        "def_partition",
        producer="post_search",
        target_decision="which def to inspect",
        earliest_event=EVENT_SEARCH_RESULT,
        deliver_by=EVENT_SEARCH_RESULT,       # §1: same search_result
        surface="post_search",
        native_renderer="grep-native",
        max_dose="1",
        freshness_deps=("nodes", "edges_rev"),  # §1: nodes+edges_rev
        receipt_predicate="inspected_partitioned_def",
        causal_eval="paired_wrong_branch_reads_delta",
    ),
    _reg(
        "caller_contract",
        producer="contract_map",
        target_decision="how to modify a fn",
        earliest_event=EVENT_FILE_VIEW,
        deliver_by=EVENT_FILE_VIEW,           # §1: file_view (PRE-EDIT) — last boundary
        #                                       the contract can still shape the edit
        surface="post_view",
        native_renderer="contract",
        max_dose="1",
        freshness_deps=("nodes", "edges", "props_rev"),  # §1: nodes+edges+props_rev
        receipt_predicate="preserved_caller_contract",
        causal_eval="paired_contract_break_rate",
    ),
    _reg(
        "syntax_result",
        producer="edit_check",
        target_decision="is the edit acceptable",
        earliest_event=EVENT_EDIT_RESULT,
        deliver_by=EVENT_EDIT_RESULT,         # §1: same edit_result
        surface="post_edit",
        native_renderer="compiler-native",
        max_dose="1",
        # §1: "—" (no GRAPH-revision dep — a syntax verdict is edit-local). The fact IS
        # keyed to the edit bytes it evaluated (a new edit stales it), so the honest,
        # non-empty freshness dependency is the edit itself.
        freshness_deps=("edit_rev",),
        receipt_predicate="repaired_flagged_syntax",
        causal_eval="paired_syntax_repair_latency",
    ),
    _reg(
        "signature_delta",
        producer="patch_delta",
        target_decision="must callers change",
        earliest_event=EVENT_EDIT_RESULT,
        deliver_by=EVENT_EDIT_RESULT,         # §1: transactional edit_result
        surface="post_edit",
        native_renderer="diff-native",
        max_dose="1",
        freshness_deps=("nodes", "edges_rev"),  # §1: nodes+edges_rev
        receipt_predicate="updated_callers_for_delta",
        causal_eval="paired_caller_breakage_rate",
    ),
    _reg(
        "covering_red",
        producer="covering_runner",
        target_decision="next repair action",
        earliest_event=EVENT_TEST_RESULT,
        deliver_by=EVENT_TEST_RESULT,         # §1: same test_result
        surface="post_test",
        native_renderer="test-native",
        max_dose="1",
        freshness_deps=("patch_rev",),        # §1: patch_rev
        receipt_predicate="targeted_covering_failure",
        causal_eval="paired_repair_targeting_delta",
    ),
    _reg(
        "submit_refusal",
        producer="submit_gate",
        target_decision="is completion allowed",
        earliest_event=EVENT_SUBMIT,
        deliver_by=EVENT_SUBMIT,              # §1: submit interception
        surface="submit",
        native_renderer="test-native",
        max_dose="1",
        freshness_deps=("patch_rev", "graph_rev"),  # §1: patch_rev+graph_rev
        receipt_predicate="halted_on_refusal",
        causal_eval="paired_premature_submit_rate",
    ),
    _reg(
        "cochange_prior",
        producer="curation",
        target_decision="companion-file choice",
        earliest_event=EVENT_FIRST_VIEW_EDIT,
        deliver_by=EVENT_FIRST_VIEW_EDIT,     # §1: first view/edit
        surface="post_view",
        native_renderer="list",
        max_dose="1",
        freshness_deps=("cochange_rev",),     # §1: cochange_rev
        receipt_predicate="opened_companion_file",
        causal_eval="paired_companion_hit_rate",
        # Gateway consumes cochange as a ranking prior and explicitly emits no
        # envelope/native bytes. Keep the canonical FACT row, but never grade it
        # as a fabricated model-facing delivery.
        fact_role=FACT_ROLE_INTERNAL_SUPPORT,
        ack_expected=False,
    ),
    _reg(
        "newfile_precedent",
        producer="change_surface",
        target_decision="destination/integration",
        earliest_event=EVENT_FAILED_SEARCH,
        deliver_by=EVENT_FAILED_SEARCH,       # §1: failed_search/ADD
        surface="post_search",
        native_renderer="list",
        max_dose="1",
        freshness_deps=("closure_rev",),      # §1: closure_rev
        receipt_predicate="created_at_precedent_path",
        causal_eval="paired_integration_correctness",
    ),
    _reg(
        "recovery",
        producer="governor",
        target_decision="whether to pivot",
        earliest_event=EVENT_FAILURE_OBS,
        deliver_by=EVENT_FAILURE_OBS,         # §1: same failure/loop obs
        surface="steer",
        native_renderer="imperative",
        max_dose="1",
        freshness_deps=("episode_state",),    # §1: episode_state
        receipt_predicate="pivoted_after_steer",
        causal_eval="paired_pivot_after_stuck_rate",
    ),
)

REGISTRY: dict[str, FactRegistration] = {r.fact_class: r for r in _REGISTRATIONS}


# --------------------------------------------------------------------------- #
# EVIDENCE-TYPE ALIASES (Graph-F2, bounce 2026-07-10) — the delivery-kernel's own
# vocabulary bridge.
#
# The Gateway emits a FINER-grained ``evidence_type`` vocabulary than the 11 canonical
# §1 fact classes (``def_ref_partition``/``name_fold``/``wrong_surface``/``body_concept``
# are all post_search ``def_partition`` variants; ``covering_verdict`` is ``covering_red``;
# ``cochange_partner`` is ``cochange_prior``; …). BEFORE this map, the registry keys and
# the shipped ``evidence_type`` strings DISAGREED, so wiring the render gate on the raw
# key (``if not renderable(fc): return``) would have silenced EVERY gateway fact. This
# table maps each shipped evidence_type -> its canonical registered fact class so
# :func:`renderable` passes every legit shipped fact and blocks only a truly-unregistered
# type. A dynamic ``missing_role:<role>`` is normalized to its ``missing_role`` base
# before lookup. Every VALUE must be a real REGISTRY key (enforced in :func:`_self_check`).
_EVIDENCE_TYPE_ALIASES: dict[str, str] = {
    # The proactive step-0 orientation is a localization FACT, but it serves the
    # initial-plan/open-file decision before the agent's first tool action.  Keep
    # that evidence identity distinct from the reactive ranked-localization
    # producer, whose honest boundary is search_result.
    "brief_localization": "localization",
    # Requirement status re-evaluated from fresh edit/test evidence. It is the
    # obligations FACT, but its useful boundary is the current test result rather
    # than the task-start plan boundary (declared below).
    "obligation_unexercised": "obligations",
    # Coherence-v2 observes edit churn and asks for a recovery pivot. It is a
    # recovery FACT with its own edit-result decision boundary, not a generic
    # governor failure-observation event.
    "coherence_collapse": "recovery",
    # post_search def-partition family (all answer "which def to inspect")
    "def_ref_partition": "def_partition",
    "name_fold": "def_partition",
    "wrong_surface": "def_partition",
    "body_concept": "def_partition",
    # a stack-frame localizer answers "which file to open"
    "trace_frame": "localization",
    # patch_delta signature/registration facts answer "must callers/registrations change"
    "signature_mismatch": "signature_delta",
    "companion_surface": "signature_delta",
    # SM-2b (2026-07-11): the CROSS-LANGUAGE gateway caller-contract break. It answers the
    # caller_contract DECISION ("how to modify a fn": preserve/update the call sites) so it
    # aliases to ``caller_contract`` — but it is produced by the graph-based gateway producer
    # on the EDIT event (not contract_map's PRE-EDIT file_view), so its boundary is overridden
    # to ``edit_result`` in ``_EVIDENCE_TYPE_DELIVER_BY`` (the trace_frame pattern: alias by
    # DECISION, override by TIMING). This is the honest §26.4 replacement for the legacy
    # l3.contract/l3b.evidence caller-break that patch_delta (ast-only) cannot produce.
    "caller_break": "caller_contract",
    # Structured PRE-EDIT source-view attachment. It uses the exact viewed path
    # and FACT-tier graph callers, rather than inferring a path from command text.
    "caller_contract_view": "caller_contract",
    # PRE-EDIT mirror of caller_break (2026-07-20): the caller-contract facts that ALREADY
    # ride the pre-edit ``def_partition`` physical delivery. When a symbol resolves to a
    # single def file, ``_resolve_symbol_defs`` sets ``callers_render =
    # _caller_contract_for_file(...)`` (the contract_map engine, every leak guard inherited)
    # and ``_fmt_def_facts`` renders it as the ``callers: …`` line of the def block delivered
    # at ``search_result``. Those bytes answer the caller_contract DECISION ("how to modify a
    # fn") but were only ever TYPED def_partition, so the canonical caller_contract class
    # (boundary file_view) could never be credited from this pre-edit delivery. Aliasing this
    # co-fact type to ``caller_contract`` by DECISION and overriding its boundary to
    # ``search_result`` by TIMING (below) lets the co-fact grade against its OWN honest window
    # without disturbing the canonical file_view row — exactly the caller_break precedent, one
    # boundary earlier. INERT until the delivery seam emits it (co_evidence_type, dose-once).
    "caller_contract_search": "caller_contract",
    # companion-file + new-file/integration facts
    "cochange_partner": "cochange_prior",
    "new_file_destination": "newfile_precedent",
    "missing_role": "newfile_precedent",
    # The post-creation half of newfile_precedent answers the integration
    # decision after the file exists.  Keep it distinct from the pre-creation
    # failed-search ``missing_role:*`` family so each can retain its honest
    # boundary and receipt semantics.
    "missing_role_postcreate": "newfile_precedent",
    # executed covering RED
    "covering_verdict": "covering_red",
}


def _base_fact_class(fact_class: str) -> str:
    """The lookup base of ``fact_class``: the part before the first ``:`` (so a dynamic
    ``missing_role:handler`` maps to ``missing_role``). Registered §1 keys carry no ``:``,
    so this is a no-op for them."""
    return (fact_class or "").strip().split(":", 1)[0]


def _canonical_fact_class(fact_class: str) -> str | None:
    """Resolve ``fact_class`` (a registered §1 key, a gateway ``evidence_type`` alias, or
    a dynamic ``base:suffix`` form) to its canonical REGISTRY key, or ``None`` when it is
    neither registered nor a known alias. The single resolver behind :func:`renderable`."""
    fc = (fact_class or "").strip()
    if not fc:
        return None
    if fc in REGISTRY:
        return fc
    base = _base_fact_class(fc)
    if base in REGISTRY:
        return base
    alias = _EVIDENCE_TYPE_ALIASES.get(fc) or _EVIDENCE_TYPE_ALIASES.get(base)
    return alias if alias in REGISTRY else None


# --------------------------------------------------------------------------- #
# import-time self-check — a malformed registration is a programming bug; fail LOUD at
# import (fail-closed) so a bad table can never ship a fact with an empty field, an
# unknown event/surface/renderer, or a duplicate/mismatched key.
# --------------------------------------------------------------------------- #
def _self_check() -> None:
    if len(REGISTRY) != len(_REGISTRATIONS):  # a duplicate fact_class collapsed the dict
        raise ValueError("fact_registry: duplicate fact_class in REGISTRY")
    _scalars = (
        "fact_class",
        "producer",
        "target_decision",
        "earliest_event",
        "deliver_by",
        "surface",
        "native_renderer",
        "max_dose",
        "receipt_predicate",
        "causal_eval",
    )
    for key, reg in REGISTRY.items():
        if reg.fact_class != key:
            raise ValueError(
                f"fact_registry: key {key!r} != fact_class {reg.fact_class!r}"
            )
        for name in _scalars:
            val = getattr(reg, name)
            if not isinstance(val, str) or not val.strip():
                raise ValueError(f"fact_registry: {key}.{name} is empty/non-str")
        if not reg.freshness_deps or not all(
            isinstance(d, str) and d.strip() for d in reg.freshness_deps
        ):
            raise ValueError(f"fact_registry: {key}.freshness_deps empty/malformed")
        if reg.earliest_event not in EVENTS:
            raise ValueError(f"fact_registry: {key}.earliest_event {reg.earliest_event!r}")
        if reg.deliver_by not in EVENTS:
            raise ValueError(f"fact_registry: {key}.deliver_by {reg.deliver_by!r}")
        if reg.surface not in SURFACES:
            raise ValueError(f"fact_registry: {key}.surface {reg.surface!r}")
        if reg.native_renderer not in RENDERERS:
            raise ValueError(f"fact_registry: {key}.native_renderer {reg.native_renderer!r}")
        # SS-1: ack_expected is metadata but MUST be a strict bool (a str/None would poison
        # the telemetry join that reads it) — fail LOUD at import like the other field guards.
        if not isinstance(reg.ack_expected, bool):
            raise ValueError(f"fact_registry: {key}.ack_expected is not a bool")
        if reg.fact_role not in FACT_ROLES:
            raise ValueError(f"fact_registry: {key}.fact_role {reg.fact_role!r}")
        if (
            reg.fact_role == FACT_ROLE_INTERNAL_SUPPORT
            and reg.ack_expected is not False
        ):
            raise ValueError(
                f"fact_registry: {key} internal support cannot expect delivery ack"
            )
    # Graph-F2: the evidence-type alias map is part of the vocabulary contract — a bad
    # alias must fail LOUD at import, never silently mis-route (or silence) a shipped fact.
    for src, dst in _EVIDENCE_TYPE_ALIASES.items():
        if not isinstance(src, str) or not src.strip() or ":" in src:
            raise ValueError(f"fact_registry: alias key {src!r} empty / not a base type")
        if src in REGISTRY:
            raise ValueError(f"fact_registry: alias key {src!r} shadows a real REGISTRY key")
        if dst not in REGISTRY:
            raise ValueError(f"fact_registry: alias {src!r} -> {dst!r} is not a REGISTRY key")


_self_check()


# --------------------------------------------------------------------------- #
# PUBLIC API — lookup + the core render-gating invariant.
# --------------------------------------------------------------------------- #
def registration(fact_class: str) -> FactRegistration | None:
    """The registration for ``fact_class``, or ``None`` if it is not registered.

    ``None`` (never a KeyError) so a caller can branch on registration status without a
    try/except; :func:`assert_registered` is the raising variant."""
    return REGISTRY.get(fact_class)


def is_registered(fact_class: str) -> bool:
    """True iff ``fact_class`` is a known canonical FACT inventory row."""
    return fact_class in REGISTRY


def renderable(fact_class: str) -> bool:
    """THE CORE INVARIANT — unregistered facts must NEVER render.

    Returns ``True`` iff ``fact_class`` resolves (directly, via a gateway
    ``evidence_type`` alias, or via a dynamic ``base:suffix`` form) to a registered §1
    fact class. The delivery kernel gates every model-facing render behind this predicate
    (``if not renderable(fc): return`` — correct-or-quiet, WIRED in
    :func:`groundtruth.runtime.gateway.augment`): a fact with no registration has no
    declared decision, deliver-by boundary, or dose, so it cannot be delivered on-time and
    must be silent.

    Graph-F2 (bounce 2026-07-10): the Gateway emits a FINER vocabulary than the 11
    canonical §1 keys, so this consults :data:`_EVIDENCE_TYPE_ALIASES` (unlike the STRICT
    :func:`is_registered`, which is true only for a literal §1 key). This is the sanctioned
    divergence the prior docstring anticipated ("can gain render-eligibility checks later").
    """
    return _canonical_fact_class(fact_class) is not None


def assert_registered(fact_class: str) -> FactRegistration:
    """Return the registration for ``fact_class`` or raise :class:`UnregisteredFactError`.

    The raising guard for the render path: a producer that reaches a render with an
    unregistered fact class is a bug, and this fails loud rather than silently emitting an
    undeclared fact."""
    reg = REGISTRY.get(fact_class)
    if reg is None:
        raise UnregisteredFactError(
            f"fact class {fact_class!r} is not registered — unregistered facts must "
            f"never render (register it in fact_registry.REGISTRY first)"
        )
    return reg


def all_fact_classes() -> tuple[str, ...]:
    """All registered fact classes, deterministically SORTED (stable across calls/processes
    — no set-iteration order leaks in). The canonical 11-row FACT inventory; consult
    :func:`fact_role_for` before applying a model-delivery proof contract."""
    return tuple(sorted(REGISTRY))


def evidence_grain_for(fact_class: str) -> tuple[str, ...]:
    """The FINER producer / evidence-type identifiers that all COLLAPSE to canonical
    ``fact_class`` — the grain the 11-key §1 vocabulary hides. For ``localization`` this surfaces
    BOTH the ``trace_frame`` evidence-type alias AND the ``ranked_localization`` producer, so a
    loc_reslot audit can tell a stack-frame localizer from a ranked-localization delivery even
    though both share fact_class ``localization`` (Cluster-5 ITEM 3, 2026-07-18).

    PURE + registry-derived — reads ONLY the existing authority tables and NEVER mutates the
    canonical mapping: (a) evidence-type aliases whose canonical class is ``fact_class``,
    (b) the class's registered producers (``_EVIDENCE_TYPE_PRODUCERS``), (c) the class's own
    registration producer. Returns ``()`` for an unregistered class; sorted + de-duplicated.
    """
    canon = _canonical_fact_class(fact_class)
    if canon is None or canon not in REGISTRY:
        return ()
    grain: set[str] = {src for src, dst in _EVIDENCE_TYPE_ALIASES.items() if dst == canon}
    grain |= set(_EVIDENCE_TYPE_PRODUCERS.get(canon, frozenset()))
    reg = REGISTRY.get(canon)
    if reg is not None and getattr(reg, "producer", ""):
        grain.add(reg.producer)
    return tuple(sorted(grain))


def fact_role_for(fact_class: str) -> str | None:
    """Typed proof role for a canonical or shipped FACT identity.

    ``None`` is fail-closed for unregistered identities. Inventory membership,
    renderer routing, and model-visible bytes are intentionally unchanged.
    """
    reg = registration_for(fact_class)
    return reg.fact_role if reg is not None else None


# --------------------------------------------------------------------------- #
# EXECUTABLE REGISTRY — SM-0 Super-Mode spine (2026-07-11).
#
# BEFORE this block the registry was DESCRIPTIVE: the delivery kernel read only
# :func:`renderable` (a membership boolean); every registration's ``deliver_by`` /
# ``native_renderer`` / ``freshness_deps`` was STORED and never consumed, and the Gateway
# duplicated freshness in a parallel hardcoded table that could silently diverge. These
# accessors make the registration EXECUTABLE so the kernel can enforce, per fact class:
#   * the DECLARED delivery boundary (``required_event`` — the firing event must match it),
#   * the DECLARED native renderer (``required_renderer`` — a renderer-less class is dropped),
#   * the SINGLE-SOURCE freshness surfaces (``freshness_surfaces`` — one owner, not a copy).
# All gated in the Gateway behind ``GT_REGISTRY_ENFORCE``; this module is a pure declaration.
# --------------------------------------------------------------------------- #

# A few FINER evidence_types deliver at a boundary DIFFERENT from their coarse §1 canonical
# class, because :data:`_EVIDENCE_TYPE_ALIASES` maps by DECISION, not by TIMING. ``trace_frame``
# answers the localization DECISION ("which file to open") so it aliases to ``localization`` —
# but a stack-frame localizer is delivered REACTIVELY at a failure/error OBSERVATION, NOT at
# the ``task_start`` brief boundary of the v1r ``localization`` class. Recording the real
# boundary here (fix the DECLARATION, not the producer) is what lets enforcement keep the
# genuinely-correct ``trace_frame`` producer green while still catching a truly wrong-event
# fact. Keyed by evidence_type (or its ``base:suffix`` base); overrides BOTH earliest+deliver_by.
_EVIDENCE_TYPE_DELIVER_BY: dict[str, str] = {
    "brief_localization": EVENT_TASK_START,
    "obligation_unexercised": EVENT_TEST_RESULT,
    "coherence_collapse": EVENT_EDIT_RESULT,
    "trace_frame": EVENT_FAILURE_OBS,
    # SM-2b: the gateway ``caller_break`` aliases to ``caller_contract`` (deliver_by file_view,
    # PRE-EDIT — contract_map's boundary) but is produced by the graph-based gateway producer
    # ON the edit (post-signature-change), so its real last-useful boundary is ``edit_result``
    # (the callers must be updated in response to THIS edit). Overriding it here keeps
    # GT_REGISTRY_ENFORCE from EXPIRING a genuinely on-time edit-boundary caller-break while
    # still catching a truly wrong-event fact — exactly the trace_frame precedent above.
    "caller_break": EVENT_EDIT_RESULT,
    "caller_contract_view": EVENT_FILE_VIEW,
    # PRE-EDIT mirror of caller_break (2026-07-20): the ``caller_contract_search`` co-fact
    # aliases to ``caller_contract`` (canonical deliver_by file_view, PRE-EDIT) but is carried
    # by the ``def_partition`` physical delivery that fires at ``search_result`` (the agent's
    # own grep). Its real last-useful boundary is therefore ``search_result`` — the callers
    # shape the edit from the moment the def partition lands. Overriding it here keeps
    # enforcement from reporting WRONG_EVENT for a genuinely on-time pre-edit caller contract,
    # exactly as ``caller_break`` does one boundary later (edit_result). Not reactive: it has a
    # FIXED boundary, so the non-reactive wrong-event check still guards a truly mistimed fire.
    "caller_contract_search": EVENT_SEARCH_RESULT,
    # Post-creation integration guidance is generated synchronously from the
    # creating edit.  It is not the pre-creation destination suggestion carried
    # by ``missing_role:*`` at failed_search.
    "missing_role_postcreate": EVENT_EDIT_RESULT,
    # B-BND (b2): the SEAM's executed covering RED. Its canonical ``covering_red`` class
    # declares deliver_by=test_result (a covering verdict riding the agent's OWN test result),
    # but the ``covering_runner`` producer RUNS the covering test ITSELF at EDIT time and
    # delivers the RED INTO the post_edit observation (``gt_mini_patch._executed_covering_
    # emission`` stamps ``event_type='post_edit'`` and stages ``verify.horizon.executed``; the
    # delivered ledger rows on keras/dynaconf carry event_type='post_edit'). So this producer's
    # real last-useful boundary is ``edit_result`` (the edit it is targeting for the next
    # repair), NOT the agent's later test_result — exactly the caller_break precedent. Keyed on
    # the SEAM evidence_type ``covering_red`` ONLY; the GATEWAY's test-event covering wrap ships
    # ``covering_verdict`` (``gateway._produce_covering`` fact_kind), which KEEPS the canonical
    # test_result boundary — the two producers are split by evidence_type so neither is broken.
    "covering_red": EVENT_EDIT_RESULT,
}

# Observation-REACTIVE evidence types: produced FROM the current observation's OWN content (a
# stack trace in the output) and delivered AT that observation. They have no single fixed
# lifecycle boundary — the trace may ride a test, a command (`other`), an edit, or a view
# observation — so the firing event is on-time BY CONSTRUCTION and the DEFER/EXPIRED_LATE
# boundary check does NOT apply (only freshness still gates). This is precisely why
# ``trace_frame`` must NOT be pinned to its canonical ``localization`` task_start boundary:
# it is a reactive failure-observation localizer, not a step-0 brief.
#
# B-BND (b2): ``covering_red`` (the SEAM's executed covering RED) is ALSO on-time BY
# CONSTRUCTION — GT runs the covering test and delivers the verdict SYNCHRONOUSLY into the same
# post_edit observation, so it can never be LATE relative to the edit it targets. The seam,
# however, stamps the delivered row's ``actual_event='test_result'`` (the canonical class
# label) even though it delivers at the edit boundary; without reactive treatment the
# adjudicator's non-reactive ``actual_event != required_event`` check (edit_result vs
# test_result) would reject a genuinely on-time covering RED. Marking it reactive is the same
# on-time-by-construction relaxation as trace_frame and is INERT on both live paths: the GATEWAY
# reactive branch (gateway.route_delivery) only ever sees ``covering_verdict`` (never
# ``covering_red``), and the seam's ``_ga_is_preventive`` gate already returns False for the
# ``executed_world_fact`` ladder class (∉ _GA_PREVENTIVE_CLASSES) — so this cannot suppress or
# re-time a single live delivery.
_REACTIVE_EVIDENCE_TYPES: frozenset[str] = frozenset({"trace_frame", "covering_red"})


def registration_for(evidence_type: str) -> "FactRegistration | None":
    """The :class:`FactRegistration` for a SHIPPED ``evidence_type`` — resolving it directly,
    via :data:`_EVIDENCE_TYPE_ALIASES`, or via a dynamic ``base:suffix`` form (the SAME
    resolution :func:`renderable` uses), or ``None`` when it resolves to no registered class.
    The executable counterpart of :func:`registration` (which is STRICT — literal §1 keys only)."""
    canon = _canonical_fact_class(evidence_type)
    return REGISTRY.get(canon) if canon is not None else None


# Fine evidence types are allowed to have a live producer whose name differs
# from the canonical class producer.  This is an explicit authority table, not
# a string/prefix inference: adding a new producer requires registering it here.
_EVIDENCE_TYPE_PRODUCERS: dict[str, frozenset[str]] = {
    "brief_localization": frozenset({"v1r_brief"}),
    "obligation_unexercised": frozenset({"spec"}),
    "coherence_collapse": frozenset({"ss_coherence_v2"}),
    "localization": frozenset({"v1r_brief", "ranked_localization"}),
    "def_ref_partition": frozenset({"def_ref_partition"}),
    "name_fold": frozenset({"name_fold"}),
    "wrong_surface": frozenset({"wrong_surface"}),
    "body_concept": frozenset({"body_concept"}),
    "trace_frame": frozenset({"trace"}),
    "caller_break": frozenset({"caller_contract"}),
    "caller_contract_view": frozenset({"caller_contract"}),
    # Gateway envelopes use the compact ``covering`` producer id; the native
    # mini-seam execution path is owned by the concrete covering_runner engine.
    "covering_verdict": frozenset({"covering", "covering_runner"}),
}


def producer_matches(evidence_type: str, runtime_producer_id: str) -> bool:
    """Whether a runtime producer is authoritative for this evidence type."""

    reg = registration_for(evidence_type)
    if reg is None or not runtime_producer_id:
        return False
    evidence_base = (evidence_type or "").strip().split(":", 1)[0]
    allowed = _EVIDENCE_TYPE_PRODUCERS.get(
        evidence_type,
        _EVIDENCE_TYPE_PRODUCERS.get(evidence_base, frozenset({reg.producer})),
    )
    return runtime_producer_id in allowed


def required_event(evidence_type: str) -> str | None:
    """The LAST-useful delivery boundary (``deliver_by``, a fine :data:`EVENTS` name) for a
    shipped ``evidence_type``, or ``None`` when unregistered. Honors the per-evidence-type
    boundary override (:data:`_EVIDENCE_TYPE_DELIVER_BY`) so a finer type whose real boundary
    differs from its canonical class reports its OWN boundary."""
    reg = registration_for(evidence_type)
    if reg is None:
        return None
    et = (evidence_type or "").strip()
    return (
        _EVIDENCE_TYPE_DELIVER_BY.get(et)
        or _EVIDENCE_TYPE_DELIVER_BY.get(_base_fact_class(et))
        or reg.deliver_by
    )


def earliest_event_for(evidence_type: str) -> str | None:
    """The EARLIEST boundary a shipped ``evidence_type`` may deliver at (``earliest_event``),
    or ``None`` unregistered. Honors the same per-type override as :func:`required_event`
    (earliest == deliver_by for every §1 class and for the overrides)."""
    reg = registration_for(evidence_type)
    if reg is None:
        return None
    et = (evidence_type or "").strip()
    return (
        _EVIDENCE_TYPE_DELIVER_BY.get(et)
        or _EVIDENCE_TYPE_DELIVER_BY.get(_base_fact_class(et))
        or reg.earliest_event
    )


def is_reactive(evidence_type: str) -> bool:
    """True iff ``evidence_type`` is observation-REACTIVE (produced from and delivered at the
    current observation, with no fixed lifecycle boundary — e.g. ``trace_frame``). Enforcement
    treats such a fact as on-time at its firing event (boundary check inapplicable; freshness
    still applies)."""
    et = (evidence_type or "").strip()
    return et in _REACTIVE_EVIDENCE_TYPES or _base_fact_class(et) in _REACTIVE_EVIDENCE_TYPES


def required_renderer(evidence_type: str) -> str | None:
    """The DECLARED native renderer (a :data:`RENDERERS` name) for a shipped ``evidence_type``,
    or ``None`` when it resolves to no registered class. A registered class ALWAYS has a
    renderer (import-time :func:`_self_check` enforces ``native_renderer`` ∈ RENDERERS), so a
    ``None`` here means truly-unregistered — the enforcement drops it correct-or-quiet."""
    reg = registration_for(evidence_type)
    return reg.native_renderer if reg is not None else None


def ack_expected(evidence_type: str) -> bool:
    """SS-1 (2026-07-13): whether a consumption ACK is expected for a shipped ``evidence_type``
    (resolving via alias / ``base:suffix`` exactly like :func:`required_renderer`). METADATA
    ONLY — read by the ack-metrics telemetry (GT_SS_ACK_METRICS), never by the delivery kernel,
    so it changes no delivered byte. An unregistered type -> ``False`` (there is no fact whose
    ack could be expected)."""
    reg = registration_for(evidence_type)
    return bool(reg.ack_expected) if reg is not None else False


# --------------------------------------------------------------------------- #
# FRESHNESS — the SINGLE SOURCE OF TRUTH for per-evidence-type graph-surface dependencies.
#
# Freshness is genuinely PER-EVIDENCE-TYPE (finer than the 11 canonical classes): four
# gateway evidence_types share the canonical class ``def_partition`` yet ``body_concept``
# additionally depends on the body-FTS surface while its siblings do not, so collapsing to
# the canonical class would LOSE that distinction. This table (formerly the Gateway's private
# ``_FACTCLASS_FRESHNESS_SURFACES``, relocated here so there is ONE owner) is the operational
# truth; :func:`_self_check_executable` ties it back to the coarse §1 ``freshness_deps`` so it
# can no longer SILENTLY diverge. Tuple ORDER is load-bearing (the Gateway hashes the surfaces
# in order) — do not reorder.
FRESHNESS_SURFACES: dict[str, tuple[str, ...]] = {
    "def_ref_partition": ("nodes", "edges"),
    "name_fold": ("nodes", "edges"),
    "wrong_surface": ("nodes", "edges"),
    # Trace frames come from parser-validated repository paths, not graph.db.
    "signature_mismatch": ("nodes", "edges"),
    "companion_surface": ("nodes", "edges"),
    "cochange_partner": ("nodes", "edges", "cochanges"),
    # SM-2b: the caller-break reads ONLY the CALLS graph (function/method nodes + CALLS edges)
    # to count FACT-tier cross-file callers of the edited symbol — it does NOT read the
    # per-symbol ``properties`` surface (a documented narrowing of caller_contract's canonical
    # deps below), so a properties-only reindex must not stale it.
    "caller_break": ("nodes", "edges"),
    "caller_contract_view": ("nodes", "edges"),
    "body_concept": ("nodes", "edges", "content_fts"),
    "new_file_destination": ("nodes", "edges", "closure"),
    "missing_role": ("nodes", "edges", "closure"),
    "missing_role_postcreate": ("nodes", "edges", "closure"),
}

# PATCH-bound classes: freshness is the working-tree PATCH, not any GRAPH surface (the
# registry ``freshness_deps`` name is ``patch_rev``). Produced AND delivered on the same
# event, so a graph change must never falsely stale them -> empty ``valid_until``.
# NOT a home for merely graph-INDEPENDENT classes. `_self_check_executable` enforces that
# every member carries a `patch_rev` registry dep ("patch-bound 'X' lacks a patch_rev registry
# dep"), so membership means "keyed to the patch", not "does not read graph.db".
#
# I tried adding `trace_frame` here on 2026-07-28 and the self-check rejected it immediately --
# correctly. Recording the attempt so it is not retried: `_produce_trace` (`gateway.py:2773`)
# genuinely opens no database (it is `parse_stack_traces` + `_to_repo_rel`, declaring
# `observed_substrates=("repository_paths",)`), so the PREMISE behind removing it from
# `FRESHNESS_SURFACES` is true -- but this is the wrong mechanism to express it, and there is
# no right one today.
#
# Consequence of the current state, stated plainly: an unmapped class falls through
# `if not surfaces or not subs: return graph_rev, graph_rev`, so `trace_frame` is now staled by
# the WHOLE-GRAPH composite -- including `properties`/`closure`/`cochanges`, which it never
# reads. That is MORE conservative than before, i.e. it over-expires rather than serving stale
# facts, which is the safe direction and matches the docstring's "over-conservative, never
# stale". It is a precision loss, not a correctness hole.
#
# The real residual is the CROSS-CHECK: `registry_graph_surfaces("trace_frame")` still returns
# ("nodes","edges","content_fts"), so the class claims graph deps it does not have, and the
# `_SURFACE_NARROWINGS` deletion means `_self_check_executable` no longer covers it -- it
# passes by EXCLUSION rather than by satisfaction. Fixing that properly needs either a
# GRAPH_INDEPENDENT_FACTCLASSES mechanism (returning `(graph_rev, "")` with no patch_rev
# requirement) or corrected canonical deps for the class.
PATCH_BOUND_FACTCLASSES: frozenset[str] = frozenset({"covering_verdict"})

# The physical DB sub-revision surfaces (``subrev_<surface>``) a freshness token may key on.
_KNOWN_DB_SURFACES: frozenset[str] = frozenset(
    {"nodes", "edges", "content_fts", "closure", "cochanges", "properties"}
)

# Translation: a registry ``freshness_deps`` dep-name -> the DB ``subrev_<surface>`` name it
# corresponds to (``None`` = a NON-graph dep: the patch/edit/episode/issue/graph composite,
# which has no per-surface sub-revision). Kept NEXT TO the registry so the coarse §1 deps and
# the operational surfaces cannot drift apart unnoticed (:func:`_self_check_executable`).
_DEP_TO_DB_SURFACE: dict[str, str | None] = {
    "nodes": "nodes",
    "edges": "edges",
    "edges_rev": "edges",
    "content_rev": "content_fts",
    "props_rev": "properties",
    "cochange_rev": "cochanges",
    "closure_rev": "closure",
    "patch_rev": None,
    "graph_rev": None,
    "edit_rev": None,
    "episode_state": None,
    "issue": None,
}

# Documented NARROWINGS apply only to graph-backed evidence. Trace frames are
# parser-derived from validated repository paths and never read graph.db, so
# they are absent here even though canonical localization revision dependencies
# remain conservatively broader.
_SURFACE_NARROWINGS: dict[str, frozenset[str]] = {
    # SM-2b: the gateway caller-break derives its "N callers in M files" purely from the CALLS
    # graph (nodes + edges); it never reads the per-symbol ``properties`` surface that its
    # canonical ``caller_contract`` class (contract_map, which DOES render property facts)
    # depends on. Documenting the drop keeps the cross-check honest instead of tripping it.
    "caller_break": frozenset({"properties"}),
    "caller_contract_view": frozenset({"properties"}),
}


def registry_graph_surfaces(evidence_type: str) -> tuple[str, ...]:
    """The DB graph surfaces DERIVED from the canonical class's registry ``freshness_deps``
    (translated via :data:`_DEP_TO_DB_SURFACE`, dropping the non-graph deps). The COARSE §1
    view of a fact's freshness — used by the cross-check to keep the operational
    :data:`FRESHNESS_SURFACES` honest, not by the live freshness computation."""
    reg = registration_for(evidence_type)
    if reg is None:
        return ()
    out: list[str] = []
    for dep in reg.freshness_deps:
        surf = _DEP_TO_DB_SURFACE.get(dep, dep)  # unknown dep -> itself (surfaces loudly)
        if surf and surf not in out:
            out.append(surf)
    return tuple(out)


def is_patch_bound(evidence_type: str) -> bool:
    """True iff ``evidence_type`` (or its ``base:suffix`` base) is a PATCH-bound class whose
    freshness is the working-tree patch, not any graph surface."""
    et = (evidence_type or "").strip()
    return et in PATCH_BOUND_FACTCLASSES or _base_fact_class(et) in PATCH_BOUND_FACTCLASSES


def freshness_surfaces(evidence_type: str) -> tuple[str, ...] | None:
    """The SINGLE-SOURCE per-evidence-type DB graph surfaces whose ``subrev_<surface>`` a fact
    of this type depends on, or ``None`` when the type is unmapped (the caller then fails SAFE
    to the whole-graph composite — over-conservative, never stale). A ``base:suffix`` form
    (``missing_role:handler``) resolves to its base. Patch-bound classes return ``None`` here
    (the caller checks :func:`is_patch_bound` first and never reaches this)."""
    et = (evidence_type or "").strip()
    if not et:
        return None
    return FRESHNESS_SURFACES.get(et) or FRESHNESS_SURFACES.get(_base_fact_class(et))


# --------------------------------------------------------------------------- #
# import-time cross-check for the executable structures — ties the operational tables back
# to the registry so a future edit that makes them DIVERGE fails LOUD at import.
# --------------------------------------------------------------------------- #
def _self_check_executable() -> None:
    for evidence_type, producers in _EVIDENCE_TYPE_PRODUCERS.items():
        if registration_for(evidence_type) is None:
            raise ValueError(
                f"fact_registry: producer authority {evidence_type!r} resolves to no class"
            )
        if not producers or not all(
            isinstance(producer, str) and producer.strip() for producer in producers
        ):
            raise ValueError(
                f"fact_registry: producer authority {evidence_type!r} is empty/malformed"
            )
    # every per-type freshness key must resolve to a registered class, list only KNOWN DB
    # surfaces, be non-empty, and NOT also be patch-bound.
    for et, surfaces in FRESHNESS_SURFACES.items():
        if registration_for(et) is None:
            raise ValueError(f"fact_registry: FRESHNESS_SURFACES key {et!r} resolves to no class")
        if is_patch_bound(et):
            raise ValueError(f"fact_registry: {et!r} is both patch-bound and graph-freshness-mapped")
        if not surfaces or not all(s in _KNOWN_DB_SURFACES for s in surfaces):
            raise ValueError(f"fact_registry: FRESHNESS_SURFACES[{et!r}] has an unknown/empty surface")
        # the operational surfaces MUST cover the canonical class's graph deps, minus any
        # DOCUMENTED narrowing — a silent DROP of a canonical dep (drift) fails here.
        canonical = set(registry_graph_surfaces(et)) - _SURFACE_NARROWINGS.get(et, frozenset())
        missing = canonical - set(surfaces)
        if missing:
            raise ValueError(
                f"fact_registry: FRESHNESS_SURFACES[{et!r}]={surfaces} silently drops "
                f"canonical graph dep(s) {sorted(missing)} (document a narrowing or fix the table)"
            )
    # every patch-bound class must DECLARE patch freshness in the registry (ties the Gateway's
    # patch-boundness to the §1 ``freshness_deps`` — not a free-floating literal).
    for pb in PATCH_BOUND_FACTCLASSES:
        reg = registration_for(pb)
        if reg is None or "patch_rev" not in reg.freshness_deps:
            raise ValueError(f"fact_registry: patch-bound {pb!r} lacks a patch_rev registry dep")
    # every per-type boundary override must name a real event and resolve to a real class.
    for et, ev in _EVIDENCE_TYPE_DELIVER_BY.items():
        if ev not in EVENTS:
            raise ValueError(f"fact_registry: boundary override {et!r} -> {ev!r} not in EVENTS")
        if registration_for(et) is None:
            raise ValueError(f"fact_registry: boundary override key {et!r} resolves to no class")


_self_check_executable()
