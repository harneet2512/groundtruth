#!/usr/bin/env python3
"""Per-task truth ledger — reconciles certs, runtime witness, deep metrics, outcome.

Writes ``task_truth.json`` beside DeepSWE outcome artifacts. Witness-over-cert
rules follow gt_gt.md §12 (GRAPH_FAIL_MISSING_HANDOFF reconciled when runtime
witness holds).
"""
from __future__ import annotations

import json
import os
import sys
from typing import Any

# deepswe_outcome is imported lazily in build_task_truth to avoid circular imports
# when tests load via importlib.

_SCRIPTS = os.path.dirname(__file__)
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)
_SRC = os.path.abspath(os.path.join(_SCRIPTS, "..", "..", "src"))
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from artifact_resolver import brief_provenance, resolve_trial_artifacts  # noqa: E402
from reconcile import reconcile_graph_handoff as reconcile_graph_handoff  # noqa: E402
from groundtruth.runtime.context_policy import POLICY_VERSION as PHASE_POLICY_VERSION  # noqa: E402
from groundtruth.runtime.obligations import OBLIGATION_VERSION  # noqa: E402
from groundtruth.runtime.trajectory_state import Turn, derive_phase, derive_state  # noqa: E402
from groundtruth.runtime.verification_horizon import HORIZON_VERSION  # noqa: E402

__all__ = ["build_task_truth", "write_task_truth", "reconcile_graph_handoff"]


def _load_json(path: str) -> dict | None:
    if not path or not os.path.isfile(path):
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def _find_trial_artifacts(jobs_dir: str, *, instance_id: str | None = None) -> dict[str, str | None]:
    """Locate per-trial paths under jobs_dir (P1-29 artifact_resolver)."""
    arts = resolve_trial_artifacts(
        jobs_dir, instance_id=instance_id, strict_task_match=bool(instance_id)
    )
    return arts.as_dict()


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


def _load_deepswe_outcome():
    import importlib.util

    path = os.path.join(os.path.dirname(__file__), "..", "verify", "deepswe_outcome.py")
    spec = importlib.util.spec_from_file_location("deepswe_outcome_tt", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _load_patch_hygiene():
    import importlib.util

    path = os.path.join(os.path.dirname(__file__), "patch_hygiene.py")
    spec = importlib.util.spec_from_file_location("patch_hygiene_tt", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _patch_hygiene_from_artifacts(artifacts: dict[str, str | None]) -> dict:
    """Classify model patch for task_truth (P1-28)."""
    patch = ""
    if artifacts.get("result_json"):
        d = _load_json(artifacts["result_json"]) or {}
        patch = str((d.get("info") or {}).get("submission") or "")
    if not patch and artifacts.get("mini_trajectory"):
        d = _load_json(artifacts["mini_trajectory"]) or {}
        patch = str((d.get("info") or {}).get("submission") or "")
    ph = _load_patch_hygiene()
    return ph.classify_patch(patch)


def _first_str(*values: Any) -> str:
    for value in values:
        if isinstance(value, str) and value:
            return value
        if isinstance(value, dict):
            for key in ("command", "cmd", "content", "text", "output", "observation"):
                v = value.get(key)
                if isinstance(v, str) and v:
                    return v
    return ""


def _turns_from_mini_trajectory(path: str | None) -> list[Turn]:
    data = _load_json(path or "") or {}
    raw = data.get("messages") or data.get("trajectory") or data.get("steps") or []
    if not isinstance(raw, list):
        return []
    turns: list[Turn] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        action = item.get("action") or item.get("tool_call") or {}
        result = item.get("result") or item.get("observation") or item.get("response") or {}
        command = _first_str(
            item.get("command"),
            item.get("cmd"),
            action,
            item.get("content") if item.get("role") == "assistant" else "",
        )
        observation = _first_str(
            item.get("observation"),
            item.get("output"),
            result,
            item.get("content") if item.get("role") != "assistant" else "",
        )
        full = _first_str(item.get("full_observation"), item.get("content"), observation)
        if command or observation or full:
            turns.append(Turn(command=command, observation=observation, full_observation=full))
    return turns


def _trajectory_state_summary(artifacts: dict[str, str | None]) -> dict[str, Any]:
    step_limit_raw = os.environ.get("GT_STEP_LIMIT")
    try:
        step_limit = int(step_limit_raw) if step_limit_raw else None
    except ValueError:
        step_limit = None
    turns = _turns_from_mini_trajectory(artifacts.get("mini_trajectory"))
    state = derive_state(turns, step_limit=step_limit)
    return {
        "phase_policy_version": PHASE_POLICY_VERSION,
        "turns_observed": len(turns),
        "phase": derive_phase(state).value,
        "action_count": state.action_count,
        "step_limit": state.step_limit,
        "budget_fraction": round(state.budget_fraction, 8),
        "viewed_files": sorted(state.viewed_files)[:20],
        "edited_files": sorted(state.edited_files)[:20],
        "source_edit_count": state.source_edit_count,
        "test_count": state.test_count,
        "test_evidence_seen": state.test_evidence_seen,
        "last_test_failed": state.last_test_failed,
        "delivered_markers": [
            {"turn": turn, "marker": marker, "reason": reason}
            for turn, marker, reason in state.delivered_markers[-20:]
        ],
    }


def _obligation_lifecycle_summary(obligation_status: dict | None) -> dict[str, Any]:
    data = obligation_status or {}
    records = data.get("obligations") or data.get("records") or data.get("snapshot") or []
    counts: dict[str, int] = {}
    if isinstance(records, list):
        for rec in records:
            if isinstance(rec, dict):
                status = str(rec.get("status") or "unknown")
                counts[status] = counts.get(status, 0) + 1
    return {
        "version": OBLIGATION_VERSION,
        "source_present": bool(data),
        "count": sum(counts.values()),
        "status_counts": counts,
    }


def _consumption_summary(deep: dict) -> dict[str, Any]:
    return {
        "delivered": deep.get("gt_blocks_delivered"),
        "consumed": deep.get("gt_blocks_consumed"),
        "verification_followup": deep.get("gt_blocks_verification_followup"),
        "hard_enforced": deep.get("gt_blocks_hard_enforced"),
        "enforced": deep.get("gt_blocks_enforced"),
        "semantics": deep.get("gt_enforcement_semantics"),
    }


def _verification_horizon_summary(deep: dict, verifier_semantics: dict) -> dict[str, Any]:
    semantics = deep.get("gt_enforcement_semantics") or {}
    return {
        "version": HORIZON_VERSION,
        "summary": deep.get("verification_horizon") or {},
        "pre_submit_intervention": True,
        "hard_enforced": bool(semantics.get("hard_enforced")) if isinstance(semantics, dict) else False,
        "official_verifier_repair": bool(verifier_semantics.get("official_verifier_repair")),
        "self_verifier_retry": bool(verifier_semantics.get("self_verifier_retry")),
    }


def _truth_authority_map() -> dict[str, str]:
    """Product-facing authority contract for the one-surface truth ledger."""
    return {
        "outcome": "task_truth.outcome",
        "substrate": "reconciled_substrate_verdict.json",
        "runtime_witness": "task_truth.runtime_witness",
        "brief_delivery": "task_truth.brief_provenance",
        "verifier_semantics": "task_truth.verifier_semantics",
        "obligations": "task_truth.obligation_status",
        "patch_hygiene": "task_truth.patch_hygiene",
        "trajectory_integrity": "task_truth.trajectory_integrity",
        "consumption": "gt_consumption_ledger.json via task_truth.deep_metrics",
    }


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

    artifacts = _find_trial_artifacts(jobs_dir, instance_id=instance_id)
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
    arts = resolve_trial_artifacts(
        jobs_dir, instance_id=iid, strict_task_match=bool(iid)
    )
    brief_prov = brief_provenance(arts)
    retry_n = int(os.environ.get("GT_RETRY_ON_VERIFIER_FAIL") or "0")
    verifier_semantics = {
        "self_verifier_retry": retry_n > 0,
        "official_verifier_repair": False,
        "retry_count_configured": retry_n,
        "note": (
            "self_verifier_retry = in-loop GT_RETRY harness (agent re-run with test feedback); "
            "official_verifier_repair = pier/SWE-bench post-submit verifier only"
        ),
    }

    obl_path = os.environ.get("GT_OBLIGATION_STATUS", "/tmp/gt/obligation_status.json")
    if not os.path.isfile(obl_path):
        cand = os.path.join(os.path.dirname(artifacts.get("trial_dir") or ""), "obligation_status.json")
        if cand and os.path.isfile(cand):
            obl_path = cand
    obligation_status = _load_json(obl_path) if obl_path else None

    if patch_hygiene is None:
        patch_hygiene = _patch_hygiene_from_artifacts(artifacts)

    trajectory_summary = _trajectory_state_summary(artifacts)
    obligation_summary = _obligation_lifecycle_summary(obligation_status)
    consumption_summary = _consumption_summary(deep)
    horizon_summary = _verification_horizon_summary(deep, verifier_semantics)

    return {
        "schema": "gt.task_truth.v1",
        "authority": _truth_authority_map(),
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
            "gt_blocks_verification_followup": deep.get(
                "gt_blocks_verification_followup"
            ),
            "gt_blocks_hard_enforced": deep.get("gt_blocks_hard_enforced"),
            "gt_blocks_enforced": deep.get("gt_blocks_enforced"),
            "gt_enforcement_semantics": deep.get("gt_enforcement_semantics"),
        },
        "trajectory_state": trajectory_summary,
        "runtime_control": {
            "phase_policy_version": PHASE_POLICY_VERSION,
            "trajectory_state_summary": trajectory_summary,
            "obligation_lifecycle_summary": obligation_summary,
            "verification_horizon_summary": horizon_summary,
            "consumption_summary": consumption_summary,
            "enforcement_semantics": {
                "pre_submit_intervention": horizon_summary["pre_submit_intervention"],
                "hard_enforced": horizon_summary["hard_enforced"],
                "official_verifier_repair": horizon_summary["official_verifier_repair"],
                "self_verifier_retry": horizon_summary["self_verifier_retry"],
            },
            "adapter_witness": {
                "gt_prebuilt_active": signal.get("gt_prebuilt_active"),
                "hook_hash_match": signal.get("hook_hash_match"),
                "gt_meta_present": signal.get("gt_meta_present"),
            },
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
        "brief_provenance": brief_prov,
        "verifier_semantics": verifier_semantics,
        "oracle_events_status": {
            "path": arts.oracle_events,
            "present": bool(arts.oracle_events and os.path.isfile(arts.oracle_events)),
        },
        "obligation_status": obligation_status or {},
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
    write_reconciled_substrate_verdict(truth, os.path.dirname(out_path))
    return out_path


def build_reconciled_substrate_verdict(truth: dict[str, Any]) -> dict[str, Any]:
    """Single dashboard-facing substrate verdict after witness reconciliation (P0-07)."""
    certs = truth.get("certs") or {}
    graph_cert = certs.get("graph_certificate.json") or {}
    lsp_cert = certs.get("lsp_certificate.json") or {}
    reconciled = truth.get("reconciled") or {}
    graph_handoff = reconciled.get("graph_handoff", "unproven")
    return {
        "schema": "gt.reconciled_substrate_verdict.v1",
        "instance_id": truth.get("instance_id"),
        "graph_handoff": graph_handoff,
        "witness_holds": reconciled.get("witness_holds"),
        "contradictions": reconciled.get("contradictions") or [],
        "graph_certificate_verdict": graph_cert.get("verdict"),
        "lsp_certificate_verdict": lsp_cert.get("verdict"),
        "outcome_failure_class": (truth.get("outcome") or {}).get("failure_class"),
        "in_resolved_denominator": (truth.get("outcome") or {}).get("in_resolved_denominator"),
        "authority": "task_truth.json",
        "authority_map": truth.get("authority") or _truth_authority_map(),
    }


def write_reconciled_substrate_verdict(
    truth: dict[str, Any], out_dir: str
) -> str:
    """Write reconciled_substrate_verdict.json beside task_truth."""
    path = os.path.join(out_dir, "reconciled_substrate_verdict.json")
    payload = build_reconciled_substrate_verdict(truth)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2)
        fh.write("\n")
    return path


def main(argv: list[str] | None = None) -> int:
    import sys

    args = (argv or sys.argv)[1:]
    jobs_dir = args[0] if args else "jobs"
    out_path = args[1] if len(args) > 1 else None
    if not os.path.isdir(jobs_dir):
        print(f"skip: jobs dir missing: {jobs_dir}", file=sys.stderr)
        return 0
    path = write_task_truth(jobs_dir, out_path=out_path)
    print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
