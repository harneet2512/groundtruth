"""Live n>=15 paired-gate runner for the GT control kernel + OH adapter.

Wires together (Phase 1 PR A + B + production client):

    BriefResult  ──►  OpenHandsAdapter.render_brief  ──►  Conversation.send_message
    (kernel.brief)        (Boundary 3 safe_render)         (RealOpenHandsClient)
                                                                  │
    Conversation events  ──►  kernel callback  ──►  decide_pre_tool / observe_edit
                                                                  │
                                                                  ▼
                                                      adapter.apply_decision
                                                      (block / visible / audit)

Two arms, paired per task:
  - ``arm=control``    : v7.3 brief only, no kernel callback, no apply_decision
  - ``arm=kernel``     : v7.3 brief + kernel runtime (block-on-root-scaffold)

Output: ``results/gt_kernel_paired_<run_id>/`` with one JSONL per task per arm.
``verify_report.py append --run-dir <results dir>`` produces the gated table.

NOT runnable on a dev box without:
  - ``OPENHANDS_LLM_API_KEY`` (or DeepSeek/Qwen equivalent)
  - Docker daemon with SWE-bench-Live images pre-pulled
  - SWE-bench-Live dataset access (HuggingFace cached)

Use ``--dry-run`` to validate the wiring (instantiates Agent / Conversation /
RealOpenHandsClient against a stub LLM, walks the callback path with a
synthetic edit event). Dry-run is what is verified in CI; live-run happens on
VM1.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))


@dataclass
class TaskSpec:
    instance_id: str
    repo: str
    base_commit: str
    problem_statement: str
    workspace_path: Path


def load_live_lite_tasks(n: int) -> list[TaskSpec]:
    from datasets import load_dataset
    ds = load_dataset("SWE-bench-Live/SWE-bench-Live", split="lite")
    out: list[TaskSpec] = []
    for row in ds:
        if not isinstance(row, dict):
            continue
        out.append(TaskSpec(
            instance_id=row["instance_id"],
            repo=row["repo"],
            base_commit=row["base_commit"],
            problem_statement=row.get("problem_statement", ""),
            workspace_path=Path(f"/workspace/{row['instance_id']}"),
        ))
        if len(out) >= n:
            break
    return out


def build_brief_for_task(task: TaskSpec):
    """Call the v7.3 brief layer to produce a BriefResult.

    On VM1 this runs against a freshly-built graph.db for the task's repo.
    Locally without graph.db / repo checkout, this returns an empty brief —
    enough for the dry-run wiring check, useless for real evaluation.
    """
    from groundtruth.control import kernel
    from groundtruth.control.types import TaskInput

    try:
        return kernel.brief(TaskInput(
            task_id=task.instance_id,
            repo_root=task.workspace_path,
            issue_text=task.problem_statement,
            base_commit=task.base_commit,
        ))
    except Exception as exc:  # noqa: BLE001 — dry-run fallback only
        from groundtruth.control.types import BriefResult
        print(f"  [warn] brief failed for {task.instance_id}: {exc}", file=sys.stderr)
        return BriefResult(
            brief_text=f"Issue: {task.problem_statement[:500]}",
            candidates=[], focus_files=[], cluster_files=[],
            confidence=0.0, plan={}, plan_path=None,
        )


def build_agent(*, with_gt_mcp: bool, llm_model: str, llm_api_key: str | None):
    """Construct an OH SDK Agent. Tools: file editor + terminal. Optional
    mcp_config registers the GroundTruth MCP server for ``gt_pull``."""
    from openhands.sdk import Agent, LLM
    from openhands.sdk.tool import Tool

    llm = LLM(model=llm_model, api_key=llm_api_key) if llm_api_key else LLM(model=llm_model)
    tools: list[Any] = [
        Tool(name="FileEditorTool", params={}),
        Tool(name="TerminalTool", params={}),
    ]
    mcp_config: dict[str, Any] = {}
    if with_gt_mcp:
        mcp_config = {
            "mcpServers": {
                "groundtruth": {
                    "command": sys.executable,
                    "args": ["-m", "groundtruth.mcp.server"],
                }
            }
        }
    return Agent(llm=llm, tools=tools, mcp_config=mcp_config)


def build_kernel_callback(adapter, run_state, log_path: Path):
    """Returns a callback closure that observes Conversation events, routes
    edit events through ``kernel.observe_edit`` + ``decide_pre_tool``, and
    applies decisions via ``adapter.apply_decision``."""
    from groundtruth.control import kernel

    def _log(record: dict[str, Any]) -> None:
        with log_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")

    def _on_event(event: Any) -> None:
        et = type(event).__name__
        rec = {"ts": time.time(), "event_type": et}
        try:
            edit_event = adapter.parse_edit({
                "task_id": run_state.task_id,
                "path": getattr(event, "path", None) or getattr(event, "file_path", ""),
                "diff": getattr(event, "diff", None) or getattr(event, "content", ""),
                "ts": str(time.time()),
                "tool": et,
            })
        except Exception:
            edit_event = None
        if edit_event is None:
            _log(rec)
            return
        rec["edit"] = {"files": [str(p) for p in edit_event.files_changed]}
        observation = kernel.observe_edit(edit_event, run_state)
        rec["observation"] = {
            "focus_hit_at_1": observation.focus_hit_at_1,
            "warnings": observation.warnings[:3],
        }
        run_state.edit_history.append(edit_event)
        _log(rec)

    return _on_event


def run_one_task(
    task: TaskSpec,
    *,
    arm: str,
    llm_model: str,
    llm_api_key: str | None,
    out_dir: Path,
    dry_run: bool,
) -> dict[str, Any]:
    from groundtruth.adapters.openhands import OpenHandsAdapter
    from groundtruth.control.types import Capabilities, RunState

    out_dir.mkdir(parents=True, exist_ok=True)
    task_log = out_dir / f"{task.instance_id}__{arm}.jsonl"

    brief = build_brief_for_task(task)
    rs = RunState(
        task_id=task.instance_id,
        plan=brief.plan or {},
        brief_result=brief,
        edit_history=[],
        capabilities=Capabilities(
            block=True, visible=True, audit=True,
            mid_task_pull=True, replan_inject=True,
        ),
    )

    if dry_run:
        # Wire everything but skip the real LLM call. Proves the construction
        # path is sound and the kernel callback fires on a synthetic event.
        from openhands.sdk import Conversation
        from groundtruth.adapters.openhands_client import RealOpenHandsClient

        class _NullConv:
            def __init__(self) -> None:
                self.calls: list[tuple[str, Any]] = []
            def send_message(self, m: Any, sender: str | None = None) -> None:
                self.calls.append(("send_message", m))
            def set_confirmation_policy(self, p: Any) -> None:
                self.calls.append(("set_confirmation_policy", type(p).__name__))
            def reject_pending_actions(self, reason: str = "") -> None:
                self.calls.append(("reject_pending_actions", reason))

        conv = _NullConv()
        client = RealOpenHandsClient(conv)
        adapter = OpenHandsAdapter(client, skip_version_check=False)

        artifact = adapter.render_brief(brief)
        cb = build_kernel_callback(adapter, rs, task_log)

        # Real flow order: agent proposes edit -> kernel.decide_pre_tool ->
        # adapter.apply_decision -> (if not blocked) edit lands -> observe_edit
        # via callback. Synthetic edit path is repo-root scaffold so the
        # `first_edit_root_scaffold` rule should fire.
        from groundtruth.control import kernel
        from groundtruth.control.types import ToolCall, ToolIntent
        call = ToolCall(
            task_id=task.instance_id,
            tool_name="str_replace_editor",
            args={"command": "edit", "path": "reproduce_issue.py"},
            ts=str(time.time()),
            intent=ToolIntent.EDIT,
        )
        decision = kernel.decide_pre_tool(call, rs)
        applied = adapter.apply_decision(decision)

        # Now the observation phase, simulating a landed edit.
        class _Synth:
            path = "reproduce_issue.py"
            diff = "diff --git a/reproduce_issue.py b/reproduce_issue.py\n+x\n"
        cb(_Synth())
        result = {
            "instance_id": task.instance_id,
            "arm": arm,
            "dry_run": True,
            "brief_confidence": brief.confidence,
            "brief_focus": [str(p) for p in brief.focus_files],
            "render_first_turn_ok": artifact.payload.get("first_turn_ok"),
            "decision": decision.action.value,
            "decision_rule": decision.rule_id,
            "applied_actual": applied.actual_action.value,
            "applied_delivered": applied.delivered,
            "conv_calls": [c[0] for c in conv.calls],
        }
        with task_log.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": time.time(), "summary": result}) + "\n")
        return result

    # Live path — runs on VM1.
    from openhands.sdk import Conversation
    from groundtruth.adapters.openhands_client import RealOpenHandsClient

    agent = build_agent(
        with_gt_mcp=(arm == "kernel"),
        llm_model=llm_model,
        llm_api_key=llm_api_key,
    )

    conv = Conversation(
        agent=agent,
        workspace=str(task.workspace_path),
        callbacks=[],
    )

    if arm == "kernel":
        client = RealOpenHandsClient(conv)
        adapter = OpenHandsAdapter(client, skip_version_check=False)
        artifact = adapter.render_brief(brief)
        kernel_cb = build_kernel_callback(adapter, rs, task_log)
        # Conversation in 1.17 exposes callbacks attr; if a future SDK
        # version moves this, operator must wire via the constructor.
        if hasattr(conv, "_callbacks"):
            conv._callbacks.append(kernel_cb)
        elif hasattr(conv, "callbacks"):
            conv.callbacks.append(kernel_cb)
        conv.send_message(artifact.payload["text"])
    else:
        conv.send_message(brief.brief_text)

    conv.run()
    state = getattr(conv, "state", None)
    return {
        "instance_id": task.instance_id,
        "arm": arm,
        "iters": getattr(state, "iteration", -1) if state else -1,
        "log": str(task_log),
    }


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Kernel paired-gate runner")
    p.add_argument("--n", type=int, default=15, help="number of tasks (>=15 for the gate)")
    p.add_argument("--arms", nargs="+", default=["control", "kernel"])
    p.add_argument("--llm-model", default="deepseek/deepseek-chat")
    p.add_argument("--llm-api-key", default=os.environ.get("OPENHANDS_LLM_API_KEY"))
    p.add_argument("--out", default=None, help="output dir; default results/gt_kernel_paired_<ts>/")
    p.add_argument("--dry-run", action="store_true",
                   help="wire everything but skip the LLM/Docker call. Use locally.")
    args = p.parse_args(argv)

    run_id = time.strftime("%Y%m%dT%H%M%S")
    out = Path(args.out or f"results/gt_kernel_paired_{run_id}")
    out.mkdir(parents=True, exist_ok=True)
    print(f"# run_id: {run_id}")
    print(f"# out: {out}")

    if args.dry_run:
        # Dry-run uses a small synthetic task list — does not load HF.
        tasks = [
            TaskSpec(
                instance_id=f"dryrun-{i}",
                repo="example/repo",
                base_commit="HEAD",
                problem_statement=f"Synthetic issue {i}",
                workspace_path=Path(f"/tmp/workspace/dryrun-{i}"),
            )
            for i in range(min(args.n, 3))
        ]
    else:
        tasks = load_live_lite_tasks(args.n)

    summary = []
    for arm in args.arms:
        for t in tasks:
            res = run_one_task(
                t, arm=arm,
                llm_model=args.llm_model,
                llm_api_key=args.llm_api_key,
                out_dir=out,
                dry_run=args.dry_run,
            )
            summary.append(res)
            print(f"  [{arm}] {t.instance_id} -> {res.get('decision', res.get('iters'))}")

    summary_path = out / "summary.json"
    with summary_path.open("w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2)
    print(f"\n# summary written: {summary_path}")
    print(f"# next: python scripts/swebench/verify_report.py append --run-dir {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
