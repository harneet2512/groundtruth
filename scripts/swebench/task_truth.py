#!/usr/bin/env python3
"""Per-task truth ledger — reconciles certs, runtime witness, deep metrics, outcome.

Writes ``task_truth.json`` beside DeepSWE outcome artifacts. Witness-over-cert
rules follow gt_gt.md §12 (GRAPH_FAIL_MISSING_HANDOFF reconciled when runtime
witness holds).
"""
from __future__ import annotations

import glob
import json
import os
from typing import Any

# deepswe_outcome is imported lazily in build_task_truth to avoid circular imports
# when tests load via importlib.


def _load_json(path: str) -> dict | None:
    if not path or not os.path.isfile(path):
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def _find_trial_artifacts(jobs_dir: str) -> dict[str, str | None]:
    """Locate per-trial paths under jobs_dir (best-effort)."""
    trials = sorted(glob.glob(os.path.join(jobs_dir, "*", "*__*", "result.json")))
    trial_dir = os.path.dirname(trials[-1]) if trials else None
    out: dict[str, str | None] = {
        "trial_dir": trial_dir,
        "result_json": trials[-1] if trials else None,
        "mini_trajectory": None,
        "canonical_trajectory": None,
        "deep_metrics": None,
        "outcome_json": os.environ.get("GT_DEEPSWE_OUTCOME_JSON"),
    }
    if trial_dir:
        for name in ("mini-swe-agent.trajectory.json", "trajectory.json"):
            p = os.path.join(trial_dir, "agent", name)
            if os.path.isfile(p):
                if name.startswith("mini"):
                    out["mini_trajectory"] = p
                else:
                    out["canonical_trajectory"] = p
    # Deep metrics beside trial or /tmp
    for pat in (
        os.path.join(jobs_dir, "**", "gt_deep_metrics_*.json"),
        "/tmp/gt_deep_metrics_*.json",
    ):
        hits = sorted(glob.glob(pat, recursive="**" in pat))
        if hits:
            out["deep_metrics"] = hits[-1]
            break
    return out


def _trajectory_integrity(artifacts: dict[str, str | None]) -> dict[str, Any]:
    canon = artifacts.get("canonical_trajectory")
    mini = artifacts.get("mini_trajectory")
    canon_bytes = os.path.getsize(canon) if canon and os.path.isfile(canon) else None
    mini_bytes = os.path.getsize(mini) if mini and os.path.isfile(mini) else None
    return {
        "canonical_path": canon,
        "canonical_bytes": canon_bytes,
        "mini_path": mini,
        "mini_bytes": mini_bytes,
        "mini_fallback": bool(canon_bytes == 0 and mini_bytes and mini_bytes > 0),
    }


def reconcile_graph_handoff(signal: dict) -> dict[str, Any]:
    """§12 witness-over-cert reconciliation for graph handoff."""
    witness_holds = (
        signal.get("gt_prebuilt_active") is True
        and signal.get("hook_hash_match") is True
    )
    cert_verdicts = signal.get("cert_verdicts") or {}
    graph_cert = cert_verdicts.get("graph_certificate.json") or {}
    graph_verdict = graph_cert.get("verdict") or ""
    contradictions: list[str] = []

    if graph_cert.get("is_fail") and graph_verdict == "GRAPH_FAIL_MISSING_HANDOFF":
        if witness_holds:
            status = "witness_overrides"
        else:
            status = "fail"
            contradictions.append("GRAPH_FAIL_MISSING_HANDOFF without runtime witness")
    elif graph_cert.get("is_fail"):
        status = "fail"
        contradictions.append(f"graph cert fail: {graph_verdict}")
    elif witness_holds:
        status = "pass"
    elif signal.get("gt_prebuilt_active") is False:
        status = "fail"
        contradictions.append("gt_prebuilt_active=false")
    elif not signal.get("gt_meta_present"):
        status = "unproven"
        contradictions.append("no [GT_META] witness")
    else:
        status = "pass"

    return {
        "graph_handoff": status,
        "witness_holds": witness_holds,
        "contradictions": contradictions,
    }


def _load_deepswe_outcome():
    import importlib.util

    path = os.path.join(os.path.dirname(__file__), "..", "verify", "deepswe_outcome.py")
    spec = importlib.util.spec_from_file_location("deepswe_outcome_tt", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def build_task_truth(
    jobs_dir: str,
    *,
    instance_id: str | None = None,
    trial_log: str = "",
    cert_dir: str | None = None,
    patch_hygiene: dict | None = None,
) -> dict:
    """Assemble reconciled per-task truth record."""
    do = _load_deepswe_outcome()

    artifacts = _find_trial_artifacts(jobs_dir)
    reward: float | None = None
    n_agent_steps: int | None = None
    exit_status: str | None = None
    iid = instance_id

    if artifacts.get("result_json"):
        d = _load_json(artifacts["result_json"]) or {}
        info = d.get("info") or {}
        vr = d.get("verifier_result") or {}
        reward = (vr.get("rewards") or {}).get("reward")
        n_agent_steps = d.get("n_agent_steps")
        exit_status = info.get("exit_status")
        if not iid:
            iid = do.extract_instance_id(d, info, trial_dir=artifacts.get("trial_dir"))

    if not trial_log:
        log_path = os.environ.get("GT_TRIAL_LOG")
        if log_path and os.path.isfile(log_path):
            trial_log = do._read_trial_log(log_path)  # noqa: SLF001

    if not cert_dir:
        cert_dir = os.environ.get("GT_CERT_DIR")
        if not cert_dir or not os.path.isdir(cert_dir):
            cert_dir = "/tmp/gt" if os.path.isdir("/tmp/gt") else None

    eval_no_report = do._detect_eval_no_report(jobs_dir)  # noqa: SLF001
    infra_subtype = do.detect_infra_subtype(jobs_dir, trial_log)

    signal = do.build_signal_record(
        instance_id=iid,
        reward=reward,
        n_agent_steps=n_agent_steps,
        exit_status=exit_status,
        trial_log=trial_log,
        cert_dir=cert_dir,
        eval_no_report=eval_no_report,
        infra_subtype=infra_subtype,
    )

    deep = _load_json(artifacts.get("deep_metrics") or "") or {}
    traj_int = _trajectory_integrity(artifacts)
    reconciled = reconcile_graph_handoff(signal)

    return {
        "schema": "gt.task_truth.v1",
        "instance_id": iid,
        "certs": signal.get("cert_verdicts") or {},
        "runtime_witness": {
            "gt_prebuilt_active": signal.get("gt_prebuilt_active"),
            "hook_hash_match": signal.get("hook_hash_match"),
            "gt_meta_present": signal.get("gt_meta_present"),
            "cert_fail_reconciled": signal.get("cert_fail_reconciled"),
        },
        "deep_metrics": {
            "path": artifacts.get("deep_metrics"),
            "outcome": deep.get("outcome"),
            "resolved": deep.get("resolved"),
            "gt_delivery": deep.get("gt_delivery"),
            "gt_blocks_delivered": deep.get("gt_blocks_delivered"),
            "gt_blocks_consumed": deep.get("gt_blocks_consumed"),
            "gt_blocks_enforced": deep.get("gt_blocks_enforced"),
        },
        "outcome": {
            "reward": signal.get("reward"),
            "resolved": signal.get("failure_class") == "RESOLVED",
            "failure_class": signal.get("failure_class"),
            "infra_subtype": signal.get("infra_subtype"),
            "in_resolved_denominator": signal.get("in_resolved_denominator"),
        },
        "trajectory_integrity": traj_int,
        "patch_hygiene": patch_hygiene or {},
        "reconciled": reconciled,
        "signals": signal,
    }


def write_task_truth(jobs_dir: str, out_path: str | None = None, **kwargs: Any) -> str:
    """Build and persist task_truth.json; return output path."""
    truth = build_task_truth(jobs_dir, **kwargs)
    if not out_path:
        trial_dir = _find_trial_artifacts(jobs_dir).get("trial_dir")
        out_path = os.path.join(trial_dir or jobs_dir, "task_truth.json")
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(truth, fh, indent=2)
    return out_path
