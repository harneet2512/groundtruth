#!/usr/bin/env python3
"""Run GT on 1 Live Lite task with 1 worker."""
import os, sys
sys.path.insert(0, os.getcwd())
from datasets import load_dataset
from evaluation.benchmarks.swe_bench.run_infer import (
    filter_dataset, set_dataset_type, get_llm_config_arg,
    make_metadata, prepare_dataset, run_evaluation, process_instance,
)

TASK = sys.argv[1] if len(sys.argv) > 1 else "python-babel__babel-1164"
OUT = os.path.expanduser("~/results/gt_1task")

ds = load_dataset("SWE-bench-Live/SWE-bench-Live", split="lite")
set_dataset_type("SWE-bench-Live/SWE-bench-Live")
tests = filter_dataset(ds.to_pandas(), "instance_id")
tests = tests[tests["instance_id"] == TASK]
print(f"Task: {TASK}, found: {len(tests)}")
if len(tests) == 0:
    print("Task not found!")
    sys.exit(1)

llm = get_llm_config_arg("qwen3")
llm.log_completions = True
llm.modify_params = False
meta = make_metadata(llm, "SWE-bench-Live/SWE-bench-Live", "CodeActAgent", 50, "gt_1task", OUT, details={"mode": "swe"})
out_file = os.path.join(meta.eval_output_dir, "output.jsonl")
instances = prepare_dataset(tests, out_file, eval_n_limit=None)
for col in ["PASS_TO_PASS", "FAIL_TO_PASS"]:
    if col in instances.columns:
        instances[col] = instances[col].apply(str)

run_evaluation(instances, meta, out_file, num_workers=1, process_instance_func=process_instance, max_retries=3)
