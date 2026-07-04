"""Collect real early agent traces for the fixed v8.2 pilot.

This is artifact plumbing only. It does not import or modify the v8.2 scheduler
rules. The collector runs the existing local SWE-bench-style OpenAI tool agent
against holdout_v1 local repo checkouts, stops at the v8.2 early budget, and
writes the accepted trajectory schema under results/governor_v8_2_agent_artifacts.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from benchmarks.swebench.agent import SWEBenchAgent
from benchmarks.swebench.config import AgentMode, SWEBenchConfig
from benchmarks.swebench.cost_tracker import CostTracker
from scripts.run_governor_v8_2 import eval_bugs, select_pilot
from groundtruth.pretask.v8_2_scheduler import parse_trace_artifact


EARLY_ACTION_STEPS = 12
EDIT_MARKERS = ("edit", "write", "create", "insert", "str_replace", "apply_patch", "patch")
DEFAULT_OUT_ROOT = Path("results/governor_v8_2_agent_artifacts")
DEFAULT_RUN_ROOT = Path("results/governor_v8_2_agent_runs")


def _jsonable(value: Any) -> Any:
    try:
        json.dumps(value)
        return value
    except TypeError:
        return str(value)


def _artifact_value(value: Any) -> Any:
    if isinstance(value, str):
        return value.replace("\\", "/")
    if isinstance(value, dict):
        return {str(k): _artifact_value(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_artifact_value(v) for v in value]
    return _jsonable(value)


def _action_text(name: str, args: dict[str, Any]) -> str:
    if name == "bash":
        return str(args.get("command", ""))
    if name == "view_file":
        return f"view_file {args.get('path', '')}".replace("\\", "/")
    if name == "edit_file":
        return f"edit_file {args.get('path', '')}".replace("\\", "/")
    if name == "search":
        path = args.get("path") or "."
        include = f" --include {args.get('include')}" if args.get("include") else ""
        return f"search {args.get('pattern', '')} {path}{include}".replace("\\", "/")
    return f"{name} {json.dumps(args, sort_keys=True, default=str)}".replace("\\", "/")


def _is_material_edit(name: str, args: dict[str, Any]) -> bool:
    text = f"{name}\n{json.dumps(args, sort_keys=True, default=str)}\n{_action_text(name, args)}"
    low = text.lower()
    return any(marker in low for marker in EDIT_MARKERS)


def _find_git_bash() -> str | None:
    candidates = [
        Path(r"C:\Program Files\Git\bin\bash.exe"),
        Path(r"C:\Program Files\Git\usr\bin\bash.exe"),
        Path(r"C:\Program Files (x86)\Git\bin\bash.exe"),
    ]
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)
    found = shutil.which("bash")
    return found


class EarlyTraceAgent(SWEBenchAgent):
    """SWEBenchAgent with v8.2 early-budget trace capture."""

    def __init__(self, *args: Any, early_action_steps: int = EARLY_ACTION_STEPS, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.early_action_steps = early_action_steps
        self.trace_steps: list[dict[str, Any]] = []
        self.stop_reason = ""
        self._git_bash = _find_git_bash()

    def _exec_bash(self, command: str, timeout: int = 60) -> str:
        """Use Git Bash on Windows when WSL bash is the broken PATH default."""
        if sys.platform != "win32":
            return super()._exec_bash(command, timeout=timeout)
        bash = self._git_bash
        if not bash:
            return super()._exec_bash(command, timeout=timeout)
        try:
            result = subprocess.run(
                [bash, "-lc", command],
                cwd=self.repo_path,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
            )
            output = result.stdout
            if result.stderr:
                output += f"\nSTDERR:\n{result.stderr}"
            if result.returncode != 0:
                output += f"\n[Exit code: {result.returncode}]"
            if len(output) > 10000:
                output = output[:5000] + "\n\n... (truncated) ...\n\n" + output[-3000:]
            return output or "(no output)"
        except subprocess.TimeoutExpired:
            return f"Command timed out after {timeout}s"
        except Exception as exc:
            return f"Error executing command: {exc}"

    def _exec_search(self, pattern: str, path: str | None = None, include: str | None = None) -> str:
        """Use ripgrep for local Windows collection when grep is unavailable."""
        if sys.platform != "win32":
            return super()._exec_search(pattern, path=path, include=include)
        rg = shutil.which("rg")
        if rg is None:
            return super()._exec_search(pattern, path=path, include=include)
        cmd = [rg, "-n", "--color=never"]
        if include:
            cmd.extend(["--glob", include])
        cmd.extend([pattern, path or "."])
        try:
            result = subprocess.run(
                cmd,
                cwd=self.repo_path,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
            )
            output = result.stdout
            if len(output) > 10000:
                lines = output.splitlines()
                output = "\n".join(lines[:100]) + f"\n\n... ({len(lines) - 100} more matches)"
            return output or "No matches found."
        except subprocess.TimeoutExpired:
            return "Search timed out."
        except Exception as exc:
            return f"Search error: {exc}"

    async def solve(self, instance_id: str, problem_statement: str) -> str | None:
        messages = [
            {"role": "system", "content": self.get_system_prompt(problem_statement)},
            {"role": "user", "content": self._format_task(problem_statement)},
        ]
        tools = self.get_tools()
        self._submitted = False

        for turn in range(self.config.max_turns):
            if self.cost_tracker.get_task_cost(instance_id) >= self.config.max_cost_per_task:
                self.stop_reason = "cost_cap"
                break

            call_kwargs: dict[str, Any] = {
                "model": self.config.model,
                "messages": messages,
                "tools": tools,
                "tool_choice": "auto",
                "max_completion_tokens": self.config.max_tokens_per_turn,
            }
            if self.config.temperature is not None and not self.config.model.startswith("gpt-5"):
                call_kwargs["temperature"] = self.config.temperature
            response = self.client.chat.completions.create(**call_kwargs)

            usage = response.usage
            if usage:
                self.cost_tracker.record(
                    instance_id,
                    input_tokens=usage.prompt_tokens,
                    output_tokens=usage.completion_tokens,
                )

            choice = response.choices[0]
            message = choice.message
            message_dict = message.model_dump()
            messages.append(message_dict)

            if not message.tool_calls:
                self.stop_reason = "no_tool_calls"
                break

            for tool_call in message.tool_calls:
                fn_name = tool_call.function.name
                try:
                    fn_args = json.loads(tool_call.function.arguments)
                except json.JSONDecodeError:
                    fn_args = {}

                result = await self._execute_tool(fn_name, fn_args)
                trace_step = {
                    "action": _action_text(fn_name, fn_args),
                    "tool": fn_name,
                    "args": _artifact_value(fn_args),
                    "observation": _artifact_value(result),
                    "response": _artifact_value(message.content or ""),
                    "turn": turn,
                    "tool_call_id": tool_call.id,
                }
                self.trace_steps.append(trace_step)

                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "content": result,
                    }
                )

                if fn_name == "submit_patch":
                    self._submitted = True
                    self.stop_reason = "submit_patch"
                    break
                if _is_material_edit(fn_name, fn_args):
                    self.stop_reason = "first_material_edit"
                    break
                if len(self.trace_steps) >= self.early_action_steps:
                    self.stop_reason = "early_action_budget"
                    break

            if self.stop_reason:
                break

        self.turns_used = turn + 1 if "turn" in locals() else 0
        self.conversation_history = messages
        return self._extract_patch()


def _run_git(repo_path: Path, args: list[str]) -> None:
    subprocess.run(
        ["git", "-C", str(repo_path), *args],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
    )


def reset_repo_to_parent(bug: dict[str, Any]) -> None:
    repo_path = Path(bug["repo_path"])
    parent = str(bug["parent_commit"])
    _run_git(repo_path, ["checkout", "-f", parent])
    _run_git(repo_path, ["clean", "-fdx"])


def collect_one(
    bug: dict[str, Any],
    *,
    out_root: Path,
    run_root: Path,
    model: str,
    max_cost: float,
    max_tokens: int,
    timeout_seconds: int,
    early_action_steps: int,
) -> dict[str, Any]:
    bug_id = str(bug["bug_id"])
    reset_repo_to_parent(bug)
    task_dir = out_root / bug_id
    task_dir.mkdir(parents=True, exist_ok=True)
    run_task_dir = run_root / bug_id
    run_task_dir.mkdir(parents=True, exist_ok=True)

    config = SWEBenchConfig(
        mode=AgentMode.BASELINE,
        model=model,
        max_turns=early_action_steps,
        max_cost_per_task=max_cost,
        timeout_seconds=timeout_seconds,
        max_tokens_per_turn=max_tokens,
    )
    cost_tracker = CostTracker(model=model)
    agent = EarlyTraceAgent(
        config=config,
        cost_tracker=cost_tracker,
        repo_path=str(Path(bug["repo_path"])),
        early_action_steps=early_action_steps,
    )
    status = "ok"
    error = None
    patch = None
    try:
        import asyncio

        patch = asyncio.run(agent.solve(bug_id, bug.get("issue_body") or bug.get("issue_title") or ""))
    except Exception as exc:
        status = "agent_error"
        error = f"{type(exc).__name__}: {exc}"

    artifact = {
        "trajectory": agent.trace_steps,
        "info": {
            "bug_id": bug_id,
            "repo": bug["repo"],
            "repo_path": bug["repo_path"],
            "parent_commit": bug["parent_commit"],
            "model": model,
            "collector": "scripts/collect_governor_v8_2_traces.py",
            "early_action_steps": early_action_steps,
            "action_steps_collected": len(agent.trace_steps),
            "stop_reason": agent.stop_reason or status,
            "turns_used": agent.turns_used,
            "status": status,
            "error": error,
            "patch_bytes_at_stop": len(patch or ""),
            "cost_usd": cost_tracker.get_task_cost(bug_id),
        },
    }
    artifact_path = task_dir / "trajectory.json"
    artifact_path.write_text(json.dumps(artifact, indent=2, sort_keys=True), encoding="utf-8")

    raw_path = run_task_dir / "raw_conversation.json"
    raw_path.write_text(
        json.dumps(
            {
                "messages": agent.conversation_history,
                "info": artifact["info"],
            },
            indent=2,
            sort_keys=True,
            default=str,
        ),
        encoding="utf-8",
    )
    parsed = parse_trace_artifact(out_root, bug_id)
    return {
        "bug_id": bug_id,
        "repo": bug["repo"],
        "artifact": str(artifact_path),
        "raw_conversation": str(raw_path),
        "collector_status": status,
        "collector_error": error,
        "stop_reason": artifact["info"]["stop_reason"],
        "action_steps_collected": len(agent.trace_steps),
        "v8_2_parse_status": parsed.status,
        "v8_2_signal_count": len(parsed.events),
    }


def validate_existing(pilot: list[dict[str, Any]], out_root: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for bug in pilot:
        parsed = parse_trace_artifact(out_root, str(bug["bug_id"]))
        rows.append(
            {
                "bug_id": bug["bug_id"],
                "repo": bug["repo"],
                "status": parsed.status,
                "artifact": parsed.artifact,
                "action_steps": parsed.action_steps,
                "signal_count": len(parsed.events),
                "agent_file_count": len(parsed.agent_files),
                "error": parsed.error,
            }
        )
    return rows


def load_pilot(holdout: Path) -> list[dict[str, Any]]:
    return select_pilot(eval_bugs(holdout))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--holdout", default="holdout_v1.jsonl")
    parser.add_argument("--out-root", default=str(DEFAULT_OUT_ROOT))
    parser.add_argument("--run-root", default=str(DEFAULT_RUN_ROOT))
    parser.add_argument("--model", default=os.environ.get("MODEL_NAME_EXACT", "gpt-5-mini"))
    parser.add_argument("--max-cost", type=float, default=0.75)
    parser.add_argument("--max-tokens", type=int, default=4096)
    parser.add_argument("--timeout-seconds", type=int, default=600)
    parser.add_argument("--early-action-steps", type=int, default=EARLY_ACTION_STEPS)
    parser.add_argument("--bug-id", default="")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--validate-only", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    out_root = Path(args.out_root)
    run_root = Path(args.run_root)
    pilot = load_pilot(Path(args.holdout))
    if args.bug_id:
        pilot = [bug for bug in pilot if str(bug["bug_id"]) == args.bug_id]
        if not pilot:
            print(f"ERROR: {args.bug_id} is not in the fixed v8.2 pilot", file=sys.stderr)
            return 2
    if args.limit:
        pilot = pilot[: args.limit]

    if args.validate_only:
        rows = validate_existing(pilot, out_root)
        print(json.dumps(rows, indent=2, sort_keys=True))
        return 0 if all(row["status"] == "ok" for row in rows) else 1

    if not os.environ.get("OPENAI_API_KEY"):
        print("ERROR: OPENAI_API_KEY is required to collect real agent traces", file=sys.stderr)
        return 2

    out_root.mkdir(parents=True, exist_ok=True)
    run_root.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, Any]] = []
    for bug in pilot:
        logging.info("collecting %s", bug["bug_id"])
        row = collect_one(
            bug,
            out_root=out_root,
            run_root=run_root,
            model=args.model,
            max_cost=args.max_cost,
            max_tokens=args.max_tokens,
            timeout_seconds=args.timeout_seconds,
            early_action_steps=args.early_action_steps,
        )
        rows.append(row)
        print(json.dumps(row, sort_keys=True))
    manifest_path = run_root / "collection_manifest.jsonl"
    with manifest_path.open("a", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, sort_keys=True) + "\n")
    return 0 if all(row["v8_2_parse_status"] == "ok" for row in rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())
