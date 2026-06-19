#!/usr/bin/env python3
"""Pre-launch audit for expensive benchmark workflow dispatches.

This is intentionally local and cheap: run it before `gh workflow run` so known
configuration failures are caught before a GitHub Actions run is created.
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path


DIGEST_RE = re.compile(r"^ghcr\.io/[^@\s]+@sha256:[0-9a-f]{64}$")
SHARD_RE = re.compile(r"^(?P<index>[1-9][0-9]*)/(?P<total>[1-9][0-9]*)$")
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
ROOT = Path(__file__).resolve().parents[2]


def _fail(code: str, message: str) -> int:
    print(f"{code}: {message}", file=sys.stderr)
    return 1


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def _resolve_ref(repo: str, ref: str) -> str:
    if SHA_RE.match(ref.strip().lower()):
        return ref.strip().lower()
    try:
        return subprocess.check_output(
            ["gh", "api", f"repos/{repo}/commits/{ref}", "--jq", ".sha"],
            text=True,
            stderr=subprocess.STDOUT,
        ).strip().lower()
    except Exception as exc:
        raise RuntimeError(f"could not resolve {repo}@{ref}: {exc}") from exc


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
    if args.expected_head_sha:
        expected = args.expected_head_sha.strip().lower()
        if not SHA_RE.match(expected):
            return _fail("PRELAUNCH_EXPECTED_HEAD_INVALID", "expected head SHA must be 40 lowercase hex chars.")
        try:
            actual = _resolve_ref(args.repo, args.ref)
        except RuntimeError as exc:
            return _fail("PRELAUNCH_REF_RESOLVE_FAILED", str(exc))
        if actual != expected:
            return _fail("PRELAUNCH_REF_SHA_MISMATCH", f"dispatch ref {args.ref} resolves to {actual}, expected {expected}.")

    if args.surface == "pro":
        workflow = _read(ROOT / ".github" / "workflows" / "swebench_pro_full.yml")
        if '"$GT_HARNESS_PYTHON" benchmarks/swebench/run_mini_gt_pro_v10.py' not in workflow:
            return _fail("PRELAUNCH_PRO_HARNESS_PYTHON_MISSING", "Pro workflow must run the native runner via GT_HARNESS_PYTHON.")
        if "PRO_HARNESS_READY" not in workflow or 'test -x "$GT_HARNESS_PYTHON"' not in workflow:
            return _fail("PRELAUNCH_PRO_HARNESS_READY_MISSING", "Pro workflow must assert the runner interpreter before trial.")
        if "load_dataset" in workflow or "huggingface" in workflow.lower():
            return _fail("PRELAUNCH_PRO_HF_RUNTIME_FORBIDDEN", "Pro workflow must not depend on Hugging Face at trial time.")
    else:
        workflow = _read(ROOT / ".github" / "workflows" / "deepswe_full.yml")
        if '"$GT_HARNESS_PIER" run' not in workflow or 'test -x "$GT_HARNESS_PIER"' not in workflow:
            return _fail("PRELAUNCH_DEEPSWE_HARNESS_PIER_MISSING", "DeepSWE workflow must run pier via explicit GT_HARNESS_PIER.")

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
    parser.add_argument("--expected-head-sha", default="", help="Optional audited commit SHA expected for dispatch.")
    return _audit(parser.parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
