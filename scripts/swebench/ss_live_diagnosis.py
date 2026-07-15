#!/usr/bin/env python3
"""Render an exact-128, diagnosis-only view of downloaded SS artifacts.

This reader never promotes a feature from flags, file existence, layer names, or
payload text. Delivery-family attribution requires typed feature lineage. A
missing lineage record is an explicit UNMEASURED state, never DARK.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections import Counter
from pathlib import Path
from typing import Any

from gt_feature_inventory import (
    canonical_feature_inventory,
    performance_metric_definitions,
)
from feature_opportunity import collect_feature_opportunities
from consumption_ledger import _typed_lineage_from_row
from groundtruth.runtime.feature_lineage import FeatureRef, cap_role_for


DELIVERY_BUCKETS = (
    "NOT_ELIGIBLE",
    "DARK_ELIGIBLE_NO_PRODUCER",
    "PRODUCED_NOT_DELIVERED",
    "SEALED_DELIVERED_UNGRADED",
    "WRONG_INFO",
    "LATE",
    "STEP_BEHIND",
    "NOVEL_IGNORED",
    "ACKNOWLEDGED",
    "CAUSAL_P5",
)
PERF_BUCKETS = ("MEASURED", "NOT_APPLICABLE", "UNMEASURED", "FAILED")
TYPED_TERMINAL_GATES = {
    "support": (
        "supported_fact_delivery_join", "candidate_local_contribution",
        "source_contribution_correct", "timing_inherited_from_fact_delivery",
        "source_causal_fair_probe",
    ),
    "infra_control": (
        "runtime_member_control_receipt", "mediated_fact_ids",
        "mediation_correct", "mediation_causal_fair_probe",
    ),
}


def _load_json(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def _load_expected_task_ids(path: Path) -> tuple[str, ...]:
    """Load the independently frozen task population in manifest order."""
    payload = _load_json(path)
    raw = payload.get("task_ids")
    if (
        not isinstance(raw, list)
        or not raw
        or any(not isinstance(task, str) or not task for task in raw)
        or len(raw) != len(set(raw))
    ):
        raise ValueError(f"expected-task manifest is malformed: {path}")
    return tuple(raw)


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for number, raw in enumerate(handle, 1):
            line = raw.strip()
            if not line:
                continue
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ValueError(f"expected JSON object at {path}:{number}")
            rows.append(value)
    return rows


def _value(metric: object) -> object:
    return metric.get("value") if isinstance(metric, dict) else None


def _lineage_from_row(row: dict[str, Any]) -> dict[str, Any] | None:
    """Return only registry-rebuilt lineage from the actual flat ledger schema."""
    return _typed_lineage_from_row(row)


def _lineage_has_feature(
    lineage: object, feature: str, family: str,
) -> bool:
    if not isinstance(lineage, dict):
        return False
    if lineage.get("schema") != "gt.feature_lineage.v1":
        return False
    if family not in {"CAP", "FACT"}:
        return False
    refs = lineage.get("features")
    if not isinstance(refs, list):
        return False
    for ref in refs:
        if not isinstance(ref, dict):
            continue
        category = ref.get("category")
        feature_id = ref.get("feature_id")
        role = ref.get("role")
        if not isinstance(category, str):
            continue
        if not isinstance(feature_id, str):
            continue
        if not isinstance(role, str):
            continue
        try:
            typed = FeatureRef(category, feature_id, role)
        except ValueError:
            continue
        if typed.category != family or typed.feature_id != feature:
            continue
        if family == "CAP" and typed.role != "byte_owner":
            continue
        if family == "FACT" and typed.role != "fact":
            continue
        return True
    return False


def _causal_proven(lineage: object) -> bool:
    return bool(
        isinstance(lineage, dict)
        and lineage.get("causal_contribution_proven") is True
    )


def classify_delivery_feature(
    feature: str,
    family: str,
    lifecycle: dict[str, Any],
    readiness: dict[str, Any],
    ledger_rows: list[dict[str, Any]],
    joined_entries: list[dict[str, Any]],
) -> str:
    """Classify one FACT or byte-owning CAP row without inferred attribution."""
    if _value(lifecycle.get("eligible")) is False:
        return "NOT_ELIGIBLE"

    rows = [
        row for row in ledger_rows
        if _lineage_has_feature(_lineage_from_row(row), feature, family)
    ]
    entries = [
        entry for entry in joined_entries
        if _lineage_has_feature(entry.get("feature_lineage"), feature, family)
        and entry.get("source") == "trajectory"
        and entry.get("joined") is True
        and entry.get("join_method") == "seal"
    ]

    reasons = {str(row.get("reason") or "") for row in rows}
    if "ss_step_behind" in reasons:
        return "STEP_BEHIND"
    if "ss_late" in reasons:
        return "LATE"

    if not rows and not entries:
        if readiness.get("lineage_complete") is True:
            produced = _value(lifecycle.get("produced"))
            if produced is False:
                return "DARK_ELIGIBLE_NO_PRODUCER"
            if produced is True:
                return "PRODUCED_NOT_DELIVERED"
        return "UNMEASURED:missing_lineage"

    delivered_rows = [
        row for row in rows
        if row.get("outcome") == "delivered"
        and isinstance(row.get("content_sha256_16"), str)
        and int(row.get("chars_delivered") or 0) > 0
    ]
    if not delivered_rows:
        return "PRODUCED_NOT_DELIVERED"
    if not entries:
        return "SEALED_DELIVERED_UNGRADED"

    gates = readiness.get("gates")
    gates = gates if isinstance(gates, dict) else {}
    if (
        gates.get("correct_info") is False
        or _value(lifecycle.get("truth_valid")) is False
        or _value(lifecycle.get("authority_valid")) is False
        or _value(lifecycle.get("stale")) is True
    ):
        return "WRONG_INFO"
    if (
        gates.get("correct_rl_adhered_time") is False
        or _value(lifecycle.get("expired_late")) is True
    ):
        return "LATE"
    if gates.get("correct_info") is not True or gates.get(
        "correct_rl_adhered_time"
    ) is not True:
        return "SEALED_DELIVERED_UNGRADED"

    receipts = [
        int(entry.get("receipt") or 0)
        for entry in entries
        if isinstance(entry.get("receipt"), int)
        and not isinstance(entry.get("receipt"), bool)
    ]
    receipt = max(receipts, default=0)
    causal = any(_causal_proven(entry.get("feature_lineage")) for entry in entries)
    if (
        causal
        and receipt >= 2
        and all(gates.get(name) is True for name in (
            "delivered_byte_proven", "correct_info",
            "correct_rl_adhered_time", "acknowledged", "leak_zero",
            "dose_lte_one", "fair_probe",
        ))
    ):
        return "CAUSAL_P5"
    if receipt >= 2:
        return "ACKNOWLEDGED"
    if receipt == 1:
        return "NOVEL_IGNORED"
    return "SEALED_DELIVERED_UNGRADED"


def classify_typed_terminal(record: object, expected_role: str) -> str:
    """Classify support/control terminals without borrowing delivery gates."""
    if expected_role not in TYPED_TERMINAL_GATES or not isinstance(record, dict):
        return f"FAILED:{expected_role}:record_contract"
    status = record.get("status")
    if status == "UNMEASURED":
        blocker = record.get("blocker")
        detail = blocker if isinstance(blocker, str) and blocker else "record_status"
        return f"UNMEASURED:{expected_role}:{detail}"
    if status != "MEASURED":
        return f"FAILED:{expected_role}:record_status"
    if expected_role == "support" and record.get("source") != "acq_provenance":
        return "FAILED:support:source_contract"

    readiness = record.get("ss_readiness")
    if not isinstance(readiness, dict) or readiness.get("role") != expected_role:
        return f"FAILED:{expected_role}:readiness_role"
    gates = readiness.get("gates")
    expected_gates = TYPED_TERMINAL_GATES[expected_role]
    if not isinstance(gates, dict) or tuple(gates) != expected_gates:
        return f"FAILED:{expected_role}:gate_schema"
    if any(value is not True and value is not False and value is not None
           for value in gates.values()):
        return f"FAILED:{expected_role}:gate_value"
    for gate in expected_gates:
        if gates[gate] is False:
            return f"FAILED:{expected_role}:{gate}"
    for gate in expected_gates:
        if gates[gate] is None:
            return f"UNMEASURED:{expected_role}:{gate}"

    live_witness = readiness.get("live_witness")
    ss_live = readiness.get("ss_live")
    if not isinstance(live_witness, bool) or not isinstance(ss_live, bool):
        return f"FAILED:{expected_role}:terminal_schema"
    if live_witness and ss_live:
        return f"{expected_role.upper()}_SS_LIVE"
    if live_witness or ss_live:
        return f"FAILED:{expected_role}:terminal_inconsistent"
    return f"UNMEASURED:{expected_role}:live_witness"


def _opportunity_record(record: object) -> dict[str, Any]:
    """Return only the additive, non-promoting opportunity projection."""
    if not isinstance(record, dict):
        return {
            "status": "UNMEASURED", "reason": "no_bound_opportunity",
            "eligible_opportunity": None, "decision_boundary_evidence": [],
        }
    value = record.get("opportunity_evidence")
    if value is None:
        return {
            "status": "UNMEASURED", "reason": "no_bound_opportunity",
            "eligible_opportunity": None, "decision_boundary_evidence": [],
        }
    if not isinstance(value, dict) or value.get("status") not in {"BOUND", "UNMEASURED"}:
        return {
            "status": "UNMEASURED", "reason": "opportunity_record_malformed",
            "eligible_opportunity": None, "decision_boundary_evidence": [],
        }
    return value


def classify_cap_control(feature: str, record: object) -> str:
    """Classify non-byte-owning CAP roles by their own terminal contract."""
    role = cap_role_for(feature)
    if role == "mediator":
        return classify_typed_terminal(record, "infra_control")
    if role == "eligibility":
        opportunity = _opportunity_record(record)
        if opportunity.get("status") != "BOUND":
            if opportunity.get("reason") == "no_bound_opportunity":
                return "UNMEASURED:eligibility_control:terminal_contract_unavailable"
            return (
                "UNMEASURED:eligibility_control:"
                + str(opportunity.get("reason") or "opportunity_unavailable")
            )
        # A validated parent-policy anchor proves the candidate opportunity,
        # not whether the model had already committed to the action.  The
        # chronological gt-math audit owns that terminal timing verdict.
        return "UNMEASURED:eligibility_control:chronological_decision_audit_pending"
    return "FAILED:capability_support:unexpected_control_dispatch"


def _task_lifecycle(
    metrics: dict[str, Any], feature: str, family: str,
) -> dict[str, Any]:
    if family == "CAP":
        record = (metrics.get("features") or {}).get(feature)
        lifecycle = record.get("lifecycle") if isinstance(record, dict) else None
        return lifecycle if isinstance(lifecycle, dict) else {}
    if family == "FACT":
        record = (metrics.get("fact_classes") or {}).get(feature)
        return record if isinstance(record, dict) else {}
    record = (metrics.get("ss_features") or {}).get(feature)
    return record if isinstance(record, dict) else {}


def _perf_status(value: object) -> str:
    if not isinstance(value, dict):
        return "UNMEASURED"
    status = value.get("status")
    if status in PERF_BUCKETS[:3]:
        return str(status)
    return "FAILED"


def _requires_visible_audit(feature: str, family: str) -> bool:
    """Whether this row's terminal contract depends on model-visible bytes.

    ACQ support is joined through a delivered FACT, while FACT and byte-owning
    CAP rows directly own delivered bytes.  CAP eligibility and mediation rows
    have separate typed control contracts and must not inherit a delivery gate
    they do not own.
    """
    return family in {"ACQ", "FACT"} or (
        family == "CAP" and cap_role_for(feature) == "byte_owner"
    )


def _run_perf_rows(run_metrics: dict[str, Any]) -> dict[str, str]:
    if run_metrics.get("schema") != "gt_run_metrics.v2":
        raise ValueError("run metrics must use canonical gt_run_metrics.v2")
    if run_metrics.get("mandatory_performance_metric_count") != 58:
        raise ValueError("run metrics must declare exactly 58 mandatory PERF rows")
    mandatory = run_metrics.get("mandatory_performance")
    mandatory = mandatory if isinstance(mandatory, dict) else {}
    result: dict[str, str] = {}
    for section, definitions in performance_metric_definitions().items():
        section_rows = mandatory.get(section)
        section_rows = section_rows if isinstance(section_rows, dict) else {}
        for feature, _value_type in definitions:
            result[feature] = _perf_status(section_rows.get(feature))
    return result


def _validate_task_metrics(
    record: dict[str, Any], inventory: dict[str, tuple[str, ...]], path: Path,
) -> None:
    expected = {name: family for family, names in inventory.items() for name in names}
    rows = record.get("ss_features")
    if not isinstance(rows, dict) or set(rows) != set(expected):
        raise ValueError(f"task feature inventory is not exact-128: {path}")
    if any(not isinstance(rows[name], dict)
           or rows[name].get("family") != family
           for name, family in expected.items()):
        raise ValueError(f"task feature family mapping is malformed: {path}")
    integrity = record.get("ss_integrity")
    if (
        not isinstance(integrity, dict)
        or integrity.get("inventory_complete") is not True
        or not isinstance(integrity.get("required_inputs_complete"), bool)
    ):
        raise ValueError(f"task feature integrity contract is malformed: {path}")


def _validate_run_metrics_population(
    run_metrics: dict[str, Any], task_count: int,
) -> bool:
    """Validate population arithmetic and report whether collection is complete.

    A structurally valid incomplete population remains diagnosable.  The caller
    marks it non-publishable instead of losing the exact expected-task matrix.
    """
    population = run_metrics.get("task_population")
    observed_records = population.get("observed_record_count") if isinstance(
        population, dict
    ) else None
    observed_unique = population.get("observed_unique_count") if isinstance(
        population, dict
    ) else None
    expected_count = population.get("expected_count", task_count) if isinstance(
        population, dict
    ) else None
    missing = population.get("missing_tasks") if isinstance(population, dict) else None
    duplicate = population.get("duplicate_tasks") if isinstance(population, dict) else None
    unexpected = population.get("unexpected_tasks") if isinstance(population, dict) else None
    invalid = population.get("invalid_task_records") if isinstance(population, dict) else None
    if (
        not isinstance(run_metrics.get("mandatory_performance_collection_complete"), bool)
        or not isinstance(population, dict)
        or expected_count != task_count
        or not isinstance(run_metrics.get("tasks"), int)
        or isinstance(run_metrics.get("tasks"), bool)
        or not isinstance(observed_records, int)
        or isinstance(observed_records, bool)
        or not isinstance(observed_unique, int)
        or isinstance(observed_unique, bool)
        or run_metrics.get("tasks") != observed_records
        or observed_unique < 0
        or observed_unique > observed_records
        or any(not isinstance(value, list) for value in (
            missing, duplicate, unexpected, invalid,
        ))
        or any(not isinstance(task, str) or not task for rows in (
            missing, duplicate, unexpected,
        ) for task in rows)
        or observed_unique + len(missing) - len(unexpected) != task_count
    ):
        raise ValueError("run metrics population/integrity does not match diagnosis tasks")
    return bool(
        run_metrics["mandatory_performance_collection_complete"]
        and observed_records == task_count
        and observed_unique == task_count
        and missing == []
        and duplicate == []
        and unexpected == []
        and invalid == []
    )


def _validate_deep_metric_tasks(
    root: Path, expected_tasks: tuple[str, ...],
) -> dict[str, list[str]]:
    """Validate deep-metric identities without hiding the diagnosis artifact."""
    observed: list[str] = []
    for path in sorted(root.rglob("gt_deep_metrics_*.json")):
        record = _load_json(path)
        task = record.get("task_id")
        if record.get("schema") != "gt_deep_metrics.v2" or not isinstance(task, str) or not task:
            raise ValueError(f"deep metrics identity/schema is malformed: {path}")
        observed.append(task)
    counts = Counter(observed)
    return {
        "missing": [task for task in expected_tasks if task not in counts],
        "duplicate": sorted(task for task, count in counts.items() if count != 1),
        "unexpected": sorted(set(counts) - set(expected_tasks)),
    }


def _find_one(directory: Path, pattern: str) -> Path | None:
    matches = sorted(directory.glob(pattern))
    return matches[0] if len(matches) == 1 else None


_PROFILE_BINDING_ARTIFACTS = (
    "gt_run_identity.json",
    "gt_profile_activation.json",
    "gt_profile_receipt.json",
)
_COMMIT_RE = re.compile(r"^[0-9a-f]{40,64}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_PINNED_IMAGE_RE = re.compile(r"^.+@sha256:[0-9a-f]{64}$")


def _completion_artifact_names(task: str) -> tuple[str, ...]:
    """Artifacts atomically bound by the task-completion receipt."""
    return (
        "mini-swe-agent.trajectory.json",
        "brief_result.json",
        "task_truth.json",
        "gt_artifacts/gt_run_identity.json",
        "gt_artifacts/gt_agent_exit.json",
        "gt_artifacts/gt_profile_activation.json",
        "gt_artifacts/gt_profile_receipt.json",
        "gt_artifacts/gt_batch_activation.json",
        f"gt_runtime_ledger_{task}.jsonl",
        f"gt_runtime_ledger_attestation_{task}.json",
        f"gt_deep_metrics_{task}.json",
        f"gt_feature_metrics_{task}.json",
        f"gt_performance_metrics_{task}.json",
        f"gt_behavioral_impact_{task}.json",
    )


def _task_completion_issues(
    task_dir: Path, task: str, workflow_run_id: str,
) -> list[str]:
    """Validate one atomic completion receipt and every artifact it seals."""
    path = task_dir / "gt_task_completion.json"
    if not path.is_file():
        return ["task_completion:missing"]
    try:
        receipt = _load_json(path)
    except (OSError, ValueError, json.JSONDecodeError):
        return ["task_completion:malformed"]

    issues: list[str] = []
    if receipt.get("schema") != "gt.task_completion.v1":
        issues.append("task_completion:schema")
    if receipt.get("task") != task:
        issues.append("task_completion:task")
    if receipt.get("workflow_run_id") != workflow_run_id:
        issues.append("task_completion:workflow_run_id")
    if receipt.get("status") != "COMPLETE":
        issues.append("task_completion:status")
    hashes = receipt.get("artifact_sha256")
    expected = _completion_artifact_names(task)
    if not isinstance(hashes, dict) or set(hashes) != set(expected):
        issues.append("task_completion:artifact_inventory")
        hashes = hashes if isinstance(hashes, dict) else {}
    for relative in expected:
        artifact = task_dir / relative
        if not artifact.is_file():
            issues.append(f"task_completion:artifact_missing:{relative}")
            continue
        expected_hash = hashes.get(relative)
        if not isinstance(expected_hash, str) or _SHA256_RE.fullmatch(expected_hash) is None:
            issues.append(f"task_completion:artifact_digest:{relative}")
            continue
        actual_hash = hashlib.sha256(artifact.read_bytes()).hexdigest()
        if actual_hash != expected_hash:
            issues.append(f"task_completion:artifact_hash:{relative}")
    return sorted(set(issues))


def _valid_requested_ref(value: object) -> bool:
    """Accept a bounded Git branch/tag/SHA spelling, never an empty/control value."""
    return bool(
        isinstance(value, str)
        and 0 < len(value) <= 1024
        and value == value.strip()
        and not any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in value)
    )


def _diagnosis_integrity(
    tasks: dict[str, tuple[Path, dict[str, Any]]],
    run_metrics: dict[str, Any],
    expected_members: tuple[str, ...],
) -> dict[str, Any]:
    """Bind each diagnosis task to immutable run and in-agent Profile-2 proof.

    These receipts prove run/profile identity only. They never prove delivery,
    timing, acknowledgment, causal contribution, or the terminal SS-LIVE bit.
    """
    expected = set(expected_members)
    expected_run_id = str(run_metrics.get("run_id") or "")
    task_records: dict[str, dict[str, Any]] = {}
    consensus_rows: list[tuple[str, str, str, str, str]] = []

    for task, (metrics_path, _metrics) in sorted(tasks.items()):
        artifact_dir = metrics_path.parent / "gt_artifacts"
        payloads: dict[str, dict[str, Any]] = {}
        issues: list[str] = []
        artifacts: dict[str, dict[str, Any]] = {}
        for filename in _PROFILE_BINDING_ARTIFACTS:
            path = artifact_dir / filename
            if not path.is_file():
                issues.append(f"missing:{filename}")
                artifacts[filename] = {"path": f"gt_artifacts/{filename}", "loaded": False}
                continue
            try:
                payloads[filename] = _load_json(path)
            except (OSError, ValueError, json.JSONDecodeError):
                issues.append(f"malformed:{filename}")
                artifacts[filename] = {"path": f"gt_artifacts/{filename}", "loaded": False}
            else:
                artifacts[filename] = {"path": f"gt_artifacts/{filename}", "loaded": True}

        identity = payloads.get("gt_run_identity.json")
        if identity is not None:
            if identity.get("schema") != "gt.run_identity.v1":
                issues.append("run_identity:schema")
            resolved = identity.get("gt_ref_resolved")
            if not isinstance(resolved, str) or _COMMIT_RE.fullmatch(resolved) is None:
                issues.append("run_identity:gt_ref_resolved")
            for field in ("seam_sha256", "runner_sha256"):
                value = identity.get(field)
                if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
                    issues.append(f"run_identity:{field}")
            expected_digest = identity.get("substrate_digest_expected")
            actual_digest = identity.get("substrate_digest_actual")
            if (
                not isinstance(expected_digest, str)
                or _PINNED_IMAGE_RE.fullmatch(expected_digest) is None
                or actual_digest != expected_digest
            ):
                issues.append("run_identity:substrate_digest")
            workflow_run_id = str(identity.get("workflow_run_id") or "")
            if not expected_run_id or workflow_run_id != expected_run_id:
                issues.append("run_identity:workflow_run_id")
            requested = identity.get("gt_ref_requested")
            prepared = identity.get("gt_ref_prepared_sha")
            if (
                not _valid_requested_ref(requested)
                or not isinstance(prepared, str)
                or _COMMIT_RE.fullmatch(prepared) is None
                or prepared != resolved
            ):
                issues.append("run_identity:gt_ref_binding")
            if identity.get("baseline") is not False:
                issues.append("run_identity:baseline")
            if not any(issue.startswith("run_identity:") for issue in issues):
                consensus_rows.append((
                    str(requested), str(prepared), str(resolved),
                    str(actual_digest), workflow_run_id,
                ))

        activation = payloads.get("gt_profile_activation.json")
        activation_members: set[str] = set()
        if activation is not None:
            if activation.get("schema") != "gt.profile_activation.v1":
                issues.append("profile_activation:schema")
            if str(activation.get("profile") or "") != "2":
                issues.append("profile_activation:profile_mismatch")
            raw_members = activation.get("members")
            if (
                not isinstance(raw_members, list)
                or any(not isinstance(member, str) for member in raw_members)
                or len(raw_members) != len(set(raw_members))
            ):
                issues.append("profile_activation:members_malformed")
            else:
                activation_members = set(raw_members)
                if activation_members != expected:
                    issues.append("profile_activation:members_mismatch")

        receipt = payloads.get("gt_profile_receipt.json")
        receipt_members: set[str] = set()
        patched_classes: list[str] = []
        if receipt is not None:
            if receipt.get("schema") != "gt.profile_receipt.v1":
                issues.append("profile_receipt:schema")
            if str(receipt.get("gt_rl_profile") or "") != "2":
                issues.append("profile_receipt:profile_mismatch")
            raw_members = receipt.get("members_on")
            if (
                not isinstance(raw_members, list)
                or any(not isinstance(member, str) for member in raw_members)
                or len(raw_members) != len(set(raw_members))
            ):
                issues.append("profile_receipt:members_malformed")
            else:
                receipt_members = set(raw_members)
                if not expected.issubset(receipt_members):
                    issues.append("profile_receipt:members_mismatch")
            source = receipt.get("member_source")
            if (
                not isinstance(source, dict)
                or not expected.issubset(source)
                or any(source.get(member) not in {"inherited", "resolved"} for member in expected)
            ):
                issues.append("profile_receipt:member_source")
            raw_patched = receipt.get("patched_classes")
            if (
                not isinstance(raw_patched, list)
                or not raw_patched
                or any(not isinstance(name, str) or not name for name in raw_patched)
            ):
                issues.append("profile_receipt:patched_classes")
            else:
                patched_classes = raw_patched

        if activation is not None and receipt is not None and (
            activation_members != expected or not activation_members.issubset(receipt_members)
        ):
            issues.append("profile_binding:member_disagreement")

        issues.extend(_task_completion_issues(
            metrics_path.parent, task, expected_run_id,
        ))

        issues = sorted(set(issues))
        task_records[task] = {
            "status": "BOUND" if not issues else "UNMEASURED",
            "issues": issues,
            "artifacts": artifacts,
            "identity": {
                "workflow_run_id": identity.get("workflow_run_id") if identity else None,
                "gt_ref_requested": identity.get("gt_ref_requested") if identity else None,
                "gt_ref_prepared_sha": identity.get("gt_ref_prepared_sha") if identity else None,
                "gt_ref_resolved": identity.get("gt_ref_resolved") if identity else None,
                "substrate_digest": identity.get("substrate_digest_actual") if identity else None,
            },
            "profile": {
                "requested": "2",
                "activation_member_count": len(activation_members),
                "receipt_member_count": len(receipt_members),
                "patched_classes": patched_classes,
            },
        }

    consensus = sorted(set(consensus_rows))
    run_issues: list[str] = []
    if not tasks:
        run_issues.append("no_tasks")
    if len(consensus) != 1 or len(consensus_rows) != len(tasks):
        run_issues.append("run_identity_consensus")
    incomplete = [task for task, record in task_records.items() if record["status"] != "BOUND"]
    complete = bool(tasks) and not incomplete and not run_issues
    return {
        "schema": "gt.ss_live_diagnosis.integrity.v1",
        "publishable": complete,
        "identity_profile_binding_complete": complete,
        "required_artifacts": [
            *list(_PROFILE_BINDING_ARTIFACTS), "gt_task_completion.json",
        ],
        "expected_profile": "2",
        "expected_member_count": len(expected),
        "run_issues": run_issues,
        "incomplete_tasks": incomplete,
        "identity_consensus": (
            {
                "gt_ref_requested": consensus[0][0],
                "gt_ref_prepared_sha": consensus[0][1],
                "gt_ref_resolved": consensus[0][2],
                "substrate_digest": consensus[0][3],
                "workflow_run_id": consensus[0][4],
            }
            if len(consensus) == 1 else None
        ),
        "tasks": task_records,
        "proof_scope": "identity_and_profile_only_no_ss_live_promotion",
    }


def _task_artifacts(
    metrics_path: Path, metrics: dict[str, Any]
) -> tuple[
    list[dict[str, Any]], list[dict[str, Any]], dict[str, Any], str | None,
]:
    directory = metrics_path.parent
    trajectory_path = _find_one(directory, "mini-swe-agent.trajectory.json")
    ledger_path = _find_one(directory, "gt_runtime_ledger*.jsonl")
    if trajectory_path is None or ledger_path is None:
        return [], [], {}, "missing_artifacts"
    try:
        trajectory = _load_json(trajectory_path)
        rows = _load_jsonl(ledger_path)
        from consumption_ledger import build_consumption_ledger
        ledger = build_consumption_ledger(
            trajectory, runtime_ledger_path=str(ledger_path)
        )
        entries = ledger.get("entries")
        if not isinstance(entries, list):
            return rows, [], {}, "malformed_consumption_ledger"
        messages = trajectory.get("messages")
        if not isinstance(messages, list):
            return rows, [], {}, "malformed_trajectory_messages"
        opportunity = collect_feature_opportunities(
            rows,
            messages,
            canonical_feature_inventory(),
        )
        if opportunity["integrity"].get("publishable") is not True:
            return rows, [entry for entry in entries if isinstance(entry, dict)], opportunity, (
                "malformed_feature_opportunity"
            )
        return (
            rows,
            [entry for entry in entries if isinstance(entry, dict)],
            opportunity,
            None,
        )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return [], [], {}, f"artifact_error:{type(exc).__name__}"


def _aggregate_bucket(task_buckets: dict[str, str]) -> str:
    if not task_buckets:
        return "UNMEASURED:no_tasks"
    counts = Counter(task_buckets.values())
    if len(counts) == 1:
        return next(iter(counts))
    order = (
        "WRONG_INFO", "LATE", "STEP_BEHIND",
        "PRODUCED_NOT_DELIVERED", "DARK_ELIGIBLE_NO_PRODUCER",
        "SEALED_DELIVERED_UNGRADED", "NOVEL_IGNORED", "ACKNOWLEDGED",
        "CAUSAL_P5", "NOT_ELIGIBLE",
    )
    failed = sorted(bucket for bucket in counts if bucket.startswith("FAILED:"))
    if failed:
        return failed[0]
    unmeasured = sorted(bucket for bucket in counts if bucket.startswith("UNMEASURED:"))
    if unmeasured:
        return unmeasured[0]
    return next(bucket for bucket in order if bucket in counts)


def diagnose_run(
    run_dir: Path | str,
    run_metrics_path: Path | str,
    *,
    expected_tasks_path: Path | str | None = None,
) -> dict[str, Any]:
    """Build one exact canonical row per feature plus explicit per-task buckets."""
    root = Path(run_dir)
    inventory = canonical_feature_inventory()
    feature_paths = sorted(root.rglob("gt_feature_metrics_*.json"))
    discovered_tasks: dict[str, tuple[Path, dict[str, Any]]] = {}
    duplicate_feature_tasks: set[str] = set()
    for path in feature_paths:
        record = _load_json(path)
        if record.get("schema") != "gt.feature_metrics.v1":
            raise ValueError(f"non-canonical feature metrics: {path}")
        task = record.get("task")
        if not isinstance(task, str) or not task:
            raise ValueError(f"feature metrics missing task identity: {path}")
        _validate_task_metrics(record, inventory, path)
        if task in discovered_tasks:
            duplicate_feature_tasks.add(task)
            continue
        discovered_tasks[task] = (path, record)

    if expected_tasks_path is None:
        task_order = tuple(sorted(discovered_tasks))
    else:
        task_order = _load_expected_task_ids(Path(expected_tasks_path))
    expected_set = set(task_order)
    unexpected_feature_tasks = tuple(sorted(set(discovered_tasks) - expected_set))
    duplicate_feature_metric_tasks = tuple(sorted(duplicate_feature_tasks))
    duplicate_expected_tasks = tuple(
        task for task in task_order if task in duplicate_feature_tasks
    )
    missing_tasks = tuple(
        task for task in task_order if task not in discovered_tasks
    )
    unavailable_tasks = {
        **{task: "missing_feature_metrics" for task in missing_tasks},
        **{task: "duplicate_feature_metrics" for task in duplicate_expected_tasks},
    }
    tasks = {
        task: value for task, value in discovered_tasks.items()
        if task in expected_set and task not in duplicate_feature_tasks
    }

    deep_metric_population = _validate_deep_metric_tasks(root, task_order)
    missing_deep_metric_tasks = tuple(deep_metric_population["missing"])
    run_metrics = _load_json(Path(run_metrics_path))
    run_metrics_population_complete = _validate_run_metrics_population(
        run_metrics, len(task_order)
    )
    run_perf = _run_perf_rows(run_metrics)
    diagnosis_integrity = _diagnosis_integrity(
        tasks, run_metrics, inventory["CAP"]
    )
    incomplete_input_tasks = sorted(
        task for task, (_path, metrics) in tasks.items()
        if isinstance(metrics.get("ss_integrity"), dict)
        and metrics["ss_integrity"].get("required_inputs_complete") is False
    )
    if incomplete_input_tasks:
        diagnosis_integrity["run_issues"] = sorted(set([
            *diagnosis_integrity["run_issues"], "task_feature_inputs_incomplete",
        ]))
        diagnosis_integrity["publishable"] = False
    diagnosis_integrity["task_feature_inputs_incomplete"] = incomplete_input_tasks
    diagnosis_integrity["feature_record_population"] = {
        "expected": list(task_order),
        "missing": list(missing_tasks),
        "duplicate": list(duplicate_feature_metric_tasks),
        "unexpected": list(unexpected_feature_tasks),
    }
    diagnosis_integrity["deep_metric_population"] = deep_metric_population
    if not run_metrics_population_complete:
        diagnosis_integrity["run_issues"] = sorted(set([
            *diagnosis_integrity["run_issues"], "run_metrics_population_incomplete",
        ]))
        diagnosis_integrity["publishable"] = False
        diagnosis_integrity["identity_profile_binding_complete"] = False
    if missing_deep_metric_tasks:
        diagnosis_integrity["run_issues"] = sorted(set([
            *diagnosis_integrity["run_issues"], "missing_deep_metrics",
        ]))
        diagnosis_integrity["publishable"] = False
        diagnosis_integrity["identity_profile_binding_complete"] = False
    for issue, members in (
        ("duplicate_feature_metrics", duplicate_feature_metric_tasks),
        ("unexpected_feature_metrics", unexpected_feature_tasks),
        ("duplicate_deep_metrics", tuple(deep_metric_population["duplicate"])),
        ("unexpected_deep_metrics", tuple(deep_metric_population["unexpected"])),
    ):
        if members:
            diagnosis_integrity["run_issues"] = sorted(set([
                *diagnosis_integrity["run_issues"], issue,
            ]))
            diagnosis_integrity["publishable"] = False
            diagnosis_integrity["identity_profile_binding_complete"] = False
    if missing_tasks:
        for task in missing_tasks:
            diagnosis_integrity["tasks"][task] = {
                "status": "UNMEASURED",
                "issues": ["feature_metrics:missing"],
                "artifacts": {},
                "identity": {},
                "profile": {},
            }
        diagnosis_integrity["run_issues"] = sorted(set(
            [*diagnosis_integrity["run_issues"], "missing_feature_metrics"]
        ))
        diagnosis_integrity["incomplete_tasks"] = sorted(set(
            [*diagnosis_integrity["incomplete_tasks"], *missing_tasks]
        ))
        diagnosis_integrity["publishable"] = False
        diagnosis_integrity["identity_profile_binding_complete"] = False
    for task in duplicate_expected_tasks:
        diagnosis_integrity["tasks"][task] = {
            "status": "UNMEASURED",
            "issues": ["feature_metrics:duplicate"],
            "artifacts": {},
            "identity": {},
            "profile": {},
        }
    if duplicate_expected_tasks:
        diagnosis_integrity["incomplete_tasks"] = sorted(set([
            *diagnosis_integrity["incomplete_tasks"], *duplicate_expected_tasks,
        ]))
    contexts = {
        task: _task_artifacts(path, metrics)
        for task, (path, metrics) in tasks.items()
    }
    rows_out: list[dict[str, Any]] = []
    for family, features in inventory.items():
        for feature in features:
            task_buckets: dict[str, str] = {}
            if family == "PERF":
                for task in task_order:
                    if task in unavailable_tasks:
                        task_buckets[task] = "UNMEASURED:" + unavailable_tasks[task]
                        continue
                    _path, metrics = tasks[task]
                    row = (metrics.get("ss_features") or {}).get(feature)
                    task_buckets[task] = _perf_status(row)
                run_bucket = run_perf[feature]
            else:
                for task in task_order:
                    if task in unavailable_tasks:
                        task_buckets[task] = "UNMEASURED:" + unavailable_tasks[task]
                        continue
                    _path, metrics = tasks[task]
                    ledger_rows, entries, opportunity, artifact_error = contexts[task]
                    integrity = metrics.get("ss_integrity")
                    if (
                        isinstance(integrity, dict)
                        and integrity.get("visible_audit_complete") is not True
                        and _requires_visible_audit(feature, family)
                    ):
                        task_buckets[task] = "UNMEASURED:visible_audit_incomplete"
                        continue
                    if artifact_error is not None:
                        task_buckets[task] = f"UNMEASURED:{artifact_error}"
                        continue
                    feature_row = (metrics.get("ss_features") or {}).get(feature)
                    readiness = (
                        feature_row.get("ss_readiness")
                        if isinstance(feature_row, dict) else {}
                    )
                    readiness = readiness if isinstance(readiness, dict) else {}
                    if family == "ACQ":
                        if _find_one(_path.parent, "brief_result.json") is None:
                            task_buckets[task] = "UNMEASURED:support:missing_brief_result"
                        else:
                            task_buckets[task] = classify_typed_terminal(
                                feature_row, "support"
                            )
                    elif family == "CAP" and cap_role_for(feature) != "byte_owner":
                        control_row = dict(feature_row) if isinstance(feature_row, dict) else {}
                        control_row["opportunity_evidence"] = opportunity.get(
                            "features", {}
                        ).get(feature, _opportunity_record(feature_row))
                        task_buckets[task] = classify_cap_control(feature, control_row)
                    else:
                        task_buckets[task] = classify_delivery_feature(
                            feature, family,
                            _task_lifecycle(metrics, feature, family),
                            readiness, ledger_rows, entries,
                        )
                run_bucket = _aggregate_bucket(task_buckets)
            task_opportunities: dict[str, dict[str, Any]] = {}
            for task in task_order:
                if task in unavailable_tasks:
                    task_opportunities[task] = {
                        "status": "UNMEASURED",
                        "reason": unavailable_tasks[task],
                        "eligible_opportunity": None,
                        "decision_boundary_evidence": [],
                    }
                    continue
                _path, metrics = tasks[task]
                opportunity = contexts[task][2]
                task_opportunities[task] = (
                    opportunity.get("features", {}).get(
                        feature,
                        _opportunity_record(
                            (metrics.get("ss_features") or {}).get(feature)
                        ),
                    )
                    if family in {"CAP", "FACT"}
                    else {
                        "status": "NOT_APPLICABLE",
                        "reason": f"{family.lower()}_has_no_candidate_delivery_contract",
                    }
                )
            rows_out.append({
                "feature": feature,
                "family": family,
                "run_bucket": run_bucket,
                "task_buckets": task_buckets,
                "bucket_counts": dict(sorted(Counter(task_buckets.values()).items())),
                "task_opportunities": task_opportunities,
            })

    return {
        "schema": "gt.ss_live_diagnosis.v1",
        "feature_count": len(rows_out),
        "task_count": len(task_order),
        "observed_task_count": len(tasks),
        "observed_feature_record_count": len(feature_paths),
        "missing_feature_metric_tasks": list(missing_tasks),
        "duplicate_feature_metric_tasks": list(duplicate_feature_metric_tasks),
        "unexpected_feature_metric_tasks": list(unexpected_feature_tasks),
        "missing_deep_metric_tasks": list(missing_deep_metric_tasks),
        "family_counts": {family: len(features) for family, features in inventory.items()},
        "integrity": diagnosis_integrity,
        "rows": rows_out,
    }


def render_markdown(result: dict[str, Any]) -> str:
    missing = result.get("missing_feature_metric_tasks") or []
    missing_deep = result.get("missing_deep_metric_tasks") or []
    lines = [
        f"requested_tasks={result.get('task_count', 0)}",
        f"observed_feature_records={result.get('observed_feature_record_count', 0)}",
        f"usable_expected_task_records={result.get('observed_task_count', 0)}",
        "missing_feature_records=" + (", ".join(missing) if missing else "NONE"),
        "duplicate_feature_records=" + (
            ", ".join(result.get("duplicate_feature_metric_tasks") or []) or "NONE"
        ),
        "unexpected_feature_records=" + (
            ", ".join(result.get("unexpected_feature_metric_tasks") or []) or "NONE"
        ),
        "missing_deep_metrics=" + (", ".join(missing_deep) if missing_deep else "NONE"),
        f"publishable={str(result.get('integrity', {}).get('publishable') is True).lower()}",
        "",
        "| Family | Feature | Run bucket | Per-task buckets |",
        "|---|---|---|---|",
    ]
    for row in result.get("rows", []):
        tasks = "; ".join(
            f"{task}={bucket}" for task, bucket in row["task_buckets"].items()
        )
        lines.append(
            f"| {row['family']} | {row['feature']} | {row['run_bucket']} | {tasks} |"
        )
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_dir")
    parser.add_argument("--run-metrics", required=True)
    parser.add_argument(
        "--expected-tasks",
        help="frozen JSON task population; missing records remain explicit UNMEASURED cells",
    )
    parser.add_argument("--format", choices=("json", "markdown"), default="json")
    parser.add_argument("--output")
    args = parser.parse_args(argv)

    diagnose_kwargs = (
        {"expected_tasks_path": args.expected_tasks}
        if args.expected_tasks is not None else {}
    )
    result = diagnose_run(args.run_dir, args.run_metrics, **diagnose_kwargs)
    rendered = (
        json.dumps(result, indent=2, sort_keys=False) + "\n"
        if args.format == "json" else render_markdown(result)
    )
    if args.output:
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(rendered, encoding="utf-8")
    else:
        print(rendered, end="")
    return 0 if result["integrity"]["publishable"] is True else 3


if __name__ == "__main__":
    raise SystemExit(main())
