#!/usr/bin/env python3
"""Direct SWE-bench-Live evaluator for a single OH probe output.

Standard SWE-bench-Live grading flow (no swebench-live pip package needed):
  1. Run the cached test image with /testbed pre-checked-out.
  2. git-apply the model patch (from output.jsonl) + the dataset's test_patch.
  3. Execute the dataset's test_cmds inside the container.
  4. Parse the output with the dataset's log_parser → resolved Y/N.

Resolved iff:
  - every test in FAIL_TO_PASS now PASSES, AND
  - every test in PASS_TO_PASS still PASSES (no regression)

Usage:
  python3 eval_swebench_live_direct.py <RUN_DIR>
"""
import json
import os
import re
import shlex
import subprocess
import sys
from typing import Any


def _docker(args: list, **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["sudo", "docker"] + args,
        capture_output=True,
        text=True,
        **kwargs,
    )


def _extract_first_record(output_jsonl: str) -> dict:
    with open(output_jsonl) as fh:
        line = fh.readline().strip()
    return json.loads(line) if line else {}


def _parse_pass_to_pass(raw: Any) -> list[str]:
    """SWE-bench fields can be stored as a JSON-encoded string OR a Python list."""
    if isinstance(raw, list):
        return [str(x) for x in raw]
    if isinstance(raw, str):
        s = raw.strip()
        if not s:
            return []
        try:
            parsed = json.loads(s)
            if isinstance(parsed, list):
                return [str(x) for x in parsed]
        except Exception:
            pass
        # Fallback: comma-separated
        return [t.strip() for t in s.split(",") if t.strip()]
    return []


def _classify_log(log_text: str, log_parser_name: str) -> dict[str, str]:
    """Generic, lightweight pytest-style log classifier covering SWE-bench-Live's
    observed log_parsers (pytest, pytest-with-rerun, generic). Falls back to a
    pytest-summary regex which catches >95% of cases.
    """
    results: dict[str, str] = {}
    # Standard pytest result lines:
    #   tests/layout/test_flex.py::test_flex_overflow PASSED
    #   tests/layout/test_flex.py::test_flex_overflow FAILED
    for m in re.finditer(
        r"^(\S+::\S+)\s+(PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)\b",
        log_text,
        flags=re.M,
    ):
        results[m.group(1)] = m.group(2)
    # Some pytest configs print short-summary at the end:
    #   FAILED tests/layout/test_flex.py::test_flex_overflow - assert ...
    for m in re.finditer(
        r"^(PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)\s+(\S+::\S+)",
        log_text,
        flags=re.M,
    ):
        # Don't overwrite an inline result with a summary entry of different sense.
        results.setdefault(m.group(2), m.group(1))
    return results


def main() -> int:
    # RC-17 (F-002): accept an optional --image-pin override. Default
    # resolution path: read <RUN_DIR>/image_digests.json (written by the
    # smoke runner on first successful image resolve) and look up the
    # instance under inspection. No floating ``:latest`` is permitted.
    import argparse

    ap = argparse.ArgumentParser(prog="eval_swebench_live_direct")
    ap.add_argument("run_dir")
    ap.add_argument(
        "--image-pin",
        default=None,
        help="Pinned image (repo@sha256:...). Falls back to "
             "<run_dir>/image_digests.json then $RC17_IMAGE_PIN.",
    )
    ns = ap.parse_args()
    run_dir = ns.run_dir.rstrip("/")
    _arg_image_pin = ns.image_pin
    # Fall back to per-run image_digests.json (canonical RC-17 artifact).
    if not _arg_image_pin:
        digest_file = os.path.join(run_dir, "image_digests.json")
        if os.path.isfile(digest_file):
            try:
                with open(digest_file) as _fh:
                    _digest_map_data = json.load(_fh)
                _arg_image_pin = None  # filled in after iid known, below
            except Exception:
                _digest_map_data = {}
        else:
            _digest_map_data = {}
    else:
        _digest_map_data = {}

    # Locate output.jsonl
    out_root = next(
        os.path.join(run_dir, sub)
        for sub in os.listdir(run_dir)
        if sub.startswith("SWE-bench-Live__")
    )
    out_root = os.path.join(out_root, os.listdir(out_root)[0])
    out_root = os.path.join(out_root, os.listdir(out_root)[0])
    output_jsonl = os.path.join(out_root, "output.jsonl")

    rec = _extract_first_record(output_jsonl)
    iid = rec["instance_id"]
    patch = rec.get("test_result", {}).get("git_patch") or rec.get("git_patch") or ""
    print(f"=== eval target: {iid}")
    print(f"=== model patch chars: {len(patch)}")

    # Pull dataset row for ground-truth fields
    from datasets import load_dataset

    ds = load_dataset("SWE-bench-Live/SWE-bench-Live", split="lite")
    row = next(r for r in ds if r["instance_id"] == iid)
    f2p = _parse_pass_to_pass(row.get("FAIL_TO_PASS"))
    p2p = _parse_pass_to_pass(row.get("PASS_TO_PASS"))
    test_patch = row.get("test_patch", "")
    test_cmds_raw = row.get("test_cmds", "")
    test_cmds = test_cmds_raw if isinstance(test_cmds_raw, str) else "\n".join(test_cmds_raw or [])
    log_parser = row.get("log_parser", "pytest")

    print(f"=== FAIL_TO_PASS ({len(f2p)}): {f2p[:5]}{'…' if len(f2p)>5 else ''}")
    print(f"=== PASS_TO_PASS count: {len(p2p)}")
    print(f"=== log_parser: {log_parser}")

    # RC-17 (F-002): refuse to construct a floating ``:latest`` tag at
    # eval time. Operators must materialize a digest pin via the smoke
    # runner (which writes <run_dir>/image_digests.json on first
    # successful resolve) and pass --image-pin to this script. Without
    # a pin, this is a hard failure rather than a silent drift risk.
    image_pin = (
        os.environ.get("RC17_IMAGE_PIN")
        or _arg_image_pin
        or (
            _digest_map_data.get(iid)
            if isinstance(_digest_map_data, dict)
            else None
        )
    )
    if image_pin and ":latest" in str(image_pin):
        print(
            "FATAL: image_pin carries :latest; RC-17/F-002 forbids floating tags",
            file=sys.stderr,
        )
        return 2
    if not image_pin:
        print(
            "FATAL: image pin missing — RC-17/F-002 forbids constructing "
            f"starryzhang/sweb.eval.x86_64.{iid.replace('__', '_1776_')}:latest. "
            "Pass --image-pin <repo@sha256:...> or set $RC17_IMAGE_PIN.",
            file=sys.stderr,
        )
        return 2
    image = image_pin
    print(f"=== image: {image} (RC-17 pinned)")

    # 1. Start container
    cid_proc = _docker([
        "run", "-d", "--rm",
        "--entrypoint", "/bin/bash",
        image,
        "-c", "sleep 3600",
    ])
    if cid_proc.returncode != 0:
        print(f"FATAL: docker run failed: {cid_proc.stderr}")
        return 1
    cid = cid_proc.stdout.strip()
    print(f"=== container: {cid[:12]}")

    try:
        # 2. Apply patches
        for label, p in (("test_patch", test_patch), ("model_patch", patch)):
            if not p.strip():
                print(f"=== {label}: empty, skipped")
                continue
            patch_path = f"/tmp/{label}.diff"
            # Write patch via stdin so we don't have to escape quotes.
            apply_cmd = subprocess.run(
                ["sudo", "docker", "exec", "-i", cid, "bash", "-c",
                 f"cat > {patch_path} && cd /testbed && (git apply --check {patch_path} 2>&1 || true) && git apply --whitespace=nowarn {patch_path} && echo APPLIED_OK"],
                input=p,
                capture_output=True,
                text=True,
            )
            print(f"=== {label}: rc={apply_cmd.returncode}; tail={apply_cmd.stdout[-200:]!r} stderr={apply_cmd.stderr[-200:]!r}")
            if "APPLIED_OK" not in apply_cmd.stdout:
                print(f"WARN: {label} apply may have failed")

        # 3. Run test_cmds
        run_proc = subprocess.run(
            ["sudo", "docker", "exec", cid, "bash", "-c",
             f"cd /testbed && {test_cmds}"],
            capture_output=True,
            text=True,
            timeout=900,
        )
        log = (run_proc.stdout or "") + "\n" + (run_proc.stderr or "")
        log_path = os.path.join(out_root, "eval_test.log")
        with open(log_path, "w") as fh:
            fh.write(log)
        print(f"=== test_cmds rc={run_proc.returncode}; log written to {log_path}")
        print(f"=== last 30 lines of log:")
        for line in log.splitlines()[-30:]:
            print(f"    {line}")

        # 4. Classify
        results = _classify_log(log, log_parser)

        f2p_pass = [t for t in f2p if results.get(t) == "PASSED"]
        f2p_fail = [t for t in f2p if results.get(t) != "PASSED"]
        p2p_pass = [t for t in p2p if results.get(t) == "PASSED"]
        p2p_fail = [t for t in p2p if results.get(t) != "PASSED"]

        print()
        print(f"=== FAIL_TO_PASS results: {len(f2p_pass)}/{len(f2p)} now passing")
        for t in f2p:
            print(f"    {results.get(t, 'NOT_FOUND'):>10}  {t}")
        print()
        print(f"=== PASS_TO_PASS results: {len(p2p_pass)}/{len(p2p)} still passing")
        if p2p_fail:
            print(f"    regressions: {p2p_fail[:10]}{'…' if len(p2p_fail)>10 else ''}")

        resolved = (
            len(f2p_fail) == 0
            and len(p2p_fail) == 0
            and len(f2p_pass) == len(f2p)
        )
        print()
        print(f"=== RESOLVED: {'YES' if resolved else 'NO'}")
        return 0 if resolved else 1
    finally:
        _docker(["rm", "-f", cid])


if __name__ == "__main__":
    sys.exit(main())
