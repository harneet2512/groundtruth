#!/usr/bin/env python3
"""Pre-compute GroundTruth analysis for SWE-bench tasks.

Since Qwen3-Coder ignores bash-based GT tool calls even with MANDATORY prompting,
we pre-compute GT analysis and inject it directly into the prompt as context.

Usage:
    python3 scripts/swebench/gt_pre_analyze.py --instance django__django-12856 --container <cid>
    python3 scripts/swebench/gt_pre_analyze.py --instance-file fifty_tasks.txt --output-dir /tmp/gt_analyses

For batch mode (used by run script):
    Reads instance IDs, runs gt_tool.py inside their Docker containers,
    saves analysis as .txt files that get injected into the prompt template.
"""
import argparse
import json
import os
import re
import subprocess
import sys


def extract_symbols_from_issue(problem_statement: str, max_symbols: int = 3) -> list[str]:
    """Extract likely class/symbol names from an issue description.

    Uses regex to find CamelCase words and common Python identifiers.
    """
    # CamelCase class names (2+ capital letters in word)
    camel = re.findall(r'\b([A-Z][a-z]+(?:[A-Z][a-z]+)+)\b', problem_statement)
    # ALL_CAPS constants
    caps = re.findall(r'\b([A-Z][A-Z_]{2,})\b', problem_statement)
    # snake_case function/method names that appear near code context
    snake = re.findall(r'`(\w+(?:_\w+)+)`', problem_statement)
    # Dotted references like Model.method
    dotted = re.findall(r'\b([A-Z][a-zA-Z]*\.[a-z_]\w*)\b', problem_statement)

    # Deduplicate, prefer CamelCase (most likely to be classes)
    seen = set()
    symbols = []
    for s in camel + [d.split('.')[0] for d in dotted] + snake + caps:
        if s not in seen and len(s) > 2:
            seen.add(s)
            symbols.append(s)
            if len(symbols) >= max_symbols:
                break

    return symbols


def run_gt_in_container(container_id: str, command: str, symbol: str = "") -> str:
    """Run gt_tool.py inside a Docker container and capture output."""
    cmd = ["docker", "exec", container_id, "python3", "/tmp/gt_tool.py", command]
    if symbol:
        cmd.append(symbol)

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=60,
        )
        output = result.stdout.strip()
        if result.returncode != 0 and result.stderr:
            output += f"\n[stderr: {result.stderr.strip()[:200]}]"
        return output
    except subprocess.TimeoutExpired:
        return f"[timeout after 60s for {command} {symbol}]"
    except Exception as e:
        return f"[error: {e}]"


def ensure_gt_tool_in_container(container_id: str, gt_tool_path: str) -> bool:
    """Copy gt_tool.py into the container if not already present."""
    # Check if already there
    check = subprocess.run(
        ["docker", "exec", container_id, "test", "-f", "/tmp/gt_tool.py"],
        capture_output=True,
    )
    if check.returncode == 0:
        return True

    # Copy it in
    result = subprocess.run(
        ["docker", "cp", gt_tool_path, f"{container_id}:/tmp/gt_tool.py"],
        capture_output=True,
    )
    return result.returncode == 0


def analyze_instance(
    container_id: str,
    problem_statement: str,
    gt_tool_path: str,
) -> str:
    """Run GT analysis for a single instance and return formatted output."""
    # Ensure gt_tool.py is in the container
    if not ensure_gt_tool_in_container(container_id, gt_tool_path):
        return "[GT pre-analysis failed: could not copy gt_tool.py into container]"

    symbols = extract_symbols_from_issue(problem_statement)
    if not symbols:
        return "[GT pre-analysis: no symbols extracted from issue]"

    sections = []
    sections.append(f"Symbols detected: {', '.join(symbols)}")

    # Run groundtruth_impact on top 2 symbols
    for sym in symbols[:2]:
        output = run_gt_in_container(container_id, "groundtruth_impact", sym)
        if output and "[error" not in output and "[timeout" not in output:
            sections.append(f"### Impact analysis: {sym}\n{output}")

    # Run groundtruth_references on top symbol
    if symbols:
        output = run_gt_in_container(container_id, "groundtruth_references", symbols[0])
        if output and "[error" not in output and "[timeout" not in output:
            sections.append(f"### References: {symbols[0]}\n{output}")

    if len(sections) <= 1:
        return "[GT pre-analysis: no useful output from gt_tool.py]"

    return "\n\n".join(sections)


def load_swebench_instances(dataset_path: str = "princeton-nlp/SWE-bench_Lite") -> dict:
    """Load SWE-bench instances from HuggingFace dataset. Returns {instance_id: problem_statement}."""
    try:
        from datasets import load_dataset
        ds = load_dataset(dataset_path, split="test")
        return {row["instance_id"]: row["problem_statement"] for row in ds}
    except ImportError:
        print("WARNING: datasets not installed, cannot load SWE-bench instances")
        return {}


def main():
    parser = argparse.ArgumentParser(description="Pre-compute GT analysis for SWE-bench tasks")
    parser.add_argument("--instance", help="Single instance ID to analyze")
    parser.add_argument("--instance-file", help="File with instance IDs (one per line)")
    parser.add_argument("--container", help="Docker container ID (for single instance mode)")
    parser.add_argument("--output-dir", default="/tmp/gt_analyses", help="Directory to save analysis files")
    parser.add_argument("--gt-tool", help="Path to gt_tool.py",
                        default=os.path.join(os.path.dirname(__file__), "../../benchmarks/swebench/gt_tool.py"))
    parser.add_argument("--dataset", default="princeton-nlp/SWE-bench_Lite")
    parser.add_argument("--problem-statement", help="Problem statement (for single instance without dataset)")

    args = parser.parse_args()
    os.makedirs(args.output_dir, exist_ok=True)

    gt_tool_path = os.path.abspath(args.gt_tool)
    if not os.path.exists(gt_tool_path):
        print(f"ERROR: gt_tool.py not found at {gt_tool_path}")
        sys.exit(1)

    if args.instance and args.container:
        # Single instance mode
        problem = args.problem_statement or ""
        if not problem:
            instances = load_swebench_instances(args.dataset)
            problem = instances.get(args.instance, "")

        analysis = analyze_instance(args.container, problem, gt_tool_path)
        out_file = os.path.join(args.output_dir, f"{args.instance}.txt")
        with open(out_file, "w") as f:
            f.write(analysis)
        print(f"Saved: {out_file}")
        print(analysis)
    elif args.instance_file:
        # Batch mode — just extract symbols and save for later container injection
        with open(args.instance_file) as f:
            instance_ids = [line.strip() for line in f if line.strip()]

        instances = load_swebench_instances(args.dataset)
        for iid in instance_ids:
            problem = instances.get(iid, "")
            symbols = extract_symbols_from_issue(problem)
            out_file = os.path.join(args.output_dir, f"{iid}.symbols.json")
            with open(out_file, "w") as f:
                json.dump({"instance_id": iid, "symbols": symbols}, f)
            print(f"  {iid}: symbols={symbols}")

        print(f"\nExtracted symbols for {len(instance_ids)} instances → {args.output_dir}")
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
