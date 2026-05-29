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

_B64_CHUNK_SIZE = 45_000  # per-chunk; each chunk becomes one Dockerfile RUN line (< 65535)


def _b64_chunks(content: str | None) -> list[str]:
    if not content:
        return []
    enc = base64.b64encode(content.encode("utf-8")).decode("ascii")
    return [enc[i : i + _B64_CHUNK_SIZE] for i in range(0, len(enc), _B64_CHUNK_SIZE)]


# GT files go to /opt/gt — a persistent, non-volume, non-tmpfs location (unlike
# /tmp, whose build-time contents may not survive to the runtime container).
_GT_DIR = "/opt/gt"

# Repo-root detection, written to /opt/gt/gt_root.txt (one RUN line).
_ROOT_DETECT = (
    f'mkdir -p {_GT_DIR}; chmod 755 {_GT_DIR}; '
    'REPO_ROOT=""; '
    'for d in /home/user /testbed /workspace /app /repo; do '
    '[ -d "$d/.git" ] && REPO_ROOT="$d" && break; done; '
    '[ -z "$REPO_ROOT" ] && REPO_ROOT=$(find / -maxdepth 3 -name .git -type d 2>/dev/null | head -1 | sed "s|/.git||"); '
    '[ -z "$REPO_ROOT" ] && REPO_ROOT="/home/user"; '
    f'echo "$REPO_ROOT" > {_GT_DIR}/gt_root.txt; '
    'echo "GT: installed root=$REPO_ROOT" >&2 || true'
)

# Tiny snippet appended to mini-swe-agent's installed default.py at build time so
# the patch loads whenever DefaultAgent's module imports — independent of
# sitecustomize / PYTHONPATH / python -S (the v2 failure mode). base64'd to dodge
# all shell-quoting issues; exception-guarded so it can never break mini.
_BOOTSTRAP_SNIPPET = (
    "\ntry:\n"
    f'    import sys as _gts; _gts.path.insert(0, "{_GT_DIR}"); import gt_mini_patch  # GroundTruth\n'
    "except Exception:\n"
    "    pass\n"
)
_BOOTSTRAP_B64 = base64.b64encode(_BOOTSTRAP_SNIPPET.encode("utf-8")).decode("ascii")

# Locate mini-swe-agent's installed default.py (under root or agent home) and
# append the bootstrap. Runs as root at build; guarded so build never fails.
_APPEND_TO_MINI = (
    "set +e; "
    'export PATH="/root/.local/bin:$HOME/.local/bin:$PATH"; '
    '. "$HOME/.local/bin/env" 2>/dev/null; . /root/.local/bin/env 2>/dev/null; '
    'BIN="$(command -v mini-swe-agent || command -v mini || ls /root/.local/bin/mini-swe-agent /home/*/.local/bin/mini-swe-agent 2>/dev/null | head -1)"; '
    'if [ -z "$BIN" ]; then echo "GT: mini bin not found; patch-load skipped" >&2; exit 0; fi; '
    'MPY="$(head -n1 "$BIN" | sed "s/^#!//")"; '
    'DEF="$("$MPY" -c "import minisweagent.agents.default as m;print(m.__file__)" 2>/dev/null)"; '
    'if [ -z "$DEF" ]; then echo "GT: default.py not found; patch-load skipped" >&2; exit 0; fi; '
    f'echo "{_BOOTSTRAP_B64}" | base64 -d >> "$DEF"; '
    'echo "GT: appended patch-load to $DEF" >&2'
)


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


def _inject_steps() -> list[InstallStep]:
    """GT injection split across MANY small InstallSteps.

    Each InstallStep renders to ONE Dockerfile `RUN` line, and Docker caps a line
    at 65535 bytes — so the 173KB gt_hook.py CANNOT live in a single RUN (that was
    the v1 bug: build failed with 'dockerfile line greater than max allowed size').
    We emit one RUN per ~45KB base64 chunk (each well under the limit), then decode.
    """
    hook = _b64_chunks(_GT_HOOK_CONTENT)
    if not hook:
        return [InstallStep(user="root", run='echo "GT WARNING: gt_hook.py missing — GT skipped" >&2 || true')]
    steps: list[InstallStep] = [InstallStep(user="root", run=f"mkdir -p {_GT_DIR} && chmod 755 {_GT_DIR}")]
    # gt_hook.py — the container-native evidence engine
    for i, c in enumerate(hook):
        op = ">" if i == 0 else ">>"
        steps.append(InstallStep(user="root", run=f'echo "{c}" {op} {_GT_DIR}/gt_hook.b64'))
    steps.append(InstallStep(
        user="root",
        run=f"base64 -d {_GT_DIR}/gt_hook.b64 > {_GT_DIR}/gt_hook.py && chmod 755 {_GT_DIR}/gt_hook.py && rm -f {_GT_DIR}/gt_hook.b64",
    ))
    # gt_mini_patch.py — the loop patch (loaded via the default.py append below)
    patch = _b64_chunks(_PATCH_CONTENT)
    if patch:
        for i, c in enumerate(patch):
            op = ">" if i == 0 else ">>"
            steps.append(InstallStep(user="root", run=f'echo "{c}" {op} {_GT_DIR}/gt_patch.b64'))
        steps.append(InstallStep(
            user="root",
            run=f"base64 -d {_GT_DIR}/gt_patch.b64 > {_GT_DIR}/gt_mini_patch.py && chmod 644 {_GT_DIR}/gt_mini_patch.py && rm -f {_GT_DIR}/gt_patch.b64",
        ))
        steps.append(InstallStep(user="root", run=_APPEND_TO_MINI))
    steps.append(InstallStep(user="root", run=_ROOT_DETECT))
    return steps


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
            spec.steps.extend(_inject_steps())
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
