"""Minimal JSONL trace analyzer.

Reads gt_traces.jsonl and answers:
- How many times each endpoint was called
- Which internal components fired most often
- Which were most often suppressed/abstained
- Which endpoints were driven by which components
- Abstention frequency

Usage:
    python -m groundtruth.observability.analyzer [trace_file]
    python -m groundtruth.observability.analyzer --json [trace_file]
"""

from __future__ import annotations

import json
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any


def load_traces(path: Path) -> list[dict[str, Any]]:
    """Load all traces from a JSONL file."""
    traces: list[dict[str, Any]] = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                traces.append(json.loads(line))
    return traces


def analyze(traces: list[dict[str, Any]]) -> dict[str, Any]:
    """Produce a summary from trace records."""
    endpoint_calls: Counter[str] = Counter()
    component_used: Counter[str] = Counter()
    component_suppressed: Counter[str] = Counter()
    component_abstained: Counter[str] = Counter()
    component_failed: Counter[str] = Counter()
    component_skipped: Counter[str] = Counter()
    endpoint_components: dict[str, Counter[str]] = defaultdict(Counter)
    endpoint_verdicts: dict[str, Counter[str]] = defaultdict(Counter)
    total_duration_ms = 0.0

    for t in traces:
        ep = t.get("request", {}).get("endpoint", "unknown")
        endpoint_calls[ep] += 1
        total_duration_ms += t.get("response", {}).get("total_duration_ms", 0.0)

        verdict = t.get("response", {}).get("verdict", "")
        if verdict:
            endpoint_verdicts[ep][verdict] += 1

        for c in t.get("components", []):
            name = c.get("component", "unknown")
            status = c.get("status", "unknown")
            if status == "used":
                component_used[name] += 1
                endpoint_components[ep][name] += 1
            elif status == "suppressed":
                component_suppressed[name] += 1
            elif status == "abstained":
                component_abstained[name] += 1
            elif status == "failed":
                component_failed[name] += 1
            elif status == "skipped":
                component_skipped[name] += 1

    n = len(traces)
    return {
        "total_traces": n,
        "endpoint_calls": dict(endpoint_calls.most_common()),
        "avg_duration_ms": round(total_duration_ms / n, 1) if n else 0,
        "component_used": dict(component_used.most_common(15)),
        "component_suppressed": dict(component_suppressed.most_common(10)),
        "component_abstained": dict(component_abstained.most_common(10)),
        "component_failed": dict(component_failed.most_common(10)),
        "component_skipped": dict(component_skipped.most_common(10)),
        "endpoint_component_drivers": {
            ep: dict(counts.most_common(5)) for ep, counts in endpoint_components.items()
        },
        "endpoint_verdicts": {
            ep: dict(counts.most_common(5)) for ep, counts in endpoint_verdicts.items()
        },
        "abstention_rate": (
            round(
                sum(component_abstained.values())
                / (sum(component_used.values()) + sum(component_abstained.values()))
                * 100,
                1,
            )
            if (sum(component_used.values()) + sum(component_abstained.values())) > 0
            else 0
        ),
    }


def format_summary(result: dict[str, Any]) -> str:
    """Format analysis result as human-readable text."""
    lines = [
        f"Traces: {result['total_traces']}",
        f"Avg duration: {result['avg_duration_ms']}ms",
        f"Abstention rate: {result['abstention_rate']}%",
        "",
        "Endpoint calls:",
    ]
    for ep, count in result["endpoint_calls"].items():
        lines.append(f"  {ep}: {count}")

    lines.append("")
    lines.append("Most used components:")
    for comp, count in result["component_used"].items():
        lines.append(f"  {comp}: {count}")

    if result["component_suppressed"]:
        lines.append("")
        lines.append("Most suppressed:")
        for comp, count in result["component_suppressed"].items():
            lines.append(f"  {comp}: {count}")

    if result["component_abstained"]:
        lines.append("")
        lines.append("Most abstained:")
        for comp, count in result["component_abstained"].items():
            lines.append(f"  {comp}: {count}")

    if result["endpoint_verdicts"]:
        lines.append("")
        lines.append("Verdicts by endpoint:")
        for ep, verdicts in result["endpoint_verdicts"].items():
            lines.append(f"  {ep}:")
            for v, count in verdicts.items():
                lines.append(f"    {v}: {count}")

    return "\n".join(lines)


def main() -> None:
    """CLI entry point."""
    args = sys.argv[1:]
    as_json = "--json" in args
    args = [a for a in args if a != "--json"]

    path = Path(args[0]) if args else Path(".groundtruth/traces/gt_traces.jsonl")
    if not path.exists():
        print(f"No trace file at {path}", file=sys.stderr)
        sys.exit(1)

    traces = load_traces(path)
    result = analyze(traces)

    if as_json:
        print(json.dumps(result, indent=2))
    else:
        print(format_summary(result))


if __name__ == "__main__":
    main()
