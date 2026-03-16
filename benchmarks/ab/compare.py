#!/usr/bin/env python3
"""Compare A/B benchmark results: no_mcp vs with_groundtruth_mcp.

Reads both condition JSON files and produces a single comparison report with deltas.

Usage:
  python -m benchmarks.ab.compare [--results-dir benchmarks/ab/results] [--output report.md]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def load_report(path: Path) -> dict:
    """Load a condition JSON report."""
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def compare(no_mcp: dict, with_mcp: dict) -> str:
    """Generate a markdown comparison report."""
    lines: list[str] = []

    meta_no = no_mcp.get("metadata", {})
    meta_mcp = with_mcp.get("metadata", {})

    lines.append("# A/B Benchmark Comparison")
    lines.append("")
    lines.append("| Metric | no_mcp | with_groundtruth_mcp | Delta |")
    lines.append("|--------|--------|----------------------|-------|")

    # Hallucination metrics
    def row(label: str, v_no: float | int, v_mcp: float | int, fmt: str = ".2f") -> None:
        if isinstance(v_no, float) and isinstance(v_mcp, float):
            delta = v_mcp - v_no
            delta_str = f"{delta:+.2f}" if delta != 0 else "0"
        else:
            delta_str = f"{v_mcp - v_no:+d}" if (v_no != v_mcp) else "0"
        if isinstance(v_no, float):
            lines.append(f"| {label} | {v_no:{fmt}} | {v_mcp:{fmt}} | {delta_str} |")
        else:
            lines.append(f"| {label} | {v_no} | {v_mcp} | {delta_str} |")

    total_no = no_mcp.get("total_cases", 0)
    total_mcp = with_mcp.get("total_cases", 0)
    def pct(x: float) -> str:
        return f"{x * 100:.1f}%"

    row("Total cases", total_no, total_mcp, "d")
    row("Detected", no_mcp.get("detected", 0), with_mcp.get("detected", 0), "d")
    row("Fix correct", no_mcp.get("fix_correct", 0), with_mcp.get("fix_correct", 0), "d")
    lines.append(f"| Detection rate | {pct(no_mcp.get('detection_rate', 0))} | {pct(with_mcp.get('detection_rate', 0))} | |")
    lines.append(f"| Fix rate | {pct(no_mcp.get('fix_rate', 0))} | {pct(with_mcp.get('fix_rate', 0))} | |")
    row("Elapsed (s)", no_mcp.get("elapsed_s", 0), with_mcp.get("elapsed_s", 0), ".3f")

    # File relevance
    fr_no = no_mcp.get("file_relevance", {}).get("results", [])
    fr_mcp = with_mcp.get("file_relevance", {}).get("results", [])
    if fr_no and fr_mcp:
        avg_prec_no = sum(r.get("precision", 0) for r in fr_no) / len(fr_no)
        avg_prec_mcp = sum(r.get("precision", 0) for r in fr_mcp) / len(fr_mcp)
        avg_rec_no = sum(r.get("recall", 0) for r in fr_no) / len(fr_no)
        avg_rec_mcp = sum(r.get("recall", 0) for r in fr_mcp) / len(fr_mcp)
        lines.append("| **File relevance** | | | |")
        lines.append(f"| Avg precision | {pct(avg_prec_no)} | {pct(avg_prec_mcp)} | |")
        lines.append(f"| Avg recall | {pct(avg_rec_no)} | {pct(avg_rec_mcp)} | |")

    # MCP proof
    proof = meta_mcp.get("mcp_proof")
    lines.append("")
    lines.append("## MCP proof (with_groundtruth_mcp)")
    lines.append("")
    if proof:
        lines.append(f"- **connection_ok:** {proof.get('connection_ok', False)}")
        lines.append(f"- **tools_discovered:** {len(proof.get('tools_discovered', []))} tools")
        lines.append(f"- **substantive_tool_count:** {proof.get('substantive_tool_count', 0)}")
        lines.append(f"- **valid:** {proof.get('valid', False)}")
    else:
        lines.append("No MCP proof in report.")
    lines.append("")

    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Compare A/B benchmark results (no_mcp vs with_groundtruth_mcp)"
    )
    parser.add_argument(
        "--results-dir",
        type=Path,
        default=Path(__file__).resolve().parent / "results",
        help="Directory containing no_mcp.json and with_groundtruth_mcp.json",
    )
    parser.add_argument(
        "--output",
        "-o",
        type=Path,
        default=None,
        help="Write report to this file (default: stdout)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output machine-readable comparison as JSON",
    )
    args = parser.parse_args()

    results_dir = args.results_dir
    no_mcp_path = results_dir / "no_mcp.json"
    with_mcp_path = results_dir / "with_groundtruth_mcp.json"

    if not no_mcp_path.is_file():
        print(f"Error: {no_mcp_path} not found", file=sys.stderr)
        return 1
    if not with_mcp_path.is_file():
        print(f"Error: {with_mcp_path} not found", file=sys.stderr)
        return 1

    no_mcp = load_report(no_mcp_path)
    with_mcp = load_report(with_mcp_path)

    if args.json:
        # Machine-readable: summary deltas + paths
        fr_no = no_mcp.get("file_relevance", {}).get("results", [])
        fr_mcp = with_mcp.get("file_relevance", {}).get("results", [])
        avg = lambda r, k: sum(x.get(k, 0) for x in r) / len(r) if r else 0
        out = {
            "no_mcp": {
                "detection_rate": no_mcp.get("detection_rate"),
                "fix_rate": no_mcp.get("fix_rate"),
                "elapsed_s": no_mcp.get("elapsed_s"),
                "file_relevance_avg_precision": avg(fr_no, "precision"),
                "file_relevance_avg_recall": avg(fr_no, "recall"),
            },
            "with_groundtruth_mcp": {
                "detection_rate": with_mcp.get("detection_rate"),
                "fix_rate": with_mcp.get("fix_rate"),
                "elapsed_s": with_mcp.get("elapsed_s"),
                "file_relevance_avg_precision": avg(fr_mcp, "precision"),
                "file_relevance_avg_recall": avg(fr_mcp, "recall"),
                "mcp_proof_valid": with_mcp.get("metadata", {}).get("mcp_proof", {}).get("valid"),
            },
            "delta": {
                "fix_rate": with_mcp.get("fix_rate", 0) - no_mcp.get("fix_rate", 0),
                "elapsed_s": with_mcp.get("elapsed_s", 0) - no_mcp.get("elapsed_s", 0),
            },
        }
        text = json.dumps(out, indent=2)
    else:
        text = compare(no_mcp, with_mcp)

    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text, encoding="utf-8")
        print(f"Report written to {args.output}")
    else:
        print(text)

    return 0


if __name__ == "__main__":
    sys.exit(main())
