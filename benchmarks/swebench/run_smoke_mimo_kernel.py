"""Paired baseline + kernel smoke on SWE-bench-Live-Lite via OpenRouter MiMo.

Mirrors run_smoke_mimo.py exactly, then applies the kernel monkey-patch from
oh_gt_kernel_wrapper.install_kernel_patch() before run_evaluation() so the
kernel post-edit hook fires inside each container.

Usage: run_smoke_mimo_kernel.py <arm_name> <workers> <maxiter> <iids_file>
        ARM env: GT_KERNEL_ARM=control|kernel (default kernel)
        Brief env: GT_PRETASK_PATH=/path/to/v7_brief.py (existing v7.3 flow)
"""
from __future__ import annotations

import os
import sys

# install patches BEFORE importing the harness so the monkey-patch order is
# right (oh_gt_kernel_wrapper imports SWEBenchEvaluation when the patch runs)
sys.path.insert(0, os.path.expanduser("~/gt-kernel/scripts/swebench"))

from datasets import load_dataset
from evaluation.benchmarks.swe_bench.run_infer import (
    filter_dataset,
    get_llm_config_arg,
    make_metadata,
    prepare_dataset,
    process_instance,
    run_evaluation,
    set_dataset_type,
)

# Apply kernel patch immediately after harness import; before any
# evaluation runs.
import oh_gt_kernel_wrapper

if not oh_gt_kernel_wrapper.install_kernel_patch():
    print("WARN: kernel patch did not install; continuing without it.")

ARM = sys.argv[1]
WORKERS = int(sys.argv[2]) if len(sys.argv) > 2 else 4
MAXITER = int(sys.argv[3]) if len(sys.argv) > 3 else 100
IIDS_FILE = sys.argv[4] if len(sys.argv) > 4 else "/home/Lenovo/instance_ids_smoke10.txt"

with open(IIDS_FILE) as fh:
    TASKS = [line.strip() for line in fh if line.strip()]

OUT = os.path.expanduser(f"~/results/{ARM}")

ds = load_dataset("SWE-bench-Live/SWE-bench-Live", split="lite")
set_dataset_type("SWE-bench-Live/SWE-bench-Live")
tests = filter_dataset(ds.to_pandas(), "instance_id")
tests = tests[tests["instance_id"].isin(TASKS)]
print(f"ARM={ARM} TASKS={len(tests)}/{len(TASKS)} WORKERS={WORKERS} ITER={MAXITER}")
print(f"GT_KERNEL_ARM={os.environ.get('GT_KERNEL_ARM', 'kernel')}")
print(f"GT_PRETASK_PATH={os.environ.get('GT_PRETASK_PATH', '<unset>')}")

llm = get_llm_config_arg("mimo")
llm.log_completions = True
llm.modify_params = False
meta = make_metadata(
    llm,
    "SWE-bench-Live/SWE-bench-Live",
    "CodeActAgent",
    MAXITER,
    ARM,
    OUT,
    details={"mode": "swe", "kernel_arm": os.environ.get("GT_KERNEL_ARM", "kernel")},
)
out_file = os.path.join(meta.eval_output_dir, "output.jsonl")
instances = prepare_dataset(tests, out_file, eval_n_limit=None)
for col in ["PASS_TO_PASS", "FAIL_TO_PASS"]:
    if col in instances.columns:
        instances[col] = instances[col].apply(str)
run_evaluation(
    instances,
    meta,
    out_file,
    num_workers=WORKERS,
    process_instance_func=process_instance,
    max_retries=2,
)
