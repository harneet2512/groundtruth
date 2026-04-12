"""Boundary Guard — prevents new semantic logic from landing in benchmarks/.

Run in CI or pre-commit to enforce that new semantic extraction, contract
mining, verifier policy, and procedure logic lives in src/groundtruth/,
NOT in benchmarks/swebench/ or hook files.

Usage:
    python scripts/check_boundary.py [--diff]

Exit code 0 = clean, 1 = violation found.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys


# Files that should NOT grow new semantic functions
GUARDED_PATHS = [
    "benchmarks/swebench/gt_intel.py",
    "benchmarks/swebench/swe_agent_state_gt.py",
    "benchmarks/swebench/run_mini_gt_hooked.py",
]

# Patterns that indicate new semantic logic
SEMANTIC_PATTERNS = [
    r"^\+\s*def\s+extract_",       # New extraction functions
    r"^\+\s*def\s+compute_",       # New computation functions
    r"^\+\s*def\s+mine_",          # New mining functions
    r"^\+\s*def\s+verify_",        # New verification functions
    r"^\+\s*def\s+score_",         # New scoring functions
    r"^\+\s*class\s+\w+Extractor", # New extractor classes
    r"^\+\s*class\s+\w+Miner",     # New miner classes
    r"^\+\s*class\s+\w+Verifier",  # New verifier classes
    r"^\+\s*class\s+\w+Scorer",    # New scorer classes
]

# Allowed patterns (adapter shims, imports from substrate)
ALLOWED_PATTERNS = [
    r"from groundtruth\.(substrate|contracts|verification|procedures|repo_intel)",
    r"try_substrate_evidence",
    r"# SUBSTRATE_SHIM",
]


def check_diff(diff_text: str) -> list[str]:
    """Check a diff for boundary violations.

    Returns list of violation descriptions.
    """
    violations: list[str] = []
    current_file: str = ""

    for line in diff_text.splitlines():
        if line.startswith("diff --git"):
            # Extract filename
            parts = line.split(" b/")
            current_file = parts[-1] if len(parts) > 1 else ""
            continue

        # Only check guarded files
        if not any(current_file.endswith(p) for p in GUARDED_PATHS):
            continue

        # Check for semantic patterns in added lines
        for pattern in SEMANTIC_PATTERNS:
            if re.match(pattern, line):
                # Check if it's an allowed pattern
                if any(re.search(ap, line) for ap in ALLOWED_PATTERNS):
                    continue
                violations.append(
                    f"VIOLATION in {current_file}: {line.strip()}\n"
                    f"  -> New semantic logic must go in src/groundtruth/,"
                    f"not in benchmark adapters."
                )

    return violations


def get_staged_diff() -> str:
    """Get git diff of staged changes."""
    result = subprocess.run(
        ["git", "diff", "--cached", "--unified=0"],
        capture_output=True,
        text=True,
    )
    return result.stdout


def get_branch_diff() -> str:
    """Get diff from current branch vs main."""
    result = subprocess.run(
        ["git", "diff", "master...HEAD", "--unified=0"],
        capture_output=True,
        text=True,
    )
    return result.stdout


def main() -> None:
    parser = argparse.ArgumentParser(description="GT Boundary Guard")
    parser.add_argument(
        "--diff",
        choices=["staged", "branch", "stdin"],
        default="branch",
        help="Diff source (default: branch vs master)",
    )
    args = parser.parse_args()

    if args.diff == "staged":
        diff_text = get_staged_diff()
    elif args.diff == "branch":
        diff_text = get_branch_diff()
    else:
        diff_text = sys.stdin.read()

    if not diff_text:
        print("No diff to check.")
        sys.exit(0)

    violations = check_diff(diff_text)

    if violations:
        print(f"FAIL: {len(violations)} boundary violation(s) found:\n")
        for v in violations:
            print(f"  {v}")
        print(
            "\nRule: New semantic logic (extraction, mining, verification, scoring) "
            "must live in src/groundtruth/{substrate,contracts,verification,"
            "repo_intel,procedures}/.\n"
            "Benchmark hooks may only CALL into substrate, not OWN logic."
        )
        sys.exit(1)
    else:
        print("OK: No boundary violations found.")
        sys.exit(0)


if __name__ == "__main__":
    main()
