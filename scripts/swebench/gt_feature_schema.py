#!/usr/bin/env python3
"""gt_feature_schema — the FeatureMetricEvent schema + controlled vocabularies.

This is the pure, LLM-free, stdlib-only schema spine for per-feature GT metrics
(``gt.feature_metrics.v1``). It declares:

* :func:`mv` — the MetricValue wrapper. EVERY metric value in the feature-metrics
  output is a ``{value, status, source_artifact, source_messages, grader_version}``
  dict so a reader can never confuse an UNMEASURED hole for a measured ``0``.
* :data:`LIFECYCLE_FIELDS` — the universal per-feature lifecycle (eligible ...
  baseline_or_holdout_status). Every field is populated with what the artifacts can
  prove; everything else is an explicit UNMEASURED MetricValue with a reason.
* :class:`FeatureMetricEvent` — the ATOMIC unit:
  ``task × attempt × unresolved_decision × feature × fact_class × delivery_instance
  × dedup_key``, retaining message indices + artifact identities.

WHY A WRAPPER, NOT A BARE FLOAT (gt-math §0 / MANDATORY_METRICS): a metric that is
missing must be ``status=UNMEASURED`` with a reason, and a metric with no opportunity
must be ``status=NOT_ELIGIBLE`` — NEVER a fabricated zero. A feature receives NO credit
for existing, firing, rendering, or appearing in a resolved task; the schema forces the
grader to state, for every number, WHERE it came from and WHETHER it is real.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

# The single version stamp carried by every MetricValue this grader emits. Bump on any
# change to a grading predicate so a stale reading can never be silently compared to a
# fresh one.
GRADER_VERSION = "gt.feature_metrics.v1"

# ── metric-value status vocabulary ────────────────────────────────────────────
STATUS_MEASURED = "MEASURED"          # a real value computed from an artifact
STATUS_UNMEASURED = "UNMEASURED"      # evidence to compute it is absent (never 0)
STATUS_NOT_ELIGIBLE = "NOT_ELIGIBLE"  # the run never created the required condition
_STATUSES = frozenset({STATUS_MEASURED, STATUS_UNMEASURED, STATUS_NOT_ELIGIBLE})

# ── per-feature verdict vocabulary (gt-math fact-class admission) ──────────────
VERDICT_ADMIT = "ADMIT"   # clean + positive consumption/efficacy evidence
VERDICT_HOLD = "HOLD"     # clean but insufficient exposures / inconclusive
VERDICT_CUT = "CUT"       # repeatedly inert / harmful / late / stale / dose-negative
VERDICT_FIX = "FIX"       # correctness/architecture failure prevents judgement
VERDICT_DARK = "DARK"     # enabled but produced no model-visible path when eligible
VERDICTS = frozenset({VERDICT_ADMIT, VERDICT_HOLD, VERDICT_CUT, VERDICT_FIX, VERDICT_DARK})

# ── feature role vocabulary ───────────────────────────────────────────────────
# INFRA (enabling): envelopes, registry enforcement, edit bridges, freshness, native
#   rendering, arbitration, cross-session policy → correctness + mediation metrics only,
#   NEVER a direct help credit.
# DIRECT (value): localization, content/body, contracts, patch delta, scope, obligations,
#   edit checks, covering execution, verification plans, recovery, completion refusal →
#   require a matched baseline/holdout behavioural delta for any positive efficacy claim.
ROLE_INFRA = "infrastructure"
ROLE_DIRECT = "direct"
ROLES = frozenset({ROLE_INFRA, ROLE_DIRECT})

# ── baseline comparability vocabulary (gt-math Ledger 1 R12) ───────────────────
BASELINE_MATCHED = "MATCHED_TRAJECTORY"  # a matched GT-off trajectory exists → deltas OK
BASELINE_VERDICT_ONLY = "VERDICT_ONLY"   # only an outcome verdict → no behavioural delta
BASELINE_HOLDOUT = "SHADOW_HOLDOUT"      # randomized shadow-holdout arm
BASELINE_UNAVAILABLE = "UNAVAILABLE"     # nothing to compare against

# ── the universal per-feature lifecycle (handoff §"Universal ... lifecycle") ───
# ORDER is the report/serialisation order — do not reorder without bumping the schema.
LIFECYCLE_FIELDS: tuple[str, ...] = (
    # opportunity
    "eligible", "not_eligible", "correct_abstain",
    # production / truth
    "produced", "truth_valid", "authority_valid", "validation_failed",
    # delivery / timing
    "delivered", "deferred", "expired_late", "stale",
    # arbitration
    "entered_arbiter", "arbiter_won", "arbiter_lost",
    # native form / bytes
    "native_valid", "render_observation_hash_match", "dose_tokens",
    # consumption
    "receipt_level", "reacquired",
    # state
    "state_changed", "state_durable",
    # dispositions
    "redundant", "inert", "accelerated", "enabled_progress", "harmful",
    # efficiency (direct-value only; UNMEASURED for infra)
    "steps_saved", "tokens_saved", "cost_delta", "baseline_or_holdout_status",
)


def mv(
    value: Any,
    status: str,
    *,
    source_artifact: str = "",
    source_messages: Sequence[int] | None = None,
    reason: str = "",
) -> dict[str, Any]:
    """Build one MetricValue. ``status`` MUST be a member of :data:`_STATUSES`.

    A MEASURED value must not be ``None`` (that is a contradiction — measured-but-empty is
    a bug, not a reading); an UNMEASURED/NOT_ELIGIBLE value carries ``value=None`` and a
    ``reason``. This fails LOUD on a mis-constructed metric rather than silently shipping a
    fabricated number (gt-math rule 12: missing evidence is UNMEASURED, never a pass/zero).
    """
    if status not in _STATUSES:
        raise ValueError(f"mv: unknown status {status!r} (allowed {sorted(_STATUSES)})")
    if status == STATUS_MEASURED and value is None:
        raise ValueError("mv: MEASURED value must not be None (use UNMEASURED with a reason)")
    if status != STATUS_MEASURED and value is not None:
        raise ValueError(f"mv: {status} value must be None (got {value!r})")
    out: dict[str, Any] = {
        "value": value,
        "status": status,
        "source_artifact": source_artifact,
        "source_messages": list(source_messages) if source_messages else [],
        "grader_version": GRADER_VERSION,
    }
    if reason:
        out["reason"] = reason
    return out


def measured(value: Any, *, source_artifact: str = "", source_messages: Sequence[int] | None = None):
    """MEASURED MetricValue shorthand."""
    return mv(value, STATUS_MEASURED, source_artifact=source_artifact, source_messages=source_messages)


def unmeasured(reason: str, *, source_artifact: str = ""):
    """UNMEASURED MetricValue shorthand (missing evidence — NEVER a zero)."""
    return mv(None, STATUS_UNMEASURED, source_artifact=source_artifact, reason=reason)


def not_eligible(reason: str, *, source_artifact: str = ""):
    """NOT_ELIGIBLE MetricValue shorthand (the run never created the condition)."""
    return mv(None, STATUS_NOT_ELIGIBLE, source_artifact=source_artifact, reason=reason)


def new_lifecycle(reason: str = "not_computed") -> dict[str, dict[str, Any]]:
    """A fresh lifecycle dict — every field an UNMEASURED MetricValue with ``reason``.

    The collector overwrites only the fields the artifacts can PROVE; any field left at
    this default is an honest UNMEASURED, never a fabricated measured value. This is the
    anti-``default-to-zero`` guarantee (defect #6)."""
    return {f: unmeasured(reason) for f in LIFECYCLE_FIELDS}


@dataclass(frozen=True)
class FeatureMetricEvent:
    """The ATOMIC feature-metric unit.

    Identity =
    ``task × attempt × unresolved_decision × feature × fact_class × delivery_instance
    × dedup_key``. It retains the message indices + artifact identities that produced it so
    the aggregate can reconcile 1:1 against the atomic records (E0.4/E0.5 referential
    integrity + reconciliation).
    """

    task: str
    attempt: int
    feature: str                 # a Profile-2 member (env flag) — the FEATURE identity
    fact_class: str              # a fact_registry class the feature produces/mediates
    delivery_instance: int       # ordinal of this production within (feature, fact_class)
    dedup_key: str               # stable content/identity key (seal or layer+file+chars)
    unresolved_decision: str = ""      # the open agent decision this instance targeted
    ledger_index: int | None = None    # row index in the runtime ledger (host record)
    observation_message: int | None = None   # model-visible observation msg index (receipt)
    source_artifact: str = ""          # the artifact this event was derived from
    role: str = ROLE_DIRECT
    lifecycle: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {
            "task": self.task,
            "attempt": self.attempt,
            "feature": self.feature,
            "fact_class": self.fact_class,
            "delivery_instance": self.delivery_instance,
            "dedup_key": self.dedup_key,
            "unresolved_decision": self.unresolved_decision,
            "ledger_index": self.ledger_index,
            "observation_message": self.observation_message,
            "source_artifact": self.source_artifact,
            "role": self.role,
            "lifecycle": self.lifecycle,
        }


def feature_summary_from_lifecycle(lifecycle: Mapping[str, Any], verdict: str) -> dict[str, Any]:
    """Project a full lifecycle down to the compact per-feature record the
    ``gt.feature_metrics.v1`` schema exposes:
    ``{eligible, correct, delivered, consumed, state_changed, baseline_or_holdout,
    behavior_delta, cost, harm, verdict}``. Each cell is the underlying MetricValue so the
    compact view never loses the status/source provenance."""
    if verdict not in VERDICTS:
        raise ValueError(f"feature_summary: bad verdict {verdict!r}")
    lc = lifecycle
    return {
        "eligible": lc.get("eligible"),
        "correct": lc.get("truth_valid"),
        "delivered": lc.get("delivered"),
        "consumed": lc.get("receipt_level"),
        "state_changed": lc.get("state_changed"),
        "baseline_or_holdout": lc.get("baseline_or_holdout_status"),
        "behavior_delta": lc.get("steps_saved"),
        "cost": lc.get("dose_tokens"),
        "harm": lc.get("harmful"),
        "verdict": verdict,
    }
