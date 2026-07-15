#!/usr/bin/env python3
"""Plan paid live SS opportunities from validated historical task shapes.

Every input in this module is selection evidence only.  It cannot satisfy a
terminal gate, create a live witness, or promote an SS feature.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable, Mapping

from groundtruth.runtime.fact_registry import fact_role_for
from groundtruth.runtime.feature_lineage import (
    CAP_BYTE_OWNER_MECHANISMS,
    cap_role_for,
)

try:
    from scripts.swebench.gt_feature_inventory import canonical_feature_inventory
except ModuleNotFoundError:
    from gt_feature_inventory import canonical_feature_inventory  # type: ignore[no-redef]


SCHEMA = "gt.ss_live_coverage_campaign.v2"
STRUCTURAL_SCHEMA = "gt.ss_structural_opportunity_manifest.v1"
METRICS_SCHEMA = "gt.feature_metrics.v1"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_STRUCTURAL_FACT = {
    "callable_signature_changed": "signature_delta",
    "source_file_added": "newfile_precedent",
}
_CALLABLE_DECLARATIONS = (
    re.compile(r"^(?:async\s+)?def\s+([A-Za-z_]\w*)\s*\("),
    re.compile(r"^(?:pub(?:\([^)]*\))?\s+)?(?:async\s+)?fn\s+([A-Za-z_]\w*)\s*\("),
    re.compile(r"^func\s+(?:\([^)]*\)\s*)?([A-Za-z_]\w*)\s*\("),
    re.compile(
        r"^(?:(?:export|default)\s+)*(?:async\s+)?function\s+"
        r"([A-Za-z_$][\w$]*)\s*\("
    ),
)
_NON_CALLABLE_NAMES = frozenset({"if", "for", "while", "switch", "catch", "return"})
SelectionEvidence = dict[tuple[str, str], list[dict[str, Any]]]


def _valid_source(value: object) -> bool:
    return bool(
        isinstance(value, Mapping)
        and isinstance(value.get("path"), str)
        and value.get("path")
        and isinstance(value.get("sha256"), str)
        and _SHA256.fullmatch(str(value["sha256"]))
    )


def _add_evidence(
    evidence: SelectionEvidence,
    feature: str,
    task: str,
    record: Mapping[str, Any],
) -> None:
    native = dict(record)
    rows = evidence.setdefault((feature, task), [])
    if native not in rows:
        rows.append(native)


def _callable_declaration(line: str) -> tuple[str, str] | None:
    """Return a conservative language-neutral callable declaration identity."""
    stripped = line.strip()
    for pattern in _CALLABLE_DECLARATIONS:
        match = pattern.search(stripped)
        if match is not None:
            return match.group(1), " ".join(stripped.split())
    generic = re.match(
        r"^(?P<prefix>[A-Za-z_$][\w$<>,.?\[\] :*&]*\s+)?"
        r"(?P<name>[A-Za-z_$][\w$]*)\s*\([^()]*\)\s*(?P<suffix>.*)$",
        stripped,
    )
    if generic is None or generic.group("name") in _NON_CALLABLE_NAMES:
        return None
    prefix = generic.group("prefix") or ""
    suffix = generic.group("suffix").strip()
    declaration_suffix = (
        suffix.startswith(("{", ":", "->", "throws ")) or suffix == ";"
    )
    if not declaration_suffix or (suffix == ";" and not prefix.strip()):
        return None
    return generic.group("name"), " ".join(stripped.split())


def _signature_delta_paths(patch: str) -> set[str]:
    """Paths with removed+added declarations for one symbol and a changed signature."""
    removed: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
    added: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
    current_path = ""
    for line in patch.replace("\\", "/").splitlines():
        if line.startswith("+++ b/"):
            current_path = line[6:]
            continue
        if not current_path or line.startswith(("---", "+++")):
            continue
        target = removed if line.startswith("-") else added if line.startswith("+") else None
        if target is None:
            continue
        declaration = _callable_declaration(line[1:])
        if declaration is not None:
            symbol, signature = declaration
            target[current_path][symbol].add(signature)
    changed: set[str] = set()
    for path in set(removed) & set(added):
        for symbol in set(removed[path]) & set(added[path]):
            if removed[path][symbol] != added[path][symbol]:
                changed.add(path)
    return changed


def _positive_selection_signal(
    feature: str,
    family: str,
    row: Mapping[str, Any],
    *,
    cap_role: str | None,
    fact_role: str | None,
) -> tuple[bool, str]:
    readiness = row.get("ss_readiness")
    readiness = readiness if isinstance(readiness, Mapping) else {}
    gates = readiness.get("gates")
    gates = gates if isinstance(gates, Mapping) else {}
    opportunity = row.get("opportunity_evidence")
    opportunity = opportunity if isinstance(opportunity, Mapping) else {}

    if family == "FACT":
        if fact_role == "internal_support":
            if (
                readiness.get("role") == "internal_support"
                and gates.get("runtime_support_receipt") is True
            ):
                return True, "historical_internal_support_receipt"
            return False, "no_typed_internal_support_signal"
        if gates.get("delivered_byte_proven") is True:
            return True, "historical_exact_delivery"
        if opportunity.get("status") == "BOUND":
            return True, "historical_bound_opportunity"
    elif family == "CAP":
        if cap_role == "byte_owner":
            if gates.get("delivered_byte_proven") is True:
                return True, "historical_exact_byte_owner_delivery"
            if opportunity.get("status") == "BOUND":
                return True, "historical_bound_byte_owner_opportunity"
        elif cap_role in {"mediator", "eligibility"}:
            mediation = readiness.get("mediation")
            mediation = mediation if isinstance(mediation, Mapping) else {}
            if (
                readiness.get("role") == "infra_control"
                and gates.get("runtime_member_control_receipt") is True
                and bool(mediation.get("runtime_linked_fact_ids"))
            ):
                return True, "historical_typed_control_participation"
    elif family == "ACQ":
        if gates.get("candidate_local_contribution") is True:
            return True, "historical_candidate_local_contribution"
    # PERF rows are run-population obligations, not task-shape signals.
    return False, f"no_role_correct_{family.lower()}_selection_signal:{feature}"


def load_selection_signals(
    documents: Iterable[Mapping[str, Any]],
    inventory: Mapping[str, tuple[str, ...]],
    *,
    cap_roles: Mapping[str, str] | None = None,
    fact_roles: Mapping[str, str] | None = None,
) -> tuple[dict[str, set[str]], SelectionEvidence]:
    """Return validated historical feature-to-task opportunities with provenance."""
    family_of = {
        feature: family for family, features in inventory.items() for feature in features
    }
    cap_roles = cap_roles or {
        feature: cap_role_for(feature) for feature in inventory.get("CAP", ())
    }
    fact_roles = fact_roles or {
        feature: str(fact_role_for(feature) or "")
        for feature in inventory.get("FACT", ())
    }
    tasks_by_feature: dict[str, set[str]] = defaultdict(set)
    evidence: SelectionEvidence = {}

    for document in documents:
        if document.get("schema") != METRICS_SCHEMA or document.get("profile") != "2":
            continue
        source = document.get("_selection_source")
        task = document.get("task")
        rows = document.get("ss_features")
        if (
            not _valid_source(source)
            or not isinstance(task, str) or not task
            or not isinstance(rows, Mapping)
        ):
            continue
        fact_classes = document.get("fact_classes")
        fact_classes = fact_classes if isinstance(fact_classes, Mapping) else {}
        for feature, row in rows.items():
            expected_family = family_of.get(str(feature))
            if (
                expected_family is None
                or not isinstance(row, Mapping)
                or row.get("family") != expected_family
            ):
                continue
            positive, reason = _positive_selection_signal(
                str(feature), expected_family, row,
                cap_role=cap_roles.get(str(feature)),
                fact_role=fact_roles.get(str(feature)),
            )
            if expected_family == "FACT" and fact_roles.get(str(feature)) != "internal_support":
                lifecycle = fact_classes.get(feature)
                produced = lifecycle.get("produced") if isinstance(lifecycle, Mapping) else None
                if (
                    not positive
                    and isinstance(produced, Mapping)
                    and produced.get("status") == "MEASURED"
                    and produced.get("value") is True
                    and isinstance(produced.get("source_artifact"), str)
                    and produced.get("source_artifact")
                ):
                    positive, reason = True, "historical_producer_reached"
            if not positive:
                continue
            tasks_by_feature[str(feature)].add(task)
            _add_evidence(evidence, str(feature), task, {
                "reason": reason,
                "source_path": source["path"],
                "source_sha256": source["sha256"],
                "proof_scope": "selection_only_no_ss_promotion",
            })

    owner_facts = {
        owner: tuple(sorted({
            binding.fact_class for binding in mechanism.bindings
            if binding.fact_class is not None
        }))
        for owner, mechanism in CAP_BYTE_OWNER_MECHANISMS.items()
    }
    return merge_selection_signals(tasks_by_feature, evidence, owner_facts=owner_facts)


def merge_selection_signals(
    tasks_by_feature: Mapping[str, set[str]],
    evidence: Mapping[tuple[str, str], list[dict[str, Any]]],
    *,
    owner_facts: Mapping[str, tuple[str, ...]],
) -> tuple[dict[str, set[str]], SelectionEvidence]:
    """Copy signals and inherit a structural FACT opportunity to its byte owner."""
    merged = {feature: set(tasks) for feature, tasks in tasks_by_feature.items()}
    merged_evidence: SelectionEvidence = {
        key: [dict(record) for record in records] for key, records in evidence.items()
    }
    for owner, facts in owner_facts.items():
        for fact in facts:
            for task in merged.get(fact, set()):
                merged.setdefault(owner, set()).add(task)
                for source in merged_evidence.get((fact, task), [{}]):
                    inherited = {
                        key: value for key, value in source.items() if key != "reason"
                    }
                    inherited["reason"] = f"owned_fact_opportunity:{fact}"
                    _add_evidence(merged_evidence, owner, task, inherited)
    return merged, merged_evidence


def load_structural_opportunities(
    manifest_path: Path,
) -> tuple[dict[str, set[str]], SelectionEvidence]:
    """Load a task-neutral, exact-source-bound structural opportunity manifest."""
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid structural opportunity manifest: {exc}") from exc
    if not isinstance(manifest, Mapping) or manifest.get("schema") != STRUCTURAL_SCHEMA:
        raise ValueError("unsupported structural opportunity manifest schema")
    source = manifest.get("source_artifact")
    if not _valid_source(source):
        raise ValueError("structural opportunity manifest has malformed source artifact")
    source_path = Path(str(source["path"]))
    if not source_path.is_absolute():
        source_path = manifest_path.parent / source_path
    try:
        source_bytes = source_path.read_bytes()
        actual_source_sha = hashlib.sha256(source_bytes).hexdigest()
    except OSError as exc:
        raise ValueError(f"structural opportunity source artifact unavailable: {exc}") from exc
    if actual_source_sha != source["sha256"]:
        raise ValueError("structural opportunity source artifact hash mismatch")
    patches_by_task: dict[str, str] = {}
    try:
        for line_number, raw_line in enumerate(source_bytes.decode("utf-8").splitlines(), 1):
            if not raw_line.strip():
                continue
            source_record = json.loads(raw_line)
            if not isinstance(source_record, Mapping):
                raise ValueError
            task = source_record.get("instance_id")
            patch = source_record.get("model_patch", source_record.get("patch"))
            if not isinstance(task, str) or not task or not isinstance(patch, str):
                raise ValueError
            if task in patches_by_task:
                raise ValueError(f"duplicate task {task!r}")
            patches_by_task[task] = patch
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ValueError(
            f"structural opportunity source is not canonical patch JSONL at line "
            f"{locals().get('line_number', 0)}: {exc}"
        ) from exc

    opportunities = manifest.get("opportunities")
    if not isinstance(opportunities, list):
        raise ValueError("structural opportunity manifest opportunities must be a list")
    signals: dict[str, set[str]] = defaultdict(set)
    evidence: SelectionEvidence = {}
    for index, row in enumerate(opportunities):
        if not isinstance(row, Mapping):
            raise ValueError(f"structural opportunity {index} must be an object")
        task, kind = row.get("task"), row.get("kind")
        patch_sha, paths = row.get("patch_sha256"), row.get("matched_paths")
        retain = row.get("retain", False)
        if (
            not isinstance(task, str) or not task
            or kind not in _STRUCTURAL_FACT
            or not isinstance(patch_sha, str) or _SHA256.fullmatch(patch_sha) is None
            or not isinstance(paths, list) or not paths
            or any(not isinstance(path, str) or not path for path in paths)
            or not isinstance(retain, bool)
        ):
            raise ValueError(f"structural opportunity {index} is malformed")
        frozen_patch = patches_by_task.get(task)
        if frozen_patch is None:
            raise ValueError(f"structural opportunity {index} task absent from source")
        if hashlib.sha256(frozen_patch.encode("utf-8")).hexdigest() != patch_sha:
            raise ValueError(f"structural opportunity {index} patch hash mismatch")
        normalized_patch = frozen_patch.replace("\\", "/")
        normalized_paths = [str(path).replace("\\", "/") for path in paths]
        if any(path not in normalized_patch for path in normalized_paths):
            raise ValueError(f"structural opportunity {index} matched path absent from patch")
        if kind == "source_file_added" and not any(
            f"--- /dev/null\n+++ b/{path}" in normalized_patch
            for path in normalized_paths
        ):
            raise ValueError(
                f"structural opportunity {index} has no new-file diff for matched paths"
            )
        if kind == "callable_signature_changed" and not (
            set(normalized_paths) & _signature_delta_paths(frozen_patch)
        ):
            raise ValueError(
                f"structural opportunity {index} has no callable signature delta "
                "for matched paths"
            )
        feature = _STRUCTURAL_FACT[str(kind)]
        signals[feature].add(task)
        _add_evidence(evidence, feature, task, {
            "reason": f"frozen_patch_shape:{kind}",
            "source_path": str(source["path"]),
            "source_sha256": actual_source_sha,
            "patch_sha256": patch_sha,
            "matched_paths": list(paths),
            "retain_for_campaign": retain,
            "proof_scope": "selection_only_no_ss_promotion",
        })
    return dict(signals), evidence


def select_campaign(
    inventory: Mapping[str, tuple[str, ...]],
    tasks_by_feature: Mapping[str, set[str]],
    evidence: Mapping[tuple[str, str], list[dict[str, Any]]],
    *,
    locked_tasks: Iterable[str] = (),
) -> dict[str, Any]:
    """Keep locked tasks, then greedily add tasks covering unplanned opportunities."""
    features = tuple(
        name for family in ("ACQ", "CAP", "FACT", "PERF")
        for name in inventory[family]
    )
    retained = sorted({
        task for (_feature, task), records in evidence.items()
        if any(record.get("retain_for_campaign") is True for record in records)
    })
    selected = list(dict.fromkeys(
        [task for task in locked_tasks if task] + retained
    ))
    non_perf = tuple(
        name for family in ("ACQ", "CAP", "FACT") for name in inventory[family]
    )
    covered = {
        feature for feature in non_perf
        if set(selected).intersection(tasks_by_feature.get(feature, set()))
    }
    all_tasks = sorted({task for tasks in tasks_by_feature.values() for task in tasks})
    while True:
        gains = [(
            sum(
                feature not in covered and task in tasks_by_feature.get(feature, set())
                for feature in non_perf
            ),
            task,
        ) for task in all_tasks if task not in selected]
        gain, task = max(gains, default=(0, ""), key=lambda item: (item[0], item[1]))
        if gain <= 0:
            break
        selected.append(task)
        covered.update(
            feature for feature in non_perf
            if task in tasks_by_feature.get(feature, set())
        )

    family_of = {
        feature: family for family, names in inventory.items() for feature in names
    }
    rows: list[dict[str, Any]] = []
    for feature in features:
        family = family_of[feature]
        if family == "PERF":
            terminal_role = "measurement"
            role_specific_obligation = "live_run_measurement"
        elif family == "ACQ":
            terminal_role = "support"
            role_specific_obligation = "live_source_contribution_effect"
        elif family == "FACT":
            terminal_role = str(fact_role_for(feature) or "fact_delivery")
            role_specific_obligation = (
                "live_internal_support_effect"
                if terminal_role == "internal_support"
                else "live_delivery_acknowledgment_and_causality"
            )
        else:
            try:
                terminal_role = cap_role_for(feature)
            except ValueError:
                terminal_role = "byte_owner"
            role_specific_obligation = (
                "live_delivery_acknowledgment_and_causality"
                if terminal_role == "byte_owner"
                else "live_control_mediation_effect"
            )
        historical = sorted(tasks_by_feature.get(feature, set()))
        planned = [task for task in selected if task in historical]
        selection_evidence = [
            record
            for task in planned
            for record in evidence.get((feature, task), [])
        ]
        if family == "PERF" and selected:
            planned = list(selected)
            selection_evidence = [{
                "reason": "planned_run_aggregation_obligation",
                "proof_scope": "selection_only_live_measurement_required",
            }]
        rows.append({
            "feature_id": feature,
            "family": family,
            "terminal_live_proof_required": True,
            "terminal_role": terminal_role,
            "role_specific_obligation": role_specific_obligation,
            "historically_observed": bool(historical),
            "historical_opportunity_tasks": historical,
            "planned_opportunity_tasks": planned,
            "selection_evidence": selection_evidence,
        })
    unplanned = [row["feature_id"] for row in rows if not row["planned_opportunity_tasks"]]
    return {
        "schema": SCHEMA,
        "meaning": "campaign planning only; no row is SS-LIVE proof or promotion",
        "campaign_tasks": selected,
        "campaign_task_count": len(selected),
        "planned_opportunity_rows": len(rows) - len(unplanned),
        "unplanned_opportunity_rows": unplanned,
        "rows": rows,
    }


def _documents(root: Path) -> list[Mapping[str, Any]]:
    documents: list[Mapping[str, Any]] = []
    for path in sorted(root.rglob("gt_feature_metrics_*.json")):
        try:
            raw = path.read_bytes()
            value = json.loads(raw.decode("utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        if not isinstance(value, Mapping):
            continue
        document = dict(value)
        document["_selection_source"] = {
            "path": path.relative_to(root).as_posix(),
            "sha256": hashlib.sha256(raw).hexdigest(),
        }
        documents.append(document)
    return documents


def _locked_tasks(path: Path | None) -> list[str]:
    if path is None:
        return []
    return [
        line.strip() for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--metrics-root", type=Path, required=True)
    parser.add_argument("--locked-tasks", type=Path)
    parser.add_argument("--structural-opportunities", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    inventory = canonical_feature_inventory()
    signals, evidence = load_selection_signals(_documents(args.metrics_root), inventory)
    if args.structural_opportunities is not None:
        structural_signals, structural_evidence = load_structural_opportunities(
            args.structural_opportunities
        )
        for feature, tasks in structural_signals.items():
            signals.setdefault(feature, set()).update(tasks)
        for key, records in structural_evidence.items():
            for record in records:
                _add_evidence(evidence, key[0], key[1], record)
        owner_facts = {
            owner: tuple(sorted({
                binding.fact_class for binding in mechanism.bindings
                if binding.fact_class is not None
            }))
            for owner, mechanism in CAP_BYTE_OWNER_MECHANISMS.items()
        }
        signals, evidence = merge_selection_signals(
            signals, evidence, owner_facts=owner_facts
        )
    result = select_campaign(
        inventory, signals, evidence, locked_tasks=_locked_tasks(args.locked_tasks)
    )
    rendered = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if args.output is None:
        print(rendered, end="")
    else:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
