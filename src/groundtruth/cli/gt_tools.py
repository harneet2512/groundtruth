#!/usr/bin/env python3
"""CLI interface for GT composite tools — designed for agent use via bash.

Usage (inside the container):
    gt_lookup <symbol> [--file <path>]
    gt_impact <symbol> [--file <path>]
    gt_check <file_path>

Budget: gt_lookup=2/task, gt_impact=2/task, gt_check=3/task.
When budget is exhausted, returns BUDGET_EXHAUSTED with redirect suggestion.
"""
import argparse
import os
import sys


def main():
    parser = argparse.ArgumentParser(prog="gt", description="GroundTruth code intelligence tools")
    sub = parser.add_subparsers(dest="command")

    p_lookup = sub.add_parser("lookup", help="Callers + callees + tests for a symbol (cap=2)")
    p_lookup.add_argument("symbol")
    p_lookup.add_argument("--file", default="")

    p_impact = sub.add_parser("impact", help="Blast radius + sibling norms (cap=2)")
    p_impact.add_argument("symbol")
    p_impact.add_argument("--file", default="")

    p_check = sub.add_parser("check", help="Pre-submit file validation (cap=3)")
    p_check.add_argument("file_path")

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        sys.exit(1)

    db_path = os.environ.get("GT_GRAPH_DB", "/tmp/gt_index.db")
    root_path = os.environ.get("GT_REPO_ROOT", "/workspace")

    from groundtruth.mcp.composite import gt_lookup_impl, gt_impact_impl, gt_check_impl

    if args.command == "lookup":
        print(gt_lookup_impl(args.symbol, db_path=db_path, root_path=root_path, file_path=args.file))
    elif args.command == "impact":
        print(gt_impact_impl(args.symbol, db_path=db_path, root_path=root_path, file_path=args.file))
    elif args.command == "check":
        print(gt_check_impl(args.file_path, db_path=db_path, root_path=root_path))


if __name__ == "__main__":
    main()
