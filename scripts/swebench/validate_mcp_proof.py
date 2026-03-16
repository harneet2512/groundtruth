#!/usr/bin/env python3
"""Validate MCP proof artifacts; reject invalid runs. Exit 0 if valid, 1 if invalid."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Add repo root so we can import benchmarks.swebench
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from benchmarks.swebench.proof import validate_proof_from_dir


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate MCP proof for a run directory")
    parser.add_argument(
        "proof_dir",
        type=Path,
        help="Directory containing mcp_usage.json or proof/<instance_id>/ subdirs",
    )
    parser.add_argument("--quiet", "-q", action="store_true", help="Only exit code, no output")
    args = parser.parse_args()

    proof_dir = args.proof_dir.resolve()
    if not proof_dir.exists():
        if not args.quiet:
            print(f"Error: {proof_dir} does not exist", file=sys.stderr)
        return 1

    valid, message, proof = validate_proof_from_dir(proof_dir)
    if valid:
        if not args.quiet:
            print("VALID:", message)
        return 0
    if not args.quiet:
        print("INVALID:", message, file=sys.stderr)
        if proof:
            print("  connection_ok:", proof.connection_ok, file=sys.stderr)
            print("  substantive_tool_count:", proof.substantive_tool_count, file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
