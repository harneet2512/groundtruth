#!/usr/bin/env python3
"""Batch evaluator for SWE-bench-Live output.jsonl files.

Evaluates each task by:
1. Starting the Docker container (starryzhang image)
2. Applying model_patch + test_patch
3. Running test_cmds
4. Checking FAIL_TO_PASS tests pass and PASS_TO_PASS don't regress

Usage:
  python3 eval_batch.py <output_t0.jsonl> [output_v1.jsonl] --output results.json

Runs eval in parallel (one Docker container per task, up to --workers).
"""
import argparse
import json
import os
import re
import subprocess
import sys
from concurrent.futures import ProcessPoolExecutor, as_completed


def docker(args: list, timeout: int = 600, input: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["docker"] + args,
        capture_output=True, text=True, timeout=timeout, input=input,
    )


def process_git_patch(patch: str) -> str:
    """Clean patch — same logic as OH's process_git_patch in eval_infer.py."""
    if not isinstance(patch, str) or not patch.strip():
        return ""
    patch = patch.replace("\r\n", "\n")
    lines = patch.split("\n")
    for i, line in enumerate(lines):
        if line.startswith("diff --git"):
            patch = "\n".join(lines[i:])
            break
    return patch.rstrip() + "\n"


def parse_list_field(raw) -> list[str]:
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
        return [t.strip() for t in s.split(",") if t.strip()]
    return []


def classify_log(log_text: str) -> dict[str, str]:
    results: dict[str, str] = {}
    for m in re.finditer(
        r"^(\S+::\S+)\s+(PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)\b",
        log_text, flags=re.M,
    ):
        results[m.group(1)] = m.group(2)
    for m in re.finditer(
        r"^(PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)\s+(\S+::\S+)",
        log_text, flags=re.M,
    ):
        results.setdefault(m.group(2), m.group(1))
    return results


def eval_single(iid: str, patch: str, dataset_row: dict) -> dict:
    """Evaluate one task. Returns {instance_id, resolved, reason, ...}"""
    f2p = parse_list_field(dataset_row.get("FAIL_TO_PASS"))
    p2p = parse_list_field(dataset_row.get("PASS_TO_PASS"))
    test_patch = dataset_row.get("test_patch", "")
    test_cmds_raw = dataset_row.get("test_cmds", "")
    test_cmds = test_cmds_raw if isinstance(test_cmds_raw, str) else "\n".join(test_cmds_raw or [])

    if not patch.strip():
        return {"instance_id": iid, "resolved": False, "reason": "empty_patch"}

    # Image name
    slug = iid.replace("__", "_1776_")
    image = f"starryzhang/sweb.eval.x86_64.{slug}:latest"

    # Start container
    cid_proc = docker(["run", "-d", "--rm", "--entrypoint", "/bin/bash", image, "-c", "sleep 3600"])
    if cid_proc.returncode != 0:
        return {"instance_id": iid, "resolved": False, "reason": f"docker_start_failed: {cid_proc.stderr[:200]}"}
    cid = cid_proc.stdout.strip()

    try:
        # Apply patches via docker cp (stdin pipe can corrupt patches)
        import tempfile
        for label, p in [("test_patch", test_patch), ("model_patch", patch)]:
            if not p.strip():
                continue
            with tempfile.NamedTemporaryFile(mode="w", suffix=".diff", delete=False) as tf:
                tf.write(p)
                tf.flush()
                tmp_path = tf.name
            try:
                docker(["cp", tmp_path, f"{cid}:/tmp/{label}.diff"], timeout=30)
                r = docker(["exec", cid, "bash", "-c",
                    f"cd /testbed && git apply --allow-empty /tmp/{label}.diff"], timeout=60)
                if r.returncode != 0:
                    r2 = docker(["exec", cid, "bash", "-c",
                        f"cd /testbed && git apply --allow-empty --3way /tmp/{label}.diff"], timeout=60)
                    if r2.returncode != 0:
                        return {"instance_id": iid, "resolved": False, "reason": f"{label}_failed: {r2.stderr[:200]}"}
            finally:
                os.unlink(tmp_path)

        # Run tests
        r = docker(["exec", cid, "bash", "-c", f"cd /testbed && {test_cmds}"], timeout=600)
        log = r.stdout + "\n" + r.stderr

        # Parse results
        test_results = classify_log(log)

        # Check FAIL_TO_PASS
        f2p_passed = 0
        f2p_failed = 0
        for t in f2p:
            status = test_results.get(t, "MISSING")
            if status == "PASSED":
                f2p_passed += 1
            else:
                f2p_failed += 1

        # Check PASS_TO_PASS
        p2p_regressed = 0
        for t in p2p:
            status = test_results.get(t, "PASSED")
            if status in ("FAILED", "ERROR"):
                p2p_regressed += 1

        resolved = f2p_failed == 0 and f2p_passed == len(f2p) and p2p_regressed == 0 and len(f2p) > 0

        return {
            "instance_id": iid,
            "resolved": resolved,
            "reason": "resolved" if resolved else f"f2p:{f2p_passed}/{len(f2p)}_p2p_reg:{p2p_regressed}",
            "f2p_total": len(f2p),
            "f2p_passed": f2p_passed,
            "p2p_regressed": p2p_regressed,
        }

    except subprocess.TimeoutExpired:
        return {"instance_id": iid, "resolved": False, "reason": "timeout"}
    except Exception as e:
        return {"instance_id": iid, "resolved": False, "reason": str(e)[:200]}
    finally:
        docker(["kill", cid], timeout=10)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("inputs", nargs="+", help="output.jsonl files")
    parser.add_argument("--output", default="eval_results.json")
    parser.add_argument("--workers", type=int, default=4)
    args = parser.parse_args()

    # Load all tasks from input files, clean patches with process_git_patch
    tasks = []
    for path in args.inputs:
        with open(path, encoding="utf-8") as f:
            for line in f:
                obj = json.loads(line)
                iid = obj["instance_id"]
                raw_patch = obj.get("test_result", {}).get("git_patch", "") or ""
                patch = process_git_patch(raw_patch)
                tasks.append((iid, patch))

    print(f"Loaded {len(tasks)} tasks from {len(args.inputs)} files")

    # Load dataset for ground truth
    from datasets import load_dataset
    ds = load_dataset("SWE-bench-Live/SWE-bench-Live", split="test")
    ds_map = {r["instance_id"]: r for r in ds}

    # Also check lite split
    ds_lite = load_dataset("SWE-bench-Live/SWE-bench-Live", split="lite")
    for r in ds_lite:
        ds_map[r["instance_id"]] = r

    results = []
    resolved_count = 0

    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        futures = {}
        for iid, patch in tasks:
            row = ds_map.get(iid)
            if not row:
                results.append({"instance_id": iid, "resolved": False, "reason": "not_in_dataset"})
                continue
            fut = pool.submit(eval_single, iid, patch, dict(row))
            futures[fut] = iid

        for fut in as_completed(futures):
            result = fut.result()
            results.append(result)
            status = "RESOLVED" if result["resolved"] else "NOT_RESOLVED"
            print(f"  {result['instance_id']}: {status} ({result.get('reason', '')})")
            if result["resolved"]:
                resolved_count += 1

    # Write results
    with open(args.output, "w") as f:
        json.dump({"total": len(results), "resolved": resolved_count, "results": results}, f, indent=2)

    print(f"\n=== {resolved_count}/{len(results)} RESOLVED ===")
    print(f"Results: {args.output}")
    return 0 if resolved_count > 0 else 1


if __name__ == "__main__":
    sys.exit(main())
