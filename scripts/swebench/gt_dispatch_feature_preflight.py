#!/usr/bin/env python3
"""Fail-closed static dispatch audit for the exact 128-feature inventory.

The manifest is derived from source-declared authority tables in this checkout.
It proves only that static producer/control, collector, relationship, and
artifact plumbing exists; it never proves runtime executability, opportunity,
delivery, acknowledgment, or SS-LIVE.
"""
from __future__ import annotations

import argparse
import ast
import json
from functools import lru_cache
from pathlib import Path
from typing import Any

from acq_provenance import ACQ_PRODUCER_AUTHORITIES, ACQ_SOURCE_COMPONENTS
from gt_feature_inventory import canonical_feature_inventory, performance_metric_definitions
from groundtruth.runtime.control_participation import control_contract
from groundtruth.runtime.fact_registry import registration
from groundtruth.runtime.feature_lineage import (
    CAP_BYTE_OWNER_MECHANISMS,
    cap_role_for,
)


SCHEMA = "gt.static_dispatch_feature_manifest.v1"
AUTHORITY_FIELDS = (
    "producer_authority", "collector_authority", "evidence_relationship", "terminal_artifact",
)
_ROOT = Path(__file__).resolve().parents[2]
_SOURCE_ROOTS = (_ROOT / "src", _ROOT / "scripts" / "swebench")


def _perf_authorities() -> dict[str, tuple[str, str]]:
    return {
        name: (section, value_type)
        for section, definitions in performance_metric_definitions().items()
        for name, value_type in definitions
    }


@lru_cache(maxsize=None)
def _checkout_module_source(module_name: str) -> Path | None:
    """Resolve a module from this checkout with exact-case path components."""
    if not module_name or any(not part for part in module_name.split(".")):
        return None
    parts = module_name.split(".")
    relative_candidates = (
        (*parts[:-1], f"{parts[-1]}.py"),
        (*parts, "__init__.py"),
    )
    for root in _SOURCE_ROOTS:
        for relative in relative_candidates:
            current = root
            matched = True
            for component in relative:
                try:
                    if component not in {entry.name for entry in current.iterdir()}:
                        matched = False
                        break
                except OSError:
                    matched = False
                    break
                current /= component
            if matched and current.is_file():
                return current
    return None


@lru_cache(maxsize=None)
def _source_declares_callable(module_name: str, attribute: str) -> bool:
    """Verify an exact direct declaration without importing runtime deps.

    The prepare runner is a static-audit host, not the substrate that executes
    acquisition.  Optional runtime dependencies may therefore be absent there.
    Reading the checkout module's AST preserves a strict authority check: only
    an exact top-level callable declaration is accepted; import aliases, missing
    modules, syntax errors, and nonexistent attributes remain blocked.
    """
    source = _checkout_module_source(module_name)
    if source is None:
        return False
    try:
        tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
    except (OSError, SyntaxError, UnicodeError):
        return False
    return any(
        isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        and node.name == attribute
        for node in tree.body
    )


def _callable_authority(path: object) -> bool:
    if isinstance(path, (list, tuple)):
        return bool(path) and all(_callable_authority(item) for item in path)
    if not isinstance(path, str) or "." not in path:
        return False
    module_name, attribute = path.rsplit(".", 1)
    return _source_declares_callable(module_name, attribute)


def _derived_rows() -> dict[str, dict[str, Any]]:
    inventory = canonical_feature_inventory()
    perf = _perf_authorities()
    rows: dict[str, dict[str, Any]] = {}
    for family, names in inventory.items():
        for name in names:
            blocked_by: list[str] = []
            if family == "ACQ":
                component = ACQ_SOURCE_COMPONENTS.get(name)
                producer = ACQ_PRODUCER_AUTHORITIES.get(name)
                if not component:
                    blocked_by.append("missing_acq_source_component")
                if not _callable_authority(producer):
                    blocked_by.append("missing_source_declared_acq_producer_authority")
                row = {
                    "family": family,
                    # JSON is the artifact boundary. Materialize compound ACQ
                    # authorities as arrays now so write/read validation cannot
                    # drift tuple -> list merely because the manifest round-tripped.
                    "producer_authority": list(producer) if producer else None,
                    "decision_authority": component,
                    "collector_authority": "scripts.swebench.acq_provenance.ACQ_SOURCE_COMPONENTS",
                    "evidence_relationship": (
                        f"support_source:{component}->typed_FACT:localization" if component else None
                    ),
                    "terminal_artifact": "brief_result.json",
                }
            elif family == "CAP":
                role = cap_role_for(name)
                if role == "byte_owner":
                    mechanism = CAP_BYTE_OWNER_MECHANISMS.get(name)
                    bindings = ([{
                        "producer": binding.producer,
                        "layer": binding.layer,
                        "fact_class": binding.fact_class,
                    } for binding in mechanism.bindings] if mechanism else [])
                    if mechanism is None or not bindings:
                        blocked_by.append("missing_live_byte_owner_mechanism_binding")
                    row = {
                        "family": family,
                        "producer_authority": {
                            "mechanism": mechanism.mechanism if mechanism else None,
                            "bindings": bindings,
                        },
                        "decision_authority": None,
                        "collector_authority": "scripts.swebench.gt_feature_metrics._member_delivery_byte_proven",
                        "evidence_relationship": (
                            "typed_CAP_byte_owner->registered_FACT->sealed_delivery_observation"
                            if mechanism and mechanism.mechanism == "typed_lineage"
                            else "exact_profile_member->authorized_layer->sealed_delivery_observation"
                        ),
                        "terminal_artifact": "gt_runtime_ledger_<task>.jsonl",
                    }
                else:
                    contract = control_contract(name)
                    sites = list(contract.decision_sites)
                    if contract.measurement_status != "SUPPORTED" or not sites:
                        blocked_by.append("missing_executable_control_decision_site")
                    relationship = (
                        "control_participation->typed_FACT_candidate->sealed_delivery_observation"
                        if role == "mediator"
                        else "control_participation->eligibility_decision"
                    )
                    row = {
                        "family": family,
                        "producer_authority": sites,
                        "decision_authority": sites,
                        "collector_authority": "scripts.swebench.gt_feature_metrics.control_participation",
                        "evidence_relationship": relationship,
                        "terminal_artifact": "gt_runtime_ledger_<task>.jsonl",
                    }
            elif family == "FACT":
                fact = registration(name)
                if fact is None:
                    blocked_by.append("missing_fact_registration")
                row = {
                    "family": family,
                    "producer_authority": fact.producer if fact else None,
                    "decision_authority": fact.earliest_event if fact else None,
                    "collector_authority": "scripts.swebench.gt_feature_metrics.typed_FACT_lineage",
                    "evidence_relationship": "registered_FACT->sealed_delivery_observation",
                    "terminal_artifact": "gt_runtime_ledger_<task>.jsonl",
                }
            else:
                authority = perf.get(name)
                if authority is None:
                    blocked_by.append("missing_mandatory_metric_contract")
                    section, value_type = "", ""
                else:
                    section, value_type = authority
                run_level = value_type == "run_ratio"
                if section == "behavioral_impact":
                    producer_authority = (
                        "gt_behavioral_impact.analyze_trajectory"
                    )
                elif name in {"p2p_regression_rate", "caller_breakage_count"}:
                    producer_authority = (
                        "task_truth._build_verifier_truth"
                    )
                else:
                    section_producers = {
                        "localization": "_compute_localization",
                        "edit_quality": "_compute_edit_quality",
                        "interface_preservation": "_compute_interface_preservation",
                        "scope_completeness": "_compute_scope_completeness",
                        "stuck_recovery": "_compute_stuck_recovery",
                        "verify_before_submit": "_compute_verify_before_submit",
                        "gt_attribution": "_compute_gt_attribution",
                        "token_efficiency": "_compute_token_efficiency",
                    }
                    producer_authority = (
                        "gt_run_metrics.aggregate_run_metrics"
                        if run_level else
                        f"gt_performance_metrics.{section_producers[section]}"
                    )
                if not _callable_authority(producer_authority):
                    blocked_by.append("missing_source_declared_perf_producer_authority")
                row = {
                    "family": family,
                    "producer_authority": producer_authority,
                    "decision_authority": f"_MANDATORY_METRICS:{section}.{name}:{value_type}",
                    "collector_authority": (
                        "gt_run_metrics.aggregate_run_metrics" if run_level
                        else "gt_feature_metrics._performance_feature_records"
                    ),
                    "evidence_relationship": "mandatory_metric_contract->typed_metric_artifact",
                    "terminal_artifact": (
                        "gt_run_metrics_v2_<run>.json" if run_level
                        else "gt_deep_metrics_<task>.json"
                    ),
                }
            row["blocked_by"] = blocked_by
            rows[name] = row
    return rows


def build_static_dispatch_manifest() -> dict[str, Any]:
    inventory = canonical_feature_inventory()
    rows = _derived_rows()
    return {
        "schema": SCHEMA,
        "family_counts": {family: len(names) for family, names in inventory.items()},
        "feature_count": len(rows),
        "dynamic_opportunity_proven": False,
        "ss_live_proven": False,
        "meaning": (
            "static checkout source-declared authority completeness only; "
            "never runtime-executability, dynamic, or SS proof"
        ),
        "features": rows,
    }


def validate_static_dispatch_manifest(manifest: object) -> dict[str, Any]:
    inventory = canonical_feature_inventory()
    expected_names = {name: family for family, names in inventory.items() for name in names}
    if not isinstance(manifest, dict) or manifest.get("schema") != SCHEMA:
        raise ValueError("static dispatch preflight: invalid manifest schema")
    features = manifest.get("features")
    if not isinstance(features, dict) or len(features) != 129 or set(features) != set(expected_names):
        raise ValueError("static dispatch preflight: manifest must contain exact 129 inventory")
    counts = {family: len(names) for family, names in inventory.items()}
    if manifest.get("family_counts") != counts:
        raise ValueError("static dispatch preflight: family-count drift")
    if manifest.get("dynamic_opportunity_proven") is not False or manifest.get("ss_live_proven") is not False:
        raise ValueError("static dispatch preflight: static manifest cannot claim dynamic or SS-LIVE proof")

    expected_rows = _derived_rows()
    errors: list[str] = []
    blocked: list[str] = []
    for name, expected in expected_rows.items():
        actual = features[name]
        if not isinstance(actual, dict):
            errors.append(f"{name}:malformed_row")
            continue
        for field in ("family", "decision_authority", *AUTHORITY_FIELDS):
            if actual.get(field) != expected.get(field):
                errors.append(f"{name}:authority_drift:{field}")
        for field in AUTHORITY_FIELDS:
            value = actual.get(field)
            if value is None or value == "" or value == []:
                errors.append(f"{name}:missing_{field}")
        expected_blockers = expected["blocked_by"]
        if actual.get("blocked_by") != expected_blockers:
            errors.append(f"{name}:authority_drift:blocked_by")
        if expected_blockers:
            blocked.append(name)
            errors.extend(f"{name}:{reason}" for reason in expected_blockers)
    return {
        "schema": "gt.static_dispatch_feature_preflight.v1",
        "valid": not errors,
        "errors": sorted(errors),
        "blocked_features": sorted(blocked),
        "feature_count": len(features),
        "family_counts": counts,
        "dispatch_authorized": not errors,
        "dynamic_opportunity_proven": False,
        "ss_live_proven": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    try:
        manifest = build_static_dispatch_manifest()
        result = validate_static_dispatch_manifest(manifest)
        payload = {"manifest": manifest, "preflight": result}
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(json.dumps(payload, sort_keys=True))
        return 0 if result["valid"] else 1
    except Exception as exc:  # noqa: BLE001 - CLI must always leave a diagnostic artifact
        payload = {
            "manifest": None,
            "preflight": {
                "schema": "gt.static_dispatch_feature_preflight.v1",
                "valid": False,
                "dispatch_authorized": False,
                "error": f"{type(exc).__name__}: {exc}",
            },
        }
        if args.output:
            try:
                args.output.parent.mkdir(parents=True, exist_ok=True)
                args.output.write_text(
                    json.dumps(payload, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8",
                )
            except OSError:
                pass
        print(json.dumps(payload, sort_keys=True))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["build_static_dispatch_manifest", "validate_static_dispatch_manifest"]
