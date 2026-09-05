"""GT global arbiter (SM-5) — the ONE ranked competition over all delivery planes.

Before SM-5 the mini seam shipped GT bytes through FOUR independent planes per
observation — the task-start brief, the always-on Lane-A data plane (uncapped, no
arbitration), the Lane-B steer gate (<=1 steer), and the GT_GATEWAY fact plane (<=1
fact via ``adapters.miniswe.arbitrate``). Three of them (Lane-A, Lane-B, Gateway) can
each append to the SAME observation on the SAME turn, so the model could receive up to
``N+1+1`` GT doses in one tool result, and the three planes deduped on THREE different
key derivations (Lane-A content+state hash, Lane-B gate hash, Gateway envelope
``dedup_key``) — a fact delivered on one plane did not suppress the same fact on
another.

This module is the SM-5 collapse: a PURE, deterministic, LLM-free ranked competition
that takes the eligible candidates from ALL planes and returns AT MOST ONE global dose,
with every loser logged with its reason. It owns ONLY the DECISION (rank + dedup +
already-acquired suppression + correct-time labeling); the SEAM owns rendering, the
leak firewall, sealing, and the byte splice (each winner is delivered by its own
plane's existing, leak-checked delivery path — never re-rendered here). So this module
touches NO free text (no payload, no provenance, no command output) and therefore
CANNOT leak a test identity — it operates on opaque dedup keys, normalized symbols, and
producer class names only.

THE LADDER (SM-5 scope, highest wins the single dose):

    executed failing world-fact        (the repo's own covering RED / executed verify)
  > current edit violation             (a break in the agent's just-written code)
  > active obligation violation        (an edited-but-unexercised requirement)
  > bilateral caller contract          (a signature change breaking cross-file callers)
  > one causal / companion chain        (a co-change / companion-registration surface)
  > localization-miss completion        (where-is-X orientation the agent hasn't found)
  > recovery                            (stuck / loop / failure nudges)
  > everything-else-stays-internal      (a ranking prior with no external dose)

Correct-time (the "each decision -> its boundary" rule): a fact delivered AFTER its
decision boundary is REPAIR SUPPORT, not preventive guidance. :func:`arbitrate` computes
``repair_support`` on the winner (``current_ordinal > boundary_ordinal``) so the caller
labels a late delivery honestly instead of pretending it steered a decision already made.

PURE · DETERMINISTIC · LLM-FREE · stdlib-only. No time, no randomness, no groundtruth
imports (the caller computes the unified ``dedup_key`` via the already-shipped
``evidence_envelope.derive_dedup_key`` and passes it in), so the engine is trivially
import-closed and unit-testable with no graph / no harness.

SS-1 (GT_SS_ARBITER_V2, 2026-07-13) — the ONE sanctioned env read. :func:`arbitrate`
takes ``ss_v2`` and, when it is left ``None``, resolves the activation flag from
``os.environ`` (:func:`_ss_v2_on`). This is the ONLY I/O in the module and it exists so
the ALREADY-SHIPPED seam call site (``gt_mini_patch._global_pool_flush`` -> ``arbitrate``,
which SS-1 must not edit) activates the v2 behaviors end-to-end without a seam edit. The
RANKING core stays pure: every unit test passes ``ss_v2`` EXPLICITLY (never touches env),
and the whole v2 branch is a strict no-op when the flag is unset OR when candidates carry
the inert defaults — so ``arbitrate`` is byte-identical to the pre-SS-1 engine with
``GT_SS_ARBITER_V2`` unset.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from .feature_lineage import DeliveryLineage

__all__ = [
    "Candidate",
    "ArbitrationResult",
    "arbitrate",
    "rank_of",
    "class_of_kind",
    "RANK_LADDER",
    # plane constants
    "PLANE_LANE_A",
    "PLANE_STEER",
    "PLANE_GATEWAY",
    # loser-reason vocabulary
    "REASON_DEDUP",
    "REASON_ACQUIRED",
    "REASON_OUTRANKED",
    "REASON_INTERNAL",
    # SS-1 (GT_SS_ARBITER_V2) loser-reason vocabulary
    "REASON_SS_EMPTY_PAYLOAD",
    "REASON_REDUNDANT",
]

# --------------------------------------------------------------------------- #
# plane + loser-reason vocabularies
# --------------------------------------------------------------------------- #
PLANE_LANE_A = "lane_a"
PLANE_STEER = "steer"
PLANE_GATEWAY = "gateway"

REASON_DEDUP = "dedup"  # dedup_key already in the delivered chain
REASON_ACQUIRED = "already_acquired"  # the agent already acquired the fact's target
REASON_OUTRANKED = "outranked"  # a higher-ladder candidate won the single dose
REASON_INTERNAL = "internal"  # not on the ladder -> never an external dose
# SS-1 (GT_SS_ARBITER_V2) reasons — inert unless the flag is on AND the candidate carries
# the corresponding non-default signal (rendered_chars==0 / redundant_with_delivered).
REASON_SS_EMPTY_PAYLOAD = "ss_empty_payload"  # zero rendered bytes -> never a delivered row
REASON_REDUNDANT = "redundant_delivered"  # localization superseded by a delivered def_partition

# --------------------------------------------------------------------------- #
# THE LADDER — class -> priority (higher wins the single global dose).
# --------------------------------------------------------------------------- #
RANK_LADDER: dict[str, int] = {
    "executed_world_fact": 70,  # the repo's own covering RED / executed verification
    "edit_violation": 60,  # a break in the agent's just-written code
    "obligation_violation": 50,  # an edited-but-unexercised requirement
    "caller_contract": 40,  # a signature change breaking cross-file callers
    "causal_chain": 30,  # a co-change / companion-registration surface
    "localization": 20,  # where-is-X orientation
    "recovery": 10,  # stuck / loop / failure nudges
}

# Producer kind / evidence_type -> ladder class. An unmapped kind (and its ``base:suffix``
# base) -> "" == INTERNAL (a ranking prior with no external dose). This is the SINGLE
# source that projects BOTH the mini seam's tagged kinds (l3.contract, l5.no_test, …) AND
# the Gateway's evidence_types (caller_break, def_ref_partition, …) onto the ONE ladder,
# so a Lane-A block and a Gateway fact compete on the same axis.
_KIND_TO_CLASS: dict[str, str] = {
    # executed failing world-fact — the repo's OWN executed RED, not advisory text.
    "covering_verdict": "executed_world_fact",
    "verify.horizon.executed": "executed_world_fact",
    "l5.build_fail": "executed_world_fact",
    # current edit violation — a break in the code the agent JUST WROTE (its own file).
    "edit.syntax": "edit_violation",
    # active obligation violation — an edited-but-unexercised requirement.
    "obligation.unexercised": "obligation_violation",
    "obligation.resurface": "obligation_violation",
    "spec.obligation": "obligation_violation",
    # bilateral caller contract — a signature change that breaks cross-file callers.
    # ``signature_mismatch`` (patch_delta W-C, Python arity detail) is the SAME caller-break
    # family as SM-2b ``caller_break`` (cross-language) — it manifests in the CALLERS, not the
    # edited file, so it ranks HERE (at caller_contract), never above it (SM-5 addendum: keep
    # patch_delta's caller axis at/below caller_contract so it never double-delivers a caller
    # break — the gateway's own <=1 arbiter already picks the more-specific one before this pool).
    "caller_break": "caller_contract",
    "caller_contract": "caller_contract",
    "signature_mismatch": "caller_contract",
    "l3.contract": "caller_contract",
    "l3b.evidence": "caller_contract",
    # one causal / companion chain — co-change / companion-registration surfaces.
    # ``companion_surface`` (patch_delta W-C, "registers siblings X but not Y") is the DISTINCT
    # axis patch_delta adds beyond caller_contract — a registration/companion gap, not a caller
    # break — so GT_PATCH_DELTA earns its Profile-2 slot for THIS class (SM-5 addendum).
    "companion_surface": "causal_chain",
    "cochange_partner": "causal_chain",
    "l3.cochange": "causal_chain",
    "detect.coherence": "causal_chain",
    "semantic_drift": "causal_chain",
    # localization-miss completion — where-is-X orientation.
    "def_ref_partition": "localization",
    "name_fold": "localization",
    "wrong_surface": "localization",
    "body_concept": "localization",
    "trace_frame": "localization",
    "new_file_destination": "localization",
    "missing_role": "localization",
    "missing_role_postcreate": "localization",
    # Ranked localization emits the literal fact class. Class names that can enter
    # the pool must resolve like their producer aliases (the same defensive rule
    # used by ``recovery``), or the global arbiter rejects a valid dose as INTERNAL.
    "localization": "localization",
    "post_search.localize": "localization",
    "consensus.scope_map": "localization",
    "consensus.scope": "localization",
    "concern.consensus": "localization",
    # recovery — stuck / loop / failure nudges.
    "l5.stuck": "recovery",
    "l5.failure": "recovery",
    "l5.no_test": "recovery",
    "detect.loop": "recovery",
    "verify.horizon.advisory": "recovery",
    "verify.horizon.urgent": "recovery",
    "verify.horizon.pivot": "recovery",
    "verify.horizon.gate": "recovery",
    # W6 FIX 1a (2026-07-12) — DEFENSIVE SELF-MAP for the seam's OWN recovery candidate.
    # ``gt_mini_patch._recovery_candidate`` enters the global pool with the LITERAL kind
    # "recovery" (the Lane-B gate stamps ``_last_gate_winner_kind = "recovery"``, threaded
    # into ``_global_pool_add_steer``). BEFORE this self-map, "recovery" was NOT a key here
    # (only the PRODUCER kinds that map TO the "recovery" CLASS were: l5.stuck / detect.loop /
    # verify.horizon.*), so ``class_of_kind("recovery")`` -> "" -> ``rank_of`` None ->
    # :func:`arbitrate` dropped it ``REASON_INTERNAL``. Measured on run 29217805592: 57/57
    # pool-recovery suppressions ``global_arbiter:internal`` across 22/30 tasks, 0 recovery
    # deliveries anywhere — the ONE historically proven-consumed GT form (short · active ·
    # at-the-decision) was STRUCTURALLY unable to reach the ladder. The class value already
    # existed ("recovery" at rank 10); this only makes the class its own key so the class
    # NAME (not just a producer kind) resolves.
    "recovery": "recovery",
}

# Tier string -> a stable secondary rank (VERIFIED beats a HYPOTHESIS at the same
# ladder class). Unknown tier -> INFO's rank. Kept local so the engine imports nothing.
_TIER_RANK: dict[str, int] = {"VERIFIED": 3, "WARNING": 2, "INFO": 1, "HYPOTHESIS": 0}

# --------------------------------------------------------------------------- #
# SS-1 (GT_SS_ARBITER_V2) — the ONE activation flag + the PREVENTIVE ladder classes.
# --------------------------------------------------------------------------- #
_SS_V2_OFF_VALUES = frozenset({"", "0", "false", "no", "off"})


def _ss_v2_on() -> bool:
    """The GT_SS_ARBITER_V2 activation flag (the module's ONLY env read; default OFF ->
    byte-identical). Consulted by :func:`arbitrate` ONLY when its ``ss_v2`` arg is left
    ``None`` (the unedited seam call site); every unit test passes ``ss_v2`` explicitly and
    never reaches this."""
    return os.environ.get("GT_SS_ARBITER_V2", "").strip().lower() not in _SS_V2_OFF_VALUES


# PREVENTIVE ladder classes = guidance meant to arrive BEFORE the decision it steers
# (localization before a search branch; a caller/companion contract AT the edit). The same
# set the seam's SM-10 uses (``gt_mini_patch._GA_PREVENTIVE_CLASSES``). Under SS-V2 a
# preventive fact whose pre-submit-completeness decision point is still LIVE
# (``obligations_open``) is NOT labeled repair_support — it is exactly its decision point.
_PREVENTIVE_CLASSES: frozenset[str] = frozenset({"caller_contract", "causal_chain", "localization"})


def _is_preventive_class(kind: str) -> bool:
    """True iff ``kind`` projects onto a PREVENTIVE ladder class. Reactive/post-event classes
    (executed_world_fact / edit_violation / obligation_violation / recovery) -> False."""
    return class_of_kind(kind) in _PREVENTIVE_CLASSES


def _repair_support(c: "Candidate", v2: bool) -> bool:
    """The correct-time verdict on the single-dose winner: True iff delivered AFTER its
    decision boundary (repair support, not preventive guidance).

    SS-V2 RELAXATION (v2 True): a PREVENTIVE fact (scope / companion / caller contract)
    delivered at a review/test boundary is NOT repair support while its decision point —
    pre-submit COMPLETENESS — is still LIVE (``obligations_open``: unresolved obligations or
    unedited GT-scope files remain). That is exactly when it should steer, so the seam must
    NOT late-suppress it; suppress only after a clean submit-gate pass (``obligations_open``
    back to False). The relaxation only ever makes the verdict MORE lenient (True->False),
    so a reactive class (already protected by the seam's own is_reactive exemption) is never
    regressed. Byte-identical when v2 is off OR ``obligations_open`` is the default False:
    the return is ``current_ordinal > boundary_ordinal`` exactly as the pre-SS-1 engine."""
    late = c.current_ordinal > c.boundary_ordinal
    if not late:
        return False
    if v2 and c.obligations_open and _is_preventive_class(c.kind):
        return False
    return True


def class_of_kind(kind: str) -> str:
    """The ladder CLASS of a producer ``kind`` / ``evidence_type`` (``""`` = internal).
    A ``base:suffix`` family (e.g. ``missing_role:registry``) falls back to its base."""
    k = (kind or "").strip()
    if k in _KIND_TO_CLASS:
        return _KIND_TO_CLASS[k]
    base = k.split(":", 1)[0]
    return _KIND_TO_CLASS.get(base, "")


def rank_of(kind: str) -> "int | None":
    """The ladder PRIORITY of ``kind`` (higher wins), or ``None`` when the kind is not
    on the ladder — an INTERNAL ranking prior that is never an external dose."""
    cls = class_of_kind(kind)
    return RANK_LADDER.get(cls)


def _norm_symbol(s: str) -> str:
    """The comparison form of an acquired-suppression symbol (a file rel or a probe
    stem): backslash-normalized, stripped, lowercased. Deterministic."""
    return (s or "").replace("\\", "/").strip().lower()


# --------------------------------------------------------------------------- #
# candidate + result
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Candidate:
    """One plane's would-be delivery, projected onto the ladder for the ONE competition.

    The engine reads ONLY these scalar fields — never the delivered bytes — so it can
    RANK and DEDUP without any leak surface. The caller carries the actual delivery
    (rendered bytes + seal) out-of-band and invokes it for the winner alone.
    """

    plane: str  # PLANE_LANE_A / PLANE_STEER / PLANE_GATEWAY
    kind: str  # producer kind / evidence_type (projected to a class)
    dedup_key: str = ""  # the UNIFIED envelope dedup key (one derivation, all planes)
    symbol: str = ""  # normalized target (file rel / symbol) for acquired-suppression
    tier: str = "INFO"  # VERIFIED / WARNING / INFO / HYPOTHESIS
    confidence: float = 0.0  # [0, 1]
    boundary_ordinal: int = 0  # the decision-point ordinal this fact belongs to
    current_ordinal: int = 0  # the firing-event ordinal (for correct-time labeling)
    suppressible_if_acquired: bool = False  # localization-class facts only
    seq: int = 0  # stable insertion order (deterministic tiebreak)
    # SS-1 (GT_SS_ARBITER_V2) signals — ALL inert defaults, so a candidate built without them
    # is byte-identical under the flag (every v2 branch is a no-op on these defaults). The
    # SEAM populates them when it has the signal (rendered bytes / open obligations / a
    # delivered def_ref_partition for this target); the engine only READS the scalars — still
    # no free-text surface, so leak=0 by construction is preserved.
    rendered_chars: int = (
        -1
    )  # bytes the plane WILL render: 0 => empty (reject); -1 => unknown (skip)
    obligations_open: bool = (
        False  # a preventive fact whose pre-submit-completeness point is still live
    )
    redundant_with_delivered: bool = (
        False  # localization superseded by an already-delivered def_ref_partition
    )
    # Producer-natural semantic identity. ``symbol`` above is deliberately the
    # acquisition target (normally a repo-relative path); keeping ``fact_id``
    # distinct prevents path-vs-symbol confusion when the seam proves that a
    # localization fact was superseded by an earlier def/ref answer.
    fact_id: str = field(default="", compare=False)
    # Audit-only typed identity. Arbitration never reads it.
    lineage: "DeliveryLineage | None" = field(default=None, compare=False)

    @property
    def tier_rank(self) -> int:
        return _TIER_RANK.get(self.tier, 1)


@dataclass
class ArbitrationResult:
    """The single global dose + the labeled losers.

    ``winner`` is the one Candidate the caller delivers (``None`` == silence). Every
    other eligible-or-rejected candidate is in ``losers`` as ``(Candidate, reason)`` so
    the caller can log WHY each plane was suppressed (the SM-5 loser-log generalization
    — today only Lane-B logs its losers). ``repair_support`` is the correct-time verdict
    on the winner: True iff it is delivered AFTER its own decision boundary."""

    winner: "Candidate | None" = None
    repair_support: bool = False
    losers: list = field(default_factory=list)  # list[tuple[Candidate, str]]


# WS-1 (2026-07-23) class-rotation: demote a candidate whose ladder CLASS already delivered
# this attempt below every not-yet-delivered class, so the single dose rotates to a starved
# class (def_partition after localization) instead of the same argmax winner. Large enough to
# sink any delivered class beneath any fresh one across the whole ladder; the seam passes the
# accumulated delivered CLASSES only under GT_DOSE_ROTATE, default empty => no decay =>
# byte-identical to the pre-WS-1 ranking.
_CLASS_ROTATION_DECAY = 1000

# WS-1a COALITION EXEMPTION (RepoShapley): localization brief + def_ref_partition are ONE
# coalition inside the shared "localization" class (class_of_kind collapses both to
# "localization"). Class-keyed rotation would sink def_ref_partition together with the brief
# the moment the brief delivers — starving the coalition partner instead of unstarving it.
# def_ref_partition is exempt from its class's rotation sink so it RISES to the next dose after
# the brief; once it itself delivers, dedup / REASON_REDUNDANT reject its repeat, so no monopoly.
# Inert unless rotation is ON (dclasses non-empty) AND a def_ref_partition candidate competes.
_COALITION_EXEMPT_KINDS = frozenset({"def_ref_partition"})


def arbitrate(
    candidates: "list[Candidate]",
    *,
    acquired: "frozenset[str] | set[str]" = frozenset(),
    delivered: "frozenset[str] | set[str]" = frozenset(),
    delivered_classes: "frozenset[str] | set[str]" = frozenset(),
    ss_v2: "bool | None" = None,
) -> ArbitrationResult:
    """The ONE ranked competition (SM-5). Returns AT MOST ONE winner + every loser's reason.

    Filtering, in order (each rejection is a logged loser, never a silent drop):
      1. INTERNAL — a kind not on :data:`RANK_LADDER` is a ranking prior with no external
         form (reason :data:`REASON_INTERNAL`); it never competes.
      2. SS-V2 EMPTY-PAYLOAD — a candidate that will render ZERO bytes
         (``rendered_chars == 0``) is rejected (:data:`REASON_SS_EMPTY_PAYLOAD`) so it can
         never win the dose / produce a delivered ledger row (the hydra-3005 ``chars=0 /
         empty seal`` delivered row). ``rendered_chars < 0`` (== unknown, the default) is
         SKIPPED, so a candidate built without the signal is unaffected. v2-gated.
      3. DEDUP — a candidate whose ``dedup_key`` is already in ``delivered`` (the ONE
         unified delivered-dedup chain across all planes) is a repeat
         (:data:`REASON_DEDUP`).
      4. ALREADY-ACQUIRED — a ``suppressible_if_acquired`` (localization-class) candidate
         whose normalized ``symbol`` the agent already acquired (``acquired`` = the
         normalized edited/viewed targets) is redundant (:data:`REASON_ACQUIRED`). The
         generalization of the ~3 narrow producer-local acquisition gates; conservative
         (empty ``symbol`` is never suppressed, so a fresh search answer survives).
      5. SS-V2 REDUNDANT-RETIRE — a localization-class candidate flagged
         ``redundant_with_delivered`` (a def_ref_partition for the SAME target already
         delivered this episode) is RETIRED (:data:`REASON_REDUNDANT`). This is the ONLY
         SS-V2 retire: a merely-LATE localization fact is NOT retired here — it stays
         eligible and DEFERS via the seam's loser re-arm (driven by ``repair_support``),
         re-competing at the next search/review boundary (defer-and-refire, not destroy).
         v2-gated + inert on the default ``redundant_with_delivered == False``.

    Then the single dose: the highest ladder class wins, breaking ties by tier, then
    confidence (8-dp), then stable insertion ``seq``, then ``dedup_key`` — a TOTAL
    deterministic order (no set iteration, no time, no randomness). Every non-winning
    eligible candidate is a :data:`REASON_OUTRANKED` loser.

    ``repair_support`` on the winner = ``current_ordinal > boundary_ordinal`` (delivered
    after its decision boundary — REPAIR SUPPORT), with the SS-V2 relaxation in
    :func:`_repair_support` (a preventive fact with a still-live pre-submit-completeness
    decision point is NOT repair support).

    ``ss_v2``: ``None`` (the default, used by the unedited seam call) resolves the flag from
    ``GT_SS_ARBITER_V2`` via :func:`_ss_v2_on`; an EXPLICIT ``True``/``False`` (every unit
    test) wins and keeps the call pure. Every v2 branch is a strict no-op when the flag is
    off OR when candidates carry the inert defaults, so the result is byte-identical to the
    pre-SS-1 engine with ``GT_SS_ARBITER_V2`` unset."""
    v2 = _ss_v2_on() if ss_v2 is None else bool(ss_v2)
    acq = {_norm_symbol(s) for s in (acquired or ())}
    delivered_set = set(delivered or ())
    dclasses = set(delivered_classes or ())  # WS-1 class-rotation (empty => byte-identical)
    losers: list = []
    eligible: list[tuple[int, Candidate]] = []
    for c in candidates:
        r = rank_of(c.kind)
        if r is None:
            losers.append((c, REASON_INTERNAL))
            continue
        if v2 and c.rendered_chars == 0:
            losers.append((c, REASON_SS_EMPTY_PAYLOAD))
            continue
        if c.dedup_key and c.dedup_key in delivered_set:
            losers.append((c, REASON_DEDUP))
            continue
        if c.suppressible_if_acquired and c.symbol and _norm_symbol(c.symbol) in acq:
            losers.append((c, REASON_ACQUIRED))
            continue
        if v2 and c.redundant_with_delivered and class_of_kind(c.kind) == "localization":
            losers.append((c, REASON_REDUNDANT))
            continue
        # WS-1 class-rotation: a candidate whose CLASS already delivered this attempt is sunk
        # below every fresh class so the dose rotates (empty dclasses => r_eff == r => identical).
        # WS-1a: a coalition-exempt kind (def_ref_partition) is NOT sunk with its class — the
        # coalition partner must rise to the next dose after the localization brief delivers.
        _rotate = (
            dclasses and class_of_kind(c.kind) in dclasses and c.kind not in _COALITION_EXEMPT_KINDS
        )
        r_eff = r - _CLASS_ROTATION_DECAY if _rotate else r
        eligible.append((r_eff, c))
    if not eligible:
        return ArbitrationResult(winner=None, repair_support=False, losers=losers)
    eligible.sort(
        key=lambda rc: (
            -rc[0],  # ladder class, desc
            -rc[1].tier_rank,  # tier, desc
            -round(rc[1].confidence, 8),  # confidence, desc
            rc[1].seq,  # stable insertion order, asc
            rc[1].dedup_key,  # total order tiebreak
        )
    )
    winner = eligible[0][1]
    for _r, c in eligible[1:]:
        losers.append((c, REASON_OUTRANKED))
    repair = _repair_support(winner, v2)
    return ArbitrationResult(winner=winner, repair_support=repair, losers=losers)
