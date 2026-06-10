#!/usr/bin/env python3
"""GT v1.1 runner for OpenHands v0.44.0.

Monkey-patches process_instance to inject gt_hook.py into the runtime
after initialization, then runs the standard SWE-bench evaluation.

Usage (from oh-benchmarks/ dir):
    python /path/to/oh_gt_v11_runner.py \
        --llm-config .llm_config/vertex_qwen3.json \
        --eval-output-dir ~/results/gt_v11 \
        --eval-num-workers 2 --max-iterations 50 \
        --dataset princeton-nlp/SWE-bench_Verified --split test \
        --filter-instances django__django-10097,django__django-10554
"""

import base64
import os
import sys

GT_HOOK_PATH = os.environ.get(
    "GT_HOOK_PATH",
    os.path.join(os.path.expanduser("~"), "gt_hook.py"),
)
GT_LOG_DIR = os.environ.get("GT_LOG_DIR", "/tmp/gt_logs")


def patch_and_run():
    import evaluation.benchmarks.swe_bench.run_infer as swe_mod
    from evaluation.benchmarks.swe_bench.run_infer import (
        process_instance as _orig_process,
    )

    if not os.path.exists(GT_HOOK_PATH):
        print(f"ERROR: gt_hook.py not found at {GT_HOOK_PATH}")
        sys.exit(1)

    with open(GT_HOOK_PATH, "rb") as f:
        hook_bytes = f.read()
    hook_b64 = base64.b64encode(hook_bytes).decode("ascii")
    CHUNK = 8000
    chunks = [hook_b64[i : i + CHUNK] for i in range(0, len(hook_b64), CHUNK)]
    print(f"GT v1.1: gt_hook.py={len(hook_bytes)} bytes, {len(chunks)} chunks")
    os.makedirs(GT_LOG_DIR, exist_ok=True)

    _orig_init = swe_mod.initialize_runtime
    _orig_complete = swe_mod.complete_runtime

    def _patched_init(runtime, instance, metadata):
        _orig_init(runtime, instance, metadata)
        iid = instance.get("instance_id", "?")
        _inject_hook(runtime, iid)

    def _inject_hook(runtime, iid):
        from openhands.events.action import CmdRunAction
        from openhands.events.observation import CmdOutputObservation

        ok = True
        for i, chunk in enumerate(chunks):
            op = ">" if i == 0 else ">>"
            obs = runtime.run_action(CmdRunAction(command=f"echo -n '{chunk}' {op} /tmp/gt_hook.b64"))
            if not isinstance(obs, CmdOutputObservation) or obs.exit_code != 0:
                print(f"  WARN: chunk {i} failed for {iid}")
                ok = False
                break

        if not ok:
            print(f"  gt_hook injection FAILED: {iid}")
            return

        obs = runtime.run_action(CmdRunAction(
            command="base64 -d /tmp/gt_hook.b64 > /tmp/gt_hook.py && "
                    "chmod +x /tmp/gt_hook.py && rm -f /tmp/gt_hook.b64 && echo GT_HOOK_READY"
        ))
        if "GT_HOOK_READY" in getattr(obs, "content", ""):
            print(f"  gt_hook.py injected: {iid}")
        else:
            print(f"  WARN: injection uncertain: {iid}")

    def _patched_complete(runtime, instance):
        iid = instance.get("instance_id", "?")
        from openhands.events.action import CmdRunAction
        try:
            obs = runtime.run_action(CmdRunAction(command="cat /tmp/gt_hook_log.jsonl 2>/dev/null || true"))
            stdout = getattr(obs, "content", "")
            if stdout and stdout.strip() and stdout.strip() != "true":
                with open(os.path.join(GT_LOG_DIR, f"{iid}.jsonl"), "w") as fh:
                    fh.write(stdout)
                print(f"  hook log: {iid} ({len(stdout)} bytes)")

            obs2 = runtime.run_action(CmdRunAction(command="cat /tmp/gt_hook_stdout.log 2>/dev/null || true"))
            stdout2 = getattr(obs2, "content", "")
            if stdout2 and stdout2.strip() and stdout2.strip() != "true":
                with open(os.path.join(GT_LOG_DIR, f"{iid}_stdout.log"), "w") as fh:
                    fh.write(stdout2)
                print(f"  stdout log: {iid} ({len(stdout2)} bytes)")
        except Exception as e:
            print(f"  WARN: log extraction failed: {iid}: {e}")

        return _orig_complete(runtime, instance)

    swe_mod.initialize_runtime = _patched_init
    swe_mod.complete_runtime = _patched_complete
    # Also patch in the module's globals dict to catch direct name lookups
    swe_mod.__dict__["complete_runtime"] = _patched_complete
    swe_mod.__dict__["initialize_runtime"] = _patched_init
    print("Patched initialize_runtime + complete_runtime for GT v1.1")
    print(f"  verify: init patched={swe_mod.initialize_runtime is _patched_init}")
    print(f"  verify: complete patched={swe_mod.complete_runtime is _patched_complete}")

    # --- Parse args and run evaluation (mirrors __main__ block) ---
    from datasets import load_dataset

    parser = swe_mod.get_parser()
    parser.add_argument("--dataset", type=str, default="princeton-nlp/SWE-bench_Verified")
    parser.add_argument("--split", type=str, default="test")
    parser.add_argument("--filter-instances", type=str, default=None)
    args, _ = parser.parse_known_args()

    dataset = load_dataset(args.dataset, split=args.split)
    swe_mod.set_dataset_type(args.dataset)
    tests = swe_mod.filter_dataset(dataset.to_pandas(), "instance_id")
    print(f"Loaded {len(tests)} tasks from {args.dataset}")

    if args.filter_instances:
        ids = set(args.filter_instances.split(","))
        tests = tests[tests["instance_id"].isin(ids)]
        print(f"Filtered to {len(tests)} tasks")

    llm_config = swe_mod.get_llm_config_arg(args.llm_config)
    if llm_config is None:
        print("ERROR: No LLM config (use --llm-config)")
        sys.exit(1)
    llm_config.log_completions = True
    llm_config.modify_params = False

    metadata = swe_mod.make_metadata(
        llm_config,
        args.dataset,
        args.agent_cls,
        args.max_iterations,
        args.eval_note,
        args.eval_output_dir,
        details={"mode": "swe"},
    )

    output_file = os.path.join(metadata.eval_output_dir, "output.jsonl")
    print(f"Output file: {output_file}")

    instances = swe_mod.prepare_dataset(
        tests, output_file, args.eval_n_limit, eval_ids=args.eval_ids
    )

    if len(instances) > 0 and not isinstance(
        instances["PASS_TO_PASS"][instances["PASS_TO_PASS"].index[0]], str
    ):
        for col in ["PASS_TO_PASS", "FAIL_TO_PASS"]:
            instances[col] = instances[col].apply(lambda x: str(x))

    swe_mod.run_evaluation(
        instances,
        metadata,
        output_file,
        args.eval_num_workers,
        swe_mod.process_instance,
        max_retries=5,
    )


if __name__ == "__main__":
    patch_and_run()
