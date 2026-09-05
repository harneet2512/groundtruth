"""shadow_holdout — the SHADOW-HOLDOUT causal instrument (gt-math skill E10).

THE PROBLEM E10 SOLVES. A benchmark run proves at most "GT delivered a correct fact and
the agent later did X". It CANNOT, on its own, kill the K8 "would-have-done-it-anyway"
objection: the agent might have taken X with or without the GT fact. The only affordable
counterfactual — short of re-running every task with GT off — is a SINGLE-SHOT RANDOMIZED
SHADOW-HOLDOUT: per eligible delivery, a deterministic seed decides DELIVER vs HOLDOUT, so
ONE benchmark run yields a delivered-vs-withheld contrast PER FACT CLASS. Comparing the two
arms (subsequent action targeting the fact, independent reacquisition, steps-to-relevant-
edit, resolved-state) estimates the average intervention effect of each fact class without
re-running the tasks.

THIS MODULE is the pure decision kernel: :func:`assign` maps
``(task_id, fact_class, dedup_key, rate)`` to ``DELIVER`` / ``HOLDOUT`` via a sha256 bucket.
It is consulted at the seam's single delivery chokepoint(s) AFTER a fact has already won
arbitration and passed every SS screen (leak / provenance / novelty / dedup / dose) — i.e.
it decides ONLY among facts that WOULD otherwise deliver. On ``HOLDOUT`` the seam ships zero
model bytes and writes a shadow-ledger row (the withheld render's HASH, never the payload).

FIVE INVARIANTS (all enforced here + at import, all unit-tested):

  1. DETERMINISTIC & STATELESS. The verdict is a pure function of the four inputs — a
     sha256 over ``task_id | fact_class | dedup_key``, no RNG object, no time, no I/O. The
     SAME inputs ALWAYS yield the SAME verdict, so an offline analyzer re-derives every
     assignment from the recorded inputs alone (reproducible; no separate seed log).

  2. PRODUCTION-SAFE RATE FLOOR. ``rate == 0`` (the default of ``GT_SS_SHADOW_RATE``) ->
     ``DELIVER`` for EVERYTHING, so the flag can be ON in a production profile and withhold
     NOTHING (byte-identical delivery). Withholding is opt-in per eval, never a default.

  3. SAFETY ALLOWLIST (the cardinal E10 rule). Only ADVISORY / FACTUAL classes participate.
     CARDINAL INTEGRITY / SAFETY classes (``syntax_result`` / ``submit_refusal`` — an edit-
     acceptability check and the completion gate) are NEVER eligible for holdout at ANY rate:
     withholding a safety check could ship a broken edit or allow a premature submit and
     corrupt the episode / invalidate the run. The decision is an ALLOWLIST (only
     :data:`PARTICIPATING_CLASSES` can be held out); an unknown / safety / unresolvable class
     ALWAYS delivers. Leak / provenance screens are not a deliverable class at all — they run
     BEFORE this kernel is consulted, so they are never randomized by construction.

  4. INHERENT STRATIFICATION. ``fact_class`` is folded into the hash, so the holdout fraction
     applies per class (a class's withheld share is ~``rate`` independent of other classes).

  5. LEAK-SAFE BY CONSTRUCTION. This module never renders or returns model-facing bytes — it
     returns only the two-valued verdict. The withheld payload lives only in the seam's
     shadow-ledger row as a HASH (see the seam), never here.

PURE · DETERMINISTIC · STDLIB-ONLY (``hashlib``). No time, no randomness, no I/O, no
third-party or ``groundtruth`` imports — safe to consult on the hot delivery path.
"""
from __future__ import annotations

import hashlib

__all__ = [
    "DELIVER",
    "HOLDOUT",
    "PARTICIPATING_CLASSES",
    "SAFETY_EXCLUDED_CLASSES",
    "assign",
    "canonical_class",
    "is_participating",
    "parse_rate",
]

# --------------------------------------------------------------------------- #
# the two-valued verdict
# --------------------------------------------------------------------------- #
DELIVER = "DELIVER"   # ship the fact's bytes to the model (the control-within-run arm)
HOLDOUT = "HOLDOUT"   # withhold: zero model bytes, shadow-ledger row only (the treated arm)

# --------------------------------------------------------------------------- #
# the participate / exclude sets — the E10 safety allowlist.
#
# Keys are the canonical fact_registry fact classes (fact_registry.REGISTRY). The
# participate set is derived from the ladder: every ADVISORY / FACTUAL class whose value is
# information the agent could otherwise self-acquire, so withholding it is a clean causal
# probe (never a repository-integrity risk). The safety set is the two CARDINAL classes whose
# absence could corrupt an episode — E10 forbids randomizing them; they are graded
# deterministically under E1/E2/E7 instead.
#
#   CLASS              PART?  ONE-LINE RATIONALE
#   obligations         YES   advisory step-0 plan — withholding probes plan-coverage effect.
#   localization        YES   advisory "which file to open" — probes steps-to-first-edit.
#   def_partition       YES   advisory "which def to inspect" — probes wrong-branch reads.
#   caller_contract     YES   advisory "how to modify a fn" — probes contract-break rate.
#   signature_delta     YES   advisory "must callers change" — probes caller-breakage rate.
#   covering_red        YES   advisory executed-RED "next repair" (informs, does NOT gate) —
#                             probes repair-targeting. NOT the submit gate; safe to withhold.
#   cochange_prior      YES   advisory companion-file hint — probes companion-hit rate.
#   newfile_precedent   YES   advisory new-file destination — probes integration correctness.
#   recovery            YES   advisory pivot/steer nudge (the ONE proven-consumed form) — the
#                             central causal question: does the steer change the pivot?
#   syntax_result       NO    CARDINAL: "is the edit acceptable" — withholding could let a
#                             structurally broken edit persist and corrupt the episode.
#   submit_refusal      NO    CARDINAL: "is completion allowed" — withholding could allow a
#                             premature / broken submit and invalidate the run.
# --------------------------------------------------------------------------- #
PARTICIPATING_CLASSES: frozenset[str] = frozenset(
    {
        "obligations",
        "localization",
        "def_partition",
        "caller_contract",
        "signature_delta",
        "covering_red",
        "cochange_prior",
        "newfile_precedent",
        "recovery",
    }
)

SAFETY_EXCLUDED_CLASSES: frozenset[str] = frozenset(
    {
        "syntax_result",   # edit.syntax — the edit-acceptability integrity check
        "submit_refusal",  # the completion gate
    }
)

# --------------------------------------------------------------------------- #
# class resolution — raw identifier -> canonical fact_registry class.
#
# The seam consults this kernel with whatever identity it has at each chokepoint: a canonical
# fact_registry class, a finer gateway ``evidence_type``, a seam-tagged ``kind`` (``l3.contract``),
# or a ladder class. This table (a superset mirror of ``fact_registry._EVIDENCE_TYPE_ALIASES``
# and ``global_arbiter._KIND_TO_CLASS``, kept local so the kernel imports nothing) resolves them
# ALL to a canonical fact_registry class. SAFETY is guaranteed by the ALLOWLIST regardless of
# this table's completeness: an identifier that resolves to a non-participating class (or to
# ``None``) always DELIVERS, so an unmapped participating alias merely never gets held out (safe,
# just unexercised) and a safety kind can never leak into the participate set.
# Every VALUE is a canonical class in ``PARTICIPATING_CLASSES | SAFETY_EXCLUDED_CLASSES`` (the
# import-time self-check enforces this).
# --------------------------------------------------------------------------- #
_CLASS_RESOLUTION: dict[str, str] = {
    # --- canonical fact_registry classes (identity) ---
    "obligations": "obligations",
    "localization": "localization",
    "brief_localization": "localization",
    "def_partition": "def_partition",
    "caller_contract": "caller_contract",
    "signature_delta": "signature_delta",
    "covering_red": "covering_red",
    "cochange_prior": "cochange_prior",
    "newfile_precedent": "newfile_precedent",
    "recovery": "recovery",
    "syntax_result": "syntax_result",     # SAFETY
    "submit_refusal": "submit_refusal",    # SAFETY
    # --- fact_registry finer evidence_type aliases ---
    "def_ref_partition": "def_partition",
    "name_fold": "def_partition",
    "wrong_surface": "def_partition",
    "body_concept": "def_partition",
    "trace_frame": "localization",
    "signature_mismatch": "signature_delta",
    "companion_surface": "signature_delta",
    "caller_break": "caller_contract",
    "cochange_partner": "cochange_prior",
    "new_file_destination": "newfile_precedent",
    "missing_role": "newfile_precedent",
    "covering_verdict": "covering_red",
    # --- seam tagged kinds (gt_mini_patch Lane A / Lane B producers) ---
    "l3.contract": "caller_contract",
    "l3b.evidence": "caller_contract",
    "l3.cochange": "cochange_prior",
    # detect.coherence is the ss_coherence_v2 collapse steer. The global arbiter projects it
    # onto the ``causal_chain`` LADDER class (a RANKING axis) alongside the co-change surfaces,
    # but its FACT identity is NOT cochange — the seam's typed lineage stamps it
    # ``coherence_collapse`` (gt_mini_patch._LANE_REGISTERED_PRODUCERS), and the registry aliases
    # ``coherence_collapse`` -> ``recovery`` (fact_registry._EVIDENCE_TYPE_ALIASES). The shadow
    # cohort join keys on the FACT class the OUTCOME/lineage side records (recovery), so a holdout
    # draw MUST record ``recovery`` too — recording ``cochange_prior`` here (the ladder grouping)
    # would file the draw under a cohort the outcome never joins. Mirror the registry, not the ladder.
    "detect.coherence": "recovery",
    "semantic_drift": "cochange_prior",
    "post_search.localize": "localization",
    "consensus.scope": "localization",
    "consensus.scope_map": "localization",
    "concern.consensus": "localization",
    "obligation.resurface": "obligations",
    "obligation.unexercised": "obligations",
    "spec.obligation": "obligations",
    "l5.stuck": "recovery",
    "l5.failure": "recovery",
    "l5.no_test": "recovery",
    "detect.loop": "recovery",
    "verify.horizon.advisory": "recovery",
    "verify.horizon.urgent": "recovery",
    "verify.horizon.pivot": "recovery",
    "verify.horizon.gate": "recovery",
    # executed world-fact producers -> the advisory covering_red class (participating)
    "verify.horizon.executed": "covering_red",
    "l5.build_fail": "covering_red",
    # --- SAFETY seam kinds (edit integrity + the submit/completion gate) -> excluded ---
    "edit.syntax": "syntax_result",         # SAFETY
    "edit_check": "syntax_result",           # SAFETY
    "submit_gate": "submit_refusal",         # SAFETY
    "completion_cert": "submit_refusal",     # SAFETY
    "submit.refusal": "submit_refusal",      # SAFETY
}

# the fixed hash space — the first 8 bytes of the sha digest as an unsigned 64-bit bucket.
_HASH_SPACE = 1 << 64
# the byte that joins the hash inputs (a unit separator that cannot appear in a class/kind
# token, so the concatenation is injective — no two distinct input tuples collide by shifting).
_SEP = "\x1f"


def canonical_class(raw: str) -> "str | None":
    """Resolve ``raw`` (a canonical class, a finer evidence_type, a seam ``kind``, a ladder
    class, a ``base:suffix`` dynamic form, or a plane-prefixed ``ga.<kind>`` /
    ``gateway.<kind>`` label) to its canonical fact_registry class, or ``None`` when it
    resolves to no known class.

    Resolution order: strip a leading plane prefix (``ga.`` arbiter telemetry, ``gateway.``
    gateway-plane rows — arm-4 carried gateway.trace_frame/gateway.def_ref_partition as its 8
    'unknown' deliveries) -> exact match -> the part before the first ``:`` (dynamic
    ``missing_role:handler`` forms). Prefix-stripping is allowlist-safe: a stripped kind that
    resolves to a safety class still never participates. Total: never raises."""
    k = (raw or "").strip()
    if not k:
        return None
    for _pref in ("ga.", "gateway."):  # plane prefixes carried by ledger row labels
        if k.startswith(_pref):
            k = k[len(_pref):]
            break
    hit = _CLASS_RESOLUTION.get(k)
    if hit is not None:
        return hit
    base = k.split(":", 1)[0]
    return _CLASS_RESOLUTION.get(base)


def is_participating(raw: str) -> bool:
    """True iff ``raw`` resolves to an ADVISORY class eligible for shadow holdout. A safety /
    unknown / unresolvable class -> False (it can never be held out)."""
    canon = canonical_class(raw)
    return canon is not None and canon in PARTICIPATING_CLASSES


def parse_rate(rate: object) -> float:
    """The holdout fraction as a float clamped to ``[0.0, 1.0]``. Accepts a float, an int, or
    a string (``GT_SS_SHADOW_RATE`` like ``"0.2"``). Any malformed / non-numeric / NaN /
    ``None`` value FAILS SAFE to ``0.0`` (never withhold) — the production-safe posture."""
    if rate is None:
        return 0.0
    try:
        # float() strips surrounding whitespace and accepts int/float/str; a non-convertible
        # object lands in the except arm (the fail-safe) — the ignore is truthful.
        r = float(rate)  # type: ignore[arg-type]  # pyright: ignore[reportArgumentType]
    except (TypeError, ValueError):
        return 0.0
    if r != r:  # NaN
        return 0.0
    if r < 0.0:
        return 0.0
    if r > 1.0:
        return 1.0
    return r


def _bucket(
    task_id: str, canon: str, dedup_key: str, predecision_id: str = ""
) -> int:
    """The deterministic sha256 bucket in ``[0, 2**64)`` for one delivery instance. Folds the
    canonical class in (invariant 4: stratification) and the ``dedup_key`` (so distinct
    delivery instances of the same class get independent draws). Injective framing via the
    unit separator; ``surrogatepass`` so a surrogate-bearing key can never raise.

    B1 (Cluster-4): ``predecision_id`` OPTIONALLY folds the PRE-DECISION STATE identity into
    the draw so two instances of the same class at DIFFERENT decision states draw
    independently (the offline fair-probe pairing keys on this same identity). It is APPENDED
    only when non-empty, so a bare ``(task_id, canon, dedup_key)`` call (the seam hot path,
    which has no trajectory prefix at delivery time) is BYTE-IDENTICAL to the pre-Cluster-4
    join — invariant 1 (deterministic/stateless) preserved, invariant 2 unaffected."""
    parts = [task_id or "", canon or "", dedup_key or ""]
    if predecision_id:
        parts.append(predecision_id)
    raw = _SEP.join(parts)
    digest = hashlib.sha256(raw.encode("utf-8", "surrogatepass")).digest()
    return int.from_bytes(digest[:8], "big")


def assign(
    task_id: str,
    fact_class: str,
    dedup_key: str,
    rate: object,
    predecision_id: str = "",
) -> str:
    """The shadow-holdout verdict for one eligible delivery: ``DELIVER`` or ``HOLDOUT``.

    Consulted at the seam ONLY for a fact that has already won arbitration and passed every SS
    screen (it WOULD deliver). The verdict is a pure, deterministic function of the inputs:

      * ``rate <= 0``            -> ``DELIVER`` (production-safe floor; invariant 2).
      * class NOT participating  -> ``DELIVER`` (safety allowlist; invariant 3 — a safety /
                                    unknown / unresolvable class is NEVER held out).
      * ``rate >= 1``            -> ``HOLDOUT`` (participating classes only).
      * else                     -> ``HOLDOUT`` iff ``_bucket(...) < rate * 2**64`` — a ~``rate``
                                    per-class share, deterministic and reproducible.

    ``fact_class`` may be a canonical class, a finer evidence_type, a seam ``kind``, or a
    ladder class — it is resolved via :func:`canonical_class`. ``predecision_id`` (B1,
    Cluster-4) is an OPTIONAL pre-decision-state identity folded into the draw; the seam hot
    path omits it (byte-identical to the pre-Cluster-4 draw). Never raises."""
    r = parse_rate(rate)
    if r <= 0.0:
        return DELIVER
    canon = canonical_class(fact_class)
    if canon is None or canon not in PARTICIPATING_CLASSES:
        return DELIVER  # allowlist: only advisory participating classes are ever withheld
    if r >= 1.0:
        return HOLDOUT
    # integer threshold from the (deterministic, IEEE-754) product — no float in the compare.
    threshold = int(r * _HASH_SPACE)
    return (
        HOLDOUT
        if _bucket(task_id, canon, dedup_key, predecision_id or "") < threshold
        else DELIVER
    )


# --------------------------------------------------------------------------- #
# import-time self-check — the safety allowlist is a load-bearing invariant; a table edit
# that broke it must fail LOUD at import, never silently randomize a safety class.
# --------------------------------------------------------------------------- #
def _self_check() -> None:
    overlap = PARTICIPATING_CLASSES & SAFETY_EXCLUDED_CLASSES
    if overlap:
        raise ValueError(
            f"shadow_holdout: participate/safety sets overlap {sorted(overlap)}"
        )
    known = PARTICIPATING_CLASSES | SAFETY_EXCLUDED_CLASSES
    for src, dst in _CLASS_RESOLUTION.items():
        if not isinstance(src, str) or not src.strip():
            raise ValueError(f"shadow_holdout: resolution key {src!r} empty/non-str")
        if dst not in known:
            raise ValueError(
                f"shadow_holdout: resolution {src!r} -> {dst!r} is not a known class"
            )
    # every safety class resolves to ITSELF and is non-participating (the E10 exclusion).
    for s in SAFETY_EXCLUDED_CLASSES:
        if _CLASS_RESOLUTION.get(s) != s or s in PARTICIPATING_CLASSES:
            raise ValueError(f"shadow_holdout: safety class {s!r} mis-registered")


_self_check()
