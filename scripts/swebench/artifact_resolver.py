#!/usr/bin/env python3
"""Central artifact path resolution for DeepSWE trial dirs (P1-29)."""
from __future__ import annotations

import glob
import hashlib
import importlib.util
import json
import os
from dataclasses import dataclass
from typing import Any


@dataclass
class TrialArtifacts:
    trial_dir: str | None
    result_json: str | None
    mini_trajectory: str | None
    canonical_trajectory: str | None
    deep_metrics: str | None
    task_truth: str | None
    outcome_json: str | None
    oracle_events: str | None
    runtime_ledger: str | None
    delivered_instruction: str | None
    adapter_witness: str | None
    brief_txt: str | None

    def as_dict(self) -> dict[str, str | None]:
        return {
            "trial_dir": self.trial_dir,
            "result_json": self.result_json,
            "mini_trajectory": self.mini_trajectory,
            "canonical_trajectory": self.canonical_trajectory,
            "deep_metrics": self.deep_metrics,
            "task_truth": self.task_truth,
            "outcome_json": self.outcome_json,
            "oracle_events": self.oracle_events,
            "runtime_ledger": self.runtime_ledger,
            "delivered_instruction": self.delivered_instruction,
            "adapter_witness": self.adapter_witness,
            "brief_txt": self.brief_txt,
        }


def _sha256_file(path: str | None) -> str | None:
    if not path or not os.path.isfile(path):
        return None
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _read_text(path: str | None) -> str | None:
    if not path or not os.path.isfile(path):
        return None
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return None


def _sha256_text(text: str | None) -> str | None:
    if text is None:
        return None
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _extract_gt_task_brief(text: str | None) -> str | None:
    """Return the delivered <gt-task-brief> block, if present."""
    if not text:
        return None
    start = text.find("<gt-task-brief")
    if start < 0:
        return None
    end_tag = "</gt-task-brief>"
    end = text.find(end_tag, start)
    if end < 0:
        return None
    return text[start:end + len(end_tag)]


def _sealed_brief_entry(artifacts: TrialArtifacts) -> dict[str, Any] | None:
    """Return one exact trajectory/ledger brief join, never a tag-only guess."""
    if not artifacts.mini_trajectory or not artifacts.runtime_ledger:
        return None
    brief_rows = 0
    try:
        with open(artifacts.runtime_ledger, encoding="utf-8") as stream:
            for line in stream:
                row = json.loads(line)
                if (
                    isinstance(row, dict)
                    and row.get("outcome") == "delivered"
                    and row.get("layer") == "brief.task"
                    and int(row.get("chars_delivered") or 0) > 0
                ):
                    brief_rows += 1
    except (OSError, TypeError, ValueError):
        return None
    if brief_rows != 1:
        return None
    module_path = os.path.join(os.path.dirname(__file__), "consumption_ledger.py")
    spec = importlib.util.spec_from_file_location(
        "artifact_resolver_consumption_ledger", module_path,
    )
    if not spec or not spec.loader:
        return None
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
        ledger = module.ledger_from_trajectory_path(
            artifacts.mini_trajectory,
            runtime_ledger_path=artifacts.runtime_ledger,
        )
    except (OSError, TypeError, ValueError):
        return None
    if (
        ledger.get("visible_audit_complete") is not True
        or int(ledger.get("exact_seal_ambiguity_count") or 0) != 0
        or int(ledger.get("physical_identity_conflict_count") or 0) != 0
    ):
        return None
    entries = [
        entry for entry in ledger.get("entries", [])
        if isinstance(entry, dict)
        and entry.get("ledger_layer") == "brief.task"
        and entry.get("joined") is True
        and entry.get("source") == "trajectory"
        and entry.get("join_method") == "seal"
        and isinstance(entry.get("rendered_text"), str)
    ]
    return entries[0] if len(entries) == 1 else None


def resolve_trial_artifacts(
    jobs_dir: str,
    *,
    instance_id: str | None = None,
    strict_task_match: bool = False,
) -> TrialArtifacts:
    """Locate per-trial artifacts under jobs_dir."""
    pattern = os.path.join(jobs_dir, "*", "*__*", "result.json")
    trials = sorted(glob.glob(pattern))
    if instance_id and strict_task_match:
        needle = f"{instance_id}__"
        trials = [t for t in trials if needle in os.path.basename(os.path.dirname(t))]
    trial_dir = os.path.dirname(trials[-1]) if trials else None
    result_json = trials[-1] if trials else None

    mini_traj = canon_traj = None
    if trial_dir:
        for name in ("mini-swe-agent.trajectory.json", "trajectory.json"):
            p = os.path.join(trial_dir, "agent", name)
            if os.path.isfile(p):
                if name.startswith("mini"):
                    mini_traj = p
                else:
                    canon_traj = p
    if not mini_traj:
        candidate = os.path.join(jobs_dir, "mini-swe-agent.trajectory.json")
        if os.path.isfile(candidate):
            mini_traj = candidate

    deep_metrics = None
    for pat in (
        os.path.join(jobs_dir, "**", "gt_deep_metrics_*.json"),
        "/tmp/gt_deep_metrics_*.json",
    ):
        hits = sorted(glob.glob(pat, recursive="**" in pat))
        if hits:
            deep_metrics = hits[-1]
            break

    task_truth = os.path.join(trial_dir, "task_truth.json") if trial_dir else None
    if task_truth and not os.path.isfile(task_truth):
        task_truth = None

    outcome = os.environ.get("GT_DEEPSWE_OUTCOME_JSON")
    if not outcome and trial_dir:
        cand = os.path.join(os.path.dirname(jobs_dir), "outcome.json")
        outcome = cand if os.path.isfile(cand) else None

    oracle_events = os.environ.get("GT_ORACLE_EVENTS")
    if not oracle_events and trial_dir:
        parent = os.path.dirname(jobs_dir)
        for name in (f"gt_oracle_events_{instance_id}.jsonl", "gt_oracle_events.jsonl"):
            cand = os.path.join(parent, name)
            if os.path.isfile(cand):
                oracle_events = cand
                break

    runtime_ledger = os.environ.get("GT_RUNTIME_LEDGER")
    if runtime_ledger and not os.path.isfile(runtime_ledger):
        runtime_ledger = None
    if not runtime_ledger:
        parent = os.path.dirname(jobs_dir)
        for base in (trial_dir, jobs_dir, parent, "/tmp/gt"):
            if not base:
                continue
            for name in ("gt_runtime_ledger.jsonl", f"gt_runtime_ledger_{instance_id}.jsonl"):
                cand = os.path.join(base, name)
                if os.path.isfile(cand):
                    runtime_ledger = cand
                    break
            if runtime_ledger:
                break

    delivered = None
    adapter_witness = None
    brief = None
    for base in (
        trial_dir, jobs_dir, os.path.dirname(jobs_dir) if jobs_dir else None, "/tmp/gt",
    ):
        if not base:
            continue
        d = os.path.join(base, "delivered_instruction.txt")
        if os.path.isfile(d):
            delivered = d
        aw = os.path.join(base, "adapter_witness.json")
        if os.path.isfile(aw):
            adapter_witness = aw
        b = os.path.join(base, "gt_artifacts", "brief.txt")
        if os.path.isfile(b):
            brief = b
        if delivered and brief and adapter_witness:
            break

    return TrialArtifacts(
        trial_dir=trial_dir,
        result_json=result_json,
        mini_trajectory=mini_traj,
        canonical_trajectory=canon_traj,
        deep_metrics=deep_metrics,
        task_truth=task_truth,
        outcome_json=outcome,
        oracle_events=oracle_events,
        runtime_ledger=runtime_ledger,
        delivered_instruction=delivered,
        adapter_witness=adapter_witness,
        brief_txt=brief,
    )


def brief_provenance(artifacts: TrialArtifacts) -> dict[str, Any]:
    """P1-09 / P2-13 - substrate brief survives inside the agent prompt."""
    brief_text = _read_text(artifacts.brief_txt)
    delivered_text = _read_text(artifacts.delivered_instruction)
    delivered_block = _extract_gt_task_brief(delivered_text)
    sealed_entry = None if delivered_block is not None else _sealed_brief_entry(artifacts)
    if sealed_entry is not None:
        delivered_block = sealed_entry["rendered_text"]
    substrate_hash = _sha256_text(brief_text)
    delivered_block_hash = _sha256_text(delivered_block)
    delivered_container = delivered_text if delivered_text is not None else delivered_block
    delivered_contains = (
        bool(brief_text and delivered_container and brief_text in delivered_container)
        if brief_text is not None and delivered_container is not None
        else None
    )
    if substrate_hash and delivered_block_hash:
        brief_match = substrate_hash == delivered_block_hash
    elif delivered_contains is not None:
        brief_match = delivered_contains
    else:
        brief_match = None
    return {
        "substrate_brief_sha256": substrate_hash,
        "delivered_instruction_sha256": _sha256_file(artifacts.delivered_instruction),
        "delivered_brief_block_sha256": delivered_block_hash,
        "delivered_contains_substrate_brief": delivered_contains,
        "brief_match": brief_match,
        "match_semantics": "delivered_brief_block_or_exact_seal_or_containment",
        "delivery_join_method": (
            sealed_entry.get("join_method") if sealed_entry is not None else None
        ),
        "delivery_message_index": (
            sealed_entry.get("msg_index") if sealed_entry is not None else None
        ),
    }
