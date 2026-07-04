#!/usr/bin/env python3
"""Summarize paired GT/baseline targeted DeepSWE industrial validation outputs."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def _load(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _find_validation(root: Path) -> list[Path]:
    if root.is_file() and root.name == "industrial_sota_validation.json":
        return [root]
    return sorted(root.rglob("industrial_sota_validation.json")) if root.is_dir() else []


def _task_id(path: Path, payload: dict[str, Any]) -> str:
    explicit = payload.get("instance_id") or payload.get("task_id")
    if explicit:
        return str(explicit)
    parts = path.parts
    for part in reversed(parts):
        if part.startswith("deepswe-full-"):
            return part.removeprefix("deepswe-full-")
    return path.parent.name


def _index(root: Path) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for path in _find_validation(root):
        payload = _load(path)
        tid = _task_id(path, payload)
        out[tid] = {"path": str(path), "payload": payload}
    return out


def _item_statuses(payload: dict[str, Any]) -> dict[int, str]:
    statuses: dict[int, str] = {}
    for item in payload.get("items") or []:
        if isinstance(item, dict) and isinstance(item.get("id"), int):
            statuses[int(item["id"])] = str(item.get("status") or "missing")
    return statuses


def summarize(gt_root: Path, baseline_root: Path | None) -> dict[str, Any]:
    gt = _index(gt_root)
    baseline = _index(baseline_root) if baseline_root else {}
    task_ids = sorted(set(gt) | set(baseline))
    rows: list[dict[str, Any]] = []
    item_counts: dict[str, dict[str, int]] = {}
    for tid in task_ids:
        gt_payload = (gt.get(tid) or {}).get("payload") or {}
        base_payload = (baseline.get(tid) or {}).get("payload") or {}
        gt_status = _item_statuses(gt_payload)
        base_status = _item_statuses(base_payload)
        for item_id, status in gt_status.items():
            bucket = item_counts.setdefault(str(item_id), {})
            bucket[status] = bucket.get(status, 0) + 1
        rows.append(
            {
                "task_id": tid,
                "gt_validation": (gt.get(tid) or {}).get("path"),
                "baseline_validation": (baseline.get(tid) or {}).get("path"),
                "gt_counts": gt_payload.get("counts") or {},
                "baseline_counts": base_payload.get("counts") or {},
                "gt_evidence_items": sorted(k for k, v in gt_status.items() if v == "evidence"),
                "gt_missing_items": sorted(k for k, v in gt_status.items() if v == "missing"),
                "gt_contradicted_items": sorted(k for k, v in gt_status.items() if v == "contradicted"),
                "baseline_present": tid in baseline,
            }
        )
    return {
        "schema": "gt.paired_industrial_sota_summary.v1",
        "gt_root": str(gt_root),
        "baseline_root": str(baseline_root) if baseline_root else None,
        "paired_task_count": sum(1 for tid in task_ids if tid in gt and tid in baseline),
        "gt_task_count": len(gt),
        "baseline_task_count": len(baseline),
        "item_status_counts_gt": item_counts,
        "tasks": rows,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--gt", required=True, help="GT-arm artifact root")
    parser.add_argument("--baseline", help="baseline-arm artifact root")
    args = parser.parse_args(argv)
    print(json.dumps(summarize(Path(args.gt), Path(args.baseline) if args.baseline else None), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
