"""In-container GroundTruth patch for mini-swe-agent (Points B/C of the
GT integration guide, re-authored for the mini-swe-agent harness).

Pier runs mini-swe-agent as an installed CLI *inside the task container*, so the
guide's host-side `runtime.run_action` monkey-patch is not reachable from the
Pier orchestrator. Instead this module is injected into the container as
`sitecustomize.py` on `PYTHONPATH`, so Python imports it at interpreter startup
— before mini-swe-agent's loop runs — and it patches the loop in place.

Attachment mapping (guide item -> mini-swe-agent):
  item 8  run_action            -> DefaultAgent.execute_actions
  item 9  classify_tool_event   -> _classify(command)
  item 10 observation text field-> output["output"]  (rendered into <output> by
          the model's format_observation_messages, so appended text reaches the
          agent verbatim)
  item 12 GT_BASELINE switch     -> _GT_BASELINE early no-op

Evidence comes from the REAL GT hooks (guide Section 6(a) REUSE VERBATIM):
  python3 -m groundtruth.hooks.post_view --root --db --file --iteration-ratio
  python3 -m groundtruth.hooks.post_edit --root --db --file --structured-output
The container must have the `groundtruth` package importable and a per-task
graph.db (path in GT_GRAPH_DB). Correct-or-quiet: if a hook returns nothing,
nothing is appended. Per-file-once dedup mirrors the guide's rate limit.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys

_GT_BASELINE = bool(os.environ.get("GT_BASELINE"))
_ROOT_FILE = os.environ.get("GT_ROOT_FILE", "/tmp/gt_root.txt")
_HOOK_TIMEOUT = int(os.environ.get("GT_HOOK_TIMEOUT", "30"))

# per-file-once dedup, keyed (kind, relpath) — mirrors guide's config.evidence_sent
_seen: set[tuple[str, str]] = set()

# Source-file extensions GT indexes (matches gt-index language set).
_SRC_EXT = (
    ".py", ".go", ".ts", ".tsx", ".js", ".jsx", ".rs", ".java", ".rb",
    ".c", ".cc", ".cpp", ".h", ".hpp", ".cs", ".php", ".kt", ".scala", ".swift",
)

# Edit-shaped commands (guide: sed/heredoc/tee -> post_edit).
_EDIT_RE = re.compile(
    r"(^|[|&;]\s*)(sed\s+-i|tee\b|patch\b|apply_patch\b)"
    r"|>>?\s*\S+"                       # > file / >> file redirect
    r"|<<\s*'?[A-Z_]+'?\s*>\s*\S+",     # heredoc into a file
)
# Read-shaped commands (guide: grep/cat/view -> post_view).
_VIEW_RE = re.compile(
    r"(^|[|&;]\s*)(cat|grep|rg|head|tail|less|more|view|nl|awk|sed\s+-n)\b",
)


def _root() -> str:
    try:
        return (open(_ROOT_FILE).read().strip()) or "/"
    except Exception:  # noqa: BLE001
        return "/"


def _first_src_file(cmd: str) -> str | None:
    """Pick the most plausible source-file token from a shell command."""
    best: str | None = None
    for tok in re.split(r"\s+", cmd):
        t = tok.strip("\"'`()<>;|&")
        if t.endswith(_SRC_EXT) and "*" not in t and "$" not in t:
            best = t  # last source-ish token (target of a redirect/edit tends to be late)
            if best:
                continue
    return best


def _classify(cmd: str) -> tuple[str | None, str | None]:
    """Map a bash command to (kind, file): post_edit | post_view | (None, None)."""
    if not cmd:
        return None, None
    f = _first_src_file(cmd)
    if not f:
        return None, None
    if _EDIT_RE.search(cmd):
        return "post_edit", f
    if _VIEW_RE.search(cmd):
        return "post_view", f
    return None, None


_GT_HOOK = os.environ.get("GT_HOOK_PATH", "/tmp/gt_hook.py")


def _run_hook(kind: str, rel: str) -> str:
    """Run the container-native GT engine (gt_hook.py — self-indexing, zero deps).

    gt_hook.py is the in-container packaging of the post_view/post_edit evidence
    logic (same [CALLERS]/[CONTRACT]/[SIGNATURE] markers). It is preferred over
    `python3 -m groundtruth.hooks.*` because the sealed task container has neither
    the groundtruth package nor a graph.db, whereas gt_hook.py builds its own
    index from the repo on the fly. If a real graph.db IS present (GT_GRAPH_DB),
    gt_hook.py uses it; otherwise it self-indexes.
    """
    root = _root()
    if kind == "post_edit":
        # verify the just-edited file against callers/contracts/tests
        args = [sys.executable, _GT_HOOK, "verify", f"--root={root}", "--quiet", "--max-items=3"]
    else:
        # understand: cross-file callers / test coverage / sibling rules / contract
        args = [sys.executable, _GT_HOOK, "understand", rel, f"--root={root}", "--quiet", "--max-lines=10"]
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=_HOOK_TIMEOUT)
        return (r.stdout or "").strip()
    except Exception:  # noqa: BLE001 — correct-or-quiet: any hook failure => no evidence
        return ""


def _evidence(cmd: str) -> str:
    if _GT_BASELINE:
        return ""
    kind, f = _classify(cmd)
    if not kind or not f:
        return ""
    root = _root()
    rel = os.path.relpath(f, root) if os.path.isabs(f) else f
    key = (kind, rel)
    if key in _seen:           # per-file-once
        return ""
    _seen.add(key)
    ev = _run_hook(kind, rel)
    if not ev:                 # correct-or-quiet
        return ""
    return f"\n<gt-evidence kind=\"{kind}\" file=\"{rel}\">\n{ev}\n</gt-evidence>"


def _install() -> None:
    if _GT_BASELINE:
        return  # control arm: do not patch at all
    try:
        from minisweagent.agents.default import DefaultAgent
    except Exception:  # noqa: BLE001 — if mini-swe-agent isn't importable, no-op
        return
    if getattr(DefaultAgent, "_gt_patched", False):
        return  # idempotent

    def patched_execute_actions(self, message: dict) -> list[dict]:
        # Reproduces DefaultAgent.execute_actions (v2.2.x) + GT augmentation.
        actions = message.get("extra", {}).get("actions", [])
        outputs = [self.env.execute(a) for a in actions]
        for a, out in zip(actions, outputs):
            try:
                cmd = a.get("command", "") if isinstance(a, dict) else str(a)
                ev = _evidence(cmd)
                if ev and isinstance(out, dict):
                    out["output"] = (out.get("output") or "") + ev
            except Exception:  # noqa: BLE001 — never break the agent loop
                pass
        return self.add_messages(
            *self.model.format_observation_messages(message, outputs, self.get_template_vars())
        )

    setattr(DefaultAgent, "execute_actions", patched_execute_actions)
    setattr(DefaultAgent, "_gt_patched", True)


_install()
