#!/usr/bin/env python3
"""Live-Lite paired runner — within-task A/B for GT-Full vs Agent-Alone.

Two conditions on the SAME instance_ids:
  A (with-GT)     : pre-task brief + MCP tools registered + post-edit/post-view
                    hooks active. Routed through `run_arm_gtfull_v2_glm47.py`
                    (LiteLLM Vertex header/retry + MCP SSE daemon + canonical
                    hooks) which exec()s into v1r arm "V1R-map+hook".
  B (agent-alone) : length-matched neutral placeholder (303 tokens; calibrated
                    to median brief length over 3 prior captures), no MCP, no
                    hooks. Routed through `run_arm_v1r_glm47.py` (LiteLLM
                    Vertex header/retry only) which exec()s into v1r arm
                    "BL+placeholder" — the placeholder is emitted via
                    `gt_pretask_placeholder.py` from `live_lite_placeholder.json`.

Same model in both arms (default: qwen3-coder-480b on Vertex MaaS, global
endpoint — us-central1 returns FAILED_PRECONDITION; pass --llm-config to
swap in vertex_glm47.json).

Inputs:
  - benchmarks/live_lite_300_ids.json     (instance_ids list, locked)
  - benchmarks/live_lite_placeholder.json (Condition B brief slot, locked)
  - .llm_config/vertex_qwen3.json | .llm_config/vertex_glm47.json
                                          (model config, switchable via --llm-config)

Output layout:
  results/live_lite_paired_<run_id>/
    arm_A/output.jsonl
    arm_A/<instance_id>/...
    arm_B/output.jsonl
    arm_B/<instance_id>/...
    paired_summary.json
    cost_log.jsonl

Run order:
  1. Condition B smoke (5 instances, ~$0.50)         — validates model wiring
  2. Condition B baseline (300 instances, ~$33)      — reference number
  3. Pre-flight A on 1 instance                      — validates GT integration
  4. Condition A on 50 instances (subset of B)       — paired comparison

The 50-task A subset is the first 50 ids of the locked list, by deterministic
slice — same ids appear in B, paired comparison is reproducible.

Usage:
  # Smoke 5 of B
  python scripts/swebench/run_live_lite_paired.py --arm B --first-n 5 \\
      --out results/live_lite_smoke_B/

  # Baseline 300 of B
  python scripts/swebench/run_live_lite_paired.py --arm B \\
      --out results/live_lite_baseline_B/

  # Pre-flight 1 of A
  python scripts/swebench/run_live_lite_paired.py --arm A --first-n 1 \\
      --out results/live_lite_preflight_A/

  # Paired 50-task A on the same ids (after baseline B has run)
  python scripts/swebench/run_live_lite_paired.py --arm A --first-n 50 \\
      --reuse-baseline results/live_lite_baseline_B/output.jsonl \\
      --out results/live_lite_paired_50/

CRITICAL: never run a baseline and an A on different `--first-n` slices —
the comparison is paired by instance_id, not by row index.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))


# ------------------------------------------------------------------ paths --

DEFAULT_IDS_FILE = REPO_ROOT / "benchmarks" / "live_lite_300_ids.json"
DEFAULT_PLACEHOLDER_FILE = REPO_ROOT / "benchmarks" / "live_lite_placeholder.json"
DEFAULT_LLM_CONFIG = REPO_ROOT / ".llm_config" / "vertex_qwen3.json"
DEFAULT_BUDGET_USD = 50.0
DEFAULT_PER_TASK_CAP_USD = 0.30
DEFAULT_V2_WRAPPER = REPO_ROOT / "scripts" / "swebench" / "run_arm_gtfull_v2_glm47.py"
# v1r imports `evaluation.benchmarks.swe_bench.run_infer` which only resolves
# from inside the oh-benchmarks repo. Override via OH_BENCHMARKS_ROOT env if
# the deployment uses a different path.
DEFAULT_OH_CWD = Path(os.environ.get("OH_BENCHMARKS_ROOT", "/home/Lenovo/oh-benchmarks"))


# ----------------------------------------------------------------- loaders --

def load_instance_ids(path: Path, first_n: int | None = None) -> list[str]:
    data = json.loads(path.read_text(encoding="utf-8"))
    ids: list[str] = data["instance_ids"]
    if first_n is not None:
        ids = ids[:first_n]
    return ids


def load_placeholder(path: Path) -> str:
    data = json.loads(path.read_text(encoding="utf-8"))
    return data["text"]


def load_llm_config(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


# ------------------------------------------------------------ cost helpers --

def estimate_task_cost_usd(
    input_tokens: int, output_tokens: int, llm_config: dict[str, Any]
) -> float:
    shape = llm_config.get("_run_shape", {})
    in_price = shape.get("input_price_per_million_usd", 0.38)
    out_price = shape.get("output_price_per_million_usd", 1.74)
    return input_tokens * in_price / 1_000_000 + output_tokens * out_price / 1_000_000


def append_cost_log(out_dir: Path, record: dict[str, Any]) -> None:
    log_path = out_dir / "cost_log.jsonl"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")


# ----------------------------------------------------------------- arms --

def build_first_turn_condition_a(instance: dict[str, Any]) -> str:
    """Generate the GT brief for an instance and return the rendered first-turn text.

    The graph.db path is resolved by the harness side (per-task /tmp/graph.db
    populated by the V3 indexer patch); this helper is invoked in-container.
    """
    from groundtruth.control import kernel
    from groundtruth.control.types import TaskInput

    task = TaskInput(
        task_id=instance["instance_id"],
        repo_root=Path(instance["workspace_path"]),
        issue_text=instance["problem_statement"],
        base_commit=instance["base_commit"],
    )
    brief = kernel.brief(task)
    return brief.brief_text


def build_first_turn_condition_b(placeholder_text: str) -> str:
    """Length-matched neutral placeholder. Same string for every B-arm task."""
    return placeholder_text


# -------------------------------------------------------------- harness call --

def invoke_harness_for_arm(
    *,
    arm: str,
    instance_ids: list[str],
    out_dir: Path,
    llm_config: dict[str, Any],
    llm_config_path: Path,
    budget_usd: float,
    per_task_cap_usd: float,
    workers_per_arm: int,
    dry_run: bool,
) -> int:
    """Run one arm of the paired evaluation.

    For now this is a SCAFFOLD: it writes per-arm config, calls the upstream
    SWE-Bench eval harness via subprocess, and lets either:
      - oh_gt_live_lite_wrapper.py (Condition A — wraps the harness with
        proactive-context injection + MCP registration + hooks), OR
      - the bare harness invocation (Condition B — no wrapper)
    do the actual work.

    Returns exit code from the harness.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    arm_dir = out_dir / f"arm_{arm}"
    arm_dir.mkdir(parents=True, exist_ok=True)

    # Persist per-arm config (for replay + telemetry).
    config = {
        "arm": arm,
        "n_instances": len(instance_ids),
        "instance_ids": instance_ids,
        "llm_config_path": str(DEFAULT_LLM_CONFIG),
        "llm_model": llm_config["model"],
        "budget_cap_usd": budget_usd,
        "per_task_cap_usd": per_task_cap_usd,
        "first_turn_strategy": (
            "kernel.brief() + MCP register + hooks=both" if arm == "A"
            else "length_matched_placeholder, no brief, no MCP, no hooks"
        ),
    }
    (arm_dir / "config.json").write_text(json.dumps(config, indent=2), encoding="utf-8")

    # Cost projection (informational; surfaced before launch per project rule)
    proj_cost = len(instance_ids) * estimate_task_cost_usd(150_000, 30_000, llm_config)
    print(f"[{arm}] projected cost: ${proj_cost:.2f} on {len(instance_ids)} tasks "
          f"(budget cap ${budget_usd:.2f})")
    if proj_cost > budget_usd:
        print(f"[{arm}] ABORT: projected ${proj_cost:.2f} exceeds budget ${budget_usd:.2f}",
              file=sys.stderr)
        return 2
    append_cost_log(out_dir, {
        "ts": time.time(), "arm": arm, "phase": "pre_launch",
        "projected_cost_usd": proj_cost,
        "n_instances": len(instance_ids),
    })

    if dry_run:
        print(f"[{arm}] dry-run — no harness invocation")
        return 0

    # ---------------- harness invocation ----------------
    # Both arms route through the v1r runner (run_arm_v1r.py) which is the
    # SWE-Bench-Live driver. Each arm is a wrapper around v1r that adds:
    #   A: LiteLLM Vertex header/retry + MCP SSE daemon + canonical hooks.
    #      Selects v1r arm "V1R-map+hook" (full GT surface).
    #   B: LiteLLM Vertex header/retry only (no MCP, no hooks). Selects
    #      v1r arm "BL+placeholder" (length-matched neutral placeholder via
    #      gt_pretask_placeholder.py — see GT_PRETASK_PATH wiring).
    # v1r picks instance_ids out of V1R_TASK_FILTER (comma-separated).
    #
    # NOTE: wrappers expect to run on the launch VM where ~/groundtruth and
    # ~/oh-benchmarks are populated. This coordinator must be invoked with
    # cwd=~/oh-benchmarks on the VM (the wrappers exec into v1r which expects
    # that layout).

    if arm == "A":
        wrapper = DEFAULT_V2_WRAPPER
        v1r_arm = "V1R-map+hook"
    else:  # arm == "B"
        wrapper = REPO_ROOT / "scripts" / "swebench" / "run_arm_v1r_glm47.py"
        v1r_arm = "BL+placeholder"

    if not wrapper.exists():
        print(f"[{arm}] WRAPPER MISSING: {wrapper}", file=sys.stderr)
        return 3

    llm_config_name = llm_config.get("_run_shape", {}).get("config_name") \
        or llm_config_path.stem.replace("vertex_", "")
    cmd = [sys.executable, str(wrapper), v1r_arm]
    env = {
        **dict(os.environ),
        "V1R_TASK_FILTER": ",".join(instance_ids),
        "V1R_LLM_CONFIG": llm_config_name,
        "V1R_OUT_ROOT": str(arm_dir),
        "V1R_WORKERS": str(workers_per_arm),
        "V1R_MAXITER": "100",
    }
    print(f"[{arm}] launching v1r via wrapper {wrapper.name} (v1r_arm={v1r_arm}, "
          f"llm_config={llm_config_name}, n_tasks={len(instance_ids)}, "
          f"workers={workers_per_arm})")
    cwd = str(DEFAULT_OH_CWD) if DEFAULT_OH_CWD.is_dir() else str(REPO_ROOT)
    return subprocess.call(cmd, cwd=cwd, env=env)


# -------------------------------------------------------------- entrypoint --

def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Live-Lite paired (within-task A/B) runner — Vertex MaaS / global endpoint")
    p.add_argument("--arm", choices=["A", "B", "both"], required=True,
                   help="Which condition to run. 'both' runs B then A sequentially.")
    p.add_argument("--instance-ids-file", type=Path, default=DEFAULT_IDS_FILE,
                   help="Locked instance_id list (default: benchmarks/live_lite_300_ids.json)")
    p.add_argument("--first-n", type=int, default=None,
                   help="Only run the first N instances (default: all). 5 for smoke; "
                        "50 for Condition A's paired subset; omit for full 300.")
    p.add_argument("--placeholder-file", type=Path, default=DEFAULT_PLACEHOLDER_FILE,
                   help="Length-matched placeholder JSON for Condition B")
    p.add_argument("--llm-config", type=Path, default=DEFAULT_LLM_CONFIG,
                   help="LiteLLM config JSON (default: .llm_config/vertex_glm47.json)")
    p.add_argument("--out", type=Path, required=True,
                   help="Output dir; arm subdirs (arm_A/, arm_B/) are created under it.")
    p.add_argument("--budget-cap-usd", type=float, default=DEFAULT_BUDGET_USD,
                   help="Hard budget cap in USD (default 50.0). Aborts before launch if "
                        "projected spend exceeds this.")
    p.add_argument("--per-task-cap-usd", type=float, default=DEFAULT_PER_TASK_CAP_USD,
                   help="Per-task cap surfaced in cost log; informational")
    p.add_argument("--workers-per-arm", type=int, default=1,
                   help="V1R_WORKERS within each arm. Default 1 (sequential) "
                        "to avoid Vertex MaaS partner-publisher 403 throttle.")
    p.add_argument("--dry-run", action="store_true",
                   help="Validate config + write per-arm files; do not invoke the harness.")
    args = p.parse_args(argv)

    # Surface cost BEFORE any paid work (project mandatory rule).
    # Validate placeholder file is readable here so we fail fast, even though
    # the actual emit happens via gt_pretask_placeholder.py on the VM.
    instance_ids = load_instance_ids(args.instance_ids_file, args.first_n)
    _ = load_placeholder(args.placeholder_file)
    llm_config = load_llm_config(args.llm_config)

    proj_cost_per_arm = len(instance_ids) * estimate_task_cost_usd(150_000, 30_000, llm_config)
    arms_to_run = ["A", "B"] if args.arm == "both" else [args.arm]
    total_cost = proj_cost_per_arm * len(arms_to_run)
    print("=" * 70)
    print(f"Live Lite paired runner — projected cost surfaced before launch")
    print(f"  Arms          : {arms_to_run}")
    print(f"  Instances     : {len(instance_ids)}")
    print(f"  Per-arm cost  : ~${proj_cost_per_arm:.2f}")
    print(f"  Total         : ~${total_cost:.2f}  (budget cap ${args.budget_cap_usd:.2f})")
    print(f"  Model         : {llm_config['model']}")
    print(f"  Endpoint      : {llm_config.get('api_base', 'unset')}")
    print(f"  Output        : {args.out.resolve()}")
    print("=" * 70)
    if total_cost > args.budget_cap_usd:
        print(f"ABORT: projected ${total_cost:.2f} exceeds budget cap ${args.budget_cap_usd:.2f}",
              file=sys.stderr)
        return 2

    args.out.mkdir(parents=True, exist_ok=True)
    rcs: dict[str, int] = {}
    for arm in arms_to_run:
        rcs[arm] = invoke_harness_for_arm(
            arm=arm,
            instance_ids=instance_ids,
            out_dir=args.out,
            llm_config=llm_config,
            llm_config_path=args.llm_config,
            budget_usd=args.budget_cap_usd,
            per_task_cap_usd=args.per_task_cap_usd,
            workers_per_arm=args.workers_per_arm,
            dry_run=args.dry_run,
        )

    summary = {
        "ts": time.time(),
        "out_dir": str(args.out.resolve()),
        "arms_run": arms_to_run,
        "n_instances": len(instance_ids),
        "first_n": args.first_n,
        "exit_codes": rcs,
        "model": llm_config["model"],
        "instance_ids_file": str(args.instance_ids_file),
        "placeholder_file": str(args.placeholder_file),
        "llm_config_file": str(args.llm_config),
    }
    (args.out / "paired_summary.json").write_text(
        json.dumps(summary, indent=2), encoding="utf-8")
    print(f"\n# summary written: {args.out / 'paired_summary.json'}")
    print(f"# next: python scripts/swebench/verify_report.py append --run-dir {args.out}")
    return 0 if all(rc == 0 for rc in rcs.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
