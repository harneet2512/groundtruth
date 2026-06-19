#!/usr/bin/env python3
"""Lightweight checkpoint doc guard (P0-14)."""
from __future__ import annotations

import os
import subprocess
import sys


def main() -> int:
    root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    latest = os.path.join(root, "LATEST_TASK.md")
    if not os.path.isfile(latest):
        print("WARN: LATEST_TASK.md missing", file=sys.stderr)
        return 0
    try:
        diff = subprocess.check_output(
            ["git", "diff", "--name-only", "HEAD~1", "HEAD"],
            cwd=root,
            text=True,
            stderr=subprocess.DEVNULL,
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        return 0
    if "docker/Dockerfile.gt-substrate" not in diff:
        return 0
    text = open(latest, encoding="utf-8").read()
    if "GT_SUBSTRATE_DIGEST" not in text and "substrate" not in text.lower():
        print(
            "FAIL: substrate Dockerfile changed but LATEST_TASK.md lacks digest/checkpoint note",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
