"""fact_registry — the fact-to-decision registry (the spine; verifyandobserve.md §1).

RL-adherent delivery has ONE unifying rule that must hold BEFORE any producer renders a
byte: **every model-facing fact class is statically registered, and an UNREGISTERED fact
must never render.** This module is that registry — a pure, deterministic, LLM-free table
mapping each of the 11 §1 fact classes to the decision it is meant to change, the
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
    "is_registered",
    "renderable",
    "assert_registered",
    "all_fact_classes",
    "UnregisteredFactError",
    # controlled vocabularies (the only legal values for the enum-ish fields)
    "EVENTS",
    "SURFACES",
    "RENDERERS",
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
    """One immutable fact-class registration — the static declaration of a model-facing
    fact's decision, timing, surface, form, dose, freshness, and grading.

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
    )


# --------------------------------------------------------------------------- #
# THE REGISTRY — the 11 model-facing fact classes of verifyandobserve.md §1.
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
        earliest_event=EVENT_TASK_START,
        deliver_by=EVENT_TASK_START,          # §1: task_start
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
    # companion-file + new-file/integration facts
    "cochange_partner": "cochange_prior",
    "new_file_destination": "newfile_precedent",
    "missing_role": "newfile_precedent",
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
    """True iff ``fact_class`` is a known model-facing fact class."""
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
    — no set-iteration order leaks in). The canonical enumeration of the model-facing fact
    universe."""
    return tuple(sorted(REGISTRY))
