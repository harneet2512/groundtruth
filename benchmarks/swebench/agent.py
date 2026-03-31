"""ReAct agent loop for SWE-bench tasks using OpenAI function calling."""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import sys
from pathlib import Path

from openai import OpenAI

from .config import SWEBenchConfig, AgentMode
from .cost_tracker import CostTracker
from .tools import BASE_TOOLS, GROUNDTRUTH_TOOLS
from .gt_v2_tools import GT_V2_PULL_TOOLS
from .groundtruth_bridge import GroundTruthBridge
from .mcp_bridge import MCPBridge
from .gt_v2_bridge import GTV2Bridge
from .gt_v2_hooks import GTV2Hooks
from . import gt_intel

logger = logging.getLogger(__name__)

# Either bridge type exposes call_tool(name, arguments) -> str
GTBridge = GroundTruthBridge | MCPBridge


class SWEBenchAgent:
    """Agent that solves SWE-bench tasks via ReAct loop with OpenAI function calling."""

    def __init__(
        self,
        config: SWEBenchConfig,
        cost_tracker: CostTracker,
        repo_path: str,
        gt_bridge: GTBridge | None = None,
        gt_integration: object | None = None,
        gt_v2_bridge: GTV2Bridge | None = None,
        gt_v2_hooks: GTV2Hooks | None = None,
    ):
        self.config = config
        self.cost_tracker = cost_tracker
        self.repo_path = repo_path
        self.gt_bridge = gt_bridge
        self.gt_integration = gt_integration  # GTIntegration for V2 mode
        self.gt_v2_bridge = gt_v2_bridge  # V2 pull: 3 tools
        self.gt_v2_hooks = gt_v2_hooks  # V2 pull: lifecycle hooks
        self.client = OpenAI()  # Uses OPENAI_API_KEY env var
        self._submitted = False
        self.turns_used: int = 0
        self.conversation_history: list[dict] = []

    def get_tools(self) -> list[dict]:
        """Get tool definitions based on agent mode."""
        tools = list(BASE_TOOLS)
        # V2 passive mode: no GT tools exposed — agent uses only base tools
        if self.config.mode in (AgentMode.GROUNDTRUTH, AgentMode.GROUNDTRUTH_MCP) and self.gt_bridge:
            tools.extend(GROUNDTRUTH_TOOLS)
        # V2 pull mode: 3 focused GT tools
        elif self.config.mode == AgentMode.GROUNDTRUTH_V2_PULL and self.gt_v2_bridge:
            tools.extend(GT_V2_PULL_TOOLS)
        return tools

    def get_system_prompt(self, problem_statement: str = "") -> str:
        """Get system prompt based on agent mode."""
        from .scaffolds import (
            BASELINE_SYSTEM_PROMPT,
            WITH_GROUNDTRUTH_SYSTEM_PROMPT,
            WITH_GROUNDTRUTH_V2_PULL_SYSTEM_PROMPT,
        )

        if self.config.mode in (AgentMode.GROUNDTRUTH, AgentMode.GROUNDTRUTH_MCP):
            return WITH_GROUNDTRUTH_SYSTEM_PROMPT
        if self.config.mode == AgentMode.GROUNDTRUTH_V2_PULL:
            return WITH_GROUNDTRUTH_V2_PULL_SYSTEM_PROMPT
        if self.config.mode == AgentMode.GROUNDTRUTH_V2 and self.gt_integration is not None:
            from .gt_integration import GTIntegration

            gt: GTIntegration = self.gt_integration  # type: ignore[assignment]
            return gt.enrich_system_prompt(problem_statement, BASELINE_SYSTEM_PROMPT)
        return BASELINE_SYSTEM_PROMPT

    def _generate_turn1_hint(self, problem_statement: str) -> str | None:
        """Generate a localization hint for turn 1 (~80 tokens max).

        Returns None if confidence is too low (silence > noise).
        """
        if self.gt_v2_bridge is None or self.gt_v2_bridge._conn is None:
            return None

        conn = self.gt_v2_bridge._conn

        # Step 1: Extract identifiers from issue text
        identifiers = gt_intel.extract_identifiers_from_issue(problem_statement)
        if not identifiers:
            logger.info("turn1_hint: SILENT — no identifiers extracted")
            return None

        # Step 2: Resolve to graph nodes
        targets = gt_intel.resolve_briefing_targets(conn, identifiers, max_targets=5)
        if not targets:
            logger.info("turn1_hint: SILENT — no targets resolved")
            return None

        # Step 3: Confidence gate — check if targets match identifiers
        # (resolve_briefing_targets falls back to top-caller-count nodes when
        # no identifier matches; those are low-confidence)
        ident_lower = {i.lower().split(".")[-1] for i in identifiers}
        matched = any(t.name.lower() in ident_lower for t in targets)
        if not matched:
            logger.info("turn1_hint: SILENT — targets are fallback (no identifier match)")
            return None

        # Step 4: Build ranked file list with cross-file callee walk
        seen: set[str] = set()
        lines: list[str] = []

        for target in targets[:3]:
            if target.file_path in seen:
                continue
            seen.add(target.file_path)
            total_callers, _ = gt_intel.get_all_callers_count(conn, target.id)
            qname = target.qualified_name or target.name
            caller_info = f", {total_callers} callers" if total_callers else ""
            lines.append(f"{len(lines) + 1}. {target.file_path} — defines {qname}(){caller_info}")

            # One-hop cross-file callees
            callees = gt_intel.get_callees(conn, target.id)
            for callee in callees[:2]:
                if callee.file_path == target.file_path or callee.file_path in seen:
                    continue
                seen.add(callee.file_path)
                lines.append(f"{len(lines) + 1}. {callee.file_path} — called by {qname}()")
                if len(lines) >= 5:
                    break

            if len(lines) >= 5:
                break

        if not lines:
            logger.info("turn1_hint: SILENT — no files to suggest")
            return None

        hint = "[GT] Likely fix locations:\n" + "\n".join(lines)
        logger.info(
            "turn1_hint: INJECTED — %d identifiers, %d targets, %d files",
            len(identifiers), len(targets), len(lines),
        )
        return hint

    async def solve(self, instance_id: str, problem_statement: str) -> str | None:
        """
        Run the agent loop on a SWE-bench task.

        Returns the git diff patch string, or None if the agent failed.
        """
        messages = [
            {"role": "system", "content": self.get_system_prompt(problem_statement)},
            {"role": "user", "content": self._format_task(problem_statement)},
        ]
        tools = self.get_tools()
        self._submitted = False
        turn = 0

        # V2.1: Turn-1 localization hint
        if self.config.mode == AgentMode.GROUNDTRUTH_V2_PULL and self.gt_v2_bridge is not None:
            hint = self._generate_turn1_hint(problem_statement)
            if hint:
                messages.append({"role": "user", "content": hint})

        for turn in range(self.config.max_turns):
            # Check cost cap
            if self.cost_tracker.get_task_cost(instance_id) >= self.config.max_cost_per_task:
                logger.warning("Cost cap reached for %s at turn %d", instance_id, turn)
                break

            try:
                # gpt-5-mini does not support temperature parameter
                call_kwargs: dict = {
                    "model": self.config.model,
                    "messages": messages,
                    "tools": tools,
                    "tool_choice": "auto",
                    "max_completion_tokens": self.config.max_tokens_per_turn,
                }
                if self.config.temperature is not None and not self.config.model.startswith("gpt-5"):
                    call_kwargs["temperature"] = self.config.temperature
                response = self.client.chat.completions.create(**call_kwargs)
            except Exception:
                logger.exception("OpenAI API error on turn %d for %s", turn, instance_id)
                break

            # Track costs
            usage = response.usage
            if usage:
                self.cost_tracker.record(
                    instance_id,
                    input_tokens=usage.prompt_tokens,
                    output_tokens=usage.completion_tokens,
                )

            choice = response.choices[0]
            message = choice.message

            # Add assistant message to history
            messages.append(message.model_dump())

            # If no tool calls, the agent is done thinking
            if not message.tool_calls:
                # Check if the text mentions submitting
                if message.content and "submit" in message.content.lower():
                    break
                # Agent stopped without submitting — give it a nudge
                if turn < self.config.max_turns - 1:
                    messages.append({
                        "role": "user",
                        "content": "You stopped without calling any tools or submitting a patch. Either continue working on the issue by calling tools, or call submit_patch when you're done.",
                    })
                continue

            # Execute tool calls
            for tool_call in message.tool_calls:
                fn_name = tool_call.function.name
                try:
                    fn_args = json.loads(tool_call.function.arguments)
                except json.JSONDecodeError:
                    fn_args = {}

                result = await self._execute_tool(fn_name, fn_args)

                # V2 pull hooks: inject targeted context after specific actions
                if self.gt_v2_hooks is not None:
                    hook_response = self._fire_v2_hook(fn_name, fn_args, turn)
                    if hook_response:
                        result = result + "\n\n" + hook_response

                messages.append({
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "content": result,
                })

                if fn_name == "submit_patch":
                    self._submitted = True

            # V2 pull hooks: on_submit fires before final break
            if self._submitted and self.gt_v2_hooks is not None:
                patch = self._extract_patch()
                if patch:
                    submit_warning = self.gt_v2_hooks.on_submit(patch)
                    if submit_warning:
                        # Append as user message so agent sees it
                        messages.append({
                            "role": "user",
                            "content": submit_warning,
                        })
                        # Don't actually break — let agent react to warning
                        self._submitted = False
                        continue

            if self._submitted:
                break

        # Record observability data
        self.turns_used = turn + 1
        self.conversation_history = messages

        # Extract the patch
        return self._extract_patch()

    def _format_task(self, problem_statement: str) -> str:
        """Format the problem statement for the agent."""
        return (
            f"You are working in the repository at {self.repo_path}.\n\n"
            f"Here is the GitHub issue to resolve:\n\n"
            f"<issue>\n{problem_statement}\n</issue>\n\n"
            f"Please resolve this issue by making the necessary code changes."
        )

    async def _execute_tool(self, name: str, arguments: dict) -> str:
        """Execute a tool call and return the result string."""
        # GroundTruth v2 pull tools
        if name.startswith("gt_") and self.gt_v2_bridge:
            return await self.gt_v2_bridge.call_tool(name, arguments)

        # GroundTruth tools (v1 bridge)
        if name.startswith("groundtruth_") and self.gt_bridge:
            return await self.gt_bridge.call_tool(name, arguments)

        # Base tools
        if name == "bash":
            return self._exec_bash(arguments.get("command", ""))
        elif name == "view_file":
            return self._exec_view_file(
                arguments.get("path", ""),
                arguments.get("start_line"),
                arguments.get("end_line"),
            )
        elif name == "edit_file":
            return self._exec_edit_file(
                arguments.get("path", ""),
                arguments.get("old_str", ""),
                arguments.get("new_str", ""),
            )
        elif name == "search":
            return self._exec_search(
                arguments.get("pattern", ""),
                arguments.get("path"),
                arguments.get("include"),
            )
        elif name == "submit_patch":
            return "Patch submitted. Your changes have been recorded."
        else:
            return json.dumps({"error": f"Unknown tool: {name}"})

    def _exec_bash(self, command: str, timeout: int = 60) -> str:
        """Execute a shell command in the repo directory."""
        if sys.platform == "win32" and not shutil.which("bash"):
            shell_cmd: list[str] = ["cmd", "/c", command]
        else:
            shell_cmd = ["bash", "-c", command]
        try:
            result = subprocess.run(
                shell_cmd,
                cwd=self.repo_path,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            output = result.stdout
            if result.stderr:
                output += f"\nSTDERR:\n{result.stderr}"
            if result.returncode != 0:
                output += f"\n[Exit code: {result.returncode}]"
            # Truncate very long output
            if len(output) > 10000:
                output = output[:5000] + "\n\n... (truncated) ...\n\n" + output[-3000:]
            return output or "(no output)"
        except subprocess.TimeoutExpired:
            return f"Command timed out after {timeout}s"
        except Exception as e:
            return f"Error executing command: {e}"

    def _exec_view_file(self, path: str, start_line: int | None = None, end_line: int | None = None) -> str:
        """Read file contents with optional line range."""
        try:
            file_path = Path(path)
            if not file_path.is_absolute():
                file_path = Path(self.repo_path) / file_path

            if not file_path.exists():
                return f"Error: File not found: {path}"

            lines = file_path.read_text(errors="replace").splitlines()

            if start_line is not None:
                start = max(0, start_line - 1)
                end = end_line if end_line is not None else len(lines)
                selected = lines[start:end]
                numbered = [f"{i + start + 1}\t{line}" for i, line in enumerate(selected)]
            else:
                # Truncate very large files
                if len(lines) > 500:
                    numbered = [f"{i+1}\t{line}" for i, line in enumerate(lines[:250])]
                    numbered.append(f"\n... ({len(lines) - 500} lines omitted) ...\n")
                    numbered.extend([f"{i+1}\t{line}" for i, line in enumerate(lines[-250:], len(lines)-250)])
                else:
                    numbered = [f"{i+1}\t{line}" for i, line in enumerate(lines)]

            return "\n".join(numbered)
        except Exception as e:
            return f"Error reading file: {e}"

    def _exec_edit_file(self, path: str, old_str: str, new_str: str) -> str:
        """str_replace style file editing."""
        try:
            file_path = Path(path)
            if not file_path.is_absolute():
                file_path = Path(self.repo_path) / file_path

            if not file_path.exists():
                return f"Error: File not found: {path}"

            content = file_path.read_text(errors="replace")

            count = content.count(old_str)
            if count == 0:
                return f"Error: old_str not found in {path}. Make sure the string matches exactly."
            if count > 1:
                return f"Error: old_str found {count} times in {path}. It must appear exactly once."

            new_content = content.replace(old_str, new_str, 1)
            file_path.write_text(new_content)

            result = f"Successfully edited {path}"

            # V2 passive validation: check the edited file
            if self.gt_integration is not None:
                from .gt_integration import GTIntegration

                gt: GTIntegration = self.gt_integration  # type: ignore[assignment]
                # Validate internally (for logging) but don't show to agent —
                # agent_fixed_after_validation = 0 across 300 tasks, and output
                # may degrade diff formatting (patch-apply errors).
                gt.post_edit_validate(str(file_path), new_content)

            return result
        except Exception as e:
            return f"Error editing file: {e}"

    def _exec_search(self, pattern: str, path: str | None = None, include: str | None = None) -> str:
        """Search for a pattern using grep (or findstr on Windows)."""
        grep_bin = shutil.which("grep")
        if grep_bin is None and sys.platform == "win32":
            # Fallback to findstr on Windows when grep is unavailable
            cmd: list[str] = ["findstr", "/s", "/n"]
            if include:
                cmd.extend(["/m", include])
            cmd.append(pattern)
            cmd.append(path or ".")
        else:
            cmd = [grep_bin or "grep", "-rn", "--color=never"]
            if include:
                cmd.extend(["--include", include])
            cmd.append(pattern)
            cmd.append(path or ".")

        try:
            result = subprocess.run(
                cmd,
                cwd=self.repo_path,
                capture_output=True,
                text=True,
                timeout=30,
            )
            output = result.stdout
            if len(output) > 10000:
                lines = output.splitlines()
                output = "\n".join(lines[:100]) + f"\n\n... ({len(lines) - 100} more matches)"
            return output or "No matches found."
        except subprocess.TimeoutExpired:
            return "Search timed out."
        except Exception as e:
            return f"Search error: {e}"

    def _fire_v2_hook(self, fn_name: str, fn_args: dict, turn: int) -> str | None:
        """Fire v2 pull hooks based on the tool call. Returns hook response or None."""
        hooks = self.gt_v2_hooks
        if hooks is None:
            return None

        if fn_name == "view_file":
            # on_file_open: fires when agent views a file (potential edit target)
            file_path = fn_args.get("path", "")
            if file_path:
                return hooks.on_file_open(file_path, turn)

        elif fn_name == "edit_file":
            # on_edit: fires when agent edits a file (pre-patch constraints)
            file_path = fn_args.get("path", "")
            if file_path:
                return hooks.on_edit(file_path)

        return None

    def _extract_patch(self) -> str | None:
        """Extract git diff from the repo."""
        try:
            git_bin = shutil.which("git") or "git"
            result = subprocess.run(
                [git_bin, "diff"],
                cwd=self.repo_path,
                capture_output=True,
                text=True,
                timeout=10,
            )
            patch = result.stdout.strip()
            return patch if patch else None
        except Exception:
            logger.exception("Failed to extract patch")
            return None
