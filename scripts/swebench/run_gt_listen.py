#!/usr/bin/env python3
"""Run GT listening test on specific Live Lite tasks."""
import os
import sys

sys.path.insert(0, os.getcwd())

from datasets import load_dataset
from evaluation.benchmarks.swe_bench.run_infer import (
    filter_dataset, set_dataset_type, get_llm_config_arg,
    make_metadata, prepare_dataset, run_evaluation, process_instance,
)

TASKS = [
    "reflex-dev__reflex-4711",
    "keras-team__keras-20396",
    "python-babel__babel-1164",
    "pdm-project__pdm-3419",
    "python-telegram-bot__python-telegram-bot-4673",
    "beetbox__beets-5457",
    "pypsa__pypsa-1112",
    "run-llama__llama_deploy-372",
    "stanfordnlp__dspy-1801",
    "mikedh__trimesh-2363",
]

OUTPUT_DIR = os.path.expanduser("~/results/gt_listen_test")

ds = load_dataset("SWE-bench-Live/SWE-bench-Live", split="lite")
set_dataset_type("SWE-bench-Live/SWE-bench-Live")
tests = filter_dataset(ds.to_pandas(), "instance_id")

# Filter to just our 10 tasks
tests = tests[tests["instance_id"].isin(TASKS)]
print(f"Tasks: {len(tests)}")
for iid in tests["instance_id"]:
    print(f"  {iid}")

llm_config = get_llm_config_arg("qwen3")
llm_config.log_completions = True
llm_config.modify_params = False

metadata = make_metadata(
    llm_config,
    "SWE-bench-Live/SWE-bench-Live",
    "CodeActAgent",
    50,
    "gt_listen_test",
    OUTPUT_DIR,
    details={"mode": "swe"},
)

output_file = os.path.join(metadata.eval_output_dir, "output.jsonl")
instances = prepare_dataset(tests, output_file, eval_n_limit=None)

for col in ["PASS_TO_PASS", "FAIL_TO_PASS"]:
    if col in instances.columns:
        instances[col] = instances[col].apply(lambda x: str(x))

run_evaluation(
    instances,
    metadata,
    output_file,
    num_workers=2,
    process_instance_func=process_instance,
    max_retries=3,
)
