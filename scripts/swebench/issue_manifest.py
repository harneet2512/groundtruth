#!/usr/bin/env python3
"""Structured issue input manifest (P1-01)."""
from __future__ import annotations

import hashlib
import json
import os
import sys
from typing import Any


def build_issue_manifest(issue_path: str, *, source: str) -> dict[str, Any]:
    text = ""
    if os.path.isfile(issue_path):
        with open(issue_path, encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest() if text else None
    return {
        "schema": "gt.issue_manifest.v1",
        "source": source,
        "path": issue_path,
        "char_length": len(text),
        "sha256": digest,
        "non_empty": bool(text.strip()),
    }


def write_issue_manifest(issue_path: str, out_path: str, *, source: str) -> dict[str, Any]:
    manifest = build_issue_manifest(issue_path, source=source)
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=2)
        fh.write("\n")
    return manifest


def main(argv: list[str] | None = None) -> int:
    args = (argv or sys.argv)[1:]
    if len(args) < 2:
        print("usage: issue_manifest.py <issue.txt> <out.json> [--source NAME]", file=sys.stderr)
        return 2
    source = "unknown"
    if "--source" in args:
        i = args.index("--source")
        source = args[i + 1] if i + 1 < len(args) else source
    write_issue_manifest(args[0], args[1], source=source)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
