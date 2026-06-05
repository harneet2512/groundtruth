"""CLI for the contract-drift lever — the SHARED entry both scaffolds invoke.

mini-swe (pull): the agent runs this after editing and before submitting.
OpenHands (push): the pre-submit hook runs this in-container for edited files.

Graph-backed (reads graph.db `properties` via drift_hook) and language-agnostic —
deliberately NOT the ast-only gt_hook.py path, so it generalizes to go/rust/ts/js.
Advisory only: prints the <gt-drift> block (or nothing) and always exits 0.

Usage:
  python3 -m groundtruth.hooks.drift_cli --root <repo> --db <graph.db> --file <rel> [--file <rel> ...]
  python3 -m groundtruth.hooks.drift_cli --root <repo> --db <graph.db> --all-modified
"""
from __future__ import annotations

import argparse
import subprocess
import sys

from groundtruth.hooks.drift_hook import drift_advisory


def _git_modified(root: str) -> list[str]:
    """Tracked files changed vs HEAD (what the agent edited this task)."""
    try:
        out = subprocess.run(
            ["git", "-C", root, "diff", "--name-only", "HEAD"],
            capture_output=True, text=True, timeout=30,
        )
        return [ln.strip() for ln in out.stdout.splitlines() if ln.strip()]
    except (subprocess.SubprocessError, OSError):
        return []


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="GT contract-drift advisory")
    p.add_argument("--root", required=True, help="repo root")
    p.add_argument("--db", required=True, help="working graph.db (the .orig baseline must exist)")
    p.add_argument("--file", action="append", default=[], help="edited file (repeatable)")
    p.add_argument("--all-modified", action="store_true", help="diff all git-modified files")
    args = p.parse_args(argv)

    files = list(args.file)
    if args.all_modified or not files:
        files = files or _git_modified(args.root)
    if not files:
        return 0
    out = drift_advisory(args.root, args.db, files)
    if out:
        print(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
