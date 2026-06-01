"""In-container GroundTruth patch for mini-swe-agent (observation interception).

Injected into the task container and loaded at interpreter startup via a .pth
file in site-packages (primary) or an append to mini-swe-agent's default.py
(backup). Patches the environment's execute() method to append GT evidence
after edit/view commands.

Attachment mapping (GT integration guide -> mini-swe-agent):
  run_action            -> Environment.execute
  classify_tool_event   -> _classify(command)
  observation text      -> output["output"] (rendered into <output> by the
                           model's format_observation_messages, so appended
                           text reaches the agent verbatim)
  GT_BASELINE switch    -> _GT_BASELINE early no-op

Evidence comes from gt_hook.py (the stdlib-only single-file evidence engine):
  gt_hook.py understand <file> --root=... --quiet --max-lines=10
  gt_hook.py verify --root=... --quiet --max-items=3
The container must have gt_hook.py at /opt/gt/gt_hook.py (injected by
GTMiniSweAgent.install_spec). If a graph.db is present at /tmp/graph.db,
gt_hook.py uses it for richer evidence; otherwise it self-indexes via AST.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys

_GT_BASELINE = bool(os.environ.get("GT_BASELINE"))
_ROOT_FILE = os.environ.get("GT_ROOT_FILE", "/opt/gt/gt_root.txt")
_HOOK_TIMEOUT = int(os.environ.get("GT_HOOK_TIMEOUT", "30"))

# per-file-once dedup, keyed (kind, relpath)
_seen: set[tuple[str, str]] = set()
# diagnostic: one-time marker so trajectory analysis can tell
# "patch never loaded" from "loaded but no evidence"
_marker_sent = False

# Source-file extensions GT indexes (matches gt-index language set).
_SRC_EXT = (
    ".py", ".go", ".ts", ".tsx", ".js", ".jsx", ".rs", ".java", ".rb",
    ".c", ".cc", ".cpp", ".h", ".hpp", ".cs", ".php", ".kt", ".scala", ".swift",
)

# Edit-shaped commands: sed -i, tee, patch, apply_patch, redirects, heredocs.
_EDIT_RE = re.compile(
    r"(^|[|&;]\s*)(sed\s+-i|tee\b|patch\b|apply_patch\b)"
    r"|>>?\s*\S+"
    r"|<<\s*'?[A-Z_]+'?\s*>\s*\S+",
)
# Read-shaped commands: cat, grep, head, tail, etc.
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
            best = t
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


_GT_HOOK = os.environ.get("GT_HOOK_PATH", "/opt/gt/gt_hook.py")


def _run_hook(kind: str, rel: str) -> str:
    """Run gt_hook.py for post-edit or post-view evidence.

    Uses -S to skip site processing so our own .pth does not re-import
    minisweagent (which prints a banner to stdout).  gt_hook.py is
    stdlib-only so -S is safe.
    """
    root = _root()
    db_path = os.environ.get("GT_GRAPH_DB", "/tmp/graph.db")
    db_flag = f"--db={db_path}" if os.path.isfile(db_path) else ""

    if kind == "post_edit":
        args = [sys.executable, "-S", _GT_HOOK, "verify",
                f"--root={root}", "--quiet", "--max-items=3"]
        if db_flag:
            args.append(db_flag)
    else:
        args = [sys.executable, "-S", _GT_HOOK, "understand", rel,
                f"--root={root}", "--quiet", "--max-lines=10"]
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=_HOOK_TIMEOUT)
        return (r.stdout or "").strip()
    except Exception:  # noqa: BLE001 -- correct-or-quiet
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
    if key in _seen:
        return ""
    _seen.add(key)
    ev = _run_hook(kind, rel)
    if not ev:
        return ""
    return f"\n<gt-evidence kind=\"{kind}\" file=\"{rel}\">\n{ev}\n</gt-evidence>"


def _augment_output(action, out) -> None:
    """Append GT evidence to a command's output dict."""
    global _marker_sent
    if not isinstance(out, dict):
        return
    try:
        if not _marker_sent:
            out["output"] = (out.get("output") or "") + "\n[gt-patch:loaded]"
            _marker_sent = True
        cmd = action.get("command", "") if isinstance(action, dict) else str(action)
        ev = _evidence(cmd)
        if ev:
            out["output"] = (out.get("output") or "") + ev
    except Exception:  # noqa: BLE001 -- never break the agent loop
        pass


def _wrap_execute(orig):
    def execute(self, action, *args, **kwargs):
        out = orig(self, action, *args, **kwargs)
        _augment_output(action, out)
        return out

    return execute


# Patch the ENVIRONMENT classes, not agent classes.  Every agent type
# (DefaultAgent, InteractiveAgent, ProgressTrackingAgent) calls
# self.env.execute(action), so wrapping env.execute is agent-class-agnostic.
_ENV_CLASSES = [
    ("minisweagent.environments.local", "LocalEnvironment"),
    ("minisweagent.environments.docker", "DockerEnvironment"),
    ("minisweagent.environments.singularity", "SingularityEnvironment"),
]


def _install() -> None:
    if _GT_BASELINE:
        return
    import importlib

    for modname, clsname in _ENV_CLASSES:
        try:
            cls = getattr(importlib.import_module(modname), clsname)
        except Exception:  # noqa: BLE001 -- env class not in this install
            continue
        if getattr(cls, "_gt_patched", False):
            continue
        try:
            cls.execute = _wrap_execute(cls.execute)
            cls._gt_patched = True
        except Exception:  # noqa: BLE001
            pass


_install()
