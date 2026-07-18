#!/usr/bin/env python3
"""Exact-128 SS proof contract and fail-closed producer-artifact audit (schema v3)."""
from __future__ import annotations

import hashlib
import json
import re
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

_SCRIPT_DIR = str(Path(__file__).resolve().parent)
if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)

try:
    from scripts.swebench.acq_provenance import ACQ_SOURCE_COMPONENTS
    from scripts.swebench.gt_feature_inventory import (
        canonical_feature_inventory,
        performance_metric_definitions,
    )
    from scripts.swebench.gt_feature_metrics import (
        layer_to_fact_class,
        member_fact_classes,
    )
    from scripts.swebench.gt_run_metrics import normalized_right_censor_observation
except ModuleNotFoundError:
    from acq_provenance import ACQ_SOURCE_COMPONENTS  # type: ignore[no-redef]
    from gt_feature_inventory import (  # type: ignore[no-redef]
        canonical_feature_inventory,
        performance_metric_definitions,
    )
    from gt_feature_metrics import (  # type: ignore[no-redef]
        layer_to_fact_class,
        member_fact_classes,
    )
    from gt_run_metrics import (  # type: ignore[no-redef]
        normalized_right_censor_observation,
    )

from groundtruth.runtime.fact_registry import (  # noqa: E402
    FACT_ROLE_INTERNAL_SUPPORT,
    REGISTRY,
    fact_role_for,
)
from groundtruth.runtime.feature_lineage import cap_role_for  # noqa: E402


SCHEMA = "gt.ss_proof_manifest.v3"
PREFLIGHT_SCHEMA = "gt.ss_proof_preflight.v3"
FAMILY_STAGE = {"ACQ": 0, "CAP": 1, "FACT": 1, "PERF": 2}
PER_FEATURE_OFFLINE_PROOF_DEPENDENCIES = (
    "red_first", "mutations_ge_2", "byte_identity_off", "lipi", "truth_proof",
    "writer_plumbing_fixture", "replay_feature_verdict",
)
GLOBAL_OFFLINE_PROOF_DEPENDENCIES = ("ss_gate_x2", "replay_oracle_execution")
DELIVERY_LIVE_PROOF_DEPENDENCIES = (
    "paid_live_trajectory", "producer_seal_join", "correct_info",
    "correct_rl_adhered_time", "receipt_ge_2", "leak_zero", "dose_lte_one", "fair_probe",
)
CAP_BYTE_OWNER_LIVE_PROOF_DEPENDENCIES = (
    *DELIVERY_LIVE_PROOF_DEPENDENCIES,
    "typed_cap_byte_owner",
)
SUPPORT_LIVE_PROOF_DEPENDENCIES = (
    "supported_fact_delivery_join", "candidate_local_contribution",
    "source_contribution_correct",
    "timing_inherited_from_fact_delivery", "source_causal_fair_probe",
)
INFRA_LIVE_PROOF_DEPENDENCIES = (
    "runtime_member_control_receipt", "mediated_fact_ids", "mediation_correct",
    "mediation_causal_fair_probe",
)
ELIGIBILITY_LIVE_PROOF_DEPENDENCIES = (
    "runtime_member_control_receipt", "eligibility_opportunity_receipt",
    "eligibility_decision_correct", "eligibility_causal_fair_probe",
)
MEASUREMENT_LIVE_PROOF_DEPENDENCIES = (
    "live_metric_artifact", "eight_decimal_precision", "formula_denominator_valid",
    "applicability_classified", "task_or_cohort_coverage", "aggregate_complete",
    "matched_delta_when_required",
)
FACT_RUNTIME_OWNERSHIP_PROOFS = (
    "runtime_evidence_type_registration", "runtime_producer_id",
    "required_event_match", "reactive_classification",
)
INTERNAL_FACT_SUPPORT_PROOFS = (
    "runtime_support_receipt", "supported_candidate_id",
    "downstream_decision_join", "support_correct", "support_causal_fair_probe",
)
ROW_FIELDS = (
    "family", "role", "terminal_contract", "eligibility", "ownership", "required_artifact_contracts",
    "observed_artifact_requirements", "truth_authority",
    "chronological_boundary_authority", "receipt_rule",
    "causal_fair_probe_requirement", "offline_proof_dependencies",
    "live_proof_dependencies", "BLOCKED_BY",
)
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


def _default_scoreboard() -> Path:
    return Path(__file__).resolve().parents[2] / ".claude" / "reports" / "SS_SCOREBOARD.json"


def _scoreboard_inventory(path: str | Path) -> dict[str, tuple[str, ...]]:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, TypeError) as exc:
        raise ValueError(f"ss proof manifest: scoreboard unreadable: {exc}") from exc
    raw = payload.get("feature_inventory") if isinstance(payload, dict) else None
    if not isinstance(raw, dict) or raw.get("count") != 129:
        raise ValueError("ss proof manifest: scoreboard inventory/count must declare exact 129")
    out: dict[str, tuple[str, ...]] = {}
    for family in ("ACQ", "CAP", "FACT", "PERF"):
        names = raw.get(family)
        if not isinstance(names, list) or not all(isinstance(name, str) for name in names):
            raise ValueError(f"ss proof manifest: scoreboard {family} inventory malformed")
        out[family] = tuple(names)
    return out


def _validate_scoreboard(path: str | Path, executable: dict[str, tuple[str, ...]]) -> None:
    scoreboard = _scoreboard_inventory(path)
    for family, names in executable.items():
        if len(scoreboard[family]) != len(names) or set(scoreboard[family]) != set(names):
            raise ValueError(f"ss proof manifest: scoreboard {family} name/count drift")


def _writers(*artifacts: str) -> dict[str, str]:
    authorities = {
        "brief_result": "groundtruth.runtime.brief_cache",
        "profile_activation": "workflow Profile-2 activation writer",
        "runtime_ledger": "runtime delivery-ledger writer",
        "trajectory": "mini-swe-agent trajectory writer",
        "deep_metrics": "scripts.swebench.gt_deep_metrics",
        "run_metrics": "scripts.swebench.gt_run_metrics",
    }
    return {artifact: authorities[artifact] for artifact in artifacts}


def _acq_row(name: str) -> dict[str, Any]:
    component = ACQ_SOURCE_COMPONENTS.get(name)
    supported = component is not None
    return {
        "family": "ACQ",
        "role": "support",
        "terminal_contract": {
            "kind": "support_contribution",
            "success": "candidate-local contribution joined to one FACT delivery plus causal ablation",
            "independent_delivery_gates": False,
        },
        "eligibility": {
            "authority": "brief_result.json#metrics.localization_proof",
            "predicate": component if supported else f"UNREGISTERED:{name}",
        },
        "ownership": {
            "exact": False, "kind": "supports_fact_delivery", "producer": None,
            "layers": [], "supported_fact_classes": ["localization"],
            "source_component": component,
            "authority": "scripts.swebench.acq_provenance.ACQ_SOURCE_COMPONENTS",
        },
        "required_artifact_contracts": _writers("brief_result", "runtime_ledger", "trajectory"),
        "observed_artifact_requirements": ["brief_result", "runtime_ledger", "trajectory"],
        "truth_authority": (
            {"component": component, "requires_supported_fact_truth_join": True}
            if supported else None
        ),
        "chronological_boundary_authority": (
            {
                "authority": "joined FACT registration_for(evidence_type).required_event",
                "deliver_by": None,
                "inherits_actual_fact_delivery_boundary": True,
            }
            if supported else None
        ),
        "receipt_rule": {
            "independent_receipt": False,
            "rule": "inherit the single supported FACT receipt; never duplicate by ACQ source",
        },
        "causal_fair_probe_requirement": "source contribution within supported FACT delivery",
        "offline_proof_dependencies": list(PER_FEATURE_OFFLINE_PROOF_DEPENDENCIES),
        "live_proof_dependencies": list(SUPPORT_LIVE_PROOF_DEPENDENCIES),
        "BLOCKED_BY": [] if supported else [f"ACQ_AUTHORITY_UNREGISTERED:{name}"],
    }


def _cap_row(name: str) -> dict[str, Any]:
    cap_role = cap_role_for(name)
    fact_classes = list(member_fact_classes(name)) or ["*"]
    row_role = {
        "byte_owner": "capability_support",
        "mediator": "infra_control",
        "eligibility": "eligibility_control",
    }[cap_role]
    terminal = {
        "byte_owner": {
            "kind": "capability_delivery",
            "success": "exact typed CAP-owned bytes satisfy all seven SS delivery gates",
            "independent_delivery_gates": True,
        },
        "mediator": {
            "kind": "control_mediation",
            "success": "correct mediation over explicit FACT ids with causal intervention",
            "independent_delivery_gates": False,
        },
        "eligibility": {
            "kind": "eligibility_control",
            "success": "correct opportunity decision with a feature-specific causal intervention",
            "independent_delivery_gates": False,
        },
    }[cap_role]
    live_dependencies = {
        "byte_owner": CAP_BYTE_OWNER_LIVE_PROOF_DEPENDENCIES,
        "mediator": INFRA_LIVE_PROOF_DEPENDENCIES,
        "eligibility": ELIGIBILITY_LIVE_PROOF_DEPENDENCIES,
    }[cap_role]
    return {
        "family": "CAP",
        "role": row_role,
        "terminal_contract": terminal,
        "eligibility": {
            "authority": "task_context executable opportunity receipt",
            "predicate": f"opportunity({name}, task, event) == true; flag-on is insufficient",
        },
        "ownership": {
            "exact": False, "kind": f"capability_{cap_role}", "producer": None,
            "cap_role": cap_role,
            "layers": [], "candidate_profile_member": name,
            "mediated_fact_classes": fact_classes,
            "authority": "groundtruth.runtime.feature_lineage.cap_role_for",
        },
        "required_artifact_contracts": _writers("profile_activation", "runtime_ledger", "trajectory"),
        "observed_artifact_requirements": ["profile_activation", "runtime_ledger", "trajectory"],
        "truth_authority": {
            "byte_owner": "payload correctness for the exact typed CAP-owned bytes",
            "mediator": "mediation correctness over explicitly mediated FACT ids",
            "eligibility": "opportunity decision correctness for this CAP control",
        }[cap_role],
        "chronological_boundary_authority": (
            "typed owned FACT required_event" if cap_role == "byte_owner" else None
        ),
        "receipt_rule": {
            "independent_receipt": False,
            "rule": (
                "join the exact owned-byte receipt through typed CAP lineage; never inherit by FACT proximity"
                if cap_role == "byte_owner"
                else "N/A; control roles never inherit FACT delivery receipts"
            ),
        },
        "causal_fair_probe_requirement": {
            "byte_owner": "feature-specific matched delivery probe",
            "mediator": "matched mediation intervention",
            "eligibility": "matched eligibility-decision intervention",
        }[cap_role],
        "offline_proof_dependencies": list(PER_FEATURE_OFFLINE_PROOF_DEPENDENCIES),
        "live_proof_dependencies": list(live_dependencies),
        "BLOCKED_BY": [],
    }


def _fact_row(name: str) -> dict[str, Any]:
    registration = REGISTRY[name]
    if fact_role_for(name) == FACT_ROLE_INTERNAL_SUPPORT:
        return {
            "family": "FACT", "role": "internal_support",
            "terminal_contract": {
                "kind": "internal_support_control",
                "success": (
                    "typed internal contribution is joined to a downstream decision "
                    "with a causal support probe"
                ),
                "independent_delivery_gates": False,
            },
            "eligibility": {
                "authority": "typed internal producer opportunity receipt",
                "predicate": f"internal_opportunity({name}) == true",
            },
            "ownership": {
                "exact": False, "kind": "internal_fact_support", "producer": None,
                "layers": [], "candidate_producer": registration.producer,
                "candidate_fact_class": name,
                "runtime_requirements": list(INTERNAL_FACT_SUPPORT_PROOFS),
                "authority": "fact_registry.fact_role_for + internal producer receipt",
            },
            "required_artifact_contracts": _writers(
                "brief_result", "runtime_ledger", "trajectory"
            ),
            "observed_artifact_requirements": [
                "brief_result", "runtime_ledger", "trajectory",
            ],
            "truth_authority": {
                "internal_support_only": True,
                "freshness_dependencies": list(registration.freshness_deps),
                "missing_writer": (
                    "brief_result.metrics.localization_proof[].cochange_evidence "
                    "with exact cochange rows and cochange revision"
                ),
            },
            "chronological_boundary_authority": None,
            "receipt_rule": {
                "independent_receipt": False,
                "rule": (
                    "join a typed internal contribution to its downstream decision; "
                    "never claim a model-facing delivery receipt"
                ),
            },
            "causal_fair_probe_requirement": registration.causal_eval,
            "offline_proof_dependencies": list(PER_FEATURE_OFFLINE_PROOF_DEPENDENCIES),
            "live_proof_dependencies": list(INTERNAL_FACT_SUPPORT_PROOFS),
            "BLOCKED_BY": [
                "COCHANGE_INTERNAL_TRUTH_WITNESS_ABSENT",
                "COCHANGE_INTERNAL_CAUSAL_PROBE_ABSENT",
            ],
        }
    return {
        "family": "FACT", "role": "fact_delivery",
        "terminal_contract": {
            "kind": "seven_gate_delivery", "success": "all seven SS gates",
            "independent_delivery_gates": True,
        },
        "eligibility": {
            "authority": "fact_registry registration + executable event opportunity receipt",
            "predicate": f"opportunity({name}, {registration.earliest_event}) == true",
        },
        "ownership": {
            "exact": False, "kind": "runtime_fact_attribution", "producer": None,
            "layers": [], "candidate_producer": registration.producer,
            "candidate_fact_class": name,
            "runtime_requirements": list(FACT_RUNTIME_OWNERSHIP_PROOFS),
            "authority": "registration_for(evidence_type)+producer_id+required_event+is_reactive",
        },
        "required_artifact_contracts": _writers("runtime_ledger", "trajectory"),
        "observed_artifact_requirements": ["runtime_ledger", "trajectory"],
        "truth_authority": {
            "registry_contract_only": True, "payload_truth_proof_required": True,
            "freshness_dependencies": list(registration.freshness_deps),
        },
        "chronological_boundary_authority": {
            "authority": "registration_for(runtime evidence_type).required_event",
            "deliver_by": None, "canonical_fallback": registration.deliver_by,
            "requires_runtime_evidence_type": True, "requires_chronological_join": True,
        },
        "receipt_rule": {"predicate": registration.receipt_predicate, "minimum_level": 2},
        "causal_fair_probe_requirement": registration.causal_eval,
        "offline_proof_dependencies": list(PER_FEATURE_OFFLINE_PROOF_DEPENDENCIES),
        "live_proof_dependencies": [
            *DELIVERY_LIVE_PROOF_DEPENDENCIES, *FACT_RUNTIME_OWNERSHIP_PROOFS,
        ],
        "BLOCKED_BY": [],
    }


def _perf_authorities() -> dict[str, tuple[str, str]]:
    return {
        name: (section, value_type)
        for section, definitions in performance_metric_definitions().items()
        for name, value_type in definitions
    }


def _perf_row(name: str, authority: tuple[str, str]) -> dict[str, Any]:
    section, value_type = authority
    run_level = value_type == "run_ratio"
    artifact = "run_metrics" if run_level else "deep_metrics"
    return {
        "family": "PERF", "role": "measurement",
        "terminal_contract": {
            "kind": "typed_live_measurement",
            "success": "typed, applicable, 8dp, coverage-complete aggregate measurement",
            "independent_delivery_gates": False,
        },
        "eligibility": {
            "authority": "scripts.swebench.gt_run_metrics._MANDATORY_METRICS",
            "predicate": f"{section}.{name} structurally satisfies {value_type}",
        },
        "ownership": {
            "exact": True, "kind": "typed_metric", "producer": artifact,
            "layers": [], "section": section, "value_type": value_type,
            "scope": "run" if run_level else "task",
            "authority": "scripts.swebench.gt_run_metrics._MANDATORY_METRICS",
        },
        "required_artifact_contracts": _writers(artifact),
        "observed_artifact_requirements": [artifact],
        "truth_authority": {
            "metric_contract": f"{section}.{name}:{value_type}",
            "structural_validation_required": True,
        },
        "chronological_boundary_authority": None, "receipt_rule": None,
        "causal_fair_probe_requirement": None,
        "offline_proof_dependencies": [
            "metric_red_first", "metric_mutations_ge_2", "metric_lipi",
            "formula_denominator_fixture", "applicability_fixture",
            "coverage_fixture", "aggregate_completeness_fixture",
        ],
        "live_proof_dependencies": list(MEASUREMENT_LIVE_PROOF_DEPENDENCIES),
        "BLOCKED_BY": [],
    }


def build_ss_proof_manifest(scoreboard_path: str | Path | None = None) -> dict[str, Any]:
    inventory = canonical_feature_inventory()
    _validate_scoreboard(scoreboard_path or _default_scoreboard(), inventory)
    perf = _perf_authorities()
    features: dict[str, dict[str, Any]] = {}
    for family, names in inventory.items():
        for name in names:
            row = (
                _acq_row(name) if family == "ACQ" else
                _cap_row(name) if family == "CAP" else
                _fact_row(name) if family == "FACT" else
                _perf_row(name, perf[name])
            )
            if tuple(row) != ROW_FIELDS:
                raise ValueError(f"ss proof manifest: row schema drift for {name}")
            features[name] = row
    if len(features) != 129:
        raise ValueError(f"ss proof manifest: expected 129 unique rows, got {len(features)}")
    return {
        "schema": SCHEMA, "feature_count": 129,
        "family_counts": {family: len(names) for family, names in inventory.items()},
        "global_prerequisites": list(GLOBAL_OFFLINE_PROOF_DEPENDENCIES),
        "preflight_stage_order": dict(FAMILY_STAGE), "features": features,
    }


_SHA16 = re.compile(r"^[0-9a-f]{16}$")
_REQUIRED_WRITER_CHANGES = (
    "dispatch: no caller invokes preflight_ss_proof; audit mode cannot authorize dispatch",
    "ss_gate: add producer-owned execution_id plus run_id and seam_sha256 to the canonical report",
    "ss_replay_oracle: add execution_id, run_id, seam_sha256, and source_hashes",
    "fixture proof: add a canonical producer for RED, mutation apply/RED/restore, byte-off, and LIPI",
    "trajectory and runtime ledger: persist run_id, task_id, and seam_sha256",
    "gt_feature_metrics: persist run_id/seam_sha256 and publishable exact-128 typed integrity",
    "gt_run_metrics.v2: persist seam_sha256 and all 58 formula/applicability records",
    "live audit: persist causal fair-probe evidence; smoke summary booleans are not proof inputs",
)


def _open_bytes(reference: object, context: Mapping[str, Any]) -> bytes | None:
    if not isinstance(reference, Mapping):
        return None
    relative, claimed, root_raw = (
        reference.get("path"), reference.get("sha256"), context.get("proof_root")
    )
    if (
        not isinstance(relative, str) or not relative or Path(relative).is_absolute()
        or not isinstance(claimed, str) or not _SHA256.fullmatch(claimed)
        or not isinstance(root_raw, str) or not root_raw
    ):
        return None
    root = Path(root_raw).resolve()
    candidate = (root / relative).resolve()
    try:
        candidate.relative_to(root)
        raw = candidate.read_bytes()
    except (OSError, ValueError):
        return None
    return raw if hashlib.sha256(raw).hexdigest() == claimed else None


def _open_json(reference: object, context: Mapping[str, Any]) -> dict[str, Any] | None:
    raw = _open_bytes(reference, context)
    if raw is None:
        return None
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _open_jsonl(reference: object, context: Mapping[str, Any]) -> list[dict[str, Any]] | None:
    raw = _open_bytes(reference, context)
    if raw is None:
        return None
    rows: list[dict[str, Any]] = []
    try:
        for line in raw.decode("utf-8").splitlines():
            if not line.strip():
                continue
            value = json.loads(line)
            if not isinstance(value, dict):
                return None
            rows.append(value)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return rows or None


def _run_context_errors(context: Mapping[str, Any]) -> list[str]:
    errors: list[str] = []
    for key in ("proof_root", "run_id"):
        if not isinstance(context.get(key), str) or not context[key]:
            errors.append(f"missing_{key}")
    seam = context.get("seam_sha256")
    if not isinstance(seam, str) or not _SHA256.fullmatch(seam):
        errors.append("missing_seam_sha256")
    return errors


def _task_context_errors(context: Mapping[str, Any]) -> list[str]:
    errors = _run_context_errors(context)
    if not isinstance(context.get("task_id"), str) or not context["task_id"]:
        errors.append("missing_task_id")
    return errors


def _bound_run(value: Mapping[str, Any], context: Mapping[str, Any]) -> bool:
    return bool(
        value.get("run_id") == context.get("run_id")
        and value.get("seam_sha256") == context.get("seam_sha256")
    )


def _bound_task(value: Mapping[str, Any], context: Mapping[str, Any]) -> bool:
    return bool(
        _bound_run(value, context)
        and value.get("task_id", value.get("task")) == context.get("task_id")
    )


def _audit_gate_pair(references: object, context: Mapping[str, Any]) -> dict[str, Any]:
    errors = list(_run_context_errors(context))
    reports: list[dict[str, Any]] = []
    if not isinstance(references, list) or len(references) != 2:
        errors.append("gate_requires_two_executions")
    else:
        for index, reference in enumerate(references):
            report = _open_json(reference, context)
            if report is None:
                errors.append(f"gate_{index}_unreadable_or_hash_mismatch")
                continue
            reports.append(report)
            if report.get("gate") != "ss_gate" or report.get("exit_code") != 0:
                errors.append(f"gate_{index}_not_green")
            if not _bound_run(report, context):
                errors.append(f"gate_{index}_missing_run_or_seam_binding")
            execution_id = report.get("execution_id")
            if not isinstance(execution_id, str) or not execution_id:
                errors.append(f"gate_{index}_missing_producer_execution_id")
            scenarios = report.get("scenarios")
            if not isinstance(scenarios, list):
                errors.append(f"gate_{index}_scenarios_absent")
                continue
            by_id = {
                row.get("id"): row for row in scenarios
                if isinstance(row, Mapping) and isinstance(row.get("id"), str)
            }
            if set(by_id) != {f"S{i}" for i in range(12)}:
                errors.append(f"gate_{index}_scenario_inventory_mismatch")
                continue
            derived = {
                verdict: sum(1 for row in by_id.values() if row.get("verdict") == verdict)
                for verdict in ("PASS", "HONEST-STRUCTURAL-SKIP", "FAIL", "ERROR")
            }
            if derived != {
                "PASS": 11, "HONEST-STRUCTURAL-SKIP": 1, "FAIL": 0, "ERROR": 0,
            }:
                errors.append(f"gate_{index}_derived_counts_not_11_1_0")
            if by_id["S8"].get("verdict") != "HONEST-STRUCTURAL-SKIP":
                errors.append(f"gate_{index}_S8_not_honest_structural_skip")
    ids = [report.get("execution_id") for report in reports]
    if len(ids) == 2 and len(set(ids)) != 2:
        errors.append("gate_execution_ids_not_unique")
    return {"valid": not errors, "errors": sorted(set(errors)), "executions": len(reports)}


def _audit_replay(reference: object, context: Mapping[str, Any]) -> dict[str, Any]:
    errors = list(_run_context_errors(context))
    report = _open_json(reference, context)
    if report is None:
        errors.append("replay_unreadable_or_hash_mismatch")
        return {"valid": False, "errors": sorted(set(errors)), "task_count": 0}
    if report.get("schema") != "ss.replay_oracle_report.v2" or report.get("exit_code") != 0:
        errors.append("replay_schema_or_exit_invalid")
    if not _bound_run(report, context):
        errors.append("replay_missing_run_or_seam_binding")
    if not isinstance(report.get("execution_id"), str) or not report["execution_id"]:
        errors.append("replay_missing_producer_execution_id")
    expected = context.get("expected_replay_tasks")
    if (
        not isinstance(expected, list) or len(expected) != 29
        or len(set(expected)) != 29
        or not all(isinstance(task, str) and task for task in expected)
    ):
        errors.append("expected_replay_tasks_not_exact_29")
        expected_set: set[str] = set()
    else:
        expected_set = set(expected)
    reconstructed = report.get("tasks_reconstructed")
    reconstructed_set = (
        set(reconstructed)
        if isinstance(reconstructed, list)
        and all(isinstance(task, str) and task for task in reconstructed)
        else set()
    )
    if reconstructed_set != expected_set or len(reconstructed_set) != 29:
        errors.append("replay_reconstructed_tasks_not_exact_29")
    fixpoint = report.get("fixpoint")
    if not isinstance(fixpoint, Mapping) or set(fixpoint) != expected_set:
        errors.append("replay_fixpoint_task_coverage_mismatch")
    elif any(
        not isinstance(row, Mapping) or row.get("replayed") is not True
        or not (row.get("strict") is True or row.get("channel") is True)
        for row in fixpoint.values()
    ):
        errors.append("replay_fixpoint_not_faithful")
    cases = report.get("cases")
    if not isinstance(cases, list) or not cases:
        errors.append("replay_cases_absent")
    else:
        case_tasks = {
            row.get("task") for row in cases
            if isinstance(row, Mapping) and isinstance(row.get("task"), str)
        }
        if not case_tasks or not case_tasks.issubset(expected_set):
            errors.append("replay_case_task_coverage_invalid")
        if any(
            not isinstance(row, Mapping) or row.get("verdict") not in {"PASS", "SKIP"}
            for row in cases
        ):
            errors.append("replay_case_failure_or_unfaithful")
    invariants = report.get("invariants")
    if not isinstance(invariants, list) or not invariants:
        errors.append("replay_invariants_absent")
    elif any(
        not isinstance(row, Mapping) or row.get("verdict") not in {"PASS", "SKIP"}
        for row in invariants
    ):
        errors.append("replay_invariant_failure")
    hashes = report.get("source_hashes")
    required_hashes = {"recorded_manifest", "snapshots_manifest", "cases_manifest", "seam_source"}
    if (
        not isinstance(hashes, Mapping) or set(hashes) != required_hashes
        or any(
            not isinstance(value, str) or not _SHA256.fullmatch(value)
            for value in hashes.values()
        )
    ):
        errors.append("replay_source_hashes_absent_or_malformed")
    for forbidden in ("feature_coverage", "feature_verdicts"):
        if forbidden in report:
            errors.append(f"replay_self_attested_{forbidden}_ignored")
    return {
        "valid": not errors,
        "errors": sorted(set(errors)),
        "task_count": len(reconstructed_set),
    }


def _visible_messages(trajectory: Mapping[str, Any]) -> list[tuple[int, str]]:
    messages = trajectory.get("messages")
    if not isinstance(messages, list):
        return []
    visible: list[tuple[int, str]] = []
    for index, message in enumerate(messages):
        if not isinstance(message, Mapping) or not isinstance(message.get("content"), str):
            continue
        if (
            message.get("role") in {"user", "tool", "system", "exit"}
            or message.get("type") == "function_call_output"
        ):
            visible.append((index, message["content"]))
    return visible


def _sealed_window(content: str, chars: int, seal: str) -> str | None:
    if chars <= 0 or not _SHA16.fullmatch(seal):
        return None
    for start in range(len(content) - chars + 1):
        window = content[start:start + chars]
        if hashlib.sha256(window.encode()).hexdigest()[:16] == seal:
            return window
    return None


def _later_acknowledges(
    trajectory: Mapping[str, Any], home: int, file_path: str,
) -> bool:
    messages = trajectory.get("messages")
    if not isinstance(messages, list) or not file_path:
        return False
    entity = Path(file_path).name
    for message in messages[home + 1:]:
        if not isinstance(message, Mapping) or message.get("role") != "assistant":
            continue
        content = message.get("content")
        if isinstance(content, str) and (file_path in content or entity in content):
            return True
    return False


def _payload_leaks(payload: str) -> bool:
    lowered = payload.lower()
    return bool(
        "fail_to_pass" in lowered
        or re.search(r"\btest_[a-z0-9_]+\b", lowered)
        or re.search(r"\bassert(?:ion)?\b", lowered)
    )


def _audit_runtime(
    ledger_reference: object,
    trajectory_reference: object,
    context: Mapping[str, Any],
) -> dict[str, Any]:
    errors = list(_task_context_errors(context))
    rows = _open_jsonl(ledger_reference, context)
    trajectory = _open_json(trajectory_reference, context)
    if rows is None:
        errors.append("runtime_ledger_unreadable_or_hash_mismatch")
    if trajectory is None:
        errors.append("trajectory_unreadable_or_hash_mismatch")
    if rows is None or trajectory is None:
        return {"valid": False, "errors": sorted(set(errors)), "deliveries": []}
    if not _bound_task(trajectory, context):
        errors.append("trajectory_missing_run_task_seam_binding")
    visible = _visible_messages(trajectory)
    deliveries: list[dict[str, Any]] = []
    homes: dict[int, int] = {}
    delivered_rows = [
        row for row in rows
        if row.get("outcome") == "delivered"
        and isinstance(row.get("chars_delivered"), int)
        and not isinstance(row.get("chars_delivered"), bool)
        and row["chars_delivered"] > 0
    ]
    for index, row in enumerate(delivered_rows):
        if not _bound_task(row, context):
            errors.append(f"ledger_delivery_{index}_missing_run_task_seam_binding")
            continue
        seal = row.get("content_sha256_16")
        if not isinstance(seal, str):
            errors.append(f"ledger_delivery_{index}_missing_seal")
            continue
        match: tuple[int, str] | None = None
        for message_index, content in visible:
            payload = _sealed_window(content, row["chars_delivered"], seal)
            if payload is not None:
                match = message_index, payload
                break
        if match is None:
            errors.append(f"ledger_delivery_{index}_seal_not_in_trajectory")
            continue
        home, payload = match
        homes[home] = homes.get(home, 0) + 1
        fact = layer_to_fact_class(str(row.get("layer") or ""))
        member = row.get("profile_member")
        deliveries.append({
            "ledger_index": index,
            "home_message": home,
            "fact_class": fact,
            "profile_member": member if isinstance(member, str) else None,
            "delivered_byte_proven": True,
            # The generic delivery writer owns bytes and routing telemetry, not
            # semantic truth or chronological adjudication.  Row-level summary
            # flags are therefore untrusted input; typed producer/adjudicator
            # artifacts must establish these gates in the live-evidence join.
            "correct_info": None,
            "correct_rl_adhered_time": None,
            "acknowledged": _later_acknowledges(
                trajectory, home, str(row.get("file_path") or "")
            ),
            "leak_zero": not _payload_leaks(payload),
            "dose_lte_one": None,
            "fair_probe": None,
        })
    for delivery in deliveries:
        delivery["dose_lte_one"] = homes.get(delivery["home_message"], 0) <= 1
    if not deliveries:
        errors.append("no_byte_joined_deliveries")
    return {
        "valid": not errors,
        "errors": sorted(set(errors)),
        "deliveries": deliveries,
    }


def _audit_feature_metrics(
    reference: object, context: Mapping[str, Any], manifest: Mapping[str, Any],
) -> dict[str, Any]:
    errors = list(_task_context_errors(context))
    record = _open_json(reference, context)
    if record is None:
        errors.append("feature_metrics_unreadable_or_hash_mismatch")
        return {"valid": False, "errors": sorted(set(errors))}
    if record.get("schema") != "gt.feature_metrics.v1":
        errors.append("feature_metrics_schema_invalid")
    if not _bound_task(record, context):
        errors.append("feature_metrics_missing_run_task_seam_binding")
    canonical = record.get("ss_features")
    features = manifest["features"]
    if not isinstance(canonical, Mapping) or set(canonical) != set(features):
        errors.append("feature_metrics_not_exact_128")
        canonical = {}
    integrity = record.get("ss_integrity")
    if (
        not isinstance(integrity, Mapping)
        or integrity.get("inventory_complete") is not True
        or integrity.get("required_inputs_complete") is not True
        or integrity.get("publishable") is not True
    ):
        errors.append("feature_metrics_ss_integrity_not_publishable")
    for name, row in features.items():
        item = canonical.get(name)
        if not isinstance(item, Mapping) or item.get("family") != row["family"]:
            continue
        readiness = item.get("ss_readiness")
        role = readiness.get("role") if isinstance(readiness, Mapping) else None
        if role != row["role"]:
            errors.append(f"feature_metrics_role_mismatch:{name}")
            continue
        gates = readiness.get("gates")
        if not isinstance(gates, Mapping):
            errors.append(f"feature_metrics_gates_absent:{name}")
            continue
        if row["family"] == "ACQ":
            ablation = item.get("ablation")
            if (
                item.get("source") != "acq_provenance"
                or not isinstance(ablation, Mapping)
                or ablation.get("source_on") is not True
                or ablation.get("source_off") is not True
                or ablation.get("causal_delta") is not True
            ):
                errors.append(f"feature_metrics_acq_ablation_absent:{name}")
        if row["family"] == "PERF":
            forbidden = {"delivered_byte_proven", "acknowledged", "fair_probe"}
            if forbidden.intersection(gates):
                errors.append(f"feature_metrics_perf_delivery_shape:{name}")
            status = item.get("status")
            censor_valid = True
            if status == "RIGHT_CENSORED":
                censor_valid = bool(
                    item.get("value") is None
                    and normalized_right_censor_observation(
                        row["ownership"]["section"], name, item.get("observation")
                    ) is not None
                )
            if (
                status not in {
                    "MEASURED", "NOT_APPLICABLE", "RIGHT_CENSORED",
                }
                or not censor_valid
                or item.get("source") not in {"gt_deep_metrics", "gt_run_metrics"}
                or item.get("metric_structure_valid") is not True
                or item.get("artifact_schema_valid") is not True
                or item.get("precision_decimals") != 8
                or not isinstance(item.get("formula_provenance"), str)
                or not isinstance(item.get("denominator_provenance"), str)
            ):
                errors.append(f"feature_metrics_perf_contract_invalid:{name}")
    return {"valid": not errors, "errors": sorted(set(errors))}


def _audit_run_metrics(
    reference: object, context: Mapping[str, Any], manifest: Mapping[str, Any],
) -> dict[str, Any]:
    errors = list(_run_context_errors(context))
    record = _open_json(reference, context)
    if record is None:
        errors.append("run_metrics_unreadable_or_hash_mismatch")
        return {"valid": False, "errors": sorted(set(errors))}
    if record.get("schema") != "gt_run_metrics.v2":
        errors.append("run_metrics_schema_invalid")
    if not _bound_run(record, context):
        errors.append("run_metrics_missing_run_or_seam_binding")
    if (
        record.get("precision_decimals") != 8
        or record.get("mandatory_performance_metric_count") != 58
        or record.get("mandatory_performance_collection_complete") is not True
        or record.get("mandatory_performance_collection_failures") != []
        or record.get("mandatory_performance_metrics_with_missing_tasks") != []
    ):
        errors.append("run_metrics_collection_incomplete")
    mandatory = record.get("mandatory_performance")
    for name, row in manifest["features"].items():
        if row["family"] != "PERF":
            continue
        section = row["ownership"]["section"]
        section_rows = mandatory.get(section) if isinstance(mandatory, Mapping) else None
        metric = section_rows.get(name) if isinstance(section_rows, Mapping) else None
        if (
            not isinstance(metric, Mapping)
            or metric.get("value_type") != row["ownership"]["value_type"]
            or metric.get("status") not in {"MEASURED", "NOT_APPLICABLE"}
            or metric.get("missing_tasks") != []
            or "mean" not in metric or "median" not in metric
        ):
            errors.append(f"run_metrics_perf_invalid:{name}")
    return {"valid": not errors, "errors": sorted(set(errors))}


def preflight_ss_proof(
    manifest: Mapping[str, Any], *, mode: str = "dispatch",
    task_context: Mapping[str, Any] | None = None,
    canonical_artifacts: Mapping[str, Any] | None = None,
    **_ignored_untrusted_inputs: object,
) -> dict[str, Any]:
    """Audit actual producer artifacts without authorizing dispatch or proof promotion."""
    features = manifest.get("features")
    if manifest.get("schema") != SCHEMA or not isinstance(features, Mapping) or len(features) != 129:
        raise ValueError("ss proof preflight: invalid exact-129 v3 manifest")
    if mode not in {"dispatch", "postrun"}:
        raise ValueError(f"ss proof preflight: unsupported mode {mode!r}")
    context = task_context or {}
    artifacts = canonical_artifacts or {}
    audits = {
        "gate_x2": _audit_gate_pair(artifacts.get("gate_reports"), context),
        "replay_exact29": _audit_replay(artifacts.get("replay_oracle"), context),
        "runtime": _audit_runtime(
            artifacts.get("runtime_ledger"), artifacts.get("trajectory"), context
        ),
        "feature_metrics": _audit_feature_metrics(
            artifacts.get("feature_metrics"), context, manifest
        ),
        "run_metrics": _audit_run_metrics(
            artifacts.get("run_metrics"), context, manifest
        ),
        "fixture_burden": {
            "valid": False,
            "errors": ["canonical_fixture_evidence_producer_absent"],
        },
        "live_seven_gates": {
            "valid": False,
            "errors": [
                "recompute_from_runtime_deliveries_only",
                "correct_info_requires_producer_truth_and_freshness_fields",
                "timing_requires_producer_timing_verdict",
                "fair_probe_requires_canonical_matched_or_shadow_evidence",
            ],
            "smoke_summary_input_ignored": "smoke_audit" in artifacts,
        },
    }
    blocked = [name for name, row in features.items() if row["BLOCKED_BY"]]
    pending = [name for name in features if name not in blocked]
    if mode == "dispatch":
        buckets = {
            "support_ready": [], "capability_support_ready": [], "infra_control_ready": [],
            "fact_delivery_ready": [], "internal_support_ready": [],
            "measurement_ready": [],
            "unknown": pending, "blocked": blocked,
        }
        status = "dispatch_not_integrated_audit_only"
    else:
        buckets = {
            "support_complete": [], "capability_support_complete": [],
            "infra_control_complete": [], "fact_delivery_complete": [],
            "internal_support_complete": [],
            "measurement_complete": [], "incomplete": pending, "blocked": blocked,
        }
        status = "postrun_audit_only_no_promotion"
    ordered = [
        {
            "feature": name,
            "family": row["family"],
            "role": row["role"],
            "stage": FAMILY_STAGE[row["family"]],
            "status": "architecturally_blocked" if name in blocked else status,
            "BLOCKED_BY": list(row["BLOCKED_BY"]),
        }
        for name, row in features.items()
    ]
    return {
        "schema": PREFLIGHT_SCHEMA,
        "mode": mode,
        "dispatch_ready_enabled": False,
        "proof_promotion_enabled": False,
        "dispatch_integration": "absent:no production caller",
        "meaning": "audit-only and fail-closed; never authorizes dispatch or SS proof bits",
        "required_writer_changes": list(_REQUIRED_WRITER_CHANGES),
        "audits": audits,
        "counts": {name: len(values) for name, values in buckets.items()},
        **buckets,
        "ordered": ordered,
    }


__all__ = [
    "ROW_FIELDS", "PER_FEATURE_OFFLINE_PROOF_DEPENDENCIES",
    "GLOBAL_OFFLINE_PROOF_DEPENDENCIES", "DELIVERY_LIVE_PROOF_DEPENDENCIES",
    "FACT_RUNTIME_OWNERSHIP_PROOFS", "INTERNAL_FACT_SUPPORT_PROOFS",
    "build_ss_proof_manifest", "preflight_ss_proof",
]
