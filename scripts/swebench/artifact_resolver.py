#!/usr/bin/env python3
"""Central artifact path resolution for DeepSWE trial dirs (P1-29)."""
from __future__ import annotations

import glob
import hashlib
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
    delivered_instruction: str | None
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
            "delivered_instruction": self.delivered_instruction,
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

    delivered = None
    brief = None
    for base in (trial_dir, os.path.dirname(jobs_dir) if jobs_dir else None, "/tmp/gt"):
        if not base:
            continue
        d = os.path.join(base, "delivered_instruction.txt")
        if os.path.isfile(d):
            delivered = d
        b = os.path.join(base, "gt_artifacts", "brief.txt")
        if os.path.isfile(b):
            brief = b
        if delivered and brief:
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
        delivered_instruction=delivered,
        brief_txt=brief,
    )


def brief_provenance(artifacts: TrialArtifacts) -> dict[str, Any]:
    """P1-09 / P2-13 — substrate vs delivered brief hashes."""
    return {
        "substrate_brief_sha256": _sha256_file(artifacts.brief_txt),
        "delivered_instruction_sha256": _sha256_file(artifacts.delivered_instruction),
        "brief_match": (
            _sha256_file(artifacts.brief_txt) == _sha256_file(artifacts.delivered_instruction)
            if artifacts.brief_txt and artifacts.delivered_instruction
            else None
        ),
    }
