"""run_contract.py — orchestration determinism (host-side).

Builds run_contract.json from the dispatch metadata + the RESOLVED gt SHA the
workflow records, and hard-fails when the GHA pipeline cannot be proven to have
run the intended workflow/code/task/flags. Read-only.
"""
from __future__ import annotations

import hashlib
import json
import os


def _sha256(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8", "ignore")).hexdigest()


def build_run_contract(fields: dict) -> dict:
    """fields (whatever the workflow can supply):
      run_id, workflow_ref_requested, default_branch, gt_commit_input,
      resolved_gt_sha, expected_gt_sha, gt_use_substrate_image, task_ids_input,
      task_ids_parsed (list), matrix_tasks (list), num_tasks, worker_count,
      cache_mode, host_path_enabled (bool), container_path_enabled (bool),
      steps_skipped (list of {step, reason}).
    """
    c = dict(fields)
    c["contract"] = "run"
    ti_parsed = fields.get("task_ids_parsed") or []
    c["task_ids_count"] = len(ti_parsed)
    c["task_ids_sha256"] = _sha256("\n".join(sorted(ti_parsed))) if ti_parsed else ""
    c["matrix_task_count"] = len(fields.get("matrix_tasks") or [])

    hf: list[str] = []
    if not fields.get("gt_commit_input"):
        hf.append("gt_commit_input_missing")
    if not fields.get("resolved_gt_sha"):
        hf.append("resolved_sha_missing")  # branch used without recording the SHA
    exp = fields.get("expected_gt_sha")
    if exp and fields.get("resolved_gt_sha") and exp != fields["resolved_gt_sha"]:
        hf.append("resolved_sha_mismatch")
    if str(fields.get("gt_use_substrate_image", "")) not in ("true", "false"):
        hf.append("gt_use_substrate_image_unregistered")
    # container requested but host path ran (or both)
    if str(fields.get("gt_use_substrate_image")) == "true" and fields.get("host_path_enabled"):
        hf.append("host_path_ran_under_container_mode")
    # task_ids requested but not honored by the matrix
    if ti_parsed and fields.get("matrix_tasks") is not None:
        if set(ti_parsed) != set(fields["matrix_tasks"]):
            hf.append("task_ids_ignored_or_replaced_by_slicing")
    # a later GT step skipped because an earlier (non-GT) step failed
    for s in (fields.get("steps_skipped") or []):
        if isinstance(s, dict) and "earlier" in str(s.get("reason", "")).lower():
            hf.append("gt_step_skipped_due_to_earlier_failure")
            break
    c["hard_fail"] = hf
    return c


def build_run_contract_from_env() -> dict:
    """Best-effort build from GitHub Actions env (when the workflow doesn't pass a
    structured dict). Fields the workflow exports take precedence."""
    env = os.environ
    parsed = [x for x in (env.get("GT_TASK_IDS_PARSED", "").replace(",", "\n").split("\n")) if x.strip()]
    fields = {
        "run_id": env.get("GITHUB_RUN_ID", ""),
        "workflow_ref_requested": env.get("GITHUB_REF", ""),
        "default_branch": env.get("GITHUB_DEFAULT_BRANCH", ""),
        "gt_commit_input": env.get("GT_COMMIT_INPUT", ""),
        "resolved_gt_sha": env.get("GT_RESOLVED_SHA", ""),
        "gt_use_substrate_image": env.get("GT_USE_SUBSTRATE_IMAGE", ""),
        "task_ids_input": env.get("GT_TASK_IDS_INPUT", ""),
        "task_ids_parsed": parsed,
    }
    return build_run_contract(fields)


if __name__ == "__main__":
    print(json.dumps(build_run_contract_from_env(), indent=2, default=str))
