#!/usr/bin/env python3
"""Typed, fail-closed validation for one paid-run SS live-evidence bundle.

This module validates evidence; it never promotes a scoreboard or SS-LIVE bit.
Every positive verdict is reopened from a hash-bound typed producer artifact and
joined to one exact physical delivery in the runtime ledger, trajectory, and
consumption ledger.  Summary booleans are not an input contract.
"""
from __future__ import annotations

import hashlib
import json
import re
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

_SCRIPT_DIR = str(Path(__file__).resolve().parent)
if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)

from groundtruth.runtime.feature_lineage import cap_role_for  # noqa: E402
from scripts.swebench.consumption_ledger import scan_test_identity_leaks  # noqa: E402
from scripts.swebench.gt_feature_inventory import (  # noqa: E402
    canonical_feature_inventory,
)


CONTRACT_SCHEMA = "gt.ss_live_evidence.v1"
VALIDATION_SCHEMA = "gt.ss_live_evidence.validation.v1"
_SHA16 = re.compile(r"^[0-9a-f]{16}$")
_SHA40 = re.compile(r"^[0-9a-f]{40}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SUBSTRATE = re.compile(r"^.+@sha256:[0-9a-f]{64}$")
_LEAK_RE = re.compile(
    r"\b(?:FAIL_TO_PASS|PASS_TO_PASS|test_[A-Za-z0-9_]+|assert(?:ion)?)\b",
    re.IGNORECASE,
)

_CONTRACT_FIELDS = {"schema", "evidence_source", "run_identity", "deliveries"}
_IDENTITY_FIELDS = {
    "workflow_run_id", "task_id", "gt_ref_prepared_sha", "gt_ref_resolved",
    "substrate_digest", "seam_sha256", "profile", "baseline",
}
_DELIVERY_FIELDS = {
    "delivery_id", "features", "physical_delivery", "acknowledgment",
    "truth_evidence", "timing_evidence", "leak_count", "dose_count",
    "fair_probe_evidence",
}
_FEATURE_FIELDS = {"feature_id", "family", "role"}
_PHYSICAL_FIELDS = {
    "runtime_ledger_index", "content_sha256_16", "chars_delivered",
    "physical_id", "home_message",
}
_ACK_FIELDS = {"receipt_level", "message_index", "channel", "anchor"}
_REF_FIELDS = {"artifact_id", "sha256"}
_COMPLETION_IDENTITY_PATH = "gt_artifacts/gt_run_identity.json"
_COMPLETION_EVIDENCE_PATH = "gt_artifacts/gt_live_evidence_artifacts.json"
_EVIDENCE_MANIFEST_FIELDS = {
    "schema", "workflow_run_id", "task_id", "gt_ref_prepared_sha",
    "substrate_digest", "seam_sha256", "profile", "baseline", "artifacts",
}

_TRUTH_FIELDS = {
    "schema", "evidence_source", "workflow_run_id", "task_id", "seam_sha256",
    "delivery_seal", "features", "producer_id", "truth_verdict",
    "freshness_verdict", "source_evidence", "source_field",
    "observed_state_evidence",
}
_TIMING_FIELDS = {
    "schema", "evidence_source", "workflow_run_id", "task_id", "seam_sha256",
    "delivery_seal", "features", "grader", "adjudicator_id", "verdict",
    "required_event", "actual_event", "home_message",
    "decision_boundary_message", "research_boundary_evidence",
}
_FAIR_FIELDS = {
    "schema", "evidence_source", "workflow_run_id", "task_id", "seam_sha256",
    "delivery_seal", "features", "probe_id", "design", "adjudicator_id",
    "causal_verdict", "assignment_unit_id", "assignment_evidence",
    "treatment_evidence", "control_evidence", "outcome_evidence",
    "outcome_metric", "acknowledgment_message",
}


def _artifact_document(value: object) -> tuple[dict[str, Any] | None, bytes | None]:
    if isinstance(value, bytes):
        raw = value
    elif isinstance(value, str):
        raw = value.encode("utf-8")
    else:
        return None, None
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None, raw
    return (dict(parsed), raw) if isinstance(parsed, Mapping) else (None, raw)


def _expected_feature_roles() -> dict[str, tuple[str, str]]:
    inventory = canonical_feature_inventory()
    roles: dict[str, tuple[str, str]] = {}
    for feature in inventory["ACQ"]:
        roles[feature] = ("ACQ", "support")
    cap_roles = {
        "byte_owner": "capability_support",
        "mediator": "infra_control",
        "eligibility": "eligibility_control",
    }
    for feature in inventory["CAP"]:
        roles[feature] = ("CAP", cap_roles[cap_role_for(feature)])
    for feature in inventory["FACT"]:
        roles[feature] = ("FACT", "fact_delivery")
    for feature in inventory["PERF"]:
        roles[feature] = ("PERF", "measurement")
    return roles


def _feature_key(value: Mapping[str, Any]) -> tuple[str, str, str] | None:
    feature_id, family, role = (
        value.get("feature_id"), value.get("family"), value.get("role"),
    )
    if not all(isinstance(item, str) and item for item in (feature_id, family, role)):
        return None
    return feature_id, family, role


def _validate_features(
    raw: object, prefix: str, errors: list[str],
) -> tuple[tuple[str, str, str], ...]:
    if not isinstance(raw, list) or not raw:
        errors.append(f"{prefix}:features:missing")
        return ()
    roles = _expected_feature_roles()
    keys: list[tuple[str, str, str]] = []
    for value in raw:
        if not isinstance(value, Mapping):
            errors.append(f"{prefix}:features:malformed")
            continue
        unexpected = sorted(set(value) - _FEATURE_FIELDS)
        if unexpected or set(value) != _FEATURE_FIELDS:
            errors.append(f"{prefix}:features:fields")
            continue
        key = _feature_key(value)
        if key is None:
            errors.append(f"{prefix}:features:malformed")
            continue
        feature, family, role = key
        expected = roles.get(feature)
        if expected is None:
            errors.append(f"{prefix}:features:unknown:{feature}")
        elif expected != (family, role):
            errors.append(f"{prefix}:features:role_mismatch:{feature}")
        elif role in {"infra_control", "eligibility_control", "measurement"}:
            errors.append(f"{prefix}:features:not_delivery_bound:{feature}")
        keys.append(key)
    if len(keys) != len(set(keys)):
        errors.append(f"{prefix}:features:duplicate")
    return tuple(sorted(set(keys)))


def _resolve_artifact_bytes(
    reference: object,
    artifacts: Mapping[str, object],
    prefix: str,
    errors: list[str],
    authorized_artifacts: Mapping[str, str],
) -> bytes | None:
    if not isinstance(reference, Mapping) or set(reference) != _REF_FIELDS:
        errors.append(f"{prefix}:reference_malformed")
        return None
    artifact_id, claimed = reference.get("artifact_id"), reference.get("sha256")
    if not isinstance(artifact_id, str) or not artifact_id:
        errors.append(f"{prefix}:artifact_id")
        return None
    if not isinstance(claimed, str) or _SHA256.fullmatch(claimed) is None:
        errors.append(f"{prefix}:sha256")
        return None
    if artifact_id not in authorized_artifacts:
        errors.append(f"{prefix}:not_completion_sealed")
        return None
    if authorized_artifacts[artifact_id] != claimed:
        errors.append(f"{prefix}:completion_hash_mismatch")
        return None
    if artifact_id not in artifacts:
        errors.append(f"{prefix}:artifact_missing")
        return None
    if isinstance(artifacts[artifact_id], Mapping):
        errors.append(f"{prefix}:exact_bytes_required")
        return None
    _document, raw = _artifact_document(artifacts[artifact_id])
    if raw is None or hashlib.sha256(raw).hexdigest() != claimed:
        errors.append(f"{prefix}:hash_mismatch")
        return None
    return raw


def _resolve_artifact(
    reference: object,
    artifacts: Mapping[str, object],
    prefix: str,
    errors: list[str],
    authorized_artifacts: Mapping[str, str],
) -> dict[str, Any] | None:
    raw = _resolve_artifact_bytes(
        reference, artifacts, prefix, errors, authorized_artifacts,
    )
    if raw is None:
        return None
    document, _raw = _artifact_document(raw)
    if document is None:
        errors.append(f"{prefix}:artifact_malformed")
        return None
    return document


def _resolve_nested_evidence(
    wrapper: Mapping[str, Any],
    fields: tuple[str, ...],
    artifacts: Mapping[str, object],
    seen_artifacts: set[str],
    authorized_artifacts: Mapping[str, str],
    prefix: str,
    errors: list[str],
) -> None:
    for field in fields:
        reference = wrapper.get(field)
        nested_prefix = f"{prefix}:{field}"
        if isinstance(reference, Mapping) and isinstance(reference.get("artifact_id"), str):
            artifact_id = reference["artifact_id"]
            if artifact_id in seen_artifacts:
                errors.append(f"{nested_prefix}:duplicate_artifact")
            seen_artifacts.add(artifact_id)
        raw = _resolve_artifact_bytes(
            reference, artifacts, nested_prefix, errors, authorized_artifacts,
        )
        if raw is not None and not raw:
            errors.append(f"{nested_prefix}:empty")


def _identity_errors(
    contract_identity: object, canonical: Mapping[str, Any],
) -> tuple[dict[str, Any], list[str]]:
    errors: list[str] = []
    if not isinstance(contract_identity, Mapping):
        return {}, ["identity:missing"]
    identity = dict(contract_identity)
    if set(identity) != _IDENTITY_FIELDS:
        errors.append("identity:fields")
    prepared = identity.get("gt_ref_prepared_sha")
    resolved = identity.get("gt_ref_resolved")
    if not isinstance(prepared, str) or _SHA40.fullmatch(prepared) is None:
        errors.append("identity:gt_ref_prepared_sha")
    if not isinstance(resolved, str) or _SHA40.fullmatch(resolved) is None:
        errors.append("identity:gt_ref_resolved")
    if prepared != resolved:
        errors.append("identity:prepared_resolved_mismatch")
    if not isinstance(identity.get("substrate_digest"), str) or (
        _SUBSTRATE.fullmatch(identity["substrate_digest"]) is None
    ):
        errors.append("identity:substrate_digest")
    if not isinstance(identity.get("seam_sha256"), str) or (
        _SHA256.fullmatch(identity["seam_sha256"]) is None
    ):
        errors.append("identity:seam_sha256")
    for key in ("workflow_run_id", "task_id"):
        if not isinstance(identity.get(key), str) or not identity[key]:
            errors.append(f"identity:{key}")
    if identity.get("profile") != "2":
        errors.append("identity:profile")
    if identity.get("baseline") is not False:
        errors.append("identity:baseline")
    bindings = {
        "workflow_run_id": canonical.get("workflow_run_id"),
        "task_id": canonical.get("task_id"),
        "gt_ref_prepared_sha": canonical.get("gt_ref_prepared_sha"),
        "gt_ref_resolved": canonical.get("gt_ref_resolved"),
        "substrate_digest": canonical.get("substrate_digest_actual"),
        "seam_sha256": canonical.get("seam_sha256"),
        "profile": canonical.get("profile"),
        "baseline": canonical.get("baseline"),
    }
    for key, expected in bindings.items():
        if expected is None or identity.get(key) != expected:
            errors.append(f"identity:canonical_mismatch:{key}")
    return identity, errors


def _visible_seal_matches(
    trajectory: Mapping[str, Any], chars: int, seal: str,
) -> list[tuple[int, int, int, str]]:
    messages = trajectory.get("messages")
    if not isinstance(messages, list):
        return []
    matches: list[tuple[int, int, int, str]] = []
    for message_index, message in enumerate(messages):
        if not isinstance(message, Mapping):
            continue
        if message.get("role") not in {"user", "tool", "system", "exit"} and (
            message.get("type") != "function_call_output"
        ):
            continue
        content = message.get("content")
        if not isinstance(content, str) or chars <= 0 or chars > len(content):
            continue
        for start in range(len(content) - chars + 1):
            payload = content[start:start + chars]
            if hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16] == seal:
                matches.append((message_index, start, start + chars, payload))
    return matches


def _assistant_actions(message: Mapping[str, Any]) -> list[str]:
    actions: list[str] = []
    extra = message.get("extra")
    if isinstance(extra, Mapping) and isinstance(extra.get("actions"), list):
        for action in extra["actions"]:
            if not isinstance(action, Mapping):
                continue
            text = " ".join(
                str(action[key]) for key in ("name", "command", "path", "file_path", "file")
                if isinstance(action.get(key), str) and action[key]
            )
            if text:
                actions.append(text)
    for call in message.get("tool_calls") or []:
        if not isinstance(call, Mapping) or not isinstance(call.get("function"), Mapping):
            continue
        function = call["function"]
        text = " ".join(
            value for value in (function.get("name"), function.get("arguments"))
            if isinstance(value, str) and value
        )
        if text:
            actions.append(text)
    return actions


def _contains_exact_anchor(text: str, anchor: str) -> bool:
    """Match one delivered token/path/phrase without accepting substrings."""
    boundary = r"A-Za-z0-9_./-"
    return re.search(
        rf"(?<![{boundary}]){re.escape(anchor)}(?![{boundary}])", text,
    ) is not None


def _validate_acknowledgment(
    raw: object,
    prefix: str,
    home: int,
    delivered_payload: str,
    trajectory: Mapping[str, Any],
    consumption_entry: Mapping[str, Any],
    errors: list[str],
) -> int | None:
    if not isinstance(raw, Mapping) or set(raw) != _ACK_FIELDS:
        errors.append(f"{prefix}:acknowledgment:fields")
        return None
    level, index, channel, anchor = (
        raw.get("receipt_level"), raw.get("message_index"),
        raw.get("channel"), raw.get("anchor"),
    )
    if not isinstance(level, int) or isinstance(level, bool) or level not in {2, 3}:
        errors.append(f"{prefix}:acknowledgment:receipt_level")
    if not isinstance(index, int) or isinstance(index, bool):
        errors.append(f"{prefix}:acknowledgment:message_index")
        return None
    if index <= home:
        errors.append(f"{prefix}:acknowledgment:not_later")
    messages = trajectory.get("messages")
    if not isinstance(messages, list) or index < 0 or index >= len(messages):
        errors.append(f"{prefix}:acknowledgment:message_missing")
        return index
    message = messages[index]
    if not isinstance(message, Mapping) or message.get("role") != "assistant":
        errors.append(f"{prefix}:acknowledgment:not_assistant")
        return index
    if not isinstance(anchor, str) or not anchor.strip():
        errors.append(f"{prefix}:acknowledgment:anchor")
    elif not _contains_exact_anchor(delivered_payload, anchor):
        errors.append(f"{prefix}:acknowledgment:anchor_not_delivered_entity")
    elif channel == "prose":
        if not isinstance(message.get("content"), str) or not _contains_exact_anchor(
            message["content"], anchor,
        ):
            errors.append(f"{prefix}:acknowledgment:anchor_not_found")
    elif channel == "action":
        if not any(
            _contains_exact_anchor(action, anchor) for action in _assistant_actions(message)
        ):
            errors.append(f"{prefix}:acknowledgment:anchor_not_found")
    else:
        errors.append(f"{prefix}:acknowledgment:channel")
    if isinstance(level, int) and not isinstance(level, bool):
        if not isinstance(consumption_entry.get("receipt"), int) or consumption_entry["receipt"] < level:
            errors.append(f"{prefix}:acknowledgment:receipt_disagrees")
        expected_index = (
            consumption_entry.get("referenced_msg_index")
            if level == 2 else consumption_entry.get("acted_msg_index")
        )
        if expected_index != index:
            errors.append(f"{prefix}:acknowledgment:index_disagrees")
    return index


def _artifact_common_errors(
    artifact: Mapping[str, Any],
    expected_fields: set[str],
    schema: str,
    identity: Mapping[str, Any],
    seal: str,
    feature_keys: tuple[tuple[str, str, str], ...],
    prefix: str,
) -> list[str]:
    errors: list[str] = []
    if set(artifact) != expected_fields:
        errors.append(f"{prefix}:fields")
    if artifact.get("schema") != schema:
        errors.append(f"{prefix}:schema")
    if artifact.get("evidence_source") != "paid_live":
        errors.append(f"{prefix}:not_paid_live")
    for field, label in (
        ("workflow_run_id", "run_binding"), ("task_id", "task_binding"),
        ("seam_sha256", "seam_binding"), ("delivery_seal", "seal_binding"),
    ):
        expected = seal if field == "delivery_seal" else identity.get(field)
        if artifact.get(field) != expected:
            errors.append(f"{prefix}:{label}")
    artifact_features = artifact.get("features")
    observed: list[tuple[str, str, str]] = []
    if isinstance(artifact_features, list):
        for feature in artifact_features:
            if isinstance(feature, Mapping) and set(feature) == _FEATURE_FIELDS:
                key = _feature_key(feature)
                if key is not None:
                    observed.append(key)
    if tuple(sorted(observed)) != feature_keys or len(observed) != len(set(observed)):
        errors.append(f"{prefix}:feature_binding")
    return errors


def _lineage_keys(entry: Mapping[str, Any]) -> set[tuple[str, str, str]]:
    lineage = entry.get("feature_lineage")
    raw = lineage.get("features") if isinstance(lineage, Mapping) else None
    keys: set[tuple[str, str, str]] = set()
    role_map = {("CAP", "byte_owner"): "capability_support", ("FACT", "fact"): "fact_delivery"}
    if not isinstance(raw, list):
        return keys
    for feature in raw:
        if not isinstance(feature, Mapping):
            continue
        category, feature_id, raw_role = (
            feature.get("category"), feature.get("feature_id"), feature.get("role"),
        )
        role = role_map.get((category, raw_role))
        if isinstance(feature_id, str) and isinstance(category, str) and role:
            keys.add((feature_id, category, role))
    return keys


def _completion_bound_inventory(
    receipt_value: object,
    completion_artifacts: Mapping[str, object],
    identity: Mapping[str, Any],
    errors: list[str],
) -> dict[str, str]:
    """Open the completion receipt, sealed identity, and evidence inventory."""
    if isinstance(receipt_value, Mapping):
        errors.append("completion:exact_bytes_required")
        receipt: dict[str, Any] = {}
    else:
        receipt, _raw = _artifact_document(receipt_value)
        if receipt is None:
            receipt = {}
            errors.append("completion:malformed")
    if receipt.get("schema") != "gt.task_completion.v1":
        errors.append("completion:schema")
    if receipt.get("task") != identity.get("task_id"):
        errors.append("completion:task_binding")
    if receipt.get("workflow_run_id") != identity.get("workflow_run_id"):
        errors.append("completion:run_binding")
    if receipt.get("status") != "COMPLETE":
        errors.append("completion:status")
    sealed = receipt.get("artifact_sha256")
    if not isinstance(sealed, Mapping):
        sealed = {}
        errors.append("completion:artifact_inventory")

    opened: dict[str, dict[str, Any]] = {}
    for path, label in (
        (_COMPLETION_IDENTITY_PATH, "run_identity"),
        (_COMPLETION_EVIDENCE_PATH, "evidence_manifest"),
    ):
        claimed = sealed.get(path)
        value = completion_artifacts.get(path)
        if value is None:
            errors.append(f"completion:{label}:missing")
            continue
        if isinstance(value, Mapping):
            errors.append(f"completion:{label}:exact_bytes_required")
            continue
        document, raw = _artifact_document(value)
        if (
            not isinstance(claimed, str) or _SHA256.fullmatch(claimed) is None
            or raw is None or hashlib.sha256(raw).hexdigest() != claimed
        ):
            errors.append(f"completion:{label}:hash_mismatch")
            continue
        if document is None:
            errors.append(f"completion:{label}:malformed")
            continue
        opened[label] = document

    run_identity = opened.get("run_identity")
    if run_identity is not None:
        if run_identity.get("schema") != "gt.run_identity.v1":
            errors.append("completion:run_identity:schema")
        identity_bindings = {
            "workflow_run_id": "workflow_run_id",
            "gt_ref_prepared_sha": "gt_ref_prepared_sha",
            "gt_ref_resolved": "gt_ref_resolved",
            "substrate_digest_actual": "substrate_digest",
            "seam_sha256": "seam_sha256",
            "baseline": "baseline",
        }
        for source, target in identity_bindings.items():
            if run_identity.get(source) != identity.get(target):
                errors.append(f"completion:run_identity:{source}_binding")

    manifest = opened.get("evidence_manifest")
    if manifest is None:
        return {}
    if set(manifest) != _EVIDENCE_MANIFEST_FIELDS:
        errors.append("completion:evidence_manifest:fields")
    if manifest.get("schema") != "gt.live_evidence_artifacts.v1":
        errors.append("completion:evidence_manifest:schema")
    for field in (
        "workflow_run_id", "task_id", "gt_ref_prepared_sha", "substrate_digest",
        "seam_sha256", "profile", "baseline",
    ):
        if manifest.get(field) != identity.get(field):
            errors.append(f"completion:evidence_manifest:{field}_binding")
    artifacts = manifest.get("artifacts")
    if (
        not isinstance(artifacts, Mapping) or not artifacts
        or any(
            not isinstance(key, str) or not key
            or not isinstance(value, str) or _SHA256.fullmatch(value) is None
            for key, value in artifacts.items()
        )
    ):
        errors.append("completion:evidence_manifest:artifact_inventory")
        return {}
    return dict(artifacts)


def validate_live_evidence(
    contract: Mapping[str, Any],
    *,
    canonical_identity: Mapping[str, Any],
    runtime_ledger_rows: Sequence[Mapping[str, Any]],
    trajectory: Mapping[str, Any],
    consumption_ledger: Mapping[str, Any],
    evidence_artifacts: Mapping[str, object],
    completion_receipt: bytes | str,
    completion_artifacts: Mapping[str, object],
) -> dict[str, Any]:
    """Validate one paid-live bundle and return no promotion authority."""
    errors: list[str] = []
    if not isinstance(contract, Mapping):
        contract = {}
        errors.append("contract:malformed")
    unexpected = sorted(set(contract) - _CONTRACT_FIELDS)
    for field in unexpected:
        errors.append(f"contract:unexpected_fields:{field}")
    if set(contract) - set(unexpected) != _CONTRACT_FIELDS:
        errors.append("contract:fields")
    if contract.get("schema") != CONTRACT_SCHEMA:
        errors.append("contract:schema")
    if contract.get("evidence_source") != "paid_live":
        errors.append("contract:evidence_source_not_paid_live")
    identity, identity_issues = _identity_errors(
        contract.get("run_identity"), canonical_identity,
    )
    errors.extend(identity_issues)
    authorized_artifacts = _completion_bound_inventory(
        completion_receipt, completion_artifacts, identity, errors,
    )
    for field in ("workflow_run_id", "task_id", "seam_sha256"):
        if trajectory.get(field) != identity.get(field):
            errors.append(f"trajectory:{field}_binding")
    if consumption_ledger.get("schema") != "gt.consumption_ledger.v2":
        errors.append("consumption:schema")
    if consumption_ledger.get("visible_audit_complete") is not True:
        errors.append("consumption:visible_audit_incomplete")
    entries = consumption_ledger.get("entries")
    if not isinstance(entries, list):
        entries = []
        errors.append("consumption:entries")

    deliveries = contract.get("deliveries")
    if not isinstance(deliveries, list) or not deliveries:
        deliveries = []
        errors.append("contract:deliveries")
    seen_ids: set[str] = set()
    seen_physical: set[tuple[str, int, str]] = set()
    seen_artifacts: set[str] = set()
    validated: list[str] = []
    for ordinal, raw_delivery in enumerate(deliveries):
        fallback = f"delivery[{ordinal}]"
        if not isinstance(raw_delivery, Mapping):
            errors.append(f"{fallback}:malformed")
            continue
        delivery_id = raw_delivery.get("delivery_id")
        prefix = delivery_id if isinstance(delivery_id, str) and delivery_id else fallback
        unexpected_delivery = sorted(set(raw_delivery) - _DELIVERY_FIELDS)
        for field in unexpected_delivery:
            errors.append(f"{prefix}:unexpected_fields:{field}")
        if set(raw_delivery) - set(unexpected_delivery) != _DELIVERY_FIELDS:
            errors.append(f"{prefix}:fields")
        if not isinstance(delivery_id, str) or not delivery_id:
            errors.append(f"{prefix}:delivery_id")
        elif delivery_id in seen_ids:
            errors.append(f"{prefix}:delivery_id:duplicate")
        else:
            seen_ids.add(delivery_id)
        feature_keys = _validate_features(raw_delivery.get("features"), prefix, errors)

        physical = raw_delivery.get("physical_delivery")
        if not isinstance(physical, Mapping) or set(physical) != _PHYSICAL_FIELDS:
            errors.append(f"{prefix}:physical_delivery:fields")
            continue
        ledger_index, seal, chars, physical_id, home = (
            physical.get("runtime_ledger_index"), physical.get("content_sha256_16"),
            physical.get("chars_delivered"), physical.get("physical_id"),
            physical.get("home_message"),
        )
        if not isinstance(ledger_index, int) or isinstance(ledger_index, bool) or ledger_index < 0:
            errors.append(f"{prefix}:physical_delivery:runtime_ledger_index")
            continue
        if not isinstance(seal, str) or _SHA16.fullmatch(seal) is None:
            errors.append(f"{prefix}:physical_delivery:seal")
            continue
        if not isinstance(chars, int) or isinstance(chars, bool) or chars <= 0:
            errors.append(f"{prefix}:physical_delivery:chars")
            continue
        if not isinstance(physical_id, str) or not physical_id:
            errors.append(f"{prefix}:physical_delivery:physical_id")
            continue
        if not isinstance(home, int) or isinstance(home, bool) or home < 0:
            errors.append(f"{prefix}:physical_delivery:home_message")
            continue
        physical_key = (seal, chars, physical_id)
        if physical_key in seen_physical:
            errors.append(f"{prefix}:physical_delivery:duplicate")
        seen_physical.add(physical_key)

        runtime_row: Mapping[str, Any] = {}
        if ledger_index >= len(runtime_ledger_rows):
            errors.append(f"{prefix}:runtime_ledger:index_missing")
        else:
            runtime_row = runtime_ledger_rows[ledger_index]
            if not isinstance(runtime_row, Mapping):
                errors.append(f"{prefix}:runtime_ledger:row_malformed")
                runtime_row = {}
            if runtime_row.get("outcome") != "delivered":
                errors.append(f"{prefix}:runtime_ledger:not_delivered")
            for field, expected, label in (
                ("workflow_run_id", identity.get("workflow_run_id"), "run_binding"),
                ("task_id", identity.get("task_id"), "task_binding"),
                ("gt_ref_prepared_sha", identity.get("gt_ref_prepared_sha"), "prepared_binding"),
                ("substrate_digest", identity.get("substrate_digest"), "substrate_binding"),
                ("seam_sha256", identity.get("seam_sha256"), "seam_binding"),
            ):
                if runtime_row.get(field) != expected:
                    errors.append(f"{prefix}:runtime_ledger:{label}")
            if runtime_row.get("content_sha256_16") != seal:
                errors.append(f"{prefix}:runtime_ledger:seal_mismatch")
            if runtime_row.get("chars_delivered") != chars:
                errors.append(f"{prefix}:runtime_ledger:chars_mismatch")

        visible_matches = _visible_seal_matches(trajectory, chars, seal)
        exact_matches = [
            match for match in visible_matches
            if match[0] == home and f"m{match[0]}:{match[1]}:{match[2]}" == physical_id
        ]
        if len(visible_matches) != 1 or len(exact_matches) != 1:
            errors.append(f"{prefix}:trajectory:physical_delivery_not_unique")
        payload = exact_matches[0][3] if exact_matches else ""

        consumption_matches = [
            entry for entry in entries
            if isinstance(entry, Mapping)
            and entry.get("physical_id") == physical_id
            and entry.get("content_sha256_16") == seal
            and entry.get("chars") == chars
            and entry.get("msg_index") == home
        ]
        if len(consumption_matches) != 1:
            errors.append(f"{prefix}:consumption:physical_join")
            consumption_entry: Mapping[str, Any] = {}
        else:
            consumption_entry = consumption_matches[0]
            if consumption_entry.get("source") != "trajectory" or consumption_entry.get("joined") is not True:
                errors.append(f"{prefix}:consumption:not_byte_joined")
            if consumption_entry.get("join_method") != "seal":
                errors.append(f"{prefix}:consumption:exact_seal_join_required")
            lineage = _lineage_keys(consumption_entry)
            for feature, family, role in feature_keys:
                if family in {"CAP", "FACT"} and (feature, family, role) not in lineage:
                    errors.append(f"{prefix}:features:lineage_mismatch:{feature}")

        ack_index = _validate_acknowledgment(
            raw_delivery.get("acknowledgment"), prefix, home, payload, trajectory,
            consumption_entry, errors,
        )

        observed_leak_count = len(set(scan_test_identity_leaks(payload)))
        if _LEAK_RE.search(payload):
            observed_leak_count = max(observed_leak_count, 1)
        leak_claim = raw_delivery.get("leak_count")
        if not isinstance(leak_claim, int) or isinstance(leak_claim, bool) or leak_claim != 0:
            errors.append(f"{prefix}:leak_count:must_be_zero_integer")
        if observed_leak_count:
            errors.append(f"{prefix}:leak_count:observed_nonzero")

        physical_at_home = {
            entry.get("physical_id")
            for entry in entries
            if isinstance(entry, Mapping)
            and entry.get("source") == "trajectory"
            and entry.get("joined") is True
            and entry.get("join_method") == "seal"
            and entry.get("msg_index") == home
            and isinstance(entry.get("physical_id"), str)
        }
        runtime_physical_at_home: set[tuple[int, int, str]] = set()
        for row in runtime_ledger_rows:
            if not isinstance(row, Mapping) or row.get("outcome") != "delivered":
                continue
            row_chars, row_seal = row.get("chars_delivered"), row.get("content_sha256_16")
            if (
                not isinstance(row_chars, int) or isinstance(row_chars, bool)
                or row_chars <= 0 or not isinstance(row_seal, str)
                or _SHA16.fullmatch(row_seal) is None
            ):
                continue
            for msg_index, start, end, _payload in _visible_seal_matches(
                trajectory, row_chars, row_seal,
            ):
                if msg_index == home:
                    runtime_physical_at_home.add((start, end, row_seal))
        observed_dose = max(len(physical_at_home), len(runtime_physical_at_home))
        dose_claim = raw_delivery.get("dose_count")
        if not isinstance(dose_claim, int) or isinstance(dose_claim, bool) or dose_claim != 1:
            errors.append(f"{prefix}:dose_count:must_be_one_integer")
        if observed_dose != 1:
            errors.append(f"{prefix}:dose_count:observed:{observed_dose}")

        resolved: dict[str, dict[str, Any] | None] = {}
        for field in ("truth_evidence", "timing_evidence", "fair_probe_evidence"):
            ref = raw_delivery.get(field)
            if isinstance(ref, Mapping) and isinstance(ref.get("artifact_id"), str):
                if ref["artifact_id"] in seen_artifacts:
                    errors.append(f"{prefix}:{field}:duplicate_artifact")
                seen_artifacts.add(ref["artifact_id"])
            resolved[field] = _resolve_artifact(
                ref, evidence_artifacts, f"{prefix}:{field}", errors,
                authorized_artifacts,
            )

        truth = resolved["truth_evidence"]
        if truth is not None:
            errors.extend(_artifact_common_errors(
                truth, _TRUTH_FIELDS, "gt.producer_truth.v1", identity, seal,
                feature_keys, f"{prefix}:truth_evidence",
            ))
            if truth.get("producer_id") != runtime_row.get("runtime_producer_id"):
                errors.append(f"{prefix}:truth_evidence:producer_authority")
            if truth.get("truth_verdict") != "TRUE":
                errors.append(f"{prefix}:truth_evidence:truth_not_true")
            if truth.get("freshness_verdict") != "CURRENT":
                errors.append(f"{prefix}:truth_evidence:freshness_not_current")
            _resolve_nested_evidence(
                truth, ("source_evidence", "observed_state_evidence"),
                evidence_artifacts, seen_artifacts, authorized_artifacts,
                f"{prefix}:truth_evidence", errors,
            )
            if not isinstance(truth.get("source_field"), str) or not truth["source_field"]:
                errors.append(f"{prefix}:truth_evidence:source_field")

        timing = resolved["timing_evidence"]
        if timing is not None:
            errors.extend(_artifact_common_errors(
                timing, _TIMING_FIELDS, "gt.rl_timing.v1", identity, seal,
                feature_keys, f"{prefix}:timing_evidence",
            ))
            if timing.get("grader") != "chronological_research_boundary" or (
                timing.get("adjudicator_id") != "gt_math_chronological_v1"
            ):
                errors.append(f"{prefix}:timing_evidence:adjudicator_authority")
            if timing.get("verdict") != "ON_TIME":
                errors.append(f"{prefix}:timing_evidence:not_on_time")
            if timing.get("required_event") != timing.get("actual_event"):
                errors.append(f"{prefix}:timing_evidence:event_mismatch")
            if timing.get("home_message") != home:
                errors.append(f"{prefix}:timing_evidence:home_binding")
            boundary = timing.get("decision_boundary_message")
            if not isinstance(boundary, int) or isinstance(boundary, bool) or home > boundary:
                errors.append(f"{prefix}:timing_evidence:decision_boundary")
            _resolve_nested_evidence(
                timing, ("research_boundary_evidence",), evidence_artifacts,
                seen_artifacts, authorized_artifacts,
                f"{prefix}:timing_evidence", errors,
            )

        fair = resolved["fair_probe_evidence"]
        if fair is not None:
            errors.extend(_artifact_common_errors(
                fair, _FAIR_FIELDS, "gt.fair_probe.v1", identity, seal,
                feature_keys, f"{prefix}:fair_probe_evidence",
            ))
            if fair.get("design") not in {"MATCHED", "SHADOW"} or (
                fair.get("adjudicator_id") != "gt_fair_probe_v1"
            ):
                errors.append(f"{prefix}:fair_probe_evidence:adjudicator_authority")
            if fair.get("causal_verdict") != "GT_CAUSED_BEHAVIOR":
                errors.append(f"{prefix}:fair_probe_evidence:not_causal")
            if fair.get("acknowledgment_message") != ack_index:
                errors.append(f"{prefix}:fair_probe_evidence:ack_binding")
            _resolve_nested_evidence(
                fair,
                (
                    "assignment_evidence", "treatment_evidence",
                    "control_evidence", "outcome_evidence",
                ),
                evidence_artifacts, seen_artifacts, authorized_artifacts,
                f"{prefix}:fair_probe_evidence", errors,
            )
            for field in ("probe_id", "assignment_unit_id", "outcome_metric"):
                if not isinstance(fair.get(field), str) or not fair[field]:
                    errors.append(f"{prefix}:fair_probe_evidence:{field}")

        if isinstance(delivery_id, str) and delivery_id and not any(
            error.startswith(prefix + ":") for error in errors
        ):
            validated.append(delivery_id)

    return {
        "schema": VALIDATION_SCHEMA,
        "valid": not errors,
        "errors": sorted(set(errors)),
        "validated_delivery_ids": validated if not errors else [],
        "proof_scope": "validation_only_no_ss_live_promotion",
    }
