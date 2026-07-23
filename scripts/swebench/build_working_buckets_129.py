#!/usr/bin/env python3
"""Build the 129-row WORKING / NOT_WORKING bucket ledger (local artifact)."""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]  # repo root (…/scripts/swebench/ → …/)
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "scripts" / "swebench"), str(ROOT / "scripts")]

from gt_feature_inventory import ACQ_FEATURES, canonical_feature_inventory  # noqa: E402
from groundtruth.runtime.fact_registry import (  # noqa: E402
    FACT_ROLE_INTERNAL_SUPPORT,
    REGISTRY,
    all_fact_classes,
)
from groundtruth.runtime.feature_lineage import (  # noqa: E402
    CAP_BYTE_OWNER_IDS,
    CAP_ELIGIBILITY_IDS,
    CAP_MEDIATOR_IDS,
)

# Probe grades (authority for DIRECT sealed evidence)
PROBE_DELIVERED = {"obligations", "caller_contract", "syntax_result"}
PROBE_HOLD = {
    "covering_red",
    "submit_refusal",
    "GT_EDIT_CHECK",
    "GT_SS_SUBMIT_RED",
    "GT_CERT_DELIVERY",
}
PROBE_BROKEN = {"localization", "def_partition", "recovery"}
PROBE_NOT_ELIGIBLE = {
    "GT_CHANGE_SURFACE",
    "GT_PATCH_DELTA",
    "GT_HYPOTHESIS",
    "GT_LOC_RESLOT",
    "newfile_precedent",
    "signature_delta",
}

# Wire-audit overrides: missing opportunity ≠ NOT_WORKING if path is sound.
WIRE_WORKING_DESPITE_PROBE = {
    # Python signature/companion producers exist; probe lacked fair Python sig-change edits.
    "GT_PATCH_DELTA",
    "signature_delta",
    "GT_HYPOTHESIS",  # hypothesis envelopes exist; not the BROKEN class
    "GT_EDIT_CHECK",  # HOLD on syntax_ok is honest; DELIVERED on httpx syntax_error
}

# Proven NOT_WORKING (unwired / wrong polarity / seal never lands)
# GT_SS_NOVELTY promoted WORKING 2026-07-23: path-acq no longer starves
# post_search.localize / is_loc; claim-match + DEDUP2 remain (unit-proven).
NOT_WORKING = {
    "localization": (
        "Wire fix landed 2026-07-23 (LOC_RESLOT first-search + novelty polarity); "
        "pending sealed live DELIVERED on opportunity probe"
    ),
    "def_partition": (
        "Wire fix landed 2026-07-23 (novelty path-acq exempt); "
        "pending sealed live DELIVERED on opportunity probe"
    ),
    "recovery": (
        "control_record ValueError path fixed 2026-07-23; may still lose ≤1 dose / "
        "ss_recovery_gate — pending sealed live"
    ),
    "covering_red": "Selection unstarved (Hybrid RTS) but sealed attributed RED still unproven",
    "submit_refusal": "Submit always clean on probes; sealed refusal unproven on fair fail",
    "GT_SS_SUBMIT_RED": "Needs failing test touching edit; never sealed on probes",
    "GT_CERT_DELIVERY": "cert.decision==head.allow; clean submit delivers nothing",
    "GT_CHANGE_SURFACE": (
        "Wire fix landed 2026-07-23 (gateway cd-strip ZERO_ABSENT parity); "
        "pending sealed live on harness-prefixed empty grep opportunity"
    ),
    "newfile_precedent": (
        "Same ZERO_ABSENT path as CHANGE_SURFACE (cd-strip fix landed); "
        "pending sealed registration on live opportunity"
    ),
    "GT_LOC_RESLOT": (
        "Wire fix landed 2026-07-23 (first isolated search spends ranked loc); "
        "pending sealed live"
    ),
    "GT_POST_SEARCH": (
        "Participation recording landed on production lattice path; "
        "pending sealed DIRECT proof on live"
    ),
    "GT_SS_RECOVERY_V2": (
        "Wire fix landed 2026-07-23 (omit disagreeing recovery:* candidate_id on "
        "control participation); pending live release proof"
    ),
    "GT_VERIFY_EXECUTE": "Eligible; covering_red sealed delivery still not produced",
    "GT_VERIFICATION_PLAN": "Runs to plan_none_produced; does not yet yield sealed covering_red",
}

# Rows with landed wire fixes awaiting opportunity-shaped live seal (not yet WORKING)
FIX_LANDED_PENDING_LIVE = {
    "localization",
    "def_partition",
    "GT_LOC_RESLOT",
    "GT_POST_SEARCH",
    "GT_CHANGE_SURFACE",
    "newfile_precedent",
    "recovery",
    "GT_SS_RECOVERY_V2",
}

# Promoted this tranche (typed terminal / polarity proven; do-not-touch)
PROMOTED_WORKING = {
    "GT_SS_NOVELTY": (
        "2026-07-23: path/symbol acquisition no longer starves post_search.localize/"
        "is_loc; claim-match + localization DEDUP2 remain (tests green)"
    ),
}

ACQ_DEFS = {
    "graph_validity": "graph.db opens and is queryable for retrieval",
    "structural_depth": "Enough structural reach signals for ranking",
    "resolution_honesty": "Edges carry honest resolution_method + confidence",
    "type_intelligence": "Type/LSP-derived props available for contracts/ranking",
    "lexical_FTS5": "FTS5 name/path index populated (requires -tags sqlite_fts5)",
    "body_retrieval": "Body/content-FTS rows for behavior search",
    "semantic_embedder": "Embedding ranker loads and scores",
    "LSP": "LSP certificate / enriched signatures available",
    "freshness_basis": "file_hashes / revision anchors for post-edit freshness",
    "repo_scope": "Correct repo root / multi-repo scope for queries",
    "cochange_history": "Sealed cochange_evidence for ranking / Lane-A completeness",
    "determinism": "Repeat-identity seals for same input",
}

ELIG_DEFS = {
    "GT_POST_SEARCH": "Master enable for post_search lattice (def_partition / reactive loc)",
    "GT_BRIEF_MINIMAL": "Step-0 brief reduction (obligations+orientation under LOC_RESLOT)",
    "GT_SS_ELIGIBILITY": "Widens cd-$(…) search isolation for lattice probes",
    "GT_SS_NOVELTY": "Suppress already-acquired claims (must not false-starve novel partitions)",
    "GT_SS_DEDUP2": "Semantic entity-set dedup within a group",
    "GT_SS_LATE_DROP": "Drop late obligation/loc doses past decision boundary",
    "GT_SS_SHADOW": "Holdout assignment (rate>0); zeros model bytes on HOLDOUT",
    "GT_SS_EXEC_TRUTH": "Drop phantom covering files absent from tree",
    "GT_SS_RECOVERY_V2": "Typed recovery-candidate release gate",
    "GT_EDIT_OVERLAY": "Stale-envelope mute for edited-not-this-turn files",
    "GT_OBLIGATION_FRESHNESS": "Demote tested obligations",
    "GT_REGISTRY_ENFORCE": "Registry timing/renderer enforcement",
    "GT_XSESSION_MEMORY": "Cross-session inert-class suppression",
    "GT_D7_RELATEDNESS": "Receipt relatedness for consumption credit (not delivery drop)",
}

MED_DEFS = {
    "GT_BRIEF_NATIVE": "Native obligations checklist form",
    "GT_COMPLETION_CERT": "Host completion-certificate telemetry (cannot change allow/block)",
    "GT_CONTENT_LEG": "Body-BM25 content leg for localization",
    "GT_CONTRACT_BILATERAL": "Bilateral [CONSUMED] contract line",
    "GT_CONTRACT_MODE": "Mode-conditioned caller narration",
    "GT_CONTRACT_NATIVE": "Native contract render (may drop tagged props)",
    "GT_EVIDENCE_NATIVE": "Native evidence render",
    "GT_GATEWAY": "gateway.augment ≤1 FACT plane",
    "GT_GATEWAY_EDIT_BRIDGES": "Edit before/after bridges for patch_delta/caller",
    "GT_GATEWAY_NATIVE": "Native gateway render form",
    "GT_GLOBAL_ARBITER": "Cross-plane ≤1 dose arbitration",
    "GT_INSEAM_METRICS": "In-seam participation/receipt stamps (zero model bytes)",
    "GT_L6_FRESH": "Post-edit graph work-copy freshness",
    "GT_LANE_ENVELOPE": "Lane budget DROP-WHOLE when over cap",
    "GT_NUDGE_NATIVE": "Native recovery nudge form",
    "GT_POST_SEARCH_NATIVE": "Native def_partition form",
    "GT_SCOPE_NATIVE": "Native/additive scope lines",
    "GT_SEM_BODY": "Body-channel passages for ranking",
    "GT_SS_ACK_FORM": "Imperative-only ack shaping (can drop empty)",
    "GT_SS_ACK_METRICS": "Ack watch/scan telemetry",
    "GT_SS_ARBITER_V2": "Empty/redundant envelope drop in arbit",
    "GT_SS_COHERENCE_V2": "Coherence churn fire gate for recovery",
    "GT_SS_PROVENANCE": "Strip bad-path lines; whole-suppress if empty",
    "GT_STEER_NATIVE": "Native steer form",
    "GT_VERIFICATION_PLAN": "Progressive syntax→unit verification plan",
    "GT_VERIFY_EXECUTE": "Execute covering tests for covering_red/submit",
    "GT_XSESSION_RANKUP": "Cross-session boost among ≤1 winners",
}

BYTE_DEFS = {
    "GT_EDIT_CHECK": "At-edit syntax/parse diagnostics (syntax_result bytes)",
    "GT_CHANGE_SURFACE": "New-file destination / missing-role on ZERO_ABSENT",
    "GT_PATCH_DELTA": "Signature/companion delta on structured edits",
    "GT_HYPOTHESIS": "Hypothesis-tier gateway envelopes",
    "GT_LOC_RESLOT": "Reactive ranked localization CAP (search_result)",
    "GT_SS_SUBMIT_RED": "Submit-time RED from agent failing test on edit",
    "GT_CERT_DELIVERY": "Model-facing completion-cert refusal render",
}


def working_means(family: str, role: str) -> str:
    if family == "FACT" and role == "direct_delivery":
        return (
            "Wired to sealed observation; on fair opportunity → DELIVERED "
            "or honest HOLD — never silent dark / false-live. Quiet OK if no opportunity."
        )
    if family == "FACT" and role == "internal_support":
        return "Ranking prior wired; Gateway does not ship cochange envelopes; quiet OK."
    if family == "CAP" and role == "byte_owner":
        return (
            "Byte-owner path can seal observation bytes on opportunity; "
            "honest HOLD/NOT_ELIGIBLE when opportunity absent."
        )
    if family == "CAP" and role == "eligibility":
        return "Records APPLIED/NO_EFFECT with correct polarity; does not wrongly starve DIRECT."
    if family == "CAP" and role == "mediator":
        return "Form/dose/enable does what it claims; no accidental drop of valid DIRECT bytes."
    if family == "ACQ":
        return "Witness admits when substrate has signal; missing → UNMEASURED not fake MEASURED."
    if family == "PERF":
        return "Computes from traj/ledger/patch with honest missingness; never gates product fire."
    return "Typed terminal correct."


def classify(fid: str, family: str, role: str) -> tuple[str, str]:
    if fid in PROMOTED_WORKING:
        return "WORKING", PROMOTED_WORKING[fid]
    if fid in NOT_WORKING:
        return "NOT_WORKING", NOT_WORKING[fid]
    if fid in PROBE_BROKEN:
        return "NOT_WORKING", "Probe BROKEN sealed path"
    if fid in WIRE_WORKING_DESPITE_PROBE or fid in PROBE_DELIVERED:
        return "WORKING", "Wire proven / sealed DELIVERED or sound opportunity-miss"
    if fid in PROBE_HOLD and fid not in NOT_WORKING:
        # HOLD can be working if honest — EDIT_CHECK is; covering stays NOT_WORKING above
        if fid == "GT_EDIT_CHECK":
            return "WORKING", "Honest HOLD on syntax_ok; DELIVERED on syntax_error (httpx)"
        return "NOT_WORKING", f"Probe HOLD without sealed catch proof ({fid})"
    if fid in PROBE_NOT_ELIGIBLE and fid not in WIRE_WORKING_DESPITE_PROBE:
        return "NOT_WORKING", "Probe NOT_ELIGIBLE with known opportunity/wiring gap"
    # Default: support rows assumed WORKING unless listed — PERF/ACQ/form mediators
    if family == "PERF":
        if fid in {"has_patch"}:  # not in list; outcome classifier noted in doctrine
            pass
        return "WORKING", "Offline measure; computes with honest missingness (do-not-touch)"
    if family == "ACQ":
        return "WORKING", "Witness-only; does not suppress DIRECT bytes (do-not-touch unless substrate red)"
    if role in {"mediator", "eligibility"} and fid not in NOT_WORKING:
        return "WORKING", "Typed control wired; freeze unless proven starve polarity"
    if role == "internal_support":
        return "WORKING", "Internal ranking prior by design"
    return "WORKING", "Default wire-OK pending contrary evidence"


def main() -> None:
    inv = canonical_feature_inventory()
    rows: list[dict] = []

    for fid in inv["ACQ"]:
        rows.append(
            {
                "id": fid,
                "family": "ACQ",
                "role": "acquisition",
                "what_it_does": ACQ_DEFS.get(fid, fid),
                "working_means": working_means("ACQ", "acquisition"),
                "code_site": "acq_provenance / v1r_brief acquisition_sources",
            }
        )

    for fid in inv["CAP"]:
        if fid in CAP_BYTE_OWNER_IDS:
            role = "byte_owner"
            what = BYTE_DEFS.get(fid, fid)
        elif fid in CAP_ELIGIBILITY_IDS:
            role = "eligibility"
            what = ELIG_DEFS.get(fid, fid)
        elif fid in CAP_MEDIATOR_IDS:
            role = "mediator"
            what = MED_DEFS.get(fid, fid)
        else:
            role = "mediator"
            what = fid
        rows.append(
            {
                "id": fid,
                "family": "CAP",
                "role": role,
                "what_it_does": what,
                "working_means": working_means("CAP", role),
                "code_site": "rl_profile PROFILE_MEMBERS[2] + gt_mini_patch / gateway",
            }
        )

    for fid in inv["FACT"]:
        reg = REGISTRY[fid]
        role = (
            "internal_support"
            if reg.fact_role == FACT_ROLE_INTERNAL_SUPPORT
            else "direct_delivery"
        )
        rows.append(
            {
                "id": fid,
                "family": "FACT",
                "role": role,
                "what_it_does": (
                    f"Decision: {reg.target_decision}; producer={reg.producer}; "
                    f"deliver_by={reg.deliver_by}; surface={reg.surface}"
                ),
                "working_means": working_means("FACT", role),
                "code_site": f"fact_registry:{fid} → {reg.producer}",
                "producer": reg.producer,
                "deliver_by": reg.deliver_by,
                "surface": reg.surface,
            }
        )

    for section, defs in __import__("gt_run_metrics", fromlist=["_MANDATORY_METRICS"])._MANDATORY_METRICS.items():
        for name, typ in defs:
            rows.append(
                {
                    "id": name,
                    "family": "PERF",
                    "role": "measurement",
                    "section": section,
                    "what_it_does": f"PERF {section}: {typ}",
                    "working_means": working_means("PERF", "measurement"),
                    "code_site": "gt_run_metrics._MANDATORY_METRICS / gt_performance_metrics",
                }
            )

    assert len(rows) == 129, len(rows)

    working, not_working = [], []
    for r in rows:
        bucket, reason = classify(r["id"], r["family"], r["role"])
        r["bucket"] = bucket
        r["block_reason"] = reason if bucket == "NOT_WORKING" else ""
        r["do_not_touch"] = bucket == "WORKING"
        r["evidence"] = {
            "probe_30025310582": (
                "DELIVERED"
                if r["id"] in PROBE_DELIVERED
                else "HOLD"
                if r["id"] in PROBE_HOLD
                else "BROKEN"
                if r["id"] in PROBE_BROKEN
                else "NOT_ELIGIBLE"
                if r["id"] in PROBE_NOT_ELIGIBLE
                else "n/a"
            ),
            "probe_30034619505_note": (
                "covering_empty 23→0 via Hybrid RTS; sealed covering_red still 0"
                if r["id"] in {"covering_red", "GT_VERIFY_EXECUTE", "GT_VERIFICATION_PLAN"}
                else ""
            ),
            "fix_landed_pending_live": r["id"] in FIX_LANDED_PENDING_LIVE,
            "promoted_this_tranche": r["id"] in PROMOTED_WORKING,
        }
        (working if bucket == "WORKING" else not_working).append(r["id"])

    # Fix queue (plan order)
    fix_queue = [
        {
            "plane": "1_reactive_search",
            "status": "wire_fixed_pending_live_seal",
            "ids": [
                "localization",
                "def_partition",
                "GT_LOC_RESLOT",
                "GT_POST_SEARCH",
            ],
            "promoted": ["GT_SS_NOVELTY"],
            "first_fixes": [
                "Exempt post_search.localize/is_loc from path-only novelty (claim+dedup2)",
                "Under LOC_RESLOT: once-per-episode ranked loc on first isolated search",
                "Record GT_POST_SEARCH participation on production lattice path",
            ],
        },
        {
            "plane": "2_absence_newfile",
            "status": "wire_fixed_pending_live_seal",
            "ids": ["GT_CHANGE_SURFACE", "newfile_precedent"],
            "first_fixes": [
                "gateway._grep_result_empty cd-strip parity with mini (W13 + SS-4)",
                "Opportunity-shaped DeepSWE with harness-prefixed empty grep before create",
            ],
        },
        {
            "plane": "3_presubmit_catch",
            "status": "open",
            "ids": [
                "covering_red",
                "submit_refusal",
                "GT_SS_SUBMIT_RED",
                "GT_CERT_DELIVERY",
                "GT_VERIFY_EXECUTE",
                "GT_VERIFICATION_PLAN",
            ],
            "first_fixes": [
                "Fair failing covering opportunity → sealed attributed covering_red",
                "Submit refusal on attributed fail (not clean)",
            ],
        },
        {
            "plane": "4_recovery",
            "status": "wire_partial_pending_live",
            "ids": ["recovery", "GT_SS_RECOVERY_V2"],
            "first_fixes": [
                "Omit disagreeing recovery:* candidate_id on control participation (stops control_record_error:ValueError)",
                "Ensure recovery can win ≤1 dose on failure_obs (still open)",
            ],
        },
    ]

    out = {
        "schema": "gt.working_buckets.v1",
        "denominator": 129,
        "working_definition": (
            "Wired correctly on typed terminal; quiet OK without opportunity; "
            "NOT deliver-every-time. WORKING frozen do-not-touch."
        ),
        "counts": {
            "WORKING": len(working),
            "NOT_WORKING": len(not_working),
            "ACQ": len(inv["ACQ"]),
            "CAP": len(inv["CAP"]),
            "FACT": len(inv["FACT"]),
            "PERF": len(inv["PERF"]),
        },
        "WORKING": sorted(working),
        "NOT_WORKING": sorted(not_working),
        "fix_queue": fix_queue,
        "rows": rows,
    }
    path = ROOT / ".tmp_working_buckets_129.json"
    path.write_text(json.dumps(out, indent=2), encoding="utf-8")
    print(f"wrote {path}")
    print("WORKING", len(working), "NOT_WORKING", len(not_working))
    print("NOT_WORKING ids:", sorted(not_working))


if __name__ == "__main__":
    main()
