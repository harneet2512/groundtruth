#!/usr/bin/env python3
"""Read and count the local industrial/SOTA validation trajectory."""
from __future__ import annotations

import argparse
import ast
import json
from collections import Counter
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_TRAJECTORY = ROOT / ".groundtruth" / "industrial_sota_local_trajectory.jsonl"


def _records(path: Path) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError as exc:
            raise SystemExit(f"{path}:{n}: invalid JSONL: {exc}") from exc
        if isinstance(rec, dict):
            out.append(rec)
    return out


def _json_stdout(step: dict[str, Any]) -> dict[str, Any]:
    raw = step.get("stdout") or ""
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _ledger_stdout(step: dict[str, Any]) -> dict[str, Any]:
    raw = (step.get("stdout") or "").strip()
    try:
        data = ast.literal_eval(raw)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def summarize(path: Path) -> dict[str, Any]:
    records = _records(path)
    steps = [r for r in records if r.get("schema") == "gt.local_validation_step.v1"]
    summary = next(
        (r for r in reversed(records) if r.get("schema") == "gt.local_validation_trajectory_summary.v1"),
        {},
    )
    by_id = {str(s.get("check_id")): s for s in steps}
    status_counts = Counter("ok" if s.get("ok") else "failed" for s in steps)

    iteration = _json_stdout(by_id.get("iteration_selector_multilingual", {}))
    deepswe = _json_stdout(by_id.get("deepswe_selector_explicit", {}))
    gate = _json_stdout(by_id.get("industrial_validation_gate_smoke", {}))
    ledger = _ledger_stdout(by_id.get("ledger_count", {}))
    resolution = _json_stdout(by_id.get("resolution_method_audit_smoke", {}))

    iteration_langs = Counter(str(t.get("language") or "unknown") for t in iteration.get("tasks", []))
    deepswe_langs = Counter(str(t.get("language") or "unknown") for t in deepswe.get("tasks", []))
    failed = [
        {
            "check_id": s.get("check_id"),
            "rc": s.get("rc"),
            "timed_out": s.get("timed_out"),
            "stderr": (s.get("stderr") or "")[:500],
        }
        for s in steps
        if not s.get("ok")
    ]

    graph_counts = {}
    graphs = resolution.get("graphs") or []
    if graphs:
        g0 = graphs[0]
        graph_counts = {
            "graph_count": resolution.get("graph_count"),
            "call_edges": g0.get("call_edges"),
            "untrusted_name_match_call_edges": g0.get("untrusted_name_match_call_edges"),
            "ambiguous_candidate_call_edges": g0.get("ambiguous_candidate_call_edges"),
            "lsp_promoted_call_edges": g0.get("lsp_promoted_call_edges"),
            "trusted_call_edges": g0.get("trusted_call_edges"),
            "trusted_ratio": g0.get("trusted_ratio"),
        }

    return {
        "schema": "gt.local_validation_trajectory_read.v1",
        "trajectory": str(path),
        "record_count": len(records),
        "step_count": len(steps),
        "step_status_counts": dict(status_counts),
        "summary_record": {
            "step_count": summary.get("step_count"),
            "ok_count": summary.get("ok_count"),
            "failed_count": summary.get("failed_count"),
        },
        "failed_steps": failed,
        "ledger": ledger,
        "industrial_gate_counts": gate.get("counts") or {},
        "iteration_surface_languages": dict(sorted(iteration_langs.items())),
        "deepswe_surface_languages": dict(sorted(deepswe_langs.items())),
        "resolution_audit": graph_counts,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("trajectory", nargs="?", default=str(DEFAULT_TRAJECTORY))
    args = parser.parse_args(argv)
    print(json.dumps(summarize(Path(args.trajectory)), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
