#!/usr/bin/env python3
"""Pre-launch audit for expensive benchmark workflow dispatches.

This is intentionally local and cheap: run it before `gh workflow run` so known
configuration failures are caught before a GitHub Actions run is created.
"""
from __future__ import annotations

import argparse
import re
import sys


DIGEST_RE = re.compile(r"^ghcr\.io/[^@\s]+@sha256:[0-9a-f]{64}$")
SHARD_RE = re.compile(r"^(?P<index>[1-9][0-9]*)/(?P<total>[1-9][0-9]*)$")


def _fail(code: str, message: str) -> int:
    print(f"{code}: {message}", file=sys.stderr)
    return 1


def _audit(args: argparse.Namespace) -> int:
    if args.require_pinned_substrate == "1":
        if not args.gt_substrate_digest:
            hint = ""
            if args.repo == "harneet2512/groundtruth":
                hint = " harneet2512/groundtruth has no repo variable; pass --gt-substrate-digest explicitly."
            return _fail(
                "PRELAUNCH_GT_SUBSTRATE_DIGEST_MISSING",
                f"require_pinned_substrate=1 requires an immutable ghcr.io/...@sha256 digest.{hint}",
            )
        if not DIGEST_RE.match(args.gt_substrate_digest):
            return _fail(
                "PRELAUNCH_GT_SUBSTRATE_DIGEST_INVALID",
                "gt_substrate_digest must be an immutable ghcr.io/...@sha256:<64 hex> reference.",
            )

    if args.surface == "pro" and args.mode == "full":
        if not args.shard:
            return _fail(
                "PRELAUNCH_PRO_FULL_SHARD_MISSING",
                "SWE-bench Pro full has 731 tasks; pass --shard i/n to stay under the matrix cap.",
            )
        match = SHARD_RE.match(args.shard)
        if not match:
            return _fail("PRELAUNCH_PRO_FULL_SHARD_INVALID", "shard must have the form i/n, for example 1/3.")
        index = int(match.group("index"))
        total = int(match.group("total"))
        if index > total:
            return _fail("PRELAUNCH_PRO_FULL_SHARD_INVALID", "shard index must be <= shard total.")

    try:
        max_parallel = int(args.max_parallel)
    except ValueError:
        return _fail("PRELAUNCH_MAX_PARALLEL_INVALID", "max_parallel must be an integer.")
    if max_parallel < 1:
        return _fail("PRELAUNCH_MAX_PARALLEL_INVALID", "max_parallel must be >= 1.")

    if not args.ref:
        return _fail("PRELAUNCH_REF_MISSING", "dispatch ref must be explicit.")

    print("PRELAUNCH_AUDIT_PASS")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Audit benchmark workflow dispatch inputs before launch.")
    parser.add_argument("--surface", choices=("deepswe", "pro"), required=True)
    parser.add_argument("--repo", required=True, help="GitHub repo in owner/name form.")
    parser.add_argument("--ref", required=True, help="Branch, tag, or SHA used for workflow dispatch.")
    parser.add_argument("--gt-substrate-digest", default="")
    parser.add_argument("--require-pinned-substrate", choices=("0", "1"), default="1")
    parser.add_argument("--mode", default="", help="Pro mode: smoke, pilot, pilot100, or full.")
    parser.add_argument("--shard", default="", help="Pro full shard spec, e.g. 1/3.")
    parser.add_argument("--max-parallel", default="20")
    return _audit(parser.parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
