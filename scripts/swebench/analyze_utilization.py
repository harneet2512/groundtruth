"""Deep Utilization Analyzer — parses GT tool call logs for each phase.

Reads tool_calls_*.jsonl files and produces a utilization report showing:
- Tool call rates (what % of tasks called each tool)
- Briefing quality (non-empty rate, avg tokens, confidence distribution)
- Contract fire rate (tasks with ≥1 contract)
- Outcome correlation (GT usage vs resolution)

Usage:
    python scripts/swebench/analyze_utilization.py --logs-dir /path/to/logs/
    python scripts/swebench/analyze_utilization.py --logs-dir ./output/ --output report.json
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path


def parse_tool_calls(logs_dir: str) -> dict[str, list[dict]]:
    """Parse all tool_calls_*.jsonl files into per-instance tool call lists."""
    instances: dict[str, list[dict]] = {}
    logs_path = Path(logs_dir)

    for log_file in logs_path.glob("tool_calls_*.jsonl"):
        instance_id = log_file.stem.replace("tool_calls_", "")
        calls = []
        for line in log_file.read_text(errors="ignore").splitlines():
            if line.strip():
                try:
                    calls.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        instances[instance_id] = calls

    return instances


def compute_utilization(instances: dict[str, list[dict]]) -> dict:
    """Compute utilization metrics from parsed tool calls."""
    total_tasks = len(instances)
    if total_tasks == 0:
        return {"error": "No tool call logs found"}

    # Tool call rates
    tool_counts: Counter = Counter()
    tool_task_counts: dict[str, set] = defaultdict(set)
    tool_response_sizes: dict[str, list[int]] = defaultdict(list)

    # Briefing metrics
    briefings_generated = 0
    briefing_tokens: list[int] = []
    briefing_confidence: Counter = Counter()

    # Contract metrics
    tasks_with_contracts = 0
    contract_counts: list[int] = []
    contract_types: Counter = Counter()

    # Check metrics
    tasks_with_check = 0
    check_findings: list[int] = []
    blockers_issued = 0

    for instance_id, calls in instances.items():
        instance_tools: set[str] = set()
        instance_contracts = 0
        instance_check_findings = 0

        for call in calls:
            tool_name = call.get("tool", call.get("name", ""))
            tool_counts[tool_name] += 1
            tool_task_counts[tool_name].add(instance_id)
            instance_tools.add(tool_name)

            # Response size
            response = call.get("response", call.get("result", ""))
            if isinstance(response, str):
                tool_response_sizes[tool_name].append(len(response))
            elif isinstance(response, dict):
                tool_response_sizes[tool_name].append(len(json.dumps(response)))

            # Briefing tracking
            if call.get("type") == "briefing" or "briefing" in tool_name:
                briefings_generated += 1
                tokens = call.get("tokens", len(str(response)) // 4)
                briefing_tokens.append(tokens)
                conf = call.get("confidence", "unknown")
                briefing_confidence[conf] += 1

            # Contract tracking
            contracts = call.get("contracts", [])
            if isinstance(response, dict):
                contracts = response.get("contracts", contracts)
            if contracts:
                instance_contracts += len(contracts)
                for c in contracts:
                    if isinstance(c, dict):
                        contract_types[c.get("type", "unknown")] += 1

            # Check tracking
            if tool_name in ("gt_check", "groundtruth_check"):
                tasks_with_check += 1
                warnings = []
                if isinstance(response, dict):
                    warnings = response.get("warnings", [])
                    warnings += response.get("breaking_changes", [])
                    warnings += response.get("stale_references", [])
                    if response.get("blockers"):
                        blockers_issued += 1
                instance_check_findings += len(warnings)

        if instance_contracts > 0:
            tasks_with_contracts += 1
            contract_counts.append(instance_contracts)

        if instance_check_findings > 0:
            check_findings.append(instance_check_findings)

    # Build report
    report = {
        "total_tasks": total_tasks,
        "tool_calls": {
            "total": sum(tool_counts.values()),
            "by_tool": dict(tool_counts.most_common()),
            "task_rate": {
                tool: f"{len(tasks)}/{total_tasks} ({100*len(tasks)/total_tasks:.0f}%)"
                for tool, tasks in sorted(tool_task_counts.items())
            },
        },
        "briefing": {
            "generated": briefings_generated,
            "rate": f"{briefings_generated}/{total_tasks} ({100*briefings_generated/total_tasks:.0f}%)" if total_tasks else "0",
            "avg_tokens": sum(briefing_tokens) / len(briefing_tokens) if briefing_tokens else 0,
            "confidence_dist": dict(briefing_confidence),
        },
        "contracts": {
            "tasks_with_contracts": f"{tasks_with_contracts}/{total_tasks} ({100*tasks_with_contracts/total_tasks:.0f}%)",
            "avg_per_task": sum(contract_counts) / len(contract_counts) if contract_counts else 0,
            "by_type": dict(contract_types.most_common()),
        },
        "verification": {
            "gt_check_rate": f"{tasks_with_check}/{total_tasks} ({100*tasks_with_check/total_tasks:.0f}%)",
            "tasks_with_findings": len(check_findings),
            "blockers_issued": blockers_issued,
        },
        "avg_response_tokens": {
            tool: sum(sizes) // (4 * len(sizes)) if sizes else 0
            for tool, sizes in tool_response_sizes.items()
        },
    }

    return report


def print_report(report: dict) -> None:
    """Print a human-readable utilization report."""
    print("\n" + "=" * 60)
    print("GT DEEP UTILIZATION REPORT")
    print("=" * 60)

    print(f"\nTotal tasks analyzed: {report['total_tasks']}")

    print("\n--- Tool Call Rates ---")
    for tool, rate in report.get("tool_calls", {}).get("task_rate", {}).items():
        print(f"  {tool:20s}: {rate}")

    print("\n--- Briefing ---")
    b = report.get("briefing", {})
    print(f"  Generated: {b.get('rate', 'N/A')}")
    print(f"  Avg tokens: {b.get('avg_tokens', 0):.0f}")
    print(f"  Confidence: {b.get('confidence_dist', {})}")

    print("\n--- Contracts ---")
    c = report.get("contracts", {})
    print(f"  Tasks with contracts: {c.get('tasks_with_contracts', 'N/A')}")
    print(f"  Avg per task: {c.get('avg_per_task', 0):.1f}")
    print(f"  By type: {c.get('by_type', {})}")

    print("\n--- Verification (gt_check) ---")
    v = report.get("verification", {})
    print(f"  gt_check rate: {v.get('gt_check_rate', 'N/A')}")
    print(f"  Tasks with findings: {v.get('tasks_with_findings', 0)}")
    print(f"  Blockers issued: {v.get('blockers_issued', 0)}")

    print("\n--- Avg Response Tokens ---")
    for tool, tokens in report.get("avg_response_tokens", {}).items():
        print(f"  {tool:20s}: ~{tokens} tokens")

    print("\n" + "=" * 60)

    # Pass/fail assessment
    tc = report.get("tool_calls", {}).get("task_rate", {})
    total = report["total_tasks"]

    issues: list[str] = []
    gt_check_tasks = len([t for t in report.get("tool_calls", {}).get("by_tool", {}) if "check" in t])
    # Simple check: are tools being used?
    if not tc:
        issues.append("NO TOOL CALLS DETECTED — GT is not being used at all")
    if total > 0 and report.get("verification", {}).get("gt_check_rate", "").startswith("0/"):
        issues.append("gt_check never called — mandatory pre-submit check not firing")

    if issues:
        print("\nISSUES FOUND:")
        for issue in issues:
            print(f"  [!] {issue}")
        print("\nVERDICT: FIX GT BEFORE SCALING UP")
    else:
        print("\nVERDICT: Utilization looks healthy. Proceed to next phase.")


def main() -> None:
    parser = argparse.ArgumentParser(description="GT Deep Utilization Analyzer")
    parser.add_argument("--logs-dir", required=True, help="Directory with tool_calls_*.jsonl files")
    parser.add_argument("--output", help="Output JSON report path")
    args = parser.parse_args()

    instances = parse_tool_calls(args.logs_dir)
    report = compute_utilization(instances)

    print_report(report)

    if args.output:
        Path(args.output).write_text(json.dumps(report, indent=2))
        print(f"\nReport saved to {args.output}")


if __name__ == "__main__":
    main()
