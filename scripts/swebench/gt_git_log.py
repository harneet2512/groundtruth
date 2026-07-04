#!/usr/bin/env python3
"""Structured git operation logger for the industrial/SOTA evidence trail.

Every git command run through here appends one JSONL record to
.groundtruth/git_ops.jsonl (command, rc, stdout/stderr tails, duration, cwd) so
git operations are READABLE and AUDITABLE as evidence — not lost to the shell.

Feeds:
  - 27_todos #4  (run_manifest.commit_parity.status == match): commit_parity()
  - 27_todos #14 (artifact sha256 hashes match bytes): tracked_artifact_hashes()

Usable two ways:
  CLI:    python scripts/swebench/gt_git_log.py head
          python scripts/swebench/gt_git_log.py parity --expect <sha>
          python scripts/swebench/gt_git_log.py status
  import: from gt_git_log import run_git, commit_parity, head_sha
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
LOG_PATH = ROOT / ".groundtruth" / "git_ops.jsonl"
_TAIL = 4000  # cap stored stdout/stderr so the log stays readable


def _append(record: dict[str, Any]) -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOG_PATH.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, sort_keys=True) + "\n")


def run_git(args: list[str], *, cwd: Path = ROOT, timeout_s: int = 120, label: str = "") -> dict[str, Any]:
    """Run one git command, log a structured JSONL record, return the result dict."""
    start = time.time()
    try:
        proc = subprocess.run(
            ["git", *args], cwd=str(cwd), text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout_s,
        )
        rc, out, err, timed_out = proc.returncode, proc.stdout or "", proc.stderr or "", False
    except subprocess.TimeoutExpired as exc:
        rc, timed_out = None, True
        out = (exc.stdout or "") if isinstance(exc.stdout, str) else ""
        err = (exc.stderr or "") if isinstance(exc.stderr, str) else ""
    record = {
        "schema": "gt.git_op.v1",
        "label": label or (args[0] if args else "git"),
        "argv": ["git", *args],
        "cwd": str(cwd),
        "rc": rc,
        "timed_out": timed_out,
        "duration_s": round(time.time() - start, 3),
        "stdout_tail": out[-_TAIL:],
        "stderr_tail": err[-_TAIL:],
    }
    _append(record)
    return record


def head_sha(cwd: Path = ROOT) -> str:
    r = run_git(["rev-parse", "HEAD"], cwd=cwd, label="head_sha")
    return (r["stdout_tail"] or "").strip()


def commit_parity(expected: str, cwd: Path = ROOT) -> dict[str, Any]:
    """27_todos #4: is HEAD the expected commit? Logs + returns a parity verdict."""
    actual = head_sha(cwd)
    expected = (expected or "").strip()
    status = "match" if (actual and expected and actual.startswith(expected[:12]) or actual == expected) else "mismatch"
    verdict = {
        "schema": "gt.commit_parity.v1",
        "expected": expected,
        "actual": actual,
        "status": status if expected else "no_expected_given",
    }
    _append({"schema": "gt.git_op.v1", "label": "commit_parity", "verdict": verdict})
    return verdict


def status_porcelain(cwd: Path = ROOT) -> dict[str, Any]:
    r = run_git(["status", "--porcelain"], cwd=cwd, label="status_porcelain")
    lines = [ln for ln in (r["stdout_tail"] or "").splitlines() if ln.strip()]
    return {"schema": "gt.git_status.v1", "dirty_count": len(lines), "entries": lines[:200]}


def tracked_artifact_hashes(paths: list[str], cwd: Path = ROOT) -> dict[str, Any]:
    """27_todos #14: sha256 of the given artifact files (bytes on disk), logged."""
    out: dict[str, str | None] = {}
    for rel in paths:
        p = (cwd / rel)
        if p.is_file():
            h = hashlib.sha256()
            with p.open("rb") as fh:
                for chunk in iter(lambda: fh.read(1 << 20), b""):
                    h.update(chunk)
            out[rel] = h.hexdigest()
        else:
            out[rel] = None
    rec = {"schema": "gt.artifact_hashes.v1", "hashes": out}
    _append({"schema": "gt.git_op.v1", "label": "artifact_hashes", **rec})
    return rec


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="structured git op logger -> .groundtruth/git_ops.jsonl")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("head")
    sub.add_parser("status")
    p_par = sub.add_parser("parity")
    p_par.add_argument("--expect", required=True)
    p_hash = sub.add_parser("hashes")
    p_hash.add_argument("paths", nargs="+")
    p_raw = sub.add_parser("run")
    p_raw.add_argument("args", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)

    if args.cmd == "head":
        print(json.dumps({"head": head_sha()}, indent=2))
    elif args.cmd == "status":
        print(json.dumps(status_porcelain(), indent=2, sort_keys=True))
    elif args.cmd == "parity":
        print(json.dumps(commit_parity(args.expect), indent=2, sort_keys=True))
    elif args.cmd == "hashes":
        print(json.dumps(tracked_artifact_hashes(args.paths), indent=2, sort_keys=True))
    elif args.cmd == "run":
        print(json.dumps(run_git(args.args), indent=2, sort_keys=True))
    print(f"[logged -> {LOG_PATH}]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
