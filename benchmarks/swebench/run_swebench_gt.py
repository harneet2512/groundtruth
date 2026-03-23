#!/usr/bin/env python3
"""
SWE-bench evaluation runner with GroundTruth MCP integration.

Extends the standard OpenHands SWE-bench runner to inject:
1. MCP config for the groundtruth_check tool (via gt_mcp_bridge.py)
2. Custom prompt template with GT workflow instructions
3. Observability: per-instance GT usage logging

Usage:
  cd /home/user/oh-benchmarks
  uv run python /home/user/groundtruth/benchmarks/swebench/run_swebench_gt.py \
    llm_config.json \
    --workspace docker \
    --max-iterations 100 \
    --select instances.txt \
    --prompt-path /home/user/groundtruth/benchmarks/swebench/prompts/gt_check_only.j2 \
    --output-dir ./eval_outputs/gt_qwen
"""

import json
import os
import sys
import time
from pathlib import Path

# Add oh-benchmarks to sys.path so we can import from benchmarks.*
OH_DIR = os.environ.get("OH_DIR", str(Path.home() / "oh-benchmarks"))
if OH_DIR not in sys.path:
    sys.path.insert(0, OH_DIR)

from benchmarks.swebench.config import INFER_DEFAULTS
from benchmarks.swebench.run_infer import SWEBenchEvaluation, get_instruction
from benchmarks.utils.args_parser import get_parser
from benchmarks.utils.console_logging import summarize_instance
from benchmarks.utils.conversation import build_event_persistence_callback
from benchmarks.utils.evaluation_utils import (
    construct_eval_output_dir,
    get_default_on_result_writer,
)
from benchmarks.utils.llm_config import load_llm_config
from benchmarks.utils.models import EvalInstance, EvalMetadata, EvalOutput
from benchmarks.utils.acp import (
    get_acp_forward_env,
    is_acp_agent,
    build_acp_agent,
    setup_acp_workspace,
    workspace_keepalive,
)
from benchmarks.utils.fake_user_response import run_conversation_with_fake_user_response
from benchmarks.utils.critics import create_critic
from benchmarks.swebench.run_infer import get_tools_for_preset
from benchmarks.swebench import constants

from openhands.sdk import Agent, Conversation, Tool, get_logger
from openhands.sdk.context.condenser import LLMSummarizingCondenser

logger = get_logger(__name__)

# ── GT Bridge Path ───────────────────────────────────────────────────

GT_BRIDGE_PATH = os.environ.get(
    "GT_BRIDGE_PATH",
    str(Path(__file__).parent / "gt_mcp_bridge.py"),
)
GT_OBS_LOG = os.environ.get("GT_OBS_LOG", "/tmp/gt_obs.jsonl")


class GTSWEBenchEvaluation(SWEBenchEvaluation):
    """SWE-bench evaluation with GroundTruth MCP tools injected."""

    def evaluate_instance(
        self, instance: EvalInstance, workspace
    ) -> EvalOutput:
        """Override to inject MCP config for groundtruth_check."""
        from typing import Any

        # Build MCP config pointing to gt_mcp_bridge.py
        mcp_config = {
            "mcpServers": {
                "groundtruth": {
                    "command": sys.executable,
                    "args": [GT_BRIDGE_PATH],
                    "env": {
                        "GT_OBS_LOG": GT_OBS_LOG,
                    },
                }
            }
        }

        tools = get_tools_for_preset(
            preset=self.metadata.tool_preset,
            enable_browser=False,
        )
        condenser = None
        if self.metadata.enable_condenser:
            condenser = LLMSummarizingCondenser(
                llm=self.metadata.llm.model_copy(update={"usage_id": "condenser"}),
                max_size=self.metadata.condenser_max_size,
                keep_first=self.metadata.condenser_keep_first,
            )

        agent = Agent(
            llm=self.metadata.llm,
            tools=tools,
            system_prompt_kwargs={"cli_mode": True},
            condenser=condenser,
            mcp_config=mcp_config,
        )

        setup_acp_workspace(self.metadata.agent_type, workspace)

        repo_path = f"/workspace/{instance.data['repo'].split('/')[-1]}/"
        instance.data["repo_path"] = repo_path

        persist_callback = build_event_persistence_callback(
            run_id=self.metadata.eval_output_dir,
            instance_id=instance.id,
            attempt=self.current_attempt,
        )

        conversation = Conversation(
            agent=agent,
            workspace=workspace,
            callbacks=[persist_callback],
            max_iteration_per_run=self.metadata.max_iterations,
            delete_on_close=True,
        )

        logger.info("repo_path: %s", repo_path)

        # Copy testbed to workspace (same as base class)
        cp_result = workspace.execute_command(
            f"mkdir -p {repo_path} ; cp -r /testbed/. {repo_path}"
        )
        assert cp_result.exit_code == 0, f"cp failed: {cp_result.stderr}"

        git_reset = workspace.execute_command(f"cd {repo_path} ; git reset --hard")
        assert git_reset.exit_code == 0, f"git reset failed: {git_reset.stderr}"

        instruction = get_instruction(
            instance=instance.data,
            metadata=self.metadata,
            workspace_path=workspace.working_dir,
        )

        # Log that GT is active for this instance
        _log_instance_start(instance.id)

        with workspace_keepalive(self.metadata.agent_type, workspace):
            conversation.send_message(instruction)
            run_conversation_with_fake_user_response(conversation)

        # git add + commit
        workspace.execute_command(f"cd {repo_path} ; git add -A")
        workspace.execute_command(
            f"cd {repo_path} && "
            f"git config --global user.email '{constants.GIT_USER_EMAIL}' && "
            f"git config --global user.name '{constants.GIT_USER_NAME}' && "
            f"git commit --no-verify -m '{constants.GIT_COMMIT_MESSAGE}'"
        )

        # Get git patch
        base_commit = instance.data["base_commit"]
        git_patch_result = workspace.execute_command(
            f"cd {repo_path} ; git --no-pager diff --no-color {base_commit} HEAD"
        )
        assert git_patch_result.exit_code == 0, f"git diff failed: {git_patch_result.stderr}"
        git_patch = git_patch_result.stdout

        summarize_instance(
            instance_id=instance.id,
            conversation=conversation,
            git_patch=git_patch,
            logger=logger,
        )

        test_result: dict[str, Any] = {"git_patch": git_patch}

        out = EvalOutput(
            instance_id=instance.id,
            attempt=self.current_attempt,
            test_result=test_result,
            instruction=instruction,
            error=None,
            history=list(conversation.state.events),
            metrics=conversation.conversation_stats.get_combined_metrics(),
        )
        return out


def _log_instance_start(instance_id: str) -> None:
    """Log the start of a GT-enabled instance evaluation."""
    try:
        with open(GT_OBS_LOG, "a") as f:
            f.write(json.dumps({
                "ts": time.time(),
                "event": "instance_start",
                "instance_id": instance_id,
            }) + "\n")
    except Exception:
        pass


def add_prompt_path_argument_gt(parser, caller_file: str) -> None:
    """Add --prompt-path that accepts any .j2 file path."""
    gt_default = str(Path(__file__).parent / "prompts" / "gt_check_only.j2")

    def _resolve(value: str) -> str:
        p = Path(value)
        if p.is_file():
            return str(p.resolve())
        # Try relative to GT prompts dir
        candidate = Path(__file__).parent / "prompts" / p.name
        if candidate.is_file():
            return str(candidate.resolve())
        raise ValueError(f"Prompt template not found: {value}")

    parser.add_argument(
        "--prompt-path",
        type=_resolve,
        default=gt_default,
        help=f"Prompt template path (default: {gt_default})",
    )


def main() -> None:
    parser = get_parser()
    add_prompt_path_argument_gt(parser, __file__)

    # Override defaults for GT runs
    gt_defaults = {
        **INFER_DEFAULTS,
        "dataset": "princeton-nlp/SWE-bench_Lite",
        "max_iterations": 100,
        "num_workers": 1,  # sequential for smoke test
    }
    parser.set_defaults(**gt_defaults)
    args = parser.parse_args()

    llm = load_llm_config(args.llm_config_path)
    logger.info("LLM config: %s", llm.model)

    dataset_description = (
        args.dataset.replace("/", "__") + "-" + args.split.replace("/", "__")
    )
    structured_output_dir = construct_eval_output_dir(
        base_dir=args.output_dir,
        dataset_name=dataset_description,
        model_name=llm.model,
        max_iterations=args.max_iterations,
        eval_note=args.note or "gt-check",
    )

    critic = create_critic(args)

    enable_condenser = args.enable_condenser
    if args.disable_condenser:
        enable_condenser = False

    metadata = EvalMetadata(
        llm=llm,
        dataset=args.dataset,
        dataset_split=args.split,
        max_iterations=args.max_iterations,
        eval_output_dir=structured_output_dir,
        details={},
        prompt_path=args.prompt_path,
        eval_limit=args.n_limit,
        env_setup_commands=["export PIP_CACHE_DIR=~/.cache/pip"],
        n_critic_runs=args.n_critic_runs,
        critic=critic,
        selected_instances_file=args.select,
        max_retries=args.max_retries,
        workspace_type=args.workspace,
        tool_preset=args.tool_preset,
        enable_delegation=args.enable_delegation,
        agent_type=args.agent_type,
        enable_condenser=enable_condenser,
        condenser_max_size=args.condenser_max_size,
        condenser_keep_first=args.condenser_keep_first,
    )

    evaluator = GTSWEBenchEvaluation(
        metadata=metadata,
        num_workers=args.num_workers,
    )

    logger.info("=== GT-enabled SWE-bench run ===")
    logger.info("Bridge: %s", GT_BRIDGE_PATH)
    logger.info("Obs log: %s", GT_OBS_LOG)

    evaluator.run(on_result=get_default_on_result_writer(evaluator.output_path))

    logger.info("Evaluation completed!")
    print(json.dumps({"output_json": str(evaluator.output_path)}))


if __name__ == "__main__":
    main()
