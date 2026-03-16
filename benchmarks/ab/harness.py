#!/usr/bin/env python3
"""A/B benchmark harness: single entrypoint for no_mcp and with_groundtruth_mcp.

Usage:
  python -m benchmarks.ab.harness --condition no_mcp [--fixture all]
  python -m benchmarks.ab.harness --condition with_groundtruth_mcp [--fixture all]
  python -m benchmarks.ab.harness --condition both [--fixture all]

Output: benchmarks/ab/results/<condition>.json and run metadata including MCP proof.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid
from pathlib import Path

# Ensure project root, src, and benchmarks are on path (runner uses _fixtures from benchmarks/)
_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
if str(_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(_ROOT / "src"))
if str(_ROOT / "benchmarks") not in sys.path:
    sys.path.insert(0, str(_ROOT / "benchmarks"))

from benchmarks.ab.models import ABCondition, ABReport
from benchmarks.ab.no_mcp_runner import run_no_mcp
from benchmarks.ab.mcp_client_runner import run_with_groundtruth_mcp


def write_report(report: ABReport, output_dir: Path, condition: str) -> None:
    """Write JSON report and run metadata."""
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"{condition}.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(report.to_dict(), f, indent=2)
    print(f"Results written to {path}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="A/B benchmark: no_mcp vs with_groundtruth_mcp (provable MCP usage)"
    )
    parser.add_argument(
        "--condition",
        required=True,
        choices=["no_mcp", "with_groundtruth_mcp", "both"],
        help="Which condition(s) to run",
    )
    parser.add_argument(
        "--fixture",
        default="all",
        choices=["all", "project_ts", "project_py", "project_go", "typescript", "python", "go"],
        help="Fixture filter (default: all)",
    )
    parser.add_argument(
        "--output-dir",
        default=None,
        help="Output directory (default: benchmarks/ab/results)",
    )
    parser.add_argument(
        "--model",
        default=None,
        help="Model name (for agent-based runs; stored in metadata)",
    )
    parser.add_argument(
        "--temperature",
        type=float,
        default=None,
        help="Temperature (for agent-based runs; stored in metadata)",
    )
    parser.add_argument(
        "--max-tokens",
        type=int,
        default=None,
        help="Max tokens (for agent-based runs; stored in metadata)",
    )
    args = parser.parse_args()

    output_dir = Path(args.output_dir) if args.output_dir else _ROOT / "benchmarks" / "ab" / "results"

    run_id = str(uuid.uuid4())
    model_config = {
        "model": args.model,
        "temperature": args.temperature,
        "max_tokens": args.max_tokens,
    }

    if args.condition == "no_mcp":
        print("Running no_mcp (in-process, no MCP server)...")
        report = asyncio.run(run_no_mcp(
            fixture_filter=args.fixture,
            run_id=run_id,
            model=model_config["model"],
            temperature=model_config["temperature"],
            max_tokens=model_config["max_tokens"],
        ))
        write_report(report, output_dir, "no_mcp")
        print(f"  Cases: {report.total_cases}, detected: {report.detected}, fix_ok: {report.fix_correct}")
        return 0

    if args.condition == "with_groundtruth_mcp":
        print("Running with_groundtruth_mcp (spawn server, connect client, prove tool use)...")
        report = run_with_groundtruth_mcp(
            fixture_filter=args.fixture,
            run_id=run_id,
            model=model_config["model"],
            temperature=model_config["temperature"],
            max_tokens=model_config["max_tokens"],
        )
        write_report(report, output_dir, "with_groundtruth_mcp")
        proof = report.metadata.mcp_proof
        if proof:
            print(f"  MCP proof: connection_ok={proof.connection_ok}, tools_discovered={len(proof.tools_discovered)}, substantive_calls={proof.substantive_tool_count}, valid={proof.valid}")
        print(f"  Cases: {report.total_cases}, detected: {report.detected}, fix_ok: {report.fix_correct}")
        if proof and not proof.valid:
            print("  WARNING: MCP proof invalid (run did not meet minimum substantive tool use)")
            return 1
        return 0

    # both
    print("Running no_mcp...")
    report_no = asyncio.run(run_no_mcp(
        fixture_filter=args.fixture,
        run_id=run_id,
        model=model_config["model"],
        temperature=model_config["temperature"],
        max_tokens=model_config["max_tokens"],
    ))
    write_report(report_no, output_dir, "no_mcp")
    print("Running with_groundtruth_mcp...")
    report_mcp = run_with_groundtruth_mcp(
        fixture_filter=args.fixture,
        run_id=run_id,
        model=model_config["model"],
        temperature=model_config["temperature"],
        max_tokens=model_config["max_tokens"],
    )
    write_report(report_mcp, output_dir, "with_groundtruth_mcp")
    proof = report_mcp.metadata.mcp_proof
    if proof:
        print(f"  MCP proof: connection_ok={proof.connection_ok}, substantive_calls={proof.substantive_tool_count}, valid={proof.valid}")
    print("\nComparison:")
    print(f"  no_mcp:   cases={report_no.total_cases}, detected={report_no.detected}, fix_ok={report_no.fix_correct}")
    print(f"  w/ MCP:   cases={report_mcp.total_cases}, detected={report_mcp.detected}, fix_ok={report_mcp.fix_correct}")
    return 0 if (not proof or proof.valid) else 1


if __name__ == "__main__":
    sys.exit(main())
