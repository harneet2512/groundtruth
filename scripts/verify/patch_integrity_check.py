#!/usr/bin/env python3
"""patch_integrity_check.py -- Verify patch production and integrity from run artifacts.

For each task in the output directory, checks:
1. Whether a patch was produced (output.jsonl contains git_patch with 'diff')
2. Whether the patch is well-formed (starts with 'diff --git', has hunks)
3. Computes SHA-256 hash for dedup/comparison

Usage:
    python scripts/verify/patch_integrity_check.py --output-dir /path/to/artifacts

Output: JSON to stdout.
Exit 0 on success, 1 on script error.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from pathlib import Path


def find_output_jsonls(output_dir: str) -> list[Path]:
    """Find all output.jsonl files in the artifact directory tree."""
    results = []
    for root, _dirs, files in os.walk(output_dir):
        for f in files:
            if f == "output.jsonl":
                results.append(Path(root) / f)
    return sorted(results)


def infer_task_id(output_jsonl: Path) -> str:
    """Infer task_id from directory structure or file content."""
    for part in output_jsonl.parts:
        if part.startswith("task-"):
            return part.replace("task-", "", 1)
        if "__" in part and any(c.isdigit() for c in part):
            return part
    try:
        with open(output_jsonl, "r", encoding="utf-8", errors="replace") as fh:
            first = fh.readline().strip()
            if first:
                d = json.loads(first)
                return d.get("instance_id", d.get("task_id", str(output_jsonl.parent.name)))
    except Exception:
        pass
    return str(output_jsonl.parent.name)


def extract_patch(output_jsonl: Path) -> str | None:
    """Extract the git patch from output.jsonl."""
    try:
        with open(output_jsonl, "r", encoding="utf-8", errors="replace") as fh:
            for raw_line in fh:
                raw_line = raw_line.strip()
                if not raw_line:
                    continue
                try:
                    record = json.loads(raw_line)
                except json.JSONDecodeError:
                    continue

                # Standard locations for the patch
                patch = (
                    record.get("git_patch", "")
                    or record.get("test_result", {}).get("git_patch", "")
                    or record.get("model_patch", "")
                    or ""
                )
                if patch and "diff" in patch:
                    return patch

                # Also check history entries for the final patch submission
                history = record.get("history", [])
                if isinstance(history, list):
                    for entry in reversed(history):
                        p = entry.get("git_patch", "") or ""
                        if p and "diff" in p:
                            return p
    except OSError as exc:
        print(f"ERROR: cannot read {output_jsonl}: {exc}", file=sys.stderr)
    return None


def check_patch_wellformed(patch: str) -> dict:
    """Check if a patch is well-formed."""
    issues = []

    # Must start with diff --git (after possible whitespace)
    lines = patch.strip().splitlines()
    if not lines:
        return {"wellformed": False, "issues": ["empty patch"]}

    has_diff_header = any(line.startswith("diff --git ") for line in lines)
    if not has_diff_header:
        issues.append("missing 'diff --git' header")

    # Must have at least one hunk header (@@ ... @@)
    has_hunk = any(line.startswith("@@") for line in lines)
    if not has_hunk:
        issues.append("missing hunk header (@@)")

    # Should have +/- lines
    has_additions = any(line.startswith("+") and not line.startswith("+++") for line in lines)
    has_deletions = any(line.startswith("-") and not line.startswith("---") for line in lines)
    if not has_additions and not has_deletions:
        issues.append("no additions or deletions")

    # Check for common corruption: binary garbage
    try:
        patch.encode("utf-8")
    except UnicodeEncodeError:
        issues.append("contains non-UTF-8 bytes")

    # Check for truncation: diff header without corresponding hunk
    diff_count = sum(1 for line in lines if line.startswith("diff --git "))
    hunk_count = sum(1 for line in lines if line.startswith("@@"))
    if diff_count > 0 and hunk_count == 0:
        issues.append(f"truncated: {diff_count} diff headers but 0 hunks")

    return {
        "wellformed": len(issues) == 0,
        "issues": issues,
        "diff_headers": diff_count,
        "hunk_count": hunk_count,
        "has_additions": has_additions,
        "has_deletions": has_deletions,
        "total_lines": len(lines),
        "total_chars": len(patch),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Check patch integrity from run artifacts")
    parser.add_argument("--output-dir", required=True, help="Root directory with run artifacts")
    args = parser.parse_args()

    output_dir = args.output_dir
    if not os.path.isdir(output_dir):
        print(json.dumps({"error": f"Not a directory: {output_dir}"}))
        return 1

    output_files = find_output_jsonls(output_dir)
    if not output_files:
        print(json.dumps({"error": "No output.jsonl files found", "dir": output_dir}))
        return 1

    results = []

    for ojf in output_files:
        task_id = infer_task_id(ojf)
        patch = extract_patch(ojf)

        if patch is None:
            results.append({
                "task_id": task_id,
                "output_jsonl": str(ojf),
                "patch_produced": False,
                "patch_integrity": "missing",
                "sha256": None,
                "details": None,
            })
            continue

        details = check_patch_wellformed(patch)
        sha = hashlib.sha256(patch.encode("utf-8", errors="replace")).hexdigest()

        integrity = "clean" if details["wellformed"] else "malformed"

        results.append({
            "task_id": task_id,
            "output_jsonl": str(ojf),
            "patch_produced": True,
            "patch_integrity": integrity,
            "sha256": sha,
            "details": details,
        })

    patches_produced = sum(1 for r in results if r["patch_produced"])
    patches_clean = sum(1 for r in results if r["patch_integrity"] == "clean")

    output = {
        "check": "patch_integrity_check",
        "tasks_checked": len(results),
        "patches_produced": patches_produced,
        "patches_clean": patches_clean,
        "patches_malformed": patches_produced - patches_clean,
        "patches_missing": len(results) - patches_produced,
        "results": results,
    }

    print(json.dumps(output, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
