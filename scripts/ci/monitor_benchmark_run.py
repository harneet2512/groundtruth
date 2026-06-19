#!/usr/bin/env python3
"""Poll a GitHub Actions benchmark run and summarize outcomes from artifacts."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


def run(cmd: list[str]) -> str:
    return subprocess.check_output(cmd, text=True, stderr=subprocess.STDOUT)


def gh_json(args: list[str]) -> Any:
    return json.loads(run(["gh", *args]))


def list_artifacts(repo: str, run_id: str) -> list[dict[str, Any]]:
    pages = gh_json(
        [
            "api",
            f"repos/{repo}/actions/runs/{run_id}/artifacts?per_page=100",
            "--paginate",
            "--slurp",
        ]
    )
    artifacts: list[dict[str, Any]] = []
    for page in pages:
        artifacts.extend(page.get("artifacts") or [])
    return artifacts


def download_new_artifacts(repo: str, run_id: str, artifacts: list[dict[str, Any]], cache: Path) -> None:
    cache.mkdir(parents=True, exist_ok=True)
    for artifact in artifacts:
        name = artifact.get("name") or ""
        if not name:
            continue
        dst = cache / name
        marker = dst / ".downloaded"
        if marker.exists():
            continue
        dst.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ["gh", "run", "download", run_id, "--repo", repo, "--name", name, "--dir", str(dst)],
            text=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        marker.write_text(str(artifact.get("id") or "") + "\n", encoding="utf-8")


def outcome_records(cache: Path) -> list[dict[str, Any]]:
    recs: list[dict[str, Any]] = []
    for path in cache.glob("*/outcome.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            recs.extend(data.get("tasks") or [])
        except Exception:
            continue
    return recs


def evidence_counts(cache: Path) -> tuple[int, int]:
    total = 0
    ok = 0
    for path in cache.glob("*/artifact_manifest.json"):
        total += 1
        try:
            if json.loads(path.read_text(encoding="utf-8")).get("ok") is True:
                ok += 1
        except Exception:
            pass
    return ok, total


def patch_counts(cache: Path) -> tuple[int, int]:
    total = 0
    patched = 0
    for task_dir in cache.iterdir() if cache.exists() else []:
        if not task_dir.is_dir():
            continue
        total += 1
        has_patch = False
        for path in task_dir.rglob("*"):
            if path.suffix.lower() not in {".patch", ".diff"}:
                continue
            try:
                if path.is_file() and path.stat().st_size > 0:
                    has_patch = True
                    break
            except OSError:
                continue
        if has_patch:
            patched += 1
    return patched, total


def metric_totals(cache: Path) -> dict[str, float | int]:
    totals: dict[str, float | int] = {
        "metrics": 0,
        "llm_calls": 0.0,
        "tokens_in": 0.0,
        "tokens_out": 0.0,
        "tokens_total": 0.0,
        "tokens_cached": 0.0,
        "cost_usd": 0.0,
        "gt_tokens": 0.0,
        "metric_patches": 0,
    }
    if not cache.exists():
        return totals
    for path in cache.glob("*/gt_deep_metrics_*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        eff = data.get("efficiency") or {}
        totals["metrics"] = int(totals["metrics"]) + 1
        totals["llm_calls"] = float(totals["llm_calls"]) + float(eff.get("llm_calls") or 0.0)
        totals["tokens_in"] = float(totals["tokens_in"]) + float(eff.get("llm_tokens_in") or 0.0)
        totals["tokens_out"] = float(totals["tokens_out"]) + float(eff.get("llm_tokens_out") or 0.0)
        totals["tokens_total"] = float(totals["tokens_total"]) + float(eff.get("llm_tokens_total") or 0.0)
        totals["tokens_cached"] = float(totals["tokens_cached"]) + float(eff.get("llm_tokens_cached") or 0.0)
        totals["cost_usd"] = float(totals["cost_usd"]) + float(eff.get("llm_cost_usd") or 0.0)
        totals["gt_tokens"] = float(totals["gt_tokens"]) + float(
            eff.get("gt_injected_tokens_total") or data.get("gt_injected_tokens_total") or 0.0
        )
        if data.get("has_patch") is True:
            totals["metric_patches"] = int(totals["metric_patches"]) + 1
    return totals


def class_counts(recs: list[dict[str, Any]]) -> dict[str, int]:
    counts = {"RESOLVED": 0, "AGENT": 0, "GT": 0, "INFRA": 0, "UNKNOWN": 0}
    for rec in recs:
        cls = str(rec.get("class") or rec.get("outcome_class") or "UNKNOWN").upper()
        counts[cls if cls in counts else "UNKNOWN"] += 1
    return counts


def validity_signal(*, failure: int, counts: dict[str, int]) -> tuple[str, str]:
    reasons: list[str] = []
    if failure > 0:
        reasons.append(f"failure={failure}")
    if counts.get("INFRA", 0) > 0:
        reasons.append(f"INFRA={counts['INFRA']}")
    if counts.get("UNKNOWN", 0) > 0:
        reasons.append(f"UNKNOWN={counts['UNKNOWN']}")
    if reasons:
        return "INVALID_ACTIONABLE", ",".join(reasons)
    return "OBSERVE", "-"


def trial_job(job: dict[str, Any]) -> bool:
    name = str(job.get("name") or "")
    return name not in {"prepare", "summarize"} and not name.startswith("${{ matrix.task }}")


def summarize(repo: str, run_id: str, benchmark: str, cache: Path) -> str:
    view = gh_json(["run", "view", run_id, "--repo", repo, "--json", "status,conclusion,jobs,url"])
    jobs = [j for j in view.get("jobs") or [] if trial_job(j)]
    status_counts: dict[str, int] = {}
    conclusion_counts: dict[str, int] = {}
    for job in jobs:
        status_counts[str(job.get("status") or "unknown")] = status_counts.get(str(job.get("status") or "unknown"), 0) + 1
        conclusion = str(job.get("conclusion") or "")
        if conclusion:
            conclusion_counts[conclusion] = conclusion_counts.get(conclusion, 0) + 1

    artifacts = [a for a in list_artifacts(repo, run_id) if str(a.get("name") or "").startswith(("deepswe-full-", "pro-full-"))]
    download_new_artifacts(repo, run_id, artifacts, cache)
    recs = outcome_records(cache)
    counts = class_counts(recs)
    denom = counts["RESOLVED"] + counts["AGENT"] + counts["GT"]
    resolved_rate = (counts["RESOLVED"] / denom) if denom else 0.0
    evidence_ok, evidence_total = evidence_counts(cache)
    evidence_latch = (evidence_ok / evidence_total) if evidence_total else 0.0
    artifact_latch = (len(artifacts) / len(jobs)) if jobs else 0.0
    patched, patch_total = patch_counts(cache)
    metrics = metric_totals(cache)
    patch_rate = (patched / patch_total) if patch_total else 0.0
    metric_patch_total = int(metrics["metrics"])
    metric_patches = int(metrics["metric_patches"])
    if metric_patch_total:
        patched = max(patched, metric_patches)
        patch_total = max(patch_total, metric_patch_total)
        patch_rate = patched / patch_total
    running = status_counts.get("in_progress", 0)
    completed = status_counts.get("completed", 0)
    queued = status_counts.get("queued", 0)
    success = conclusion_counts.get("success", 0)
    failure = conclusion_counts.get("failure", 0)
    cancelled = conclusion_counts.get("cancelled", 0)
    left = max(0, len(jobs) - completed)
    validity, diagnosis = validity_signal(failure=failure, counts=counts)

    return (
        f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {benchmark} run={run_id} "
        f"status={view.get('status')} conclusion={view.get('conclusion') or '-'} "
        f"running={running} queued={queued} completed={completed} "
        f"left={left} success={success} failure={failure} cancelled={cancelled} "
        f"validity={validity} diagnosis={diagnosis} "
        f"resolution_rate={resolved_rate:.8f} "
        f"patch_rate={patch_rate:.2%} ({patched}/{patch_total}) "
        f"metrics={int(metrics['metrics'])}/{len(artifacts)} "
        f"llm_calls={float(metrics['llm_calls']):.8f} "
        f"tokens_in={float(metrics['tokens_in']):.8f} "
        f"tokens_out={float(metrics['tokens_out']):.8f} "
        f"tokens_total={float(metrics['tokens_total']):.8f} "
        f"tokens_cached={float(metrics['tokens_cached']):.8f} "
        f"cost_usd={float(metrics['cost_usd']):.8f} "
        f"gt_tokens={float(metrics['gt_tokens']):.8f} "
        f"artifacts={len(artifacts)} artifact_latch={artifact_latch:.2%} "
        f"evidence_latch={evidence_latch:.2%} ({evidence_ok}/{evidence_total}) "
        f"outcomes={len(recs)} classes={counts} "
        f"url={view.get('url')}"
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--benchmark", required=True)
    parser.add_argument("--interval-min", type=float, required=True)
    parser.add_argument("--cache-dir", type=Path, required=True)
    parser.add_argument("--log", type=Path, required=True)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()

    args.log.parent.mkdir(parents=True, exist_ok=True)
    while True:
        try:
            line = summarize(args.repo, args.run_id, args.benchmark, args.cache_dir)
        except Exception as exc:
            line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {args.benchmark} monitor_error={exc!r}"
        print(line, flush=True)
        with args.log.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
        if args.once:
            return 0
        time.sleep(args.interval_min * 60)


if __name__ == "__main__":
    sys.exit(main())
