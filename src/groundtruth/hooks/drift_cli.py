"""CLI for the contract-DELTA lever — the mini-swe (pull) entry the `gt drift` shim calls.

The agent runs `gt drift <file>` after editing; this calls the SHARED engine
``groundtruth.hooks.contract_delta.compute_delta`` and prints the ``[CONTRACT-DELTA]``
lines (or nothing — correct-or-quiet). compute_delta recovers the pre-edit content from
git HEAD and indexes old+current the SAME single-file way (an unedited function is
byte-identical → no phantom drift), so this needs NO frozen baseline. Reads the code
graph only: no tests, no patch edits, always exits 0.

Replaces the retired reindex-diff path (drift_advisory / build_drift), which produced
false positives by diffing a full-build baseline vs an incremental `gt-index -file`
reindex. See docs/MINISWE_CONTRACT_DELTA_HANDOFF_20260605.md §0,§2.

Usage (matches the pier `gt` shim):
  python3 -m groundtruth.hooks.drift_cli --root <repo> --db <graph.db> --file <rel> [--file ...]
  python3 -m groundtruth.hooks.drift_cli --root <repo> --db <graph.db> --all-modified
"""
from __future__ import annotations

import argparse
import subprocess
import sys

from groundtruth.hooks.contract_delta import compute_delta


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


def _diff_for(root: str, file_rel: str) -> str:
    """Unified diff (-U0) of one edited file vs HEAD — compute_delta's pre-edit fallback
    when `git show HEAD:<file>` is unavailable."""
    try:
        out = subprocess.run(
            ["git", "-C", root, "diff", "-U0", "HEAD", "--", file_rel],
            capture_output=True, text=True, timeout=30,
        )
        return out.stdout or ""
    except (subprocess.SubprocessError, OSError):
        return ""


def _to_rel(root: str, path: str) -> str:
    """Normalize an edited path to the slash-relative form stored in nodes.file_path."""
    p = (path or "").replace("\\", "/")
    r = (root or "").replace("\\", "/").rstrip("/")
    if r and p.startswith(r + "/"):
        p = p[len(r) + 1:]
    return p.lstrip("./")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="GT contract-DELTA (pull): what your edit changed in the behavioral contract")
    p.add_argument("--root", required=True, help="repo root")
    p.add_argument("--db", required=True,
                   help="current graph.db (for the consequence: verified callers + twins)")
    p.add_argument("--file", action="append", default=[], help="edited file (repeatable)")
    p.add_argument("--all-modified", action="store_true", help="diff all git-modified files")
    args = p.parse_args(argv)

    files = list(args.file)
    if args.all_modified or not files:
        files = files or _git_modified(args.root)
    if not files:
        return 0

    lines: list[str] = []
    for f in files:
        rel = _to_rel(args.root, f)
        try:
            lines += compute_delta(
                args.db, rel, repo_root=args.root, diff_text=_diff_for(args.root, rel))
        except Exception:  # noqa: BLE001 — advisory layer must never break the agent
            pass
    if lines:
        print("<gt-delta>")
        print("\n".join(lines))
        print("</gt-delta>")
    return 0


if __name__ == "__main__":
    sys.exit(main())
