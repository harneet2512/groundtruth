#!/usr/bin/env python3
"""Build a conservative two-level ``gt.complete.report.v1`` from live task records.

The builder derives the current inventory and role partition from executable registries.
Offline fixtures, replay, flags, and code presence never promote Level 1 or Level 2.  A
positive randomized receipt endpoint is reported as behavioral influence only; it is not
automatically durable progress or a solve.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from gt_feature_inventory import canonical_feature_inventory
from live_causal_cohort import (
    evaluate_live_cohort,
    opportunity_from_dict,
)
from groundtruth.runtime.fact_registry import (
    FACT_ROLE_INTERNAL_SUPPORT,
    fact_role_for,
)
from groundtruth.runtime.feature_lineage import cap_role_for


SCHEMA = "gt.complete.report.v1"


def _role(family: str, feature_id: str) -> str:
    if family == "ACQ":
        return "source"
    if family == "PERF":
        return "measurement"
    if family == "FACT":
        return (
            "internal_support"
            if fact_role_for(feature_id) == FACT_ROLE_INTERNAL_SUPPORT
            else "direct_fact"
        )
    cap_role = cap_role_for(feature_id)
    if cap_role not in {"byte_owner", "eligibility", "mediator"}:
        raise ValueError(f"unknown CAP role for {feature_id}: {cap_role!r}")
    return str(cap_role)


def derived_inventory() -> dict[str, Any]:
    inventory = canonical_feature_inventory()
    roles = Counter(
        _role(family, feature_id)
        for family, feature_ids in inventory.items()
        for feature_id in feature_ids
    )
    family_counts = {family: len(feature_ids) for family, feature_ids in inventory.items()}
    total = sum(family_counts.values())
    direct = roles["byte_owner"] + roles["direct_fact"]
    return {
        "features": inventory,
        "family_counts": family_counts,
        "role_counts": dict(sorted(roles.items())),
        "total": total,
        "direct_delivery_count": direct,
    }


def _readiness(feature: dict[str, Any] | None) -> dict[str, Any]:
    value = feature.get("ss_readiness") if isinstance(feature, dict) else None
    return value if isinstance(value, dict) else {}


def _explicitly_applicable(family: str, feature: dict[str, Any] | None) -> bool:
    if not isinstance(feature, dict):
        return False
    if family in {"ACQ", "PERF"}:
        return feature.get("status") == "MEASURED"
    opportunity = feature.get("opportunity_evidence")
    return bool(
        isinstance(opportunity, dict)
        and opportunity.get("status") == "BOUND"
        and opportunity.get("eligible_opportunity") is True
    )


def _explicit_quiet(feature: dict[str, Any] | None) -> bool:
    if not isinstance(feature, dict):
        return False
    opportunity = feature.get("opportunity_evidence")
    return bool(
        isinstance(opportunity, dict)
        and opportunity.get("status") == "BOUND"
        and opportunity.get("eligible_opportunity") is False
    )


def _applicability_unresolved(
    family: str, feature: dict[str, Any] | None,
) -> bool:
    if not isinstance(feature, dict):
        return True
    if family in {"ACQ", "PERF"}:
        return feature.get("status") != "MEASURED"
    opportunity = feature.get("opportunity_evidence")
    return not (
        isinstance(opportunity, dict)
        and opportunity.get("status") == "BOUND"
        and isinstance(opportunity.get("eligible_opportunity"), bool)
    )


def _gate_values(readiness: dict[str, Any]) -> list[bool | None]:
    gates = readiness.get("gates")
    if not isinstance(gates, dict):
        return []
    return [
        value if isinstance(value, bool) else None
        for value in gates.values()
    ]


def _opportunity_counts(records: list[dict[str, Any]]) -> dict[str, int]:
    eligible = delivered = withheld = quiet = lost = failed = 0
    for record in records:
        fair = ((record.get("ss_integrity") or {}).get("fair_probe") or {})
        for row in fair.get("opportunity_observations") or []:
            if not isinstance(row, dict):
                continue
            eligible += 1
            if row.get("assignment") == "DELIVER":
                delivered += 1
            elif row.get("assignment") == "HOLDOUT":
                withheld += 1
            if row.get("failures"):
                failed += 1
        for feature in (record.get("ss_features") or {}).values():
            if _explicit_quiet(feature):
                quiet += 1
            opportunity = feature.get("opportunity_evidence") if isinstance(feature, dict) else None
            if (
                isinstance(opportunity, dict)
                and opportunity.get("status") == "BOUND"
                and int(opportunity.get("selected_count") or 0) == 0
            ):
                lost += int(opportunity.get("opportunity_count") or 0)
    return {
        "eligible": eligible,
        "delivered": delivered,
        "withheld": withheld,
        "quiet": quiet,
        "lost": lost,
        "failed": failed,
    }


def _cohorts(
    records: list[dict[str, Any]],
    minimum_detectable_effect: float | None,
) -> dict[str, dict[str, Any]]:
    by_class: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        fair = ((record.get("ss_integrity") or {}).get("fair_probe") or {})
        for row in fair.get("opportunity_observations") or []:
            if isinstance(row, dict):
                by_class[str(row.get("fact_class") or "")].append(row)
    if minimum_detectable_effect is None:
        return {
            fact_class: {
                "schema": "gt.live_causal_cohort.v1",
                "status": "UNMEASURED",
                "identification_strength": "NONE",
                "failures": ["preregistered_minimum_detectable_effect_absent"],
                "n_opportunities": len(rows),
            }
            for fact_class, rows in sorted(by_class.items())
        }
    return {
        fact_class: evaluate_live_cohort(
            [opportunity_from_dict(row) for row in rows],
            minimum_detectable_effect=minimum_detectable_effect,
        )
        for fact_class, rows in sorted(by_class.items())
    }


def build_report(
    task_records: Iterable[dict[str, Any]],
    *,
    study_id: str,
    scope: str,
    claim_contract: dict[str, Any],
    system_identity: dict[str, Any],
    minimum_detectable_effect: float | None = None,
    held_out_test_set: bool = False,
    deployment_like_context: bool = False,
) -> dict[str, Any]:
    """Build a role-correct, non-promotional GT Complete report."""
    if scope not in {"task", "run", "product"}:
        raise ValueError("scope must be task, run, or product")
    records = [record for record in task_records if isinstance(record, dict)]
    inventory = derived_inventory()
    feature_rows: dict[str, list[tuple[str, dict[str, Any] | None]]] = defaultdict(list)
    for record in records:
        rows = record.get("ss_features")
        rows = rows if isinstance(rows, dict) else {}
        for family, feature_ids in inventory["features"].items():
            for feature_id in feature_ids:
                feature = rows.get(feature_id)
                feature_rows[feature_id].append(
                    (family, feature if isinstance(feature, dict) else None)
                )

    applicable: set[str] = set()
    live: set[str] = set()
    quiet: set[str] = set()
    violations: set[str] = set()
    dark: set[str] = set()
    for family, feature_ids in inventory["features"].items():
        for feature_id in feature_ids:
            observations = feature_rows.get(feature_id, [])
            if scope == "product":
                applicable.add(feature_id)
            elif any(_explicitly_applicable(family, row) for _, row in observations):
                applicable.add(feature_id)
            if observations and all(
                _readiness(row).get("ss_live") is True
                for _, row in observations
                if _explicitly_applicable(family, row)
            ) and any(
                _explicitly_applicable(family, row) for _, row in observations
            ):
                live.add(feature_id)
            if observations and all(_explicit_quiet(row) for _, row in observations):
                quiet.add(feature_id)
            applicable_rows = [
                row for _, row in observations
                if _explicitly_applicable(family, row)
            ]
            if any(False in _gate_values(_readiness(row)) for row in applicable_rows):
                violations.add(feature_id)
            if feature_id in applicable and feature_id not in live and feature_id not in violations:
                dark.add(feature_id)
            if (
                scope != "product"
                and (
                    not observations
                    or any(
                        _applicability_unresolved(family, row)
                        for _, row in observations
                    )
                )
                and feature_id not in violations
            ):
                dark.add(feature_id)
            if scope == "product" and not observations:
                dark.add(feature_id)

    inventory_complete = bool(records) and all(
        isinstance(record.get("ss_integrity"), dict)
        and record["ss_integrity"].get("inventory_complete") is True
        for record in records
    )
    inputs_complete = bool(records) and all(
        isinstance(record.get("ss_integrity"), dict)
        and record["ss_integrity"].get("required_inputs_complete") is True
        for record in records
    )
    live_population = bool(records) and all(
        ((record.get("ss_integrity") or {}).get("live_run_provenance") or {}).get(
            "verdict"
        ) == "LIVE_PAID"
        for record in records
    )
    validity_failures: list[str] = []
    if not inventory_complete:
        validity_failures.append("program_inventory_or_projection_incomplete")
    if not inputs_complete:
        validity_failures.append("required_inputs_incomplete")
    if not live_population:
        validity_failures.append("complete_live_population_not_proven")
    validity_status = "PASS" if not validity_failures else "PARTIAL"

    if violations:
        level1_status = "LEVEL1_FAIL"
    elif dark:
        level1_status = "LEVEL1_PARTIAL" if live else "LEVEL1_UNMEASURED"
    elif validity_status != "PASS":
        level1_status = "LEVEL1_UNMEASURED"
    else:
        level1_status = "LEVEL1_PASS"

    cohort_results = _cohorts(records, minimum_detectable_effect)
    cohort_valid = bool(cohort_results) and all(
        result.get("status") != "INVALID" and not result.get("failures")
        for result in cohort_results.values()
    )
    positive_cohorts = [
        result for result in cohort_results.values()
        if result.get("status") == "POSITIVE_EFFECT"
    ]
    powered_cohorts = [
        result for result in cohort_results.values()
        if result.get("identification_strength") == "COHORT_CAUSAL"
    ]
    creditable_positive_cohorts = (
        positive_cohorts
        if held_out_test_set and claim_contract.get("pre_specified") is True
        else []
    )
    level1_pass = level1_status == "LEVEL1_PASS"
    # The only cohort endpoint currently exported is registered receipt. It proves a
    # behavior response, not beneficial durable state or solution-path success.
    if level1_pass and creditable_positive_cohorts:
        level2_status = "CHANGED_ACTION"
        strength = "COHORT_CAUSAL"
        final_verdict = "GT_WORKING_BEHAVIOR_CHANGED"
    elif level1_pass:
        level2_status = "BEHAVIOR_UNMEASURED"
        strength = "NONE"
        final_verdict = "GT_WORKING_BEHAVIOR_UNMEASURED"
    elif level1_status == "LEVEL1_FAIL":
        level2_status = "NOT_CREDITABLE"
        strength = "NONE"
        final_verdict = "GT_NOT_WORKING_AS_INTENDED"
    else:
        level2_status = "BEHAVIOR_UNMEASURED"
        strength = "NONE"
        final_verdict = "GT_OPERATION_UNPROVEN"

    evidence_items = [{
        "id": "E1",
        "kind": "metric",
        "artifact": "gt.feature_metrics.v1 task records",
        "pointer": "ss_features + ss_integrity",
        "summary": (
            f"Executable inventory {inventory['total']} rows; "
            f"{inventory['direct_delivery_count']} are direct-delivery rows."
        ),
    }]
    feature_attribution: list[dict[str, Any]] = []
    for family, feature_ids in inventory["features"].items():
        for feature_id in feature_ids:
            role = _role(family, feature_id)
            is_live = feature_id in live
            if family == "PERF":
                credit = "MEASUREMENT" if is_live else "NONE"
            elif not is_live:
                credit = "NONE"
            elif role == "direct_fact":
                credit = "DIRECT"
            elif role == "byte_owner":
                credit = "ENABLING"
            elif role in {"source", "eligibility", "mediator", "internal_support"}:
                credit = "ENABLING"
            else:
                credit = "NONE"
            feature_attribution.append({
                "feature_id": feature_id,
                "family": family,
                "role": role,
                "credit": credit,
                "identification_strength": "NONE",
                "component_intervention": False,
                "evidence_ids": ["E1"],
                "statement": (
                    "Role-correct live conformance established; no independent component "
                    "causal effect assigned."
                    if is_live else
                    "No role-correct live conformance and behavioral credit established."
                ),
            })

    experiment_result = positive_cohorts[0] if positive_cohorts else (
        powered_cohorts[0] if powered_cohorts else {}
    )
    opportunities = _opportunity_counts(records)
    return {
        "schema": SCHEMA,
        "study_id": study_id,
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "claim_contract": claim_contract,
        "system_identity": system_identity,
        "validity": {
            "status": validity_status,
            "failures": validity_failures,
            "task_validity_checked": False,
            "leakage_zero": bool(records) and all(
                int((record.get("integrity") or {}).get("leak_count") or 0) == 0
                for record in records
            ),
            "treatment_identity_proven": bool(records) and all(
                not ((record.get("ss_integrity") or {}).get(
                    "physical_identity_conflict_count"
                ) or 0)
                for record in records
            ),
            "outcome_validity_proven": False,
            "grader_calibrated": False,
            "evaluation_awareness_checked": False,
            "cohort_valid": cohort_valid,
            "cohort_failures": sorted({
                str(failure)
                for result in cohort_results.values()
                for failure in (result.get("failures") or [])
            }),
        },
        "level_1": {
            "scope": scope,
            "status": level1_status,
            "inventory_total": inventory["total"],
            "inventory_family_counts": inventory["family_counts"],
            "inventory_role_counts": inventory["role_counts"],
            "direct_delivery_rows": inventory["direct_delivery_count"],
            "applicable_rows": len(applicable),
            "ss_live_rows": len(live & applicable),
            "correct_quiet_or_not_eligible_rows": len(quiet),
            "dark_or_unmeasured_rows": len(dark),
            "violation_rows": len(violations),
            "violation_feature_ids": sorted(violations),
            "dark_or_unmeasured_feature_ids": sorted(dark),
            "all_applicable_role_standards_pass": (
                level1_status == "LEVEL1_PASS"
            ),
            "program_inventory_complete": inventory_complete,
            "statement": (
                f"{inventory['total']} executable rows were derived; "
                f"{inventory['direct_delivery_count']} are direct delivery, and no "
                "offline result was promoted to live conformance."
            ),
        },
        "opportunities": opportunities,
        "mechanism": {
            "exact_delivery_proven": level1_pass,
            "correct_information_proven": level1_pass,
            "timing_proven": level1_pass,
            "model_response_proven": bool(creditable_positive_cohorts) and level1_pass,
            "durable_state_change_proven": False,
            "reacquisition_excluded": bool(creditable_positive_cohorts) and level1_pass,
        },
        "level_2": {
            "status": level2_status,
            "identification_strength": strength,
            "credited_intervention_level1_pass": level1_pass,
            "reasoning_changed": False,
            "relevant_action_changed": bool(creditable_positive_cohorts) and level1_pass,
            "beneficial_durable_state_changed": False,
            "solution_path_proven": False,
            "correct_resolution": False,
            "harm_proven": False,
            "statement": (
                "A powered live randomized cohort changed the registered-receipt endpoint; "
                "beneficial durable progress and solve contribution remain unmeasured."
                if creditable_positive_cohorts and level1_pass else
                "Behavioral help is not creditable without Level-1-passing live operation "
                "and consequence evidence."
            ),
            "cohorts": cohort_results,
        },
        "experiment": {
            "intervention_executed": cohort_valid,
            "randomized": cohort_valid,
            "same_model_policy": bool(powered_cohorts) and cohort_valid,
            "n_tasks": int(experiment_result.get("n_tasks") or 0),
            "n_treatment": int(experiment_result.get("n_deliver") or 0),
            "n_control": int(experiment_result.get("n_holdout") or 0),
            "n_clusters": int(experiment_result.get("n_tasks") or 0),
            "effect_estimate": experiment_result.get("effect_estimate"),
            "effect_unit": "registered-receipt risk difference",
            "confidence_interval": experiment_result.get("confidence_interval"),
            "replay_fidelity": None,
            "assignment_probabilities_logged": cohort_valid,
            "held_out_test_set": bool(held_out_test_set and cohort_valid),
            "preregistered": bool(
                claim_contract.get("pre_specified") is True and cohort_valid
            ),
            "deployment_like_context": bool(deployment_like_context),
        },
        "evidence_items": evidence_items,
        "feature_attribution": feature_attribution,
        "verdict": {
            "final_verdict": final_verdict,
            "confidence": "unknown" if not powered_cohorts else "moderate",
            "statement": (
                "GT help is bounded by the two-level conformance-then-behavior rule; "
                "missing live or causal evidence is not replaced by offline validation."
            ),
        },
        "harms": [],
        "heterogeneity": [],
        "generalization_limits": [
            "A smoke witness cannot establish product-wide coverage.",
            "A registered-receipt effect is not automatically durable progress or a solve.",
        ],
        "unknowns": [
            "Representative live eligibility coverage for rows without SS-LIVE evidence.",
            "Beneficial durable-state and solution-path counterfactuals.",
        ],
        "next_identifying_experiment": (
            "Pre-register the live opportunity endpoint and minimum detectable effect, "
            "collect both randomized arms across independent task clusters, and continue "
            "the consequence horizon through durable verification state."
        ),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("inputs", nargs="+", type=Path)
    parser.add_argument("--study-id", required=True)
    parser.add_argument("--scope", choices=("task", "run", "product"), required=True)
    parser.add_argument("--claim-contract", type=Path, required=True)
    parser.add_argument("--system-identity", type=Path, required=True)
    parser.add_argument("--minimum-detectable-effect", type=float)
    parser.add_argument("--held-out-test-set", action="store_true")
    parser.add_argument("--deployment-like-context", action="store_true")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    records = [json.loads(path.read_text(encoding="utf-8")) for path in args.inputs]
    report = build_report(
        records,
        study_id=args.study_id,
        scope=args.scope,
        claim_contract=json.loads(args.claim_contract.read_text(encoding="utf-8")),
        system_identity=json.loads(args.system_identity.read_text(encoding="utf-8")),
        minimum_detectable_effect=args.minimum_detectable_effect,
        held_out_test_set=args.held_out_test_set,
        deployment_like_context=args.deployment_like_context,
    )
    args.output.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["SCHEMA", "build_report", "derived_inventory"]
