#!/usr/bin/env python3
"""gt_feature_metrics — per-feature behavioural-contract metrics (gt.feature_metrics.v1).

Upgrades GT metrics so EVERY enabled Profile-2 feature is measured independently against
its behavioural contract, answering — per feature — the handoff's eight questions:

  (1) eligible opportunity?  (2) correct/fresh/authoritative?  (3) delivered at the correct
  boundary?  (4) consumed without reacquisition?  (5) predicted state changed durably?
  (6) steps/tokens/searches/rewrites/failures saved vs a matched baseline/holdout?
  (7) harm?  (8) verdict ADMIT/HOLD/CUT/FIX.

A feature receives NO credit for existing, firing, rendering, or appearing in a resolved
task. Resolution is secondary. Missing evidence is UNMEASURED (never a fabricated zero).
Gold-assisted diagnostics are kept out of the agent-observation-only headlines.

DYNAMIC, NOT HARDCODED:
  * the enabled member set is enumerated from ``rl_profile.PROFILE_MEMBERS`` (never a copied
    table); a member added to the profile but not classified here FAILS LOUD at import.
  * the expected fact-class contracts (boundary, renderer, receipt predicate) come from
    ``fact_registry.REGISTRY`` / ``registration_for`` at run time.

OFFLINE TRANSITION DERIVATION (no seam edits — defect-2 native detection):
  the runtime ledger already records every producer's disposition
  (delivered / suppressed_hidden_only=arbiter-loser / suppressed_duplicate=dose /
  ``ga.*``=arbiter candidate), the consumption receipt ladder (W1's v2, seal-joined), and
  the per-turn oracle obligation coverage. This module joins those offline; it names the
  transitions that still need in-seam instrumentation (see ``in_seam_instrumentation_todo``).

PURE-ish: stdlib + the repo's own metric/registry modules. No LLM, no network.
"""
from __future__ import annotations

import glob
import hashlib
import json
import math
import os
import re
import shutil
import sys
from collections import Counter, defaultdict
from typing import Any, Iterable, Mapping

from artifact_deepswe.ledger_attestation import validate_attestation

# repo schema + registries (fail loud if the substrate lacks them — a collection failure,
# never a silent empty record).
from gt_feature_schema import (  # noqa: E402
    BASELINE_MATCHED,
    BASELINE_UNAVAILABLE,
    GRADER_VERSION,
    ROLE_DIRECT,
    ROLE_INFRA,
    VERDICT_ADMIT,
    VERDICT_CUT,
    VERDICT_DARK,
    VERDICT_FIX,
    VERDICT_HOLD,
    FeatureMetricEvent,
    feature_summary_from_lifecycle,
    measured,
    new_lifecycle,
    not_eligible,
    unmeasured,
)
from gt_feature_inventory import (  # noqa: E402
    canonical_feature_inventory,
    performance_metric_definitions,
)
from feature_opportunity import (  # noqa: E402
    attach_opportunity_evidence,
    collect_feature_opportunities,
)
from live_run_provenance import detect_live_run  # noqa: E402
from attestation_join import (  # noqa: E402
    ATTESTED_FACT_CLASSES,
    join_truth,
    load_attestations,
    truth_join_to_dict,
)
from chronology_extract import (  # noqa: E402  (SPEC-J3 timing join)
    adjudicate_deliveries,
    extract_block_chronologies,
    extract_chronologies,
    timing_by_fact_class,
    validate_block_lineage,
)
from receipt_predicates import (  # noqa: E402  (B-cluster Gate 4 acknowledgment evaluators)
    acknowledgment_for_row,
    acknowledgment_by_fact_class,
)
from receipt_sidecar import (  # noqa: E402  (sealed runtime receipt corroboration)
    _RECEIPT_EXEMPT_LAYERS,  # the DECLARED per-layer receipt-authority exemptions (CLASS-2 a/b)
    canonical_receipt_key,
    join_receipt_evidence,
    load_receipt_sidecar,
    sealed_receipt_expected,
)
from fair_probe_result import (  # noqa: E402  (SPEC-J4 fair-probe result)
    fair_probe_bool_by_fact_class,
    join_fair_probes,
)
from consumption_ledger import (  # noqa: E402  (DEFECT 7/8 per-fire byte authority + leak scanner)
    MODEL_VISIBLE_SOURCES,
    PHYSICAL_DELIVERY_BOUND,
    PROVIDER_PAYLOAD_JOIN_METHOD,
    physical_delivery_authority,
    scan_test_identity_leaks,
)

#: The join methods that PROVE bytes, one per physical record (#43).
_PHYSICAL_BYTE_JOIN_METHODS = frozenset({"seal", PROVIDER_PAYLOAD_JOIN_METHOD})
from live_evidence import _LEAK_RE  # noqa: E402  (DEFECT 7 per-fire leak class, reused verbatim)
from groundtruth.runtime.feature_lineage import (  # noqa: E402
    CAP_BYTE_OWNER_MECHANISMS,
    CAP_BYTE_OWNER_IDS,
    CAP_ELIGIBILITY_IDS,
    CAP_MEDIATOR_IDS,
    cap_role_for,
)
from groundtruth.runtime.control_participation import (  # noqa: E402
    CONTROL_PRECEDES_DELIVERY,
    CONTROL_PARTICIPATION_SCHEMA,
    RECEIPT_FOLLOWS_DELIVERY,
    ControlParticipation,
)
from groundtruth.runtime.evidence_envelope import (  # noqa: E402
    observation_binding_from_dict,
    observation_binding_to_dict,
    validate_observation_binding,
)
from groundtruth.runtime.runtime_attestation import (  # noqa: E402
    runtime_attestation_diagnostic,
)
from groundtruth.runtime.reasoning_runtime import (  # noqa: E402
    # The capsule-hash preimage label. This collector RECOMPUTES the capsule hash to verify
    # it, so the label must be the writer's -- imported, never a local literal. See
    # DECISION_CAPSULE_SCHEMA for the four-way hand-sync defect this replaces.
    DECISION_CAPSULE_SCHEMA as _DECISION_CAPSULE_SCHEMA,
)
from groundtruth.runtime.fact_registry import (  # noqa: E402
    FACT_ROLE_INTERNAL_SUPPORT,
    fact_role_for,
    is_registered,
    producer_matches,
    registration_for,
    required_renderer,
)

# ---------------------------------------------------------------------------
# Fail-loud loaders — a truly-absent dependency is a COLLECTION FAILURE, not
# ``performance={}`` / an empty record (defect #1 & #7).
# ---------------------------------------------------------------------------

def load_performance_module():
    """Import the performance-metrics collector, FAILING LOUD if the module is absent.

    The historical defect (gt_deep_metrics) wrapped ``from gt_performance_metrics import
    compute_performance_metrics`` in a bare ``except Exception: performance = {}`` so a run
    launched without ``scripts/swebench`` on ``PYTHONPATH`` silently produced NO performance
    section. This loader re-raises a legible ImportError instead — the caller must fix the
    path, not ship an empty metric."""
    try:
        import gt_performance_metrics as _pm  # noqa: F401
    except ImportError as exc:  # module truly absent → collection failure
        raise ImportError(
            "gt_feature_metrics: gt_performance_metrics is REQUIRED and is not importable "
            "(is scripts/swebench on PYTHONPATH?). A missing module is a COLLECTION FAILURE, "
            "never performance={} / an empty feature record."
        ) from exc
    return _pm


def _profile_registry():
    from groundtruth.runtime import rl_profile as _rp
    from groundtruth.runtime import fact_registry as _fr
    return _rp, _fr


# ---------------------------------------------------------------------------
# Member classification — role + the fact classes each member produces/mediates.
# The TABLE is documentation-grade domain knowledge; it is CROSS-CHECKED at import
# against the dynamic rl_profile members and fact_registry classes so any drift
# (a new profile member, a renamed fact class, a broken module→producer link)
# fails LOUD rather than dropping a feature silently.
# ---------------------------------------------------------------------------

# DIRECT-value producer members → the registry fact class they PRODUCE. Efficacy claims for
# these require a matched baseline/holdout behavioural delta.
def _owner_fact_class(feature_id: str) -> str | None:
    classes = {
        binding.fact_class
        for binding in CAP_BYTE_OWNER_MECHANISMS[feature_id].bindings
        if binding.fact_class is not None
    }
    if len(classes) > 1:
        raise ValueError(
            f"gt_feature_metrics: {feature_id} has ambiguous byte-owner FACT classes"
        )
    return next(iter(classes), None)


# Compatibility projection for lifecycle grouping. The sole mechanism authority is
# CAP_BYTE_OWNER_MECHANISMS; coherence intentionally has no fabricated FACT identity.
_DIRECT_MEMBER_FACTCLASS: dict[str, str | None] = {
    feature_id: _owner_fact_class(feature_id)
    for feature_id in CAP_BYTE_OWNER_MECHANISMS
}

# INFRASTRUCTURE members → the fact class(es) they MEDIATE (render / freshen / arbitrate /
# shape / cross-session-rank). They get correctness + mediation metrics ONLY — never a
# direct help credit. An empty tuple = a KERNEL mediator that touches ALL classes.
_INFRA_MEMBER_MEDIATES: dict[str, tuple[str, ...]] = {
    "GT_GATEWAY": (),                       # the delivery kernel
    "GT_GATEWAY_NATIVE": (),                # native rendering, all classes
    "GT_GATEWAY_EDIT_BRIDGES": (),          # edit before/after event bridges
    "GT_LANE_ENVELOPE": (),                 # the evidence envelope
    "GT_REGISTRY_ENFORCE": (),              # the executable-registry gate
    "GT_GLOBAL_ARBITER": (),                # the one-dose global arbiter
    "GT_EDIT_OVERLAY": (),                  # episode-overlay freshness
    "GT_L6_FRESH": (),                      # per-turn in-container reindex freshness
    "GT_XSESSION_MEMORY": (),               # cross-session learned policy
    "GT_XSESSION_RANKUP": (),               # cross-session winner promotion
    "GT_BRIEF_MINIMAL": ("obligations", "localization"),  # step-0 brief shaping
    "GT_STEER_NATIVE": ("recovery",),       # native rendering of the steer/recovery fact
    "GT_POST_SEARCH_NATIVE": ("def_partition",),          # native def-partition render
    "GT_SCOPE_NATIVE": ("localization",),   # native scope constraint (localization surface)
    "GT_CONTRACT_MODE": ("caller_contract",),             # contract shaping
    "GT_CONTRACT_BILATERAL": ("caller_contract",),        # bilateral contract shaping
    "GT_D7_RELATEDNESS": ("cochange_prior",),             # relatedness → companion/cochange
    "GT_OBLIGATION_FRESHNESS": ("obligations",),          # obligation freshness
    "GT_VERIFY_EXECUTE": ("covering_red",),
    "GT_VERIFICATION_PLAN": ("covering_red",),
    "GT_COMPLETION_CERT": ("submit_refusal",),
    "GT_CONTENT_LEG": ("localization",),
    "GT_SEM_BODY": ("localization",),
    # W10 RL-native FORM sweep (2026-07-13) — the same native-render family as
    # GT_STEER_NATIVE/GT_POST_SEARCH_NATIVE/GT_SCOPE_NATIVE: FORM of an existing class,
    # never a new fact producer. Caught by the 2-task smoke (run 29232070057): the
    # import-time crosscheck fail-louded on these 5 as UNCLASSIFIED — correct behavior,
    # classified here as mediators.
    "GT_CONTRACT_NATIVE": ("caller_contract",),           # native contract diagnostic form
    "GT_EVIDENCE_NATIVE": ("caller_contract",),           # native evidence rg-row form
    "GT_NUDGE_NATIVE": ("recovery",),                     # nudge frame drop (imperative body)
    "GT_BRIEF_NATIVE": ("obligations",),                  # brief obligations checklist form
    "GT_INSEAM_METRICS": (),                              # host-side instrumentation only
    # SS-1..SS-N SUPER-SEAM adherence sweep (2026-07-13) — every one is an INFRA MEDIATOR
    # (arbitrate/dedup/novelty/timing/provenance/telemetry over EXISTING classes), never a new
    # fact PRODUCER, so they get correctness+mediation metrics only, never a direct help credit.
    # An empty tuple = a KERNEL mediator that touches ALL classes (the arbiter/novelty/dedup/
    # late-drop/provenance/ack kernel); a named class = the class that SS behavior most shapes.
    "GT_SS_ARBITER_V2": (),                               # the one-dose arbiter (defer/relax/empty-guard)
    "GT_SS_NOVELTY": (),                                  # novelty gate across all classes
    "GT_SS_DEDUP2": (),                                   # 2nd-order cross-plane dedup, all classes
    "GT_SS_RECOVERY_V2": ("recovery",),                  # recovery selection v2
    "GT_SS_PROVENANCE": (),                               # provenance/seal on every delivery
    "GT_SS_LATE_DROP": (),                                # late-fact drop timing, all classes
    "GT_SS_ACK_METRICS": (),                              # host-side ack telemetry only
    "GT_SS_ACK_FORM": (),                                 # SS-5 FORM: preamble reframes reading of ALL classes + obligations checklist
    "GT_SS_EXEC_TRUTH": ("covering_red",),                # SS-2 mediator: runner-eligible covering selection; kills unexecuted assurances
    "GT_SS_ELIGIBILITY": (),                              # SS-4 mediator: cd-$() prefix widening for search isolation (post_search/loc legs)
    "GT_POST_SEARCH": ("def_partition",),                # ITEM 0: post_search lattice MASTER enable (eligibility gate for the def_partition producer)
    "GT_SS_SHADOW": (),                                   # SS-8 mediator: shadow-holdout deliver/withhold across ALL participating advisory classes (E10 causal instrument; inert at rate 0)
    # P4 (B-TERM 2026-07-16): GT_SS_COHERENCE_V2 reclassified byte_owner → mediator
    # (feature_lineage CAP_BYTE_OWNER_IDS). It mediates the ``recovery`` FACT class — the
    # coherence-collapse detector shapes the recovery/pivot nudge: fact_registry aliases
    # ``coherence_collapse`` → ``recovery`` (_EVIDENCE_TYPE_ALIASES) and authorizes producer
    # ``ss_coherence_v2`` for it (_EVIDENCE_TYPE_PRODUCERS). Its SS-LIVE obligation is now
    # live_control_mediation_effect, not the (unsatisfiable) byte-owner bar.
    "GT_SS_COHERENCE_V2": ("recovery",),
}

# Backing-module → producer agreement drift-guard: these members' _MEMBER_CAPABILITY_MODULE
# basename must equal the registry ``producer`` of their declared fact class (proves the
# table is not drifting from the code). Only the members whose module IS the producer.
_MODULE_PRODUCER_MEMBERS: dict[str, str] = {
    "GT_EDIT_CHECK": "edit_check",
    "GT_PATCH_DELTA": "patch_delta",
    "GT_CHANGE_SURFACE": "change_surface",
}


def member_role(member: str) -> str:
    try:
        cap_role = cap_role_for(member)
    except ValueError as exc:
        raise KeyError(f"gt_feature_metrics: unclassified CAP member {member!r}") from exc
    if cap_role == "byte_owner":
        return ROLE_DIRECT
    if cap_role in {"eligibility", "mediator"}:
        return ROLE_INFRA
    raise KeyError(
        f"gt_feature_metrics: Profile-2 member {member!r} is UNCLASSIFIED — add it to "
        f"_DIRECT_MEMBER_FACTCLASS or _INFRA_MEMBER_MEDIATES (never let a feature drop "
        f"silently out of the output)."
    )


def member_fact_classes(member: str) -> tuple[str, ...]:
    """The fact classes a member produces (direct) or mediates (infra). Empty = kernel
    mediator (all classes)."""
    if member in _DIRECT_MEMBER_FACTCLASS:
        fact_class = _DIRECT_MEMBER_FACTCLASS[member]
        return (fact_class,) if fact_class is not None else ()
    return _INFRA_MEMBER_MEDIATES[member]


def _member_chronological_time(
    member: str, timing_by_fc: dict[str, bool | None]
) -> bool | None:
    """SPEC-J3: a byte-owner member's timing = the join over its owned fact class(es). True
    only when every measured owned class is ON_TIME (and at least one is measured); False if
    any owned class is LATE/STEP_BEHIND; None when no owned class is measured (fail-closed)."""
    measured = [
        timing_by_fc.get(fc)
        for fc in member_fact_classes(member)
        if timing_by_fc.get(fc) is not None
    ]
    if not measured:
        return None
    return all(measured)


def _member_fair_probe(
    member: str, fair_probe_by_fc: dict[str, bool | None]
) -> bool | None:
    """SPEC-J4: a byte-owner member's fair-probe gate = the join over its owned fact class(es).
    True only when every measured owned class is a proven causal result (Cluster-4 B5:
    behavioral CAUSAL only; mechanism-only CAUSAL_FORK and CAUSAL_PAIRED never set the gate);
    False if any owned class self-localized; None when no owned class is measured (fail-closed)."""
    measured = [
        fair_probe_by_fc.get(fc)
        for fc in member_fact_classes(member)
        if fair_probe_by_fc.get(fc) is not None
    ]
    if not measured:
        return None
    return all(measured)


def _member_acknowledgment(
    member: str, ack_by_fc: dict[str, bool | None]
) -> bool | None:
    """B-cluster Gate 4: a byte-owner member's acknowledgment = the join over its owned fact
    class(es), using the registry-specific receipt evaluator rollup. True only when every
    measured owned class acknowledged (>=1 measured); False if any owned class did NOT; None
    when no owned class is measured (fail-closed -> the receipt-ladder fallback then applies)."""
    measured = [
        ack_by_fc.get(fc)
        for fc in member_fact_classes(member)
        if ack_by_fc.get(fc) is not None
    ]
    if not measured:
        return None
    return all(measured)


def profile_members(profile: str) -> list[str]:
    """The Profile-2 members, enumerated DYNAMICALLY from rl_profile (never hardcoded)."""
    rp, _ = _profile_registry()
    members = rp.PROFILE_MEMBERS.get(str(profile))
    if not members:
        raise KeyError(f"gt_feature_metrics: unknown profile {profile!r}")
    return sorted(members)


def _import_time_crosscheck() -> None:
    """Fail LOUD if the member classification has drifted from rl_profile / fact_registry."""
    rp, fr = _profile_registry()
    members = set(rp.PROFILE_MEMBERS["2"])
    classified = set(_DIRECT_MEMBER_FACTCLASS) | set(_INFRA_MEMBER_MEDIATES)
    if set(_DIRECT_MEMBER_FACTCLASS) != set(CAP_BYTE_OWNER_IDS):
        raise ValueError("gt_feature_metrics: byte-owner table drift from feature_lineage")
    expected_owner_classes = {
        feature_id: _owner_fact_class(feature_id)
        for feature_id in CAP_BYTE_OWNER_MECHANISMS
    }
    if _DIRECT_MEMBER_FACTCLASS != expected_owner_classes:
        raise ValueError(
            "gt_feature_metrics: byte-owner FACT projection drift from mechanism authority"
        )
    if set(_INFRA_MEMBER_MEDIATES) != set(CAP_ELIGIBILITY_IDS | CAP_MEDIATOR_IDS):
        raise ValueError("gt_feature_metrics: CAP control table drift from feature_lineage")
    # (a) every profile member is classified; no stray classification for a non-member.
    missing = members - classified
    if missing:
        raise ValueError(f"gt_feature_metrics: unclassified Profile-2 members {sorted(missing)}")
    stray = classified - members
    if stray:
        raise ValueError(f"gt_feature_metrics: classified non-members {sorted(stray)}")
    # (b) every declared fact class resolves in the registry.
    all_classes = set(fr.all_fact_classes())
    for m, fc in _DIRECT_MEMBER_FACTCLASS.items():
        if fc is None:
            continue
        if fc not in all_classes:
            raise ValueError(f"gt_feature_metrics: {m} → unknown fact class {fc!r}")
    for m, fcs in _INFRA_MEMBER_MEDIATES.items():
        for fc in fcs:
            if fc not in all_classes:
                raise ValueError(f"gt_feature_metrics: {m} mediates unknown class {fc!r}")
    # (c) every registry fact class is produced or mediated by >=1 member (coverage).
    covered = {fc for fc in _DIRECT_MEMBER_FACTCLASS.values() if fc is not None}
    for fcs in _INFRA_MEMBER_MEDIATES.values():
        covered |= set(fcs)
    uncovered = all_classes - covered
    if uncovered:
        raise ValueError(f"gt_feature_metrics: fact classes with no member {sorted(uncovered)}")
    # (d) module→producer drift guard for the 5 module-backed producers.
    cap = getattr(rp, "_MEMBER_CAPABILITY_MODULE", {})
    for m, producer in _MODULE_PRODUCER_MEMBERS.items():
        mod = cap.get(m, "")
        if mod and mod.rsplit(".", 1)[-1] != producer:
            raise ValueError(
                f"gt_feature_metrics: {m} module {mod!r} basename != producer {producer!r} "
                f"(table drift vs rl_profile._MEMBER_CAPABILITY_MODULE)"
            )
        fact_class = _DIRECT_MEMBER_FACTCLASS[m]
        if fact_class is None:
            raise ValueError(f"gt_feature_metrics: {m} module producer has no FACT class")
        reg = fr.registration(fact_class)
        if reg is not None and reg.producer != producer:
            raise ValueError(
                f"gt_feature_metrics: {m} declared producer {producer!r} != registry "
                f"producer {reg.producer!r} for {fact_class!r}"
            )


_import_time_crosscheck()


# ---------------------------------------------------------------------------
# Ledger layer → fact class. The ``gateway.<evidence_type>`` family is resolved
# DYNAMICALLY through fact_registry aliases; the legacy engine labels are an
# explicit documented table. ``ga.`` (global-arbiter candidate) prefix is stripped
# first. Returns None for a pure-infra layer (e.g. L6 reindex) with no fact class.
# ---------------------------------------------------------------------------
_LEGACY_LAYER_FACTCLASS: dict[str, str] = {
    "l3b.evidence": "caller_contract",
    "l3.contract": "caller_contract",
    "consensus.scope_map": "localization",
    "consensus.scope": "localization",
    "edit.syntax": "syntax_result",
    "semantic_drift": "cochange_prior",
    "spec.obligation": "obligations",
    "obligation.resurface": "obligations",
    "detect.loop": "recovery",
    "recovery": "recovery",
    "completion_cert": "submit_refusal",
    "submit_gate": "submit_refusal",
    "cochange": "cochange_prior",
    "l3.cochange": "cochange_prior",
    "nudge": "recovery",
}
_INFRA_LAYER_PREFIXES = ("L6", "l6")  # freshness/reindex staging — not a fact class


def layer_to_fact_class(layer: str) -> str | None:
    lay = (layer or "").strip()
    if not lay:
        return None
    if lay.startswith("ga."):
        lay = lay[3:]
    for p in _INFRA_LAYER_PREFIXES:
        if lay == p or lay.startswith(p + "."):
            return None
    mapped = _LEGACY_LAYER_FACTCLASS.get(lay)
    if mapped is not None:
        return mapped
    # Runtime rows may carry a registry evidence-type directly (including an
    # arbiter ``ga.`` prefix), while Gateway rows namespace it as ``gateway.*``.
    # Resolve both through the executable registry before declaring the layer
    # infrastructure-only/unmapped.
    evidence_type = lay.split(".", 1)[1] if lay.startswith("gateway.") else lay
    _, fr = _profile_registry()
    reg = fr.registration_for(evidence_type)
    return reg.fact_class if reg is not None else None


def _typed_fact_class(payload: object) -> str | None:
    """Return a FACT only from registry-valid producer lineage.

    Layer names are routing labels, not ownership.  In particular, the generic
    verification advisory cannot borrow ``covering_red`` from the executed
    covering runner merely because both share the verify horizon.
    """
    if not isinstance(payload, dict):
        return None
    lineage = payload.get("feature_lineage")
    row = lineage if isinstance(lineage, dict) else payload
    if row.get("schema", row.get("lineage_schema")) != "gt.feature_lineage.v1":
        return None
    evidence_type = row.get("evidence_type")
    runtime_producer = row.get("runtime_producer_id")
    if not isinstance(evidence_type, str) or not evidence_type:
        return None
    registered_producer = row.get("registered_producer_id")
    fact_class = row.get("fact_class")
    if not isinstance(fact_class, str) or not fact_class:
        return None
    registration = (
        registration_for(evidence_type) if isinstance(evidence_type, str) else None
    )
    if (
        row.get("producer_registration_match") is not True
        or registration is None
        or registration.fact_class != fact_class
        or registration.producer != registered_producer
        or not isinstance(runtime_producer, str)
        or not producer_matches(evidence_type, runtime_producer)
    ):
        return None
    layer = str(payload.get("ledger_layer") or payload.get("layer") or "")
    if fact_class == "covering_red" and layer.startswith("verify.horizon.") and (
        layer != "verify.horizon.executed" or runtime_producer != "covering_runner"
    ):
        return None
    return str(fact_class)


def is_arbiter_candidate(layer: str) -> bool:
    return (layer or "").strip().startswith("ga.")


# consumption-ledger ``kind`` (W1 v2 tag families) → fact class.
_CONSUMPTION_KIND_FACTCLASS: dict[str, str] = {
    "l3b.evidence": "caller_contract",
    "l3b.contract": "caller_contract",
    "consensus.scope": "localization",
    "cochange": "cochange_prior",
    "nudge": "recovery",
    "brief.task": "obligations",
    "brief.localization": "localization",
    "brief.obligations": "obligations",
}


# ---------------------------------------------------------------------------
# Artifact loading (structured; never a content grep for a verdict string).
# ---------------------------------------------------------------------------

def _load_json(path: str) -> Any:
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError, TypeError):
        return None


def load_jsonl(path: str) -> list[dict]:
    """Load a JSONL artifact in stored order (chronological structured read)."""
    rows: list[dict] = []
    if not path or not os.path.isfile(path):
        return rows
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(obj, dict):
                    rows.append(obj)
    except OSError:
        pass
    return rows


def load_jsonl_strict(path: str) -> tuple[list[dict], list[int]]:
    """Load JSONL while retaining malformed line numbers for fail-closed audits."""
    rows: list[dict] = []
    invalid: list[int] = []
    if not path or not os.path.isfile(path):
        return rows, invalid
    try:
        with open(path, encoding="utf-8", errors="strict") as stream:
            for line_number, line in enumerate(stream, 1):
                if not line.strip():
                    continue
                try:
                    value = json.loads(line)
                except (UnicodeDecodeError, json.JSONDecodeError):
                    invalid.append(line_number)
                    continue
                if not isinstance(value, dict):
                    invalid.append(line_number)
                    continue
                rows.append(value)
    except (OSError, UnicodeDecodeError):
        invalid.append(0)
    return rows, invalid


def _visible_audit_inputs_complete(
    trajectory_path: str | None, ledger_path: str | None,
) -> bool:
    """True only when both visible-byte audit inputs parse completely."""
    if not trajectory_path or not ledger_path:
        return False
    try:
        with open(trajectory_path, encoding="utf-8") as fh:
            trajectory = json.load(fh)
        if not isinstance(trajectory, dict) or not isinstance(
            trajectory.get("messages"), list
        ) or not trajectory["messages"]:
            return False
        if not validate_attestation(ledger_path):
            return False
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, TypeError):
        return False
    return True


def _find_one(task_dir: str, *patterns: str) -> str | None:
    for pat in patterns:
        hits = sorted(glob.glob(os.path.join(task_dir, pat)))
        if hits:
            return hits[0]
    return None


def _find_named_input(task_dir: str, filename: str, *, locations: int = 3) -> str | None:
    """Find an exact-name task input in the task dir or its two run parents.

    The live workflow places feature inputs across ``/tmp/gt/<task>`` and
    ``/tmp``.  The lookup is intentionally bounded and never glob-selects a
    different task's artifact.
    """
    current = os.path.abspath(task_dir)
    if locations < 1:
        raise ValueError("gt_feature_metrics: named-input locations must be positive")
    for _ in range(locations):
        candidate = os.path.join(current, filename)
        if os.path.isfile(candidate):
            return candidate
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent
    return None


def _receipt_corroborated_acknowledgment(
    task: str,
    task_dir: str,
    rows: list[dict],
    chronologies: list[Any],
    trajectory_acknowledgment: dict[str, bool | None],
    *,
    messages: list[dict],
    attestations: Iterable[Any] | None,
) -> tuple[dict[str, bool | None], dict[str, Any]]:
    """Require sealed sidecar corroboration for envelope-owned delivery rows.

    The sidecar never creates acknowledgment.  A row can pass only when its
    registry-specific model-authored trajectory predicate is True *and* the exact
    ``(seal, candidate, observation, opportunity)`` receipt ladder reached
    ``referenced`` or later.  Compound task-start brief blocks have no envelope
    receipt path and retain their trajectory-only result.
    """
    expected = sealed_receipt_expected(rows)
    sidecar_path = _find_one(
        task_dir,
        f"gt_receipts_{task}.jsonl",
        "gt_receipts_*.jsonl",
        "gt_receipts.jsonl",
    )
    marker_paths = sorted(
        glob.glob(os.path.join(task_dir, "gt_receipt_integrity_*.json"))
    )
    sidecar = load_receipt_sidecar(
        sidecar_path or os.path.join(task_dir, f"gt_receipts_{task}.jsonl"),
        sealed_delivery_expected=expected,
        collection_integrity_paths=marker_paths,
    )

    values_by_class: dict[str, list[bool | None]] = defaultdict(list)
    joins: list[dict[str, Any]] = []
    join_failures: set[str] = set()
    envelope_rows_seen = 0
    expected_row_indices = {
        index for index, row in enumerate(rows)
        if sealed_receipt_expected((row,))
    }
    joined_row_indices: set[int] = set()
    expected_keys: set[Any] = set()
    for chronology in chronologies:
        fact_class = getattr(chronology, "fact_class", None)
        row_index = getattr(chronology, "ledger_row_index", None)
        if type(row_index) is not int or row_index < 0 or row_index >= len(rows):
            continue
        # CLASS-2(b): the receipt IDENTITY join is keyed on the bound+sealed envelope delivery — the
        # SAME condition as sealed_receipt_expected — NOT on fact_class registration, so the
        # expectation and the join stay CONSISTENT. An unregistered fact_class means only that there
        # is no registered acknowledgment predicate to GRADE; it must never turn a real sealed
        # delivery into a phantom ``receipt_expected_delivery_chronology_missing`` (a row counted
        # expected but skipped from the join) nor, when the lane path persisted its receipt, an
        # orphan ``receipt_sidecar_unbound_identity`` (a real receipt whose key was never reconciled).
        # So EVERY bound+sealed row is reconciled (joined + its key added to expected_keys); only a
        # REGISTERED class additionally contributes a graded acknowledgment value — a registered
        # class is byte-identical to before. envelope-owned shares the DECLARED
        # _RECEIPT_EXEMPT_LAYERS with sealed_receipt_expected so an exempt layer (submit_refusal via
        # producer attestation, brief.task via block-lineage) is trajectory-only on BOTH sides.
        registered = isinstance(fact_class, str) and bool(fact_class)
        row = rows[row_index]
        try:
            binding = observation_binding_from_dict(row.get("observation_binding"))
        except (TypeError, ValueError):
            binding = None
        candidate_id = row.get("candidate_id")
        seal = row.get("content_sha256_16")
        envelope_owned = (
            row.get("layer") not in _RECEIPT_EXEMPT_LAYERS
            and binding is not None
            and not validate_observation_binding(binding)
            and isinstance(candidate_id, str)
            and bool(candidate_id)
            and isinstance(seal, str)
        )
        if not envelope_owned:
            if registered:
                values_by_class[fact_class].append(acknowledgment_for_row(
                    chronology,
                    messages=messages,
                    ledger_rows=rows,
                    attestations=attestations,
                ))
            continue

        envelope_rows_seen += 1
        joined_row_indices.add(row_index)
        try:
            key = canonical_receipt_key(
                content_sha256_16=seal,
                candidate_id=candidate_id,
                observation_id=binding.observation_id,
                opportunity_id=binding.opportunity_id,
            )
        except (TypeError, ValueError):
            if registered:
                values_by_class[fact_class].append(None)
            join_failures.add("receipt_delivery_identity_invalid")
            continue
        expected_keys.add(key)
        if not registered:
            # sealed delivery with NO registered acknowledgment predicate: identity reconciled
            # (joined + expected) so it is neither a phantom missing-chronology nor an orphan
            # unbound-identity — there is simply no registered class to attribute a value to.
            continue
        trajectory_value = acknowledgment_for_row(
            chronology,
            messages=messages,
            ledger_rows=rows,
            attestations=attestations,
        )
        joined = join_receipt_evidence(
            sidecar,
            key,
            trajectory_corroborated=trajectory_value is True,
        )
        # THE SIDECAR CAN NO LONGER DISPROVE ACKNOWLEDGMENT (2026-07-28).
        #
        # `acknowledgment_supported` is `_TRANSITION_RANK[transition] >= rank("referenced")`
        # (receipt_sidecar), and the runtime is now permitted to write ONLY `delivered`
        # (`RUNTIME_EMITTABLE_RECEIPT_STATES`) because the seam cannot evaluate any higher
        # rung -- it has no `policy_text` and no decision-commit index. So `delivered` is no
        # longer evidence that the model did not acknowledge; it is the only thing the writer
        # is ALLOWED to say.
        #
        # Reading it as disproof here would be actively worse than the bug it replaced. The
        # observation-binding fix routes rows INTO this join for the first time (`envelope_
        # owned` requires a non-null binding; run 30390877219 had `envelope_rows_seen: 0`, so
        # every row took the trajectory-only branch below). Combining the two changes without
        # this guard would turn a True trajectory predicate into an affirmative False, flip
        # Gate 4 `acknowledged` to False for every envelope-owned registered class, and
        # terminal them all NOVEL_IGNORED -- SS-LIVE 0/17 again, but now unfalsifiable in the
        # opposite direction.
        #
        # The sidecar's honest remaining role is IDENTITY CORROBORATION (`integrity_ok` +
        # `matched`); the acknowledgment verdict belongs to the trajectory predicate, and the
        # precommit-window authority is `fair_probe_result._treatment_acted`.
        if trajectory_value is None or not joined.integrity_ok or not joined.matched:
            value: bool | None = None
        elif trajectory_value is False:
            value = False
        elif not joined.acknowledgment_supported and _runtime_ladder_is_capped():
            # Corroborated identity, and the writer was structurally unable to say more.
            # Defer to the trajectory rather than manufacture a disproof.
            value = trajectory_value
        else:
            value = joined.acknowledgment_supported
        values_by_class[fact_class].append(value)
        join_failures.update(joined.failure_codes)
        joins.append({
            "ledger_row_index": row_index,
            "fact_class": fact_class,
            "content_sha256_16": key.content_sha256_16,
            "candidate_id": key.candidate_id,
            "observation_id": key.observation_id,
            "opportunity_id": key.opportunity_id,
            "trajectory_acknowledgment": trajectory_value,
            "sidecar_transition": joined.sidecar_transition,
            "matched": joined.matched,
            "acknowledgment_supported": value,
            "failure_codes": list(joined.failure_codes),
        })

    corroborated = dict(trajectory_acknowledgment)
    for fact_class, values in values_by_class.items():
        if values and all(value is True for value in values):
            corroborated[fact_class] = True
        elif any(value is False for value in values):
            corroborated[fact_class] = False
        else:
            corroborated[fact_class] = None
    sidecar_failures = [
        {
            "code": failure.code,
            "line_number": failure.line_number,
            "detail": failure.detail,
        }
        for failure in sidecar.failures
    ]
    join_failures.update(failure["code"] for failure in sidecar_failures)
    missing_expected_rows = sorted(expected_row_indices - joined_row_indices)
    unexpected_sidecar_keys = sorted(
        {
            record.key for record in sidecar.records
            if record.key not in expected_keys
        }
    )
    if missing_expected_rows:
        join_failures.add("receipt_expected_delivery_chronology_missing")
    if unexpected_sidecar_keys:
        join_failures.add("receipt_sidecar_unbound_identity")
    integrity_ok = (
        sidecar.integrity_ok
        and (not expected or sidecar.source_present)
        and not join_failures
        and (not expected or envelope_rows_seen > 0)
    )
    return corroborated, {
        "schema": "gt.receipt_corroboration.v1",
        "sealed_delivery_expected": expected,
        "sidecar_source": sidecar.source,
        "sidecar_present": sidecar.source_present,
        "sidecar_integrity_ok": sidecar.integrity_ok,
        "envelope_rows_seen": envelope_rows_seen,
        "expected_ledger_row_indices": sorted(expected_row_indices),
        "joined_ledger_row_indices": sorted(joined_row_indices),
        "missing_expected_ledger_row_indices": missing_expected_rows,
        "unexpected_sidecar_identity_count": len(unexpected_sidecar_keys),
        "joins": joins,
        "failures": sidecar_failures,
        "join_failure_codes": sorted(join_failures),
        "integrity_ok": integrity_ok,
    }


def _value_honors_8dp(value: Any) -> bool:
    """True iff every numeric leaf can be represented without losing >8dp precision."""
    if value is None or isinstance(value, (bool, int)):
        return True
    if isinstance(value, float):
        return math.isfinite(value) and round(value, 8) == value
    if isinstance(value, dict):
        return all(
            isinstance(key, str) and _value_honors_8dp(item)
            for key, item in value.items()
        )
    return False


# Per-metric provenance overrides (D2/D4/D5, 2026-07-18). The generic pointers
# ``MANDATORY_METRICS.md#{section}.{name}`` / ``mandatory_contract:{value_type}`` neither name the
# REAL formula basis nor the ACTUAL task-scope denominator. These overrides make each honest:
# they name the underscore denominator source in the deep-metrics artifact and, for the two token
# metrics that publish an ESTIMATE / STEP-PROXY rather than exact per-token attribution, DISCLOSE
# that basis so a reader never mistakes a proxy for a measured token formula. Any (section, name)
# not listed keeps the honest task-scope default (``gt_deep_metrics:performance.{section}.{name}``).
_PERF_PROVENANCE_OVERRIDES: dict[tuple[str, str], dict[str, str]] = {
    # D2: wasted_token_rate is a STEP proxy, NOT the §9 per-token formula (no per-step token
    # attribution exists in the trajectory) — say so in formula_provenance so it is never
    # mislabeled MEASURED-as-token.
    ("token_efficiency", "wasted_token_rate"): {
        "formula_provenance": "STEP-PROXY non_gold_steps/(gold_steps+non_gold_steps); NOT the "
        "MANDATORY §9 per-token formula (no per-step token attribution in the trajectory)",
        "denominator_provenance": "gt_deep_metrics:performance.token_efficiency._non_idle_step_count "
        "(step-count proxy, not tokens)",
    },
    # D5: gt_token_overhead's numerator gt_injected_tokens is a chars/4 token ESTIMATE — disclose it.
    ("token_efficiency", "gt_token_overhead"): {
        "formula_provenance": "gt_injected_tokens(=gt_observation_chars/4 token ESTIMATE)/"
        "total_tokens_in (MANDATORY_METRICS.md#token_efficiency.gt_token_overhead)",
        "denominator_provenance": "gt_deep_metrics:performance.token_efficiency.total_tokens_in",
    },
    # D4: name the real underscore/field denominators for the gold-ratio task-scope rows.
    ("token_efficiency", "tokens_per_gold_edit"): {
        "denominator_provenance": "gt_deep_metrics:performance.token_efficiency._n_gold_edited",
    },
    ("localization", "localization_recall"): {
        "denominator_provenance": "gt_deep_metrics:performance.localization.n_gold_files",
    },
    ("localization", "navigation_directness"): {
        "denominator_provenance": "gt_deep_metrics:performance.localization.n_gold_files",
    },
    ("scope_completeness", "scope_coverage"): {
        "denominator_provenance": "gt_deep_metrics:performance.scope_completeness.n_gold_files",
    },
}


def _perf_provenance(section: str, name: str) -> tuple[str, str]:
    """Honest (formula_provenance, denominator_provenance) for a task-scope PERF row. The default
    denominator NAMES the real source field in the deep-metrics artifact (D4 — never the fake
    ``mandatory_contract:<type>``); the override map refines the audit-named proxy/estimate rows."""
    override = _PERF_PROVENANCE_OVERRIDES.get((section, name), {})
    formula = override.get("formula_provenance", f"MANDATORY_METRICS.md#{section}.{name}")
    denom = override.get(
        "denominator_provenance", f"gt_deep_metrics:performance.{section}.{name}"
    )
    return formula, denom


def _performance_feature_records(
    task: str, task_dir: str,
) -> tuple[dict[str, dict[str, Any]], list[str], str | None]:
    from gt_run_metrics import _metric_state

    definitions = performance_metric_definitions()
    path = _find_named_input(task_dir, f"gt_deep_metrics_{task}.json")
    payload = _load_json(path) if path else None
    artifact = os.path.basename(path) if path else None
    records: dict[str, dict[str, Any]] = {}
    missing: list[str] = []

    identity_payload = (
        payload
        if isinstance(payload, dict) and payload.get("task_id") == task
        else None
    )
    identity_ok = identity_payload is not None
    artifact_schema_valid = bool(
        identity_payload is not None
        and identity_payload.get("schema") == "gt_deep_metrics.v2"
    )
    precision_decimals = (
        identity_payload.get("precision_decimals")
        if identity_payload is not None else None
    )
    if path and not identity_ok:
        missing.append("PERF.task_identity")
    for section, metrics in definitions.items():
        section_payload: Any = None
        if identity_payload is not None:
            if section == "behavioral_impact":
                section_payload = identity_payload.get(section)
            else:
                performance = identity_payload.get("performance")
                section_payload = performance.get(section) if isinstance(performance, dict) else None
        for name, value_type in metrics:
            if value_type == "run_ratio":
                records[name] = {
                    "family": "PERF", "status": "NOT_APPLICABLE",
                    "source": "gt_run_metrics", "source_artifact": None,
                    "value": None, "value_type": value_type,
                    "metric_structure_valid": True,
                    "value_precision_valid": True,
                    "artifact_schema_valid": False,
                    "precision_decimals": None,
                    "formula_provenance": f"MANDATORY_METRICS.md#{section}.{name}",
                    "denominator_provenance": f"mandatory_contract:{value_type}",
                    "coverage_scope": "run",
                    "reason": "run-level ratio; measured only after run aggregation",
                }
                continue
            present = isinstance(section_payload, dict) and name in section_payload
            raw = section_payload.get(name) if present else None
            state, _normalized_value, applicability = (
                _metric_state(identity_payload, section, name, value_type)
                if identity_payload is not None else ("unmeasured", None, None)
            )
            status = {
                "measured": "MEASURED",
                "not_applicable": "NOT_APPLICABLE",
                "right_censored": "RIGHT_CENSORED",
                "unmeasured": "UNMEASURED",
                "failed": "UNMEASURED",
            }[state]
            if (
                section == "behavioral_impact"
                and identity_payload is not None
                and isinstance(identity_payload.get("behavioral_impact"), dict)
                and identity_payload["behavioral_impact"].get("collection_error")
            ):
                status = "UNMEASURED"
            if status == "UNMEASURED":
                missing.append(f"PERF.{name}")
            records[name] = {
                "family": "PERF", "status": status,
                "source": "gt_deep_metrics", "source_artifact": artifact,
                "value": raw if status == "MEASURED" else None,
                "value_type": value_type,
                "metric_structure_valid": status in {
                    "MEASURED", "NOT_APPLICABLE", "RIGHT_CENSORED",
                },
                "value_precision_valid": (
                    True if status == "RIGHT_CENSORED"
                    else _value_honors_8dp(raw) if present else False
                ),
                "artifact_schema_valid": artifact_schema_valid,
                "precision_decimals": precision_decimals,
                "formula_provenance": _perf_provenance(section, name)[0],
                "denominator_provenance": _perf_provenance(section, name)[1],
                "coverage_scope": "run" if value_type == "run_ratio" else "task",
                "applicability": applicability,
                "observation": (
                    applicability.get("observation")
                    if isinstance(applicability, dict) else None
                ),
                "reason": None if status in {"MEASURED", "RIGHT_CENSORED"} else (
                    "not applicable for this task" if status == "NOT_APPLICABLE"
                    else "required metric missing or malformed"
                ),
            }
    return records, sorted(set(missing)), path


def _run_ratio_feature_record(
    run_id: str,
    artifact_path: str | None,
    *,
    section: str,
    name: str,
    value_type: str,
    expected_tasks: int,
) -> dict[str, Any]:
    """Validate one run-ratio row from the authoritative gt_run_metrics.v2 artifact."""
    payload = _load_json(artifact_path) if artifact_path else None
    metric: Any = None
    if isinstance(payload, dict):
        mandatory = payload.get("mandatory_performance")
        section_payload = mandatory.get(section) if isinstance(mandatory, dict) else None
        metric = section_payload.get(name) if isinstance(section_payload, dict) else None
    status = metric.get("status") if isinstance(metric, dict) else None
    value = metric.get("value") if isinstance(metric, dict) else None
    applicability = metric.get("applicability") if isinstance(metric, dict) else None
    population = payload.get("task_population") if isinstance(payload, dict) else None
    applicable = (
        applicability.get("applicable") if isinstance(applicability, dict) else None
    )
    predicate = (
        applicability.get("predicate") if isinstance(applicability, dict) else None
    )
    applicability_reason = (
        applicability.get("reason") if isinstance(applicability, dict) else None
    )
    applicability_valid = bool(
        isinstance(applicable, bool)
        and isinstance(predicate, str) and bool(predicate.strip())
        and isinstance(applicability_reason, str)
        and bool(applicability_reason.strip())
    )
    measured_value_valid = (
        status == "MEASURED"
        and applicability_valid and applicable is True
        and isinstance(value, (int, float)) and not isinstance(value, bool)
        and math.isfinite(float(value)) and float(value) >= 0.0
        and isinstance(payload, dict) and payload.get("resolved", 0) > 0
    )
    not_applicable_valid = (
        status == "NOT_APPLICABLE" and value is None
        and applicability_valid and applicable is False
        and isinstance(payload, dict) and payload.get("resolved") == 0
    )
    contract_valid = bool(
        isinstance(payload, dict)
        and payload.get("schema") == "gt_run_metrics.v2"
        and payload.get("run_id") == run_id
        and payload.get("precision_decimals") == 8
        and payload.get("mandatory_performance_metric_count") == 58
        and payload.get("mandatory_performance_collection_complete") is True
        and payload.get("tasks") == expected_tasks
        and isinstance(population, dict)
        and population.get("expected_count") == expected_tasks
        and population.get("observed_record_count") == expected_tasks
        and population.get("observed_unique_count") == expected_tasks
        and population.get("missing_tasks") == []
        and population.get("duplicate_tasks") == []
        and population.get("unexpected_tasks") == []
        and population.get("invalid_task_records") == []
        and payload.get("invalid_deep_metric_records") == {}
        and isinstance(metric, dict)
        and metric.get("value_type") == value_type
        and metric.get("aggregation") == "ratio_of_run_total_cost_to_resolved_count"
        and metric.get("missing_tasks") == []
        and metric.get("measured_tasks") == expected_tasks
        and (measured_value_valid or not_applicable_valid)
        and isinstance(payload.get("token_efficiency"), dict)
        and payload["token_efficiency"].get(name) == value
        and payload["token_efficiency"].get("cost_collection_complete") is True
    )
    return {
        "family": "PERF",
        "status": status if contract_valid else "UNMEASURED",
        "source": "gt_run_metrics",
        "source_artifact": (
            os.path.basename(artifact_path)
            if contract_valid and isinstance(artifact_path, str) else None
        ),
        "value": value if contract_valid and status == "MEASURED" else None,
        "value_type": value_type,
        "metric_structure_valid": contract_valid,
        "value_precision_valid": contract_valid and _value_honors_8dp(value),
        "artifact_schema_valid": contract_valid,
        "precision_decimals": 8 if contract_valid else None,
        "formula_provenance": f"MANDATORY_METRICS.md#{section}.{name}",
        "denominator_provenance": "gt_run_metrics:run total_cost_usd/resolved count",
        "coverage_scope": "run",
        "applicability": applicability if applicability_valid else None,
        "run_aggregate": metric if contract_valid else None,
        "task_coverage_valid": contract_valid,
        "aggregate_coverage_valid": contract_valid,
        "reason": None if contract_valid else "run-level metric artifact missing or malformed",
    }


def _run_distribution_feature_record(
    run_id: str,
    artifact_path: str | None,
    task_rows: list[dict[str, Any]],
    *,
    section: str,
    name: str,
    value_type: str,
    expected_tasks: int,
) -> dict[str, Any]:
    """Bind one task-scope PERF row to its canonical complete run distribution."""
    payload = _load_json(artifact_path) if artifact_path else None
    metric: Any = None
    if isinstance(payload, dict):
        mandatory = payload.get("mandatory_performance")
        section_payload = mandatory.get(section) if isinstance(mandatory, dict) else None
        metric = section_payload.get(name) if isinstance(section_payload, dict) else None
    population = payload.get("task_population") if isinstance(payload, dict) else None
    status = metric.get("status") if isinstance(metric, dict) else None
    measured_tasks = metric.get("measured_tasks") if isinstance(metric, dict) else None
    not_applicable = metric.get("not_applicable_tasks") if isinstance(metric, dict) else None
    right_censored = metric.get("right_censored_tasks") if isinstance(metric, dict) else None
    event_observed = metric.get("event_observed_tasks") if isinstance(metric, dict) else None
    task_ids = [row.get("_task") for row in task_rows]
    task_identity_valid = bool(
        len(task_ids) == expected_tasks
        and all(isinstance(task, str) and bool(task) for task in task_ids)
        and len(set(task_ids)) == expected_tasks
    )
    task_measured = sorted(
        task for row, task in zip(task_rows, task_ids)
        if row.get("status") == "MEASURED" and isinstance(task, str)
    )
    task_not_applicable = sorted(
        task for row, task in zip(task_rows, task_ids)
        if row.get("status") == "NOT_APPLICABLE" and isinstance(task, str)
    )
    task_censored = sorted(
        task for row, task in zip(task_rows, task_ids)
        if row.get("status") == "RIGHT_CENSORED" and isinstance(task, str)
    )
    task_rows_resolved = bool(
        task_identity_valid
        and all(row.get("status") in {
            "MEASURED", "NOT_APPLICABLE", "RIGHT_CENSORED",
        } for row in task_rows)
        and len(task_measured) + len(task_not_applicable) + len(task_censored)
        == expected_tasks
    )
    aggregate_partition_valid = bool(
        isinstance(not_applicable, list)
        and all(isinstance(task, str) for task in not_applicable)
        and sorted(not_applicable) == task_not_applicable
        and isinstance(right_censored, list)
        and all(isinstance(task, str) for task in right_censored)
        and sorted(right_censored) == task_censored
        and isinstance(measured_tasks, int) and not isinstance(measured_tasks, bool)
        and measured_tasks == len(task_measured) + len(task_censored)
        and (
            value_type == "per_tag_rate_dict" and event_observed is None
            or isinstance(event_observed, list)
            and all(isinstance(task, str) for task in event_observed)
            and sorted(event_observed) == task_measured
        )
    )
    contract_valid = bool(
        isinstance(payload, dict)
        and payload.get("schema") == "gt_run_metrics.v2"
        and payload.get("run_id") == run_id
        and payload.get("precision_decimals") == 8
        and payload.get("mandatory_performance_metric_count") == 58
        and payload.get("mandatory_performance_collection_complete") is True
        and payload.get("tasks") == expected_tasks
        and isinstance(population, dict)
        and population.get("expected_count") == expected_tasks
        and population.get("observed_record_count") == expected_tasks
        and population.get("observed_unique_count") == expected_tasks
        and population.get("missing_tasks") == []
        and population.get("duplicate_tasks") == []
        and population.get("unexpected_tasks") == []
        and population.get("invalid_task_records") == []
        and payload.get("invalid_deep_metric_records") == {}
        and isinstance(metric, dict)
        and metric.get("value_type") == value_type
        and status in {"MEASURED", "NOT_APPLICABLE"}
        and metric.get("missing_tasks") == []
        and metric.get("unmeasured_tasks") == []
        and metric.get("failed_tasks") == []
        and aggregate_partition_valid
        and task_rows_resolved
    )
    first = dict(task_rows[0]) if task_rows else {}
    first.pop("_task", None)
    return {
        **first,
        "status": status if contract_valid else "UNMEASURED",
        "source": "gt_run_metrics",
        "source_artifact": (
            os.path.basename(artifact_path)
            if contract_valid and isinstance(artifact_path, str) else None
        ),
        "value": None,
        "value_type": value_type,
        "metric_structure_valid": contract_valid,
        "value_precision_valid": contract_valid,
        "artifact_schema_valid": contract_valid,
        "precision_decimals": 8 if contract_valid else None,
        "coverage_scope": "run",
        "run_aggregate": metric if contract_valid else None,
        "task_coverage_valid": task_rows_resolved,
        "aggregate_coverage_valid": contract_valid,
        "reason": None if contract_valid else "canonical run distribution missing or malformed",
    }


# ---------------------------------------------------------------------------
# Ledger classification — the offline transition derivation.
# ---------------------------------------------------------------------------

def classify_ledger(rows: list[dict]) -> dict[str, dict[str, Any]]:
    """Bucket runtime-ledger rows by fact class, deriving the delivery/arbitration/dose
    transitions the seam already records (defect #2 — no seam edit).

    Per fact class returns:
      produced, delivered, arbiter_candidates, arbiter_lost, dose_suppressed,
      stale, expired_late, delivered_chars, delivered_rows (index list),
      delivered_boundaries (set of event_type), delivered_files (set).
    """
    per: dict[str, dict[str, Any]] = defaultdict(lambda: {
        "produced": 0, "delivered": 0, "arbiter_candidates": 0, "arbiter_lost": 0,
        "dose_suppressed": 0, "stale": 0, "expired_late": 0, "allowed": 0,
        # loser_* = a SUPPRESSED arbiter/dose candidate's late/stale reason. It is
        # mediation info (a losing candidate), NOT a correctness failure of the
        # DELIVERED fact — so it must NOT drive the delivered fact to FIX. Only a row
        # actually DELIVERED late/stale sets expired_late/stale.
        "loser_late": 0, "loser_stale": 0,
        "delivered_chars": 0, "delivered_rows": [], "delivered_boundaries": set(),
        "delivered_files": set(), "suppressed_hidden": 0,
    })
    for idx, r in enumerate(rows):
        layer = str(r.get("layer") or "")
        # task #35: the obligations PLAN-LOAD marker. Its layer is deliberately in NO
        # layer→fact-class map (every mapped row counts as `produced` below, and a
        # load-time marker must never manufacture production) — so it is captured HERE,
        # under a sentinel key, dict-shaped so the `.values()` aggregations read zeros.
        # Sole consumer: `_fact_class_eligible("obligations")`.
        if layer == "obligation.plan" and str(r.get("reason") or "") == "obligation_plan_loaded":
            per["__obligation_plan_loaded__"]["marker"] = True
            continue
        fc = _typed_fact_class(r) or layer_to_fact_class(layer)
        # P1-2 (2026-07-29): a canonical.provider_delivery row carries its facts NESTED
        # in ``evidence_lineage`` ([{candidate_id, fact_class, cap_owners}]) with no
        # top-level evidence_type and a layer in no layer→fact map — so all 12
        # fixed-smoke capsules vanished from per-class lifecycle counts
        # (_obligations_delivered=0 despite 10 lineages). Each REGISTERED lineage class
        # is credited against the shared capsule row; unregistered entries credit
        # nothing (fail-closed). Same expansion direction_arrival landed in 9f634c1db.
        fact_classes: list[str] = [fc] if fc is not None else []
        if fc is None and layer == "canonical.provider_delivery":
            for entry in r.get("evidence_lineage") or []:
                if not isinstance(entry, dict):
                    continue
                lin_fc = entry.get("fact_class")
                if (
                    isinstance(lin_fc, str)
                    and lin_fc
                    and lin_fc not in fact_classes
                    and registration_for(lin_fc) is not None
                ):
                    fact_classes.append(lin_fc)
        if not fact_classes:
            continue
        outcome = str(r.get("outcome") or "")
        reason = str(r.get("reason") or "")
        # ── MARKER ROWS ARE NOT PRODUCTION (2026-07-29). The seam writes rows that
        # annotate or mark, never manufacture a fact; counting them as `produced`
        # (and the ack rows as `delivered`) inflated every lifecycle verdict:
        #   * ss_ack annotations ride outcome="delivered" with chars=0 — the WRITER's
        #     contract (gt_mini_patch._ss_emit_ack_row) says every delivered-payload
        #     view "requires chars_delivered>0" and excludes them; this reader must too.
        #   * outcome="eligible" (producer_boundary) marks an OPPORTUNITY, not a fact.
        #   * outcome="evaluated" is the trigger census — the denominator for dark.
        #   * allow/clean verdicts mean the gate RAN and correctly produced NOTHING —
        #     tracked as `allowed` (correct silence), never `produced`.
        #   * any other outcome="delivered" row with chars<=0 is internal telemetry
        #     by the same writer contract.
        if str(r.get("event_type") or "") == "ack" or reason == "ss_ack":
            continue
        if outcome in ("eligible", "evaluated"):
            continue
        if outcome in ("allow", "submit_clean", "clean", "allow_clean"):
            # the gate RAN and correctly ALLOWED (e.g. a clean submit) — a CORRECT
            # ABSTAIN of the refusal fact, NOT a dark/missing delivery.
            for fc in fact_classes:
                per[fc]["allowed"] += 1
            continue
        if outcome == "delivered" and int(r.get("chars_delivered") or 0) <= 0:
            continue
        arb = is_arbiter_candidate(layer)
        for fc in fact_classes:
            b = per[fc]
            b["produced"] += 1
            if arb:
                b["arbiter_candidates"] += 1
            if outcome == "delivered":
                b["delivered"] += 1
                b["delivered_chars"] += int(r.get("chars_delivered") or 0)
                b["delivered_rows"].append(idx)
                b["delivered_boundaries"].add(str(r.get("event_type") or ""))
                # a DELIVERED fact whose own reason flags stale/late is a real correctness/
                # timing failure (→ FIX), distinct from a suppressed loser's lateness.
                if "stale" in reason:
                    b["stale"] += 1
                if "late" in reason or "expired" in reason:
                    b["expired_late"] += 1
                fp = r.get("file_path")
                if fp:
                    b["delivered_files"].add(fp)
                # Caller-contract CO-FACT (2026-07-20): the SAME physical delivery also
                # carries authorized caller-contract bytes (the seam's ``co_fact`` sidecar,
                # stamped when the pre-edit def-facts block renders a ``callers:`` line).
                # Credit caller_contract delivered on this SAME row so its
                # delivered_byte_proven gate reflects the pre-edit delivery, at its
                # search_result boundary. Dose-safe: this mints NO physical_id (dose is
                # graded on the shared physical_id via the consumption ledger) — it is a
                # second FACT credit on ONE physical delivery, never a second dose.
                # Authorized identity only (self-declared registered producer/evidence/
                # class). Legacy single-fact rows only — a canonical capsule never stamps
                # co_fact, and its lineage classes are already expanded above.
                co = r.get("co_fact")
                if (
                    isinstance(co, dict)
                    and co.get("fact_class") == "caller_contract"
                    and co.get("evidence_type") == "caller_contract_search"
                    and co.get("producer_registration_match") is True
                ):
                    cb = per["caller_contract"]
                    cb["produced"] += 1
                    cb["delivered"] += 1
                    cb["delivered_chars"] += int(r.get("chars_delivered") or 0)
                    cb["delivered_rows"].append(idx)
                    cb["delivered_boundaries"].add("search_result")
                    if fp:
                        cb["delivered_files"].add(fp)
            elif outcome == "suppressed_hidden_only":
                b["suppressed_hidden"] += 1
                if arb or reason.startswith("global_arbiter:"):
                    b["arbiter_lost"] += 1
                if "late" in reason:
                    b["loser_late"] += 1
                if "stale" in reason:
                    b["loser_stale"] += 1
            elif outcome == "suppressed_duplicate":
                b["dose_suppressed"] += 1
    return per


# ---------------------------------------------------------------------------
# Trajectory-derived state predicates (agent-observation only).
# ---------------------------------------------------------------------------

def _timeline(trajectory: dict) -> list[dict]:
    """Reuse the performance module's chronological timeline parser (one source of truth)."""
    pm = load_performance_module()
    return pm._parse_timeline(trajectory.get("messages", []) or [])


def _path_match(a: str, b: str) -> bool:
    a, b = (a or "").strip("/"), (b or "").strip("/")
    if not a or not b:
        return False
    return a == b or a.endswith("/" + b) or b.endswith("/" + a)


def state_predicates(timeline: list[dict], ledger_by_fc: dict[str, dict]) -> dict[str, Any]:
    """The PROVABLE fact-specific state-change predicates (handoff step 5), from the agent's
    own chronological actions. Each returns (eligible, state_changed, note) — a value only
    where the trajectory proves it, else UNMEASURED at the call site."""
    steps = [e for e in timeline if e["role"] == "assistant"]
    out: dict[str, Any] = {}

    # syntax_result (edit_check): eligible iff an edit was followed by a build/syntax-fail
    # observation; state_changed iff a LATER observation to the same file shows no fail.
    edit_fail_files: list[tuple[int, str]] = []
    for i, ev in enumerate(timeline):
        if ev["role"] == "assistant" and ev.get("is_edit") and ev.get("edited_file"):
            for j in range(i + 1, min(i + 3, len(timeline))):
                if timeline[j]["role"] == "observation" and timeline[j].get("has_build_fail"):
                    edit_fail_files.append((ev["step"], ev["edited_file"]))
                    break
    syntax_eligible = bool(edit_fail_files)
    syntax_cleared = False
    if syntax_eligible:
        # a later clean observation after a re-edit of the flagged file
        for fstep, ffile in edit_fail_files:
            later_clean = False
            for ev in timeline:
                if ev["role"] == "observation" and ev["step"] > fstep and not ev.get("has_build_fail"):
                    later_clean = True
                    break
            if later_clean:
                syntax_cleared = True
                break
    out["syntax_result"] = (syntax_eligible, syntax_cleared)

    # covering_red: eligible iff a test observation with a failure marker exists;
    # state_changed iff a later test observation is clean (RED→GREEN).
    test_fail_steps = [
        ev["step"] for i, ev in enumerate(timeline)
        if ev["role"] == "assistant" and ev.get("is_test")
        and any(timeline[j].get("has_build_fail")
                for j in range(i + 1, min(i + 3, len(timeline)))
                if timeline[j]["role"] == "observation")
    ]
    covering_eligible = bool(test_fail_steps)
    covering_green = False
    if covering_eligible:
        last_fail = max(test_fail_steps)
        for i, ev in enumerate(timeline):
            if ev["role"] == "assistant" and ev.get("is_test") and ev["step"] > last_fail:
                clean = all(
                    not timeline[j].get("has_build_fail")
                    for j in range(i + 1, min(i + 3, len(timeline)))
                    if timeline[j]["role"] == "observation"
                )
                obs_exists = any(
                    timeline[j]["role"] == "observation"
                    for j in range(i + 1, min(i + 3, len(timeline)))
                )
                if clean and obs_exists:
                    covering_green = True
                    break
    out["covering_red"] = (covering_eligible, covering_green)

    # localization / def_partition: eligible iff a localization decision was open (>=1
    # search); state_changed iff a delivered site was edited without a later re-search of it.
    searched = any(ev.get("is_search") for ev in steps)
    delivered_loc_files: set[str] = set()
    for fc in ("localization", "def_partition"):
        for fp in ledger_by_fc.get(fc, {}).get("delivered_files", set()):
            delivered_loc_files.add(fp)
    edited_delivered = False
    for ev in steps:
        if ev.get("is_edit") and ev.get("edited_file"):
            if any(_path_match(ev["edited_file"], d) for d in delivered_loc_files):
                edited_delivered = True
                break
    loc_eligible = searched or bool(delivered_loc_files)
    out["localization"] = (loc_eligible, edited_delivered and bool(delivered_loc_files))
    out["def_partition"] = (bool(ledger_by_fc.get("def_partition", {}).get("delivered")), edited_delivered)

    # submit_refusal: eligible iff a submit_refusal was delivered (a completion cert / gate
    # fired); state_changed iff the agent returned to edit/test AFTER that refusal.
    refusal_delivered = bool(ledger_by_fc.get("submit_refusal", {}).get("delivered"))
    returned_to_work = False
    if refusal_delivered:
        # the refusal fires late (near submit); any edit/test in the final third counts.
        n = len(steps)
        tail = steps[int(n * 0.66):] if n else []
        returned_to_work = any(ev.get("is_edit") or ev.get("is_test") for ev in tail)
    out["submit_refusal"] = (refusal_delivered, returned_to_work)
    return out


# ---------------------------------------------------------------------------
# Baseline behavioural endpoints (agent-observation only; matched trajectory).
# ---------------------------------------------------------------------------

def behavioural_endpoints(timeline: list[dict]) -> dict[str, int | None]:
    """A small, gold-free set of agent-observed behavioural endpoints for a trajectory."""
    steps = [e for e in timeline if e["role"] == "assistant"]
    total_steps = len(steps)
    first_edit_step = None
    unique_views_before_edit: set[str] = set()
    search_count = 0
    for ev in steps:
        if ev.get("is_search"):
            search_count += 1
        if ev.get("is_edit") and first_edit_step is None:
            first_edit_step = ev["step"]
        if first_edit_step is None and ev.get("viewed_file"):
            unique_views_before_edit.add(ev["viewed_file"])
    return {
        "total_steps": total_steps,
        "steps_to_first_edit": first_edit_step,
        "files_viewed_before_first_edit": len(unique_views_before_edit),
        "search_count": search_count,
    }


def _baseline_trajectory_path(task: str, baseline_root: str | None) -> str | None:
    if not baseline_root or not os.path.isdir(baseline_root):
        return None
    for half in ("", "half0", "half1"):
        base = os.path.join(baseline_root, half) if half else baseline_root
        for name in (f"ll-full-{task}", task):
            cand = os.path.join(base, name, "mini-swe-agent.trajectory.json")
            if os.path.isfile(cand):
                return cand
    return None


# ---------------------------------------------------------------------------
# Consumption receipts (W1 v2) — the ONLY consumption source; tool output cannot promote.
# ---------------------------------------------------------------------------

def _physical_identity_conflicts(entries: object) -> set[str]:
    """Return physical ids with non-identical owners, independent of entry order."""
    claims: dict[str, set[str]] = defaultdict(set)
    if not isinstance(entries, list):
        return set()
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("source") != "trajectory":
            continue
        physical_id = entry.get("physical_id")
        if not isinstance(physical_id, str) or not physical_id:
            continue
        claims[physical_id].add(json.dumps(
            entry, sort_keys=True, ensure_ascii=False, default=str
        ))
    return {physical_id for physical_id, variants in claims.items() if len(variants) > 1}


def _consumption_by_fact_class(
    trajectory: dict, runtime_ledger_path: str | None,
) -> "tuple[dict[str, dict], dict]":
    """Per-fact-class receipt rollup from W1's v2 consumption ledger (chronological receipt
    grading; NEVER token-overlap or same-file coincidence — defect #3). Returns
    ``(per_fact_class_rollup, full_ledger)`` — the ledger rides along for integrity fields."""
    cl_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "consumption_ledger.py")
    import importlib.util
    spec = importlib.util.spec_from_file_location("consumption_ledger_gfm", cl_path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    ledger = mod.build_consumption_ledger(trajectory, runtime_ledger_path=runtime_ledger_path)
    out: dict[str, dict] = defaultdict(lambda: {"delivered": 0, "referenced": 0, "acted": 0, "max_level": 0})
    ledger_join_required = bool(runtime_ledger_path)
    conflict_ids = _physical_identity_conflicts(ledger.get("entries"))
    conflict_ids.update(ledger.get("physical_identity_conflict_ids") or [])
    seen_physical: set[str] = set()
    for entry_index, entry in enumerate(ledger.get("entries", []) or []):
        if not isinstance(entry, dict) or entry.get("source") != "trajectory":
            continue
        if ledger_join_required and entry.get("joined") is not True:
            continue
        physical_id = str(
            entry.get("physical_id") or f"entry:{entry_index}"
        )
        if physical_id in conflict_ids:
            continue
        if physical_id in seen_physical:
            continue
        seen_physical.add(physical_id)
        kind = str(entry.get("kind") or "")
        fc = _typed_fact_class(entry) or layer_to_fact_class(
            str(entry.get("ledger_layer") or kind)
        )
        if fc is None:
            fc = _CONSUMPTION_KIND_FACTCLASS.get(kind)
        if fc is None:
            continue
        lvl = int(entry.get("receipt") or 0)
        agg = out[fc]
        agg["delivered"] += 1
        agg["referenced"] += int(lvl >= 2)
        agg["acted"] += int(lvl >= 3)
        agg["max_level"] = max(agg["max_level"], lvl)
    return dict(out), ledger


def unjoined_receipts_by_fact_class(ledger: dict) -> dict[str, int]:
    """Per fact class, the count of HOST-RECORDED deliveries with NO model-visible receipt.

    C15-shape companion to :func:`_consumption_by_fact_class` (2026-07-28). That function
    rolls up receipts over the rows it could JOIN to the trajectory; this one counts the rows
    it could NOT — the ``source == "ledger_only"`` entries the consumption ledger mints for a
    delivered runtime row that never matched an observation block (``receipt: None``,
    ``joined: False``). Those are UNMEASURED deliveries, not consumed ones and not inert ones.

    WHY THIS IS A SEPARATE NAMESPACE, NOT A SUBTRACTION: ``classify_ledger``'s per-class
    ``delivered`` and the consumption rollup's ``delivered`` are different populations — the
    caller-contract co-fact credits a second FACT on ONE physical delivery, and the
    consumption ledger dedups by ``physical_id``. Differencing those two counts would invent
    holes. The hole count is read from the evidence that names itself a hole.
    """
    out: dict[str, int] = defaultdict(int)
    for entry in (ledger or {}).get("entries", []) or []:
        if not isinstance(entry, dict) or entry.get("source") != "ledger_only":
            continue
        kind = str(entry.get("kind") or "")
        fc = _typed_fact_class(entry) or layer_to_fact_class(
            str(entry.get("ledger_layer") or kind)
        )
        if fc is None:
            fc = _CONSUMPTION_KIND_FACTCLASS.get(kind)
        if fc is None:
            continue
        out[fc] += 1
    return dict(out)


# ---------------------------------------------------------------------------
# Native (tag-free) delivery detection via the runtime-ledger CONTENT SEAL
# (content_sha256_16) joined to the model-visible observation bytes — defect #2.
#
# Super-Mode facts ride native channels (grep output, compiler notes, test
# transcripts) with NO <gt-*> tag, so W1's tag-based consumption ladder cannot see
# them. The seam SEALS each delivered fact (sha256[:16] of the exact rendered bytes).
# This joins a DELIVERED ledger row to the observation that carries those bytes by
# the seal — never by fuzzy text. A row WITHOUT a seal is model-receipt UNMEASURED
# (host-attested delivery only); this is the transition that still needs in-seam
# instrumentation (the seam must emit content_sha256_16 per delivered native fact).
# ---------------------------------------------------------------------------
_MAX_SEAL_SCAN = 40000  # cap the per-observation window scan (bounds offline cost)


def _content_carries_seal(content: str, seal: str, chars: int, native_text: str | None) -> bool:
    """True iff ``content`` contains the exact delivered bytes for ``seal``.

    Fast path: the seam recorded the exact ``native_text`` → substring + hash check.
    Fallback: slide a ``chars``-wide window and hash — bounded by :data:`_MAX_SEAL_SCAN`."""
    if native_text:
        return native_text in content and _sha16(native_text) == seal
    if chars <= 0 or chars > len(content) or len(content) > _MAX_SEAL_SCAN:
        return False
    for off in range(0, len(content) - chars + 1):
        if _sha16(content[off:off + chars]) == seal:
            return True
    return False


def _sha16(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()[:16]


def join_native_delivery(rows: list[dict], messages: list[dict]) -> dict[int, int | None]:
    """Map each DELIVERED ledger row index -> the observation message index that carries its
    sealed bytes (model-receipt), or None when unconfirmable.

    None means UNMEASURED (no seal, or the seal did not match any observation) — NEVER a
    silent False that reads as 'not received'. Requires the seam to emit ``content_sha256_16``
    (optionally ``native_text``) per delivered row."""
    obs = [
        (i, _c)
        for i, m in enumerate(messages)
        if isinstance(m, dict) and m.get("role") in ("tool", "user")
        and isinstance((_c := m.get("content")), str)
    ]
    out: dict[int, int | None] = {}
    for idx, r in enumerate(rows):
        if str(r.get("outcome") or "") != "delivered":
            continue
        seal = r.get("content_sha256_16")
        if not seal:
            out[idx] = None  # no seal → model-receipt UNMEASURED (in-seam TODO)
            continue
        chars = int(r.get("chars_delivered") or 0)
        native_text = r.get("native_text") if isinstance(r.get("native_text"), str) else None
        hit = None
        for mi, content in obs:
            if content and _content_carries_seal(content, seal, chars, native_text):
                hit = mi
                break
        out[idx] = hit
    return out


def native_visible_by_fact_class(rows: list[dict], messages: list[dict]) -> dict[str, int]:
    """Per fact class, the count of DELIVERED native rows whose sealed bytes were located in
    a model-visible observation (defect-2 native receipt)."""
    join = join_native_delivery(rows, messages)
    out: dict[str, int] = defaultdict(int)
    for idx, mi in join.items():
        if mi is None:
            continue
        fc = _typed_fact_class(rows[idx]) or layer_to_fact_class(
            str(rows[idx].get("layer") or "")
        )
        if fc is not None:
            out[fc] += 1
    return dict(out)


_NATIVE_RENDER_IDS: frozenset[str] = frozenset({"native", "lane"})


def native_renderer_audit_by_fact_class(rows: list[dict]) -> dict[str, bool]:
    """Per fact class, the exact registry-renderer audit verdict for its DELIVERED rows
    (defect-5, run #2). Replaces the fabricated ``native_valid = measured(True)`` that
    passed on MERE delivery.

    A delivered row proves NATIVE form only when BOTH hold, joined on the same delivered
    row (candidate+seal+span the seam already stamps):
      * its ``renderer_id`` is a native-channel render (``native``/``lane``) — a bespoke
        ``tagged`` <gt-*> render is NOT the registry native form; and
      * its ``evidence_type`` resolves to a registered class with a required native
        renderer (``fact_registry.required_renderer`` is not ``None``).

    A class is native-valid True iff EVERY render-identity-carrying delivered row of that
    class proves native form AND at least one such row exists; any ``tagged`` (or
    renderer-mismatched) delivered row taints the class to False. A class whose delivered
    rows carry NO render identity is ABSENT from this map → honest UNMEASURED upstream
    (never a fabricated True)."""
    verdict: dict[str, bool] = {}
    for r in rows:
        if not isinstance(r, dict) or str(r.get("outcome") or "") != "delivered":
            continue
        renderer_id = r.get("renderer_id")
        if not isinstance(renderer_id, str) or not renderer_id:
            continue  # no render identity → cannot audit this row (fail-closed)
        fc = _typed_fact_class(r) or layer_to_fact_class(str(r.get("layer") or ""))
        if fc is None:
            continue
        evidence_type = r.get("evidence_type")
        native_ok = bool(
            renderer_id in _NATIVE_RENDER_IDS
            and isinstance(evidence_type, str)
            and evidence_type
            and required_renderer(evidence_type) is not None
        )
        verdict[fc] = native_ok if fc not in verdict else (verdict[fc] and native_ok)
    return verdict


_CANONICAL_ACK_FEATURE = "GT_SS_ACK_METRICS"
_CANONICAL_DELIVERY_SCHEMA = "gt.canonical_delivery.v1"
_CANONICAL_ACK_SCHEMA = "gt.canonical_ack_receipt.v1"


def _is_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(char in "0123456789abcdef" for char in value)
    )


def _canonical_json_text(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _canonical_message_hash(message: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        _canonical_json_text(message).encode("utf-8")
    ).hexdigest()


def _normalized_ack_phrase(value: object) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(value.casefold().split())


def _ack_path_anchor(value: object) -> str:
    if not isinstance(value, str):
        return ""
    candidate = value.strip().replace("\\", "/")
    candidate = candidate.split("::", 1)[0]
    candidate = re.sub(r":\d+(?::\d+)?$", "", candidate)
    if candidate.startswith("/testbed/"):
        candidate = candidate[len("/testbed/"):]
    return candidate if "/" in candidate else ""


def _ack_symbol_anchor(value: object) -> str:
    if not isinstance(value, str):
        return ""
    candidate = value.rsplit("::", 1)[-1].strip()
    if "/" in candidate or "\\" in candidate:
        return ""
    return (
        candidate
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{2,}", candidate)
        else ""
    )


def _canonical_ack_linked_action(
    actions: object,
    evidence: Mapping[str, Any],
) -> bool:
    if not isinstance(actions, (list, tuple)):
        return False
    provenance = evidence.get("provenance")
    subject = evidence.get("subject")
    if (
        not isinstance(subject, str)
        or not isinstance(provenance, list)
        or not all(isinstance(value, str) for value in provenance)
    ):
        return False
    path_anchors = {
        anchor
        for value in (subject, *provenance)
        if (anchor := _ack_path_anchor(value))
    }
    symbol_anchors = {
        anchor
        for value in (subject, *provenance)
        if (anchor := _ack_symbol_anchor(value))
    }
    for action in actions:
        if not isinstance(action, Mapping):
            continue
        fragments = [
            action.get(key)
            for key in ("command", "path", "file_path", "target", "subject")
            if isinstance(action.get(key), str)
        ]
        action_text = " ".join(fragments).replace("\\", "/")
        if not action_text:
            continue
        if any(
            re.search(
                rf"(?<![A-Za-z0-9_./-]){re.escape(path)}"
                rf"(?![A-Za-z0-9_./-])",
                action_text,
            )
            for path in path_anchors
        ):
            return True
        if any(
            re.search(rf"(?<!\w){re.escape(symbol)}(?!\w)", action_text)
            for symbol in symbol_anchors
        ):
            return True
    return False


def _validated_canonical_delivery(
    row: dict[str, Any],
) -> tuple[str, dict[str, Any], Any]:
    if (
        row.get("schema") != _CANONICAL_DELIVERY_SCHEMA
        or row.get("event_type") != "canonical_provider_delivery"
        or row.get("layer") != "canonical.provider_delivery"
        or row.get("outcome") != "delivered"
        or "fact_class" in row
    ):
        raise ValueError("malformed canonical delivery row")
    delivery_attempt_id = row.get("delivery_attempt_id")
    capsule_text = row.get("capsule_text")
    evidence_ids = row.get("evidence_ids")
    manifest_json = row.get("evidence_manifest_json")
    provider_payload_json = row.get("bound_provider_payload_json")
    if (
        not isinstance(delivery_attempt_id, str)
        or not delivery_attempt_id
        or not isinstance(row.get("observation_id"), str)
        or not row["observation_id"]
        or not isinstance(row.get("model_call_id"), str)
        or not row["model_call_id"]
        or not isinstance(capsule_text, str)
        or not capsule_text
        or not isinstance(evidence_ids, list)
        or not evidence_ids
        or not all(
            isinstance(evidence_id, str) and evidence_id
            for evidence_id in evidence_ids
        )
        or len(set(evidence_ids)) != len(evidence_ids)
        or not isinstance(manifest_json, str)
        or not manifest_json
        or not isinstance(provider_payload_json, str)
        or not provider_payload_json
        or not _is_sha256(row.get("capsule_hash"))
        or not _is_sha256(row.get("rendered_content_hash"))
        or not _is_sha256(row.get("evidence_manifest_hash"))
        or not _is_sha256(row.get("provider_payload_hash"))
        or not isinstance(row.get("provider_response_id"), str)
        or not row["provider_response_id"]
        or row.get("provider_terminal_kind") not in {
            "COMPLETED",
            "INCOMPLETE",
            "TOOL_USE",
            "REFUSAL",
        }
        or type(row.get("delivery_phase_ordinal")) is not int
        or row["delivery_phase_ordinal"] <= 0
    ):
        raise ValueError("malformed canonical delivery identity")
    rendered_hash = hashlib.sha256(
        capsule_text.encode("utf-8")
    ).hexdigest()
    if (
        rendered_hash != row["rendered_content_hash"]
        or row.get("content_sha256_16") != rendered_hash[:16]
        or row.get("chars_delivered") != len(capsule_text)
    ):
        raise ValueError("canonical capsule text/seal mismatch")
    manifest = json.loads(manifest_json)
    if (
        not isinstance(manifest, dict)
        or _canonical_json_text(manifest) != manifest_json
        or hashlib.sha256(
            manifest_json.encode("utf-8")
        ).hexdigest() != row["evidence_manifest_hash"]
    ):
        raise ValueError("canonical manifest hash mismatch")
    manifest_evidence = manifest.get("evidence")
    if (
        not isinstance(manifest_evidence, list)
        or [
            item.get("evidence_id")
            for item in manifest_evidence
            if isinstance(item, dict)
        ] != evidence_ids
    ):
        raise ValueError("canonical manifest membership mismatch")
    for item in manifest_evidence:
        if (
            not isinstance(item, dict)
            or not isinstance(item.get("subject"), str)
            or not isinstance(item.get("claim"), str)
            or not item["claim"]
            or not isinstance(item.get("actionable_consequence"), str)
            or not isinstance(item.get("provenance"), list)
            or not all(
                isinstance(value, str) for value in item["provenance"]
            )
        ):
            raise ValueError("malformed canonical manifest member")
    expected_capsule_hash = hashlib.sha256(
        _canonical_json_text(
            {
                "schema": _DECISION_CAPSULE_SCHEMA,
                "rendered_content_hash": rendered_hash,
                "evidence_manifest_hash": row["evidence_manifest_hash"],
            }
        ).encode("utf-8")
    ).hexdigest()
    if row["capsule_hash"] != expected_capsule_hash:
        raise ValueError("canonical capsule identity mismatch")
    binding = observation_binding_from_dict(
        row.get("observation_binding")
    )
    if (
        binding is None
        or validate_observation_binding(
            binding,
            expected_candidate_id=row["capsule_hash"],
        )
    ):
        raise ValueError("canonical delivery binding mismatch")

    capsule_binding = row.get("capsule_binding")
    if not isinstance(capsule_binding, dict):
        raise ValueError("canonical capsule binding missing")
    if (
        capsule_binding.get("schema") != "gt.capsule_binding.v1"
        or capsule_binding.get("model_call_id") != row["model_call_id"]
        or capsule_binding.get("observation_id") != row["observation_id"]
        or capsule_binding.get("evidence_ids") != evidence_ids
        or capsule_binding.get("capsule_hash") != row["capsule_hash"]
        or capsule_binding.get("provider_payload_hash")
        != row["provider_payload_hash"]
        or capsule_binding.get("evidence_manifest_hash")
        != row["evidence_manifest_hash"]
        or type(capsule_binding.get("message_index")) is not int
        or capsule_binding["message_index"] < 0
        or type(capsule_binding.get("content_index")) is not int
        or capsule_binding["content_index"] < 0
    ):
        raise ValueError("canonical capsule binding mismatch")
    provider_payload = json.loads(provider_payload_json)
    if (
        _canonical_json_text(provider_payload) != provider_payload_json
        or hashlib.sha256(
            provider_payload_json.encode("utf-8")
        ).hexdigest() != row["provider_payload_hash"]
    ):
        raise ValueError("canonical provider payload hash mismatch")
    messages = (
        provider_payload.get("messages")
        if isinstance(provider_payload, dict) else None
    )
    message_index = capsule_binding["message_index"]
    content_index = capsule_binding["content_index"]
    if (
        not isinstance(messages, list)
        or message_index >= len(messages)
        or not isinstance(messages[message_index], dict)
        or not isinstance(messages[message_index].get("content"), list)
        or content_index >= len(messages[message_index]["content"])
        or not isinstance(
            messages[message_index]["content"][content_index], dict
        )
        or messages[message_index]["content"][content_index].get("text")
        != capsule_text
    ):
        raise ValueError("canonical capsule location mismatch")
    return delivery_attempt_id, manifest, binding


def _validated_canonical_receipt(
    row: dict[str, Any],
) -> tuple[str, Any]:
    if (
        row.get("schema") != _CANONICAL_ACK_SCHEMA
        or row.get("event_type") != "canonical_ack_receipt"
        or row.get("layer") != "canonical.ack_receipt"
        or row.get("outcome") != "evaluated"
        or row.get("chars_delivered") != 0
        or int(row.get("receipt") or 0) < 3
        or "fact_class" in row
    ):
        raise ValueError("malformed canonical receipt row")
    delivery_attempt_id = row.get("delivery_attempt_id")
    if (
        not isinstance(delivery_attempt_id, str)
        or not delivery_attempt_id
        or not _is_sha256(row.get("capsule_hash"))
        or not _is_sha256(row.get("evidence_manifest_hash"))
        or not _is_sha256(row.get("response_hash"))
        or not isinstance(row.get("provider_response_id"), str)
        or not row["provider_response_id"]
        or not isinstance(row.get("matched_evidence_id"), str)
        or not row["matched_evidence_id"]
        or not isinstance(row.get("evidence_ids"), list)
        or type(row.get("delivery_phase_ordinal")) is not int
        or type(row.get("acknowledgment_phase_ordinal")) is not int
        or row["acknowledgment_phase_ordinal"]
        <= row["delivery_phase_ordinal"]
    ):
        raise ValueError("malformed canonical receipt identity")
    binding = observation_binding_from_dict(
        row.get("observation_binding")
    )
    if (
        binding is None
        or validate_observation_binding(
            binding,
            expected_candidate_id=row["capsule_hash"],
        )
    ):
        raise ValueError("canonical receipt binding mismatch")
    return delivery_attempt_id, binding


def _canonical_ack_evidence(
    rows: list[dict],
    messages: list[dict],
) -> tuple[
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[int],
]:
    """Join one capsule-level delivery to one exact committed acknowledgment."""

    invalid_rows: list[int] = []
    deliveries: dict[str, list[tuple[int, dict[str, Any], dict[str, Any], Any]]] = (
        defaultdict(list)
    )
    receipts: dict[str, list[tuple[int, dict[str, Any], Any]]] = defaultdict(list)
    for index, row in enumerate(rows):
        delivery_candidate = (
            row.get("schema") == _CANONICAL_DELIVERY_SCHEMA
            or row.get("event_type") == "canonical_provider_delivery"
        )
        receipt_candidate = (
            row.get("schema") == _CANONICAL_ACK_SCHEMA
            or (
                row.get("event_type") == "canonical_ack_receipt"
                and row.get("layer") == "canonical.ack_receipt"
            )
        )
        if delivery_candidate:
            try:
                delivery_attempt_id, manifest, binding = (
                    _validated_canonical_delivery(row)
                )
            except (KeyError, TypeError, ValueError):
                invalid_rows.append(index)
            else:
                deliveries[delivery_attempt_id].append(
                    (index, row, manifest, binding)
                )
        elif receipt_candidate:
            try:
                delivery_attempt_id, binding = (
                    _validated_canonical_receipt(row)
                )
            except (KeyError, TypeError, ValueError):
                invalid_rows.append(index)
            else:
                receipts[delivery_attempt_id].append(
                    (index, row, binding)
                )

    records: list[dict[str, Any]] = []
    joins: list[dict[str, Any]] = []
    for delivery_attempt_id, receipt_rows in receipts.items():
        delivery_rows = deliveries.get(delivery_attempt_id, [])
        if len(delivery_rows) != 1 or len(receipt_rows) != 1:
            continue
        delivery_index, delivery, manifest, delivery_binding = delivery_rows[0]
        receipt_index, receipt, receipt_binding = receipt_rows[0]
        identity_fields = (
            "capsule_hash",
            "evidence_manifest_hash",
            "evidence_ids",
            "provider_response_id",
            "delivery_phase_ordinal",
        )
        if (
            any(
                receipt.get(field) != delivery.get(field)
                for field in identity_fields
            )
            or receipt_binding != delivery_binding
        ):
            continue
        manifest_evidence = manifest["evidence"]
        matched_evidence_id = receipt["matched_evidence_id"]
        if matched_evidence_id not in {
            item["evidence_id"] for item in manifest_evidence
        }:
            continue
        expected_receipt_key = hashlib.sha256(
            _canonical_json_text(
                {
                    "delivery_attempt_id": delivery_attempt_id,
                    "capsule_hash": delivery["capsule_hash"],
                    "evidence_id": matched_evidence_id,
                    "provider_response_id": receipt[
                        "provider_response_id"
                    ],
                    "response_hash": receipt["response_hash"],
                }
            ).encode("utf-8")
        ).hexdigest()
        if receipt.get("receipt_key") != expected_receipt_key:
            continue
        committed_matches: list[tuple[int, Mapping[str, Any]]] = []
        for message_index, message in enumerate(messages):
            if (
                not isinstance(message, Mapping)
                or message.get("role") != "assistant"
            ):
                continue
            extra = message.get("extra")
            response = (
                extra.get("response")
                if isinstance(extra, Mapping) else None
            )
            if (
                isinstance(response, Mapping)
                and response.get("id") == receipt["provider_response_id"]
                and _canonical_message_hash(message)
                == receipt["response_hash"]
            ):
                committed_matches.append((message_index, message))
        if len(committed_matches) != 1:
            continue
        message_index, committed_message = committed_matches[0]
        normalized_content = _normalized_ack_phrase(
            committed_message.get("content")
        )
        extra = committed_message.get("extra")
        actions = (
            extra.get("actions")
            if isinstance(extra, Mapping) else None
        )
        acknowledged = [
            item
            for item in manifest_evidence
            if (
                _normalized_ack_phrase(item.get("claim"))
                and _normalized_ack_phrase(item.get("claim"))
                in normalized_content
                and _canonical_ack_linked_action(actions, item)
            )
        ]
        if (
            len(acknowledged) != 1
            or acknowledged[0]["evidence_id"] != matched_evidence_id
        ):
            continue
        item = {
            "row_index": receipt_index,
            "feature_id": _CANONICAL_ACK_FEATURE,
            "role": "mediator",
            "decision_site": "provider_response_commit",
            "decision": "APPLIED",
            "candidate_chars": delivery["chars_delivered"],
            "candidate_sha256_16": delivery["content_sha256_16"],
            "candidate_id": delivery["capsule_hash"],
            "fact_class": None,
            "observation_binding": observation_binding_to_dict(
                delivery_binding
            ),
            "delivery_attempt_id": delivery_attempt_id,
        }
        records.append(item)
        joins.append(
            {
                **item,
                "delivery_row_index": delivery_index,
                "delivery_layer": delivery["layer"],
                "delivery_phase_ordinal": delivery[
                    "delivery_phase_ordinal"
                ],
                "acknowledgment_phase_ordinal": receipt[
                    "acknowledgment_phase_ordinal"
                ],
                "capsule_hash": delivery["capsule_hash"],
                "evidence_manifest_hash": delivery[
                    "evidence_manifest_hash"
                ],
                "evidence_ids": list(delivery["evidence_ids"]),
                "matched_evidence_id": matched_evidence_id,
                "provider_response_id": receipt["provider_response_id"],
                "response_hash": receipt["response_hash"],
                "observation_message_index": message_index,
                "observation_joined": True,
                "canonical_delivery_joined": True,
                "receipt_level": int(receipt["receipt"]),
                "referenced_message_index": message_index,
                "acted_message_index": message_index,
            }
        )
    return records, joins, invalid_rows


def _control_participation_evidence(
    rows: list[dict], messages: list[dict], consumption_ledger: dict[str, Any],
    brief_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Validate typed control rows and join mediator candidates without inference.

    A profile flag, nearby class delivery, or same-iteration row is never evidence.
    Mediators require the producer contract, concrete candidate id/class/bytes, and an
    exact typed temporal relation to delivery. Ordinary controls precede a matching
    delivery. Receipt graders follow the exact delivery they grade and additionally
    require trajectory-authored receipt evidence. Observation/receipt data comes only
    from the existing final-delivery seal join.
    """
    records: dict[str, list[dict[str, Any]]] = defaultdict(list)
    joins: dict[str, list[dict[str, Any]]] = defaultdict(list)
    invalid_rows: list[int] = []
    invalid_brief_rows: list[int] = []
    observation_join = join_native_delivery(rows, messages)
    entries = consumption_ledger.get("entries")
    entries = entries if isinstance(entries, list) else []
    canonical_records, canonical_joins, canonical_invalid = (
        _canonical_ack_evidence(rows, messages)
    )
    if canonical_records:
        records[_CANONICAL_ACK_FEATURE].extend(canonical_records)
    if canonical_joins:
        joins[_CANONICAL_ACK_FEATURE].extend(canonical_joins)
    invalid_rows.extend(canonical_invalid)

    for index, row in enumerate(rows):
        if not (
            row.get("layer") == "control.participation"
            or row.get("schema") == CONTROL_PARTICIPATION_SCHEMA
        ):
            continue
        try:
            control_ref = row.get("control_ref")
            if not isinstance(control_ref, dict) or set(control_ref) != {
                "category", "feature_id", "role",
            } or control_ref.get("category") != "CAP":
                raise ValueError("malformed control_ref")
            if (
                row.get("schema") != CONTROL_PARTICIPATION_SCHEMA
                or row.get("layer") != "control.participation"
                or row.get("event_type") != "control_decision"
                or row.get("outcome") != "evaluated"
                or row.get("chars_delivered") != 0
                or row.get("participation_decision") == "ERROR"
                or not isinstance(row.get("reason"), str)
            ):
                raise ValueError("non-terminal or failed participation row")
            observation_binding = observation_binding_from_dict(
                row.get("observation_binding")
            )
            decision_site = row.get("decision_site")
            decision = row.get("participation_decision")
            iteration = row.get("iteration")
            candidate_chars = row.get("candidate_chars")
            candidate_sha256_16 = row.get("candidate_sha256_16")
            candidate_id = row.get("candidate_id")
            # Current seam rows reserve the ledger-level reason for row identity and
            # carry the control's domain reason separately. Historical artifacts put
            # the domain reason directly in ``reason``.
            reason = row.get("decision_reason", row.get("reason"))
            if (
                not isinstance(decision_site, str)
                or not isinstance(decision, str)
                or type(iteration) is not int
                or type(candidate_chars) is not int
                or not isinstance(candidate_sha256_16, str)
                or not isinstance(candidate_id, str)
                or not isinstance(reason, str)
            ):
                raise ValueError("malformed participation scalar fields")
            record = ControlParticipation(
                schema=row["schema"],
                feature_id=control_ref["feature_id"],
                role=control_ref["role"],
                decision_site=decision_site,
                decision=decision,
                iteration=iteration,
                candidate_chars=candidate_chars,
                candidate_sha256_16=candidate_sha256_16,
                fact_class=row.get("fact_class"),
                candidate_id=candidate_id,
                reason=reason,
                temporal_relation=row.get(
                    "temporal_relation", CONTROL_PRECEDES_DELIVERY,
                ),
                related_delivery_iteration=row.get("related_delivery_iteration"),
                observation_binding=observation_binding,
            )
            if record.role == "mediator" and record.decision == "APPLIED" and (
                not record.candidate_id
                or record.candidate_chars <= 0
                or not record.candidate_sha256_16
            ):
                raise ValueError("mediator candidate identity incomplete")
        except (KeyError, TypeError, ValueError):
            invalid_rows.append(index)
            continue

        item = {
            "row_index": index,
            "feature_id": record.feature_id,
            "role": record.role,
            "decision_site": record.decision_site,
            "decision": record.decision,
            "iteration": record.iteration,
            "candidate_chars": record.candidate_chars,
            "candidate_sha256_16": record.candidate_sha256_16,
            "fact_class": record.fact_class,
            "candidate_id": record.candidate_id,
            "temporal_relation": record.temporal_relation,
            "related_delivery_iteration": record.related_delivery_iteration,
            "observation_binding": (
                observation_binding_to_dict(record.observation_binding)
                if record.observation_binding is not None else None
            ),
        }
        records[record.feature_id].append(item)
        if (
            record.role != "mediator"
            or record.decision not in {"APPLIED", "NO_EFFECT"}
            or not record.candidate_id
            or record.candidate_chars <= 0
            or not record.candidate_sha256_16
        ):
            continue

        if record.temporal_relation == RECEIPT_FOLLOWS_DELIVERY:
            delivery_indices: "list[int]" = list(range(index - 1, -1, -1))
        else:
            # CONTROL_PRECEDES_DELIVERY: the seam may FLUSH the sealed delivery row
            # BEFORE the control row even though the control decision came first
            # temporally, so search forward (normal) THEN backward (flushed) — the
            # in-loop identity match still gates which row actually binds, so this
            # only widens WHERE a real delivery is found, never WHICH one qualifies.
            delivery_indices = [
                *range(index + 1, len(rows)),
                *range(index - 1, -1, -1),
            ]
        for delivery_index in delivery_indices:
            delivery = rows[delivery_index]
            if delivery.get("outcome") != "delivered":
                continue
            if delivery.get("lineage_schema") != "gt.feature_lineage.v1":
                continue
            if delivery.get("fact_class") != record.fact_class:
                continue
            runtime_producer = delivery.get("runtime_producer_id")
            registered_producer = delivery.get("registered_producer_id")
            evidence_type = delivery.get("evidence_type")
            if not isinstance(evidence_type, str):
                continue
            fact_registration = registration_for(evidence_type)
            if (
                delivery.get("producer_registration_match") is not True
                or not isinstance(runtime_producer, str)
                or not runtime_producer
                or not isinstance(registered_producer, str)
                or not registered_producer
                or fact_registration is None
                or fact_registration.producer != registered_producer
                or fact_registration.fact_class != record.fact_class
                or not producer_matches(evidence_type, runtime_producer)
            ):
                continue
            try:
                delivery_binding = observation_binding_from_dict(
                    delivery.get("observation_binding")
                )
            except (TypeError, ValueError):
                continue
            if (
                (record.observation_binding is None) != (delivery_binding is None)
                or (
                    record.observation_binding is not None
                    and record.observation_binding != delivery_binding
                )
            ):
                continue
            if (
                delivery.get("chars_delivered") != record.candidate_chars
                or delivery.get("content_sha256_16") != record.candidate_sha256_16
            ):
                continue
            downstream_id = delivery.get("candidate_id")
            if downstream_id != record.candidate_id:
                continue
            delivery_iteration = delivery.get("iteration")
            if record.temporal_relation == RECEIPT_FOLLOWS_DELIVERY:
                if delivery_iteration != record.related_delivery_iteration:
                    continue
            elif (
                type(delivery_iteration) is not int
                or delivery_iteration < record.iteration
            ):
                continue
            receipt_level = 0
            referenced_message_index = None
            acted_message_index = None
            for entry in entries:
                if not isinstance(entry, dict) or entry.get("source") != "trajectory":
                    continue
                entry_chars = entry.get("ledger_chars", entry.get("chars"))
                if (
                    entry.get("content_sha256_16") == record.candidate_sha256_16
                    and entry_chars == record.candidate_chars
                    and entry.get("ledger_layer") == delivery.get("layer")
                    and entry.get("joined") is True
                    and entry.get("join_method") == "seal"
                ):
                    try:
                        entry_binding = observation_binding_from_dict(
                            entry.get("observation_binding")
                        )
                    except (TypeError, ValueError):
                        continue
                    if (
                        (record.observation_binding is None)
                        != (entry_binding is None)
                        or (
                            record.observation_binding is not None
                            and record.observation_binding != entry_binding
                        )
                    ):
                        continue
                    candidate_receipt = int(entry.get("receipt") or 0)
                    if candidate_receipt >= receipt_level:
                        receipt_level = candidate_receipt
                        referenced_message_index = entry.get("referenced_msg_index")
                        acted_message_index = entry.get("acted_msg_index")
            observation_message_index = observation_join.get(delivery_index)
            if record.temporal_relation == RECEIPT_FOLLOWS_DELIVERY:
                if observation_message_index is None:
                    continue
                later_receipt_message = (
                    acted_message_index
                    if receipt_level >= 3 else referenced_message_index
                )
                if record.decision == "APPLIED" and not (
                    receipt_level >= 2
                    and type(later_receipt_message) is int
                    and later_receipt_message > observation_message_index
                ):
                    continue
                if record.decision == "NO_EFFECT" and receipt_level != 1:
                    continue
            joins[record.feature_id].append({
                **item,
                "delivery_row_index": delivery_index,
                "delivery_iteration": delivery_iteration,
                "delivery_layer": delivery.get("layer"),
                "observation_message_index": observation_message_index,
                "observation_joined": observation_message_index is not None,
                "receipt_level": receipt_level,
                "referenced_message_index": referenced_message_index,
                "acted_message_index": acted_message_index,
            })
            break

    if brief_payload is not None:
        block_receipt_fn = None
        try:
            from acq_provenance import (
                _block_receipt as block_receipt_fn,
                _producer_delivery_home,
                _validated_blocks,
            )

            if brief_payload.get("schema") != "gt.brief_result.v1":
                raise ValueError("unsupported brief result schema")
            brief = brief_payload.get("brief_text")
            metrics = brief_payload.get("metrics")
            if (
                not isinstance(brief, str)
                or not brief
                or brief_payload.get("brief_sha256")
                != hashlib.sha256(brief.encode("utf-8", "surrogatepass")).hexdigest()
                or not isinstance(metrics, dict)
            ):
                raise ValueError("malformed whole brief identity")
            raw_receipts = metrics.get("block_receipts")
            blocks_by_id = _validated_blocks(brief, raw_receipts)
            if not isinstance(raw_receipts, list):
                raise ValueError("block_receipts must be a list")
            blocks: dict[str, dict[str, Any]] = {}
            for receipt in raw_receipts:
                if not isinstance(receipt, dict):
                    raise ValueError("block receipt must be an object")
                candidate_id = receipt.get("candidate_id")
                block_id = receipt.get("block_id")
                if (
                    not isinstance(candidate_id, str)
                    or not candidate_id
                    or candidate_id in blocks
                    or block_id not in blocks_by_id
                ):
                    raise ValueError("block receipt candidate identity is not exact and unique")
                blocks[candidate_id] = blocks_by_id[block_id]
            raw_controls = metrics.get("control_participation", [])
            if not isinstance(raw_controls, list):
                raise ValueError("control_participation must be a list")
            parent_home = _producer_delivery_home((brief,), entries, messages)
        except (ImportError, TypeError, ValueError):
            invalid_brief_rows.append(-1)
            raw_controls = []
            blocks = {}
            parent_home = None

        for brief_index, raw in enumerate(raw_controls):
            try:
                if not isinstance(raw, dict):
                    raise ValueError("control row must be an object")
                control_ref = raw.get("control_ref")
                if not isinstance(control_ref, dict) or set(control_ref) != {
                    "category", "feature_id", "role",
                } or control_ref.get("category") != "CAP":
                    raise ValueError("malformed control_ref")
                if raw.get("schema") != CONTROL_PARTICIPATION_SCHEMA or raw.get("decision") == "ERROR":
                    raise ValueError("failed participation row")
                decision_site = raw.get("decision_site")
                decision = raw.get("decision")
                iteration = raw.get("iteration")
                candidate_chars = raw.get("candidate_chars")
                candidate_sha256_16 = raw.get("candidate_sha256_16")
                candidate_id = raw.get("candidate_id")
                reason = raw.get("reason")
                if (
                    not isinstance(decision_site, str)
                    or not isinstance(decision, str)
                    or type(iteration) is not int
                    or type(candidate_chars) is not int
                    or not isinstance(candidate_sha256_16, str)
                    or not isinstance(candidate_id, str)
                    or not isinstance(reason, str)
                ):
                    raise ValueError("malformed brief participation scalar fields")
                record = ControlParticipation(
                    schema=raw["schema"],
                    feature_id=control_ref["feature_id"],
                    role=control_ref["role"],
                    decision_site=decision_site,
                    decision=decision,
                    iteration=iteration,
                    candidate_chars=candidate_chars,
                    candidate_sha256_16=candidate_sha256_16,
                    fact_class=raw.get("fact_class"),
                    candidate_id=candidate_id,
                    reason=reason,
                    temporal_relation=raw.get(
                        "temporal_relation", CONTROL_PRECEDES_DELIVERY,
                    ),
                    related_delivery_iteration=raw.get(
                        "related_delivery_iteration"
                    ),
                )
            except (KeyError, TypeError, ValueError):
                invalid_brief_rows.append(brief_index)
                continue

            item = {
                "row_index": None,
                "brief_row_index": brief_index,
                "source_artifact": "brief_result.json",
                "feature_id": record.feature_id,
                "role": record.role,
                "decision_site": record.decision_site,
                "decision": record.decision,
                "iteration": record.iteration,
                "candidate_chars": record.candidate_chars,
                "candidate_sha256_16": record.candidate_sha256_16,
                "fact_class": record.fact_class,
                "candidate_id": record.candidate_id,
                "temporal_relation": record.temporal_relation,
                "related_delivery_iteration": record.related_delivery_iteration,
                "observation_binding": None,
            }
            records[record.feature_id].append(item)
            if (
                record.role != "mediator"
                or record.decision not in {"APPLIED", "NO_EFFECT"}
                or record.candidate_chars <= 0
                or not record.candidate_sha256_16
            ):
                continue
            block = blocks.get(record.candidate_id)
            if (
                block is None
                or block.get("fact_class") != record.fact_class
                or block.get("chars") != record.candidate_chars
                or str(block.get("sha256") or "")[:16] != record.candidate_sha256_16
            ):
                invalid_brief_rows.append(brief_index)
                continue
            if parent_home is None or not callable(block_receipt_fn):
                continue
            candidate_path = (
                record.candidate_id.split(":", 1)[1]
                if record.candidate_id.startswith("localization:") else ""
            )
            block_receipt = block_receipt_fn(
                block, candidate_path, messages, parent_home["msg_index"],
            )
            joins[record.feature_id].append({
                **item,
                "delivery_row_index": None,
                "delivery_layer": parent_home["ledger_layer"],
                "parent_brief_content_sha256_16": parent_home["content_sha256_16"],
                "observation_message_index": parent_home["msg_index"],
                "observation_joined": True,
                "receipt_level": int(block_receipt["level"] or 1),
                "referenced_message_index": block_receipt["referenced_message_index"],
                "acted_message_index": block_receipt["acted_message_index"],
            })
    correctness = _control_declared_effect_correctness(rows, records, joins)
    return {
        "records": dict(records),
        "joins": dict(joins),
        "correctness": correctness,
        "invalid_rows": sorted(set(invalid_rows)),
        "invalid_brief_rows": sorted(set(invalid_brief_rows)),
        "valid": not invalid_rows and not invalid_brief_rows,
    }


# G1 (2026-07-18): the declared polarity authority for eligibility-control decisions.
# Keyed by (feature_id, participation_decision); values name the refereeing effect on the
# record's identified candidate. Sourced from the runtime's own decision vocabulary (seam
# writers), empirically verified against real run ledgers. A combination absent here is
# deliberately ungraded (correct-or-quiet) — e.g. GT_SS_ELIGIBILITY APPLIED=widened_prefix is
# an enabling ACTION on the search prefix, not a candidate ruling, and GT_D7_RELATEDNESS rows
# carry no candidate identity. Extend ONLY with runtime-verified semantics.
_ELIGIBILITY_DECISION_POLARITY: dict[tuple[str, str], str] = {
    ("GT_SS_DEDUP2", "APPLIED"): "blocks",       # semantic_duplicate
    ("GT_SS_DEDUP2", "NO_EFFECT"): "permits",    # novel_entity_set
    ("GT_SS_NOVELTY", "APPLIED"): "blocks",      # step_behind
    ("GT_SS_NOVELTY", "NO_EFFECT"): "permits",   # novel_entity
    ("GT_SS_SHADOW", "APPLIED"): "blocks",       # holdout (bytes withheld from the model)
    ("GT_SS_SHADOW", "NO_EFFECT"): "permits",    # deliver
    ("GT_SS_LATE_DROP", "APPLIED"): "blocks",    # late
    ("GT_SS_LATE_DROP", "NO_EFFECT"): "permits", # on_time
}


def _exact_control_candidate_identity(
    record: dict[str, Any],
) -> tuple[Any, str, str, str, int, int] | None:
    """Return the exact policy-observation candidate identity or fail closed."""
    candidate_id = record.get("candidate_id")
    fact_class = record.get("fact_class")
    seal = record.get("candidate_sha256_16")
    chars = record.get("candidate_chars")
    iteration = record.get("iteration")
    if (
        not isinstance(candidate_id, str)
        or not candidate_id
        or not isinstance(fact_class, str)
        or not fact_class
        or not is_registered(fact_class)
        or not isinstance(seal, str)
        or not seal
        or type(chars) is not int
        or chars <= 0
        or type(iteration) is not int
        or iteration < 0
    ):
        return None
    try:
        binding = observation_binding_from_dict(record.get("observation_binding"))
    except (TypeError, ValueError):
        return None
    if (
        binding is None
        or validate_observation_binding(
            binding, expected_candidate_id=candidate_id,
        )
    ):
        return None
    return binding, fact_class, candidate_id, seal, chars, iteration


def _control_declared_effect_correctness(
    rows: list[dict],
    records: dict[str, list[dict[str, Any]]],
    joins: dict[str, list[dict[str, Any]]],
) -> dict[str, bool | None]:
    """PRODUCT DECISION 1 (P5, B-TERM 2026-07-16): grade whether each control's DECLARED effect
    matched the downstream ledger reality. Deterministic · producer-owned · re-verifiable · can
    FAIL — and CORRECT-OR-QUIET: it emits a True/False verdict ONLY where the declaration has an
    UNAMBIGUOUS downstream meaning, and ``None`` (honest UNMEASURED) wherever it does not.

    Only two declarations have a role/polarity-independent downstream truth, so only these grade:

    * MEDIATOR APPLIED → CORRECT iff the record EXACT-JOINED a downstream delivered row. The join
      (``_control_participation_evidence``) already encodes the full producer/seal/candidate-id
      contract, so a joined APPLIED is provably delivered. A mediator that DECLARES APPLIED and did
      NOT join = INCORRECT (``False``, exposed) — the definitive lie the task names.
    * MEDIATOR SUPPRESSED → CORRECT iff NO delivered row carries the exact suppressed candidate
      bytes (seal+chars). A SUPPRESSED candidate whose exact bytes WERE delivered = INCORRECT
      (``False``) — the suppression is contradicted by the ledger.

    Everything else → ``None`` (UNMEASURED), deliberately, because a delivery-presence heuristic is
    UNSOUND for it:
      - MEDIATOR NO_EFFECT that JOINED → ``True`` (the join confirmed the delivered-but-ineffective
        reality the receipt-follows / pre-delivery contract requires); NOT joined → ``None``
        (a "no downstream effect" claim we cannot refute, not necessarily a lie).
      - Any record with NO candidate identity (a zero-candidate no-op) → skipped (unverifiable).

    G1 (SS-REFEREE gate-4 ``effect_enforced``, 2026-07-18 — upgrades the P5 "polarity is not
    machine-derivable" abstention): eligibility polarity IS machine-derivable through the explicit
    declared authority ``_ELIGIBILITY_DECISION_POLARITY`` keyed by ``(feature_id, decision)`` and
    aligned with the runtime's own decision vocabulary (verified against real run ledgers:
    dedup2 APPLIED=semantic_duplicate, novelty APPLIED=step_behind, shadow APPLIED=holdout,
    late_drop APPLIED=late all BLOCK an identified candidate; their NO_EFFECT forms PERMIT it).
    Grading, only where the record carries exact candidate identity:
      - BLOCKS → CORRECT iff the exact suppressed/withheld bytes were NOT delivered
        (``not _carried``): a blocked candidate whose bytes shipped anyway is the refereeing lie.
      - PERMITS → confirmed-consistent (``True``) only when the bytes WERE delivered; a permitted
        candidate that never ships is NOT a lie (the arbiter may out-rank it) → contributes
        nothing. This asymmetry is the semantic difference from mediator APPLIED (which must
        join) and is exactly why eligibility must never be relabeled as mediation.
      - A ``(feature_id, decision)`` absent from the authority stays ungraded (correct-or-quiet
        preserved for enabling-action rules like widened_prefix whose effect is not a candidate).

    Member rollup: ≥1 ``False`` → ``False`` (exposed); else ≥1 ``True`` → ``True``; else ``None``.
    """
    delivered_seals: set[tuple[str, int]] = set()
    for row in rows:
        if not isinstance(row, dict) or row.get("outcome") != "delivered":
            continue
        seal = row.get("content_sha256_16")
        chars = row.get("chars_delivered")
        if isinstance(seal, str) and seal and isinstance(chars, int) and not isinstance(chars, bool):
            delivered_seals.add((seal, chars))

    def _has_identity(rec: dict[str, Any]) -> bool:
        seal = rec.get("candidate_sha256_16")
        chars = rec.get("candidate_chars")
        return bool(seal) and isinstance(chars, int) and not isinstance(chars, bool) and chars > 0

    def _carried(rec: dict[str, Any]) -> bool:
        seal = rec.get("candidate_sha256_16")
        chars = rec.get("candidate_chars")
        if (
            not isinstance(seal, str)
            or not seal
            or type(chars) is not int
            or chars <= 0
        ):
            return False
        return (seal, chars) in delivered_seals

    # NO-GO defect 4 (2026-07-18) — the opportunity/control TRANSACTION BOUNDARY. The seam
    # evaluates eligibility rulings during pure batch preparation, BEFORE the formatter
    # commits the observation; opportunity rows are persisted only after formatter success.
    # A ruling whose opportunity was never committed must not be graded: a BLOCKS verdict
    # would otherwise earn vacuous True credit ("bytes not delivered") when in reality the
    # whole observation aborted. Grade an eligibility ruling ONLY when its exact validated
    # observation binding matches a committed feature.opportunity row.
    committed_opportunity_bindings: list[Any] = []
    for row in rows:
        if not isinstance(row, dict) or row.get("layer") != "feature.opportunity":
            continue
        try:
            opportunity_binding = observation_binding_from_dict(
                row.get("observation_binding")
            )
        except (TypeError, ValueError):
            continue
        if opportunity_binding is None or validate_observation_binding(
            opportunity_binding
        ):
            continue
        committed_opportunity_bindings.append(opportunity_binding)

    def _opportunity_committed(binding: Any) -> bool:
        return any(
            committed == binding for committed in committed_opportunity_bindings
        )

    def _eligibility_carried(
        identity: tuple[Any, str, str, str, int, int],
    ) -> bool:
        binding, fact_class, candidate_id, seal, chars, control_iteration = identity
        for row in rows:
            if (
                not isinstance(row, dict)
                or row.get("outcome") != "delivered"
                or row.get("lineage_schema") != "gt.feature_lineage.v1"
                or row.get("fact_class") != fact_class
                or row.get("candidate_id") != candidate_id
                or row.get("content_sha256_16") != seal
                or row.get("chars_delivered") != chars
                or type(row.get("iteration")) is not int
                or row["iteration"] < control_iteration
            ):
                continue
            try:
                delivery_binding = observation_binding_from_dict(
                    row.get("observation_binding")
                )
            except (TypeError, ValueError):
                continue
            if (
                delivery_binding is not None
                and not validate_observation_binding(
                    delivery_binding, expected_candidate_id=candidate_id,
                )
                and delivery_binding == binding
            ):
                return True
        return False

    out: dict[str, bool | None] = {}
    for feature_id, recs in records.items():
        joined_keys = {
            (join.get("row_index"), join.get("brief_row_index"))
            for join in joins.get(feature_id, [])
        }
        verdicts: list[bool] = []
        for rec in recs:
            decision = rec.get("decision")
            if not _has_identity(rec):
                continue
            role = rec.get("role")
            if role == "eligibility":
                # G1: grade only through the declared polarity authority (correct-or-quiet).
                polarity = _ELIGIBILITY_DECISION_POLARITY.get(
                    (str(rec.get("feature_id") or ""), str(decision or ""))
                )
                identity = _exact_control_candidate_identity(rec)
                if identity is None:
                    continue
                if not _opportunity_committed(identity[0]):
                    # NO-GO defect 4: no committed opportunity row for this exact binding —
                    # the observation may have aborted before commit; a verdict here would
                    # be vacuous credit. Ungraded (correct-or-quiet), never a pass.
                    continue
                carried = _eligibility_carried(identity)
                if polarity == "blocks":
                    verdicts.append(not carried)        # blocked candidate shipped → False (lie)
                elif polarity == "permits" and carried:
                    verdicts.append(True)               # permitted and shipped → confirmed
                # permits-but-not-shipped (arbiter may out-rank) and unknown combos → nothing.
                continue
            if role != "mediator":
                continue
            rec_key = (rec.get("row_index"), rec.get("brief_row_index"))
            if decision == "APPLIED":
                verdicts.append(rec_key in joined_keys)          # joined → True, else False (lie)
            elif decision == "NO_EFFECT":
                if rec_key in joined_keys:
                    verdicts.append(True)                        # delivered-but-ineffective, confirmed
                # not joined → unverifiable "no effect" claim → contributes nothing (None-leaning)
            elif decision == "SUPPRESSED":
                verdicts.append(not _carried(rec))               # carried → False (suppression is a lie)
            # any other decision (ERROR — already filtered upstream) is unverifiable.
        out[feature_id] = (all(verdicts) if verdicts else None)
    return out


# ---------------------------------------------------------------------------
# Fact-class lifecycle — the substantive per-class grade.
# ---------------------------------------------------------------------------

def oracle_gate_losses(oracle_rows: list[dict]) -> dict[str, Any] | None:
    """Summarise the ≤1/turn gate's loss ledger for this task — the oracle-era denominator.

    WHY (task #34). Under GT_ORACLE_ROUTE the layers are candidate PRODUCERS and the gate
    emits at most one block per turn, so most features MUST be silent most turns — "dark"
    is not a defect statement unless it separates *never produced* from *produced and
    outranked*. The gt.oracle_event.v2 rows carry exactly that split (reasons:
    delivered / irrelevant / below_floor / outranked), and as of 2026-07-29 the writer
    stamps `task` + `iteration`, making them attributable.

    NAMESPACE LAW: `kind` is the GATE-KIND namespace. It is surfaced VERBATIM as a
    diagnostic and is deliberately NOT mapped onto fact classes or the LAYER namespace —
    two prior headline findings died on exactly that name-diff. Rows without an
    `iteration` (pre-2026-07-29 artifacts) are counted but their iterations are not
    fabricated.
    """
    if not oracle_rows:
        return None
    losses: dict[str, dict[str, Any]] = {}
    emissions = 0
    for row in oracle_rows:
        if not isinstance(row, dict) or row.get("schema") != "gt.oracle_event.v2":
            continue
        it = row.get("iteration")
        if row.get("emitted"):
            emissions += 1
        for s in row.get("suppressed") or []:
            if not isinstance(s, dict):
                continue
            kind = str(s.get("kind") or "")
            reason = str(s.get("reason") or "")
            if not kind or not reason:
                continue
            slot = losses.setdefault(kind, {"total": 0, "by_reason": {}, "iterations": []})
            slot["total"] += 1
            slot["by_reason"][reason] = int(slot["by_reason"].get(reason, 0)) + 1
            if isinstance(it, int) and len(slot["iterations"]) < 50:
                slot["iterations"].append(it)
    if not losses and not emissions:
        return None
    return {
        "schema": "gt.oracle_gate_losses.v1",
        "namespace": "gate_kind",  # NEVER name-diff against layer/fact-class names
        "emissions": emissions,
        "losses_by_kind": {k: losses[k] for k in sorted(losses)},
    }


def _fact_class_eligible(fc: str, timeline: list[dict], ledger_by_fc: dict, oracle_rows: list[dict],
                         has_submission: bool) -> tuple[bool | None, str]:
    """Did the run create the condition where this fact class COULD fire?

    Returns True / False / **None**. ``None`` means the offline artifacts carry no
    signal either way — an honest UNMEASURED, never a default.  The obligations
    class is the reason the third state exists: this predicate previously read
    ``bool(oracle_rows) or True``, which is unconditionally True, so obligations was
    graded eligible on EVERY task and — via the ``eligible and produced == 0`` arm
    below — earned a manufactured ``correct_abstain`` on every task where the
    producer said nothing.  It could not be graded dark by construction.
    """
    steps = [e for e in timeline if e["role"] == "assistant"]
    any_edit = any(e.get("is_edit") for e in steps)
    any_test = any(e.get("is_test") for e in steps)
    any_search = any(e.get("is_search") for e in steps)
    any_view = any(e.get("viewed_file") for e in steps)
    if fc == "obligations":
        # Eligibility here is "an obligations PLAN existed", which is independent of
        # whether the producer fired.  The seam now emits a one-shot host-side marker at
        # PLAN-LOAD time (layer ``obligation.plan``, reason ``obligation_plan_loaded`` —
        # task #35), on a layer deliberately absent from every layer→fact-class map so
        # it can never count as ``produced``.  Marker present → eligible.  Marker ABSENT
        # stays UNMEASURED, never False: a pre-marker artifact and a genuinely plan-less
        # task are indistinguishable from absence, and ``verdict_for`` reads
        # eligible-False as "correctly silent, never CUT" — collapsing the unknown to
        # False would manufacture that pass.  (The obligations ATTESTATION cannot serve
        # here: it is written only after a delivery, so keying on it collapses
        # eligibility into production.  The oracle telemetry cannot either: its ``kind``
        # is the GATE-KIND namespace, not the LAYER namespace.)
        if ledger_by_fc.get("__obligation_plan_loaded__"):
            return (True, "obligation.plan marker: a plan was loaded this task")
        return (None, "no obligations-plan-load marker (pre-marker artifact or no plan)")
    if fc == "localization":
        return (any_search or any_view, "a which-file-to-open decision was open")
    if fc == "def_partition":
        return (any_search, "a search returned candidate defs")
    if fc == "caller_contract":
        return (any_edit, "a function was edited (callers may need preserving)")
    if fc == "syntax_result":
        return (any_edit, "an edit exists to syntax-check")
    if fc == "signature_delta":
        return (any_edit, "an edit may have changed a signature")
    if fc == "covering_red":
        return (any_test, "a test was run")
    if fc == "submit_refusal":
        return (has_submission, "a submission/exit was reached")
    if fc == "cochange_prior":
        return (any_edit or any_view, "a first view/edit happened (companion decision)")
    if fc == "newfile_precedent":
        # only eligible if the run created a new file OR a search returned nothing.
        created = any(e.get("is_edit") and e.get("edited_file") for e in steps)
        return (created and ledger_by_fc.get("newfile_precedent", {}).get("produced", 0) > 0,
                "a new-file/missing-role destination decision was open")
    if fc == "recovery":
        looped = ledger_by_fc.get("recovery", {}).get("produced", 0) > 0
        return (looped, "a stuck/loop/failure observation occurred")
    return (False, "no eligibility rule")


def fact_class_lifecycle(
    fc: str,
    *,
    timeline: list[dict],
    ledger_by_fc: dict[str, dict],
    consumption_by_fc: dict[str, dict],
    state_by_fc: dict[str, tuple],
    oracle_rows: list[dict],
    has_submission: bool,
    baseline_status: str,
    registry,
    ledger_artifact: str,
    traj_artifact: str,
    native_visible: int = 0,
    native_renderer_valid: bool | None = None,
    unjoined_receipts: int | None = None,
) -> dict[str, Any]:
    """Assemble the universal lifecycle for one fact class from the offline evidence."""
    lc: dict[str, Any] = new_lifecycle("not_applicable_to_this_class")
    b = ledger_by_fc.get(fc, {})
    cons = consumption_by_fc.get(fc, {})

    eligible, why = _fact_class_eligible(fc, timeline, ledger_by_fc, oracle_rows, has_submission)
    if eligible is None:
        # No signal either way. Emitting False here would swap one manufactured
        # verdict for its mirror image; the honest report is that we cannot tell.
        lc["eligible"] = unmeasured(why, source_artifact=traj_artifact)
        lc["not_eligible"] = unmeasured(why, source_artifact=traj_artifact)
    else:
        lc["eligible"] = measured(bool(eligible), source_artifact=traj_artifact)
        lc["not_eligible"] = measured(not bool(eligible), source_artifact=traj_artifact)

    produced = int(b.get("produced", 0))
    delivered = int(b.get("delivered", 0))
    allowed = int(b.get("allowed", 0))
    lc["produced"] = measured(produced > 0, source_artifact=ledger_artifact)
    # correct_abstain: (a) eligible but the producer correctly stayed silent (nothing
    # produced), OR (b) the producer RAN and correctly ALLOWED (a clean submit gate) — it
    # produced no refusal because none was warranted. Both are correct silence, NOT dark.
    if eligible is True and produced == 0 and allowed == 0:
        lc["correct_abstain"] = measured(True, source_artifact=ledger_artifact)
    elif delivered == 0 and allowed > 0:
        # an allow row alone proves the gate RAN and correctly produced nothing —
        # `produced` no longer counts allow rows (they are correct silence, not facts),
        # so this arm keys on `allowed`, not on manufactured production.
        lc["correct_abstain"] = measured(True, source_artifact=ledger_artifact)
    elif produced > 0:
        lc["correct_abstain"] = measured(False, source_artifact=ledger_artifact)

    # truth/authority: the runtime ledger does not carry a per-row truth proof; the seam
    # only emits validated facts (envelope validation upstream). We can assert the delivered
    # bytes exist + the boundary matches the registry, but per-payload truth is UNMEASURED
    # without the graph cross-check → keep honest.
    lc["delivered"] = measured(delivered > 0, source_artifact=ledger_artifact,
                               source_messages=[])
    if delivered > 0:
        lc["truth_valid"] = unmeasured("no per-row payload↔graph cross-check in ledger",
                                       source_artifact=ledger_artifact)
        lc["authority_valid"] = unmeasured("no per-row tier/confidence in ledger",
                                           source_artifact=ledger_artifact)
        # DEFECT-5 (run #2): native_valid is an EXACT registry-renderer audit, never a
        # fabricated True on mere delivery. It is MEASURED only when the delivered rows
        # carry a render identity to audit (renderer_id ↔ required_renderer); a
        # render-identity-less delivery leaves the honest UNMEASURED default.
        if isinstance(native_renderer_valid, bool):
            lc["native_valid"] = measured(native_renderer_valid, source_artifact=ledger_artifact)
        lc["expired_late"] = measured(int(b.get("expired_late", 0)) > 0, source_artifact=ledger_artifact)
        lc["stale"] = measured(int(b.get("stale", 0)) > 0, source_artifact=ledger_artifact)
        lc["dose_tokens"] = measured(round(int(b.get("delivered_chars", 0)) / 4.0, 8),
                                     source_artifact=ledger_artifact)
    elif eligible is True and produced > 0:
        lc["delivered"] = measured(False, source_artifact=ledger_artifact)

    # arbitration
    cand = int(b.get("arbiter_candidates", 0))
    lost = int(b.get("arbiter_lost", 0))
    if cand > 0:
        lc["entered_arbiter"] = measured(True, source_artifact=ledger_artifact)
        lc["arbiter_lost"] = measured(lost > 0, source_artifact=ledger_artifact)
        lc["arbiter_won"] = measured(delivered > 0, source_artifact=ledger_artifact)
    else:
        lc["entered_arbiter"] = measured(False, source_artifact=ledger_artifact)

    # consumption receipts (W1 v2). Native (gateway.*) tag-free classes are not in the
    # tag-based per_class → their receipt is host-attested only (UNMEASURED model-receipt).
    if cons:
        lvl = int(cons.get("max_level", 0))
        lc["receipt_level"] = measured(lvl, source_artifact=traj_artifact)
        lc["reacquired"] = measured(lvl < 3 and delivered > 0, source_artifact=traj_artifact)
        lc["redundant"] = measured(False, source_artifact=traj_artifact)
    elif delivered > 0 and native_visible > 0:
        # native (tag-free) delivery CONFIRMED model-visible via the content seal (defect-2
        # seal-join). Level 1 = present in the observation stream; higher levels still need
        # model-authored action attribution, which native facts lack a tag to anchor.
        lc["receipt_level"] = measured(1, source_artifact=traj_artifact)
        lc["reacquired"] = unmeasured("native action-attribution needs in-seam anchor")
    elif delivered > 0:
        lc["receipt_level"] = unmeasured(
            "native/tag-free delivery: host-attested only; needs seal-join to observation "
            "bytes (in-seam content_sha256_16 not present in this ledger)",
            source_artifact=ledger_artifact)

    # ── INERT vs UNMEASURED — the C15 namespace split (2026-07-28) ────────────────
    # THE DEFECT this replaces: ``inert = measured(delivered > 0 and lvl < 2)``. ``delivered``
    # counts EVERY delivered runtime-ledger row for the class; ``lvl`` is the max receipt over
    # ONLY the rows that could be JOINED to a model-visible observation. Comparing across
    # those two namespaces reported a class with 1-of-3 rows joined at level 1 as
    # ``inert = MEASURED True`` — "GT delivered something that did nothing" — when the truth
    # for the other 2 was "we could not measure what it did". It was also DISCONTINUOUS: with
    # ZERO joined rows ``cons`` is empty and the field honestly stayed UNMEASURED, so LESS
    # evidence produced a MORE confident verdict.
    #
    # The split, following ``acquired_*``/``delivered_*`` (commit 8f60643f4):
    #   inert_receipt_joined   — deliveries with a graded receipt that showed no reference
    #   inert_receipt_unjoined — deliveries with no model-visible receipt to grade (holes)
    #   inert                  — the class-level boolean, ASYMMETRIC (see below)
    # As in C15 the UNMEASURED verdict is decided by EVIDENCE (a counted ``ledger_only``
    # entry from the consumption ledger), never by reading a flag.
    #
    # THE ASYMMETRY, and why it is not a hedge: "this class did NOTHING" is a universal claim
    # over every delivery, so ONE ungraded delivery defeats it. "this class did SOMETHING" is
    # existential — ONE joined receipt at level >= 2 FALSIFIES inertness outright, and the
    # holes are then irrelevant. So a positive reference still yields MEASURED False even with
    # holes; only the True verdict needs full coverage. Measured on run 30390877219: without
    # this asymmetry 4 provable ``inert=False`` cells were withdrawn to UNMEASURED for nothing.
    if delivered > 0:
        joined_deliveries = int(cons.get("delivered", 0)) if cons else 0
        joined_referenced = int(cons.get("referenced", 0)) if cons else 0
        lvl_all = int(cons.get("max_level", 0)) if cons else 0
        lc["inert_receipt_joined"] = measured(
            max(0, joined_deliveries - joined_referenced), source_artifact=traj_artifact)
        if unjoined_receipts is None:
            # fail-closed: a caller that did not supply receipt-join coverage gets no verdict.
            # A 0 default here would silently restore the exact false confidence removed above.
            lc["inert_receipt_unjoined"] = unmeasured(
                "receipt-join coverage not supplied by the caller",
                source_artifact=ledger_artifact)
            holes: int | None = None
        else:
            holes = max(0, int(unjoined_receipts))
            lc["inert_receipt_unjoined"] = measured(holes, source_artifact=ledger_artifact)

        if joined_referenced > 0:
            # existential falsification — at least one delivery was demonstrably referenced.
            lc["inert"] = measured(False, source_artifact=traj_artifact)
        elif holes is None:
            lc["inert"] = unmeasured(
                "receipt-join coverage unknown: cannot separate inert from unmeasured",
                source_artifact=ledger_artifact)
        elif holes:
            lc["inert"] = unmeasured(
                f"{holes} delivered row(s) of this class have no model-visible receipt "
                f"to grade ({joined_deliveries} joined); inert is not established",
                source_artifact=ledger_artifact)
        elif not cons:
            # zero holes AND zero graded receipts = the class has NO receipt evidence at
            # all, not a clean sweep of unreferenced deliveries. Reached by the
            # caller_contract CO-FACT, which credits a second FACT on a physical delivery
            # that joins under its OWN layer, so this class owns no consumption entry.
            lc["inert"] = unmeasured(
                "delivered rows carry no receipt-graded consumption entry for this class "
                "(co-fact / tag-free credit); inert is not established",
                source_artifact=ledger_artifact)
        else:
            lc["inert"] = measured(lvl_all < 2, source_artifact=traj_artifact)
    elif cons:
        # THE VACUOUS FORM of the same cross-namespace bug: the runtime ledger records ZERO
        # delivered rows for this class while the trajectory carries a graded receipt for it.
        # ``measured(delivered > 0 and lvl < 2)`` returned a confident MEASURED False here —
        # a disposition of delivered evidence, asserted where the delivery count says there
        # is none. 34 such cells on run 29714439700 (all ``obligations``, receipt_level 1).
        # The sources disagree; the field has no subject. Say so instead of defaulting.
        lc["inert"] = unmeasured(
            "runtime ledger records 0 delivered rows for this class while the trajectory "
            "carries a graded receipt: sources disagree, inert has no subject",
            source_artifact=ledger_artifact)

    # state change (the provable predicates)
    if fc in state_by_fc:
        s_elig, s_changed = state_by_fc[fc]
        if s_elig:
            lc["state_changed"] = measured(bool(s_changed), source_artifact=traj_artifact)
            lc["state_durable"] = (measured(bool(s_changed), source_artifact=traj_artifact)
                                   if s_changed else measured(False, source_artifact=traj_artifact))
        else:
            lc["state_changed"] = not_eligible("state predicate condition not created",
                                               source_artifact=traj_artifact)

    # harm: default false-measured; a stale/expired delivered-and-consumed fact is a harm
    # signal, but we only mark measured harm when the mechanism is present.
    harm = bool(delivered and (int(b.get("stale", 0)) or int(b.get("expired_late", 0))) and
                cons.get("max_level", 0) >= 3)
    lc["harmful"] = measured(harm, source_artifact=ledger_artifact)

    # efficiency: only meaningful with a matched baseline/holdout — and even then a
    # task-level delta is NOT attributable to a single fact class without a per-class
    # holdout (E10). Keep steps/tokens_saved UNMEASURED with an explicit reason; the
    # task-level matched delta lives in gt_feature_effects.
    lc["baseline_or_holdout_status"] = measured(baseline_status, source_artifact=traj_artifact)
    if baseline_status == BASELINE_MATCHED:
        lc["steps_saved"] = unmeasured("per-class attribution needs shadow-holdout (E10); "
                                       "task-level matched delta is in gt_feature_effects")
        lc["tokens_saved"] = unmeasured("per-class attribution needs shadow-holdout (E10)")
    else:
        lc["steps_saved"] = unmeasured(f"baseline {baseline_status}")
        lc["tokens_saved"] = unmeasured(f"baseline {baseline_status}")

    # dispositions from the above
    if delivered > 0:
        lvl = int(cons.get("max_level", 0)) if cons else 0
        lc["accelerated"] = unmeasured("acceleration requires non-reacquisition + no prior "
                                       "evidence + matched baseline (G8)")
        lc["enabled_progress"] = unmeasured("enabling requires a relieved blocking constraint (G9)")
    lc["_why_eligible"] = why  # non-schema breadcrumb
    return lc


# ---------------------------------------------------------------------------
# Verdict — ADMIT/HOLD/CUT/FIX/DARK per gt-math admission standard.
# ---------------------------------------------------------------------------

def _val(metric: dict | None):
    return metric.get("value") if isinstance(metric, dict) else None


def _any3(metrics_iter) -> bool | None:
    """Three-valued OR over MetricValue dicts: True > UNMEASURED > False.

    Plain ``any()`` reads an UNMEASURED leaf as False, and ``verdict_for`` treats
    ``eligible is False`` as "correctly silent, never CUT" — so missing evidence
    silently became a PASS. Here an unknown that could have been True keeps the
    result unknown, and only an all-False scope reports False.
    """
    saw_unknown = False
    for metric in metrics_iter:
        value = _val(metric)
        if value:
            return True
        if value is None:
            saw_unknown = True
    return None if saw_unknown else False


def verdict_for(lifecycle: dict[str, Any], role: str) -> str:
    elig = _val(lifecycle.get("eligible"))
    produced = _val(lifecycle.get("produced"))
    delivered = _val(lifecycle.get("delivered"))
    receipt = _val(lifecycle.get("receipt_level"))
    state = _val(lifecycle.get("state_changed"))
    stale = _val(lifecycle.get("stale"))
    late = _val(lifecycle.get("expired_late"))
    harmful = _val(lifecycle.get("harmful"))
    correct_abstain = _val(lifecycle.get("correct_abstain"))
    if harmful:
        return VERDICT_CUT
    # a DELIVERED fact that was stale/late is a correctness/timing failure (FIX). A
    # SUPPRESSED loser being late is arbiter behaviour, not a delivered-fact failure —
    # which is why stale/late here reflect DELIVERED timing only (see classify_ledger).
    if delivered and (stale or late):
        return VERDICT_FIX
    if elig is False:
        return VERDICT_HOLD  # correctly silent / no opportunity — never CUT for not firing
    if elig and delivered is False and correct_abstain is True:
        return VERDICT_HOLD  # eligible + correctly abstained (silent or allowed)
    if elig and delivered is False and produced is False:
        return VERDICT_HOLD  # eligible + nothing to say
    if elig and delivered is False and produced:
        return VERDICT_DARK  # produced but never reached the model despite eligibility
    if state is True:
        return VERDICT_ADMIT  # predicted state changed
    if isinstance(receipt, int) and receipt >= 3:
        return VERDICT_HOLD   # acted, but state-change/efficacy not yet established
    if delivered:
        return VERDICT_HOLD   # delivered clean; behavioural evidence inconclusive
    return VERDICT_HOLD


_ACK_UNSET = object()  # B-cluster Gate 4: "no acknowledgment override supplied" sentinel.


def ss_gate_readiness(
    lifecycle: dict[str, Any],
    *,
    byte_proven: bool,
    leak_free: bool | None,
    dose_ok: bool | None,
    fair_probe: bool | None,
    live_witness: bool,
    chronological_time: bool | None = None,
    acknowledged: bool | None | object = _ACK_UNSET,
) -> dict[str, Any]:
    """Fail-closed projection of the seven SS-LIVE gates for one feature.

    ``None`` means the artifact cannot prove that gate. Offline fixture/replay
    evidence may populate individual gates, but the terminal bit additionally
    requires an explicitly identified live witness.

    B-cluster (Gate 4): ``acknowledged`` accepts the registry-specific acknowledgment verdict
    from :mod:`receipt_predicates` (True/False/None). When it is the ``_ACK_UNSET`` sentinel
    (the historical direct callers), the ``acknowledged`` gate falls back to the receipt-ladder
    value (``receipt_level >= 2``) only for legacy callers that omit the override. Once a
    registry/sidecar join is supplied, its ``True``/``False``/``None`` result is authoritative;
    an unmeasured strict join may not be promoted by generic telemetry.
    """
    delivered = _val(lifecycle.get("delivered"))
    truth = _val(lifecycle.get("truth_valid"))
    authority = _val(lifecycle.get("authority_valid"))
    stale = _val(lifecycle.get("stale"))
    late = _val(lifecycle.get("expired_late"))
    receipt = _val(lifecycle.get("receipt_level"))

    delivered_byte_proven = bool(delivered is True and byte_proven)
    if truth is False or authority is False:
        correct_info: bool | None = False
    elif truth is True and authority is True:
        correct_info = True
    else:
        correct_info = None

    if stale is True or late is True:
        correct_time: bool | None = False
    elif chronological_time is True or chronological_time is False:
        correct_time = chronological_time
    else:
        correct_time = None

    receipt_ack = receipt >= 2 if isinstance(receipt, int) else None
    # The generic ladder is a compatibility fallback only when no registry-specific authority
    # was supplied. A supplied None is explicitly UNMEASURED and must remain so.
    if acknowledged is _ACK_UNSET:
        acknowledged_gate: bool | None = receipt_ack
    elif acknowledged is None:
        acknowledged_gate = None
    else:
        acknowledged_gate = bool(acknowledged)
    gates: dict[str, bool | None] = {
        "delivered_byte_proven": delivered_byte_proven,
        "correct_info": correct_info,
        "correct_rl_adhered_time": correct_time,
        "acknowledged": acknowledged_gate,
        "leak_zero": leak_free if isinstance(leak_free, bool) else None,
        "dose_lte_one": dose_ok if isinstance(dose_ok, bool) else None,
        "fair_probe": fair_probe,
    }
    return _readiness_from_gates(gates, live_witness=live_witness)


_SS_GATE_NAMES = (
    "delivered_byte_proven",
    "correct_info",
    "correct_rl_adhered_time",
    "acknowledged",
    "leak_zero",
    "dose_lte_one",
    "fair_probe",
)

_MEASUREMENT_GATE_NAMES = (
    "artifact_valid",
    "metric_structure_valid",
    "precision_8dp",
    "formula_provenance",
    "denominator_provenance",
    "applicability_resolved",
    "task_coverage",
    "aggregate_coverage",
)

# PRODUCT DECISION 1 (P5, B-TERM 2026-07-16): the control terminal's COMPLETENESS gates are the
# THREE deterministic, producer-derived facts — the control's own runtime receipt, the exact
# mediated FACT id(s) it joined, and whether its DECLARED effect matched what the ledger shows
# downstream (mediation_correct, a REAL check that can FAIL). The causal fair-probe is NOT a
# completeness gate here: a randomized/paired causal probe is registered per FACT class (gate 7 on
# the FACT terminal, fair_probe_result.py), and a control's causal evidence RIDES the mediated
# FACT's probe rather than being an independent control-grain probe. It is reported as a separate
# enrichment field (``mediation_causal_fair_probe``) so ``infra_control_complete`` is REACHABLE
# (previously it was structurally unreachable: mediation_correct and the causal probe were both
# hardwired None for all 39/40 control CAPs).
_INFRA_CONTROL_GATE_NAMES = (
    "runtime_member_control_receipt",
    "mediated_fact_ids",
    "mediation_correct",
)
_SUPPORT_GATE_NAMES = (
    "supported_fact_delivery_join",
    "candidate_local_contribution",
    "source_contribution_correct",
    "timing_inherited_from_fact_delivery",
    "source_causal_fair_probe",
)
_INTERNAL_SUPPORT_GATE_NAMES = (
    "runtime_support_receipt",
    "supported_candidate_id",
    "downstream_decision_join",
    "support_correct",
    "support_causal_fair_probe",
)


def _readiness_from_gates(
    gates: dict[str, bool | None], *, live_witness: bool = False,
) -> dict[str, Any]:
    """Normalize one seven-gate projection without manufacturing live proof."""
    if tuple(gates) != _SS_GATE_NAMES:
        raise ValueError(
            "gt_feature_metrics: SS readiness gate order/schema drift; got "
            f"{list(gates)}"
        )
    blockers = [name for name, value in gates.items() if value is not True]
    if not live_witness:
        blockers.append("live_witness")
    return {
        "gates": gates,
        "live_witness": bool(live_witness),
        "ss_live": bool(live_witness and all(value is True for value in gates.values())),
        "blockers": blockers,
    }


def _typed_readiness(
    role: str,
    gates: dict[str, bool | None],
    *,
    gate_names: tuple[str, ...],
    live_witness: bool = False,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Normalize a non-delivery readiness contract without borrowing SS delivery gates."""
    if tuple(gates) != gate_names:
        raise ValueError(
            f"gt_feature_metrics: {role} readiness gate order/schema drift; got {list(gates)}"
        )
    blockers = [name for name, value in gates.items() if value is not True]
    if not live_witness:
        blockers.append("live_witness")
    complete = all(value is True for value in gates.values())
    out: dict[str, Any] = {
        "role": role,
        "gates": gates,
        "live_witness": bool(live_witness),
        "ss_live": bool(live_witness and complete),
        f"{role}_complete": complete,
        "blockers": blockers,
    }
    if extra:
        out.update(extra)
    return out


def _measurement_only_readiness(
    record: dict[str, Any] | None = None,
    *,
    aggregate_coverage: bool = False,
    live_witness: bool = False,
) -> dict[str, Any]:
    """Typed PERF terminal: measurement validity/coverage, never model delivery."""
    row = record or {}
    status = row.get("status")
    applicability_resolved = status in {
        "MEASURED", "NOT_APPLICABLE", "RIGHT_CENSORED",
    }
    task_scope = row.get("coverage_scope") == "task"
    artifact_valid = bool(
        applicability_resolved
        and row.get("artifact_schema_valid") is True
        and isinstance(row.get("source_artifact"), str)
        and bool(row.get("source_artifact"))
        and (task_scope or row.get("source") == "gt_run_metrics")
    )
    gates: dict[str, bool | None] = {
        "artifact_valid": artifact_valid,
        "metric_structure_valid": row.get("metric_structure_valid") is True,
        "precision_8dp": bool(
            row.get("precision_decimals") == 8
            and row.get("value_precision_valid") is True
        ),
        "formula_provenance": bool(row.get("formula_provenance")),
        "denominator_provenance": bool(row.get("denominator_provenance")),
        "applicability_resolved": applicability_resolved,
        "task_coverage": bool(
            (applicability_resolved and task_scope)
            or row.get("task_coverage_valid") is True
        ),
        "aggregate_coverage": bool(aggregate_coverage),
    }
    return _typed_readiness(
        "measurement",
        gates,
        gate_names=_MEASUREMENT_GATE_NAMES,
        live_witness=live_witness,
        extra={
            "coverage": {
                "declared_scope": row.get("coverage_scope"),
                "task": applicability_resolved,
                "aggregate": bool(aggregate_coverage),
            }
        },
    )


def _infra_control_readiness(
    member: str,
    fact_classes: tuple[str, ...],
    fact_lifecycles: dict[str, dict[str, Any]],
    *,
    ledger_artifact: str,
    control_evidence: dict[str, Any] | None = None,
    fair_probe_by_fc: dict[str, bool | None] | None = None,
    live_witness: bool = False,
) -> dict[str, Any]:
    """Typed CAP-control terminal with links, never copied FACT delivery gates.

    PRODUCT DECISION 1 (P5, B-TERM 2026-07-16): completeness = receipt + mediated_fact_ids +
    ``mediation_correct``. ``mediation_correct`` is a REAL, producer-derived, re-verifiable check
    (computed in ``_control_participation_evidence`` from the control's OWN ledger record vs the
    downstream ledger reality): a control that DECLARES APPLIED with no exact-joined delivered row,
    or SUPPRESSED whose exact candidate bytes were nonetheless delivered, grades ``False`` —
    exposed, never silently passed. The causal fair-probe is reported as a SEPARATE enrichment
    field (``mediation_causal_fair_probe``), NOT a completeness gate: a control's causal evidence
    rides the mediated FACT class's registered probe (fair_probe_result.py). Absent probe → None
    (honest). This makes ``infra_control_complete`` reachable, closing the prior structural block
    (mediation_correct hardwired None → complete unreachable for all control CAPs).
    """
    scope = sorted(fact_classes or tuple(fact_lifecycles))
    evidence = control_evidence or {}
    records = list((evidence.get("records") or {}).get(member) or [])
    joins = list((evidence.get("joins") or {}).get(member) or [])
    role = cap_role_for(member)

    def linked(field: str) -> list[str]:
        return [
            fact_id for fact_id in scope
            if _val(fact_lifecycles.get(fact_id, {}).get(field)) is True
        ]

    eligible_fact_ids = linked("eligible")
    produced_fact_ids = linked("produced")
    delivered_fact_ids = linked("delivered")
    if role == "eligibility":
        # G1: an eligibility referee never joins a delivery (a permit does not cause one).
        # Its runtime linkage evidence is the typed record itself: an identified ruling
        # (candidate seal+chars) naming the fact class it refereed. Rulings without
        # candidate identity (enabling actions, no-op evaluations) link nothing.
        runtime_linked = sorted({
            identity[1] for record in records
            if (identity := _exact_control_candidate_identity(record)) is not None
            and identity[1] in scope
        })
    else:
        runtime_linked = sorted({
            str(join["fact_class"]) for join in joins if join.get("fact_class") in scope
        })
    control_receipt = bool(joins) if role == "mediator" else bool(records)
    # DECISION 1: the control's declared effect matched the downstream ledger reality. Producer-
    # owned (the ledger is the control's own record), deterministic, and re-verifiable. None when
    # the control emitted no verifiable record (fail-closed UNMEASURED, never a manufactured pass).
    mediation_correct = (
        (evidence.get("correctness") or {}).get(member)
        if control_evidence is not None else None
    )
    # DECISION 1: causal ENRICHMENT (never gates completeness). The control's causal evidence is
    # the J4 fair-probe of the FACT class(es) it mediates (the same per-class join a byte owner
    # reads via ``_member_fair_probe``). A kernel mediator (empty mediated scope) has no single
    # mediated class → None; absent probe → None (honest).
    mediation_causal_fair_probe = _member_fair_probe(member, fair_probe_by_fc or {})
    mediation = {
        "status": "MEASURED" if control_receipt else "UNMEASURED",
        "linked_fact_ids": scope,
        "runtime_linked_fact_ids": runtime_linked,
        "runtime_linkage_reason": (
            (
                "exact typed eligibility ruling bound to a policy-observation candidate"
                if role == "eligibility"
                else "exact typed control candidate joined to downstream FACT delivery"
            )
            if control_receipt
            else "exact profile-member control receipt unavailable"
        ),
        "eligible_fact_ids": eligible_fact_ids,
        "produced_fact_ids": produced_fact_ids,
        "delivered_fact_ids": delivered_fact_ids,
        "declared_effect_correct": mediation_correct,
        "causal_fair_probe": mediation_causal_fair_probe,
        "source_artifact": ledger_artifact,
    }
    if control_evidence is not None:
        mediation["participation_records"] = records
        mediation["participation_joins"] = joins
    gates: dict[str, bool | None] = {
        "runtime_member_control_receipt": True if control_receipt else None,
        "mediated_fact_ids": True if runtime_linked else None,
        "mediation_correct": mediation_correct,
    }
    return _typed_readiness(
        "infra_control",
        gates,
        gate_names=_INFRA_CONTROL_GATE_NAMES,
        live_witness=live_witness,
        extra={
            "member": member,
            "mediation": mediation,
            # Enrichment mirror of the FACT terminal's gate-7 fair_probe: reported, not gating.
            "mediation_causal_fair_probe": mediation_causal_fair_probe,
            # T2-audit finding 3 (2026-07-18): this terminal is the INTERIM 3-gate schema.
            # The full SS-REFEREE doctrine (rl_timed_intervention, leak containment,
            # restraint, protective_value, causal intervention) is the DECLARED promotion
            # bar (ss_proof_manifest dependencies) and is NOT yet independently gated here.
            # An INFRA_CONTROL_SS_LIVE instance therefore never implies doctrine-level
            # promotion; the diagnosis emits the executable missing-dependency delta.
            "standard": "ss_referee_interim_3gate_v1",
        },
    )


def _internal_fact_support_readiness(
    fact_class: str,
    lifecycle: dict[str, Any],
    source_record: dict[str, Any],
    *,
    ledger_artifact: str,
    live_witness: bool = False,
    fair_probe_by_fc: dict[str, bool | None] | None = None,
) -> dict[str, Any]:
    """Typed internal FACT terminal; never projects model-delivery gates.

    The cochange source is not a second model delivery. It contributes to one
    localization candidate already sealed and acknowledged by the brief path.
    Truth and causality remain separate gates and are never inferred from use.
    """
    candidate_id = source_record.get("candidate_id")
    candidate_path = source_record.get("candidate_path")
    seal = source_record.get("content_sha256_16")
    chars = source_record.get("chars_delivered")
    block_seal = source_record.get("block_content_sha256_16")
    block_span = source_record.get("block_char_span")
    delivery_home = source_record.get("delivery_message_index")
    receipt_level = source_record.get("receipt_level")
    receipt_evidence = source_record.get("receipt_evidence")
    normalized_path = str(candidate_path or "").replace("\\", "/")
    while normalized_path.startswith("./"):
        normalized_path = normalized_path[2:]
    normalized_path = normalized_path.lstrip("/")
    receipt_index = None
    if isinstance(receipt_evidence, dict):
        receipt_index = (
            receipt_evidence.get("acted_message_index")
            if isinstance(receipt_level, int) and receipt_level >= 3
            else receipt_evidence.get("referenced_message_index")
        )
    exact_source_field = any(
        isinstance(field, str)
        and re.fullmatch(
            r"metrics\.localization_proof\[\d+\]\.components\.cochange",
            field,
        )
        for field in source_record.get("source_fields") or []
    )
    runtime_receipt = bool(
        source_record.get("status") == "MEASURED"
        and source_record.get("source_artifact") == "brief_result.json"
        and exact_source_field
        and isinstance(seal, str) and re.fullmatch(r"[0-9a-f]{16}", seal)
        and isinstance(chars, int) and not isinstance(chars, bool) and chars > 0
        and isinstance(source_record.get("producer_entry_index"), int)
        and not isinstance(source_record.get("producer_entry_index"), bool)
        and isinstance(source_record.get("producer_ledger_layer"), str)
        and bool(source_record.get("producer_ledger_layer"))
        and isinstance(delivery_home, int) and not isinstance(delivery_home, bool)
        and delivery_home >= 0
        and isinstance(receipt_level, int) and not isinstance(receipt_level, bool)
        and receipt_level >= 2
        and isinstance(receipt_index, int) and not isinstance(receipt_index, bool)
        and receipt_index > delivery_home
    )
    candidate_bound = bool(
        runtime_receipt
        and isinstance(candidate_id, str)
        and candidate_id == f"localization:{normalized_path}"
        and isinstance(candidate_path, str) and bool(candidate_path)
        and isinstance(block_seal, str)
        and re.fullmatch(r"[0-9a-f]{16}", block_seal)
        and isinstance(block_span, list) and len(block_span) == 2
        and all(isinstance(value, int) and not isinstance(value, bool) for value in block_span)
        and 0 <= block_span[0] < block_span[1]
        and (
            (
                source_record.get("producer_payload_scope") == "whole_brief"
                and block_span[1] <= chars
            )
            or (
                source_record.get("producer_payload_scope") == "exact_block"
                and block_span[1] - block_span[0] == chars
            )
        )
    )
    downstream_join = bool(
        candidate_bound
        and source_record.get("supported_fact_class") == "localization"
    )

    def producer_gate(field: str) -> bool | None:
        value = source_record.get(field)
        return value if isinstance(value, bool) else None

    # SPEC-J4: the cochange support has no fair-probe design of its own — its causal verdict is
    # the adjudicated fair-probe of the FACT class it contributed to (the localization candidate
    # it was sealed and acknowledged into). INHERIT that bool; UNMEASURED -> None (fail-closed).
    # Schema-distinct from the producer's own None default: the inheritance is marked explicitly.
    supported_fc = source_record.get("supported_fact_class")
    inherited_support_fair_probe: bool | None = None
    if fair_probe_by_fc is not None and isinstance(supported_fc, str):
        fp_candidate = fair_probe_by_fc.get(supported_fc)
        if isinstance(fp_candidate, bool):
            inherited_support_fair_probe = fp_candidate

    return _typed_readiness(
        "internal_support",
        {
            "runtime_support_receipt": True if runtime_receipt else None,
            "supported_candidate_id": True if candidate_bound else None,
            "downstream_decision_join": True if downstream_join else None,
            "support_correct": producer_gate("source_contribution_correct"),
            "support_causal_fair_probe": inherited_support_fair_probe,
        },
        gate_names=_INTERNAL_SUPPORT_GATE_NAMES,
        # Offline evidence never sets the live bit; a receipt-chain join does not
        # distinguish a paid trajectory from a replay. The live bit is supplied only
        # from run-provenance (live_run_provenance), never inferred from the join.
        live_witness=live_witness,
        extra={
            "fact_class": fact_class,
            "source_artifact": ledger_artifact,
            "legacy_lifecycle_present": bool(lifecycle),
            "delivery_gates_inapplicable": True,
            "candidate_id": candidate_id,
            "supported_fact_class": source_record.get("supported_fact_class"),
            "downstream_delivery_seal": seal,
            "downstream_receipt_level": receipt_level,
            **(
                {"support_causal_fair_probe_inherited_from_fact": supported_fc}
                if inherited_support_fair_probe is not None else {}
            ),
        },
    )


def _valid_readiness_projection(value: object) -> bool:
    """Validate the canonical object and its derived terminal/blocker fields."""
    if not isinstance(value, dict):
        return False
    gates = value.get("gates")
    if not isinstance(gates, dict):
        return False
    role = value.get("role")
    gate_names = {
        None: _SS_GATE_NAMES,
        "measurement": _MEASUREMENT_GATE_NAMES,
        "infra_control": _INFRA_CONTROL_GATE_NAMES,
        "support": _SUPPORT_GATE_NAMES,
        "internal_support": _INTERNAL_SUPPORT_GATE_NAMES,
    }.get(role, ())
    if not gate_names or tuple(gates) != gate_names:
        return False
    if any(gate is not True and gate is not False and gate is not None
           for gate in gates.values()):
        return False
    live_witness = value.get("live_witness")
    ss_live = value.get("ss_live")
    if not isinstance(live_witness, bool) or not isinstance(ss_live, bool):
        return False
    complete = all(gate is True for gate in gates.values())
    expected_live = live_witness and complete
    expected_blockers = [name for name, gate in gates.items() if gate is not True]
    if not live_witness:
        expected_blockers.append("live_witness")
    if ss_live is not expected_live or value.get("blockers") != expected_blockers:
        return False
    if role is not None and value.get(f"{role}_complete") is not complete:
        return False
    return True


def _acquisition_readiness(
    record: dict[str, Any], *, leak_free: bool | None, dose_ok: bool | None,
    live_witness: bool = False,
    fair_probe_by_fc: dict[str, bool | None] | None = None,
    timing_by_fc: dict[str, bool | None] | None = None,
) -> dict[str, Any]:
    """Project ACQ support evidence without borrowing the FACT delivery gates."""
    receipt = record.get("receipt_level")
    seal = record.get("content_sha256_16")
    candidate_local = bool(
        isinstance(record.get("source_artifact"), str)
        and record.get("source_artifact")
        and isinstance(record.get("block_id"), str)
        and record.get("block_id")
        and isinstance(seal, str)
        and seal
    )
    joined = bool(
        record.get("status") == "MEASURED"
        and candidate_local
        and isinstance(receipt, int) and not isinstance(receipt, bool)
        and receipt >= 2
    )
    # SPEC-J4: an ACQ candidate has no fair-probe design of its own — its causal contribution is
    # the causal verdict of the FACT class it supports. INHERIT that verdict ONLY when it is a
    # concrete bool (behavioral CAUSAL -> True, SELF_LOCALIZED -> False;
    # CAUSAL_PAIRED is enrichment-only -> None); an UNMEASURED fact verdict stays None
    # (fail-closed). The inheritance is marked explicitly, never silent.
    supported_fc = record.get("supported_fact_class")
    inherited_fair_probe: bool | None = None
    if fair_probe_by_fc is not None and isinstance(supported_fc, str):
        candidate = fair_probe_by_fc.get(supported_fc)
        if isinstance(candidate, bool):
            inherited_fair_probe = candidate
    # SPEC-J3: an ACQ candidate has no delivery timing of its own — its adhered-time is the
    # supported FACT class's chronologically adjudicated delivery verdict. INHERIT that bool
    # (ON_TIME -> True, LATE/STEP_BEHIND -> False); an UNMEASURED class stays None (fail-closed).
    inherited_timing: bool | None = None
    if timing_by_fc is not None and isinstance(supported_fc, str):
        timing_candidate = timing_by_fc.get(supported_fc)
        if isinstance(timing_candidate, bool):
            inherited_timing = timing_candidate
    # B-ACQ: source-contribution truth is the producer's OWN self-sealed attestation, joined by
    # the collector (acq_provenance._valid_contribution_attestation) into the record. A collector
    # shape-validation is NOT source truth (the 47dacfd0f class) — this reads a producer verdict
    # only, staying None when the attestation is absent/tampered.
    attested = record.get("source_contribution_correct")
    source_contribution_correct = attested if isinstance(attested, bool) else None
    extra_fields: dict[str, Any] = {}
    if inherited_fair_probe is not None:
        extra_fields["source_causal_fair_probe_inherited_from_fact"] = supported_fc
    if inherited_timing is not None:
        extra_fields["timing_inherited_from_fact_class"] = supported_fc
    extra = extra_fields or None
    return _typed_readiness(
        "support",
        {
            "supported_fact_delivery_join": joined,
            "candidate_local_contribution": candidate_local,
            # B-ACQ: producer-attested source-contribution truth (self-sealed to the delivered
            # block); SPEC-J3 inherited delivery timing of the supported FACT class. Both stay
            # None when their typed authority is absent — never manufactured here.
            "source_contribution_correct": source_contribution_correct,
            "timing_inherited_from_fact_delivery": inherited_timing,
            # SPEC-J4: inherited from the supported FACT class's adjudicated fair-probe verdict.
            "source_causal_fair_probe": inherited_fair_probe,
        },
        gate_names=_SUPPORT_GATE_NAMES,
        # Offline evidence never sets the live bit (gt_gt.md execution ledger). Gates
        # 1-6 passing does not distinguish a paid trajectory from a replay; the terminal
        # live bit is joined ONLY from run-provenance artifacts (live_run_provenance),
        # never inferred here.
        live_witness=live_witness,
        extra=extra,
    )


def _row_has_seal_join(
    row: dict[str, Any], entries: object,
) -> bool:
    """Whether one exact producer row is byte-joined into a model observation."""
    if not isinstance(entries, list):
        return False
    seal = row.get("content_sha256_16")
    chars = row.get("chars_delivered")
    if not isinstance(seal, str) or not seal:
        return False
    if not isinstance(chars, int) or isinstance(chars, bool) or chars <= 0:
        return False
    for entry in entries:
        # #43: a delivery is byte-joined in EITHER physical record -- the agent's
        # own message stream (seal span) or the bound provider payload (capsule
        # coordinates). The provider-boundary capsule is model-visible and never
        # present in the trajectory file, so a trajectory-only predicate left
        # every CAP byte owner unprovable in the production posture.
        if (
            not isinstance(entry, dict)
            or entry.get("source") not in MODEL_VISIBLE_SOURCES
        ):
            continue
        if (
            entry.get("joined") is not True
            or entry.get("join_method") not in _PHYSICAL_BYTE_JOIN_METHODS
        ):
            continue
        entry_chars = entry.get("ledger_chars", entry.get("chars"))
        if entry.get("content_sha256_16") != seal or entry_chars != chars:
            continue
        if entry.get("ledger_layer") != row.get("layer"):
            continue
        return True
    return False


def _member_delivery_byte_proven(
    member: str,
    rows: list[dict],
    consumption_ledger: dict[str, Any],
) -> bool:
    """Prove a byte owner through its one authorized attribution mechanism."""
    if cap_role_for(member) != "byte_owner":
        return False
    authority = CAP_BYTE_OWNER_MECHANISMS.get(member)
    if authority is None:
        return False
    entries = consumption_ledger.get("entries")
    for row in rows:
        if row.get("outcome") != "delivered":
            continue
        if authority.mechanism == "typed_lineage":
            refs = row.get("feature_ids")
            if not isinstance(refs, list) or {
                "category": "CAP", "feature_id": member, "role": "byte_owner",
            } not in refs:
                continue
            evidence_type = row.get("evidence_type")
            runtime_producer = row.get("runtime_producer_id")
            registered_producer = row.get("registered_producer_id")
            if not isinstance(evidence_type, str) or not evidence_type:
                continue
            fact_class = row.get("fact_class")
            fact_registration = (
                registration_for(evidence_type) if isinstance(evidence_type, str) else None
            )
            evidence_base = (
                evidence_type.split(":", 1)[0] if isinstance(evidence_type, str) else ""
            )
            binding_matches = any(
                binding.producer == runtime_producer
                and binding.layer == evidence_base
                and binding.fact_class == fact_class
                for binding in authority.bindings
            )
            if (
                row.get("lineage_schema") != "gt.feature_lineage.v1"
                or row.get("producer_registration_match") is not True
                or fact_registration is None
                or fact_registration.fact_class != fact_class
                or fact_registration.producer != registered_producer
                or not isinstance(runtime_producer, str)
                or not producer_matches(evidence_type, runtime_producer)
                or not binding_matches
            ):
                continue
        elif authority.mechanism == "exact_profile_member":
            if row.get("profile_member") != member:
                continue
            layer = str(row.get("layer") or "")
            binding_matches = any(
                binding.layer == layer
                and (
                    binding.fact_class is None
                    or layer_to_fact_class(layer) == binding.fact_class
                )
                for binding in authority.bindings
            )
            if not binding_matches:
                continue
        else:
            continue
        if _row_has_seal_join(row, entries):
            return True
    # CANONICAL ROUTE (#41 hole 1). The capsule delivery row carries neither `feature_ids` nor
    # `profile_member` -- both are legacy-lane stamps -- so before this every CAP byte owner was
    # unprovable whenever the canonical runtime was attached, i.e. in the intended production
    # posture, REGARDLESS of whether its producer fired.
    #
    # The row instead carries `evidence_lineage`, one entry per delivered evidence, whose
    # `cap_owners` are the ALREADY-AUTHORIZED byte-owner ids (`_authorized_cap_byte_owners`:
    # an explicit byte_owner ref in the lineage AND a mechanism binding for that fact class).
    # This reader re-checks the claim against the SAME static authority table rather than
    # trusting the stamp -- the row must prove its own attribution, exactly as the two
    # mechanisms above do.
    #
    # APPLIES TO ALL SEVEN OWNERS, including the `exact_profile_member` ones. I first
    # restricted this to `typed_lineage` because `build_lineage` REFUSES to mint a CAP ref for
    # GT_CERT_DELIVERY / GT_EDIT_CHECK / GT_HYPOTHESIS. That is true of the GATEWAY path and
    # false of the canonical one: `canonical_producers._lineage` (:264-290) adds the byte-owner
    # FeatureRef directly, gated on the mechanism having a binding for that fact class, so
    # those three ARE authorized on a canonical record (`canonical_producers.py:569, :623,
    # :711`). Restricting by mechanism here would have excluded exactly the three owners this
    # branch exists to rescue.
    #
    # NOT `profile_member`: stamping that column on a canonical row cannot work, because the
    # exact-profile mechanism also requires `binding.layer == row["layer"]` and a canonical
    # row's layer is the constant "canonical.provider_delivery". Making it match would mean
    # writing a lane layer the record does not have, or deleting the only structural check
    # that mechanism has. The producer-authorized owner is the honest witness.
    if True:
        for row in rows:
            if row.get("outcome") != "delivered":
                continue
            for entry in row.get("evidence_lineage") or ():
                if not isinstance(entry, dict):
                    continue
                owners = entry.get("cap_owners")
                if not isinstance(owners, list) or member not in owners:
                    continue
                fact_class = entry.get("fact_class")
                if not any(
                    binding.fact_class == fact_class for binding in authority.bindings
                ):
                    continue
                if _row_has_seal_join(row, entries):
                    return True
    return False


def _block_delivery_byte_proofs(
    rows: list[dict],
    block_chronologies: list[Any],
) -> frozenset[str]:
    """Fact classes byte-proven through COMPOUND-row blocks — same physical authority.

    RUN-#3 PILOT (2026-07-18, R1): GT ships facts in two physical shapes — a fact alone in
    its own delivered row, and several facts sharing ONE physical observation (the brief),
    identified per block in ``block_lineage``. The byte-proof consumer read only top-level
    row fields, so compound deliveries could never byte-prove any class even when the
    independent consumption authority seal-joined their bytes (writer truth verified on
    live artifacts BEFORE this reader changed).

    This path rides the EXISTING block-grain authority (``extract_block_chronologies``):
    a block qualifies iff its parent row's physical delivery is BOUND and the block's own
    seal located inside that exact message (both enforced by the extractor), AND the
    block's OWN lineage satisfies the SAME producer contract the single-fact path enforces
    (schema, registration resolution, class match, producer match) — never weaker.

    LEGACY-COMPOUND READER — DORMANT ON THE CANONICAL PATH (C14, 2026-07-28). No
    production writer emits ``block_lineage``; the prepend-and-seal producer it was built
    for was deleted with the prepend itself (4f525f424). This function is ADDITIVE ONLY —
    ``proofs`` starts empty and only ever gains members, so with zero compound rows the
    loop body never runs and it returns an empty frozenset. It therefore cannot byte-prove
    LESS than the single-fact path would; it simply adds nothing. Kept because SS-10
    replay reads RECORDED artifacts that do contain compound rows.
    """
    lineage_by_key: dict[tuple[int, str], dict[str, Any]] = {}
    duplicate_keys: set[tuple[int, str]] = set()
    for row_index, row in enumerate(rows):
        if (
            not isinstance(row, dict)
            or row.get("outcome") != "delivered"
            or row.get("compound_delivery") is not True
            or row.get("compound_lineage_schema")
            != "gt.compound_feature_lineage.v1"
        ):
            continue
        for block in row.get("block_lineage") or []:
            if isinstance(block, dict) and isinstance(block.get("block_id"), str):
                key = (row_index, block["block_id"])
                if key in lineage_by_key:
                    duplicate_keys.add(key)
                else:
                    lineage_by_key[key] = block
    proofs: set[str] = set()
    for chron in block_chronologies:
        block_id = getattr(chron, "block_id", None)
        row_index = getattr(chron, "ledger_row_index", None)
        if not isinstance(block_id, str) or not block_id or type(row_index) is not int:
            continue
        if getattr(chron, "physical_join_state", None) != "PHYSICAL_DELIVERY_BOUND":
            continue
        key = (row_index, block_id)
        if key in duplicate_keys:
            continue
        block = lineage_by_key.get(key)
        if block is None:
            continue
        span = block.get("char_span")
        block_chars = block.get("chars_delivered")
        block_seal = block.get("content_sha256_16")
        block_candidate_id = block.get("candidate_id")
        declared = block.get("declared_fact_class")
        typed = validate_block_lineage(
            block, parent_actual_event=str(rows[row_index].get("event_type") or "")
        )
        if (
            not isinstance(span, list)
            or len(span) != 2
            or not all(type(offset) is int for offset in span)
            or type(block_chars) is not int
            or block_chars <= 0
            or block_chars != span[1] - span[0]
            or not isinstance(block_seal, str)
            or typed is None
            or not isinstance(declared, str)
            or not declared
            or getattr(chron, "block_lineage_validated", None) is not True
            or getattr(chron, "block_char_span", None) != tuple(span)
            or getattr(chron, "block_chars_delivered", None) != block_chars
            or getattr(chron, "delivery_seal", None) != block_seal
            or not isinstance(block_candidate_id, str)
            or not block_candidate_id
            or getattr(chron, "block_candidate_id", None) != block_candidate_id
            or getattr(chron, "fact_class", None) != declared
            or getattr(chron, "evidence_type", None) != typed[0]
            or not isinstance(getattr(chron, "parent_physical_id", None), str)
            or not getattr(chron, "parent_physical_id", None)
            or not isinstance(getattr(chron, "physical_id", None), str)
            or not getattr(chron, "physical_id", None)
        ):
            continue
        expected_physical_id = (
            f"{chron.parent_physical_id}:block:{span[0]}:{span[1]}:{block_id}"
        )
        if chron.physical_id != expected_physical_id:
            continue
        proofs.add(declared)
    return frozenset(proofs)


def _fact_delivery_byte_proven(
    fact_class: str,
    rows: list[dict],
    consumption_ledger: dict[str, Any],
    *,
    block_byte_proofs: frozenset[str] = frozenset(),
) -> bool:
    """True only for authoritative typed FACT lineage with an exact seal join.

    Two physical shapes prove bytes (R1): a single-fact row (top-level typed lineage +
    row seal join, below) or a compound-row BLOCK (``_block_delivery_byte_proofs`` — the
    block-grain physical authority + the identical producer contract).
    """
    if fact_class in block_byte_proofs:
        return True
    entries = consumption_ledger.get("entries")
    for row in rows:
        if row.get("outcome") != "delivered":
            continue
        if row.get("lineage_schema") != "gt.feature_lineage.v1":
            continue
        if row.get("fact_class") != fact_class:
            continue
        evidence_type = row.get("evidence_type")
        runtime_producer = row.get("runtime_producer_id")
        registered_producer = row.get("registered_producer_id")
        if not isinstance(evidence_type, str) or not evidence_type:
            continue
        fact_registration = (
            registration_for(evidence_type) if isinstance(evidence_type, str) else None
        )
        if (
            row.get("producer_registration_match") is not True
            or fact_registration is None
            or fact_registration.fact_class != fact_class
            or fact_registration.producer != registered_producer
            or not isinstance(runtime_producer, str)
                or not producer_matches(evidence_type, runtime_producer)
        ):
            continue
        if _row_has_seal_join(row, entries):
            return True
    return False


def _exact_profile_owner_lifecycle(
    member: str,
    rows: list[dict],
    consumption_ledger: dict[str, Any],
    *,
    ledger_artifact: str,
    traj_artifact: str,
) -> dict[str, Any]:
    """Lifecycle facts provable without borrowing a registered FACT lifecycle."""
    lc = new_lifecycle("exact_profile_member_observation_not_proven")
    authority = CAP_BYTE_OWNER_MECHANISMS.get(member)
    if authority is None or authority.mechanism != "exact_profile_member":
        return lc
    entries = consumption_ledger.get("entries")
    entries = entries if isinstance(entries, list) else []
    for row in rows:
        if (
            row.get("outcome") != "delivered"
            or row.get("profile_member") != member
        ):
            continue
        layer = str(row.get("layer") or "")
        if not any(
            binding.layer == layer
            and (
                binding.fact_class is None
                or layer_to_fact_class(layer) == binding.fact_class
            )
            for binding in authority.bindings
        ):
            continue
        lc["eligible"] = measured(True, source_artifact=ledger_artifact)
        lc["not_eligible"] = measured(False, source_artifact=ledger_artifact)
        lc["produced"] = measured(True, source_artifact=ledger_artifact)
        lc["authority_valid"] = measured(True, source_artifact=ledger_artifact)
        if not _row_has_seal_join(row, entries):
            lc["delivered"] = measured(False, source_artifact=ledger_artifact)
            continue
        receipt = 1
        source_messages: list[int] = []
        for entry in entries:
            if not isinstance(entry, dict) or entry.get("source") != "trajectory":
                continue
            entry_chars = entry.get("ledger_chars", entry.get("chars"))
            if (
                entry.get("joined") is True
                and entry.get("join_method") == "seal"
                and entry.get("content_sha256_16") == row.get("content_sha256_16")
                and entry_chars == row.get("chars_delivered")
                and entry.get("ledger_layer") == layer
            ):
                receipt = max(receipt, int(entry.get("receipt") or 0))
                if isinstance(entry.get("msg_index"), int):
                    source_messages.append(entry["msg_index"])
        lc["delivered"] = measured(
            True, source_artifact=traj_artifact, source_messages=sorted(set(source_messages))
        )
        lc["receipt_level"] = measured(
            receipt, source_artifact=traj_artifact, source_messages=sorted(set(source_messages))
        )
        return lc
    return lc


# ---------------------------------------------------------------------------
# Leak canary — no task id / gold path on any model-facing field.
# ---------------------------------------------------------------------------

def leak_canary(delivered_files: Iterable[str], task: str, gold_paths: Iterable[str]) -> list[str]:
    """Return leaked tokens found in model-facing delivered file identities. Zero is required.
    (This grader emits no hidden test identity itself; it scans what the seam delivered.)"""
    leaks: list[str] = []
    gold_set = {os.path.basename(g) for g in gold_paths if g}
    for f in delivered_files:
        base = os.path.basename(f or "")
        if base in gold_set:
            leaks.append(f"gold_path:{base}")
    return leaks


# ---------------------------------------------------------------------------
# Infra signals + member rollup.
# ---------------------------------------------------------------------------

def _infra_signals(rows: list[dict], ledger_by_fc: dict[str, dict]) -> dict[str, Any]:
    """Kernel-level signals (L6 freshness staging, global arbiter totals, dose dedup) used
    by the infrastructure-member rollups — mediation evidence, never a direct help credit."""
    l6_staged = False
    l6_reindex = 0
    for r in rows:
        lay = str(r.get("layer") or "")
        if lay == "L6" or lay.startswith("L6."):
            oc = str(r.get("outcome") or "")
            if oc == "STAGED_OK":
                l6_staged = True
            if oc == "REINDEX_OK":
                l6_reindex += 1
    total_cand = sum(int(b.get("arbiter_candidates", 0)) for b in ledger_by_fc.values())
    total_lost = sum(int(b.get("arbiter_lost", 0)) for b in ledger_by_fc.values())
    total_dose = sum(int(b.get("dose_suppressed", 0)) for b in ledger_by_fc.values())
    any_prod = any(int(b.get("produced", 0)) > 0 for b in ledger_by_fc.values())
    any_deliv = any(int(b.get("delivered", 0)) > 0 for b in ledger_by_fc.values())
    return {
        "l6_staged": l6_staged, "l6_reindex": l6_reindex,
        "arbiter_candidates": total_cand, "arbiter_lost": total_lost,
        "dose_suppressed": total_dose, "any_produced": any_prod, "any_delivered": any_deliv,
    }


def _infra_member_lifecycle(
    member: str,
    fcs: tuple[str, ...],
    fact_lifecycles: dict[str, dict],
    infra_signals: dict[str, Any],
    baseline_status: str,
    *,
    ledger_artifact: str,
    traj_artifact: str,
) -> dict[str, Any]:
    """Mediation lifecycle for an infrastructure member: correctness + mediation ONLY. The
    efficiency fields stay UNMEASURED — an enabling feature never earns direct help credit
    (its effect is traced through the downstream facts it mediates)."""
    lc = new_lifecycle("infra_mediation_not_applicable")
    lc["baseline_or_holdout_status"] = measured(baseline_status, source_artifact=traj_artifact)
    lc["steps_saved"] = unmeasured("infrastructure: mediation-only, never direct help credit")
    lc["tokens_saved"] = unmeasured("infrastructure: mediation-only, never direct help credit")
    lc["cost_delta"] = unmeasured("infrastructure: mediation-only")
    lc["state_changed"] = not_eligible("infra mediates; owns no direct state predicate")
    lc["harmful"] = measured(False, source_artifact=ledger_artifact)
    for field in (
        "delivered", "expired_late", "stale", "native_valid",
        "render_observation_hash_match", "receipt_level", "reacquired",
    ):
        lc[field] = not_eligible(
            "infra control owns mediation, not a downstream FACT delivery",
            source_artifact=ledger_artifact,
        )

    if member == "GT_GLOBAL_ARBITER":
        cand = int(infra_signals["arbiter_candidates"])
        lc["eligible"] = measured(cand > 0, source_artifact=ledger_artifact)
        lc["not_eligible"] = measured(cand == 0, source_artifact=ledger_artifact)
        lc["entered_arbiter"] = measured(cand > 0, source_artifact=ledger_artifact)
        lc["arbiter_lost"] = measured(int(infra_signals["arbiter_lost"]) > 0, source_artifact=ledger_artifact)
        lc["arbiter_won"] = measured(bool(infra_signals["any_delivered"]), source_artifact=ledger_artifact)
        lc["produced"] = measured(cand > 0, source_artifact=ledger_artifact)
        lc["dose_tokens"] = measured(int(infra_signals["dose_suppressed"]), source_artifact=ledger_artifact)
        return lc

    if member in ("GT_L6_FRESH", "GT_EDIT_OVERLAY"):
        staged = bool(infra_signals["l6_staged"]) or int(infra_signals["l6_reindex"]) > 0
        lc["eligible"] = measured(staged, source_artifact=ledger_artifact)
        lc["not_eligible"] = measured(not staged, source_artifact=ledger_artifact)
        lc["produced"] = measured(staged, source_artifact=ledger_artifact)
        return lc

    scope = list(fcs) if fcs else list(fact_lifecycles)
    elig = _any3(fact_lifecycles[fc].get("eligible") for fc in scope)
    prod = any(_val(fact_lifecycles[fc].get("produced")) for fc in scope)
    abstain = _any3(fact_lifecycles[fc].get("correct_abstain") for fc in scope)
    if elig is None:
        # At least one scoped class is UNMEASURED and none is a definite True, so
        # the mediator's own eligibility is unknown. `any()` would have read that
        # unknown as False, and `verdict_for` treats eligible-False as "correctly
        # silent, never CUT" — a manufactured PASS from missing evidence.
        _why = "scoped fact class eligibility is UNMEASURED"
        lc["eligible"] = unmeasured(_why, source_artifact=ledger_artifact)
        lc["not_eligible"] = unmeasured(_why, source_artifact=ledger_artifact)
    else:
        lc["eligible"] = measured(bool(elig), source_artifact=ledger_artifact)
        lc["not_eligible"] = measured(not elig, source_artifact=ledger_artifact)
    lc["produced"] = measured(bool(prod), source_artifact=ledger_artifact)
    # a mediator whose class correctly abstained (e.g. cert render on a clean submit) is a
    # correct abstain, NOT dark.
    if abstain:
        lc["correct_abstain"] = measured(True, source_artifact=ledger_artifact)
    # a SCOPED mediator inherits the timing/freshness failure of the class it shaped: if the
    # delivered mediated fact was stale/late, the mediation failed (→ FIX). A KERNEL mediator
    # (fcs empty) touches all classes and must NOT inherit any single class's failure.
    return lc


def member_record(
    member: str,
    fact_lifecycles: dict[str, dict],
    infra_signals: dict[str, Any],
    baseline_status: str,
    *,
    ledger_artifact: str,
    traj_artifact: str,
    owner_rows: list[dict] | None = None,
    consumption_ledger: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the per-feature record (lifecycle + compact summary + verdict) for one member.

    DIRECT producer -> its fact class's substantive lifecycle. INFRA -> mediation-only."""
    role = member_role(member)
    fcs = member_fact_classes(member)
    if role == ROLE_DIRECT:
        if fcs:
            fc = fcs[0]
            lc = {k: v for k, v in fact_lifecycles[fc].items() if not k.startswith("_")}
        else:
            lc = _exact_profile_owner_lifecycle(
                member,
                owner_rows or [],
                consumption_ledger or {},
                ledger_artifact=ledger_artifact,
                traj_artifact=traj_artifact,
            )
    else:
        lc = _infra_member_lifecycle(
            member, fcs, fact_lifecycles, infra_signals, baseline_status,
            ledger_artifact=ledger_artifact, traj_artifact=traj_artifact,
        )
    verdict = verdict_for(lc, role)
    return {
        "role": role,
        "cap_role": cap_role_for(member),
        "fact_classes": list(fcs) if fcs else (["*kernel*"] if role == ROLE_INFRA else []),
        "lifecycle": lc,
        "summary": feature_summary_from_lifecycle(lc, verdict),
        "verdict": verdict,
    }


def _acquisition_feature_records(
    task_dir: str,
    consumption_ledger: dict[str, Any],
    trajectory: dict[str, Any],
) -> tuple[dict[str, dict[str, Any]], list[str]]:
    """Collect exact source-to-receipt ACQ proofs when the persisted brief exists."""
    inventory = canonical_feature_inventory()["ACQ"]
    # Unlike task-named deep metrics, brief_result has no task id in its
    # filename. Bound it to the task directory or its immediate substrate
    # parent so a run-root sidecar can never contaminate sibling tasks.
    brief_path = _find_named_input(task_dir, "brief_result.json", locations=2)
    brief_payload = _load_json(brief_path) if brief_path else None
    if brief_path is not None and not isinstance(brief_payload, dict):
        raise ValueError(
            f"gt_feature_metrics: malformed required ACQ artifact {brief_path!r}"
        )
    try:
        from acq_provenance import collect_acq_provenance
    except ImportError:
        collector_available = False
        records = {
            name: {
                "status": "UNMEASURED", "source_artifact": None,
                "receipt_level": None, "blocker": "acq_provenance_collector_unavailable",
                "block_id": None, "content_sha256_16": None,
            }
            for name in inventory
        }
    else:
        collector_available = True
        records = collect_acq_provenance(brief_payload, consumption_ledger, trajectory)

    if tuple(records) != tuple(inventory):
        raise ValueError(
            "gt_feature_metrics: ACQ collector name/order drift; expected "
            f"{list(inventory)}, got {list(records)}"
        )
    missing: list[str] = []
    if brief_path is None:
        missing.append("acq_provenance")
    if not collector_available:
        missing.append("acq_provenance_collector")
    normalized: dict[str, dict[str, Any]] = {}
    for name, raw in records.items():
        if not isinstance(raw, dict) or raw.get("status") not in {"MEASURED", "UNMEASURED"}:
            raise ValueError(f"gt_feature_metrics: malformed ACQ record for {name!r}")
        record = dict(raw)
        record["family"] = "ACQ"
        record["source"] = "acq_provenance"
        normalized[name] = record
    return normalized, sorted(set(missing))


def _canonical_task_features(
    task: str,
    task_dir: str,
    cap_features: dict[str, dict],
    fact_classes: dict[str, dict],
    fact_readiness: dict[str, dict[str, Any]],
    opportunity_by_feature: dict[str, dict[str, Any]],
    acq: dict[str, dict[str, Any]],
    acq_missing: list[str],
    *,
    leak_free: bool | None,
    dose_ok: bool | None,
    live_witness: bool = False,
    fair_probe_by_fc: dict[str, bool | None] | None = None,
    timing_by_fc: dict[str, bool | None] | None = None,
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    """Build the explicit 129-row task ledger without mutating legacy fields."""
    inventory = canonical_feature_inventory()
    master: dict[str, dict[str, Any]] = {}
    for record in acq.values():
        record["ss_readiness"] = _acquisition_readiness(
            record, leak_free=leak_free, dose_ok=dose_ok, live_witness=live_witness,
            fair_probe_by_fc=fair_probe_by_fc, timing_by_fc=timing_by_fc,
        )
    master.update(acq)
    for name in inventory["CAP"]:
        if name not in cap_features:
            raise ValueError(f"gt_feature_metrics: missing legacy CAP record {name!r}")
        master[name] = attach_opportunity_evidence({
            "family": "CAP", "status": "MEASURED", "source": "features",
            "source_artifact": "gt.feature_metrics.v1", "record_ref": f"features.{name}",
            "ss_readiness": cap_features[name]["ss_readiness"],
        }, opportunity_by_feature[name])
    for name in inventory["FACT"]:
        if name not in fact_classes:
            raise ValueError(f"gt_feature_metrics: missing legacy FACT record {name!r}")
        master[name] = attach_opportunity_evidence({
            "family": "FACT", "status": "MEASURED", "source": "fact_classes",
            "source_artifact": "gt.feature_metrics.v1", "record_ref": f"fact_classes.{name}",
            "ss_readiness": fact_readiness[name],
        }, opportunity_by_feature[name])
    performance, perf_missing, deep_path = _performance_feature_records(task, task_dir)
    for record in performance.values():
        record["ss_readiness"] = _measurement_only_readiness(
            record, live_witness=live_witness,
        )
    master.update(performance)

    expected = {name: family for family, names in inventory.items() for name in names}
    actual = {name: record.get("family") for name, record in master.items()}
    opportunity_complete = all(
        isinstance(master[name].get("opportunity_evidence"), dict)
        and master[name]["opportunity_evidence"].get("status") in {"BOUND", "UNMEASURED"}
        for family in ("CAP", "FACT") for name in inventory[family]
    )
    inventory_complete = actual == expected and opportunity_complete and all(
        _valid_readiness_projection(record.get("ss_readiness"))
        for record in master.values()
    )
    missing_required_inputs: list[str] = []
    if deep_path is None:
        missing_required_inputs.append("deep_metrics")
    missing_required_inputs.extend(item for item in acq_missing if "." not in item)
    missing_feature_inputs = sorted(
        item for item in acq_missing + perf_missing if "." in item
    )
    # Honest-reporting invariant: required_inputs_complete is False whenever EITHER
    # the artifact-level inputs OR the per-feature (dotted) inputs are missing, so
    # the flag-bound named list must enumerate BOTH — a False flag with an empty
    # missing_required_inputs is un-actionable (the culprit is unnamed). The dotted
    # feature culprits keep their finer-grained missing_feature_inputs breakout too.
    # This mirrors the downstream convention (visible_audit / control_participation)
    # where every forced-False also names its culprit in missing_required_inputs.
    missing_required_inputs.extend(missing_feature_inputs)
    missing_required_inputs = sorted(set(missing_required_inputs))
    return master, {
        "family_counts": {family: len(names) for family, names in inventory.items()},
        "inventory_count": len(master),
        "inventory_complete": inventory_complete,
        "required_inputs_complete": not missing_required_inputs,
        "missing_required_inputs": missing_required_inputs,
        "missing_feature_inputs": missing_feature_inputs,
    }


def _model_observation_owners(messages: list[dict[str, Any]]) -> dict[int, int]:
    """Map environment-message indices to the policy call that observes them.

    Parallel tool calls are serialized as multiple contiguous ``tool`` messages, but
    the model receives that whole batch in one subsequent inference observation.
    Counting each tool message as an independent observation hides double doses.
    The terminal sentinel groups trailing environment messages when no later policy
    call exists; assistant messages themselves are not environment observations.
    """
    owner = len(messages)
    owners: dict[int, int] = {}
    for index in range(len(messages) - 1, -1, -1):
        message = messages[index]
        if isinstance(message, dict) and message.get("role") == "assistant":
            owner = index
            continue
        owners[index] = owner
    return owners


# ---------------------------------------------------------------------------
# DEFECT 7 + 8 — per-FIRE gate grading (leak + dose) over the physical-delivery
# authority. Today ``leak_zero`` / ``dose_lte_one`` are graded task-globally; this
# grades EACH ``PHYSICAL_DELIVERY_BOUND`` record on its OWN rendered-text span and
# its OWN policy-observation dose, so a single leaking / double-dosed fire is named
# and taints run integrity (the existing run-global scan stays an ADDITIONAL
# fail-closed condition). No new scanner / store / schema key: the leak class reuses
# ``scan_test_identity_leaks`` + the live_evidence ``_LEAK_RE``; dose reuses the
# ``observation_binding.observation_id`` join with the legacy ``_model_observation_owners``
# fallback. Duplicate/ambiguous spans are already ``BROKEN_PHYSICAL_BINDING`` upstream.
# ---------------------------------------------------------------------------
PER_FIRE_GATE_SCHEMA = "gt.per_fire_gate.v1"


def _fire_observation_key(
    record: dict[str, Any], observation_owners: dict[int, int]
) -> tuple[str, Any]:
    """The policy-observation group key for ONE bound physical delivery.

    Prefer the exact ``observation_binding.observation_id`` (the shared atomic join
    key); fall back to the ``_model_observation_owners`` grouping of the delivery's
    message index for legacy rows that predate the binding, and finally to the
    runtime-ledger index so a keyless row can never silently merge with another."""
    binding = record.get("observation_binding")
    if isinstance(binding, dict):
        obs_id = binding.get("observation_id")
        if isinstance(obs_id, str) and obs_id:
            return ("observation_id", obs_id)
    msg_index = record.get("msg_index")
    if isinstance(msg_index, int) and not isinstance(msg_index, bool):
        return ("owner", observation_owners.get(msg_index, msg_index))
    return ("runtime_ledger_index", record.get("runtime_ledger_index"))


def _fire_leak_hits(text: str) -> list[str]:
    """Exact test-identity leak tokens in one rendered span (DEFECT 7).

    Reuses BOTH existing authorities verbatim: the structural
    ``scan_test_identity_leaks`` (``::``-qualified ids / bare ``test_…`` / F2P markers)
    AND the live_evidence ``_LEAK_RE`` class (adds the bare ``assert``/``assertion``
    family). Never a new scanner."""
    if not isinstance(text, str) or not text:
        return []
    hits: set[str] = set(scan_test_identity_leaks(text))
    for match in _LEAK_RE.finditer(text):
        hits.add(match.group(0))
    return sorted(hits)


def per_fire_gate_grades(
    consumption_ledger: dict[str, Any],
    observation_owners: dict[int, int] | None = None,
) -> dict[str, Any]:
    """Grade every PHYSICAL_DELIVERY_BOUND fire on its OWN leak + dose gate row.

    * leak (DEFECT 7): scan the exact ``rendered_text`` span; a fire with any hit
      fails its own ``leak_zero`` row and contributes its tokens to the run taint.
    * dose (DEFECT 8): dose = the count of UNIQUE physical GT deliveries homed to the
      same policy observation (``observation_id`` where present, else the legacy owner
      grouping). A FACT row and its CAP byte-owner sharing one physical span dedupe to
      ONE dose (grouped by ``physical_id``). A fire passes ``dose_lte_one`` only when
      its observation dose is <= 1.

    Pure/deterministic; ledger-only rows and BROKEN bindings are excluded (they are
    not model-visible byte-proven fires)."""
    observation_owners = observation_owners or {}
    authority = physical_delivery_authority(
        consumption_ledger if isinstance(consumption_ledger, dict) else {}
    )
    deliveries = authority.get("deliveries") if isinstance(authority, dict) else None
    deliveries = deliveries if isinstance(deliveries, dict) else {}

    bound: list[dict[str, Any]] = []
    for _index, record in sorted(deliveries.items(), key=lambda kv: str(kv[0])):
        if isinstance(record, dict) and record.get("state") == PHYSICAL_DELIVERY_BOUND:
            bound.append(record)

    # Unique physical spans per observation (dedupe FACT+CAP sharing one span).
    obs_physical: dict[tuple[str, Any], set[str]] = defaultdict(set)
    for record in bound:
        key = _fire_observation_key(record, observation_owners)
        phys = record.get("physical_id")
        obs_physical[key].add(
            str(phys) if isinstance(phys, str) and phys
            else f"idx:{record.get('runtime_ledger_index')}"
        )

    fires: list[dict[str, Any]] = []
    leak_hits: set[str] = set()
    leaking_fire_count = 0
    dose_violation_count = 0
    max_dose = 0
    for record in bound:
        key = _fire_observation_key(record, observation_owners)
        dose = len(obs_physical.get(key, ()))
        max_dose = max(max_dose, dose)
        fire_hits = _fire_leak_hits(record.get("rendered_text") or "")
        leak_free = not fire_hits
        dose_ok = dose <= 1
        if fire_hits:
            leaking_fire_count += 1
            leak_hits.update(fire_hits)
        if not dose_ok:
            dose_violation_count += 1
        fires.append({
            "runtime_ledger_index": record.get("runtime_ledger_index"),
            "physical_id": record.get("physical_id"),
            "observation_group": f"{key[0]}:{key[1]}",
            "dose": dose,
            "leak_zero": leak_free,
            "dose_lte_one": dose_ok,
            "leak_hits": fire_hits,
            "candidate_id": record.get("candidate_id"),
        })
    return {
        "schema": PER_FIRE_GATE_SCHEMA,
        "bound_fire_count": len(bound),
        "leaking_fire_count": leaking_fire_count,
        "dose_violation_count": dose_violation_count,
        "max_dose": max_dose,
        "leak_hits": sorted(leak_hits),
        "fires": fires,
    }


# ---------------------------------------------------------------------------
# DEFECT 9 — CAP byte-owner inheritance authority. A CAP byte-owner row inherits its
# owned FACT's seven gate values ONLY through its ONE authorized mechanism
# (feature_lineage.CAP_BYTE_OWNER_MECHANISMS): a typed_lineage row matching the exact
# producer/layer/fact binding + registry, or an exact_profile_member layer stamp. An
# unauthorized claim (wrong producer/layer/fact combination, or a profile stamp for a
# member that does not own THIS row's layer) is surfaced as a NAMED ownership rejection
# and never inherits. This mirrors ``_member_delivery_byte_proven``'s admission tests
# exactly, but REPORTS the reject instead of silently declining to inherit.
# ---------------------------------------------------------------------------
BYTE_OWNER_OWNERSHIP_SCHEMA = "gt.byte_owner_ownership.v1"


def _typed_cap_claim_reason(member: object, row: dict[str, Any]) -> str | None:
    """None iff a typed CAP byte-owner ref on ``row`` is authorized; else the named
    reject class. Mirrors ``_member_delivery_byte_proven`` typed_lineage admission."""
    if not isinstance(member, str) or member not in CAP_BYTE_OWNER_IDS:
        return "cap_ref_unknown_member"
    authority = CAP_BYTE_OWNER_MECHANISMS.get(member)
    if authority is None or authority.mechanism != "typed_lineage":
        return "cap_ref_for_non_typed_owner"
    evidence_type = row.get("evidence_type")
    runtime_producer = row.get("runtime_producer_id")
    registered_producer = row.get("registered_producer_id")
    fact_class = row.get("fact_class")
    if not isinstance(evidence_type, str) or not evidence_type:
        return "typed_binding_mismatch"
    fact_registration = registration_for(evidence_type)
    evidence_base = evidence_type.split(":", 1)[0]
    binding_matches = any(
        binding.producer == runtime_producer
        and binding.layer == evidence_base
        and binding.fact_class == fact_class
        for binding in authority.bindings
    )
    if (
        row.get("lineage_schema") != "gt.feature_lineage.v1"
        or row.get("producer_registration_match") is not True
        or fact_registration is None
        or fact_registration.fact_class != fact_class
        or fact_registration.producer != registered_producer
        or not isinstance(runtime_producer, str)
        or not producer_matches(evidence_type, runtime_producer)
        or not binding_matches
    ):
        return "typed_binding_mismatch"
    return None


def _profile_cap_claim_reason(member: str, row: dict[str, Any]) -> str | None:
    """None iff a profile_member stamp is authorized (or is NOT a byte-owner claim at
    all — a reclassified mediator lane stamp such as GT_SS_COHERENCE_V2 keeps its
    ``detect.coherence`` byte stamp under P4 and never inherits FACT gates, so it is
    not adjudicated here). Else the named reject class."""
    if member not in CAP_BYTE_OWNER_IDS:
        return None  # not a byte-owner claim (mediator/eligibility lane stamp) — P4
    authority = CAP_BYTE_OWNER_MECHANISMS.get(member)
    if authority is None or authority.mechanism != "exact_profile_member":
        return "profile_stamp_for_typed_owner"
    layer = str(row.get("layer") or "")
    binding_matches = any(
        binding.layer == layer
        and (
            binding.fact_class is None
            or layer_to_fact_class(layer) == binding.fact_class
        )
        for binding in authority.bindings
    )
    return None if binding_matches else "profile_layer_mismatch"


def byte_owner_ownership_audit(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """DEFECT 9: name every UNAUTHORIZED CAP byte-owner ownership claim in the ledger.

    A delivered row can claim byte ownership two ways: a typed_lineage CAP ``byte_owner``
    feature-ref, or an exact-profile ``profile_member`` stamp. Each claim is authorized
    ONLY through the member's ``CAP_BYTE_OWNER_MECHANISMS`` binding; any other producer/
    layer/fact combination (or a profile stamp for a member that does not own the row's
    layer) is a named rejection. Deterministic, read-only, no inheritance side effect."""
    rejections: list[dict[str, Any]] = []
    for row_index, row in enumerate(rows or ()):
        if not isinstance(row, dict) or row.get("outcome") != "delivered":
            continue
        refs = row.get("feature_ids")
        if isinstance(refs, list):
            for ref in refs:
                if not (
                    isinstance(ref, dict)
                    and ref.get("category") == "CAP"
                    and ref.get("role") == "byte_owner"
                ):
                    continue
                member = ref.get("feature_id")
                reason = _typed_cap_claim_reason(member, row)
                if reason is not None:
                    rejections.append({
                        "row_index": row_index,
                        "member": member if isinstance(member, str) else None,
                        "mechanism": "typed_lineage",
                        "reason": reason,
                        "evidence_type": row.get("evidence_type"),
                        "runtime_producer_id": row.get("runtime_producer_id"),
                        "fact_class": row.get("fact_class"),
                    })
        member = row.get("profile_member")
        if isinstance(member, str) and member:
            reason = _profile_cap_claim_reason(member, row)
            if reason is not None:
                rejections.append({
                    "row_index": row_index,
                    "member": member,
                    "mechanism": "exact_profile_member",
                    "reason": reason,
                    "layer": row.get("layer"),
                    "fact_class": row.get("fact_class"),
                })
    return {
        "schema": BYTE_OWNER_OWNERSHIP_SCHEMA,
        "valid": not rejections,
        "rejection_count": len(rejections),
        "rejections": rejections,
    }


# ---------------------------------------------------------------------------
# Per-task collection.
# ---------------------------------------------------------------------------

def _apply_attestation_truth(
    task_dir: str,
    rows: list[dict],
    fact_lifecycles: dict[str, dict],
    ledger_artifact: str,
) -> dict[str, Any]:
    """SPEC-J2/J2b: populate lifecycle truth AND authority for the attested fact
    classes from the exactly-joined producer attestations, and return the join
    diagnostics.

    Only the classes in :data:`attestation_join.ATTESTED_FACT_CLASSES` may receive joined
    truth. That tuple is the ONE authority and is deliberately NOT restated here: this
    docstring used to enumerate six classes and had gone stale against a tuple of ten
    (``def_partition``, ``localization``, ``newfile_precedent`` and ``obligations`` had since
    grown producers). The stale list read as "these four are structurally ungradable on
    ``correct_info``" — a materially wrong picture of the product's coverage. Truth is
    overridden ONLY when the join produced a bool (a validated attestation joined a
    DELIVERED row on the exact ``(candidate_id, delivery_seal)`` identity). Authority
    (J2b, the second leg of ``correct_info``) is overridden ONLY when the join set it —
    which happens ONLY on a truth-PASS join (``tj.authority`` is ``True`` or ``None``,
    never ``False``); a FAIL/UNMEASURED join leaves ``authority_valid`` at its honest
    hard-wired UNMEASURED. Every other class — and any attested class without a
    validated joined attestation — stays UNMEASURED (the reverted ea0eb16c0 fabrication
    class is NOT reintroduced). Pure and read-only over ``task_dir``.
    """
    load = load_attestations(task_dir)
    joins = join_truth(load.attestations, rows)
    applied: list[str] = []
    applied_authority: list[str] = []
    applied_freshness: list[str] = []
    for fc in ATTESTED_FACT_CLASSES:
        tj = joins.get(fc)
        if tj is None:
            continue
        lifecycle = fact_lifecycles.get(fc)
        if lifecycle is None:
            continue
        # SPEC-J2f (defect-4, run #2): the join carries a FRESHNESS verdict that was
        # previously DISCARDED (only truth/authority were applied). Apply it to the
        # gate-relevant ``stale`` leg — read by ss_gate_readiness.correct_rl_adhered_time
        # and verdict_for. A joined freshness FAIL proves the delivered fact STALE
        # (gate-relevant false); a joined freshness PASS proves it FRESH (stale=False,
        # attestation provenance). An absent/UNMEASURED freshness (None — e.g. the
        # honest-dark localization/covering/submit_refusal freshness) leaves the honest
        # ledger-derived ``stale`` untouched (never fabricates a freshness verdict).
        if isinstance(tj.freshness, bool):
            lifecycle["stale"] = measured(
                not tj.freshness,
                source_artifact="producer_attestations",
                source_messages=[],
            )
            applied_freshness.append(fc)
        if not isinstance(tj.truth, bool):
            continue
        # source_artifact records provenance = the attestation store, not the ledger.
        lifecycle["truth_valid"] = measured(
            tj.truth, source_artifact="producer_attestations", source_messages=[]
        )
        applied.append(fc)
        # SPEC-J2b: authority is the second leg of correct_info. The join grants it
        # (``tj.authority is True``) ONLY on a truth-PASS join — validated,
        # exactly-joined, all-PASS. It is NEVER False (``authority`` is True or None),
        # so this bool guard fires only to set True; a FAIL/UNMEASURED join leaves
        # authority_valid at its honest hard-wired UNMEASURED (fail-closed).
        if isinstance(tj.authority, bool):
            lifecycle["authority_valid"] = measured(
                tj.authority,
                source_artifact="producer_attestations",
                source_messages=[],
            )
            applied_authority.append(fc)
    return {
        "schema": "gt.attestation_join.v1",
        "attestations_loaded": len(load.attestations),
        "load_diagnostics": list(load.diagnostics),
        "joined_fact_classes": {
            fc: truth_join_to_dict(tj) for fc, tj in sorted(joins.items())
        },
        "applied_truth_overrides": sorted(applied),
        "applied_authority_overrides": sorted(applied_authority),
        "applied_freshness_overrides": sorted(applied_freshness),
        "source_artifact": ledger_artifact,
    }


def _runtime_ladder_is_capped() -> bool:
    """True when the runtime may not write any receipt rung above ``delivered``.

    Derived from the ladder authority, never hardcoded: if the emittable set ever regains a
    rung above ``delivered``, a ``delivered`` receipt becomes meaningful disproof again and
    this guard disengages on its own.
    """
    try:
        from groundtruth.runtime.evidence_envelope import (
            RECEIPT_RANK,
            RECEIPT_REFERENCED,
            RUNTIME_EMITTABLE_RECEIPT_STATES,
        )
        return not any(
            RECEIPT_RANK.get(state, 0) >= RECEIPT_RANK[RECEIPT_REFERENCED]
            for state in RUNTIME_EMITTABLE_RECEIPT_STATES
        )
    except Exception:  # noqa: BLE001
        # HONEST ABOUT THE POLARITY: `return False` does NOT fail closed here. It re-arms the
        # sidecar as disproof, which is the exact outcome this helper exists to prevent --
        # reached silently, on an unknown ladder. The correct third answer is UNMEASURED, and
        # a `bool` return cannot express it.
        #
        # Left as-is because the branch is unreachable, not because it is right:
        # `gt_feature_metrics` imports `receipt_sidecar` at module scope, which imports
        # `evidence_envelope` at ITS module scope, so an import failure here means this module
        # never loaded at all. The only live path in is a KeyError from a patched or truncated
        # ladder. If this helper ever grows a tri-state return, this is the branch to fix.
        return False


def _canonical_dark_fallback_assurance(rows: list[dict[str, Any]]) -> str | None:
    """The runtime's own assurance verdict when the canonical observer went dark.

    Returns ``None`` when the ledger carries no ``canonical_runtime.dark_fallback`` row,
    which is the ONLY state in which canonical delivery may be claimed at all.

    Read, never asserted. The row's ``assurance`` is stamped from
    ``attempt_runtime.failure_state`` at the seam, so a component isolated by a NON-core
    fault reports DEGRADED rather than being mislabelled UNASSURED. A pre-flag row (proof
    mode off) carries no ``assurance`` key; a dark observer is definitionally not assured,
    so that case falls back to UNASSURED rather than to silence.

    WORST-WINS, not first-wins. ``apply_failure_policy`` can isolate a component on a
    non-core fault -- leaving emission enabled and assurance DEGRADED -- and a later core
    fault then quarantines to UNASSURED. Rows flow from the first dark observation onward, so
    returning the FIRST stated value publishes DEGRADED for an attempt that ended UNASSURED:
    the milder of the two, which is the wrong direction for a fail-closed field. Rank and take
    the minimum. An unrecognised value sorts worst, so a future member cannot read as mild by
    being unknown to this function.
    """
    order = {"ASSURED": 0, "DEGRADED": 1, "UNASSURED": 2, "BLOCKED": 3}
    worst: str | None = None
    for row in rows:
        if row.get("layer") != "canonical_runtime.dark_fallback":
            continue
        stated = row.get("assurance")
        seen = stated if isinstance(stated, str) and stated else "UNASSURED"
        if worst is None or order.get(seen, 99) > order.get(worst, 99):
            worst = seen
    return worst


def _apply_canonical_runtime_attestation(
    task_dir: str,
    ss_integrity: dict[str, Any],
    rows: list[dict[str, Any]] | None = None,
) -> None:
    """Attach canonical delivery proof without laundering it into consumption.

    Legacy trajectories have no canonical journal and remain byte-for-byte
    unchanged at this projection.  When a journal is present, its read-only
    proof metadata is additive.  A corrupt journal taints required-input
    integrity; a valid provider-terminal delivery remains only delivery proof
    and never supplies acknowledgment, behavioral influence, or causal credit.
    """

    # FAIL CLOSED ON A DARK OBSERVER (2026-07-28). Until now this projection consulted only
    # the journal, so a task whose canonical observer DIED reported nothing and was scored as
    # if the canonical route had simply been quiet. Run 30390877219 shipped 59/38/12/6
    # observations through the untimed legacy route across four tasks and this grader recorded
    # no trace of it: the safety was accidental (we read only DELIVERED rows), not structural.
    #
    # `canonical_delivery_proven` is stamped FIRST and unconditionally, so the key exists even
    # when no journal is present. Without it `delivered_count == 0` passes VACUOUSLY -- the
    # whole `canonical_runtime_attestation` block is absent unless a journal file exists, so an
    # assertion on the numerator alone would be green for a task that never even ran the
    # canonical route. The task stays in the denominator; only the numerator excludes it.
    dark_assurance = _canonical_dark_fallback_assurance(rows or [])
    if dark_assurance is not None:
        ss_integrity["canonical_delivery_proven"] = False
        # THE TAINT IS THE POINT. Writing `canonical_delivery_proven: False` into the JSON and
        # stopping there would be a fail-closed that reaches no gate: `ss_proof_manifest.
        # _audit_feature_metrics` and `ss_live_diagnosis._validate_task_metrics` consult
        # `inventory_complete` / `required_inputs_complete` / `publishable` and nothing else, so
        # a dark-observer task would still pass the manifest audit while carrying a field that
        # says its canonical claim is void. Routing it through `missing_required_inputs` puts it
        # on the ONE path the promotion authority already reads.
        ss_integrity["required_inputs_complete"] = False
        missing = set(ss_integrity.get("missing_required_inputs") or [])
        missing.add("canonical_observer_dark")
        ss_integrity["missing_required_inputs"] = sorted(missing)
    diagnostic = runtime_attestation_diagnostic(task_dir)
    if diagnostic is None:
        return
    ss_integrity["canonical_runtime_attestation"] = diagnostic
    ss_integrity["canonical_delivery_proven"] = bool(
        dark_assurance is None
        and diagnostic.get("integrity_ok") is True
        and int(diagnostic.get("delivered_count") or 0) > 0
    )
    if diagnostic.get("integrity_ok") is not True:
        ss_integrity["required_inputs_complete"] = False
        missing = set(ss_integrity.get("missing_required_inputs") or [])
        missing.add("canonical_runtime_attestation_integrity")
        ss_integrity["missing_required_inputs"] = sorted(missing)


def collect_task(
    task: str,
    task_dir: str,
    *,
    profile: str = "2",
    baseline_root: str | None = None,
    gold_paths: Iterable[str] | None = None,
    paired_baseline: dict[str, bool] | None = None,
) -> dict[str, Any]:
    """Collect the full per-task feature-metrics record (schema gt.feature_metrics.v1).

    Never fabricates: every fact class + every enabled member appears; missing evidence is
    UNMEASURED/NOT_ELIGIBLE; resolution is not consulted for any credit."""
    _, fr = _profile_registry()
    # Terminal live bit: joined ONLY from run-provenance artifacts (seam receipt +
    # workflow-only activation + non-baseline run identity + no replay report), never
    # inferred from delivery quality or gate passage (the fabrication reverted in
    # ea0eb16c0). A replay can mint the receipt, so the receipt alone is insufficient.
    _live = detect_live_run(task_dir)
    _live_witness = _live.verdict == "LIVE_PAID"
    traj_path = _find_one(task_dir, "mini-swe-agent.trajectory.json")
    traj = _load_json(traj_path) if traj_path else None
    if not isinstance(traj, dict):
        traj = {"messages": []}
    ledger_path = _find_one(task_dir, "gt_runtime_ledger_*.jsonl", "gt_runtime_ledger*.jsonl")
    oracle_path = _find_one(task_dir, "gt_oracle_events_*.jsonl")
    rows, malformed_ledger_lines = (
        load_jsonl_strict(ledger_path) if ledger_path else ([], [])
    )
    oracle_rows = load_jsonl(oracle_path) if oracle_path else []

    ledger_artifact = os.path.basename(ledger_path) if ledger_path else "runtime_ledger:absent"
    traj_artifact = os.path.basename(traj_path) if traj_path else "trajectory:absent"

    timeline = _timeline(traj)
    ledger_by_fc = classify_ledger(rows)
    consumption_by_fc, cons_ledger = _consumption_by_fact_class(traj, ledger_path)
    brief_path = _find_named_input(task_dir, "brief_result.json", locations=2)
    brief_payload = _load_json(brief_path) if brief_path else None
    acq_records, acq_missing = _acquisition_feature_records(
        task_dir, cons_ledger, traj
    )
    control_evidence = _control_participation_evidence(
        rows, traj.get("messages", []) or [], cons_ledger,
        brief_payload if isinstance(brief_payload, dict) else None,
    )
    physical_identity_conflicts = _physical_identity_conflicts(
        cons_ledger.get("entries")
    )
    physical_identity_conflicts.update(
        cons_ledger.get("physical_identity_conflict_ids") or []
    )
    visible_audit_complete = (
        _visible_audit_inputs_complete(traj_path, ledger_path)
        and cons_ledger.get("schema") == "gt.consumption_ledger.v2"
        and cons_ledger.get("visible_audit_complete") is True
        and not physical_identity_conflicts
    )
    state_by_fc = state_predicates(timeline, ledger_by_fc)
    infra_signals = _infra_signals(rows, ledger_by_fc)

    _info = traj.get("info")
    submission = str((_info.get("submission") if isinstance(_info, dict) else "") or "")
    has_submission = bool(submission) or any(
        m.get("role") == "exit" for m in traj.get("messages", []) if isinstance(m, dict)
    )
    base_path = _baseline_trajectory_path(task, baseline_root)
    baseline_status = BASELINE_MATCHED if base_path else BASELINE_UNAVAILABLE

    native_visible = native_visible_by_fact_class(rows, traj.get("messages", []) or [])
    native_renderer = native_renderer_audit_by_fact_class(rows)
    # receipt-join COVERAGE (C15 split, 2026-07-28): the per-class count of delivered rows
    # with no model-visible receipt. Without it the ``inert`` verdict cannot tell "did
    # nothing" from "not measurable" — see fact_class_lifecycle.
    unjoined_by_fc = unjoined_receipts_by_fact_class(cons_ledger)
    fact_lifecycles: dict[str, dict] = {}
    for fc in fr.all_fact_classes():
        fact_lifecycles[fc] = fact_class_lifecycle(
            fc, timeline=timeline, ledger_by_fc=ledger_by_fc,
            consumption_by_fc=consumption_by_fc, state_by_fc=state_by_fc,
            oracle_rows=oracle_rows, has_submission=has_submission,
            baseline_status=baseline_status, registry=fr,
            ledger_artifact=ledger_artifact, traj_artifact=traj_artifact,
            native_visible=native_visible.get(fc, 0),
            native_renderer_valid=native_renderer.get(fc),
            unjoined_receipts=unjoined_by_fc.get(fc, 0),
        )

    # SPEC-J2: override lifecycle truth for the four attested fact classes from the
    # producer-attestation → delivered-ledger join (fail-closed; every other class and
    # any unjoined attested class stays UNMEASURED). Must run AFTER the loop so it edits
    # the assembled lifecycles; diagnostics are surfaced into ss_integrity below.
    attestation_join_diag = _apply_attestation_truth(
        task_dir, rows, fact_lifecycles, ledger_artifact
    )

    # SPEC-J3: the chronology timing JOIN. Adjudicate every delivered ledger row against the
    # trajectory (six exact message indices per delivery) into per-fact-class timing verdicts,
    # feeding the ``correct_rl_adhered_time`` gate. Fail-closed: a class with no measured
    # verdict yields ``None`` (``timing_by_fc.get`` below) so the gate stays UNMEASURED — it
    # only tightens the gate to True (ON_TIME) / False (LATE/STEP_BEHIND) when it can prove it.
    chronological_timing = adjudicate_deliveries(traj, rows)
    timing_by_fc = timing_by_fact_class(chronological_timing)

    # B-cluster Gate 4: the registry-specific ACKNOWLEDGMENT join. Run each delivered
    # chronology (whole-row + compound-brief block) through its class's receipt-predicate
    # evaluator and roll up per fact class. Fail-closed: a class with no measured acknowledgment
    # yields None -> the ``acknowledged`` gate then falls back to the receipt ladder (byte-
    # identical to the pre-B path). Uses the SAME chronology/attestation authorities as J3.
    # R1 (2026-07-18): compute the block-grain chronologies ONCE — they feed BOTH the
    # Gate-4 acknowledgment join below and the compound-row byte-proof path (gate 1).
    _block_chronologies = extract_block_chronologies(traj, rows)
    block_byte_proofs = _block_delivery_byte_proofs(rows, _block_chronologies)
    _ack_chronologies = list(extract_chronologies(traj, rows).values())
    _ack_chronologies.extend(_block_chronologies)
    _ack_attestations = load_attestations(task_dir).attestations
    trajectory_acknowledgment_by_fc = acknowledgment_by_fact_class(
        _ack_chronologies,
        messages=(traj.get("messages") if isinstance(traj, dict) else None),
        ledger_rows=rows,
        attestations=_ack_attestations,
    )
    acknowledgment_by_fc, receipt_corroboration = (
        _receipt_corroborated_acknowledgment(
            task,
            task_dir,
            rows,
            _ack_chronologies,
            trajectory_acknowledgment_by_fc,
            messages=(
                traj.get("messages", [])
                if isinstance(traj, dict) and isinstance(traj.get("messages"), list)
                else []
            ),
            attestations=_ack_attestations,
        )
    )

    # SPEC-J4: the fair-probe RESULT join. Turn shadow-holdout rows + the chronology into
    # seal-bound MatchedProbe artifacts adjudicated through chronological_adjudication.adjudicate
    # (the CAUSAL authority), feeding the ``fair_probe`` gate: True only for behavioral CAUSAL;
    # CAUSAL_FORK and CAUSAL_PAIRED are non-behavioral enrichment, False for SELF_LOCALIZED,
    # None (absent)
    # for UNMEASURED. The paired-baseline path takes the
    # baseline verdict as an INPUT (from the caller); absent -> UNMEASURED. Fail-closed: no
    # holdout + no self-acquire + no baseline input -> every class None -> byte-identical gate.
    _pb = paired_baseline if isinstance(paired_baseline, dict) else {}
    _gt_res = _pb.get("gt_resolved")
    _base_res = _pb.get("baseline_resolved")
    fair_probe_join = join_fair_probes(
        traj, rows,
        output_dir=task_dir, task_label=task,
        live_witness=_live_witness,
        gt_resolved=_gt_res if isinstance(_gt_res, bool) else None,
        baseline_resolved=_base_res if isinstance(_base_res, bool) else None,
        # Cluster-4 B2/B3: the covering receipt (targeted_covering_failure) needs the
        # producer attestations already loaded above for the Gate-4 acknowledgment join.
        attestations=_ack_attestations,
    )
    fair_probe_by_fc = fair_probe_bool_by_fact_class(fair_probe_join)

    members = profile_members(profile)
    features: dict[str, dict] = {}
    for m in members:
        features[m] = member_record(
            m, fact_lifecycles, infra_signals, baseline_status,
            ledger_artifact=ledger_artifact, traj_artifact=traj_artifact,
            owner_rows=rows, consumption_ledger=cons_ledger,
        )

    events = _atomic_events(task, rows, ledger_artifact)

    delivered_files: set[str] = set()
    for b in ledger_by_fc.values():
        delivered_files |= set(b.get("delivered_files", set()))
    leaks = set(leak_canary(delivered_files, task, list(gold_paths or [])))
    # The SS leak gate applies to the exact model-visible payload bytes, not
    # merely their file identities. The consumption ledger performs that scan
    # while it seal-joins each delivery; preserve every hit here so the run
    # aggregate fails closed.
    leaks.update(cons_ledger.get("test_identity_leak_hits") or [])

    messages = traj.get("messages", []) or []
    opportunity_projection = collect_feature_opportunities(
        rows,
        messages,
        canonical_feature_inventory(),
    )
    observation_owners = _model_observation_owners(messages)
    physical_by_observation: dict[int, set[str]] = defaultdict(set)
    for entry_index, entry in enumerate(cons_ledger.get("entries") or []):
        if not isinstance(entry, dict) or entry.get("source") != "trajectory":
            continue
        if int(entry.get("receipt") or 0) < 1:
            continue
        msg_index = entry.get("msg_index")
        if isinstance(msg_index, int):
            owner = observation_owners.get(msg_index, msg_index)
            physical_id = str(
                entry.get("physical_id") or f"entry:{entry_index}"
            )
            physical_by_observation[owner].add(physical_id)
    doses_by_observation = Counter({
        owner: len(physical_ids)
        for owner, physical_ids in physical_by_observation.items()
    })
    max_dose = max(doses_by_observation.values(), default=0)
    dose_violations = sum(1 for count in doses_by_observation.values() if count > 1)
    # DEFECT 7 + 8: grade each PHYSICAL_DELIVERY_BOUND fire on its OWN leak span and
    # its OWN observation dose (observation_id where present, legacy owner fallback).
    # A leaking fire's exact tokens join the run-global leak taint (fail-closed), and a
    # per-observation dose violation tightens the run dose gate — the existing task-
    # global scans remain as ADDITIONAL fail-closed conditions, never replaced.
    per_fire = per_fire_gate_grades(cons_ledger, observation_owners)
    leaks.update(per_fire["leak_hits"])
    # DEFECT 9: name every unauthorized CAP byte-owner ownership claim (never inherit).
    byte_owner_ownership = byte_owner_ownership_audit(rows)
    leak_gate: bool | None = not leaks if visible_audit_complete else None
    dose_gate: bool | None = (
        (dose_violations == 0 and per_fire["dose_violation_count"] == 0)
        if visible_audit_complete else None
    )

    # The collector is an artifact grader, not a live-run authority. It exposes
    # the exact gate holes per feature but cannot manufacture the terminal live
    # witness or a causal fair-probe result.
    for member, feature in features.items():
        cap_role = cap_role_for(member)
        if cap_role != "byte_owner":
            feature["ss_readiness"] = _infra_control_readiness(
                member,
                member_fact_classes(member),
                fact_lifecycles,
                ledger_artifact=ledger_artifact,
                control_evidence=control_evidence,
                # DECISION 1: the control's causal enrichment rides the mediated FACT's J4 probe.
                fair_probe_by_fc=fair_probe_by_fc,
                # DECISION 1: parity with byte-owner/FACT terminals so infra_control_complete can
                # reach ss_live under a real live witness (never fabricated — offline stays False).
                live_witness=_live_witness,
            )
            feature["ss_readiness"]["cap_role"] = cap_role
        else:
            byte_proven = _member_delivery_byte_proven(member, rows, cons_ledger)
            feature["ss_readiness"] = ss_gate_readiness(
                feature["lifecycle"],
                byte_proven=byte_proven,
                leak_free=leak_gate,
                dose_ok=dose_gate,
                # SPEC-J4: the byte-owner's fair-probe = the join over its owned fact class(es).
                fair_probe=_member_fair_probe(member, fair_probe_by_fc),
                live_witness=_live_witness,
                # SPEC-J3: the byte-owner's timing = its owned fact class(es), adjudicated.
                chronological_time=_member_chronological_time(member, timing_by_fc),
                # B-cluster Gate 4: the byte-owner's acknowledgment = the receipt-predicate
                # evaluator join over its owned fact class(es).
                acknowledged=_member_acknowledgment(member, acknowledgment_by_fc),
            )
            feature["ss_readiness"]["cap_role"] = cap_role

    fact_readiness: dict[str, dict[str, Any]] = {}
    for fact_class, lifecycle in fact_lifecycles.items():
        if fact_role_for(fact_class) == FACT_ROLE_INTERNAL_SUPPORT:
            fact_readiness[fact_class] = _internal_fact_support_readiness(
                fact_class,
                lifecycle,
                acq_records["cochange_history"],
                ledger_artifact=ledger_artifact,
                live_witness=_live_witness,
                # SPEC-J4: inherit the causal verdict of the FACT class the cochange
                # component contributed to (its sealed localization candidate).
                fair_probe_by_fc=fair_probe_by_fc,
            )
        else:
            fact_readiness[fact_class] = ss_gate_readiness(
                lifecycle,
                byte_proven=_fact_delivery_byte_proven(
                    fact_class, rows, cons_ledger,
                    block_byte_proofs=block_byte_proofs,
                ),
                leak_free=leak_gate,
                dose_ok=dose_gate,
                # SPEC-J4: the seal-bound causal RESULT for this fact class (matched/shadow probe
                # adjudicated through chronological_adjudication.adjudicate, or the paired-baseline
                # path). Still NEVER inferred from instrument presence or gate quality — None when
                # no probe adjudicated (fail-closed).
                fair_probe=fair_probe_by_fc.get(fact_class),
                # Offline evidence never sets the live bit: gates 1-6 passing does
                # not distinguish a paid trajectory from a replay/fixture. The bit is
                # joined ONLY from run-provenance artifacts (detect_live_run above).
                live_witness=_live_witness,
                # SPEC-J3: per-fact-class timing verdict from the chronology join. None (an
                # unmeasured class) leaves correct_rl_adhered_time as before (fail-closed).
                chronological_time=timing_by_fc.get(fact_class),
                # B-cluster Gate 4: per-fact-class registry acknowledgment. None -> the
                # ``acknowledged`` gate falls back to the receipt ladder (fail-closed).
                acknowledged=acknowledgment_by_fc.get(fact_class),
            )

    endpoints = behavioural_endpoints(timeline)
    baseline_endpoints = None
    if base_path:
        base_traj = _load_json(base_path)
        if isinstance(base_traj, dict):
            baseline_endpoints = behavioural_endpoints(_timeline(base_traj))

    fc_json = {
        fc: {k: v for k, v in lc.items() if not k.startswith("_")}
        for fc, lc in fact_lifecycles.items()
    }
    ss_features, ss_integrity = _canonical_task_features(
        task, task_dir, features, fc_json, fact_readiness,
        opportunity_projection["features"],
        acq_records,
        acq_missing,
        leak_free=leak_gate, dose_ok=dose_gate,
        live_witness=_live_witness,
        fair_probe_by_fc=fair_probe_by_fc,
        timing_by_fc=timing_by_fc,
    )
    # Full provenance object for audit: which artifacts proved (or failed to prove)
    # the terminal live bit, and every named fail-closed reason.
    ss_integrity["live_run_provenance"] = _live.as_dict()
    ss_integrity["attestation_join"] = attestation_join_diag
    _apply_canonical_runtime_attestation(task_dir, ss_integrity, rows)
    # Top-level so no reader has to know the ss_integrity layout to learn that a task's
    # canonical claim is void.
    #
    # READ THE POLARITY CAREFULLY -- "UNKNOWN" IS THE GOOD STATE HERE.
    #   "UNKNOWN"  -> no dark_fallback row: the canonical observer never went dark. Nothing is
    #                 CLAIMED about delivery either way; that is `canonical_delivery_proven`'s
    #                 job. This is the healthy value.
    #   anything   -> the observer went dark and this is the WORST assurance the runtime
    #     else       reported. The canonical claim is void.
    # "ASSURED" is unreachable on this field BY CONSTRUCTION: it is set only by `initial()`
    # and by a recovery that empties `isolated_components` and re-enables emission, and both
    # imply `_canonical_observer_is_dark` is False, so no dark row can ever carry it. A
    # downstream contract of the form `assurance == "ASSURED" => success` would therefore
    # never fire for any task. The correct test is `canonical_assurance == "UNKNOWN"`.
    #
    # It is still emitted explicitly rather than omitted: an absent key lets a reader supply
    # its own optimistic default, and absence of proof must never read as proof of absence.
    canonical_assurance = _canonical_dark_fallback_assurance(rows) or "UNKNOWN"
    # SPEC-J3: per-fact-class timing verdicts + UNMEASURED reasons feeding the
    # correct_rl_adhered_time gate (the delivery-row chronology join).
    ss_integrity["chronological_timing"] = chronological_timing
    ss_integrity["receipt_corroboration"] = receipt_corroboration
    if receipt_corroboration["integrity_ok"] is not True:
        ss_integrity["required_inputs_complete"] = False
        missing = set(ss_integrity.get("missing_required_inputs") or [])
        missing.add("receipt_corroboration_integrity")
        ss_integrity["missing_required_inputs"] = sorted(missing)
    # SPEC-J4: the fair-probe result join (per-fact-class verdicts + the seal-bound probe audit
    # trail + the sealed sidecar path) feeding the ``fair_probe`` gate.
    ss_integrity["fair_probe"] = fair_probe_join
    ss_integrity["feature_opportunity"] = opportunity_projection["integrity"]
    if opportunity_projection["integrity"]["publishable"] is not True:
        ss_integrity["required_inputs_complete"] = False
        missing = set(ss_integrity.get("missing_required_inputs") or [])
        missing.add("feature_opportunity_integrity")
        ss_integrity["missing_required_inputs"] = sorted(missing)
    ss_integrity["visible_audit_complete"] = visible_audit_complete
    ss_integrity["physical_identity_conflict_count"] = len(
        physical_identity_conflicts
    )
    ss_integrity["physical_identity_conflict_ids"] = sorted(
        physical_identity_conflicts
    )
    if not visible_audit_complete:
        ss_integrity["required_inputs_complete"] = False
        missing = set(ss_integrity.get("missing_required_inputs") or [])
        missing.add("visible_audit")
        ss_integrity["missing_required_inputs"] = sorted(missing)
    if malformed_ledger_lines or not control_evidence["valid"]:
        ss_integrity["required_inputs_complete"] = False
        missing = set(ss_integrity.get("missing_required_inputs") or [])
        missing.add("control_participation_integrity")
        ss_integrity["missing_required_inputs"] = sorted(missing)
    # DEFECT 7 + 8: the itemized per-fire gate rows (leak span + observation dose).
    ss_integrity["per_fire_gate"] = per_fire
    # DEFECT 9: named CAP byte-owner ownership rejections (unauthorized inheritance).
    ss_integrity["byte_owner_ownership"] = byte_owner_ownership
    # A leaking or double-dosed fire, or an unauthorized ownership claim, is a
    # fail-closed run-integrity fault (named culprit), consistent with the run-global
    # leak/dose taints already folded into leak_gate/dose_gate above.
    if per_fire["leaking_fire_count"] or per_fire["dose_violation_count"]:
        ss_integrity["required_inputs_complete"] = False
        missing = set(ss_integrity.get("missing_required_inputs") or [])
        if per_fire["leaking_fire_count"]:
            missing.add("per_fire_leak")
        if per_fire["dose_violation_count"]:
            missing.add("per_fire_dose")
        ss_integrity["missing_required_inputs"] = sorted(missing)
    if not byte_owner_ownership["valid"]:
        ss_integrity["required_inputs_complete"] = False
        missing = set(ss_integrity.get("missing_required_inputs") or [])
        missing.add("byte_owner_ownership")
        ss_integrity["missing_required_inputs"] = sorted(missing)
    return {
        "schema": "gt.feature_metrics.v1",
        "grader_version": GRADER_VERSION,
        "profile": profile,
        "task": task,
        "attempt": 1,
        "canonical_assurance": canonical_assurance,
        "artifacts": {
            "trajectory": traj_artifact,
            "runtime_ledger": ledger_artifact,
            "oracle_events": os.path.basename(oracle_path) if oracle_path else None,
            "baseline_trajectory": base_path,
        },
        "features": features,
        "fact_classes": fc_json,
        "ss_feature_inventory_schema": "gt.ss_feature_inventory.v1",
        "ss_features": ss_features,
        "ss_integrity": ss_integrity,
        # task #34: the gate-loss diagnostic. TOP-LEVEL and OUTSIDE the legacy golden
        # projection on purpose; None when the task has no oracle telemetry.
        "oracle_gate_losses": oracle_gate_losses(oracle_rows),
        "atomic_events": [e.to_json() for e in events],
        "behavioural_endpoints": {
            "gt_on": endpoints,
            "baseline": baseline_endpoints,
            "baseline_status": baseline_status,
        },
        "integrity": {
            "enabled_members": members,
            "members_present": sorted(features),
            "all_members_present": sorted(features) == sorted(members),
            "leak_canary": sorted(leaks),
            "leak_count": len(leaks),
            "max_dose_per_observation": max_dose,
            "dose_violation_count": dose_violations,
            # DEFECT 7/8/9 per-fire + ownership rollups live in ``ss_integrity``
            # (the additive SS surface) so the legacy ``integrity`` projection stays
            # byte-identical; see ss_integrity.per_fire_gate / byte_owner_ownership.
            "consumption_schema": cons_ledger.get("schema"),
            "ledger_rows": len(rows),
            **({"runtime_ledger_malformed_lines": malformed_ledger_lines}
               if malformed_ledger_lines else {}),
            **({
                "control_participation_valid": (
                    not malformed_ledger_lines and control_evidence["valid"]
                ),
                "control_participation_invalid_rows": control_evidence["invalid_rows"],
                "control_participation_invalid_brief_rows": control_evidence["invalid_brief_rows"],
            } if any(
                row.get("layer") == "control.participation"
                or row.get("schema") == CONTROL_PARTICIPATION_SCHEMA
                for row in rows
            ) or malformed_ledger_lines or brief_payload is not None else {}),
        },
    }


def _atomic_row_identity(row: dict[str, Any]) -> tuple[str, str] | None:
    """Validate an exact joint CAP-byte-owner/FACT identity carried by one row.

    Layer names and profile membership are not producer authority. Typed owners
    must match the registry and the byte-owner binding; exact-profile owners must
    additionally name their active member and registered FACT lineage. A factless
    exact-profile owner (coherence) is absent from this FACT event projection;
    its own CAP lifecycle is collected separately.
    """
    if row.get("lineage_schema") != "gt.feature_lineage.v1":
        return None
    evidence_type = row.get("evidence_type")
    runtime_producer = row.get("runtime_producer_id")
    if not isinstance(evidence_type, str) or not evidence_type:
        return None
    fact_class = row.get("fact_class")
    if not isinstance(fact_class, str) or not fact_class:
        return None
    registration = (
        registration_for(evidence_type) if isinstance(evidence_type, str) else None
    )
    if (
        row.get("producer_registration_match") is not True
        or registration is None
        or registration.fact_class != fact_class
        or registration.producer != row.get("registered_producer_id")
        or not isinstance(runtime_producer, str)
        or not producer_matches(evidence_type, runtime_producer)
    ):
        return None
    refs = row.get("feature_ids")
    if not isinstance(refs, list) or not any(
        isinstance(ref, dict)
        and ref.get("category") == "FACT"
        and ref.get("feature_id") == fact_class
        and ref.get("role") == "fact"
        for ref in refs
    ):
        return None
    cap_refs = [
        feature_id for ref in refs
        if isinstance(ref, dict)
        and ref.get("category") == "CAP"
        and ref.get("role") == "byte_owner"
        and isinstance((feature_id := ref.get("feature_id")), str)
        and feature_id
    ]
    if len(cap_refs) == 1:
        member = cap_refs[0]
        authority = CAP_BYTE_OWNER_MECHANISMS.get(member)
        evidence_base = evidence_type.split(":", 1)[0]
        if authority is not None and authority.mechanism == "typed_lineage" and any(
            binding.producer == runtime_producer
            and binding.layer == evidence_base
            and binding.fact_class == fact_class
            for binding in authority.bindings
        ):
            return str(member), str(fact_class)
        return None
    if cap_refs:
        return None
    member = row.get("profile_member")
    if not isinstance(member, str) or not member:
        return None
    authority = CAP_BYTE_OWNER_MECHANISMS.get(member)
    layer = str(row.get("layer") or "")
    if authority is not None and authority.mechanism == "exact_profile_member" and any(
        binding.layer == layer and binding.fact_class == fact_class
        for binding in authority.bindings
    ):
        return str(member), str(fact_class)
    return None


def _atomic_events(task: str, rows: list[dict], ledger_artifact: str) -> list[FeatureMetricEvent]:
    """One event per delivered row with exact typed byte-owner authority."""
    events: list[FeatureMetricEvent] = []
    counter: Counter = Counter()
    for idx, r in enumerate(rows):
        if str(r.get("outcome") or "") != "delivered":
            continue
        identity = _atomic_row_identity(r)
        if identity is None:
            continue
        member, fc = identity
        counter[(member, fc)] += 1
        chars = int(r.get("chars_delivered") or 0)
        events.append(FeatureMetricEvent(
            task=task, attempt=1, feature=member, fact_class=fc,
            delivery_instance=counter[(member, fc)],
            dedup_key=fc + ":" + str(r.get("file_path") or "") + ":" + str(chars),
            unresolved_decision="", ledger_index=idx, observation_message=None,
            source_artifact=ledger_artifact, role=member_role(member),
            lifecycle={"delivered": True, "chars": chars,
                       "boundary": r.get("event_type") or ""},
        ))
    return events


# ---------------------------------------------------------------------------
# Writers.
# ---------------------------------------------------------------------------

def _cell(metric: dict | None) -> str:
    """Render a MetricValue for the markdown table: the value when MEASURED, else the status
    token (NOT_ELIGIBLE / UNMEASURED) — a hole never masquerades as a number."""
    if not isinstance(metric, dict):
        return "-"
    st = metric.get("status")
    if st == "MEASURED":
        v = metric.get("value")
        if isinstance(v, bool):
            return "Y" if v else "N"
        if isinstance(v, float):
            return "%.8f" % v
        return str(v)
    if st == "NOT_ELIGIBLE":
        return "n/e"
    return "unmeas"


def render_feature_table(record: dict) -> str:
    """The per-feature run table (markdown). Columns per the handoff."""
    cols = ["Feature", "Role", "Eligible", "Correct", "Delivered", "Consumed",
            "State changed", "Baseline/holdout", "Steps saved", "GT tokens", "Harm", "Verdict"]
    lines = ["| " + " | ".join(cols) + " |",
             "|" + "|".join(["---"] * len(cols)) + "|"]
    for member in sorted(record.get("features", {})):
        rec = record["features"][member]
        lc = rec["lifecycle"]
        row = [
            member, rec["role"],
            _cell(lc.get("eligible")), _cell(lc.get("truth_valid")),
            _cell(lc.get("delivered")), _cell(lc.get("receipt_level")),
            _cell(lc.get("state_changed")), _cell(lc.get("baseline_or_holdout_status")),
            _cell(lc.get("steps_saved")), _cell(lc.get("dose_tokens")),
            _cell(lc.get("harmful")), rec["verdict"],
        ]
        lines.append("| " + " | ".join(row) + " |")
    return "\n".join(lines) + "\n"


def write_task_metrics(record: dict, out_dir: str) -> tuple[str, str]:
    """Write gt_feature_metrics_<task>.json + a companion .md feature table."""
    task = record["task"]
    os.makedirs(out_dir, exist_ok=True)
    jpath = os.path.join(out_dir, "gt_feature_metrics_" + task + ".json")
    with open(jpath, "w", encoding="utf-8") as f:
        json.dump(record, f, indent=2)
    mpath = os.path.join(out_dir, "gt_feature_metrics_" + task + ".md")
    with open(mpath, "w", encoding="utf-8") as f:
        f.write("# GT feature metrics - " + task + " (profile " + str(record.get("profile")) + ")\n\n")
        f.write(render_feature_table(record))
    return jpath, mpath


# ---------------------------------------------------------------------------
# Fail-closed run aggregate.
# ---------------------------------------------------------------------------

def aggregate_run(
    run_id: str,
    task_records: list[dict],
    profile: str = "2",
    *,
    run_metrics_artifact: str | None = None,
    expected_task_ids: Iterable[str] | None = None,
) -> dict[str, Any]:
    """Aggregate per-task records into the three run-level artifacts.

    REFUSES (integrity.publishable=False) if ANY enabled feature record is absent from ANY
    task — a missing record is a COLLECTION FAILURE, not an implicit zero. eligible=0 is a
    valid record; a MISSING record is not. The CLI turns publishable=False into a nonzero
    exit."""
    enabled = profile_members(profile)
    observed_task_ids = [record.get("task") for record in task_records]
    if any(not isinstance(task, str) or not task for task in observed_task_ids):
        raise ValueError("task records must carry non-empty string identities")
    observed_counts = Counter(observed_task_ids)
    explicit_expected_population = expected_task_ids is not None
    if isinstance(expected_task_ids, (str, bytes)):
        raise ValueError("expected task population must be a sequence of task identities")
    expected = (
        list(expected_task_ids) if expected_task_ids is not None
        else [str(task) for task in observed_task_ids]
    )
    if (
        (explicit_expected_population and not expected)
        or any(not isinstance(task, str) or not task for task in expected)
        or len(expected) != len(set(expected))
    ):
        raise ValueError("expected task population must contain unique non-empty strings")
    expected_set = set(expected)
    missing_task_records = [task for task in expected if task not in observed_counts]
    unexpected_task_records = sorted(
        str(task) for task in observed_counts if task not in expected_set
    )
    duplicate_task_records = sorted(
        str(task) for task, count in observed_counts.items() if count > 1
    )
    population_complete = not (
        missing_task_records or unexpected_task_records or duplicate_task_records
    )
    # Run-scope live witness (J1 authority, detect_live_run per collect_task): the run
    # aggregate binds the WHOLE declared population, so it is live only if the population
    # is complete AND every observed task record carries a LIVE_PAID provenance verdict.
    # Absent, NOT_LIVE, or malformed provenance on any task fails closed — never
    # default-True, mirroring the per-task witness that collect_task computed.
    run_live_witness = bool(
        task_records
        and population_complete
        and all(
            isinstance(rec.get("ss_integrity"), dict)
            and isinstance(rec["ss_integrity"].get("live_run_provenance"), dict)
            and rec["ss_integrity"]["live_run_provenance"].get("verdict") == "LIVE_PAID"
            for rec in task_records
        )
    )
    run_metrics: dict[str, Any] = {
        "schema": "gt.feature_metrics.run.v1", "grader_version": GRADER_VERSION,
        "run_id": run_id, "profile": profile, "n_tasks": len(expected),
        "observed_task_count": len(task_records), "expected_task_ids": expected,
        "features": {},
    }
    effects: dict[str, Any] = {
        "schema": "gt.feature_effects.v1", "grader_version": GRADER_VERSION,
        "run_id": run_id, "profile": profile, "matched_baseline_tasks": [],
    }
    integrity: dict[str, Any] = {
        "schema": "gt.metric_integrity.v1", "grader_version": GRADER_VERSION,
        "run_id": run_id, "profile": profile, "enabled_members": enabled,
        "missing_records": [], "leak_total": 0, "dose_violation_total": 0,
        "missing_task_records": missing_task_records,
        "unexpected_task_records": unexpected_task_records,
        "duplicate_task_records": duplicate_task_records,
        "reconciliation": {}, "publishable": True,
    }
    canonical = canonical_feature_inventory()
    perf_contracts = {
        name: (section, value_type)
        for section, definitions in performance_metric_definitions().items()
        for name, value_type in definitions
    }
    ss_run_features: dict[str, dict[str, Any]] = {}
    ss_integrity: dict[str, Any] = {
        "schema": "gt.ss_feature_inventory.integrity.v1",
        "expected_family_counts": {family: len(names) for family, names in canonical.items()},
        "expected_feature_count": 129,
        "missing_records": [],
        "missing_task_records": missing_task_records,
        "unexpected_task_records": unexpected_task_records,
        "duplicate_task_records": duplicate_task_records,
        "perf_aggregate_failures": [],
        "tasks_with_inventory_drift": [],
        "tasks_with_incomplete_inputs": [],
        "publishable": bool(task_records) and population_complete,
    }
    if not population_complete:
        integrity["publishable"] = False

    from groundtruth.runtime import fact_registry as _fr_grain  # ITEM 3: pure grain accessor
    for family, names in canonical.items():
        for name in names:
            statuses: Counter = Counter()
            present = 0
            task_feature_rows: list[dict[str, Any]] = []
            for rec in task_records:
                feature = rec.get("ss_features", {}).get(name)
                if not isinstance(feature, dict) or feature.get("family") != family:
                    ss_integrity["missing_records"].append({
                        "task": rec.get("task"), "family": family, "feature": name,
                    })
                    ss_integrity["publishable"] = False
                    continue
                present += 1
                task_feature_rows.append({**feature, "_task": rec.get("task")})
                statuses[str(feature.get("status") or "UNMEASURED")] += 1
            ss_run_features[name] = {
                "family": family,
                "present_in_tasks": present,
                "statuses": dict(statuses),
            }
            if family == "FACT":
                # ITEM 3 (2026-07-18): surface the finer producer/evidence-type GRAIN alongside
                # the canonical fact_class so a loc_reslot audit can tell (e.g.) trace_frame from
                # ranked_localization even though both collapse to fact_class ``localization``.
                # Registry-derived; the canonical §1 mapping is UNCHANGED.
                ss_run_features[name]["evidence_grain"] = list(
                    _fr_grain.evidence_grain_for(name)
                )
            if family == "PERF" and task_feature_rows:
                section, value_type = perf_contracts[name]
                if value_type == "run_ratio":
                    aggregate_record = _run_ratio_feature_record(
                        run_id,
                        run_metrics_artifact,
                        section=section,
                        name=name,
                        value_type=value_type,
                        expected_tasks=len(expected),
                    )
                    aggregate_coverage = aggregate_record["aggregate_coverage_valid"]
                else:
                    aggregate_record = _run_distribution_feature_record(
                        run_id,
                        run_metrics_artifact,
                        task_feature_rows,
                        section=section,
                        name=name,
                        value_type=value_type,
                        expected_tasks=len(expected),
                    )
                    aggregate_coverage = aggregate_record["aggregate_coverage_valid"]
                if not aggregate_coverage:
                    ss_integrity["perf_aggregate_failures"].append(name)
                    ss_integrity["publishable"] = False
                ss_run_features[name]["measurement"] = aggregate_record
                ss_run_features[name]["ss_readiness"] = _measurement_only_readiness(
                    aggregate_record,
                    aggregate_coverage=aggregate_coverage,
                    live_witness=run_live_witness,
                )
    for rec in task_records:
        if not rec.get("ss_integrity", {}).get("inventory_complete", False):
            ss_integrity["tasks_with_inventory_drift"].append(rec.get("task"))
            ss_integrity["publishable"] = False
        if not rec.get("ss_integrity", {}).get("required_inputs_complete", False):
            ss_integrity["tasks_with_incomplete_inputs"].append(rec.get("task"))
            ss_integrity["publishable"] = False
    ss_integrity["tasks_with_incomplete_inputs"] = sorted(
        str(task) for task in ss_integrity["tasks_with_incomplete_inputs"]
    )
    ss_integrity["tasks_with_inventory_drift"] = sorted(
        str(task) for task in ss_integrity["tasks_with_inventory_drift"]
    )
    ss_integrity["missing_records"] = sorted(
        ss_integrity["missing_records"],
        key=lambda row: (str(row.get("task")), row["family"], row["feature"]),
    )
    ss_integrity["perf_aggregate_failures"] = sorted(
        set(ss_integrity["perf_aggregate_failures"])
    )
    run_metrics["ss_feature_inventory_schema"] = "gt.ss_feature_inventory.run.v1"
    run_metrics["ss_features"] = ss_run_features

    for member in enabled:
        verdicts: Counter = Counter()
        eligible = delivered = state_changed = 0
        present_in = 0
        for rec in task_records:
            feats = rec.get("features", {})
            if member not in feats:
                integrity["missing_records"].append({"task": rec.get("task"), "member": member})
                integrity["publishable"] = False
                continue
            present_in += 1
            fr_ = feats[member]
            verdicts[fr_["verdict"]] += 1
            lc = fr_["lifecycle"]
            if _val(lc.get("eligible")) is True:
                eligible += 1
            if _val(lc.get("delivered")) is True:
                delivered += 1
            if _val(lc.get("state_changed")) is True:
                state_changed += 1
        run_metrics["features"][member] = {
            "role": member_role(member),
            "present_in_tasks": present_in,
            "eligible_tasks": eligible,
            "delivered_tasks": delivered,
            "state_changed_tasks": state_changed,
            "verdicts": dict(verdicts),
        }

    for rec in task_records:
        be = rec.get("behavioural_endpoints", {})
        if be.get("baseline_status") != BASELINE_MATCHED or not be.get("baseline"):
            continue
        g, b = be["gt_on"], be["baseline"]

        def _delta(k, g=g, b=b):
            gv, bv = g.get(k), b.get(k)
            if gv is None or bv is None:
                return None
            return bv - gv  # positive = GT fewer/faster

        effects["matched_baseline_tasks"].append({
            "task": rec.get("task"),
            "steps_to_first_edit": {"gt_on": g.get("steps_to_first_edit"),
                                    "baseline": b.get("steps_to_first_edit"),
                                    "delta_baseline_minus_gt": _delta("steps_to_first_edit")},
            "total_steps": {"gt_on": g.get("total_steps"), "baseline": b.get("total_steps"),
                            "delta_baseline_minus_gt": _delta("total_steps")},
            "files_viewed_before_first_edit": {
                "gt_on": g.get("files_viewed_before_first_edit"),
                "baseline": b.get("files_viewed_before_first_edit"),
                "delta_baseline_minus_gt": _delta("files_viewed_before_first_edit")},
            "search_count": {"gt_on": g.get("search_count"), "baseline": b.get("search_count"),
                             "delta_baseline_minus_gt": _delta("search_count")},
        })

    leak_total = 0
    dose_violation_total = 0
    atomic_total = 0
    for rec in task_records:
        leak_total += rec.get("integrity", {}).get("leak_count", 0)
        dose_violation_total += rec.get("integrity", {}).get("dose_violation_count", 0)
        atomic_total += len(rec.get("atomic_events", []))
        if not rec.get("integrity", {}).get("all_members_present", False):
            integrity["publishable"] = False
    integrity["leak_total"] = leak_total
    if leak_total > 0:
        integrity["publishable"] = False
    integrity["dose_violation_total"] = dose_violation_total
    if dose_violation_total > 0:
        integrity["publishable"] = False
    if not ss_integrity["publishable"]:
        integrity["publishable"] = False
    integrity["reconciliation"] = {
        "atomic_events_total": atomic_total,
        "tasks_reconciled": len(task_records),
        "tasks_expected": len(expected),
        "enabled_member_count": len(enabled),
    }
    integrity["ss_129"] = ss_integrity
    return {
        "run_metrics": run_metrics,
        "effects": effects,
        "integrity": integrity,
        "ss_integrity": ss_integrity,
    }


def write_run_aggregate(run_id: str, agg: dict, out_dir: str) -> dict[str, str]:
    os.makedirs(out_dir, exist_ok=True)
    paths = {}
    for key, fname in (
        ("run_metrics", "gt_run_metrics_" + run_id + ".json"),
        ("effects", "gt_feature_effects_" + run_id + ".json"),
        ("integrity", "gt_metric_integrity_" + run_id + ".json"),
    ):
        p = os.path.join(out_dir, fname)
        with open(p, "w", encoding="utf-8") as f:
            json.dump(agg[key], f, indent=2)
        paths[key] = p
    return paths


# ---------------------------------------------------------------------------
# CLI.
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# W1b — live-bit ORDERING fix. detect_live_run scans <task_dir> + <task_dir>/
# gt_artifacts, but the live workflow writes the run-provenance identity artifacts
# to the run's SHARED agent-output dir (/gt_out) and copies them into
# trial_results/gt_artifacts only AFTER this in-container create pass runs — so the
# collect evaluated provenance on a task dir that did not yet carry them and SEALED
# the live bit false-dark (D9: the sealed record is authoritative, never rewritten).
# This co-locates the three positive identity artifacts into <task_dir>/gt_artifacts
# BEFORE detect_live_run, so the live bit is sealed against the complete set. It only
# MOVES artifacts the run already produced next to the record; it changes no truth and
# NEVER defaults to LIVE. Create pass only (never the --out re-grade → D9 preserved).
# ---------------------------------------------------------------------------
_PROVENANCE_STAGE_ARTIFACTS = (
    "gt_profile_activation.json",  # workflow-only witness (the replay discriminator)
    "gt_profile_receipt.json",     # in-seam attach witness
    "gt_run_identity.json",        # non-baseline attestation
)


def stage_provenance_artifacts(task_dir: str, shared_dir: str | None) -> dict[str, str]:
    """Co-locate the run-provenance identity artifacts from the run's SHARED output dir
    into ``<task_dir>/gt_artifacts`` so ``detect_live_run`` seals the live bit against
    the complete set. Ordering fix, not a truth change.

    Fail-closed / non-fabricating (NEVER default-LIVE):
      * copies ONLY a source that EXISTS and parses as a JSON object — a missing or
        malformed source is skipped, leaving ``detect_live_run`` to fail closed;
      * NEVER overwrites an already co-located artifact (idempotent; preserves the
        authoritative assembled copy and never manufactures a two-location conflict);
      * stages ONLY the three positive identity artifacts — never a replay report, so
        ``replay_excluded`` stays honest;
      * synthesizes NOTHING: every staged byte is a byte-for-byte copy of a real
        produced artifact.

    Returns ``{name: disposition}`` (``staged`` / ``present`` / ``absent`` /
    ``malformed`` / ``no_shared_dir``) for the integrity trail. Copies nothing when
    ``shared_dir`` is falsy or is not an existing directory.
    """
    if not shared_dir or not os.path.isdir(shared_dir):
        return {name: "no_shared_dir" for name in _PROVENANCE_STAGE_ARTIFACTS}
    dest_base = os.path.join(task_dir, "gt_artifacts")
    dispositions: dict[str, str] = {}
    for name in _PROVENANCE_STAGE_ARTIFACTS:
        # already co-located (top-level task dir OR gt_artifacts) → never clobber the
        # authoritative copy, and never introduce a differing second copy (ambiguity).
        if os.path.isfile(os.path.join(dest_base, name)) or os.path.isfile(
            os.path.join(task_dir, name)
        ):
            dispositions[name] = "present"
            continue
        src = os.path.join(shared_dir, name)
        if not os.path.isfile(src):
            dispositions[name] = "absent"
            continue
        try:
            with open(src, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            dispositions[name] = "malformed"
            continue
        if not isinstance(data, dict):
            dispositions[name] = "malformed"
            continue
        os.makedirs(dest_base, exist_ok=True)
        shutil.copyfile(src, os.path.join(dest_base, name))  # byte-exact, no re-serialize
        dispositions[name] = "staged"
    return dispositions


def _iter_task_dirs(run_dir: str) -> list[tuple[str, str]]:
    """(task, dir) for every subdir of run_dir that holds a mini trajectory."""
    out: list[tuple[str, str]] = []
    for name in sorted(os.listdir(run_dir)):
        d = os.path.join(run_dir, name)
        if os.path.isdir(d) and _find_one(d, "mini-swe-agent.trajectory.json"):
            task = name[len("ll-full-"):] if name.startswith("ll-full-") else name
            out.append((task, d))
    return out


def main(argv: list[str] | None = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="Per-feature GT behavioural-contract metrics.")
    ap.add_argument("run_dir", help="directory of per-task artifact subdirs")
    ap.add_argument("--profile", default="2")
    ap.add_argument("--baseline-root", default=None)
    ap.add_argument("--run-id", default=None)
    ap.add_argument("--out", default=None, help="run-level output dir (default: run_dir)")
    ap.add_argument(
        "--run-metrics-artifact", default=None,
        help="validated gt_run_metrics.v2 input for run-ratio PERF rows",
    )
    ap.add_argument(
        "--expected-tasks-file", default=None,
        help="JSON task population; absent task records remain an integrity failure",
    )
    ap.add_argument(
        "--shared-artifacts-dir",
        default=os.environ.get("GT_SHARED_ARTIFACTS_DIR") or None,
        help="run's SHARED agent-output dir (e.g. /gt_out) holding the run-provenance "
        "identity artifacts; the CREATE pass (no --out) co-locates them into each task "
        "dir BEFORE the live bit is sealed (W1b ordering fix). Fail-closed; never the "
        "--out re-grade pass (D9 completion-hash binding preserved).",
    )
    args = ap.parse_args(argv)

    run_dir = args.run_dir
    if not os.path.isdir(run_dir):
        print("gt_feature_metrics: run_dir not found: " + run_dir, file=sys.stderr)
        return 2
    run_id = args.run_id or os.path.basename(os.path.normpath(run_dir))
    out_dir = args.out or run_dir

    expected_task_ids = None
    if args.expected_tasks_file is not None:
        expected_payload = _load_json(args.expected_tasks_file)
        expected_task_ids = (
            expected_payload.get("task_ids")
            if isinstance(expected_payload, dict) else None
        )
        if not isinstance(expected_task_ids, list) or not expected_task_ids:
            print("gt_feature_metrics: malformed expected-task manifest", file=sys.stderr)
            return 2

    records: list[dict] = []
    for task, d in _iter_task_dirs(run_dir):
        # W1b: on the CREATE pass ONLY (never the --out re-grade, which must leave the
        # sealed task dir byte-untouched — D9), co-locate the run-provenance identity
        # artifacts from the shared output dir into <d>/gt_artifacts BEFORE collect_task
        # evaluates detect_live_run. Fail-closed: absent/malformed sources are skipped
        # and the live bit stays NOT_LIVE.
        if args.out is None and args.shared_artifacts_dir:
            stage_provenance_artifacts(d, args.shared_artifacts_dir)
        rec = collect_task(task, d, profile=args.profile, baseline_root=args.baseline_root)
        # With --out, per-task records are written UNDER out_dir and the task dir is
        # left byte-untouched: the in-container gt_task_completion.json seals the hash
        # of the task dir's gt_feature_metrics_<task>.json, and an in-place re-grade
        # (the summarize diagnosis pass) would break that binding for EVERY task
        # (live witness: run 29553735978 — 30/30 'incomplete', publishable=false,
        # while the same sealed artifacts evaluated without the in-place rewrite are
        # publishable=true). Without --out (the in-container per-task collection that
        # CREATES the sealed record), behavior is unchanged.
        write_task_metrics(rec, d if args.out is None else out_dir)
        records.append(rec)

    if not records and expected_task_ids is None:
        print("gt_feature_metrics: no task dirs with a mini trajectory", file=sys.stderr)
        return 2
    agg = aggregate_run(
        run_id,
        records,
        profile=args.profile,
        run_metrics_artifact=args.run_metrics_artifact,
        expected_task_ids=expected_task_ids,
    )
    paths = write_run_aggregate(run_id, agg, out_dir)
    publishable = (
        agg["integrity"]["publishable"]
        and agg["ss_integrity"]["publishable"]
    )
    print("gt_feature_metrics: " + str(len(records)) + " tasks, publishable=" + str(publishable))
    for k, p in paths.items():
        print("  " + k + ": " + p)
    if not publishable:
        print("gt_feature_metrics: INTEGRITY FAILURE - aggregate refuses to publish "
              "(missing_records=" + str(len(agg["integrity"]["missing_records"])) +
              ", missing_task_records=" +
              str(len(agg["integrity"]["missing_task_records"])) +
              ", leak_total=" + str(agg["integrity"]["leak_total"]) +
              ", dose_violation_total=" +
              str(agg["integrity"]["dose_violation_total"]) + ")", file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
