#!/usr/bin/env python3
"""Pull pre-built Docker images for the first 50 SWE-bench Lite test instances."""
import subprocess
import re
import sys

TAG_PREFIX = "62c2e7c-sweb.eval.x86_64"
REPO = "ghcr.io/openhands/eval-agent-server"


def get_existing_instance_ids():
    """Get instance IDs that already have local Docker images."""
    result = subprocess.run(
        ["docker", "images", "--format", "{{.Tag}}"],
        capture_output=True, text=True,
    )
    existing = set()
    for tag in result.stdout.strip().split("\n"):
        m = re.search(r"62c2e7c-sweb\.eval\.x86_64\.(.+)-source-minimal", tag)
        if m:
            raw = m.group(1)
            parts = raw.split("_1776_")
            if len(parts) == 2:
                iid = parts[0].replace("_", "-") + "__" + parts[1]
                existing.add(iid)
    return existing


def instance_id_to_tag(iid):
    """Convert instance ID to Docker image tag."""
    parts = iid.split("__")
    if len(parts) != 2:
        return None
    org = parts[0].replace("-", "_")
    return f"{REPO}:{TAG_PREFIX}.{org}_1776_{parts[1]}-source-minimal"


def get_first_n_ids(n=50):
    """Get first N instance IDs from SWE-bench Lite test set."""
    try:
        from datasets import load_dataset
        ds = load_dataset("princeton-nlp/SWE-bench_Lite", split="test")
        return [ds[i]["instance_id"] for i in range(min(n, len(ds)))]
    except Exception as e:
        print(f"WARNING: Could not load dataset via HF: {e}")
        # Fallback: try all 300
        try:
            from datasets import load_dataset as ld
            ds = ld("princeton-nlp/SWE-bench_Lite", split="test")
            return [ds[i]["instance_id"] for i in range(min(n, len(ds)))]
        except Exception:
            pass
        # Fallback: read from file if available
        try:
            with open("/tmp/first_50_ids.txt") as f:
                return [l.strip() for l in f if l.strip()]
        except FileNotFoundError:
            return []


def main():
    existing = get_existing_instance_ids()
    print(f"Already have {len(existing)} images locally")

    target_ids = get_first_n_ids(50)
    print(f"Target: {len(target_ids)} instances from SWE-bench Lite")

    runnable = [i for i in target_ids if i in existing]
    missing = [i for i in target_ids if i not in existing]
    print(f"Already runnable: {len(runnable)}")
    print(f"Need to pull: {len(missing)}")

    for iid in missing:
        tag = instance_id_to_tag(iid)
        if not tag:
            print(f"  SKIP (bad ID): {iid}")
            continue
        try:
            r = subprocess.run(
                ["docker", "pull", tag],
                capture_output=True, text=True, timeout=120,
            )
            if r.returncode == 0:
                runnable.append(iid)
                print(f"  PULLED: {iid}")
            else:
                print(f"  UNAVAILABLE: {iid}")
        except subprocess.TimeoutExpired:
            print(f"  TIMEOUT: {iid}")

    # Write runnable list
    out_file = "/tmp/runnable_50_instances.txt"
    with open(out_file, "w") as f:
        for i in sorted(runnable):
            f.write(i + "\n")
    print(f"\nTotal runnable: {len(runnable)}/{len(target_ids)}")
    print(f"Written to {out_file}")


if __name__ == "__main__":
    main()
