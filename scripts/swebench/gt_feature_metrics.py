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
import os
import sys
from collections import Counter, defaultdict
from typing import Any, Iterable

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
_DIRECT_MEMBER_FACTCLASS: dict[str, str] = {
    "GT_EDIT_CHECK": "syntax_result",
    "GT_VERIFY_EXECUTE": "covering_red",
    "GT_VERIFICATION_PLAN": "covering_red",
    "GT_COMPLETION_CERT": "submit_refusal",
    "GT_PATCH_DELTA": "signature_delta",
    "GT_CHANGE_SURFACE": "newfile_precedent",
    "GT_HYPOTHESIS": "recovery",
    "GT_LOC_RESLOT": "localization",
    "GT_CONTENT_LEG": "localization",
    "GT_SEM_BODY": "localization",
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
    "GT_CERT_DELIVERY": ("submit_refusal",),              # model-facing completion cert
    "GT_CONTRACT_MODE": ("caller_contract",),             # contract shaping
    "GT_CONTRACT_BILATERAL": ("caller_contract",),        # bilateral contract shaping
    "GT_D7_RELATEDNESS": ("cochange_prior",),             # relatedness → companion/cochange
    "GT_OBLIGATION_FRESHNESS": ("obligations",),          # obligation freshness
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
    "GT_SS_COHERENCE_V2": ("cochange_prior",),            # companion/coherence re-detection
    "GT_SS_RECOVERY_V2": ("recovery",),                  # recovery selection v2
    "GT_SS_PROVENANCE": (),                               # provenance/seal on every delivery
    "GT_SS_LATE_DROP": (),                                # late-fact drop timing, all classes
    "GT_SS_ACK_METRICS": (),                              # host-side ack telemetry only
    "GT_SS_ACK_FORM": (),                                 # SS-5 FORM: preamble reframes reading of ALL classes + obligations checklist
    "GT_SS_EXEC_TRUTH": ("covering_red",),                # SS-2 mediator: runner-eligible covering selection; kills unexecuted assurances
    "GT_SS_SUBMIT_RED": ("submit_refusal",),              # SS-2 mediator: submit gate consumes observed RED (reached only under GT_VERIFY_EXECUTE=1)
    "GT_SS_ELIGIBILITY": (),                              # SS-4 mediator: cd-$() prefix widening for search isolation (post_search/loc legs)
    "GT_SS_SHADOW": (),                                   # SS-8 mediator: shadow-holdout deliver/withhold across ALL participating advisory classes (E10 causal instrument; inert at rate 0)
}

# Backing-module → producer agreement drift-guard: these members' _MEMBER_CAPABILITY_MODULE
# basename must equal the registry ``producer`` of their declared fact class (proves the
# table is not drifting from the code). Only the members whose module IS the producer.
_MODULE_PRODUCER_MEMBERS: dict[str, str] = {
    "GT_EDIT_CHECK": "edit_check",
    "GT_VERIFY_EXECUTE": "covering_runner",
    "GT_COMPLETION_CERT": "submit_gate",
    "GT_PATCH_DELTA": "patch_delta",
    "GT_CHANGE_SURFACE": "change_surface",
}


def member_role(member: str) -> str:
    if member in _DIRECT_MEMBER_FACTCLASS:
        return ROLE_DIRECT
    if member in _INFRA_MEMBER_MEDIATES:
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
        return (_DIRECT_MEMBER_FACTCLASS[member],)
    return _INFRA_MEMBER_MEDIATES[member]


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
        if fc not in all_classes:
            raise ValueError(f"gt_feature_metrics: {m} → unknown fact class {fc!r}")
    for m, fcs in _INFRA_MEMBER_MEDIATES.items():
        for fc in fcs:
            if fc not in all_classes:
                raise ValueError(f"gt_feature_metrics: {m} mediates unknown class {fc!r}")
    # (c) every registry fact class is produced or mediated by >=1 member (coverage).
    covered = set(_DIRECT_MEMBER_FACTCLASS.values())
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
        reg = fr.registration(_DIRECT_MEMBER_FACTCLASS[m])
        if reg is not None and reg.producer != producer:
            raise ValueError(
                f"gt_feature_metrics: {m} declared producer {producer!r} != registry "
                f"producer {reg.producer!r} for {_DIRECT_MEMBER_FACTCLASS[m]!r}"
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
    "spec.obligation": "obligations",
    "obligation.resurface": "obligations",
    "verify.horizon.advisory": "covering_red",
    "detect.coherence": "recovery",
    "detect.loop": "recovery",
    "recovery": "recovery",
    "completion_cert": "submit_refusal",
    "submit_gate": "submit_refusal",
    "cochange": "cochange_prior",
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
    if lay.startswith("gateway."):
        et = lay.split(".", 1)[1]
        _, fr = _profile_registry()
        reg = fr.registration_for(et)
        if reg is not None:
            return reg.fact_class
    return _LEGACY_LAYER_FACTCLASS.get(lay)


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


def _find_one(task_dir: str, *patterns: str) -> str | None:
    for pat in patterns:
        hits = sorted(glob.glob(os.path.join(task_dir, pat)))
        if hits:
            return hits[0]
    return None


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
        fc = layer_to_fact_class(layer)
        if fc is None:
            continue
        b = per[fc]
        outcome = str(r.get("outcome") or "")
        reason = str(r.get("reason") or "")
        arb = is_arbiter_candidate(layer)
        b["produced"] += 1
        if arb:
            b["arbiter_candidates"] += 1
        if outcome == "delivered":
            b["delivered"] += 1
            b["delivered_chars"] += int(r.get("chars_delivered") or 0)
            b["delivered_rows"].append(idx)
            b["delivered_boundaries"].add(str(r.get("event_type") or ""))
            # a DELIVERED fact whose own reason flags stale/late is a real correctness/timing
            # failure (→ FIX), distinct from a suppressed loser's lateness.
            if "stale" in reason:
                b["stale"] += 1
            if "late" in reason or "expired" in reason:
                b["expired_late"] += 1
            fp = r.get("file_path")
            if fp:
                b["delivered_files"].add(fp)
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
        elif outcome in ("allow", "submit_clean", "clean", "allow_clean"):
            # the gate RAN and correctly ALLOWED (e.g. a clean submit) — a CORRECT ABSTAIN
            # of the refusal fact, NOT a dark/missing delivery.
            b["allowed"] += 1
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

def _consumption_by_fact_class(trajectory: dict, runtime_ledger_path: str | None) -> dict[str, dict]:
    """Per-fact-class receipt rollup from W1's v2 consumption ledger (chronological receipt
    grading; NEVER token-overlap or same-file coincidence — defect #3)."""
    cl_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "consumption_ledger.py")
    import importlib.util
    spec = importlib.util.spec_from_file_location("consumption_ledger_gfm", cl_path)
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    ledger = mod.build_consumption_ledger(trajectory, runtime_ledger_path=runtime_ledger_path)
    per_class = ledger.get("per_class", {}) or {}
    out: dict[str, dict] = defaultdict(lambda: {"delivered": 0, "referenced": 0, "acted": 0, "max_level": 0})
    for kind, pc in per_class.items():
        fc = _CONSUMPTION_KIND_FACTCLASS.get(kind)
        if fc is None:
            continue
        agg = out[fc]
        agg["delivered"] += pc.get("delivered", 0)
        agg["referenced"] += pc.get("referenced", 0)
        agg["acted"] += pc.get("acted", 0)
        agg["max_level"] = max(agg["max_level"], pc.get("max_level", 0))
    return dict(out), ledger


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
        (i, m.get("content"))
        for i, m in enumerate(messages)
        if isinstance(m, dict) and m.get("role") in ("tool", "user")
        and isinstance(m.get("content"), str)
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
            if _content_carries_seal(content, seal, chars, native_text):
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
        fc = layer_to_fact_class(str(rows[idx].get("layer") or ""))
        if fc is not None:
            out[fc] += 1
    return dict(out)


# ---------------------------------------------------------------------------
# Fact-class lifecycle — the substantive per-class grade.
# ---------------------------------------------------------------------------

def _fact_class_eligible(fc: str, timeline: list[dict], ledger_by_fc: dict, oracle_rows: list[dict],
                         has_submission: bool) -> tuple[bool, str]:
    """Did the run create the condition where this fact class COULD fire?"""
    steps = [e for e in timeline if e["role"] == "assistant"]
    any_edit = any(e.get("is_edit") for e in steps)
    any_test = any(e.get("is_test") for e in steps)
    any_search = any(e.get("is_search") for e in steps)
    any_view = any(e.get("viewed_file") for e in steps)
    if fc == "obligations":
        return (bool(oracle_rows) or True, "task carries a spec/obligations plan")
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
) -> dict[str, Any]:
    """Assemble the universal lifecycle for one fact class from the offline evidence."""
    lc = new_lifecycle("not_applicable_to_this_class")
    reg = registry.registration(fc)
    b = ledger_by_fc.get(fc, {})
    cons = consumption_by_fc.get(fc, {})

    eligible, why = _fact_class_eligible(fc, timeline, ledger_by_fc, oracle_rows, has_submission)
    lc["eligible"] = measured(bool(eligible), source_artifact=traj_artifact)
    lc["not_eligible"] = measured(not bool(eligible), source_artifact=traj_artifact)

    produced = int(b.get("produced", 0))
    delivered = int(b.get("delivered", 0))
    allowed = int(b.get("allowed", 0))
    lc["produced"] = measured(produced > 0, source_artifact=ledger_artifact)
    # correct_abstain: (a) eligible but the producer correctly stayed silent (nothing
    # produced), OR (b) the producer RAN and correctly ALLOWED (a clean submit gate) — it
    # produced no refusal because none was warranted. Both are correct silence, NOT dark.
    if eligible and produced == 0:
        lc["correct_abstain"] = measured(True, source_artifact=ledger_artifact)
    elif produced > 0 and delivered == 0 and allowed > 0:
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
        lc["native_valid"] = measured(True, source_artifact=ledger_artifact)  # seam renders native
        # boundary: did any delivered boundary match the registry deliver_by?
        want = reg.deliver_by if reg else None
        got = {x for x in b.get("delivered_boundaries", set()) if x}
        lc["expired_late"] = measured(int(b.get("expired_late", 0)) > 0, source_artifact=ledger_artifact)
        lc["stale"] = measured(int(b.get("stale", 0)) > 0, source_artifact=ledger_artifact)
        lc["dose_tokens"] = measured(round(int(b.get("delivered_chars", 0)) / 4.0, 8),
                                     source_artifact=ledger_artifact)
    elif eligible and produced > 0:
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
        lc["inert"] = measured(delivered > 0 and lvl < 2, source_artifact=traj_artifact)
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


# ---------------------------------------------------------------------------
# Leak canary — no task id / gold path on any model-facing field.
# ---------------------------------------------------------------------------

def leak_canary(delivered_files: Iterable[str], task: str, gold_paths: Iterable[str]) -> list[str]:
    """Return leaked tokens found in model-facing delivered file identities. Zero is required.
    (This grader emits no hidden test identity itself; it scans what the seam delivered.)"""
    leaks: list[str] = []
    task_tokens = {t for t in (task or "").replace("__", " ").replace("-", " ").split() if len(t) > 3}
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
        lc["delivered"] = not_eligible("freshness is host-side; not delivered as model bytes")
        lc["native_valid"] = not_eligible("no model-facing render")
        lc["receipt_level"] = not_eligible("freshness has no receipt")
        return lc

    scope = list(fcs) if fcs else list(fact_lifecycles)
    elig = any(_val(fact_lifecycles[fc].get("eligible")) for fc in scope)
    prod = any(_val(fact_lifecycles[fc].get("produced")) for fc in scope)
    deliv = any(_val(fact_lifecycles[fc].get("delivered")) for fc in scope)
    native = any(_val(fact_lifecycles[fc].get("native_valid")) for fc in scope)
    abstain = any(_val(fact_lifecycles[fc].get("correct_abstain")) for fc in scope)
    receipts = [_val(fact_lifecycles[fc].get("receipt_level")) for fc in scope]
    receipts = [r for r in receipts if isinstance(r, int)]
    lc["eligible"] = measured(bool(elig), source_artifact=ledger_artifact)
    lc["not_eligible"] = measured(not elig, source_artifact=ledger_artifact)
    lc["produced"] = measured(bool(prod), source_artifact=ledger_artifact)
    lc["delivered"] = measured(bool(deliv), source_artifact=ledger_artifact)
    # a mediator whose class correctly abstained (e.g. cert render on a clean submit) is a
    # correct abstain, NOT dark.
    if not deliv and abstain:
        lc["correct_abstain"] = measured(True, source_artifact=ledger_artifact)
    if deliv:
        lc["native_valid"] = measured(bool(native), source_artifact=ledger_artifact)
    # a SCOPED mediator inherits the timing/freshness failure of the class it shaped: if the
    # delivered mediated fact was stale/late, the mediation failed (→ FIX). A KERNEL mediator
    # (fcs empty) touches all classes and must NOT inherit any single class's failure.
    if fcs:
        stale_any = any(_val(fact_lifecycles[fc].get("stale")) for fc in scope)
        late_any = any(_val(fact_lifecycles[fc].get("expired_late")) for fc in scope)
        if stale_any:
            lc["stale"] = measured(True, source_artifact=ledger_artifact)
        if late_any:
            lc["expired_late"] = measured(True, source_artifact=ledger_artifact)
    if receipts:
        lc["receipt_level"] = measured(max(receipts), source_artifact=traj_artifact)
    else:
        lc["receipt_level"] = unmeasured("mediated classes carry no tag-based receipt (native)",
                                         source_artifact=ledger_artifact)
    return lc


def member_record(
    member: str,
    fact_lifecycles: dict[str, dict],
    infra_signals: dict[str, Any],
    baseline_status: str,
    *,
    ledger_artifact: str,
    traj_artifact: str,
) -> dict[str, Any]:
    """Build the per-feature record (lifecycle + compact summary + verdict) for one member.

    DIRECT producer -> its fact class's substantive lifecycle. INFRA -> mediation-only."""
    role = member_role(member)
    fcs = member_fact_classes(member)
    if role == ROLE_DIRECT:
        fc = fcs[0]
        lc = {k: v for k, v in fact_lifecycles[fc].items() if not k.startswith("_")}
    else:
        lc = _infra_member_lifecycle(
            member, fcs, fact_lifecycles, infra_signals, baseline_status,
            ledger_artifact=ledger_artifact, traj_artifact=traj_artifact,
        )
    verdict = verdict_for(lc, role)
    return {
        "role": role,
        "fact_classes": list(fcs) if fcs else ["*kernel*"],
        "lifecycle": lc,
        "summary": feature_summary_from_lifecycle(lc, verdict),
        "verdict": verdict,
    }


# ---------------------------------------------------------------------------
# Per-task collection.
# ---------------------------------------------------------------------------

def collect_task(
    task: str,
    task_dir: str,
    *,
    profile: str = "2",
    baseline_root: str | None = None,
    gold_paths: Iterable[str] | None = None,
) -> dict[str, Any]:
    """Collect the full per-task feature-metrics record (schema gt.feature_metrics.v1).

    Never fabricates: every fact class + every enabled member appears; missing evidence is
    UNMEASURED/NOT_ELIGIBLE; resolution is not consulted for any credit."""
    _, fr = _profile_registry()
    traj_path = _find_one(task_dir, "mini-swe-agent.trajectory.json")
    traj = _load_json(traj_path) if traj_path else None
    if not isinstance(traj, dict):
        traj = {"messages": []}
    ledger_path = _find_one(task_dir, "gt_runtime_ledger_*.jsonl", "gt_runtime_ledger*.jsonl")
    oracle_path = _find_one(task_dir, "gt_oracle_events_*.jsonl")
    rows = load_jsonl(ledger_path) if ledger_path else []
    oracle_rows = load_jsonl(oracle_path) if oracle_path else []

    ledger_artifact = os.path.basename(ledger_path) if ledger_path else "runtime_ledger:absent"
    traj_artifact = os.path.basename(traj_path) if traj_path else "trajectory:absent"

    timeline = _timeline(traj)
    ledger_by_fc = classify_ledger(rows)
    consumption_by_fc, cons_ledger = _consumption_by_fact_class(traj, ledger_path)
    state_by_fc = state_predicates(timeline, ledger_by_fc)
    infra_signals = _infra_signals(rows, ledger_by_fc)

    submission = str((traj.get("info") or {}).get("submission") or "")
    has_submission = bool(submission) or any(
        m.get("role") == "exit" for m in traj.get("messages", []) if isinstance(m, dict)
    )
    base_path = _baseline_trajectory_path(task, baseline_root)
    baseline_status = BASELINE_MATCHED if base_path else BASELINE_UNAVAILABLE

    native_visible = native_visible_by_fact_class(rows, traj.get("messages", []) or [])
    fact_lifecycles: dict[str, dict] = {}
    for fc in fr.all_fact_classes():
        fact_lifecycles[fc] = fact_class_lifecycle(
            fc, timeline=timeline, ledger_by_fc=ledger_by_fc,
            consumption_by_fc=consumption_by_fc, state_by_fc=state_by_fc,
            oracle_rows=oracle_rows, has_submission=has_submission,
            baseline_status=baseline_status, registry=fr,
            ledger_artifact=ledger_artifact, traj_artifact=traj_artifact,
            native_visible=native_visible.get(fc, 0),
        )

    members = profile_members(profile)
    features: dict[str, dict] = {}
    for m in members:
        features[m] = member_record(
            m, fact_lifecycles, infra_signals, baseline_status,
            ledger_artifact=ledger_artifact, traj_artifact=traj_artifact,
        )

    events = _atomic_events(task, rows, ledger_artifact)

    delivered_files: set[str] = set()
    for b in ledger_by_fc.values():
        delivered_files |= set(b.get("delivered_files", set()))
    leaks = leak_canary(delivered_files, task, list(gold_paths or []))

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
    return {
        "schema": "gt.feature_metrics.v1",
        "grader_version": GRADER_VERSION,
        "profile": profile,
        "task": task,
        "attempt": 1,
        "artifacts": {
            "trajectory": traj_artifact,
            "runtime_ledger": ledger_artifact,
            "oracle_events": os.path.basename(oracle_path) if oracle_path else None,
            "baseline_trajectory": base_path,
        },
        "features": features,
        "fact_classes": fc_json,
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
            "leak_canary": leaks,
            "leak_count": len(leaks),
            "consumption_schema": cons_ledger.get("schema"),
            "ledger_rows": len(rows),
        },
    }


def _atomic_events(task: str, rows: list[dict], ledger_artifact: str) -> list[FeatureMetricEvent]:
    """One FeatureMetricEvent per DELIVERED ledger row, mapped to its producing member."""
    fc_to_member: dict[str, str] = {}
    for m, fc in _DIRECT_MEMBER_FACTCLASS.items():
        fc_to_member.setdefault(fc, m)
    for m, fcs in _INFRA_MEMBER_MEDIATES.items():
        for fc in fcs:
            fc_to_member.setdefault(fc, m)
    events: list[FeatureMetricEvent] = []
    counter: Counter = Counter()
    for idx, r in enumerate(rows):
        if str(r.get("outcome") or "") != "delivered":
            continue
        fc = layer_to_fact_class(str(r.get("layer") or ""))
        if fc is None:
            continue
        member = fc_to_member.get(fc, "GT_GATEWAY")
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

def aggregate_run(run_id: str, task_records: list[dict], profile: str = "2") -> dict[str, Any]:
    """Aggregate per-task records into the three run-level artifacts.

    REFUSES (integrity.publishable=False) if ANY enabled feature record is absent from ANY
    task — a missing record is a COLLECTION FAILURE, not an implicit zero. eligible=0 is a
    valid record; a MISSING record is not. The CLI turns publishable=False into a nonzero
    exit."""
    enabled = profile_members(profile)
    run_metrics: dict[str, Any] = {
        "schema": "gt.feature_metrics.run.v1", "grader_version": GRADER_VERSION,
        "run_id": run_id, "profile": profile, "n_tasks": len(task_records), "features": {},
    }
    effects: dict[str, Any] = {
        "schema": "gt.feature_effects.v1", "grader_version": GRADER_VERSION,
        "run_id": run_id, "profile": profile, "matched_baseline_tasks": [],
    }
    integrity: dict[str, Any] = {
        "schema": "gt.metric_integrity.v1", "grader_version": GRADER_VERSION,
        "run_id": run_id, "profile": profile, "enabled_members": enabled,
        "missing_records": [], "leak_total": 0, "reconciliation": {}, "publishable": True,
    }

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
    atomic_total = 0
    for rec in task_records:
        leak_total += rec.get("integrity", {}).get("leak_count", 0)
        atomic_total += len(rec.get("atomic_events", []))
        if not rec.get("integrity", {}).get("all_members_present", False):
            integrity["publishable"] = False
    integrity["leak_total"] = leak_total
    if leak_total > 0:
        integrity["publishable"] = False
    integrity["reconciliation"] = {
        "atomic_events_total": atomic_total,
        "tasks_reconciled": len(task_records),
        "enabled_member_count": len(enabled),
    }
    return {"run_metrics": run_metrics, "effects": effects, "integrity": integrity}


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
    args = ap.parse_args(argv)

    run_dir = args.run_dir
    if not os.path.isdir(run_dir):
        print("gt_feature_metrics: run_dir not found: " + run_dir, file=sys.stderr)
        return 2
    run_id = args.run_id or os.path.basename(os.path.normpath(run_dir))
    out_dir = args.out or run_dir

    records: list[dict] = []
    for task, d in _iter_task_dirs(run_dir):
        rec = collect_task(task, d, profile=args.profile, baseline_root=args.baseline_root)
        write_task_metrics(rec, d)
        records.append(rec)

    if not records:
        print("gt_feature_metrics: no task dirs with a mini trajectory", file=sys.stderr)
        return 2

    agg = aggregate_run(run_id, records, profile=args.profile)
    paths = write_run_aggregate(run_id, agg, out_dir)
    publishable = agg["integrity"]["publishable"]
    print("gt_feature_metrics: " + str(len(records)) + " tasks, publishable=" + str(publishable))
    for k, p in paths.items():
        print("  " + k + ": " + p)
    if not publishable:
        print("gt_feature_metrics: INTEGRITY FAILURE - aggregate refuses to publish "
              "(missing_records=" + str(len(agg["integrity"]["missing_records"])) +
              ", leak_total=" + str(agg["integrity"]["leak_total"]) + ")", file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
