"""GT-augmented mini-swe-agent for Pier.

Extends Pier's MiniSweAgent to inject GroundTruth codebase intelligence
into the container. The agent gets access to gt_hook.py for on-demand
cross-file analysis (callers, contracts, siblings, tests) without
requiring graph.db or Python deps at runtime.

Usage with pier:
    pier run -p deep-swe/tasks/<task> \
        --agent-import-path artifact_deepswe.gt_agent:GTMiniSweAgent \
        --model deepseek/deepseek-v4-flash \
        --env docker -y

    # With custom config:
    pier run -p deep-swe/tasks/<task> \
        --agent-import-path artifact_deepswe.gt_agent:GTMiniSweAgent \
        --model deepseek/deepseek-v4-flash \
        --env docker -y \
        --ak config_file=artifact_deepswe/gt_integration/deepswe_gt_pier.yaml
"""
from __future__ import annotations

import base64
import logging
import textwrap
from pathlib import Path
from typing import Any

from pier.agents.installed.mini_swe_agent import MiniSweAgent
from pier.environments.base import BaseEnvironment
from pier.models.agent.context import AgentContext
from pier.models.agent.install import AgentInstallSpec, InstallStep

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Locate gt_hook.py -- try multiple paths relative to this file
# ---------------------------------------------------------------------------
_THIS_DIR = Path(__file__).resolve().parent
_CANDIDATES = [
    _THIS_DIR / "benchmarks" / "swebench" / "gt_hook.py",
    _THIS_DIR.parent / "benchmarks" / "swebench" / "gt_hook.py",
    _THIS_DIR / "gt_hook.py",
]

_GT_HOOK_CONTENT: str | None = None
for _p in _CANDIDATES:
    if _p.is_file():
        _GT_HOOK_CONTENT = _p.read_text(encoding="utf-8", errors="replace")
        logger.info("Loaded gt_hook.py from %s (%d bytes)", _p, len(_GT_HOOK_CONTENT))
        break

if _GT_HOOK_CONTENT is None:
    logger.warning(
        "gt_hook.py not found at any candidate path: %s",
        [str(p) for p in _CANDIDATES],
    )

# ---------------------------------------------------------------------------
# Base64-encode gt_hook.py in chunks for heredoc injection (115KB+ file)
# ---------------------------------------------------------------------------
_B64_CHUNK_SIZE = 50_000  # characters per echo line


def _encode_gt_hook() -> list[str]:
    """Return gt_hook.py as a list of base64 chunks."""
    if _GT_HOOK_CONTENT is None:
        return []
    raw = _GT_HOOK_CONTENT.encode("utf-8")
    encoded = base64.b64encode(raw).decode("ascii")
    chunks: list[str] = []
    for i in range(0, len(encoded), _B64_CHUNK_SIZE):
        chunks.append(encoded[i : i + _B64_CHUNK_SIZE])
    return chunks


_B64_CHUNKS = _encode_gt_hook()

# ---------------------------------------------------------------------------
# GT preamble injected into the agent's instruction
# ---------------------------------------------------------------------------
_GT_PREAMBLE = textwrap.dedent("""\

    ## Codebase Intelligence Tool

    You have a codebase intelligence tool at /tmp/gt_hook.py that provides \
    cross-file analysis. Before editing a file, run:

        python3 /tmp/gt_hook.py understand <filepath> \\
            --root=$(cat /tmp/gt_root.txt) --quiet --max-lines=10

    This shows you information you CANNOT get by reading the file alone:
    - Which OTHER files call functions in this file and how they use the results
    - Which TEST files cover this module (so you know where to verify)
    - Rules that hold across ALL sibling methods (patterns you must follow)
    - Behavioral contracts (what functions read/write/return)

    Use the understand command on 1-2 key files before editing. Don't over-use it.
""")


def _build_inject_script() -> str:
    """Build a shell script that writes gt_hook.py into the container.

    Uses base64-chunked transfer to handle the 115KB+ file safely in a
    shell heredoc without escaping issues.
    """
    if not _B64_CHUNKS:
        return (
            'echo "WARNING: gt_hook.py was not found at build time -- '
            'GT injection skipped" >&2\n'
        )

    lines = [
        "set -euo pipefail",
        "# --- GT injection: write gt_hook.py via base64 ---",
        "rm -f /tmp/gt_hook.b64",
    ]

    # Write each chunk as an echo >> append
    for i, chunk in enumerate(_B64_CHUNKS):
        op = ">" if i == 0 else ">>"
        lines.append(f'echo "{chunk}" {op} /tmp/gt_hook.b64')

    lines.extend([
        "base64 -d /tmp/gt_hook.b64 > /tmp/gt_hook.py",
        "rm -f /tmp/gt_hook.b64",
        "chmod +x /tmp/gt_hook.py",
        "",
        "# --- Detect repo root ---",
        'REPO_ROOT=""',
        'for d in /home/user /testbed /workspace /app /repo; do',
        '    if [ -d "$d/.git" ]; then',
        '        REPO_ROOT="$d"',
        "        break",
        "    fi",
        "done",
        'if [ -z "$REPO_ROOT" ]; then',
        '    REPO_ROOT=$(find / -maxdepth 3 -name .git -type d 2>/dev/null '
        '| head -1 | sed "s|/.git||")',
        "fi",
        'if [ -z "$REPO_ROOT" ]; then',
        '    REPO_ROOT="/home/user"',
        '    echo "WARNING: No .git found, defaulting to $REPO_ROOT" >&2',
        "fi",
        'echo "$REPO_ROOT" > /tmp/gt_root.txt',
        'echo "GT: gt_hook.py installed, repo root=$REPO_ROOT" >&2',
    ])

    return "\n".join(lines)


class GTMiniSweAgent(MiniSweAgent):
    """MiniSweAgent with GroundTruth codebase intelligence injection.

    Injects gt_hook.py into the container at install time and prepends
    a GT preamble to the agent instruction so the model knows how to
    use the tool.
    """

    @staticmethod
    def name() -> str:
        return "gt-mini-swe-agent"

    def install_spec(self) -> AgentInstallSpec:
        """Extend parent install_spec with GT injection steps."""
        spec = super().install_spec()

        gt_step = InstallStep(
            user="agent",
            run=_build_inject_script(),
        )

        # Append GT step after existing install steps
        spec.steps.append(gt_step)
        return spec

    async def run(
        self, instruction: str, environment: BaseEnvironment, context: AgentContext
    ) -> None:
        """Run mini-swe-agent with GT preamble prepended to instruction."""
        augmented = instruction.rstrip() + "\n" + _GT_PREAMBLE
        await super().run(augmented, environment, context)
