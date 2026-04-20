"""Post-shard reconciler for the 20-task baseline calibration.

For a given shard output directory, reconciles:
  - Which instance_ids were expected (from the manifest slice for this shard).
  - Which trajectories actually exist.
  - Whether each has a preds.json entry and whether the recorded patch is:
      clean (non-empty, parses as unified diff)
      empty (record exists but model_patch blank)
      malformed (record exists but diff cannot parse)
      missing (no record at all)
  - Whether the task's run.log shows the Vertex cache_control / tools collision
    (FM-1) or the LiteLLM orphan-tool-call cascade (FM-2). These are classified
    as `tool_history_corruption` so that repeat occurrences after the FM-1 fix
    are surfaced immediately rather than buried inside empty_patch.

Exit 0 only if every expected id has a trajectory AND a preds.json record.
Patch quality is REPORTED, not gated here -- that's for cal_metrics.py + pass/fail.
Silent drops (missing trajectory OR missing preds entry) are a hard fail.

Usage:
  python scripts/cal_shard_reconcile.py <shard_id> <shard_output_dir> \
      --manifest benchmarks/swebench/cal20_live_lite.manifest.json \
      --shard-slice 1-10
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path


FM1_SIGNATURE = (
    "tool config, tools and system instruction should not be set "
    "in the request when using cached content"
)
FM2_SIGNATURE = "missing corresponding tool call for tool response message"


def _iter_preds_files(output_dir: Path):
    yield from output_dir.rglob("preds.json")


def _load_all_preds(output_dir: Path) -> dict:
    merged: dict = {}
    for p in _iter_preds_files(output_dir):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            merged.update(data)
    return merged


def _find_trajectory(output_dir: Path, iid: str) -> Path | None:
    for ext in (f"{iid}.traj.json", f"{iid}.traj", f"{iid}.json"):
        for hit in output_dir.rglob(ext):
            return hit
    return None


def _patch_is_valid_diff(patch: str) -> bool:
    if not patch.strip():
        return False
    # Cheap structural check: does it contain at least one `--- ` and `+++ ` header + a hunk.
    if "--- " not in patch or "+++ " not in patch:
        return False
    if "@@" not in patch:
        return False
    # Optional hard check with `git apply --check` in a scratch dir
    try:
        proc = subprocess.run(
            ["git", "apply", "--check", "-"],
            input=patch.encode("utf-8"),
            capture_output=True,
            timeout=15,
        )
        # Hard-check may fail simply because context doesn't match any repo; that's OK --
        # the cheap structural check is authoritative. We only use git here to surface
        # catastrophically malformed diffs.
        if proc.returncode != 0 and b"error: unrecognized input" in proc.stderr:
            return False
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return True


def _iter_log_files(output_dir: Path):
    for name in ("run.log", "sweagent.log", "agent.log"):
        yield from output_dir.rglob(name)


def _scan_tool_history_corruption(output_dir: Path, expected: list[str]) -> dict[str, list[str]]:
    """Return {iid: [reasons]} for ids whose logs show FM-1 or FM-2 signatures.

    Scans all known log files in the shard output dir. For each log line that
    matches FM-1 or FM-2, looks back up to 200 lines for the most recent
    instance_id marker that matches one of the expected ids and attributes the
    hit to that task. If no instance_id context is found, the hit is attributed
    to "__unscoped__" so the count is still visible at shard level.
    """
    id_pattern = re.compile(r"(" + "|".join(re.escape(i) for i in expected) + r")") if expected else None
    hits: dict[str, set[str]] = {}
    if id_pattern is None:
        return {}

    for log_path in _iter_log_files(output_dir):
        try:
            lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue

        current_id: str | None = None
        window: list[str] = []
        for line in lines:
            low = line.lower()
            m = id_pattern.search(line)
            if m:
                current_id = m.group(1)
            if FM1_SIGNATURE in low:
                target = current_id or "__unscoped__"
                hits.setdefault(target, set()).add("fm1_cache_tools_collision")
            if FM2_SIGNATURE in low:
                target = current_id or "__unscoped__"
                hits.setdefault(target, set()).add("fm2_orphan_tool_call")
            window.append(line)
            if len(window) > 200:
                window.pop(0)

    return {iid: sorted(reasons) for iid, reasons in hits.items()}


def _parse_slice(spec: str, total: int) -> tuple[int, int]:
    a, b = spec.split("-", 1)
    lo = int(a) - 1
    hi = int(b)
    if lo < 0 or hi > total or lo >= hi:
        raise SystemExit(f"bad --shard-slice {spec} for manifest size {total}")
    return lo, hi


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("shard_id")
    p.add_argument("shard_output_dir")
    p.add_argument(
        "--manifest", default="benchmarks/swebench/cal20_live_lite.manifest.json"
    )
    p.add_argument(
        "--shard-slice",
        required=True,
        help="Inclusive 1-indexed slice of the manifest for this shard, e.g. 1-10.",
    )
    p.add_argument("--report", default=None, help="Optional JSON report path.")
    args = p.parse_args()

    manifest = json.loads(Path(args.manifest).read_text(encoding="utf-8"))
    all_ids = manifest["selected"]
    lo, hi = _parse_slice(args.shard_slice, len(all_ids))
    expected = all_ids[lo:hi]

    shard_dir = Path(args.shard_output_dir)
    if not shard_dir.exists():
        sys.stderr.write(f"FAIL: shard dir not found: {shard_dir}\n")
        return 2

    preds = _load_all_preds(shard_dir)

    missing_traj: list[str] = []
    missing_pred: list[str] = []
    empty_patch: list[str] = []
    malformed_patch: list[str] = []
    clean: list[str] = []

    for iid in expected:
        traj = _find_trajectory(shard_dir, iid)
        if traj is None:
            missing_traj.append(iid)
        record = preds.get(iid)
        if record is None:
            missing_pred.append(iid)
            continue
        patch = record.get("model_patch") or record.get("patch") or ""
        if not patch.strip():
            empty_patch.append(iid)
        elif not _patch_is_valid_diff(patch):
            malformed_patch.append(iid)
        else:
            clean.append(iid)

    corruption = _scan_tool_history_corruption(shard_dir, expected)
    tool_history_corruption = sorted(k for k in corruption if k in set(expected))

    report = {
        "shard_id": args.shard_id,
        "shard_output_dir": str(shard_dir),
        "expected_count": len(expected),
        "missing_trajectory": missing_traj,
        "missing_prediction": missing_pred,
        "empty_patch": empty_patch,
        "malformed_patch": malformed_patch,
        "clean": clean,
        "silent_drop": sorted(set(missing_traj) | set(missing_pred)),
        "tool_history_corruption": tool_history_corruption,
        "tool_history_corruption_detail": corruption,
    }

    if args.report:
        Path(args.report).write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")

    print(json.dumps(report, indent=2))

    if report["silent_drop"]:
        sys.stderr.write(
            f"FAIL: silent drop on shard {args.shard_id}: {report['silent_drop']}\n"
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
