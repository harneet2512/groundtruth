"""GroundTruth environment for mini-swe-agent — GENERALIZED, no monkey-patching.

GT attaches through mini-swe-agent's DOCUMENTED extension point: a pluggable
`environment_class`. `minisweagent.environments.get_environment_class(spec)`
imports any dotted path, so selecting

    --environment-class artifact_deepswe.gt_env.GTDockerEnvironment

makes the agent run inside a GT-aware environment. We subclass the real
environment and OVERRIDE `execute()` (clean OOP) — we never reach into or
rewrite mini-swe-agent's own classes. This is harness-generalized: the same
pattern works for any agent that selects its environment by class.

GT evidence comes from the SAME hooks OpenHands runs
(groundtruth.hooks.post_view / post_edit), against the SAME graph.db, so depth
and 5-language coverage are identical to OH by construction. The evidence
functions are pure: they take an `exec_fn(cmd) -> output` so they are agnostic
to docker/local/singularity.

Modes (auto-selected per call):
  - host-side  : groundtruth importable HERE + a host graph.db exists
                 (GT_GRAPH_DB) -> hook CLIs run as host subprocesses. OH's model.
  - in-sandbox : groundtruth injected into the container -> hook CLIs run there.
  - fallback   : neither -> legacy gt_hook.py (AST), never silent-broken.

GT_BASELINE=1 disables all evidence (control arm) — the env is then a plain
passthrough subclass.
"""
from __future__ import annotations

import hashlib
import os
import re
import subprocess
import sys
import tempfile
from typing import Any, Callable

_GT_BASELINE = bool(os.environ.get("GT_BASELINE"))
_ROOT_FILE = os.environ.get("GT_ROOT_FILE", "/opt/gt/gt_root.txt")
_GRAPH_DB = os.environ.get("GT_GRAPH_DB", "/tmp/graph.db")
_GT_INDEX = os.environ.get("GT_INDEX_BIN", "gt-index")
_GT_HOOK = os.environ.get("GT_HOOK_PATH", "/opt/gt/gt_hook.py")
_GT_PKG = os.environ.get("GT_PKG_PATH", "/opt/gt/gtpkg")
_PYP = f"PYTHONPATH={_GT_PKG}:${{PYTHONPATH:-}}"
_HOST_ROOT = os.environ.get("GT_REPO_ROOT", "")
_MAX_ITEMS = int(os.environ.get("GT_MAX_ITEMS", "5"))

_HIDDEN_PREFIXES = (
    "[GT_META]", "[GT_STATUS]", "[GT_CONFIG]", "[GT_TRACE]", "[GT_DELIVERY]",
    "[GT_COST]", "[GT_PAYLOAD]", "[GT_LLM_CONFIG]", "[GT_SUMMARY]",
    "[GT_BRIEF_DIAG]", "[GT_RANK_DIAG]", "[GT_BRIEF_FAILED]", "[GT_BRIEF_TRACEBACK]",
    "GT_PKG_OK", "GT_SELFTEST",
)
_SRC_EXT = (
    ".py", ".go", ".ts", ".tsx", ".js", ".jsx", ".rs", ".java", ".rb",
    ".c", ".cc", ".cpp", ".h", ".hpp", ".cs", ".php", ".kt", ".scala", ".swift",
)
_VIEW_RE = re.compile(r"(^|[|&;]\s*)(cat|grep|rg|head|tail|less|more|view|nl|awk|sed\s+-n)\b")

# Per-run state (one env instance per task -> instance-level state is fine, but
# module-level keeps parity with the OH wrapper's per-task process).
_seen: dict[tuple[str, str], str] = {}
_edit_state: dict[str, str] = {}


# ---------------------------------------------------------------------------
# Pure evidence helpers — exec_fn(cmd) runs a command in the agent's sandbox.
# ---------------------------------------------------------------------------
def _root() -> str:
    try:
        return open(_ROOT_FILE).read().strip() or "/"
    except Exception:  # noqa: BLE001
        return "/"


def _have_groundtruth() -> bool:
    if getattr(_have_groundtruth, "_v", None) is None:
        ok = os.path.isfile(os.path.join(_GT_PKG, "groundtruth", "hooks", "post_view.py"))
        if not ok:
            try:
                import importlib.util
                ok = importlib.util.find_spec("groundtruth.hooks.post_view") is not None
            except Exception:  # noqa: BLE001
                ok = False
        _have_groundtruth._v = ok  # type: ignore[attr-defined]
    return bool(_have_groundtruth._v)  # type: ignore[attr-defined]


def _local_gt() -> bool:
    if getattr(_local_gt, "_imp", None) is None:
        try:
            import importlib.util
            _local_gt._imp = (  # type: ignore[attr-defined]
                importlib.util.find_spec("groundtruth.hooks.post_view") is not None)
        except Exception:  # noqa: BLE001
            _local_gt._imp = False  # type: ignore[attr-defined]
    return bool(_local_gt._imp) and os.path.isfile(_GRAPH_DB)  # type: ignore[attr-defined]


def _src_files_in_cmd(cmd: str) -> list[str]:
    files: list[str] = []
    for tok in re.split(r"\s+", cmd):
        t = tok.strip("\"'`()<>;|&")
        if t.endswith(_SRC_EXT) and "*" not in t and "$" not in t and t not in files:
            files.append(t)
    return files


def _rel(path: str, root: str) -> str:
    p = path.replace("\\", "/")
    return os.path.relpath(p, root) if os.path.isabs(p) else p


def _clean(text: str) -> str:
    if "__GT_STRUCTURED__" in text:
        text = text.split("__GT_STRUCTURED__")[0]
    keep = [ln for ln in text.splitlines()
            if ln.strip() and not ln.lstrip().startswith(_HIDDEN_PREFIXES)]
    return "\n".join(keep).strip()


def _run_host_module(args: list[str]) -> str:
    """Run a groundtruth hook CLI as a HOST subprocess (full main() flow =
    language-agnostic function resolution via graph.db node positions)."""
    try:
        r = subprocess.run([sys.executable, "-m", *args],
                           capture_output=True, text=True, timeout=60)
        return r.stdout or ""
    except Exception:  # noqa: BLE001
        return ""


def _changed_files_with_diffs(exec_fn: Callable[[str], str], root: str) -> dict[str, str]:
    """ONE exec: {source_rel_path: per-file diff text}. Untracked -> ""."""
    raw = exec_fn(
        f"cd {root} 2>/dev/null && git diff HEAD 2>/dev/null; "
        f"echo '@@GT_UNTRACKED@@'; "
        f"cd {root} 2>/dev/null && git ls-files --others --exclude-standard 2>/dev/null")
    diff_part, _, untracked_part = raw.partition("@@GT_UNTRACKED@@")
    out: dict[str, str] = {}
    for ch in re.split(r"(?m)^(?=diff --git )", diff_part):
        m = re.match(r"diff --git a/(\S+) b/\S+", ch)
        if m and m.group(1).endswith(_SRC_EXT):
            out[m.group(1)] = ch
    for ln in untracked_part.splitlines():
        ln = ln.strip()
        if ln.endswith(_SRC_EXT) and ln not in out:
            out[ln] = ""
    return out


def _post_view(exec_fn: Callable[[str], str], root: str, rel: str) -> str:
    base = os.path.basename(rel).rsplit(".", 1)[0]
    if _local_gt():
        out = _clean(_run_host_module([
            "groundtruth.hooks.post_view", "--root", _HOST_ROOT or root,
            "--db", _GRAPH_DB, "--file", rel, "--structured-output"]))
        if out:
            return f"[GT] {base}:\n{out}"
    if _have_groundtruth():
        out = _clean(exec_fn(
            f"cd {root} && {_PYP} python3 -m groundtruth.hooks.post_view "
            f"--root={root} --db={_GRAPH_DB} --file={rel} --structured-output 2>/dev/null"))
        if out:
            return f"[GT] {base}:\n{out}"
        return ""
    return _fork_fallback(exec_fn, "post_view", rel, root)


def _post_edit(exec_fn: Callable[[str], str], root: str, rel: str, diff_text: str) -> str:
    if _local_gt():
        dp = ""
        try:
            with tempfile.NamedTemporaryFile("w", suffix=".diff", delete=False,
                                             encoding="utf-8") as tf:
                tf.write(diff_text or "")
                dp = tf.name
            out = _clean(_run_host_module([
                "groundtruth.hooks.post_edit", "--root", _HOST_ROOT or root,
                "--db", _GRAPH_DB, "--file", rel, "--quiet",
                "--max-items", str(_MAX_ITEMS), "--diff", dp, "--structured-output"]))
            if out:
                return f"[GT] post-edit {os.path.basename(rel)}:\n{out}"
        except Exception:  # noqa: BLE001
            pass
        finally:
            if dp:
                try:
                    os.unlink(dp)
                except OSError:
                    pass
    if _have_groundtruth():
        exec_fn(f"command -v {_GT_INDEX} >/dev/null 2>&1 && "
                f"{_GT_INDEX} -root={root} -file={rel} -output={_GRAPH_DB} >/dev/null 2>&1 || true")
        out = _clean(exec_fn(
            f"cd {root} && {_PYP} python3 -m groundtruth.hooks.post_edit "
            f"--root={root} --db={_GRAPH_DB} --file={rel} --quiet "
            f"--max-items={_MAX_ITEMS} --structured-output 2>/dev/null"))
        if out:
            return f"[GT] post-edit {os.path.basename(rel)}:\n{out}"
        return ""
    return _fork_fallback(exec_fn, "post_edit", rel, root)


def _fork_fallback(exec_fn: Callable[[str], str], kind: str, rel: str, root: str) -> str:
    if not os.path.exists(_GT_HOOK):
        return ""
    if kind == "post_edit":
        cmd = (f"python3 -S {_GT_HOOK} verify --root={root} --quiet "
               f"--max-items=3 --db={_GRAPH_DB} 2>/dev/null")
    else:
        cmd = (f"python3 -S {_GT_HOOK} understand {rel} --root={root} --quiet "
               f"--max-lines=10 --db={_GRAPH_DB} 2>/dev/null")
    out = _clean(exec_fn(cmd))
    return f"[GT] {os.path.basename(rel)} (fallback):\n{out}" if out else ""


def _dedup(layer: str, rel: str, body: str) -> str:
    key = (layer, rel)
    h = hashlib.md5("\n".join(sorted(body.split("\n"))).strip().encode()).hexdigest()
    if _seen.get(key) == h:
        return ""
    _seen[key] = h
    return body


def gt_evidence_for(exec_fn: Callable[[str], str], command: str) -> str:
    """Return the <gt-evidence> blocks to append after `command` (or "")."""
    if _GT_BASELINE or not command:
        return ""
    root = _root()
    blocks: list[str] = []
    # EDIT — diff-hash gated so the engine runs once per real change, not per cmd.
    for rel, diff_text in _changed_files_with_diffs(exec_fn, root).items():
        h = hashlib.md5((diff_text or rel).encode()).hexdigest()
        if _edit_state.get(rel) == h:
            continue
        _edit_state[rel] = h
        body = _post_edit(exec_fn, root, rel, diff_text)
        body = _dedup("l3", rel, body) if body else ""
        if body:
            blocks.append(f'<gt-evidence kind="post_edit" file="{rel}">\n{body}\n</gt-evidence>')
    # VIEW — every source file read (multi-file ok).
    if _VIEW_RE.search(command):
        for f in _src_files_in_cmd(command):
            rel = _rel(f, root)
            body = _post_view(exec_fn, root, rel)
            body = _dedup("l3b", rel, body) if body else ""
            if body:
                blocks.append(f'<gt-evidence kind="post_view" file="{rel}">\n{body}\n</gt-evidence>')
    return ("\n" + "\n".join(blocks)) if blocks else ""


# ---------------------------------------------------------------------------
# The generalized attachment: subclass + override execute(). No monkey-patch.
# ---------------------------------------------------------------------------
def _augment(env: Any, base_cls: type, action: Any, out: Any) -> None:
    if _GT_BASELINE or not isinstance(out, dict):
        return
    try:
        # exec_fn runs through the BASE class (not the override) -> no re-entrancy.
        def exec_fn(cmd: str) -> str:
            try:
                r = base_cls.execute(env, {"command": cmd})
                return r.get("output", "") if isinstance(r, dict) else str(r)
            except Exception:  # noqa: BLE001
                return ""
        if not getattr(env, "_gt_marker", False):
            out["output"] = (out.get("output") or "") + "\n[gt-env:loaded]"
            env._gt_marker = True
        command = action.get("command", "") if isinstance(action, dict) else str(action)
        ev = gt_evidence_for(exec_fn, command)
        if ev:
            out["output"] = (out.get("output") or "") + ev
    except Exception:  # noqa: BLE001 -- never break the agent loop
        pass


try:
    from minisweagent.environments.docker import DockerEnvironment

    class GTDockerEnvironment(DockerEnvironment):
        """DockerEnvironment that appends GT evidence after each command."""

        def __init__(self, **kwargs: Any) -> None:
            # The swebench runner only auto-sets `image` for the built-in "docker"
            # class name; for a custom environment_class it doesn't, so accept it
            # from GT_TASK_IMAGE (set per task by the harness/workflow).
            kwargs.setdefault("image", os.environ.get("GT_TASK_IMAGE", ""))
            super().__init__(**kwargs)

        def execute(self, action: dict, cwd: str = "", *, timeout: int | None = None) -> dict[str, Any]:
            out = super().execute(action, cwd, timeout=timeout)
            _augment(self, DockerEnvironment, action, out)
            return out
except Exception:  # noqa: BLE001 -- docker env not importable in this install
    pass


try:
    from minisweagent.environments.local import LocalEnvironment

    class GTLocalEnvironment(LocalEnvironment):
        """LocalEnvironment that appends GT evidence after each command."""

        def execute(self, action: dict, cwd: str = "", *, timeout: int | None = None) -> dict[str, Any]:
            out = super().execute(action, cwd, timeout=timeout)
            _augment(self, LocalEnvironment, action, out)
            return out
except Exception:  # noqa: BLE001
    pass
