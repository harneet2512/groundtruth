#!/usr/bin/env python3
"""Aggregate GT telemetry from OpenHands output.jsonl (gt_telemetry field)."""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path
import re
from typing import Any, Iterable


def _iter_jsonl(path: Path) -> Iterable[dict[str, Any]]:
    yield from _iter_jsonl_lines(path.read_text(encoding="utf-8", errors="replace"))


def _iter_jsonl_lines(text: str) -> Iterable[dict[str, Any]]:
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            yield json.loads(line)
        except json.JSONDecodeError:
            continue


def iter_telemetry_rows(paths: list[Path]) -> list[dict[str, Any]]:
    """Each row: instance_id, source, telemetry (full gt_telemetry dict)."""
    out: list[dict[str, Any]] = []
    for path in paths:
        if not path.is_file():
            continue
        for rec in _iter_jsonl(path):
            tel = rec.get("gt_telemetry")
            if not isinstance(tel, dict):
                inst = rec.get("instance")
                if isinstance(inst, dict):
                    tel = inst.get("gt_telemetry")
            if isinstance(tel, dict):
                out.append(
                    {
                        "instance_id": str(rec.get("instance_id", "") or ""),
                        "source": path.name,
                        "telemetry": tel,
                    }
                )
    return out


_L4_INVOCATION = re.compile(
    r"\bgt_query\s+\S+|\bgt_search\s+\S+\s+\S+|\bgt_navigate\s+\S+\s+\S+|\bgt_validate\s+\S+",
    re.IGNORECASE,
)
_EVIDENCE_TAG = re.compile(
    r"\[(?:GT_CHANGE|GT_CONTRACT|GT_CALLER|GT_SIBLING|GT_PATTERN|GT_STRUCTURAL|GT_SEMANTIC)\]",
    re.IGNORECASE,
)
_L3B_MARK = re.compile(r"\[GT_L3B\]|gt_l3b", re.IGNORECASE)
_REINDEX = re.compile(r"<gt-reindex", re.IGNORECASE)
_L5_ADVISORY = re.compile(r"<gt-advisory[^>]{0,120}layer=['\"]?L5", re.IGNORECASE)
_L5_GATE_TEXT = re.compile(r"\[GT_GATE\]", re.IGNORECASE)


def _history_gt_tool_invocation(record: dict[str, Any]) -> bool:
    """Detect real gt_* command invocations in run-style history events."""

    history = record.get("history")
    if not isinstance(history, list):
        return False

    def _action_name(event: dict[str, Any]) -> str:
        action = event.get("action")
        if isinstance(action, str):
            return action.lower()
        if isinstance(action, dict):
            for key in ("action", "class", "type", "name"):
                value = action.get(key)
                if isinstance(value, str):
                    return value.lower()
        return ""

    def _command_text(event: dict[str, Any]) -> str:
        args = event.get("args")
        if isinstance(args, dict):
            for key in ("command", "cmd", "input", "content", "text"):
                value = args.get(key)
                if isinstance(value, str) and value.strip():
                    return value
        for key in ("command", "cmd"):
            value = event.get(key)
            if isinstance(value, str) and value.strip():
                return value
        return ""

    run_kinds = {"run", "cmdrunaction", "execute_bash", "bash"}
    for event in history:
        if not isinstance(event, dict):
            continue
        action = _action_name(event)
        if action not in run_kinds:
            continue
        cmd = _command_text(event)
        if cmd and _L4_INVOCATION.search(cmd):
            return True
    return False


def _infer_layers_from_record(record: dict[str, Any]) -> dict[str, bool]:
    """Best-effort utilization inference when `gt_telemetry` isn't present.

    We rely on marker strings that are already verified by `oh_gt_full_layer_audit.py`
    (L1/L2/L3/L3b/L4/L5/L6).
    """

    s = json.dumps(record, default=str, ensure_ascii=False)
    s_low = s.lower()

    l1 = "<gt-task-brief>" in s
    l1_quality = (
        l1
        and "GT could not rank files" not in s
        and "[GT_BRIEF_FAILED]" not in s
        and "GT graph built inside" not in s
    )
    l2 = "<gt-pretask" in s

    # L3 requires post_edit + an evidence family tag.
    l3 = ("post_edit:" in s_low) and bool(_EVIDENCE_TAG.search(s))

    # L3b is post_view structural coupling.
    l3b = ("post_view:" in s_low) and bool(_L3B_MARK.search(s))

    l4 = _history_gt_tool_invocation(record)
    inst = record.get("instance")
    has_inst_advisory = isinstance(inst, dict) and bool(str(inst.get("gt_advisory", "")).strip())
    l5 = bool(_L5_ADVISORY.search(s) or _L5_GATE_TEXT.search(s) or has_inst_advisory)
    l6 = bool(_REINDEX.search(s))

    return {
        "L1": l1, "L1_quality": l1_quality, "L2": l2,
        "L3": l3, "L3b": l3b, "L4": l4, "L5": l5, "L6": l6,
    }


def _infer_telemetry_rows(paths: list[Path]) -> list[dict[str, Any]]:
    weights = {"L1": 0.2, "L2": 0.15, "L3": 0.2, "L3b": 0.1, "L4": 0.1, "L5": 0.15, "L6": 0.1}

    rows: list[dict[str, Any]] = []
    for path in paths:
        if not path.is_file():
            continue
        for rec in _iter_jsonl(path):
            if not isinstance(rec, dict):
                continue
            inst_id = str(rec.get("instance_id", "") or "")
            task_id = str(rec.get("task_id", "") or "")
            layers = _infer_layers_from_record(rec)
            l1_q = layers.pop("L1_quality", False)
            utilization = {k: (1.0 if v else 0.0) for k, v in layers.items()}
            utilization["L1_quality"] = 1.0 if l1_q else 0.0
            overall = sum(utilization.get(k, 0.0) * weights[k] for k in weights)
            layer_hits = {
                k: ({"ok": 1, "fail": 0, "skipped": 0} if v else {"ok": 0, "fail": 1, "skipped": 0})
                for k, v in layers.items()
            }
            rows.append(
                {
                    "instance_id": inst_id,
                    "source": path.name,
                    "telemetry": {
                        "task_id": task_id,
                        "utilization": utilization,
                        "overall_utilization": round(overall, 4),
                        "layer_hits": layer_hits,
                    },
                }
            )
    return rows


def _pct(vals: list[float], q: float) -> float:
    if not vals:
        return 0.0
    vs = sorted(vals)
    idx = int(round(q * (len(vs) - 1)))
    return vs[max(0, min(idx, len(vs) - 1))]


def _merge_layer_hits(rows: list[dict[str, Any]]) -> dict[str, dict[str, int]]:
    merged: dict[str, dict[str, int]] = {}
    for row in rows:
        lh = row["telemetry"].get("layer_hits") or {}
        if not isinstance(lh, dict):
            continue
        for layer, counts in lh.items():
            if not isinstance(counts, dict):
                continue
            bucket = merged.setdefault(str(layer), {"ok": 0, "fail": 0, "skipped": 0})
            for k in ("ok", "fail", "skipped"):
                bucket[k] = bucket.get(k, 0) + int(counts.get(k, 0) or 0)
    return merged


def _print_deep(rows: list[dict[str, Any]]) -> None:
    layer_keys = ("L1", "L2", "L3", "L3b", "L4", "L5", "L6")

    print("=== per-instance ===")
    print("instance_id\ttask_id\toverall\t" + "\t".join(layer_keys))
    scores: list[float] = []
    layer_series: dict[str, list[float]] = {k: [] for k in layer_keys}

    for row in rows:
        tel = row["telemetry"]
        u = tel.get("utilization") or {}
        if not isinstance(u, dict):
            u = {}
        overall = float(tel.get("overall_utilization", 0) or 0)
        scores.append(overall)
        parts = [
            row.get("instance_id", "") or "(no_id)",
            str(tel.get("task_id", "")),
            f"{overall:.3f}",
        ]
        for lk in layer_keys:
            v = 0.0
            if lk in u:
                try:
                    v = float(u[lk])
                except (TypeError, ValueError):
                    pass
            layer_series[lk].append(v)
            parts.append(f"{v:.2f}")
        print("\t".join(parts))

    print("\n=== merged layer_hits (sum ok / fail / skipped) ===")
    merged = _merge_layer_hits(rows)
    for layer in sorted(merged.keys(), key=lambda x: (len(x), x)):
        b = merged[layer]
        print(f"  {layer}: ok={b.get('ok', 0)} fail={b.get('fail', 0)} skipped={b.get('skipped', 0)}")

    print("\n=== distribution (overall + per-layer utilization) ===")
    if scores:
        print(
            f"overall: n={len(scores)} mean={statistics.mean(scores):.3f} "
            f"median={statistics.median(scores):.3f} stdev={statistics.stdev(scores) if len(scores) > 1 else 0.0:.3f} "
            f"p25={_pct(scores, 0.25):.3f} p75={_pct(scores, 0.75):.3f} min={min(scores):.3f} max={max(scores):.3f}"
        )
    for lk in layer_keys:
        vals = layer_series[lk]
        if not vals:
            continue
        print(
            f"{lk}: n={len(vals)} mean={statistics.mean(vals):.3f} "
            f"median={statistics.median(vals):.3f} stdev={statistics.stdev(vals) if len(vals) > 1 else 0.0:.3f}"
        )

    print("\n=== weakest runs (lowest overall_utilization) ===")
    ranked = sorted(
        rows,
        key=lambda r: float(r["telemetry"].get("overall_utilization", 0) or 0),
    )
    for row in ranked[: min(5, len(ranked))]:
        o = float(row["telemetry"].get("overall_utilization", 0) or 0)
        print(f"  {o:.3f}\t{row.get('instance_id')}\t{row['telemetry'].get('task_id')}")


def main() -> int:
    ap = argparse.ArgumentParser(description="GT utilization report (merge multiple output.jsonl)")
    ap.add_argument(
        "jsonl",
        type=Path,
        nargs="+",
        help="One or more output.jsonl paths (merged for stats)",
    )
    ap.add_argument("--deep", action="store_true", help="Per-instance table, merged hits, percentiles")
    args = ap.parse_args()

    for p in args.jsonl:
        if not p.is_file():
            print(f"FATAL: not a file: {p}", file=sys.stderr)
            return 2

    rows = iter_telemetry_rows(args.jsonl)
    inferred_rows = _infer_telemetry_rows(args.jsonl)
    if rows:
        def _row_key(row: dict[str, Any]) -> tuple[str, str]:
            return (str(row.get("source", "")), str(row.get("instance_id", "")))

        present = {_row_key(r) for r in rows}
        missing = [r for r in inferred_rows if _row_key(r) not in present]
        if missing:
            print(f"Supplementing {len(missing)} instance(s) without gt_telemetry using marker inference.")
            rows.extend(missing)
    else:
        print("No gt_telemetry records found. Inferring utilization from layer markers.")
        rows = inferred_rows
        if not rows:
            print("No records found to infer utilization.")
            return 0

    scores = [float(r["telemetry"].get("overall_utilization", 0) or 0) for r in rows]
    print(f"files={len(args.jsonl)} instances_with_telemetry={len(rows)}")
    print(f"overall_utilization mean={statistics.mean(scores):.3f} max={max(scores):.3f} min={min(scores):.3f}")

    layer_keys = ("L1", "L1_quality", "L2", "L3", "L3b", "L4", "L5", "L6")
    for lk in layer_keys:
        vals: list[float] = []
        for r in rows:
            u = r["telemetry"].get("utilization") or {}
            if isinstance(u, dict) and lk in u:
                try:
                    vals.append(float(u[lk]))
                except (TypeError, ValueError):
                    continue
        if vals:
            print(f"{lk} mean={statistics.mean(vals):.3f} (n={len(vals)})")

    if args.deep:
        print("")
        _print_deep(rows)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
