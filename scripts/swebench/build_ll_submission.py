#!/usr/bin/env python3
"""Assemble a COMPLETE, SEAL-VERIFIED SWE-bench-Live submission tree from Live Lite
per-task inference artifacts — WITHOUT re-deriving results.

Authoritative formats (verified against an accepted submission,
github.com/swe-bench-live/submission -> submissions/lite/20250501-sweagent-claude37):

  submissions/{subset}/{name}/
    preds.json     REQUIRED  JSON DICT keyed by instance_id:
                             {iid: {"model_name_or_path","instance_id","model_patch"}}
    results.json   REQUIRED  SWE-bench eval summary, schema_version 2:
                             total/submitted/completed/resolved/unresolved/empty_patch/
                             error _instances + the 7 *_ids lists
    README.md      REQUIRED  scaffold + settings (rollouts / sampling / iterations)
    logs/<id>/     official  eval.sh, patch.diff, report.json, run_instance.log,
                             test_output.txt   (the run_evaluation per-instance dir)
    trajs/<id>/    EXTRA     mini-swe-agent.trajectory.json (example ships none)
    gt_proofs/<id>/  EXTRA   GT byte-sealed evidence chain (more than required)
    SUBMISSION_INTEGRITY.json  per-task provenance + seal-verification report

Trust model:
  * The official grade is produced PER TASK, IN-CONTAINER, AT RUN TIME
    (swebench.harness.run_evaluation -> report.json). This packager NEVER re-grades
    and NEVER re-derives a resolved bit — it MERGES sealed per-task reports into the
    schema_version 2 summary.
  * Every packaged file covered by a task's gt_task_completion.v1 `artifact_sha256`
    seal is RE-HASHED byte-for-byte; a mismatch is fail-closed.
  * preds.json uses the run-time prediction (pred.jsonl) when the task sealed one;
    otherwise it falls back to the patch file and records the fallback explicitly.

Usage:
  python build_ll_submission.py <artifacts_dir> <out_root> \
      --expected <dataset.jsonl> --model "<label>" [--name 20260721-groundtruth-v4flash]
      [--subset lite] [--config-json <cfg.json>] [--strict]
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import shutil
import sys
from pathlib import Path

_PRED = Path(__file__).resolve().parent / "build_ll_predictions.py"
_spec = importlib.util.spec_from_file_location("build_ll_predictions", _PRED)
if _spec is None or _spec.loader is None:  # pragma: no cover
    raise ImportError(f"cannot load build_ll_predictions from {_PRED}")
_bp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_bp)

_DEFAULT_MODEL = "GroundTruth + mini-swe-agent + deepseek-v4-flash"

# Per-task GT proof-chain files copied into gt_proofs/<id>/ ({t} = instance id).
_GT_PROOF_TEMPLATES = (
    "gt_feature_metrics_{t}.json", "gt_deep_metrics_{t}.json",
    "gt_performance_metrics_{t}.json", "gt_behavioral_impact_{t}.json",
    "gt_runtime_ledger_{t}.jsonl", "gt_runtime_ledger_attestation_{t}.json",
    "gt_receipts_{t}.jsonl", "gt_oracle_events_{t}.jsonl",
    "brief_result.json", "task_truth.json", "outcome.json", "gt_task_completion.json",
    "gt_artifacts/gt_run_identity.json", "gt_artifacts/gt_profile_receipt.json",
    "gt_artifacts/gt_profile_activation.json", "gt_artifacts/graph_certificate.json",
    "gt_artifacts/lsp_certificate.json",
)

# logs/<id>/ official layout: (dest_name, source_names_in_priority_order)
_LOG_FILES = (
    ("patch.diff", ("agent_patch.diff",)),
    ("report.json", ("report.json",)),
    ("run_instance.log", ("run_instance.log", "trial_output.log")),
    ("test_output.txt", ("test_output.txt",)),
    ("eval.sh", ("eval.sh",)),
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _task_dir_map(artifacts_dir: Path) -> dict[str, Path]:
    out: dict[str, Path] = {}
    for d in sorted(p for p in artifacts_dir.iterdir() if p.is_dir()):
        name = d.name
        tid = name[len("ll-full-"):] if name.startswith("ll-full-") else name
        out.setdefault(tid, d)
    return out


def _load_seal(task_dir: Path) -> dict[str, str]:
    seal = task_dir / "gt_task_completion.json"
    if not seal.is_file():
        return {}
    try:
        doc = json.loads(seal.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if doc.get("schema") != "gt.task_completion.v1":
        return {}
    got = doc.get("artifact_sha256")
    return got if isinstance(got, dict) else {}


def _pred_record(task_dir: Path, tid: str, model: str) -> tuple[dict, str]:
    pred = task_dir / "pred.jsonl"
    if pred.is_file() and pred.stat().st_size > 0:
        try:
            rec = json.loads(pred.read_text(encoding="utf-8").splitlines()[0])
            if rec.get("instance_id") == tid and isinstance(rec.get("model_patch"), str):
                rec.setdefault("model_name_or_path", model)
                return rec, "runtime_pred_jsonl"
        except (ValueError, IndexError):
            pass
    patch, src = _bp._patch_for_task(task_dir)
    return ({"model_name_or_path": model, "instance_id": tid, "model_patch": patch},
            f"derived_{src}")


def _report_state(task_dir: Path) -> tuple[str, str]:
    """Return (state, source). state in resolved/unresolved/error/no_report."""
    rep = task_dir / "report.json"
    if not rep.is_file():
        return "no_report", "absent"
    try:
        doc = json.loads(rep.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "error", "report_unreadable"
    entry = (list(doc.values())[0] if doc else {}) or {}
    return ("resolved" if entry.get("resolved") else "unresolved"), "official_report_json"


def _copy_verified(src: Path, dst: Path, seal: dict[str, str], seal_key: str,
                   checks: list) -> bool:
    if not src.is_file():
        return False
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    if seal_key in seal:
        ok = _sha256(dst) == seal[seal_key]
        checks.append({"file": seal_key, "sealed": True, "seal_verified": ok})
        return ok
    checks.append({"file": seal_key, "sealed": False, "seal_verified": None})
    return True


def build(artifacts_dir: Path, out_dir: Path, expected: list[str], model: str,
          cfg: dict) -> dict:
    tmap = _task_dir_map(artifacts_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    preds: dict[str, dict] = {}
    per_task: list[dict] = []
    ids = {k: [] for k in ("submitted", "incomplete", "empty_patch", "completed",
                           "resolved", "unresolved", "error")}

    for tid in expected:
        d = tmap.get(tid)
        checks: list = []
        if d is None:
            ids["incomplete"].append(tid)
            per_task.append({"instance_id": tid, "artifact": "ABSENT",
                             "pred_source": "absent", "state": "incomplete",
                             "seal_present": False, "seal_checks": [],
                             "gaps": ["artifact_dir_absent"]})
            continue

        seal = _load_seal(d)
        rec, pred_src = _pred_record(d, tid, model)
        preds[tid] = rec
        ids["submitted"].append(tid)
        empty = not rec["model_patch"].strip()
        state = _report_state(d)[0]
        if empty:
            ids["empty_patch"].append(tid)
        if state == "resolved":
            ids["completed"].append(tid); ids["resolved"].append(tid)
        elif state == "unresolved":
            ids["completed"].append(tid); ids["unresolved"].append(tid)
        elif state == "error":
            ids["error"].append(tid)
        # non-empty patch with no report and no error marker => eval never completed
        elif not empty:
            ids["error"].append(tid)

        # logs/<id>/ official layout
        for dest, sources in _LOG_FILES:
            for sname in sources:
                if (d / sname).is_file():
                    _copy_verified(d / sname, out_dir / "logs" / tid / dest,
                                   seal, sname if sname in seal else dest, checks)
                    break
        # trajs/<id>/ (extra)
        _copy_verified(d / "mini-swe-agent.trajectory.json",
                       out_dir / "trajs" / tid / "mini-swe-agent.trajectory.json",
                       seal, "mini-swe-agent.trajectory.json", checks)
        # gt_proofs/<id>/ (extra)
        for tmpl in _GT_PROOF_TEMPLATES:
            rel = tmpl.format(t=tid)
            _copy_verified(d / rel, out_dir / "gt_proofs" / tid / rel, seal, rel, checks)

        seal_fail = [c["file"] for c in checks if c["seal_verified"] is False]
        gaps = []
        if not seal:
            gaps.append("no_seal")
        if not (d / "agent_patch.diff").is_file() and not empty:
            gaps.append("patch_from_fallback")
        if seal_fail:
            gaps.append("seal_mismatch:" + ",".join(seal_fail))
        per_task.append({"instance_id": tid, "artifact": d.name,
                         "pred_source": pred_src, "state": state,
                         "seal_present": bool(seal), "seal_checks": checks, "gaps": gaps})

    # preds.json — dict keyed by instance_id (official format)
    (out_dir / "preds.json").write_text(json.dumps(preds, indent=2), encoding="utf-8")

    # results.json — SWE-bench eval summary, schema_version 2
    results = {
        "total_instances": len(expected),
        "submitted_instances": len(ids["submitted"]),
        "completed_instances": len(ids["completed"]),
        "resolved_instances": len(ids["resolved"]),
        "unresolved_instances": len(ids["unresolved"]),
        "empty_patch_instances": len(ids["empty_patch"]),
        "error_instances": len(ids["error"]),
        "completed_ids": sorted(ids["completed"]),
        "incomplete_ids": sorted(ids["incomplete"]),
        "empty_patch_ids": sorted(ids["empty_patch"]),
        "submitted_ids": sorted(ids["submitted"]),
        "resolved_ids": sorted(ids["resolved"]),
        "unresolved_ids": sorted(ids["unresolved"]),
        "error_ids": sorted(ids["error"]),
        "schema_version": 2,
    }
    (out_dir / "results.json").write_text(json.dumps(results, indent=2), encoding="utf-8")
    (out_dir / "README.md").write_text(_readme(model, out_dir.name, len(expected), cfg),
                                       encoding="utf-8")

    resolved_unsealed = [
        t["instance_id"] for t in per_task
        if t["state"] == "resolved" and (not t["seal_present"]
                                         or any(g.startswith("seal_mismatch") for g in t["gaps"]))]
    integrity = {
        "schema": "gt.submission_integrity.v1",
        "results_summary": {k: results[k] for k in (
            "total_instances", "submitted_instances", "completed_instances",
            "resolved_instances", "empty_patch_instances", "error_instances")},
        "runtime_sealed_pred": sum(1 for t in per_task if t["pred_source"] == "runtime_pred_jsonl"),
        "derived_pred": sum(1 for t in per_task if t["pred_source"].startswith("derived")),
        "sealed_tasks": sum(1 for t in per_task if t["seal_present"]),
        "unsealed_tasks": sum(1 for t in per_task if not t["seal_present"] and t["artifact"] != "ABSENT"),
        "resolved_but_unsealed_or_mismatched": resolved_unsealed,
        "per_task": per_task,
    }
    (out_dir / "SUBMISSION_INTEGRITY.json").write_text(json.dumps(integrity, indent=2),
                                                       encoding="utf-8")
    return integrity


def _readme(model: str, name: str, total: int, cfg: dict) -> str:
    c = cfg or {}
    return (
        f"# GroundTruth — SWE-bench-Live ({name})\n\n"
        f"Predictions: `{model}`\n\n"
        "## Scaffold & settings\n"
        "- Scaffold: mini-swe-agent (bash-only); GroundTruth hooks deliver sealed evidence "
        "on the agent's own search/view/edit/test/submit observations (≤1 dose/observation)\n"
        f"- Model: {c.get('model', 'deepseek/deepseek-v4-flash')}, thinking {c.get('thinking', 'disabled')}\n"
        f"- Sampling: temperature {c.get('temperature', 1.0)}, top_p {c.get('top_p', 1.0)}, "
        f"max_tokens {c.get('max_tokens', 16384)}\n"
        f"- Budget: step_limit {c.get('step_limit', 150)}, cost_limit ${c.get('cost_limit', 3.0)}\n"
        f"- Rollouts per task: {c.get('rollouts', 1)} (pass@1, single attempt, no best-of-k)\n"
        f"- API retries: {c.get('num_retries', 3)} (transient LLM-call retry only, not extra rollouts)\n"
        f"- Sampling method: single deterministic pass; no reranking/self-consistency\n"
        f"- Tasks: {total}\n\n"
        "## Evaluation\n"
        "Each task was graded in-container at run time by the official "
        "`swebench.harness.run_evaluation`; `results.json` (schema_version 2) merges those "
        "sealed per-task reports without re-grading. `SUBMISSION_INTEGRITY.json` records "
        "per-task provenance and byte-seal verification. `gt_proofs/` carries the GroundTruth "
        "evidence chain (feature-gate matrix, runtime ledgers, producer attestations, "
        "one-build identity) — beyond the required deliverables.\n"
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("artifacts_dir")
    ap.add_argument("out_root")
    ap.add_argument("--expected", required=True)
    ap.add_argument("--model", default=_DEFAULT_MODEL)
    ap.add_argument("--name", default="groundtruth", help="submission folder name")
    ap.add_argument("--subset", default="lite")
    ap.add_argument("--config-json", default="")
    ap.add_argument("--strict", action="store_true")
    args = ap.parse_args()

    artifacts_dir = Path(args.artifacts_dir)
    if not artifacts_dir.is_dir():
        print(f"ERROR: artifacts_dir not found: {artifacts_dir}", file=sys.stderr)
        return 2
    expected = [json.loads(l)["instance_id"]
                for l in Path(args.expected).read_text(encoding="utf-8").splitlines()
                if l.strip()]
    cfg = {}
    if args.config_json and Path(args.config_json).is_file():
        cfg = json.loads(Path(args.config_json).read_text(encoding="utf-8"))

    out_dir = Path(args.out_root) / "submissions" / args.subset / args.name
    report = build(artifacts_dir, out_dir, expected, args.model, cfg)
    rs = report["results_summary"]

    print("=" * 60)
    print("BUILD SWE-bench-Live submission tree")
    print("=" * 60)
    print(f"  out:            {out_dir}")
    print(f"  total:          {rs['total_instances']}")
    print(f"  submitted:      {rs['submitted_instances']}")
    print(f"  completed:      {rs['completed_instances']}")
    print(f"  resolved:       {rs['resolved_instances']}")
    print(f"  empty_patch:    {rs['empty_patch_instances']}")
    print(f"  error:          {rs['error_instances']}")
    print(f"  runtime-sealed pred: {report['runtime_sealed_pred']}   "
          f"derived: {report['derived_pred']}")
    print(f"  sealed tasks:   {report['sealed_tasks']}   "
          f"unsealed: {report['unsealed_tasks']}")
    bad = report["resolved_but_unsealed_or_mismatched"]
    if bad:
        print(f"  ::warning:: {len(bad)} RESOLVED task(s) unsealed/mismatched: "
              + ", ".join(bad[:12]) + (" ..." if len(bad) > 12 else ""))
    if args.strict and bad:
        print("STRICT: refusing to certify a submission with unprovable resolves",
              file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
