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

PURE · DETERMINISTIC · LLM-FREE · stdlib-only. No time, no randomness, no I/O, no
groundtruth imports (the caller computes the unified ``dedup_key`` via the already-
shipped ``evidence_envelope.derive_dedup_key`` and passes it in), so the engine is
trivially import-closed and unit-testable with no graph / no harness.
"""
from __future__ import annotations

from dataclasses import dataclass, field

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
]

# --------------------------------------------------------------------------- #
# plane + loser-reason vocabularies
# --------------------------------------------------------------------------- #
PLANE_LANE_A = "lane_a"
PLANE_STEER = "steer"
PLANE_GATEWAY = "gateway"

REASON_DEDUP = "dedup"              # dedup_key already in the delivered chain
REASON_ACQUIRED = "already_acquired"  # the agent already acquired the fact's target
REASON_OUTRANKED = "outranked"     # a higher-ladder candidate won the single dose
REASON_INTERNAL = "internal"       # not on the ladder -> never an external dose

# --------------------------------------------------------------------------- #
# THE LADDER — class -> priority (higher wins the single global dose).
# --------------------------------------------------------------------------- #
RANK_LADDER: dict[str, int] = {
    "executed_world_fact": 70,   # the repo's own covering RED / executed verification
    "edit_violation": 60,        # a break in the agent's just-written code
    "obligation_violation": 50,  # an edited-but-unexercised requirement
    "caller_contract": 40,       # a signature change breaking cross-file callers
    "causal_chain": 30,          # a co-change / companion-registration surface
    "localization": 20,          # where-is-X orientation
    "recovery": 10,              # stuck / loop / failure nudges
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

    plane: str                        # PLANE_LANE_A / PLANE_STEER / PLANE_GATEWAY
    kind: str                         # producer kind / evidence_type (projected to a class)
    dedup_key: str = ""               # the UNIFIED envelope dedup key (one derivation, all planes)
    symbol: str = ""                  # normalized target (file rel / symbol) for acquired-suppression
    tier: str = "INFO"                # VERIFIED / WARNING / INFO / HYPOTHESIS
    confidence: float = 0.0           # [0, 1]
    boundary_ordinal: int = 0         # the decision-point ordinal this fact belongs to
    current_ordinal: int = 0          # the firing-event ordinal (for correct-time labeling)
    suppressible_if_acquired: bool = False  # localization-class facts only
    seq: int = 0                      # stable insertion order (deterministic tiebreak)

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


def arbitrate(
    candidates: "list[Candidate]",
    *,
    acquired: "frozenset[str] | set[str]" = frozenset(),
    delivered: "frozenset[str] | set[str]" = frozenset(),
) -> ArbitrationResult:
    """The ONE ranked competition (SM-5). Returns AT MOST ONE winner + every loser's reason.

    Filtering, in order (each rejection is a logged loser, never a silent drop):
      1. INTERNAL — a kind not on :data:`RANK_LADDER` is a ranking prior with no external
         form (reason :data:`REASON_INTERNAL`); it never competes.
      2. DEDUP — a candidate whose ``dedup_key`` is already in ``delivered`` (the ONE
         unified delivered-dedup chain across all planes) is a repeat
         (:data:`REASON_DEDUP`).
      3. ALREADY-ACQUIRED — a ``suppressible_if_acquired`` (localization-class) candidate
         whose normalized ``symbol`` the agent already acquired (``acquired`` = the
         normalized edited/viewed targets) is redundant (:data:`REASON_ACQUIRED`). The
         generalization of the ~3 narrow producer-local acquisition gates; conservative
         (empty ``symbol`` is never suppressed, so a fresh search answer survives).

    Then the single dose: the highest ladder class wins, breaking ties by tier, then
    confidence (8-dp), then stable insertion ``seq``, then ``dedup_key`` — a TOTAL
    deterministic order (no set iteration, no time, no randomness). Every non-winning
    eligible candidate is a :data:`REASON_OUTRANKED` loser.

    ``repair_support`` on the winner = ``current_ordinal > boundary_ordinal`` (delivered
    after its decision boundary — REPAIR SUPPORT, not preventive guidance)."""
    acq = {_norm_symbol(s) for s in (acquired or ())}
    delivered_set = set(delivered or ())
    losers: list = []
    eligible: list[tuple[int, Candidate]] = []
    for c in candidates:
        r = rank_of(c.kind)
        if r is None:
            losers.append((c, REASON_INTERNAL))
            continue
        if c.dedup_key and c.dedup_key in delivered_set:
            losers.append((c, REASON_DEDUP))
            continue
        if c.suppressible_if_acquired and c.symbol and _norm_symbol(c.symbol) in acq:
            losers.append((c, REASON_ACQUIRED))
            continue
        eligible.append((r, c))
    if not eligible:
        return ArbitrationResult(winner=None, repair_support=False, losers=losers)
    eligible.sort(
        key=lambda rc: (
            -rc[0],                         # ladder class, desc
            -rc[1].tier_rank,               # tier, desc
            -round(rc[1].confidence, 8),    # confidence, desc
            rc[1].seq,                       # stable insertion order, asc
            rc[1].dedup_key,                 # total order tiebreak
        )
    )
    winner = eligible[0][1]
    for _r, c in eligible[1:]:
        losers.append((c, REASON_OUTRANKED))
    repair = winner.current_ordinal > winner.boundary_ordinal
    return ArbitrationResult(winner=winner, repair_support=repair, losers=losers)
