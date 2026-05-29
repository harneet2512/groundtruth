"""GT-augmented mini-swe-agent for Pier — full 3-point GroundTruth integration.

Replicates, on the pier/mini-swe-agent harness, the same integration GT has on
OpenHands (see the GT Integration Replication Guide). The three injection points:

  Point A — first-turn brief : host-side, generate_v1r_brief() prepended to the
            instruction as <gt-task-brief> (run()).
  Point B — post-edit        : in-container, gt_mini_patch.py monkey-patches
            DefaultAgent.execute_actions; edit-shaped commands -> gt_hook verify.
  Point C — post-view        : same patch; read-shaped commands -> gt_hook understand.
  Arm switch — GT_BASELINE    : set => no brief, no patch injection => pure
            mini-swe-agent control arm.

Why the split: pier runs mini-swe-agent as an installed CLI INSIDE the container,
so B/C cannot be patched from the host (unlike OpenHands' in-process runner).
The patch is injected as sitecustomize.py on PYTHONPATH and fires at interpreter
startup inside the container. The GT-arm workflow must pass:
    --ae PYTHONPATH=/tmp/gt_patch --ae GT_HOOK_PATH=/tmp/gt_hook.py
and, for the brief, set GT_GRAPH_DB + GT_REPO_ROOT on the runner (from preindex).
Control arm: set GT_BASELINE=1.

Usage:
    pier run -p deep-swe/tasks/<task> \
        --agent-import-path artifact_deepswe.gt_agent:GTMiniSweAgent \
        --model deepseek/deepseek-v4-flash --env docker -y \
        --ae PYTHONPATH=/tmp/gt_patch --ae GT_HOOK_PATH=/tmp/gt_hook.py
"""
from __future__ import annotations

import base64
import logging
import os
import textwrap
from pathlib import Path

from pier.agents.installed.mini_swe_agent import MiniSweAgent
from pier.environments.base import BaseEnvironment
from pier.models.agent.context import AgentContext
from pier.models.agent.install import AgentInstallSpec, InstallStep

logger = logging.getLogger(__name__)

_GT_BASELINE = bool(os.environ.get("GT_BASELINE"))
_THIS_DIR = Path(__file__).resolve().parent

# ---------------------------------------------------------------------------
# Locate the two payloads we inject into the container.
# ---------------------------------------------------------------------------
_GT_HOOK_CANDIDATES = [
    _THIS_DIR / "benchmarks" / "swebench" / "gt_hook.py",
    _THIS_DIR.parent / "benchmarks" / "swebench" / "gt_hook.py",
    _THIS_DIR / "gt_hook.py",
]
_PATCH_PATH = _THIS_DIR / "gt_mini_patch.py"


def _load(path_candidates: list[Path]) -> str | None:
    for p in path_candidates:
        if p.is_file():
            logger.info("GT: loaded %s (%d bytes)", p, p.stat().st_size)
            return p.read_text(encoding="utf-8", errors="replace")
    logger.warning("GT: payload not found: %s", [str(p) for p in path_candidates])
    return None


_GT_HOOK_CONTENT = _load(_GT_HOOK_CANDIDATES)
_PATCH_CONTENT = _load([_PATCH_PATH])

_B64_CHUNK_SIZE = 50_000


def _b64_chunks(content: str | None) -> list[str]:
    if not content:
        return []
    enc = base64.b64encode(content.encode("utf-8")).decode("ascii")
    return [enc[i : i + _B64_CHUNK_SIZE] for i in range(0, len(enc), _B64_CHUNK_SIZE)]


def _emit_b64_file(chunks: list[str], b64_path: str, out_path: str) -> list[str]:
    """Shell lines that reconstruct a file from base64 chunks."""
    lines = [f"rm -f {b64_path}"]
    for i, chunk in enumerate(chunks):
        op = ">" if i == 0 else ">>"
        lines.append(f'echo "{chunk}" {op} {b64_path}')
    lines += [f"base64 -d {b64_path} > {out_path}", f"rm -f {b64_path}"]
    return lines


_GT_PREAMBLE = textwrap.dedent("""\

    ## GroundTruth codebase intelligence (automatic)

    As you read and edit files, GroundTruth automatically appends evidence to the
    command output inside <gt-evidence> tags: who calls a function and how, the
    tests that cover it, behavioral contracts (signature/return), and sibling
    patterns you must match. Read those tags — they are cross-file facts you
    cannot get from the file alone. They appear on their own; you do not call
    anything. When GT shows callers, do not break them; when it shows a contract,
    preserve it; when it names a test, run it to verify.
""")


def _build_inject_script() -> str:
    hook_chunks = _b64_chunks(_GT_HOOK_CONTENT)
    patch_chunks = _b64_chunks(_PATCH_CONTENT)
    if not hook_chunks:
        return 'echo "GT WARNING: gt_hook.py missing at build time — GT skipped" >&2\n'

    lines = ["set -euo pipefail", "# --- GT injection ---"]
    # 1) gt_hook.py (the container-native evidence engine)
    lines += _emit_b64_file(hook_chunks, "/tmp/gt_hook.b64", "/tmp/gt_hook.py")
    lines.append("chmod +x /tmp/gt_hook.py")
    # 2) the loop patch, as sitecustomize.py on a dir we put on PYTHONPATH
    if patch_chunks:
        lines.append("mkdir -p /tmp/gt_patch")
        lines += _emit_b64_file(patch_chunks, "/tmp/gt_patch.b64", "/tmp/gt_patch/sitecustomize.py")
    else:
        lines.append('echo "GT WARNING: gt_mini_patch.py missing — B/C hooks disabled" >&2')
    # 3) detect repo root for the hooks
    lines += [
        'REPO_ROOT=""',
        "for d in /home/user /testbed /workspace /app /repo; do",
        '    if [ -d "$d/.git" ]; then REPO_ROOT="$d"; break; fi',
        "done",
        'if [ -z "$REPO_ROOT" ]; then',
        '    REPO_ROOT=$(find / -maxdepth 3 -name .git -type d 2>/dev/null | head -1 | sed "s|/.git||")',
        "fi",
        'if [ -z "$REPO_ROOT" ]; then REPO_ROOT="/home/user"; fi',
        'echo "$REPO_ROOT" > /tmp/gt_root.txt',
        'echo "GT: installed (hook=/tmp/gt_hook.py patch=/tmp/gt_patch/sitecustomize.py root=$REPO_ROOT)" >&2',
    ]
    return "\n".join(lines)


def _generate_brief(instruction: str) -> str:
    """Point A: host-side brief from a preindexed graph.db (GT_GRAPH_DB + GT_REPO_ROOT)."""
    db = os.environ.get("GT_GRAPH_DB", "")
    root = os.environ.get("GT_REPO_ROOT", "")
    if not db or not root or not os.path.isfile(db):
        logger.info("GT: no GT_GRAPH_DB/GT_REPO_ROOT — skipping brief (Point A)")
        return ""
    try:
        from groundtruth.pretask.v1r_brief import generate_v1r_brief

        res = generate_v1r_brief(instruction, root, db)
        return (getattr(res, "brief_text", "") or "").strip()
    except Exception as e:  # noqa: BLE001 — correct-or-quiet
        logger.warning("GT: brief generation failed (%s) — skipping", e)
        return ""


class GTMiniSweAgent(MiniSweAgent):
    """mini-swe-agent + GroundTruth (brief + auto post-view/post-edit), GT_BASELINE-gated."""

    @staticmethod
    def name() -> str:
        return "gt-mini-swe-agent"

    def install_spec(self) -> AgentInstallSpec:
        spec = super().install_spec()
        if not _GT_BASELINE:
            spec.steps.append(InstallStep(user="agent", run=_build_inject_script()))
        return spec

    async def run(
        self, instruction: str, environment: BaseEnvironment, context: AgentContext
    ) -> None:
        if _GT_BASELINE:
            # control arm: pure mini-swe-agent, no GT content at all
            await super().run(instruction, environment, context)
            return
        augmented = instruction
        brief = _generate_brief(instruction)
        if brief:
            augmented = f"<gt-task-brief>\n{brief}\n</gt-task-brief>\n\n{augmented}"
        augmented = augmented.rstrip() + "\n" + _GT_PREAMBLE
        await super().run(augmented, environment, context)
