#!/usr/bin/env python3
"""Validate a task artifact bundle for stabilization analysis.

Usage:
    python scripts/gt_collect_artifacts.py <task_directory>

Checks that required files exist, reports missing optional files.
Exit code 0 = output.jsonl present. Exit code 1 = missing.
"""
from __future__ import annotations

import json
import os
import sys


REQUIRED = ["output.jsonl"]
OPTIONAL = [
    "gt_layer_events*.jsonl",
    "gt_interactions*.jsonl",
    "evidence_metrics.json",
    "eval_result.json",
    "graph.db",
    "full_run.log",
]


def find_glob(directory: str, pattern: str) -> str | None:
    """Find first file matching a glob-like pattern (supports * wildcard)."""
    import fnmatch
    for f in os.listdir(directory):
        if fnmatch.fnmatch(f, pattern):
            return os.path.join(directory, f)
    for root, dirs, files in os.walk(directory):
        for f in files:
            if fnmatch.fnmatch(f, pattern):
                return os.path.join(root, f)
    return None


def validate_bundle(directory: str) -> dict:
    """Validate artifact bundle completeness."""
    result = {
        "directory": directory,
        "valid": False,
        "files": {},
        "missing_required": [],
        "missing_optional": [],
    }

    if not os.path.isdir(directory):
        result["error"] = f"Directory does not exist: {directory}"
        return result

    for req in REQUIRED:
        if "*" in req:
            path = find_glob(directory, req)
        else:
            path = os.path.join(directory, req)
            if not os.path.isfile(path):
                path = find_glob(directory, req)

        if path and os.path.isfile(path):
            size = os.path.getsize(path)
            result["files"][req] = {"path": path, "size": size, "present": True}
        else:
            result["missing_required"].append(req)
            result["files"][req] = {"path": None, "size": 0, "present": False}

    for opt in OPTIONAL:
        if "*" in opt:
            path = find_glob(directory, opt)
        else:
            path = os.path.join(directory, opt)
            if not os.path.isfile(path):
                path = find_glob(directory, opt)

        if path and os.path.isfile(path):
            size = os.path.getsize(path)
            result["files"][opt] = {"path": path, "size": size, "present": True}
        else:
            result["missing_optional"].append(opt)
            result["files"][opt] = {"path": None, "size": 0, "present": False}

    result["valid"] = len(result["missing_required"]) == 0
    return result


def main():
    if len(sys.argv) < 2:
        print("Usage: python scripts/gt_collect_artifacts.py <task_directory>")
        sys.exit(1)

    directory = sys.argv[1]
    result = validate_bundle(directory)

    print(f"Bundle: {directory}")
    print(f"Valid: {result['valid']}")
    print()
    print(f"{'File':<35} {'Present':<10} {'Size':<15}")
    print("-" * 60)
    for name, info in result["files"].items():
        status = "YES" if info["present"] else "MISSING"
        size = f"{info['size']:,}" if info["present"] else "-"
        print(f"{name:<35} {status:<10} {size:<15}")

    if result["missing_required"]:
        print(f"\nFATAL: Missing required files: {result['missing_required']}")
        sys.exit(1)
    if result["missing_optional"]:
        print(f"\nWARN: Missing optional files: {result['missing_optional']}")

    # Write validation result
    out_path = os.path.join(directory, "bundle_validation.json")
    with open(out_path, "w") as f:
        json.dump(result, f, indent=2)
    print(f"\nValidation written to: {out_path}")


if __name__ == "__main__":
    main()
