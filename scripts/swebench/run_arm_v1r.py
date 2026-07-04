#!/usr/bin/env python3
"""V1R experiment arm runner.

Runs one arm of the V1R localization architecture experiment using the existing
OpenHands SWE-bench infrastructure. V1R-map is the frozen default architecture
(2026-05-03); BL, V1, and V1R-map+hook remain as ablation/regression arms.

Usage:
    # From the OH benchmarks dir on the launch VM, with the venv active:
    python3 <repo>/scripts/swebench/run_arm_v1r.py            # default: V1R-map
    python3 <repo>/scripts/swebench/run_arm_v1r.py V1R-map
    python3 <repo>/scripts/swebench/run_arm_v1r.py V1R-map+hook
    python3 <repo>/scripts/swebench/run_arm_v1r.py V1
    python3 <repo>/scripts/swebench/run_arm_v1r.py BL

Arms:
    V1R-map      — DEFAULT — V1R brief only, no post-edit hook
    V1R-map+hook — V1R brief + lean post-edit hook (ablation)
    V1           — legacy v1 brief + full hook (regression reference)
    BL           — no GT (control)

Paths are resolved repo-relative from this file. Env overrides:
    GT_PRETASK_PATH_V1R   — path to V1R brief bundle
    GT_INDEXER_PATH       — path to gt-index-linux binary
    GT_INTEL_LEAN_PATH    — path to lean post-edit hook (V1R-map+hook arm)
    GT_PRETASK_PATH_V1    — path to legacy v7-full brief bundle (V1 arm)
    GT_HOOK_PATH_V1       — path to legacy v1 post-edit hook (V1 arm)

Results go to ~/results/v1r_<arm_slug>_<timestamp>/
"""
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, os.getcwd())

TASKS = [
    'python-babel__babel-1141', 'matplotlib__matplotlib-29486', 'amoffat__sh-744',
    'cyclotruc__gitingest-94', 'keras-team__keras-20389',
    'projectmesa__mesa-2418', 'joke2k__faker-2155', 'conan-io__conan-17092',
    'sissbruecker__linkding-971', 'pytorch__torchtune-1806',
    'sphinx-doc__sphinx-13200', 'tox-dev__tox-3409', 'dynaconf__dynaconf-1225',
    'modelcontextprotocol__python-sdk-167', 'pdm-project__pdm-3374',
]

DEFAULT_ARM = "V1R-map"

_REPO_DIR = Path(__file__).resolve().parents[2]
_SCRIPTS_DIR = _REPO_DIR / "scripts" / "swebench"
_BIN_DIR = _REPO_DIR / "bin"

# Repo-relative defaults; env vars can override each one.
_INDEXER = os.environ.get("GT_INDEXER_PATH") or str(_BIN_DIR / "gt-index-linux")
_BUNDLE_V1R = os.environ.get("GT_PRETASK_PATH_V1R") or str(_SCRIPTS_DIR / "gt_pretask_brief_v1r.py")
_HOOK_LEAN = os.environ.get("GT_INTEL_LEAN_PATH") or str(_SCRIPTS_DIR / "gt_intel_lean.py")
# V1 arm uses legacy bundles that historically lived in $HOME on the VM. Keep
# that default for backward compatibility; override via env if relocated.
_BUNDLE_V1 = os.environ.get("GT_PRETASK_PATH_V1") or os.path.expanduser("~/gt_pretask_brief_v7_full.py")
_HOOK_V1 = os.environ.get("GT_HOOK_PATH_V1") or os.path.expanduser("~/gt_hook.py")
_PLACEHOLDER = os.environ.get("GT_PRETASK_PLACEHOLDER_PATH") or str(_SCRIPTS_DIR / "gt_pretask_placeholder.py")

ARM_CONFIGS = {
    "BL": {
        "GT_PRETASK_PATH": "",
        "GT_HOOK_PATH": "",
        "GT_KERNEL_HOOK_PATH": "",
        "GT_INDEXER_PATH": "",
    },
    "V1": {
        "GT_PRETASK_PATH": _BUNDLE_V1,
        "GT_HOOK_PATH": _HOOK_V1,
        "GT_KERNEL_HOOK_PATH": "",
        "GT_INDEXER_PATH": "",
    },
    "V1R-map": {
        "GT_PRETASK_PATH": _BUNDLE_V1R,
        "GT_HOOK_PATH": "",
        "GT_KERNEL_HOOK_PATH": "",
        "GT_INDEXER_PATH": _INDEXER,
    },
    "V1R-map+hook": {
        "GT_PRETASK_PATH": _BUNDLE_V1R,
        "GT_HOOK_PATH": _HOOK_LEAN,
        "GT_KERNEL_HOOK_PATH": "",
        "GT_INDEXER_PATH": _INDEXER,
    },
    "BL+placeholder": {
        "GT_PRETASK_PATH": _PLACEHOLDER,
        "GT_HOOK_PATH": "",
        "GT_KERNEL_HOOK_PATH": "",
        "GT_INDEXER_PATH": "",
    },
}

WORKERS = int(os.environ.get("V1R_WORKERS", "4"))
MAXITER = int(os.environ.get("V1R_MAXITER", "100"))


def main() -> int:
    if len(sys.argv) > 1:
        arm = sys.argv[1]
        used_default = False
    else:
        arm = DEFAULT_ARM
        used_default = True

    if arm not in ARM_CONFIGS:
        print(f"Usage: python3 run_arm_v1r.py [<arm>]")
        print(f"  Arms: {', '.join(ARM_CONFIGS)} (default: {DEFAULT_ARM})")
        return 1

    config = ARM_CONFIGS[arm]
    arm_slug = arm.replace("+", "_plus_")
    ts = int(time.time())
    run_name = f"v1r_{arm_slug}_{ts}"
    out_root = os.environ.get("V1R_OUT_ROOT") or os.path.expanduser("~/results")
    out = os.path.join(out_root, run_name)

    if arm in ("V1R-map", "V1R-map+hook") and not os.path.isfile(_INDEXER):
        print(
            f"FATAL: gt-index binary not found at {_INDEXER}. "
            "Run scripts/swebench/build_gt_index_linux.sh or set GT_INDEXER_PATH.",
            file=sys.stderr,
        )
        return 1
    if arm in ("V1R-map", "V1R-map+hook") and not os.path.isfile(_BUNDLE_V1R):
        print(
            f"FATAL: V1R bundle not found at {_BUNDLE_V1R}. "
            "Rebuild with `python3 scripts/swebench/build_v1r_bundle.py` or set GT_PRETASK_PATH_V1R.",
            file=sys.stderr,
        )
        return 1
    if arm == "V1R-map+hook" and not os.path.isfile(_HOOK_LEAN):
        print(
            f"FATAL: lean hook not found at {_HOOK_LEAN}. "
            "Set GT_INTEL_LEAN_PATH or check scripts/swebench/gt_intel_lean.py.",
            file=sys.stderr,
        )
        return 1

    for key, val in config.items():
        if val:
            os.environ[key] = val
        else:
            os.environ.pop(key, None)

    os.environ["GT_KERNEL_ARM"] = "control"
    os.environ["GT_ARM_NAME"] = arm

    default_label = " (default)" if used_default else ""
    print(f"=== V1R Experiment: arm={arm}{default_label} run={run_name} ===")
    print(f"  GT_PRETASK_PATH    = {config['GT_PRETASK_PATH'] or '<unset>'}")
    print(f"  GT_HOOK_PATH       = {config['GT_HOOK_PATH'] or '<unset>'}")
    print(f"  GT_KERNEL_HOOK_PATH= {config['GT_KERNEL_HOOK_PATH'] or '<unset>'}")
    print(f"  GT_INDEXER_PATH    = {config['GT_INDEXER_PATH'] or '<unset>'}")
    print(f"  WORKERS={WORKERS} ITER={MAXITER}")
    print(f"  OUT={out}")
    print(flush=True)

    from datasets import load_dataset
    from evaluation.benchmarks.swe_bench.run_infer import (
        filter_dataset, set_dataset_type, get_llm_config_arg,
        make_metadata, prepare_dataset, run_evaluation, process_instance,
    )

    ds = load_dataset('SWE-bench-Live/SWE-bench-Live', split='lite')
    set_dataset_type('SWE-bench-Live/SWE-bench-Live')
    tests = filter_dataset(ds.to_pandas(), 'instance_id')
    task_filter = os.environ.get("V1R_TASK_FILTER", "").strip()
    if task_filter:
        wanted = [t.strip() for t in task_filter.split(",") if t.strip()]
        tests = tests[tests['instance_id'].isin(wanted)]
        print(f"Tasks (filtered via V1R_TASK_FILTER): {len(tests)}/{len(wanted)}")
    else:
        tests = tests[tests['instance_id'].isin(TASKS)]
        print(f"Tasks: {len(tests)}/{len(TASKS)}")

    llm_config_name = os.environ.get("V1R_LLM_CONFIG", "qwen3_or")
    llm = get_llm_config_arg(llm_config_name)
    llm.log_completions = True
    llm.modify_params = False
    print(f"  LLM_CONFIG={llm_config_name}  VERTEXAI_LOCATION={os.environ.get('VERTEXAI_LOCATION', '<unset>')}")

    meta = make_metadata(
        llm, 'SWE-bench-Live/SWE-bench-Live', 'CodeActAgent',
        MAXITER, run_name, out, details={'mode': 'swe', 'arm': arm},
    )
    out_file = os.path.join(meta.eval_output_dir, 'output.jsonl')
    instances = prepare_dataset(tests, out_file, eval_n_limit=None)
    for col in ['PASS_TO_PASS', 'FAIL_TO_PASS']:
        if col in instances.columns:
            instances[col] = instances[col].apply(str)

    run_evaluation(
        instances, meta, out_file,
        num_workers=WORKERS,
        process_instance_func=process_instance,
        max_retries=3,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
