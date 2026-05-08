#!/usr/bin/env python3
"""OpenHands GT full-potential wrapper.

This module keeps the OpenHands-specific GT integration separate from the
SWE-agent track-4 path.  The top-level ``main`` patches the OpenHands SWE-bench
runner when that runner is importable, while the small functions below are
deliberately testable with fake runtimes and fake OpenHands action objects.
"""
from __future__ import annotations

import argparse
import base64
import io
import json
import os
import re
import sys
import tarfile
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable


_SCRIPT_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _SCRIPT_DIR.parents[1]
_SRC_DIR = _REPO_ROOT / "src"
_DEFAULT_GT_INDEX = os.environ.get("GT_INDEX_BINARY", "/tmp/gt-index-linux")
_TOOL_ROOT = _REPO_ROOT / "tools" / "sweagent"
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

WORKSPACE_ROOT = "/workspace"
GRAPH_DB = "/tmp/gt_index.db"
GT_TOOLS_DIR = "/tmp/gt_tools"

SOURCE_EXTS = (
    ".py",
    ".js",
    ".jsx",
    ".ts",
    ".tsx",
    ".go",
    ".java",
    ".rs",
    ".rb",
    ".php",
)
MUTATING_EDITOR_VERBS = {"create", "str_replace", "insert", "write"}
VIEW_EDITOR_VERBS = {"view"}
INTERNAL_GT_MARKERS = (
    "/tmp/gt-index",
    "gt-index-linux",
    "/tmp/gt_tools/",
    "groundtruth.hooks.post_edit",
    "groundtruth.hooks.post_view",
)
SCAFFOLDING_PREFIXES = (
    "reproduce_",
    "repro_",
    "debug_",
    "verify_fix",
    "verify_implementation",
    "test_fix",
    "scratch_",
    "temp_",
)
TEST_PATH_RE = re.compile(
    r"(^|/)(tests?|__tests__|spec|specs)/|(^|/)test_[^/]*$|(^|/)[^/]*_test\.[^/]*$"
)

_GT_VALIDATE_ARG_RE = re.compile(r"\bgt_validate\s+([^\s&|;<>]+)")


@dataclass(frozen=True)
class HookEvent:
    """Classified OpenHands tool event."""

    kind: str
    path: str = ""
    reason: str = ""


@dataclass
class GTRuntimeConfig:
    """Runtime paths and limits for the OH GT hook layer."""

    workspace_root: str = WORKSPACE_ROOT
    graph_db: str = GRAPH_DB
    gt_index_bin: str = _DEFAULT_GT_INDEX
    tools_dir: str = GT_TOOLS_DIR
    max_items: int = 3
    source_exts: tuple[str, ...] = SOURCE_EXTS
    pending_checks: set[str] = field(default_factory=set)
    verified_checks: set[str] = field(default_factory=set)
    edited_files: set[str] = field(default_factory=set)
    viewed_files: set[str] = field(default_factory=set)
    pending_summaries: list[tuple[str, str]] = field(default_factory=list)
    telemetry: Any = None  # GTTelemetry, optional


@dataclass
class GTTelemetry:
    """Per-run GT layer utilization (written to instance["gt_telemetry"] on finish)."""

    task_id: str
    layer_hits: dict[str, dict[str, int]] = field(default_factory=dict)

    def _bump(self, layer: str, key: str) -> None:
        bucket = self.layer_hits.setdefault(layer, {"ok": 0, "fail": 0, "skipped": 0})
        bucket[key] = bucket.get(key, 0) + 1

    def record_brief(self, ok: bool, l2_present: bool) -> None:
        self._bump("L1", "ok" if ok else "fail")
        if l2_present:
            self._bump("L2", "ok")
        else:
            self._bump("L2", "fail")

    def record_hook(self, layer: str, ok: bool, empty: bool = False) -> None:
        if empty:
            self._bump(layer, "skipped")
        else:
            self._bump(layer, "ok" if ok else "fail")

    def record_reindex(self, ok: bool) -> None:
        self._bump("L6", "ok" if ok else "fail")

    def record_gate(self, fired: bool) -> None:
        self._bump("L5", "ok" if fired else "skipped")

    def record_l4(self) -> None:
        self._bump("L4", "ok")

    def utilization(self) -> dict[str, float]:
        """Rough 0–1 utilization per layer (diagnostic only)."""

        def score(layer: str) -> float:
            b = self.layer_hits.get(layer, {})
            ok, fail = b.get("ok", 0), b.get("fail", 0)
            if ok + fail == 0:
                return 0.0
            return ok / (ok + fail)

        return {f"L{i}": score(f"L{i}") for i in range(1, 7)}

    def finalize(self) -> dict[str, Any]:
        u = self.utilization()

        # L3b keyed as L3b in hits
        b3 = self.layer_hits.get("L3b", {})
        denom = b3.get("ok", 0) + b3.get("fail", 0)
        u["L3b"] = (b3.get("ok", 0) / denom) if denom else 0.0
        ow = {"L1": 0.2, "L2": 0.15, "L3": 0.2, "L3b": 0.1, "L4": 0.1, "L5": 0.15, "L6": 0.1}
        overall = sum(u.get(k, 0.0) * w for k, w in ow.items())
        return {
            "task_id": self.task_id,
            "layer_hits": dict(self.layer_hits),
            "utilization": u,
            "overall_utilization": round(overall, 4),
        }


def _sh_single_quote(s: str) -> str:
    return "'" + str(s).replace("'", "'\"'\"'") + "'"


def _env_prefix(config: GTRuntimeConfig) -> str:
    tp = config.tools_dir.rstrip("/")
    return (
        f"export GT_GRAPH_DB={_sh_single_quote(config.graph_db)}; "
        f"export GT_REPO_ROOT={_sh_single_quote(config.workspace_root)}; "
        "export GT_PYTHON=python3; "
        "export PYTHONPATH=/tmp:${PYTHONPATH:-}; "
        f"export PATH={tp}/gt_query/bin:{tp}/gt_search/bin:"
        f"{tp}/gt_navigate/bin:{tp}/gt_validate/bin:${{PATH:-}}; "
    )


def _brief_max_tokens(text: str, max_tokens: int = 500) -> str:
    """Keep brief compact (~4 chars / token heuristic for English-ish code paths)."""

    if not text:
        return ""
    max_chars = max_tokens * 4
    lines = text.strip().split("\n")
    path_line_re = re.compile(r"[a-zA-Z0-9_./\\-]+\.[a-zA-Z0-9]{1,4}\b")

    ranked: list[str] = []
    rest: list[str] = []
    for ln in lines:
        if path_line_re.search(ln):
            ranked.append(ln)
        else:
            rest.append(ln)
    merged = ranked + rest
    out: list[str] = []
    n = 0
    for ln in merged:
        if n + len(ln) + 1 > max_chars:
            break
        out.append(ln)
        n += len(ln) + 1
    body = "\n".join(out)
    if len("\n".join(merged)) > max_chars:
        body += "\n[GT_BRIEF_TRUNCATED]"
    return body


def _derive_package_name(instance_id: str) -> str:
    if not instance_id:
        return ""
    tail = instance_id
    if "__" in instance_id:
        tail = instance_id.split("__", 1)[1]
    return re.sub(r"-\d+$", "", tail.strip())


def _rewrite_site_package_paths_in_brief(brief: str, instance_id: str, workspace_root: str) -> str:
    """Rewrite site-packages/dist-packages file paths to workspace checkout paths."""
    if not brief:
        return brief
    pkg = _derive_package_name(instance_id)
    if not pkg:
        return brief
    root = workspace_root.rstrip("/")
    file_exts = "py|go|js|ts|rs|c|cpp"
    pattern = re.compile(
        r"(?P<prefix>/[^\s\"'<>]+(?:site-packages|dist-packages)/"
        + re.escape(pkg)
        + r"/)(?P<rest>[^\s\"'<>]+\.(?:"
        + file_exts
        + r"))"
    )
    return pattern.sub(rf"{root}/{pkg}/\g<rest>", brief)


def _action_class(action: Any) -> str:
    return type(action).__name__


def _action_text(action: Any) -> str:
    return (
        getattr(action, "command", "")
        or getattr(action, "content", "")
        or getattr(action, "thought", "")
        or ""
    )


def _path_attr(action: Any) -> str:
    for attr in ("path", "file_path", "source", "target"):
        value = getattr(action, attr, "") or ""
        if value:
            return str(value)
    return ""


def _normalize_path(path: str) -> str:
    path = path.strip().strip("'\"")
    if path.startswith(WORKSPACE_ROOT + "/"):
        path = path[len(WORKSPACE_ROOT) + 1 :]
    return path.replace("\\", "/")


def _path_relative_to_workspace(path: str, config: GTRuntimeConfig) -> str:
    path = path.strip().strip("'\"").replace("\\", "/")
    root = config.workspace_root.rstrip("/")
    if root and path.startswith(root + "/"):
        return path[len(root) + 1 :]

    normalized = _normalize_path(path)
    workspace_name = root.rsplit("/", 1)[-1] if root else ""
    prefix = f"{workspace_name}/" if workspace_name else ""
    if prefix and normalized.startswith(prefix):
        return normalized[len(prefix) :]
    return normalized


def _is_source_path(path: str, source_exts: tuple[str, ...] = SOURCE_EXTS) -> bool:
    return _normalize_path(path).endswith(source_exts)


def _is_test_path(path: str) -> bool:
    return TEST_PATH_RE.search(_normalize_path(path)) is not None


def _is_scaffolding_path(path: str) -> bool:
    base = Path(_normalize_path(path)).name.lower()
    return base.startswith(SCAFFOLDING_PREFIXES)


def _is_internal_gt_command(text: str) -> bool:
    blob = f" {text}"
    return any(marker in blob for marker in INTERNAL_GT_MARKERS)


def _parse_editor_command(command: str) -> tuple[str, str] | None:
    match = re.search(r"(?:str_replace_editor|file_editor)\s+(\S+)\s+(\S+)", command)
    if match:
        return match.group(1), _normalize_path(match.group(2))
    return None


def _parse_read_command(command: str) -> str:
    patterns = [
        r"\bcat\s+(\S+)",
        r"\bsed\s+(?:-[^\s]+\s+)?(?:'[^']+'\s+|\"[^\"]+\"\s+)?(\S+)",
        r"\bhead\s+(?:-[^\s]+\s+)?(\S+)",
        r"\btail\s+(?:-[^\s]+\s+)?(\S+)",
        r"\bnl\s+(?:-[^\s]+\s+)?(\S+)",
    ]
    for pattern in patterns:
        match = re.search(pattern, command)
        if match:
            return _normalize_path(match.group(1))
    return ""


def classify_tool_event(
    action: Any, *, source_exts: tuple[str, ...] = SOURCE_EXTS
) -> HookEvent:
    """Classify an OpenHands action into post-view, post-edit, finish, or skip."""

    cls = _action_class(action)
    text = _action_text(action)
    if _is_internal_gt_command(text):
        return HookEvent("skip", reason="internal_gt_command")

    if cls in {"AgentFinishAction", "FinishAction"} or text.strip() == "finish":
        return HookEvent("finish")

    if cls in {"FileReadAction", "FileViewAction"}:
        path = _normalize_path(_path_attr(action))
        if not path:
            return HookEvent("skip", reason="no_path")
        if not _is_source_path(path, source_exts):
            return HookEvent("skip", path=path, reason="non_source_ext")
        return HookEvent("post_view", path=path)

    if cls in {"FileEditAction", "FileWriteAction"}:
        path = _normalize_path(_path_attr(action))
        if not path:
            return HookEvent("skip", reason="no_path")
        if _is_test_path(path):
            return HookEvent("skip", path=path, reason="test_path")
        if not _is_source_path(path, source_exts):
            return HookEvent("skip", path=path, reason="non_source_ext")
        return HookEvent("post_edit", path=path)

    if cls == "CmdRunAction":
        parsed = _parse_editor_command(text)
        if parsed:
            verb, path = parsed
            if verb in VIEW_EDITOR_VERBS:
                if not _is_source_path(path, source_exts):
                    return HookEvent("skip", path=path, reason="non_source_ext")
                return HookEvent("post_view", path=path)
            if verb not in MUTATING_EDITOR_VERBS:
                return HookEvent("skip", path=path, reason=f"non_mutating_verb:{verb}")
            if _is_test_path(path):
                return HookEvent("skip", path=path, reason="test_path")
            if not _is_source_path(path, source_exts):
                return HookEvent("skip", path=path, reason="non_source_ext")
            return HookEvent("post_edit", path=path)

        read_path = _parse_read_command(text)
        if read_path:
            if not _is_source_path(read_path, source_exts):
                return HookEvent("skip", path=read_path, reason="non_source_ext")
            return HookEvent("post_view", path=read_path)

    return HookEvent("skip", reason="non_hook_action")


def make_reindex_command(path: str, config: GTRuntimeConfig) -> str:
    """Build the L6 command. Uses gt-index ``-file`` mode."""

    rel = _path_relative_to_workspace(path, config)
    return (
        f"{config.gt_index_bin} -root={config.workspace_root} "
        f"-file={rel} -output={config.graph_db}"
    )


def make_view_hook_command(event: HookEvent, config: GTRuntimeConfig) -> str:
    rel_path = _path_relative_to_workspace(event.path, config)
    return (
        _env_prefix(config)
        + "python3 -m groundtruth.hooks.post_view "
        + f"--root={config.workspace_root} --db={config.graph_db} --file={rel_path}"
    )


def make_edit_hook_command(event: HookEvent, config: GTRuntimeConfig) -> str:
    return make_edit_hook_command_with_artifacts(event, config)


def make_edit_hook_command_with_artifacts(
    event: HookEvent,
    config: GTRuntimeConfig,
    *,
    diff_path: str | None = None,
    old_content_path: str | None = None,
) -> str:
    rel_path = _path_relative_to_workspace(event.path, config)
    cmd = (
        _env_prefix(config)
        + "python3 -m groundtruth.hooks.post_edit "
        + f"--root={config.workspace_root} --db={config.graph_db} "
        + f"--file={rel_path} --quiet --max-items={config.max_items}"
    )
    if diff_path:
        cmd += f" --diff={diff_path}"
    if old_content_path:
        cmd += f" --old-content={old_content_path}"
    return cmd


def _extract_extras_from_observation(obs: Any) -> dict[str, Any]:
    extras: dict[str, Any] = {}
    for attr in ("extras", "extra", "metadata"):
        value = getattr(obs, attr, None)
        if isinstance(value, dict):
            extras.update(value)
    return extras


def _deep_find_first(obj: Any, keys: set[str]) -> Any | None:
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in keys:
                return v
        for v in obj.values():
            found = _deep_find_first(v, keys)
            if found is not None:
                return found
    elif isinstance(obj, list):
        for v in obj:
            found = _deep_find_first(v, keys)
            if found is not None:
                return found
    return None


def _extract_diff_and_old_content(obs: Any) -> tuple[str, str]:
    extras = _extract_extras_from_observation(obs)
    if not extras:
        return "", ""
    diff_val = _deep_find_first(extras, {"diff"})
    old_val = _deep_find_first(extras, {"old_content", "oldContent"})
    diff = diff_val if isinstance(diff_val, str) else ""
    old_content = old_val if isinstance(old_val, str) else ""
    return diff, old_content


def _write_text_to_container(
    orig_run_action: Callable[[Any], Any], content: str, target_path: str
) -> bool:
    if not content:
        return False
    try:
        payload = content.encode("utf-8", errors="replace")
    except Exception:
        return False
    chunks = _b64_chunks(payload)
    if not chunks:
        return False
    b64_path = f"{target_path}.b64"
    parent = Path(target_path).parent.as_posix()
    _run_internal(orig_run_action, f"mkdir -p {_sh_single_quote(parent)}", 15)
    _run_internal(
        orig_run_action,
        f"rm -f {_sh_single_quote(target_path)} {_sh_single_quote(b64_path)}",
        15,
    )
    for idx, chunk in enumerate(chunks):
        op = ">" if idx == 0 else ">>"
        _run_internal(
            orig_run_action,
            f"echo -n '{chunk}' {op} {_sh_single_quote(b64_path)}",
            15,
        )
    _run_internal(
        orig_run_action,
        f"base64 -d {_sh_single_quote(b64_path)} > {_sh_single_quote(target_path)} && rm -f {_sh_single_quote(b64_path)}",
        30,
    )
    return True


def render_l4_tool_footer(installed_tools: list[str] | None = None) -> str:
    if not installed_tools:
        return ""
    usage = []
    for t in installed_tools:
        if t == "gt_query":
            usage.append("gt_query <symbol>")
        elif t == "gt_search":
            usage.append("gt_search <kind> <query>")
        elif t == "gt_navigate":
            usage.append("gt_navigate <symbol> <mode>")
        elif t == "gt_validate":
            usage.append("gt_validate <file>")
    if not usage:
        return ""
    return "\nGT tools verified on PATH (" + ", ".join(installed_tools) + "): " + "; ".join(usage) + ".\n"


def _format_l2_pretask_tag(telemetry: Any) -> str:
    """One-line, telemetry-backed L2 proof (hybrid fusion / sparse retrieval path)."""

    if telemetry is None:
        return ""
    m6 = getattr(telemetry, "module_6_hybrid", None) or {}
    if not isinstance(m6, dict) or not m6:
        return ""
    sc = m6.get("signal_counts") or {}
    parts: list[str] = []
    if isinstance(sc, dict):
        for k, v in sorted(sc.items()):
            try:
                n = int(v)
            except (TypeError, ValueError):
                continue
            if n:
                parts.append(f"{k}={n}")
    fused = m6.get("fused_candidates") or []
    fused_n = len(fused) if isinstance(fused, list) else 0
    try:
        wall = int(m6.get("wall_ms") or 0)
    except (TypeError, ValueError):
        wall = 0
    attrs = [
        "layer=\"L2\"",
        "fusion=\"rrf\"",
        f"fused_candidates=\"{fused_n}\"",
        f"wall_ms=\"{wall}\"",
    ]
    if parts:
        attrs.append(f"signals=\"{' '.join(parts)}\"")
    return "<gt-pretask " + " ".join(attrs) + " />"


def _normalize_rel_path(path: str, config: GTRuntimeConfig) -> str:
    p = path.strip().strip("'\"")
    p = _path_relative_to_workspace(p, config)
    return _normalize_path(p)


def _same_repo_file(a: str, b: str, config: GTRuntimeConfig) -> bool:
    """True if two workspace-relative paths likely refer to the same file."""

    x = _normalize_rel_path(a, config)
    y = _normalize_rel_path(b, config)
    if x == y:
        return True
    if x.endswith("/" + y) or y.endswith("/" + x):
        return True
    bx, by = Path(x).name, Path(y).name
    return bx == by != "" and (x in y or y in x)


def _path_covered_by_validation(path: str, config: GTRuntimeConfig) -> bool:
    for v in config.verified_checks:
        if _same_repo_file(path, v, config):
            return True
    return False


def register_gt_validate_paths(command: str, config: GTRuntimeConfig) -> None:
    """Record paths from completed gt_validate shell lines (observation path)."""

    for m in _GT_VALIDATE_ARG_RE.finditer(command):
        raw = m.group(1).strip().strip("'\"")
        if not raw or raw.startswith("-"):
            continue
        rel = _normalize_rel_path(raw, config)
        if rel:
            config.verified_checks.add(rel)


def render_l5_advisory(config: GTRuntimeConfig) -> str:
    edited = sorted(config.edited_files)
    pending = sorted(config.pending_checks)
    unresolved = [p for p in pending if not _path_covered_by_validation(p, config)]
    explored_not_edited = sorted(config.viewed_files - config.edited_files)
    lines = [
        "[GT_GATE] Pre-submit review:",
        f"  Files edited: {len(edited)}",
        f"  Pending checks: {len(pending)} ({len(unresolved)} unresolved)",
    ]
    if unresolved:
        unresolved_summaries: list[tuple[str, str]] = []
        seen = set()
        for p, s in config.pending_summaries:
            if p in unresolved and p not in seen:
                unresolved_summaries.append((p, s))
                seen.add(p)
        for path, summary in unresolved_summaries[:3]:
            lines.append(f"  WARNING {path}: {summary[:150]}")
    if explored_not_edited:
        lines.append(
            "  Files explored but not edited: " + ", ".join(explored_not_edited[:3])
        )
    body = "\n".join(lines)
    return (
        f'<gt-advisory layer="L5" pending_count="{len(pending)}" unresolved_count="{len(unresolved)}">\n'
        + body
        + "\n</gt-advisory>"
    )


def append_observation(obs: Any, text: str) -> Any:
    current = getattr(obs, "content", "")
    if current is None:
        current = ""
    try:
        obs.content = str(current) + text
    except Exception:
        pass
    return obs


def _cmd_action(command: str, timeout: int = 30) -> Any:
    try:
        from openhands.events.action import CmdRunAction  # type: ignore[import]

        action = CmdRunAction(command=command)
        if hasattr(action, "set_hard_timeout"):
            action.set_hard_timeout(timeout)
        return action
    except Exception:
        return _FallbackCmdRunAction(command=command, timeout=timeout)


class _FallbackCmdRunAction:
    def __init__(self, command: str, timeout: int = 30) -> None:
        self.command = command
        self.timeout = timeout

    def set_hard_timeout(self, timeout: int) -> None:
        self.timeout = timeout


def _run_internal(orig_run_action: Callable[[Any], Any], command: str, timeout: int = 30) -> str:
    obs = orig_run_action(_cmd_action(command, timeout))
    return getattr(obs, "content", "") or getattr(obs, "stdout", "") or ""


def _upload_bytes_b64(runtime: Any, payload: bytes, target_path: str, timeout: int = 120) -> None:
    b64_chunks = _b64_chunks(payload)
    b64_path = f"{target_path}.b64"
    runtime.run_action(_cmd_action(f"mkdir -p {Path(target_path).parent.as_posix()}", 30))
    for idx, chunk in enumerate(b64_chunks):
        op = ">" if idx == 0 else ">>"
        runtime.run_action(_cmd_action(f"echo -n '{chunk}' {op} {b64_path}", timeout))
    runtime.run_action(
        _cmd_action(f"base64 -d {b64_path} > {target_path} && rm -f {b64_path}", timeout)
    )


def _bundle_dir_payload(source_dir: Path, arcname: str) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        tf.add(str(source_dir), arcname=arcname)
    return buf.getvalue()


def _download_graph_db_to_host(runtime: Any, graph_db_path: str) -> str:
    obs = runtime.run_action(_cmd_action(f"base64 -w0 {graph_db_path} 2>/dev/null", 120))
    b64_content = getattr(obs, "content", "") or getattr(obs, "stdout", "") or ""
    tokens = re.findall(r"[A-Za-z0-9+/=]{128,}", b64_content)
    if not tokens:
        return ""
    best = max(tokens, key=len).strip()
    if not best:
        return ""
    # Base64 payload in OH observations can contain wrapping noise; repair padding.
    best += "=" * ((4 - (len(best) % 4)) % 4)
    data = base64.b64decode(best)
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.write(data)
    tmp.flush()
    tmp.close()
    return tmp.name


def _download_text_from_container(orig_run_action: Callable[[Any], Any], path: str) -> str:
    """Best-effort download of a small text file from the container."""
    obs = _run_internal(orig_run_action, f"base64 -w0 {_sh_single_quote(path)} 2>/dev/null", 30)
    body = (obs or "").strip()
    if not body:
        return ""
    tokens = re.findall(r"[A-Za-z0-9+/=]{8,}", body)
    payload = max(tokens, key=len).strip() if tokens else body
    if not payload:
        return ""
    payload += "=" * ((4 - (len(payload) % 4)) % 4)
    try:
        return base64.b64decode(payload).decode("utf-8", errors="replace")
    except Exception:
        return ""


def install_l4_tools(runtime: Any, config: GTRuntimeConfig) -> list[str]:
    """Upload GT tool bundles. Returns tool names that appear in ``command -v`` output."""

    tool_names = ("gt_query", "gt_search", "gt_navigate", "gt_validate")
    runtime.run_action(_cmd_action(f"mkdir -p {config.tools_dir}", 30))
    for name in tool_names:
        source_dir = _TOOL_ROOT / name
        if not source_dir.exists():
            continue
        payload = _bundle_dir_payload(source_dir, name)
        tar_target = f"{config.tools_dir}/{name}.tar.gz"
        _upload_bytes_b64(runtime, payload, tar_target)
        runtime.run_action(
            _cmd_action(
                f"cd {config.tools_dir} && tar xzf {name}.tar.gz && rm -f {name}.tar.gz",
                120,
            )
        )
    # Strip CRLF and ensure bin scripts are executable (Windows-packaged repos).
    runtime.run_action(
        _cmd_action(
            f"find {config.tools_dir} -type f \\( -path '*/bin/*' -o -name '*.sh' \\) "
            r"-exec sed -i 's/\r$//' {} \; 2>/dev/null; "
            f"find {config.tools_dir} -type f -path '*/bin/*' -exec chmod +x {{}} \\; 2>/dev/null",
            60,
        )
    )
    gdb_esc = config.graph_db.replace("'", "'\"'\"'")
    root_esc = config.workspace_root.replace("'", "'\"'\"'")
    runtime.run_action(
        _cmd_action(
            f"grep -q 'GT_GRAPH_DB=' ~/.bashrc || echo 'export GT_GRAPH_DB=\"{gdb_esc}\"' >> ~/.bashrc; "
            f"grep -q 'GT_REPO_ROOT=' ~/.bashrc || echo 'export GT_REPO_ROOT=\"{root_esc}\"' >> ~/.bashrc; "
            "grep -q 'GT_PYTHON=python3' ~/.bashrc || echo 'export GT_PYTHON=python3' >> ~/.bashrc; "
            f"grep -q '{config.tools_dir}/gt_query/bin' ~/.bashrc || echo "
            f"'export PATH={config.tools_dir}/gt_query/bin:{config.tools_dir}/gt_search/bin:"
            f"{config.tools_dir}/gt_navigate/bin:{config.tools_dir}/gt_validate/bin:$PATH' >> ~/.bashrc",
            30,
        )
    )
    check = _run_internal(
        runtime.run_action,
        _env_prefix(config) + "command -v gt_query gt_search gt_navigate gt_validate 2>/dev/null",
        30,
    )
    uniq: list[str] = []
    for ln in check.strip().splitlines():
        ln = ln.strip()
        if not ln:
            continue
        base = Path(ln).name
        if base in tool_names and base not in uniq:
            uniq.append(base)

    runtime.run_action(
        _cmd_action(_env_prefix(config) + "python3 --version 2>&1 | head -1", 15)
    )

    return uniq


def _verify_graph_nonempty(runtime: Any, config: GTRuntimeConfig, orig_ra: Callable[[Any], Any]) -> tuple[int, int]:
    sql = (
        "sqlite3 "
        + _sh_single_quote(config.graph_db)
        + " \"SELECT (SELECT COUNT(*) FROM nodes), (SELECT COUNT(*) FROM edges);\""
        " 2>&1 || echo '0 0'"
    )
    raw = _run_internal(orig_ra, _env_prefix(config) + sql, 30).strip().split("\n")[0]
    raw = raw.strip()
    parts = [p.strip() for p in re.split(r"\s*\|\s*", raw, maxsplit=1)]
    nums: list[int] = []
    for p in parts:
        try:
            nums.append(int(p))
        except ValueError:
            continue
    if len(nums) >= 2:
        return nums[0], nums[1]
    return 0, 0


def install_graph_and_hook(runtime: Any, config: GTRuntimeConfig) -> list[str]:
    """Install package, gt-index graph, PATH tools."""

    host_index = os.environ.get("GT_INDEX_BINARY", "")
    if not host_index or not Path(host_index).exists():
        cand = _REPO_ROOT / "tools" / "sweagent" / "gt_edit" / "bin" / "gt-index"
        if cand.exists():
            host_index = str(cand)

    orig_ra = runtime.run_action

    if host_index and Path(host_index).exists():
        container_bin = "/tmp/" + Path(host_index).name
        try:
            runtime.copy_to(host_index, "/tmp/")
        except Exception:
            try:
                _upload_bytes_b64(runtime, Path(host_index).read_bytes(), container_bin)
            except Exception as exc:
                print(f"WARNING: gt-index upload failed (copy+b64): {exc}", flush=True)
            else:
                config.gt_index_bin = container_bin
        else:
            config.gt_index_bin = container_bin
        runtime.run_action(_cmd_action(f"chmod +x {config.gt_index_bin}", 30))

    groundtruth_pkg = _SRC_DIR / "groundtruth"
    if groundtruth_pkg.exists():
        payload = _bundle_dir_payload(groundtruth_pkg, "groundtruth")
        _upload_bytes_b64(runtime, payload, "/tmp/gt_src.tar.gz")
        runtime.run_action(
            _cmd_action(
                "cd /tmp && tar xzf gt_src.tar.gz && rm -f gt_src.tar.gz; "
                r"grep -q 'PYTHONPATH=/tmp:${PYTHONPATH:-}' ~/.bashrc || "
                r"echo 'export PYTHONPATH=/tmp:${PYTHONPATH:-}' >> ~/.bashrc; "
                r"export PYTHONPATH=/tmp:${PYTHONPATH:-}",
                120,
            )
        )

    chk = _run_internal(
        orig_ra,
        _env_prefix(config)
        + 'python3 -c "from groundtruth.hooks.post_edit import main; print(\\"GT_PKG_OK\\")" 2>&1',
        45,
    )
    if "GT_PKG_OK" not in chk:
        print(f"WARNING: GT package import verification failed: {chk[:500]}", flush=True)

    pyver = _run_internal(
        orig_ra,
        _env_prefix(config)
        + (
            'python3 -c "import sys; v=sys.version_info; '
            'assert v.major>=3 and v.minor>=10; print(sys.version.split()[0])" 2>&1'
        ),
        20,
    )
    if pyver.startswith("Traceback") or "AssertionError" in pyver:
        print(f"WARNING: container Python >=3.10 recommended for hooks: {pyver[:240]}", flush=True)

    runtime.run_action(
        _cmd_action(
            f"{config.gt_index_bin} -root={config.workspace_root} -output={config.graph_db}",
            120,
        )
    )

    nc, ec = _verify_graph_nonempty(runtime, config, orig_ra)
    if nc == 0 and ec == 0:
        print(
            "WARNING: graph.db has zero nodes/edges after build "
            f"(root={config.workspace_root}, db={config.graph_db})",
            flush=True,
        )
    else:
        print(f"GT graph sanity OK: nodes={nc} edges={ec}", flush=True)

    return install_l4_tools(runtime, config)


def _write_gt_telemetry(instance: Any, tel: GTTelemetry | None) -> None:
    if tel is None or instance is None:
        return
    blob = tel.finalize()
    try:
        instance["gt_telemetry"] = blob
    except Exception:
        try:
            setattr(instance, "gt_telemetry", blob)
        except Exception:
            pass


def wrap_runtime_run_action(runtime: Any, config: GTRuntimeConfig | None = None) -> Any:
    """Append GT evidence to agent-visible observations for eligible events."""

    config = config or GTRuntimeConfig()
    if getattr(runtime, "_gt_full_wrapped", False):
        return runtime
    orig_run_action = runtime.run_action

    def patched_run_action(action: Any) -> Any:
        act_text = _action_text(action)
        tel_obj = getattr(config, "telemetry", None)

        if _action_class(action) == "CmdRunAction" and "gt_validate" in act_text:
            register_gt_validate_paths(act_text, config)

        if tel_obj is not None and _action_class(action) == "CmdRunAction":
            if re.search(r"\bgt_(query|search|navigate|validate)\b", act_text):
                tel_obj.record_l4()

        obs = orig_run_action(action)
        event = classify_tool_event(action, source_exts=config.source_exts)
        instance_ref = getattr(runtime, "_gt_instance", None)

        def _hook_fatal(blob: str) -> bool:
            return "[GT_STATUS] error" in blob

        if event.kind == "post_view":
            rel_view = _normalize_rel_path(event.path, config)
            if rel_view:
                config.viewed_files.add(rel_view)
            hook_out = _run_internal(orig_run_action, make_view_hook_command(event, config), 30)
            if _hook_fatal(hook_out):
                print(f"GT HOOK ERROR (post_view:{event.path}): {hook_out[:400]}", flush=True)
            if tel_obj is not None:
                fatal = _hook_fatal(hook_out)
                empty_ev = any(
                    marker in hook_out
                    for marker in (
                        "[GT_STATUS] empty",
                        "[GT_STATUS] no_evidence:",
                        "[GT_STATUS] skipped:",
                    )
                )
                ok_ev = "[GT_STATUS] success" in hook_out
                tel_obj.record_hook("L3b", ok_ev and not fatal, empty=empty_ev or (not hook_out.strip()))
            evidence = (
                f'\n\n<gt-evidence trigger="post_view:{event.path}">\n'
                f"{hook_out.strip()}\n</gt-evidence>\n"
            )
            return append_observation(obs, evidence)

        if event.kind == "post_edit":
            if _is_scaffolding_path(event.path):
                rel_p = _normalize_rel_path(event.path, config)
                if rel_p:
                    config.edited_files.add(rel_p)
                reindex_cmd = make_reindex_command(event.path, config)
                reindex_out = _run_internal(orig_run_action, reindex_cmd, 120)
                if tel_obj is not None:
                    r_ok = bool(reindex_out.strip()) and not any(
                        w in reindex_out.lower() for w in ("panic", "fatal")
                    )
                    tel_obj.record_reindex(r_ok)
                    tel_obj.record_hook("L3", ok=False, empty=True)
                evidence = (
                    f'\n\n<gt-evidence trigger="post_edit:{event.path}">\n'
                    f"<gt-reindex command=\"{reindex_cmd}\">\n"
                    f"{reindex_out.strip()}\n</gt-reindex>\n"
                    "[GT_STATUS] skipped:scaffolding_file\n"
                    "</gt-evidence>\n"
                )
                return append_observation(obs, evidence)

            # L6 reindex BEFORE L3 post_edit hook — sequential ordering is load-bearing.
            reindex_cmd = make_reindex_command(event.path, config)
            reindex_out = _run_internal(orig_run_action, reindex_cmd, 120)
            if tel_obj is not None:
                r_ok = bool(reindex_out.strip()) and not any(
                    w in reindex_out.lower() for w in ("panic", "fatal")
                )
                tel_obj.record_reindex(r_ok)

            diff_text, old_content_text = _extract_diff_and_old_content(obs)
            diff_path = ""
            old_content_path = ""
            if diff_text:
                diff_path = "/tmp/gt_diff.txt"
                _write_text_to_container(orig_run_action, diff_text, diff_path)
            if old_content_text:
                old_content_path = "/tmp/gt_old.txt"
                _write_text_to_container(orig_run_action, old_content_text, old_content_path)
            hook_out = _run_internal(
                orig_run_action,
                make_edit_hook_command_with_artifacts(
                    event,
                    config,
                    diff_path=diff_path or None,
                    old_content_path=old_content_path or None,
                ),
                45,
            )
            if _hook_fatal(hook_out):
                print(f"GT HOOK ERROR (post_edit:{event.path}): {hook_out[:400]}", flush=True)

            rel_p = _normalize_rel_path(event.path, config)
            if rel_p:
                config.edited_files.add(rel_p)
            if rel_p and hook_out and not _hook_fatal(hook_out):
                low = hook_out.lower()
                first_line = ""
                for line in hook_out.splitlines():
                    if line.strip().startswith("[GT_"):
                        first_line = line.strip()
                        break
                needs_check = (
                    "[GT_STATUS] success" in hook_out
                    or "[GT_CONTRACT]" in hook_out
                    or "[GT_CALLER]" in hook_out
                    or "likely_invalid" in low
                )
                if needs_check:
                    config.pending_checks.add(rel_p)
                    if first_line:
                        config.pending_summaries.append((rel_p, first_line))

            if tel_obj is not None:
                fatal = _hook_fatal(hook_out)
                empty_ev = any(
                    marker in hook_out
                    for marker in (
                        "[GT_STATUS] empty",
                        "[GT_STATUS] no_evidence:",
                        "[GT_STATUS] skipped:",
                    )
                )
                ok_ev = "[GT_STATUS] success" in hook_out
                tel_obj.record_hook("L3", ok_ev and not fatal, empty=empty_ev)

            evidence = (
                f'\n\n<gt-evidence trigger="post_edit:{event.path}">\n'
                f"<gt-reindex command=\"{reindex_cmd}\">\n"
                f"{reindex_out.strip()}\n</gt-reindex>\n"
                f"{hook_out.strip()}\n</gt-evidence>\n"
            )
            return append_observation(obs, evidence)

        if event.kind == "finish":
            advisory = render_l5_advisory(config)
            if advisory:
                obs = append_observation(obs, "\n\n" + advisory + "\n")

            instance_ref = getattr(runtime, "_gt_instance", None)
            hook_log_path = os.environ.get("GT_HOOK_LOG", "/tmp/gt_hooks.log")
            hook_log = _download_text_from_container(orig_run_action, hook_log_path)
            if hook_log:
                try:
                    instance_ref["gt_hook_log"] = hook_log
                except Exception:
                    try:
                        setattr(instance_ref, "gt_hook_log", hook_log)
                    except Exception:
                        pass

            tel_fin = getattr(config, "telemetry", None)
            if tel_fin is not None:
                tel_fin.record_gate(bool(advisory and advisory.strip()))
                _write_gt_telemetry(instance_ref, tel_fin)

        if _action_class(action) == "CmdRunAction":
            lower_cmd = act_text.lower()
            is_submit_cmd = "/submit" in lower_cmd or bool(
                re.search(r"\bgit\s+diff\s+head\b", lower_cmd)
            )
            if is_submit_cmd:
                advisory = render_l5_advisory(config)
                obs = append_observation(obs, "\n\n" + advisory + "\n")

        return obs

    runtime.run_action = patched_run_action
    runtime._gt_full_wrapped = True
    runtime._gt_full_config = config
    return runtime


_ORIG_INITIALIZE_RUNTIME = None
_ORIG_GET_INSTRUCTION = None


def _b64_chunks(payload: bytes, chunk_size: int = 8000) -> list[str]:
    encoded = base64.b64encode(payload).decode("ascii")
    return [encoded[i : i + chunk_size] for i in range(0, len(encoded), chunk_size)]


def patched_initialize_runtime(runtime: Any, instance: Any, metadata: Any) -> None:
    if _ORIG_INITIALIZE_RUNTIME is not None:
        _ORIG_INITIALIZE_RUNTIME(runtime, instance, metadata)
    workspace_name = (
        getattr(instance, "instance_id", "")
        or (instance.get("instance_id", "") if isinstance(instance, dict) else "")
    )
    workspace_name = (workspace_name or "").strip()

    tentative = f"{WORKSPACE_ROOT}/{workspace_name}" if workspace_name else WORKSPACE_ROOT

    qr = _sh_single_quote(tentative + "/.git")
    wr = _sh_single_quote(WORKSPACE_ROOT + "/.git")

    probe_cmd = (
        f"if [ -d {qr} ]; then echo {_sh_single_quote(tentative)}; "
        f"elif [ -d {wr} ]; then echo {_sh_single_quote(WORKSPACE_ROOT)}; "
        f"else GITDIR=$(find {_sh_single_quote(WORKSPACE_ROOT)} -maxdepth 3 -type d -name .git "
        "-print -quit 2>/dev/null); "
        '[ -n "$GITDIR" ] && dirname "$GITDIR"; fi'
    )

    probed_root = (
        _run_internal(runtime.run_action, probe_cmd, 45).strip().split("\n")[0].strip().strip("'\"")
    )
    workspace_root = probed_root if probed_root else tentative

    tel = GTTelemetry(workspace_name or "unknown")
    config = GTRuntimeConfig(workspace_root=workspace_root, telemetry=tel)

    runtime._gt_instance = instance

    l4_ok = install_graph_and_hook(runtime, config)

    try:
        instance["gt_l4_tools"] = l4_ok
    except Exception:
        try:
            setattr(instance, "gt_l4_tools", l4_ok)
        except Exception:
            pass

    issue_text = getattr(instance, "problem_statement", "") or ""
    if not issue_text and isinstance(instance, dict):
        issue_text = str(instance.get("problem_statement", "") or "")
    issue_text = str(issue_text or "")

    brief = ""
    l2_tag = ""
    task_id = workspace_name or "unknown"

    brief_runner = (
        "import json, sys\n"
        "import inspect\n"
        "meta_path, issue_path = sys.argv[1], sys.argv[2]\n"
        'with open(meta_path, encoding="utf-8") as f:\n'
        "    meta = json.load(f)\n"
        'with open(issue_path, encoding="utf-8", errors="replace") as f:\n'
        "    issue = f.read()\n"
        "try:\n"
        "    from groundtruth.pretask.v7_brief import generate_brief\n"
        "except Exception as exc:\n"
        '    print(f"[GT_BRIEF_FAILED] import: {exc}")\n'
        "else:\n"
        "    kwargs = {\n"
        "        'issue_text': issue,\n"
        "        'repo_root': meta['repo_root'],\n"
        "        'graph_db': meta['graph_db'],\n"
        "        'return_telemetry': True,\n"
        "        'task_id': meta['task_id'],\n"
        "    }\n"
        "    try:\n"
        "        if 'confidence_threshold' in inspect.signature(generate_brief).parameters:\n"
        "            kwargs['confidence_threshold'] = 0.20\n"
        "    except Exception:\n"
        "        pass\n"
        "    out = generate_brief(**kwargs)\n"
        "    if hasattr(out, 'brief'):\n"
        '        print(str(getattr(out, "brief") or "").strip())\n'
        "        tel = getattr(out, 'telemetry', None)\n"
        "        m6 = {}\n"
        "        if tel is not None:\n"
        "            m6 = getattr(tel, 'module_6_hybrid', None) or {}\n"
        '        print("\\n---GT_L2_JSON---")\n'
        '        print(json.dumps(m6 if isinstance(m6, dict) else {}))\n'
        "    else:\n"
        "        print(str(out))\n"
    )

    if issue_text.strip():
        meta = {
            "repo_root": config.workspace_root,
            "graph_db": config.graph_db,
            "task_id": task_id,
        }
        _upload_bytes_b64(runtime, brief_runner.encode("utf-8"), "/tmp/gt_brief_runner.py")
        _upload_bytes_b64(runtime, json.dumps(meta).encode("utf-8"), "/tmp/gt_brief_meta.json")
        _upload_bytes_b64(runtime, issue_text.encode("utf-8"), "/tmp/gt_issue.txt")
        raw_br = (
            _run_internal(
                runtime.run_action,
                _env_prefix(config)
                + "python3 /tmp/gt_brief_runner.py /tmp/gt_brief_meta.json /tmp/gt_issue.txt 2>&1",
                180,
            ).strip()
        )
        segments = raw_br.split("---GT_L2_JSON---")
        brief = segments[0].strip()
        l2_blob: dict[str, Any] = {}
        if len(segments) > 1:
            try:
                l2_blob = json.loads(segments[1].strip())
            except Exception:
                l2_blob = {}
        fused_candidates = l2_blob.get("fused_candidates") if isinstance(l2_blob, dict) else None
        fused_n = len(fused_candidates) if isinstance(fused_candidates, list) else 0

        class _TelNS:
            def __init__(self, d: dict[str, Any]) -> None:
                self.module_6_hybrid = d

        l2_tag = _format_l2_pretask_tag(_TelNS(l2_blob))
        low_signal_brief = (
            not brief
            or "could not deterministically localize" in brief.lower()
            or "could not localize" in brief.lower()
        )
        if fused_n == 0 and low_signal_brief:
            keyword = "issue"
            for token in re.findall(r"[A-Za-z_][A-Za-z0-9_]{3,}", issue_text):
                low = token.lower()
                if low not in {"issue", "with", "from", "that", "this", "when", "then", "have"}:
                    keyword = token
                    break
            brief = (
                "GT could not rank files with high confidence (0 candidates from graph).\n"
                "Use gt_search to locate relevant symbols. Start with: "
                f"gt_search function {keyword}"
            )

        if (
            (not brief)
            or ("GT graph built" in brief)
            or ("[GT_BRIEF_FAILED]" in brief)
            or (
                len(brief) < 100
                and "GT could not rank files with high confidence" not in brief
            )
        ):
            brief = (
                f"[GT_BRIEF_FAILED] Brief generation produced no real content "
                f"(len={len(brief)}).\nRAW:\n{raw_br[:520]}"
            )
            tel.record_brief(False, bool(l2_tag))
        else:
            tel.record_brief(True, bool(l2_tag))
    else:
        tel.record_brief(False, False)

    if l2_tag and brief and "[GT_BRIEF_FAILED]" not in brief:
        brief = f"{brief}\n\n{l2_tag}"
    brief = _rewrite_site_package_paths_in_brief(brief, workspace_name, config.workspace_root)
    brief = _brief_max_tokens(brief)

    if not brief.strip():
        brief = (
            "GT graph built inside the task container.\n"
            f"- repo_root: {config.workspace_root}\n"
            f"- graph_db: {config.graph_db}\n"
            "- tools: gt_query <symbol>, gt_search <kind> <query>, "
            "gt_navigate <symbol> <mode>, gt_validate <file>\n"
            "- hooks: post-view and post-edit evidence are appended to tool observations."
        )

    try:
        instance["gt_brief"] = brief
    except Exception:
        try:
            setattr(instance, "gt_brief", brief)
        except Exception:
            pass
    wrap_runtime_run_action(runtime, config)


def generate_task_brief(instance: Any) -> str:
    """Generate or retrieve the GT pre-task brief for the first user turn."""

    injected = getattr(instance, "gt_brief", "") or os.environ.get("GT_STATIC_BRIEF", "")
    if not injected and hasattr(instance, "get"):
        try:
            injected = instance.get("gt_brief", "")
        except Exception:
            injected = ""
    if injected.strip():
        return injected.strip()

    issue_text = getattr(instance, "problem_statement", "") or ""
    instance_id = getattr(instance, "instance_id", "") or getattr(instance, "id", "") or ""
    indexes_root = os.environ.get("GT_PREBUILT_INDEXES_ROOT", "")
    if not issue_text.strip() or not instance_id or not indexes_root:
        return ""

    graph_db = Path(indexes_root) / instance_id / "graph.db"
    if not graph_db.exists():
        return ""
    repo_root = os.environ.get("GT_REPO_EXTRACTS_ROOT", "")
    repo_path = str(Path(repo_root) / instance_id) if repo_root else ""
    if repo_path and not Path(repo_path).is_dir():
        repo_path = ""

    try:
        from groundtruth.pretask.v22_brief import generate_brief  # type: ignore[import]

        return (generate_brief(issue_text, repo_path, str(graph_db)) or "").strip()
    except Exception:
        return ""


def patched_get_instruction(instance: Any, metadata: Any) -> Any:
    if _ORIG_GET_INSTRUCTION is None:
        raise RuntimeError("OpenHands get_instruction patch was not initialized")
    msg = _ORIG_GET_INSTRUCTION(instance, metadata)
    content = getattr(msg, "content", "") or ""
    brief = generate_task_brief(instance)
    if brief:
        content = f"<gt-task-brief>\n{brief}\n</gt-task-brief>\n\n" + content
    tools_installed = getattr(instance, "gt_l4_tools", None)
    if tools_installed is None and isinstance(instance, dict):
        tools_installed = instance.get("gt_l4_tools")
    try:
        msg.content = content + render_l4_tool_footer(list(tools_installed or []))
    except Exception:
        pass
    return msg


def patch_run_infer(ri_module: Any) -> None:
    """Patch the OpenHands SWE-bench run_infer module."""

    global _ORIG_INITIALIZE_RUNTIME, _ORIG_GET_INSTRUCTION
    _ORIG_INITIALIZE_RUNTIME = ri_module.initialize_runtime
    _ORIG_GET_INSTRUCTION = ri_module.get_instruction
    ri_module.initialize_runtime = patched_initialize_runtime
    ri_module.get_instruction = patched_get_instruction


def run_openhands_fork_main(ri_module: Any, argv: list[str]) -> None:
    """Run v0.54-style OpenHands SWE-bench module after monkey-patching."""

    sys.argv = ["run_infer.py"] + argv

    import openhands.agenthub  # noqa: F401
    from datasets import load_dataset
    from evaluation.utils.shared import make_metadata, prepare_dataset, run_evaluation
    from openhands.core.config import get_evaluation_parser, get_llm_config_arg
    from openhands.core.config.condenser_config import NoOpCondenserConfig
    from openhands.core.config.utils import get_condenser_config_arg

    parser = get_evaluation_parser()
    parser.add_argument("--dataset", type=str, default="princeton-nlp/SWE-bench")
    parser.add_argument("--split", type=str, default="test")
    parser.add_argument("--mode", type=str, default="swe", choices=["swe", "swt", "swt-ci"])
    args, _ = parser.parse_known_args()
    print(f"OH_GT_FULL_ARGS argv={argv!r} eval_output_dir={args.eval_output_dir!r}", flush=True)
    if args.eval_output_dir is None and os.environ.get("OUT_ROOT"):
        args.eval_output_dir = os.environ["OUT_ROOT"]
        print(f"OH_GT_FULL_ARGS fallback eval_output_dir={args.eval_output_dir!r}", flush=True)
    if getattr(args, "agent_cls", None) is None:
        args.agent_cls = "CodeActAgent"
        print("OH_GT_FULL_ARGS fallback agent_cls='CodeActAgent'", flush=True)

    dataset = load_dataset(args.dataset, split=args.split)
    ri_module.set_dataset_type(args.dataset)
    tests = ri_module.filter_dataset(dataset.to_pandas(), "instance_id")

    llm_config = get_llm_config_arg(args.llm_config) if args.llm_config else None
    if llm_config is None:
        raise ValueError(f"Missing or unknown llm_config: {args.llm_config}")
    llm_config.log_completions = True
    llm_config.modify_params = False

    condenser_name = os.environ.get("EVAL_CONDENSER")
    condenser_config = (
        get_condenser_config_arg(condenser_name) if condenser_name else NoOpCondenserConfig()
    )
    dataset_description = args.dataset.replace("/", "__") + "-" + args.split.replace("/", "__")
    metadata = make_metadata(
        llm_config,
        dataset_description,
        args.agent_cls,
        args.max_iterations,
        args.eval_note,
        args.eval_output_dir,
        details={"mode": args.mode},
        condenser_config=condenser_config,
    )
    output_file = os.path.join(metadata.eval_output_dir, "output.jsonl")
    instances = prepare_dataset(tests, output_file, args.eval_n_limit)
    run_evaluation(
        instances,
        metadata,
        output_file,
        args.eval_num_workers,
        ri_module.process_instance,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="OpenHands GT full-potential wrapper")
    parser.add_argument("--instance-ids", default="")
    args, remainder = parser.parse_known_args()

    try:
        from evaluation.benchmarks.swe_bench import run_infer as ri  # type: ignore[import]
    except ImportError as exc:
        print(f"FATAL: cannot import OpenHands run_infer: {exc}", file=sys.stderr)
        sys.exit(1)

    patch_run_infer(ri)

    if args.instance_ids:
        ids = [s.strip() for s in args.instance_ids.split(",") if s.strip()]
        config_path = Path(ri.__file__).resolve().parent / "config.toml"
        config_path.write_text("selected_ids = " + repr(ids) + "\n", encoding="utf-8")

    sys.argv = ["run_infer.py"] + remainder
    if hasattr(ri, "main"):
        ri.main()
        return
    run_openhands_fork_main(ri, remainder)


if __name__ == "__main__":
    main()
