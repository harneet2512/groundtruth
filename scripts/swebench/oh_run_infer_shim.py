"""Shim providing SWEBenchEvaluation class for GT wrappers.

The GT wrappers (oh_gt_hook_wrapper.py, oh_gt_startupmode_wrapper.py) expect:
    from benchmarks.swebench.run_infer import SWEBenchEvaluation, main

This module adapts the v0.44.0 evaluation API to the class-based API.
"""

import importlib
import json
import os
import sys

_eval_mod = None


def _get_eval():
    global _eval_mod
    if _eval_mod is None:
        _root = os.path.dirname(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        )
        if _root not in sys.path:
            sys.path.insert(0, _root)
        _eval_mod = importlib.import_module(
            "evaluation.benchmarks.swe_bench.run_infer"
        )
    return _eval_mod


class SWEBenchEvaluation:
    """Class-based adapter for GT wrappers.

    Wrappers monkey-patch evaluate_instance() to inject GT tools
    before the original evaluation runs.
    """

    def __init__(self):
        self.metadata = None

    def evaluate_instance(self, instance, workspace):  # noqa: ARG002
        pass


def main():
    """Entry point for GT wrappers."""
    mod = _get_eval()
    parser = mod.get_parser()
    parser.add_argument("llm_config", nargs="?", help="LLM config file")
    parser.add_argument(
        "--dataset", type=str, default="princeton-nlp/SWE-bench_Verified"
    )
    parser.add_argument("--split", type=str, default="test")
    parser.add_argument("--workspace", type=str, default="docker")
    parser.add_argument("--max-iterations", type=int, default=100)
    parser.add_argument("--num-workers", type=int, default=4)
    parser.add_argument("--filter-instances", type=str, default=None)
    parser.add_argument("--output-dir", type=str, default=None)
    parser.add_argument("--prompt-path", type=str, default=None)

    args, _ = parser.parse_known_args()

    llm_config = None
    if args.llm_config:
        with open(args.llm_config) as f:
            llm_config = json.load(f)

    config = mod.get_config(args)

    if llm_config:
        config.llm.model = llm_config.get("model", config.llm.model)
        config.llm.api_base = llm_config.get("api_base", config.llm.api_base)
        config.llm.temperature = llm_config.get("temperature", 0.2)
        config.llm.top_p = llm_config.get("top_p", 0.95)

    config.agent.codeact_max_iterations = args.max_iterations

    metadata = mod.make_metadata(
        config, args.dataset, args.split, args.max_iterations, args.num_workers
    )

    dataset = mod.prepare_dataset(metadata, args.output_dir, args.num_workers)

    if args.filter_instances:
        instance_ids = set(args.filter_instances.split(","))
        dataset = dataset[dataset["instance_id"].isin(instance_ids)]

    mod.run_evaluation(
        dataset,
        metadata,
        output_dir=args.output_dir,
        num_workers=args.num_workers,
    )
