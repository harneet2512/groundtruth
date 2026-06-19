#!/usr/bin/env python3
"""Minimal repro: does our monkey-patch on run_infer.get_instruction
survive a multiprocessing.Pool worker fork?

Run on t0:
  cd /home/ubuntu/OpenHands
  PYTHONPATH=/home/ubuntu/Groundtruth:/home/ubuntu/Groundtruth/src:/home/ubuntu/OpenHands \
  /home/ubuntu/.local/bin/poetry run python /home/ubuntu/Groundtruth/scripts/swebench/diag_patch_propagation.py
"""
import os
import sys
import multiprocessing as mp


def patched_get_instruction(instance, metadata):
    pid = os.getpid()
    with open("/tmp/diag_patch_propagation.log", "a") as fh:
        fh.write(f"[pid {pid}] PATCHED get_instruction called instance={instance}\n")
    return f"PATCHED_RESULT_pid{pid}"


def install_patch():
    import evaluation.benchmarks.swe_bench.run_infer as ri
    if not hasattr(ri, "_orig_get_instruction"):
        ri._orig_get_instruction = ri.get_instruction
    ri.get_instruction = patched_get_instruction
    pid = os.getpid()
    with open("/tmp/diag_patch_propagation.log", "a") as fh:
        fh.write(f"[pid {pid}] PATCH INSTALLED ri.get_instruction={ri.get_instruction!r}\n")


def worker_call(instance):
    """Runs in the multiprocessing worker. Calls run_infer.get_instruction."""
    import evaluation.benchmarks.swe_bench.run_infer as ri
    pid = os.getpid()
    with open("/tmp/diag_patch_propagation.log", "a") as fh:
        fh.write(f"[pid {pid}] WORKER START ri.get_instruction={ri.get_instruction!r}\n")
        fh.write(f"[pid {pid}] WORKER patched_get_instruction in module={hasattr(ri,'_orig_get_instruction')}\n")
    try:
        result = ri.get_instruction(instance, None)
    except Exception as e:
        result = f"ERROR: {e}"
    with open("/tmp/diag_patch_propagation.log", "a") as fh:
        fh.write(f"[pid {pid}] WORKER RESULT={result!r}\n")
    return result


def main():
    log = "/tmp/diag_patch_propagation.log"
    if os.path.exists(log):
        os.remove(log)
    with open(log, "a") as fh:
        fh.write(f"[parent pid {os.getpid()}] start_method={mp.get_start_method()}\n")

    install_patch()

    with open(log, "a") as fh:
        fh.write(f"[parent pid {os.getpid()}] dispatching Pool(1)\n")

    with mp.Pool(processes=1) as pool:
        results = pool.map(worker_call, ["INSTANCE_A"])

    with open(log, "a") as fh:
        fh.write(f"[parent pid {os.getpid()}] pool returned: {results!r}\n")

    print("=== /tmp/diag_patch_propagation.log ===")
    with open(log) as fh:
        sys.stdout.write(fh.read())


if __name__ == "__main__":
    main()
