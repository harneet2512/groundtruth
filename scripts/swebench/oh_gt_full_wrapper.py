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
import hashlib
import io
import json
import os
import re
import sys
import tarfile
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import cost_tracking  # noqa: F401


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

GT_ITER_METRICS = os.getenv("GT_ITER_METRICS", "/tmp/gt_iter_metrics.jsonl")


def _metrics_path(config: Any, name: str) -> str:
    """Return a per-task metrics file path to avoid cross-worker clobbering.

    Falls back to the global /tmp path when config has no task_id (single-worker
    mode or early init).
    """
    task_id = getattr(config, "_meta_instance_id", None) if config is not None else None
    if task_id and task_id != "global":
        # Sanitize task_id for filesystem (replace / with _)
        safe_id = task_id.replace("/", "_").replace("\\", "_")
        return f"/tmp/gt_{name}_{safe_id}.jsonl"
    return f"/tmp/gt_{name}.jsonl"


def _classify_edit_path(p: str) -> str:
    base = os.path.basename(p or "")
    if base.startswith(("reproduce_", "debug_", "test_")) or "/tests/" in (p or "") or "/test_" in (p or ""):
        return "scaffold"
    return "source"


def _record_edit_iter(config: Any, iter_num: int, path: str) -> None:
    if config._iter_state["iter_to_first_edit"] is None:
        config._iter_state["iter_to_first_edit"] = iter_num
    if config._iter_state["iter_to_first_source_edit"] is None and _classify_edit_path(path) == "source":
        config._iter_state["iter_to_first_source_edit"] = iter_num


def _reset_iter_state(config: Any, task_id: str) -> None:
    config._iter_state.update({"task_id": task_id, "iter_to_first_edit": None, "iter_to_first_source_edit": None})


def _flush_iter_state(config: Any = None) -> None:
    state = config._iter_state if config is not None else {"task_id": None}
    path = _metrics_path(config, "iter_metrics") if config is not None else GT_ITER_METRICS
    with open(path, "a") as f:
        f.write(json.dumps(state) + "\n")


GT_DIFF_TIMELINE = os.getenv("GT_DIFF_TIMELINE", "/tmp/gt_diff_timeline.jsonl")
GT_TASK_METRICS = os.getenv("GT_TASK_METRICS", "/tmp/gt_task_metrics.jsonl")


def _record_diff_snapshot(orig_run_action: Any, config: Any, event_path: str, action_count: int) -> None:
    repo_root = _sh_single_quote(config.workspace_root)
    cmd = f"cd {repo_root} && git diff --shortstat 2>/dev/null; echo '---'; git diff --name-only 2>/dev/null | head -20"
    out = _run_internal(orig_run_action, cmd, 10)

    parts = (out or "").split("---")
    shortstat = parts[0].strip() if parts else ""
    name_list = [x.strip() for x in parts[1].strip().split("\n") if x.strip()] if len(parts) > 1 else []
    name_only_count = len(name_list)

    files_changed = insertions = deletions = 0
    m = re.search(r"(\d+) files? changed", shortstat)
    if m:
        files_changed = int(m.group(1))
    m = re.search(r"(\d+) insertion", shortstat)
    if m:
        insertions = int(m.group(1))
    m = re.search(r"(\d+) deletion", shortstat)
    if m:
        deletions = int(m.group(1))

    diff_nonzero = files_changed > 0 or name_only_count > 0

    if name_only_count < config._prev_name_only_count:
        config._new_files_deleted += config._prev_name_only_count - name_only_count
    if event_path and event_path not in config.edited_files and not event_path.startswith("["):
        config._new_files_created += 1
    config._prev_name_only_count = name_only_count

    record = {
        "iter": action_count,
        "event_path": event_path,
        "shortstat": shortstat,
        "files_changed": files_changed,
        "insertions": insertions,
        "deletions": deletions,
        "changed_files": name_list,
        "diff_nonzero": diff_nonzero,
    }
    _diff_path = _metrics_path(config, "diff_timeline")
    with open(_diff_path, "a") as f:
        f.write(json.dumps(record) + "\n")

    if diff_nonzero:
        if not config._diff_ever_nonzero:
            config._diff_first_nonzero_iter = action_count
        config._diff_ever_nonzero = True
        config._diff_last_nonzero_iter = action_count
        config._diff_just_collapsed = False
    elif config._diff_ever_nonzero:
        config._diff_collapsed_count += 1
        config._diff_collapse_after_file = event_path
        config._diff_just_collapsed = True
        print(f"[GT_META] DIFF COLLAPSED TO ZERO at iter {action_count} after {event_path}", flush=True)


def _classify_behavior(config: Any) -> str:
    has_source = any(_is_real_source_edit(f, config) for f in config.edited_files)
    if has_source and config._diff_collapsed_count > 0:
        return "collapsed"
    if has_source:
        return "source_edit"
    if config.edited_files:
        return "non_source_edit_loop"
    return "read_run_stall"


def _flush_task_end_metrics(config: Any, phase: str = "finish") -> None:
    if getattr(config, '_metrics_flushed', False):
        return
    config._metrics_flushed = True

    # Close structured telemetry writer
    writer = getattr(config, "_telemetry_writer", None)
    if writer is not None:
        try:
            writer.close()
            print(f"[GT_META] Telemetry writer closed. Files: {writer.layer_events_path}", flush=True)
        except Exception:
            pass

    _flush_iter_state(config)

    if hasattr(config, '_task_end_orig_run_action') and config._task_end_orig_run_action:
        try:
            _record_diff_snapshot(config._task_end_orig_run_action, config, f"[{phase}]", config.action_count)
        except Exception:
            pass

    task_metrics = {
        "task_id": config._meta_instance_id,
        "condenser": os.environ.get("EVAL_CONDENSER", "none"),
        "action_count": config.action_count,
        "total_edits": len(config.edited_files),
        "l5_fired": config._l5_scaffold_fired,
        "l5_fire_count": config._l5_metrics.get("l5_fire_count", 0),
        "first_real_source_edit": any(_is_real_source_edit(f, config) for f in config.edited_files),
        "diff_ever_nonzero": config._diff_ever_nonzero,
        "first_nonzero_diff_iter": config._diff_first_nonzero_iter,
        "last_nonzero_diff_iter": config._diff_last_nonzero_iter,
        "diff_collapsed_count": config._diff_collapsed_count,
        "collapse_after_file": config._diff_collapse_after_file,
        "new_files_created": config._new_files_created,
        "new_files_deleted": config._new_files_deleted,
        "additional_scratch_after_l5": config._l5_metrics.get("num_additional_scaffolds_after_l5", 0),
        "behavior_class": _classify_behavior(config),
        "phase": phase,
    }
    _task_metrics_path = _metrics_path(config, "task_metrics")
    with open(_task_metrics_path, "a") as f:
        f.write(json.dumps(task_metrics) + "\n")
    print(f"[GT_META] Task metrics ({phase}): {json.dumps(task_metrics)}", flush=True)


INTERNAL_GT_MARKERS = (
    "/tmp/gt-index",
    "gt-index-linux",
    "/tmp/gt_tools/",
    "groundtruth.hooks.post_edit",
    "groundtruth.hooks.post_view",
    "gt_hook.py",
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
    last_visible_observation: Any = None
    telemetry: Any = None  # GTTelemetry, optional
    evidence_sent: dict[str, str] = field(default_factory=dict)  # file -> evidence hash for dedup
    brief_candidates: set[str] = field(default_factory=set)
    action_count: int = 0  # PRF Iterative Checkpoints
    max_iter: int = 100
    scaffold_stripped: bool = False
    interaction_log: list[dict[str, Any]] = field(default_factory=list)
    instance_ref: Any = None
    _meta_instance_id: str = "global"
    _l5_scaffold_fired: bool = False
    _l5_last_scaffold_file: str = ""
    _l5_metrics: dict[str, Any] = field(default_factory=lambda: {
        "l5_scaffold_fired": False,
        "l5_fire_count": 0,
        "source_edit_after_l5": False,
        "num_additional_scaffolds_after_l5": 0,
        "touched_brief_candidate_after_l5": False,
    })
    _l5_edit_counts_per_file: dict[str, int] = field(default_factory=dict)
    _diff_ever_nonzero: bool = False
    _diff_first_nonzero_iter: int = 0
    _diff_last_nonzero_iter: int = 0
    _diff_collapsed_count: int = 0
    _diff_collapse_after_file: str = ""
    _diff_just_collapsed: bool = False
    _new_files_created: int = 0
    _new_files_deleted: int = 0
    _task_end_orig_run_action: Any = None
    _metrics_flushed: bool = False
    _prev_name_only_count: int = 0
    _telemetry_writer: Any = None  # GTTelemetryWriter, initialized when GT_STRUCTURED_EVENTS=1
    _pending_next_actions: list[dict[str, Any]] = field(default_factory=list)  # Online tracker for L5 ignored_next_action
    _l5_governor: Any = None
    _iter_state: dict[str, Any] = field(default_factory=lambda: {
        "task_id": None, "iter_to_first_edit": None, "iter_to_first_source_edit": None,
    })


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

    def record_l4_prefetch(self, queries: int, lines: int) -> None:
        for _ in range(queries):
            self._bump("L4", "ok")
        if queries == 0:
            self._bump("L4", "skipped")

    def utilization(self) -> dict[str, float]:
        """Rough 0–1 utilization per layer (diagnostic only)."""

        def score(layer: str) -> float:
            b = self.layer_hits.get(layer, {})
            ok, fail, skipped = b.get("ok", 0), b.get("fail", 0), b.get("skipped", 0)
            total = ok + fail + skipped
            if total == 0:
                return 0.0
            if ok > 0:
                return 1.0
            if skipped > 0 and fail == 0:
                return 1.0
            return ok / (ok + fail) if (ok + fail) else 0.0

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


def _is_real_source_edit(path: str, config: GTRuntimeConfig) -> bool:
    """Source edit = not scaffold, not test, and either indexed in graph.db or a source extension."""
    if _is_scaffolding_path(path):
        return False
    rel = _normalize_rel_path(path, config) or path
    base = Path(rel).name.lower()
    if base.startswith("test_") or "/tests/" in rel.lower() or "/test/" in rel.lower():
        return False
    if config.graph_db and os.path.exists(config.graph_db):
        try:
            import sqlite3
            conn = sqlite3.connect(config.graph_db)
            row = conn.execute(
                "SELECT 1 FROM nodes WHERE file_path LIKE ? AND is_test = 0 LIMIT 1",
                (f"%{rel}",),
            ).fetchone()
            conn.close()
            if row is not None:
                return True
            # File not in graph.db — could be a new file the agent created.
            # Fall back to extension check: treat as source if it has a source extension.
            return _is_source_path(rel, config.source_exts)
        except Exception:
            pass
    return _is_source_path(path, config.source_exts)


def _render_scaffold_advisory(scaffold_path: str, config: GTRuntimeConfig) -> str:
    """Directive advisory: stop scaffolding, touch source first."""
    scaffold_name = Path(scaffold_path).name
    lines = [
        '<gt-advisory layer="L5" trigger="non_source_without_progress">',
        "You have not made durable source progress yet.",
        f"Do not create more scratch or test files (last: {scaffold_name}).",
        "Edit source files first.",
    ]
    candidates = sorted(config.brief_candidates)[:3]
    if candidates and config.graph_db and os.path.exists(config.graph_db):
        lines.append("Start with one of these source files:")
        try:
            import sqlite3
            conn = sqlite3.connect(config.graph_db)
            for c in candidates:
                row = conn.execute(
                    "SELECT COUNT(*) FROM edges e JOIN nodes n ON e.target_id = n.id "
                    "WHERE n.file_path LIKE ? AND e.type = 'CALLS'",
                    (f"%{c}",),
                ).fetchone()
                caller_count = row[0] if row else 0
                lines.append(f"  {c} ({caller_count} callers)")
            conn.close()
        except Exception:
            for c in candidates:
                lines.append(f"  {c}")
    elif candidates:
        lines.append("Start with one of these source files:")
        for c in candidates:
            lines.append(f"  {c}")
    else:
        lines.append("Use gt_search to find the first source file to inspect.")
    lines.append("</gt-advisory>")
    return "\n".join(lines)


def _maybe_fire_l5(
    config: GTRuntimeConfig,
    path: str,
    obs: Any,
    act_text: str,
    tel_obj: Any,
    instance_ref: Any,
) -> Any:
    """Fire L5 on a non-source edit when there is no prior source progress."""
    if path == config._l5_last_scaffold_file:
        return obs
    config._l5_scaffold_fired = True
    config._l5_last_scaffold_file = path
    config._l5_metrics["l5_scaffold_fired"] = True
    config._l5_metrics["l5_fire_count"] = config._l5_metrics.get("l5_fire_count", 0) + 1
    advisory = _render_scaffold_advisory(path, config)
    if advisory:
        print(f"[GT_META] L5 non_source_edit fired for {path}", flush=True)
        _l5_ns_eid = _emit_structured_event(
            config, "L5", "non_source_edit",
            rendered_text=advisory, file_path=path,
        )
        _log_gt_interaction(config, "L5", f"non_source:{path}", "redirect", advisory,
            agent_action_before=act_text[:300], event_id=_l5_ns_eid or "")
        obs = append_observation(obs, "\n\n" + advisory + "\n")
        if tel_obj is not None:
            tel_obj.record_gate(True)
            _write_gt_telemetry(instance_ref, tel_obj)
    return obs


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
    if not config.gt_index_bin:
        return ""
    rel = _path_relative_to_workspace(path, config)
    return (
        f"{config.gt_index_bin} -root={config.workspace_root} "
        f"-file={rel} -output={config.graph_db}"
    )


def make_view_hook_command(event: HookEvent, config: GTRuntimeConfig) -> str:
    rel_path = _path_relative_to_workspace(event.path, config)
    cmd = (
        _env_prefix(config)
        + "python3 -m groundtruth.hooks.post_view "
        + f"--root={config.workspace_root} --db={config.graph_db} --file={rel_path}"
    )
    if os.environ.get("GT_REBUILD_L3B", "0") == "1":
        ratio = config.action_count / max(config.max_iter, 1)
        cmd += f" --iteration-ratio={ratio:.2f}"
        cmd += f" --total-candidates={len(getattr(config, 'brief_candidates', set()))}"
    if os.environ.get("GT_STRUCTURED_EVENTS", "0") == "1":
        cmd += " --structured-output"
    return cmd


def make_edit_hook_command(event: HookEvent, config: GTRuntimeConfig) -> str:
    return make_edit_hook_command_with_artifacts(event, config)


def make_edit_hook_command_with_artifacts(
    event: HookEvent,
    config: GTRuntimeConfig,
    *,
    diff_path: str | None = None,
    old_content_path: str | None = None,
    mode: str = "post_edit",
    iteration_ratio: float = 0.0,
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
    if os.environ.get("GT_REBUILD_L3", "0") == "1":
        cmd += f" --mode={mode}"
        cmd += f" --iteration-ratio={iteration_ratio:.2f}"
    if os.environ.get("GT_STRUCTURED_EVENTS", "0") == "1":
        cmd += " --structured-output"
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
    return ""


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


def _l5_unresolved_paths(config: GTRuntimeConfig) -> list[str]:
    pending = sorted(config.pending_checks)
    return [p for p in pending if not _path_covered_by_validation(p, config)]


def render_l5_advisory(config: GTRuntimeConfig) -> str:
    edited = sorted(config.edited_files)
    pending = sorted(config.pending_checks)
    unresolved = _l5_unresolved_paths(config)
    explored_not_edited = sorted(config.viewed_files - config.edited_files)
    if not edited and not pending and not explored_not_edited:
        return ""

    # L5: Detect stuck patterns (independent of L1 candidates — never names specific files)
    redirect_msg = ""
    scaffold_creates = sum(1 for f in edited if any(
        Path(f).name.startswith(p) for p in ("reproduce_", "repro_", "debug_", "test_fix_", "scratch_", "temp_")
    ))
    edit_counts: dict[str, int] = {}
    for f in edited:
        edit_counts[f] = edit_counts.get(f, 0) + 1
    edit_loops = any(c >= 3 for c in edit_counts.values())

    if scaffold_creates >= 3:
        candidates = sorted(config.brief_candidates)[:3]
        if candidates:
            redirect_msg = (
                "\n[GT_ADVISORY] You have created {} scaffolding files without editing source code. "
                "Edit these source files instead: {}".format(scaffold_creates, ", ".join(candidates))
            )
        else:
            redirect_msg = (
                "\n[GT_ADVISORY] You have created {} scaffolding files without editing source code. "
                "The fix is in existing source files. Use gt_search to find the relevant source file.".format(scaffold_creates)
            )
    elif edit_loops:
        looped = [f for f, c in edit_counts.items() if c >= 3]
        candidates = sorted(config.brief_candidates)[:3]
        if candidates:
            candidate_msg = " Focus on: {}".format(", ".join(candidates))
        else:
            candidate_msg = " Use gt_search to find the correct source file."
        redirect_msg = (
            "\n[GT_ADVISORY] You have edited {} {} times without converging. "
            "The current edits are not fixing the issue.{}".format(
                looped[0], edit_counts[looped[0]], candidate_msg
            )
        )

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
    body = "\n".join(lines) + redirect_msg
    return (
        f'<gt-advisory layer="L5" pending_count="{len(pending)}" unresolved_count="{len(unresolved)}">\n'
        + body
        + "\n</gt-advisory>"
    )


def _compute_has_real_evidence(layer: str, ev_type: str, gt_sent: str) -> bool:
    if not gt_sent or not gt_sent.strip():
        return False
    s = gt_sent.strip()
    if ev_type == "GT_OK":
        return False
    if layer == "L1":
        return "Traceback" not in s and "Error" not in s[:200] and len(s) > 50
    if layer == "L5":
        return "[GT_GATE]" in s or "[GT_ADVISORY]" in s
    if layer == "L6":
        return ev_type == "reindex_ok"
    return any(tag in s for tag in (
        "[GT_CHANGE]", "[GT_CONTRACT]", "[GT_PATTERN]",
        "[GT_STRUCTURAL]", "[GT_SEMANTIC]", "[GT_STATUS]",
        "[VERIFIED]", "[POSSIBLE]", "[GT_CALLER]",
        "Called by:", "Calls into:", "Imported by:",
    ))


def _log_gt_interaction(
    config: GTRuntimeConfig,
    layer: str,
    trigger: str,
    ev_type: str,
    gt_sent: str,
    agent_action_before: str = "",
    event_id: str = "",
    parent_event_id: str = "",
    next_action_type: str = "",
    next_action_file: str = "",
    next_action_command: str = "",
    next_action_test: str = "",
) -> None:
    """Record a GT→agent interaction for post-run analysis.

    Write-through: every call immediately appends one JSON line to a
    per-task file ``/tmp/gt_interactions_<task_id>.jsonl``.  The in-memory
    list + instance_ref flush are kept for backward compatibility but the
    file is the primary mechanism (survives max_iter timeout, finish-event
    misses, and instance_ref injection failures).

    Enhanced fields:
      - ``gt_sent``: full text GT injected (NOT truncated for L1 briefs)
      - ``agent_action_before``: what the agent was doing when GT fired
      - ``agent_action_after``: filled in on the NEXT action (see below)

    On each call, checks if the PREVIOUS entry in config.interaction_log is
    missing ``agent_action_after``.  If so, backfills it with the current
    trigger, creating a GT→agent→response chain for post-run analysis.
    """
    # Backfill agent_action_after on the PREVIOUS entry
    if config.interaction_log:
        prev = config.interaction_log[-1]
        if not prev.get("agent_action_after"):
            prev["agent_action_after"] = trigger
            # Also update the file — append a correction line
            try:
                correction = {
                    "type": "_backfill",
                    "target_iter": prev.get("iter"),
                    "target_layer": prev.get("layer"),
                    "agent_action_after": trigger,
                }
                _interactions_path = _metrics_path(config, "interactions")
                with open(_interactions_path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(correction) + "\n")
            except Exception:
                pass

    entry = {
        "timestamp": time.time(),
        "iter": config.action_count,
        "layer": layer,
        "trigger": trigger,
        "type": ev_type,
        "gt_sent": gt_sent,
        "gt_sent_bytes": len(gt_sent.encode("utf-8", errors="replace")),
        "gt_sent_tokens": len(gt_sent.split()) if gt_sent else 0,
        "has_real_evidence": _compute_has_real_evidence(layer, ev_type, gt_sent),
        "agent_action_before": agent_action_before,
        "agent_action_after": "",
        "event_id": event_id,
        "parent_event_id": parent_event_id,
        "next_action_type": next_action_type,
        "next_action_file": next_action_file,
        "next_action_command": next_action_command,
        "next_action_test": next_action_test,
    }
    config.interaction_log.append(entry)

    # Write-through to file — primary persistence mechanism
    _instance_id = getattr(config, "_meta_instance_id", "global")
    _meta_path = f"/tmp/gt_meta_{_instance_id}.jsonl"
    try:
        with open(_meta_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception:
        pass
    # Also write to per-task interactions path
    try:
        _interactions_path = _metrics_path(config, "interactions")
        with open(_interactions_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception:
        pass

    # Belt-and-suspenders: push to instance_ref if available
    ref = config.instance_ref
    if ref is not None:
        try:
            if isinstance(ref, dict):
                ref["gt_interactions"] = list(config.interaction_log)
            else:
                setattr(ref, "gt_interactions", list(config.interaction_log))
        except Exception:
            pass


def _emit_structured_event(
    config: GTRuntimeConfig,
    layer: str,
    event_type: str,
    *,
    emitted: bool = True,
    suppressed: bool = False,
    suppression_reason: str | None = None,
    rendered_text: str = "",
    evidence_items: list[dict] | None = None,
    next_action_type: str | None = None,
    next_action_file: str | None = None,
    next_action_test: str | None = None,
    file_path: str | None = None,
    parent_event_id: str | None = None,
    hook_output: str = "",
    verification_kind: str | None = None,
) -> str | None:
    """Emit a GTLayerEvent if GT_STRUCTURED_EVENTS is enabled. Returns event_id or None."""
    writer = getattr(config, "_telemetry_writer", None)
    if writer is None:
        return None
    try:
        from groundtruth.telemetry.schemas import GTLayerEvent

        items = evidence_items or []
        if not items and "__GT_STRUCTURED__" in hook_output:
            parts = hook_output.split("__GT_STRUCTURED__", 1)
            if len(parts) == 2:
                try:
                    items = json.loads(parts[1].strip().splitlines()[0])
                except Exception:
                    pass

        event = GTLayerEvent(
            layer=layer,
            event_type=event_type,
            eligible=True,
            emitted=emitted,
            suppressed=suppressed,
            suppression_reason=suppression_reason,
            iter=config.action_count,
            max_iter=config.max_iter,
            rendered_text=rendered_text[:2000] if rendered_text else None,
            evidence_items=items,
            next_action_type=next_action_type,
            next_action_file=next_action_file,
            verification_kind=verification_kind,
            next_action_test=next_action_test,
            file_path=file_path,
            parent_event_id=parent_event_id,
        )
        return writer.emit_layer_event(event)
    except Exception as exc:
        print(f"[GT_META] structured event emission failed: {exc}", flush=True)
        return None


def _emit_belief_event(
    config: GTRuntimeConfig,
    file_path: str,
    new_status: str,
    reason: str,
    source_event_id: str = "",
    previous_status: str | None = None,
    score: float | None = None,
) -> None:
    """Emit a GTBeliefEvent if writer is active."""
    writer = getattr(config, "_telemetry_writer", None)
    if writer is None:
        return
    try:
        from groundtruth.telemetry.schemas import GTBeliefEvent
        event = GTBeliefEvent(
            file_path=file_path,
            new_status=new_status,
            reason=reason,
            source_event_id=source_event_id or "",
            previous_status=previous_status,
            new_score=score,
            iter=config.action_count,
        )
        writer.emit_belief_event(event)
    except Exception:
        pass


def _register_pending_next_action(config: GTRuntimeConfig, event_id: str, next_action_type: str, next_action_file: str = "") -> None:
    """Register a GT next_action for online tracking. Only tracks actionable types."""
    if not next_action_type or next_action_type in ("", "NONE", "NONE_UNVERIFIABLE", None):
        return
    if os.environ.get("GT_L5_STRUCTURAL_UNVERIFIED", "0") != "1":
        return
    config._pending_next_actions.append({
        "event_id": event_id or "",
        "next_action_type": next_action_type,
        "next_action_file": next_action_file,
        "iter_emitted": config.action_count,
        "checked_count": 0,
        "followed": False,
    })


def _check_pending_next_actions(config: GTRuntimeConfig, current_action_file: str = "", current_action_type: str = "", obs: Any = None) -> Any:
    """Check pending next_actions against agent's real action. Returns obs (possibly with L5b appended)."""
    if not config._pending_next_actions:
        return obs
    expired: list[int] = []
    for i, pending in enumerate(config._pending_next_actions):
        pending["checked_count"] += 1
        # Check if action matches
        pf = pending.get("next_action_file", "")
        if pf and current_action_file and (pf in current_action_file or current_action_file in pf):
            pending["followed"] = True
        if pending["checked_count"] >= 3:
            if not pending["followed"]:
                # Full L5 -> L5b chain
                l5_eid = _emit_structured_event(
                    config, "L5", "ignored_next_action",
                    parent_event_id=pending["event_id"],
                    next_action_type=pending["next_action_type"],
                    next_action_file=pending.get("next_action_file"),
                )
                nat = pending["next_action_type"]
                naf = pending.get("next_action_file", "")
                msg = (
                    f"[GT L5: Ignored Structural Witness]\n"
                    f"Evidence: GT suggested {nat} for {naf} but agent did not follow within 3 actions.\n"
                    f"Next action: {nat.lower().replace('_', ' ')} {naf}"
                )
                try:
                    from groundtruth.trajectory.hooks import L5bSafetyChecker
                    ratio = config.action_count / max(config.max_iter, 1)
                    is_safe, reason = L5bSafetyChecker.validate(msg, ratio)
                except Exception:
                    is_safe, reason = True, None
                if is_safe and obs is not None:
                    l5b_eid = _emit_structured_event(
                        config, "L5b", "intervention_ignored_next_action",
                        parent_event_id=l5_eid, rendered_text=msg,
                        next_action_type=nat, next_action_file=naf,
                    )
                    obs = append_observation(obs, f"\n\n{msg}\n")
                    _log_gt_interaction(
                        config, "L5", "ignored_next_action", "advisory", msg,
                        event_id=l5b_eid or "", parent_event_id=l5_eid or "",
                        next_action_type=nat, next_action_file=naf,
                    )
                elif not is_safe:
                    _l5b_blk_eid = _emit_structured_event(
                        config, "L5b", "blocked_by_safety",
                        parent_event_id=l5_eid, suppressed=True,
                        suppression_reason=reason,
                    )
                    _log_gt_interaction(config, "L5b", "ignored_next_action", "blocked", f"[blocked: {reason}]", event_id=_l5b_blk_eid or "")
            expired.append(i)
    for i in reversed(expired):
        config._pending_next_actions.pop(i)
    return obs


def _flush_interaction_log(config: GTRuntimeConfig, instance_ref: Any) -> None:
    """Write interaction log to instance for artifact pull."""
    if not config.interaction_log:
        return
    if instance_ref is not None:
        try:
            if isinstance(instance_ref, dict):
                instance_ref["gt_interactions"] = config.interaction_log
            else:
                setattr(instance_ref, "gt_interactions", config.interaction_log)
        except Exception:
            pass


def _is_scaffold_name(filename: str) -> bool:
    """Return True if filename (basename only) matches a known scaffold prefix."""
    base = filename.rsplit("/", 1)[-1]
    return base.startswith(SCAFFOLDING_PREFIXES)


def _strip_scaffold_files(
    orig_run_action: Callable[[Any], Any],
    config: GTRuntimeConfig,
    instance_ref: Any,
) -> None:
    """Delete scaffold files created by the agent. Idempotent via config.scaffold_stripped.

    Only removes untracked files whose basename starts with a SCAFFOLDING_PREFIX
    (reproduce_*, debug_*, temp_*, etc.).  Files the agent may have created as
    part of a legitimate fix (new source modules, changelog entries, test data)
    are preserved — 19.3% of SWE-bench-Live Lite gold patches add new files.
    """
    if config.scaffold_stripped:
        return
    config.scaffold_stripped = True
    base_commit = ""
    if instance_ref is not None:
        if isinstance(instance_ref, dict):
            base_commit = instance_ref.get("base_commit", "")
        else:
            base_commit = getattr(instance_ref, "base_commit", "")
    if not base_commit:
        return
    ls_base = _run_internal(orig_run_action, f"git ls-tree -r --name-only {base_commit}", 30)
    base_files = {f.strip() for f in ls_base.splitlines() if f.strip()}
    ls_new = _run_internal(orig_run_action, "git ls-files --others --exclude-standard", 30)
    new_files = [f.strip() for f in ls_new.splitlines() if f.strip()]
    to_strip = [f for f in new_files if f not in base_files and _is_scaffold_name(f)]
    kept = [f for f in new_files if f not in base_files and not _is_scaffold_name(f)]
    if to_strip:
        print(f"GT_ENFORCE: Stripping {len(to_strip)} scaffold files.", flush=True)
        for f in sorted(to_strip):
            _run_internal(orig_run_action, f"rm -f {_sh_single_quote(f)}", 10)
    if kept:
        print(f"GT_ENFORCE: Kept {len(kept)} new non-scaffold files: {', '.join(sorted(kept)[:5])}", flush=True)

    _hyg_eid = _emit_structured_event(
        config, "HYGIENE", "scaffold_strip",
        emitted=bool(to_strip),
        suppressed=not to_strip,
        suppression_reason="no_scaffold_files" if not to_strip else None,
        evidence_items=[
            {"kind": "hygiene_strip", "file_path": f, "reason": "scaffold file removed"}
            for f in to_strip
        ],
    )
    _log_gt_interaction(
        config, "HYGIENE", "scaffold_strip",
        "strip_ok" if to_strip else "strip_noop",
        f"stripped={len(to_strip)} kept={len(kept)}",
        event_id=_hyg_eid or "",
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
    # Avoid relying on sqlite3 CLI availability inside evaluation containers.
    py = (
        "python3 - "
        + _sh_single_quote(config.graph_db)
        + " 2>&1 <<'PY'\n"
        + "import sqlite3\n"
        + "import sys\n"
        + "db = sys.argv[1]\n"
        + "n = e = 0\n"
        + "try:\n"
        + "    conn = sqlite3.connect(db)\n"
        + "    cur = conn.cursor()\n"
        + "    n = cur.execute('SELECT COUNT(*) FROM nodes').fetchone()[0]\n"
        + "    e = cur.execute('SELECT COUNT(*) FROM edges').fetchone()[0]\n"
        + "    conn.close()\n"
        + "except Exception:\n"
        + "    pass\n"
        + "print(f'{int(n)}|{int(e)}')\n"
        + "PY"
    )
    raw = _run_internal(orig_ra, _env_prefix(config) + py, 30).strip().split("\n")[0]
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

    copy_to_ok = False
    b64_ok = False
    copy_exc_msg = ""
    b64_exc_msg = ""
    if host_index and Path(host_index).exists():
        container_bin = "/tmp/" + Path(host_index).name
        try:
            runtime.copy_to(host_index, "/tmp/")
            copy_to_ok = True
        except Exception as copy_exc:
            copy_exc_msg = str(copy_exc)[:200]
            try:
                _upload_bytes_b64(runtime, Path(host_index).read_bytes(), container_bin)
                b64_ok = True
            except Exception as b64_exc:
                b64_exc_msg = str(b64_exc)[:200]
            else:
                config.gt_index_bin = container_bin
        else:
            config.gt_index_bin = container_bin
        runtime.run_action(_cmd_action(f"chmod +x {config.gt_index_bin}", 30))

    verify_out = _run_internal(orig_ra, f"test -x {config.gt_index_bin} && echo GT_BIN_OK", 10).strip()
    if "GT_BIN_OK" not in verify_out:
        path_bin = _run_internal(orig_ra, "command -v gt-index 2>/dev/null || true", 10).strip().split("\n", 1)[0].strip()
        if path_bin:
            print(f"[GT_META] gt-index uploaded binary unusable, falling back to PATH: {path_bin}", flush=True)
            config.gt_index_bin = path_bin
        else:
            host_exists = Path(host_index).exists() if host_index else False
            host_executable = os.access(host_index, os.X_OK) if host_index and host_exists else False
            container_dir = _run_internal(orig_ra, f"ls -la /tmp/gt-index* 2>&1 || echo 'no gt-index files'", 5).strip()
            print(
                f"[GT_META] L6 BINARY UPLOAD FAILED — diagnostics:\n"
                f"  host_path={host_index}\n"
                f"  host_exists={host_exists}\n"
                f"  host_executable={host_executable}\n"
                f"  container_target={config.gt_index_bin}\n"
                f"  container_listing={container_dir[:300]}\n"
                f"  copy_to_ok={copy_to_ok} exc={copy_exc_msg}\n"
                f"  b64_ok={b64_ok} exc={b64_exc_msg}",
                flush=True,
            )
            config.gt_index_bin = ""

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

    index_cmd = (
        f"{config.gt_index_bin} -root={_sh_single_quote(config.workspace_root)} "
        f"-output={_sh_single_quote(config.graph_db)} 2>&1"
    )
    index_out = _run_internal(orig_ra, index_cmd, 180)

    nc, ec = _verify_graph_nonempty(runtime, config, orig_ra)
    if nc == 0 and ec == 0:
        alt_root = _run_internal(
            orig_ra,
            f"git -C {_sh_single_quote(config.workspace_root)} rev-parse --show-toplevel 2>/dev/null || true",
            20,
        ).strip()
        alt_root = alt_root.split("\n", 1)[0].strip().strip("'\"")
        if alt_root and alt_root != config.workspace_root:
            retry_cmd = (
                f"{config.gt_index_bin} -root={_sh_single_quote(alt_root)} "
                f"-output={_sh_single_quote(config.graph_db)} 2>&1"
            )
            retry_out = _run_internal(orig_ra, retry_cmd, 180)
            nc, ec = _verify_graph_nonempty(runtime, config, orig_ra)
            if nc > 0 or ec > 0:
                print(
                    f"WARNING: gt-index root adjusted from {config.workspace_root} to {alt_root}",
                    flush=True,
                )
                config.workspace_root = alt_root
            elif retry_out.strip():
                print(f"WARNING: gt-index retry output: {retry_out[:400]}", flush=True)
        print(
            "WARNING: graph.db has zero nodes/edges after build "
            f"(root={config.workspace_root}, db={config.graph_db}); "
            + f"index_output={index_out[:400]!r}",
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


def _pull_hook_logs(orig_run_action: Callable[[Any], Any], instance_ref: Any) -> None:
    """Download hook logs from container into instance record."""
    if instance_ref is None:
        return
    for key, path in (
        ("gt_hook_log", os.environ.get("GT_HOOK_LOG", "/tmp/gt_hooks.log")),
        ("gt_hook_log_jsonl", "/tmp/gt_hook_log.jsonl"),
        ("gt_interactions_jsonl", "/tmp/gt_interactions.jsonl"),
    ):
        content = _download_text_from_container(orig_run_action, path)
        if content:
            try:
                instance_ref[key] = content
            except Exception:
                try:
                    setattr(instance_ref, key, content)
                except Exception:
                    pass


GT_PHASE = os.environ.get("GT_PHASE", "full").lower()

def wrap_runtime_run_action(runtime: Any, config: GTRuntimeConfig | None = None) -> Any:
    """Append GT evidence to agent-visible observations for eligible events.

    GT_PHASE env controls which layers are active:
      "B"    — L1 brief only, patched_run_action is pass-through
      "C"    — L1 + pacing ([GT_OK] placeholders, no real hooks)
      "D"    — L1 + pacing + real L3/L3b evidence
      "E"    — L1 + pacing + evidence + L5 redirect
      "full" — all layers (L1 + L3 + L3b + L5 + L6 + scaffold strip)
    """

    config = config or GTRuntimeConfig()
    if getattr(runtime, "_gt_full_wrapped", False):
        return runtime
    orig_run_action = runtime.run_action
    config._task_end_orig_run_action = orig_run_action

    def patched_run_action(action: Any) -> Any:
        # Phase B: brief-only, no run_action hooks at all
        if GT_PHASE == "b":
            return orig_run_action(action)

        act_text = _action_text(action)
        act_cls = _action_class(action)
        tel_obj = getattr(config, "telemetry", None)
        l4_recorded = False

        # Online tracker: check pending next_actions against this real agent action
        _action_file = ""
        if hasattr(action, "path"):
            _action_file = str(action.path or "")
        obs_placeholder = getattr(action, "_obs_ref", None)  # will be set after orig_run_action
        # We check pending BEFORE GT logic runs — this is the agent's real action
        # Note: obs is not available yet here; we'll check again after obs is obtained

        # Backfill agent_action_after on previous log entry for every action,
        # not just GT-triggered ones.  This creates a complete GT->agent->response
        # chain even when the next action doesn't trigger GT.
        if config.interaction_log:
            prev = config.interaction_log[-1]
            if not prev.get("agent_action_after"):
                summary = f"{act_cls}:{act_text[:200]}"
                prev["agent_action_after"] = summary

        if tel_obj is not None and _action_class(action) == "CmdRunAction":
            if re.search(r"\bgt_(query|search|navigate|validate)\b", act_text):
                tel_obj.record_l4()
                l4_recorded = True

        try:
            obs = orig_run_action(action)
        except AttributeError as ae:
            if "TaskTracking" in _action_class(action) or "'str' object" in str(ae):
                from openhands.events.observation import NullObservation  # type: ignore[import]
                obs = NullObservation("Task tracking skipped (format mismatch)")
                print(f"[GT_META] TaskTrackingAction crash intercepted: {ae}", flush=True)
            else:
                raise

        # Online tracker: check pending next_actions after obs is available
        obs = _check_pending_next_actions(config, current_action_file=_action_file, obs=obs)
        event = classify_tool_event(action, source_exts=config.source_exts)
        instance_ref = getattr(runtime, "_gt_instance", None)

        if _action_class(action) == "CmdRunAction":
            config.action_count += 1
            # Bug fix: removed global /tmp/gt_abort_reasoning.flag check — it
            # leaked across workers.  Reasoning detection telemetry still writes
            # the flag in cost_tracking._cost_callback for post-run analysis.
            # Strip scaffold on first post-loop action (complete_runtime)
            # in case finish event never fired (max_iter timeout)
            if config.action_count > config.max_iter and not config.scaffold_stripped:
                _strip_scaffold_files(orig_run_action, config, instance_ref)
                _flush_interaction_log(config, instance_ref)
                _flush_task_end_metrics(config, "max_iter")
            if "gt_validate" in act_text:
                register_gt_validate_paths(act_text, config)

            # L5 governor: detect test commands (edits handled below after event classification)
            _l5_gov = getattr(config, "_l5_governor", None)
            if _l5_gov is not None and not _GT_BASELINE and event.kind == "skip":
                try:
                    _l5d = _l5_gov.after_interaction(
                        action, obs, config.action_count, config.max_iter,
                        edited_files=config.edited_files,
                        brief_candidates=config.brief_candidates,
                        viewed_files=config.viewed_files,
                        graph_db=config.graph_db,
                        workspace_root=config.workspace_root,
                    )
                    if _l5d.fired:
                        _l5_eid = _emit_structured_event(
                            config, "L5", _l5d.hook_name,
                            evidence_items=_l5d.evidence_items,
                            verification_kind=_l5d.verification_kind,
                        )
                        if _l5d.message:
                            _l5b_eid = _emit_structured_event(
                                config, "L5b", f"intervention_{_l5d.hook_name}",
                                parent_event_id=_l5_eid,
                                rendered_text=_l5d.message,
                                next_action_type=_l5d.next_action_type,
                                next_action_file=_l5d.next_action_file,
                                next_action_test=_l5d.next_action_test,
                            )
                            obs = append_observation(obs, f"\n\n{_l5d.message}\n")
                            _log_gt_interaction(
                                config, "L5", "governor_cmd", "advisory", _l5d.message,
                                agent_action_before=act_text[:300],
                                event_id=_l5b_eid or "",
                                parent_event_id=_l5_eid or "",
                                next_action_type=_l5d.next_action_type or "",
                                next_action_file=_l5d.next_action_file or "",
                                next_action_test=_l5d.next_action_test or "",
                            )
                            _register_pending_next_action(config, _l5b_eid or "", _l5d.next_action_type or "", _l5d.next_action_file or "")
                        elif _l5d.suppressed:
                            _l5b_blk = _emit_structured_event(
                                config, "L5b", "blocked_by_safety",
                                parent_event_id=_l5_eid,
                                suppressed=True,
                                suppression_reason=_l5d.suppression_reason,
                            )
                            _log_gt_interaction(config, "L5b", "governor_cmd", "blocked", f"[blocked: {_l5d.suppression_reason}]", event_id=_l5b_blk or "")
                except Exception as l5_exc:
                    print(f"[GT_META] L5 governor error on CmdRunAction: {l5_exc}", flush=True)

        if event.kind != "finish":
            config.last_visible_observation = obs
        if l4_recorded and tel_obj is not None:
            _write_gt_telemetry(instance_ref, tel_obj)

        def _hook_fatal(blob: str) -> bool:
            return "[GT_STATUS] error" in blob

        if event.kind == "post_view":
            rel_view = _normalize_rel_path(event.path, config)
            if rel_view:
                config.viewed_files.add(rel_view)
            # Write trajectory files for L3b (post_view hook reads these)
            if config.viewed_files:
                _write_text_to_container(
                    orig_run_action,
                    "\n".join(sorted(config.viewed_files)) + "\n",
                    "/tmp/gt_viewed.txt",
                )
            # Pre-gen recovery: do NOT write brief_candidates to container.
            # G6 gate in post_edit.py reads this file — if it exists, G6 fires.
            # Pre-gen baseline had this file NEVER written (Decision 29 line 702).
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
                _write_gt_telemetry(instance_ref, tel_obj)
            hook_body = hook_out.strip()
            has_evidence = any(t in hook_body for t in (
                "[GT_CHANGE]", "[GT_CONTRACT]", "[GT_PATTERN]", "[GT_STRUCTURAL]", "[GT_SEMANTIC]", "[GT_COUPLING]",
                "Called by:", "Calls into:", "Imported by:", "[GT_STATUS] success",
            ))
            if not has_evidence:
                _l3b_ok_eid = _emit_structured_event(config, "L3b", "navigation_suppressed", emitted=False, suppressed=True, suppression_reason="no_evidence", file_path=rel_view or event.path)
                _log_gt_interaction(config, "L3b", f"post_view:{rel_view or event.path}", "GT_OK", "[GT_OK] No concerns.", agent_action_before=act_text[:300], event_id=_l3b_ok_eid or "")
                return obs

            ev_hash = hashlib.md5(hook_body.encode("utf-8", errors="replace")).hexdigest()[:12]
            prev_hash = config.evidence_sent.get(f"view:{rel_view or event.path}")
            if prev_hash == ev_hash and hook_body:
                _l3b_dd_eid = _emit_structured_event(config, "L3b", "navigation_dedup", emitted=False, suppressed=True, suppression_reason="duplicate", file_path=rel_view or event.path)
                _log_gt_interaction(config, "L3b", f"post_view:{rel_view or event.path}", "dedup", "[dedup]", agent_action_before=act_text[:300], event_id=_l3b_dd_eid or "")
                return append_observation(obs, f'\n\n<gt-evidence trigger="post_view:{event.path}" dedup="true" />\n')
            config.evidence_sent[f"view:{rel_view or event.path}"] = ev_hash
            suggestion = ""
            if "[GT_STATUS] no_evidence:" in hook_out:
                stem = Path(rel_view or event.path).stem or "symbol"
                suggestion = f"\nNo coupling data. Try: gt_search function {stem}"
            evidence = (
                f'\n\n<gt-evidence trigger="post_view:{event.path}">\n'
                f"{hook_body}{suggestion}\n</gt-evidence>\n"
            )
            print(f"[GT_META] L3b post_view evidence for {rel_view or event.path} ({len(hook_body)} chars)", flush=True)
            # Extract primary-edge next_action from structured data
            _l3b_nat = ""
            _l3b_naf = ""
            if os.environ.get("GT_L3B_PRIMARY_EDGE", "0") == "1" and hook_out and "__GT_STRUCTURED__" in hook_out:
                try:
                    _sp = hook_out.split("__GT_STRUCTURED__", 1)[1].strip().splitlines()[0]
                    _si_list = json.loads(_sp)
                    for _si in _si_list:
                        if _si.get("primary_edge") and _si.get("file_path"):
                            _l3b_nat = "READ_CALLER_CONTRACT" if _si.get("kind") == "l3b_caller_edge" else "READ_CONSUMER"
                            _l3b_naf = _si["file_path"]
                            break
                except Exception:
                    pass
            _l3b_eid = _emit_structured_event(
                config, "L3b", "navigation",
                rendered_text=hook_body,
                file_path=rel_view or event.path,
                hook_output=hook_out or "",
                next_action_type=_l3b_nat or None,
                next_action_file=_l3b_naf or None,
            )
            _log_gt_interaction(
                config, "L3b", f"post_view:{rel_view or event.path}", "evidence",
                hook_body, agent_action_before=act_text[:300],
                event_id=_l3b_eid or "",
                next_action_type=_l3b_nat,
                next_action_file=_l3b_naf,
            )
            _register_pending_next_action(config, _l3b_eid or "", _l3b_nat, _l3b_naf)
            return append_observation(obs, evidence)

        if event.kind == "post_edit":
            # --- Phase 1: Record edit state BEFORE any L5 checks (Decision 30, Bug 5 fix) ---
            rel_p = _normalize_rel_path(event.path, config)
            if rel_p:
                config.edited_files.add(rel_p)
                _emit_belief_event(config, rel_p, "unverified", "agent edited source file")
            _record_edit_iter(config, config.action_count, event.path)
            _record_diff_snapshot(orig_run_action, config, event.path, config.action_count)

            # Track per-file edit counts for edit loop detection
            edit_key = rel_p or event.path
            config._l5_edit_counts_per_file[edit_key] = config._l5_edit_counts_per_file.get(edit_key, 0) + 1

            # L5 governor: notify about source edit so it can track for Hypothesis Falsified
            _l5_gov = getattr(config, "_l5_governor", None)
            if _l5_gov is not None and not _GT_BASELINE:
                try:
                    _l5_gov.state.update_iter(config.action_count, config.max_iter)
                    if _is_real_source_edit(event.path, config):
                        _l5_gov.state.record_source_edit(rel_p or event.path)
                        print(f"[GT_META] L5 governor: tracked source edit {rel_p or event.path}", flush=True)
                    _l5_gov.state.save()
                except Exception as l5_exc:
                    print(f"[GT_META] L5 governor edit tracking error: {l5_exc}", flush=True)

            # Old L5 triggers (33/66 checkpoints, non-source, diff-collapsed, edit-loop) REMOVED.
            # All L5 logic now goes through the governor (Decision 31).

            # Pre-gen recovery: do NOT write brief_candidates to container (Decision 29 line 702).
            if config.edited_files:
                _write_text_to_container(
                    orig_run_action,
                    "\n".join(sorted(config.edited_files)) + "\n",
                    "/tmp/gt_edited_files.txt",
                )

            # --- Phase 3: Scaffolding early-exit (reindex + skip L3) ---
            if _is_scaffolding_path(event.path):
                reindex_cmd = make_reindex_command(event.path, config)
                reindex_out = _run_internal(orig_run_action, reindex_cmd, 120)
                if tel_obj is not None:
                    r_ok = bool(reindex_out.strip()) and not any(
                        w in reindex_out.lower() for w in ("panic", "fatal")
                    )
                    tel_obj.record_reindex(r_ok)
                    tel_obj.record_hook("L3", ok=False, empty=True)
                    _write_gt_telemetry(instance_ref, tel_obj)
                if config._l5_scaffold_fired:
                    config._l5_metrics["num_additional_scaffolds_after_l5"] += 1

                evidence = (
                    f'\n\n<gt-evidence trigger="post_edit:{event.path}">\n'
                    "[GT_STATUS] skipped:scaffolding_file\n"
                    "</gt-evidence>\n"
                )
                return append_observation(obs, evidence)

            # --- Phase 4: L6 reindex BEFORE L3 post_edit hook (sequential ordering is load-bearing) ---
            reindex_cmd = make_reindex_command(event.path, config)
            if not reindex_cmd:
                if tel_obj is not None:
                    tel_obj.record_reindex(False)
                print(f"[GT_META] L6 reindex SKIPPED (binary unavailable) for {event.path}", flush=True)
                _l6_skip_eid = _emit_structured_event(config, "L6", "reindex_skip", emitted=False, suppressed=True, suppression_reason="binary_unavailable", file_path=event.path)
                _log_gt_interaction(config, "L6", f"reindex:{event.path}", "reindex_skip", "binary unavailable", agent_action_before=act_text[:300], event_id=_l6_skip_eid or "")
            else:
                mtime_before_raw = _run_internal(
                    orig_run_action,
                    f"stat -c %Y {config.graph_db} 2>/dev/null || echo 0",
                    5,
                ).strip()
                mtime_before = int(mtime_before_raw or "0")
                _reindex_start = time.time()

                reindex_out = _run_internal(orig_run_action, reindex_cmd + "; echo __EXIT__$?", 120)

                exit_code = 1
                if "__EXIT__" in reindex_out:
                    parts = reindex_out.rsplit("__EXIT__", 1)
                    reindex_out = parts[0]
                    try:
                        exit_code = int(parts[1].strip())
                    except ValueError:
                        exit_code = 1

                mtime_after_raw = _run_internal(
                    orig_run_action,
                    f"stat -c %Y {config.graph_db} 2>/dev/null || echo 0",
                    5,
                ).strip()
                mtime_after = int(mtime_after_raw or "0")

                r_ok = (exit_code == 0) and (mtime_after > mtime_before)

                if r_ok and any(w in reindex_out.lower() for w in (
                    "panic", "fatal", "no such file", "not found", "cannot execute", "permission denied"
                )):
                    r_ok = False

                if tel_obj is not None:
                    tel_obj.record_reindex(r_ok)
                print(f"[GT_META] L6 reindex {'OK' if r_ok else 'FAIL'} for {event.path} (exit={exit_code}, mtime_delta={mtime_after - mtime_before})", flush=True)
                _reindex_latency = int((time.time() - _reindex_start) * 1000)
                _l6_eid = _emit_structured_event(
                    config, "L6", "reindex",
                    emitted=True, suppressed=False,
                    file_path=event.path,
                    evidence_items=[{
                        "kind": "l6_reindex",
                        "file_path": event.path,
                        "reason": f"reindex {'success' if r_ok else 'failed'}: exit={exit_code}",
                        "text": f"latency_ms={_reindex_latency} mtime_delta={mtime_after - mtime_before}",
                    }],
                )
                _log_gt_interaction(
                    config, "L6", f"reindex:{event.path}",
                    "reindex_ok" if r_ok else "reindex_fail",
                    reindex_out[:200], agent_action_before=act_text[:300],
                    event_id=_l6_eid or "",
                )

            # --- Phase 5: L3 post_edit hook ---
            diff_text, old_content_text = _extract_diff_and_old_content(obs)
            diff_path = ""
            old_content_path = ""
            if diff_text:
                diff_path = "/tmp/gt_diff.txt"
                _write_text_to_container(orig_run_action, diff_text, diff_path)
            if old_content_text:
                old_content_path = "/tmp/gt_old.txt"
                _write_text_to_container(orig_run_action, old_content_text, old_content_path)
            # Compute L3 mode from L5 governor state (Change 5a)
            _l3_mode = "post_edit"
            _l3_ratio = config.action_count / max(config.max_iter, 1)
            if os.environ.get("GT_REBUILD_L3", "0") == "1":
                _l5_gov = getattr(config, "_l5_governor", None)
                if _l5_gov is not None and _l5_gov.state.has_unresolved_failure():
                    _l3_mode = "post_failure"

            hook_out = _run_internal(
                orig_run_action,
                make_edit_hook_command_with_artifacts(
                    event,
                    config,
                    diff_path=diff_path or None,
                    old_content_path=old_content_path or None,
                    mode=_l3_mode,
                    iteration_ratio=_l3_ratio,
                ),
                45,
            )
            if _hook_fatal(hook_out):
                print(f"GT HOOK ERROR (post_edit:{event.path}): {hook_out[:400]}", flush=True)

            # L5 metrics: track post-advisory source edits
            if config._l5_scaffold_fired and _is_real_source_edit(event.path, config):
                config._l5_metrics["source_edit_after_l5"] = True
                if rel_p and rel_p in config.brief_candidates:
                    config._l5_metrics["touched_brief_candidate_after_l5"] = True

            # L3 fully decoupled from L1 (Decision 22 Fix 5) — no candidate labeling
            framing = ""

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
                tel_obj.record_hook("L3", ok_ev and not fatal, empty=empty_ev or (not hook_out.strip()))
                _write_gt_telemetry(instance_ref, tel_obj)

            hook_body_edit = hook_out.strip()
            has_evidence = any(t in hook_body_edit for t in (
                "[GT_CHANGE]", "[GT_CONTRACT]", "[GT_PATTERN]", "[GT_STRUCTURAL]", "[GT_SEMANTIC]", "[GT_COUPLING]",
                "SIGNATURE:", "SIBLING:", "CALLERS:", "[GT_STATUS] success",
            ))
            if not has_evidence:
                _l3_ok_eid = _emit_structured_event(config, "L3", "post_edit_suppressed", emitted=False, suppressed=True, suppression_reason="no_evidence", file_path=rel_p or event.path)
                _log_gt_interaction(config, "L3", f"post_edit:{rel_p or event.path}", "GT_OK", "[GT_OK] No concerns.", agent_action_before=act_text[:300], event_id=_l3_ok_eid or "")
                return obs

            edit_ev_hash = hashlib.md5(hook_body_edit.encode("utf-8", errors="replace")).hexdigest()[:12]
            prev_edit_hash = config.evidence_sent.get(f"edit:{rel_p or event.path}")
            if prev_edit_hash == edit_ev_hash and hook_body_edit:
                _l3_dd_eid = _emit_structured_event(config, "L3", "post_edit_dedup", emitted=False, suppressed=True, suppression_reason="duplicate", file_path=rel_p or event.path)
                _log_gt_interaction(config, "L3", f"post_edit:{rel_p or event.path}", "dedup", "[dedup]", agent_action_before=act_text[:300], event_id=_l3_dd_eid or "")
                evidence = (
                    f'\n\n<gt-evidence trigger="post_edit:{event.path}" dedup="true">\n'
                    "</gt-evidence>\n"
                )
            else:
                config.evidence_sent[f"edit:{rel_p or event.path}"] = edit_ev_hash
                print(f"[GT_META] L3 post_edit evidence for {rel_p or event.path} ({len(hook_body_edit)} chars)", flush=True)
                # Structural next_action hierarchy (Decision 32)
                _l3_next_action_type = ""
                _l3_next_action_file = ""
                _l3_next_action_test = ""
                if os.environ.get("GT_STRUCTURAL_NEXT_ACTION", "0") == "1" and hook_out and "__GT_STRUCTURED__" in hook_out:
                    try:
                        _struct_part = hook_out.split("__GT_STRUCTURED__", 1)[1].strip().splitlines()[0]
                        _struct_items = json.loads(_struct_part)
                        # Priority 1: caller code
                        for _si in _struct_items:
                            if _si.get("kind") == "l3_caller_code" and _si.get("file_path"):
                                _l3_next_action_type = "READ_CALLER_CONTRACT"
                                _l3_next_action_file = _si["file_path"]
                                break
                        # Priority 2: consumer/importer
                        if not _l3_next_action_type:
                            for _si in _struct_items:
                                if _si.get("kind") in ("l3b_importer_edge", "l3b_callee_edge") and _si.get("file_path"):
                                    _l3_next_action_type = "READ_CONSUMER"
                                    _l3_next_action_file = _si["file_path"]
                                    break
                        # Priority 3: signature (no caller/consumer)
                        if not _l3_next_action_type:
                            for _si in _struct_items:
                                if _si.get("kind") == "l3_signature":
                                    _l3_next_action_type = "CHECK_SIGNATURE"
                                    break
                        # Priority 4: targeted test (no structural witness)
                        if not _l3_next_action_type:
                            for _si in _struct_items:
                                if _si.get("kind") == "l3_targeted_verification":
                                    _l3_next_action_type = "RUN_TARGETED_TEST"
                                    _l3_next_action_test = _si.get("text", "")
                                    break
                        # Priority 5/6: static sanity or none
                        if not _l3_next_action_type:
                            _l3_next_action_type = "NONE_UNVERIFIABLE"
                    except Exception:
                        pass
                _l3_eid = _emit_structured_event(
                    config, "L3", "post_edit_contract",
                    rendered_text=framing + hook_body_edit,
                    file_path=rel_p or event.path,
                    hook_output=hook_out or "",
                    next_action_type=_l3_next_action_type or None,
                    next_action_file=_l3_next_action_file or None,
                    next_action_test=_l3_next_action_test or None,
                )
                _log_gt_interaction(
                    config, "L3", f"post_edit:{rel_p or event.path}", "evidence",
                    framing + hook_body_edit, agent_action_before=act_text[:300],
                    event_id=_l3_eid or "",
                    next_action_type=_l3_next_action_type,
                    next_action_file=_l3_next_action_file,
                    next_action_test=_l3_next_action_test,
                )
                _register_pending_next_action(config, _l3_eid or "", _l3_next_action_type, _l3_next_action_file)
                evidence = (
                    f'\n\n<gt-evidence trigger="post_edit:{event.path}">\n'
                    f"{framing}"
                    f"{hook_body_edit}\n"
                    + "</gt-evidence>\n"
                )
            return append_observation(obs, evidence)

        if event.kind == "finish":
            # L5 governor: unsafe finish check
            _l5_gov = getattr(config, "_l5_governor", None)
            if _l5_gov is not None and not _GT_BASELINE:
                try:
                    _l5d = _l5_gov.after_interaction(
                        action, obs, config.action_count, config.max_iter,
                        edited_files=config.edited_files,
                    )
                    if _l5d.fired:
                        _l5_eid = _emit_structured_event(config, "L5", _l5d.hook_name)
                        if _l5d.message:
                            _l5b_eid = _emit_structured_event(
                                config, "L5b", f"intervention_{_l5d.hook_name}",
                                parent_event_id=_l5_eid,
                                rendered_text=_l5d.message,
                                next_action_type=_l5d.next_action_type,
                                next_action_file=_l5d.next_action_file,
                                next_action_test=_l5d.next_action_test,
                            )
                            obs = append_observation(obs, f"\n\n{_l5d.message}\n")
                            _log_gt_interaction(
                                config, "L5", "governor_finish", "advisory", _l5d.message,
                                agent_action_before=act_text[:300],
                                event_id=_l5b_eid or "",
                                parent_event_id=_l5_eid or "",
                                next_action_type=_l5d.next_action_type or "",
                            )
                            _register_pending_next_action(config, _l5b_eid or "", _l5d.next_action_type or "", _l5d.next_action_file or "")
                        elif _l5d.suppressed:
                            _l5b_blk = _emit_structured_event(
                                config, "L5b", "blocked_by_safety",
                                parent_event_id=_l5_eid, suppressed=True,
                                suppression_reason=_l5d.suppression_reason,
                            )
                            _log_gt_interaction(config, "L5b", "governor_finish", "blocked", f"[blocked: {_l5d.suppression_reason}]", event_id=_l5b_blk or "")
                except Exception as l5_exc:
                    print(f"[GT_META] L5 governor error on finish: {l5_exc}", flush=True)

            # Kill any stuck bash process so complete_runtime can cd into the workspace.
            try:
                orig_run_action(_cmd_action("kill %1 2>/dev/null; wait 2>/dev/null; true", timeout=5))
            except Exception:
                pass
            _strip_scaffold_files(orig_run_action, config, instance_ref)
            _flush_interaction_log(config, instance_ref)

            advisory = render_l5_advisory(config)
            unresolved = _l5_unresolved_paths(config)
            # Fix 6: Keep advisory for state/telemetry but remove agent-visible injection

            instance_ref = getattr(runtime, "_gt_instance", None)
            if advisory and instance_ref is not None:
                try:
                    instance_ref["gt_advisory"] = advisory
                except Exception:
                    try:
                        setattr(instance_ref, "gt_advisory", advisory)
                    except Exception:
                        pass
            _pull_hook_logs(orig_run_action, instance_ref)

            tel_fin = getattr(config, "telemetry", None)
            if tel_fin is not None:
                tel_fin.record_gate(bool(unresolved))
                _write_gt_telemetry(instance_ref, tel_fin)
                fin = tel_fin.finalize()
                print(f"[GT_META] === TASK COMPLETE: {tel_fin.task_id} ===", flush=True)
                print(f"[GT_META] Layer hits: {json.dumps(fin.get('layer_hits', {}))}", flush=True)
                print(f"[GT_META] Utilization: {json.dumps(fin.get('utilization', {}))}", flush=True)
                print(f"[GT_META] Overall: {fin.get('overall_utilization', 0)}", flush=True)
                print(f"[GT_META] Actions: {config.action_count}, Edits: {len(config.edited_files)}, Views: {len(config.viewed_files)}", flush=True)
                print(f"[GT_META] L5 metrics: {json.dumps(config._l5_metrics)}", flush=True)
            _flush_task_end_metrics(config, "finish")

        return obs

    runtime.run_action = patched_run_action
    runtime._gt_full_wrapped = True
    runtime._gt_full_config = config
    return runtime


_ORIG_INITIALIZE_RUNTIME = None
_ORIG_GET_INSTRUCTION = None

L4_PREFETCH_MAX_QUERIES = 3
L4_PREFETCH_MAX_LINES_PER_QUERY = 4
L4_PREFETCH_MAX_CHARS = 1200
L4_PREFETCH_WALL_TIMEOUT = 30
L4_NOISE_PATTERNS = ("body spans", "sibling:")

_FILE_PATH_RE = re.compile(r"(\S+\.(?:py|go|js|ts|rs|java|rb|php))\b")


def _extract_candidate_files(brief: str) -> list[str]:
    """Extract file paths from the brief (e.g. 'loguru/_logger.py')."""
    files: list[str] = []
    seen: set[str] = set()
    for m in _FILE_PATH_RE.finditer(brief):
        fp = m.group(1)
        if fp not in seen:
            seen.add(fp)
            files.append(fp)
    return files


def _select_issue_seeded_symbols(
    orig_run_action: Callable[[Any], Any],
    config: GTRuntimeConfig,
    issue_text: str,
    candidate_files: list[str],
    max_symbols: int = 3,
) -> list[str]:
    """Issue-text-seeded symbol selection (SweRank §3.2 deterministic pattern).

    1. Extract potential identifiers from issue text
    2. Deterministically filter tokens against graph.db (Agnostic Soft-Filter)
    3. Rerank based on node degree (centrality) within candidate files
    """
    if not candidate_files:
        return []

    # Extract all likely tokens (3+ chars)
    issue_idents: list[str] = []
    seen_toks: set[str] = set()
    for m in re.finditer(r"\b([A-Za-z_][A-Za-z0-9_]{2,})\b", issue_text or ""):
        tok = m.group(1)
        if tok not in seen_toks:
            issue_idents.append(tok)
            seen_toks.add(tok)

    if not issue_idents:
        return []

    # Batch check tokens against the graph nodes - this is the "Soft-Filter"
    # A token is only kept if it actually exists as a named entity in this repo's graph.
    file_likes = " OR ".join(
        f"n.file_path LIKE '%{f.replace(chr(39), '')}'" for f in candidate_files[:5]
    )
    # We limit to first 100 tokens to keep the SQL query size reasonable
    issue_names_sql = ",".join(f"'{n.replace(chr(39), '')}'" for n in issue_idents[:100])

    py_script = (
        "import sqlite3, sys\n"
        f"c = sqlite3.connect('{config.graph_db}')\n"
        "results = []\n"
    )
    if issue_names_sql:
        # SweRank-style: intersection of issue text tokens and graph nodes
        py_script += (
            f"issue_matched = c.execute(\n"
            f"    \"SELECT DISTINCT n.name FROM nodes n \"\n"
            f"    \"WHERE n.name IN ({issue_names_sql}) \"\n"
            f"    \"AND ({file_likes}) \"\n"
            f"    \"AND n.label IN ('Function','Method','Class') \"\n"
            f"    \"LIMIT {max_symbols}\"\n"
            f").fetchall()\n"
            "results = [r[0] for r in issue_matched]\n"
        )
    py_script += (
        f"if len(results) < {max_symbols}:\n"
        f"    fallback = c.execute(\n"
        f"        \"SELECT n.name, COUNT(e.id) AS rc \"\n"
        f"        \"FROM nodes n LEFT JOIN edges e ON e.target_id = n.id \"\n"
        f"        \"WHERE ({file_likes}) \"\n"
        f"        \"AND n.label IN ('Function','Method','Class') \"\n"
        f"        \"AND n.is_exported = 1 \"\n"
        f"        \"AND substr(n.name, 1, 1) != '_' \"\n"
        f"        \"AND n.name NOT IN ('__init__','setup','main','run','test') \"\n"
        f"        \"GROUP BY n.name ORDER BY rc DESC LIMIT {max_symbols}\"\n"
        f"    ).fetchall()\n"
        f"    for r in fallback:\n"
        f"        if r[0] not in results:\n"
        f"            results.append(r[0])\n"
        f"        if len(results) >= {max_symbols}:\n"
        f"            break\n"
        "for name in results:\n"
        "    print(name)\n"
        "c.close()\n"
    )
    script_path = "/tmp/gt_symbol_query.py"
    payload = py_script.encode("utf-8")
    b64 = base64.b64encode(payload).decode("ascii")
    _run_internal(
        orig_run_action,
        f"echo -n '{b64}' | base64 -d > {script_path}",
        10,
    )
    raw = _run_internal(
        orig_run_action,
        _env_prefix(config) + f"python3 {script_path} 2>/dev/null",
        10,
    ).strip()
    symbols = [ln.strip() for ln in raw.splitlines() if ln.strip() and not ln.startswith("Traceback")]
    return symbols[:max_symbols]


def _run_l4_prefetch(
    orig_run_action: Callable[[Any], Any],
    config: GTRuntimeConfig,
    brief: str,
    issue_text: str,
    tel: Any,
) -> str:
    """Pre-fetch gt_query evidence for issue-relevant symbols. Returns formatted block."""
    candidate_files = _extract_candidate_files(brief)
    symbols = _select_issue_seeded_symbols(
        orig_run_action, config, issue_text, candidate_files, L4_PREFETCH_MAX_QUERIES,
    )
    if not symbols:
        if tel is not None:
            tel.record_l4_prefetch(0, 0)
        return ""

    import time as _time
    t0 = _time.monotonic()
    blocks: list[str] = []
    queries_run = 0
    total_lines = 0

    for sym in symbols:
        elapsed = _time.monotonic() - t0
        if elapsed >= L4_PREFETCH_WALL_TIMEOUT:
            print(f"L4_PREFETCH: wall timeout after {queries_run} queries ({elapsed:.1f}s)", flush=True)
            break
        cmd = (
            _env_prefix(config)
            + f"python3 ${{GT_TOOLS_DIR:-/tmp/gt_tools}}/gt_query/lib/gt_query.py {_sh_single_quote(sym)} 2>&1"
        )
        raw = _run_internal(orig_run_action, cmd, 10).strip()
        queries_run += 1
        if not raw or len(raw) < 10:
            continue
        lines = raw.splitlines()
        kept: list[str] = []
        for ln in lines[:L4_PREFETCH_MAX_LINES_PER_QUERY * 2]:
            if any(noise in ln for noise in L4_NOISE_PATTERNS):
                continue
            if "[VERIFIED]" in ln or "[POSSIBLE]" in ln or ln.startswith("# gt_query:"):
                kept.append(ln)
            if len(kept) >= L4_PREFETCH_MAX_LINES_PER_QUERY:
                break
        if not kept:
            continue
        total_lines += len(kept)
        blocks.append("\n".join(kept))

    # Git precedent: last commit for top 2 candidate files
    for cfile in candidate_files[:2]:
        elapsed = _time.monotonic() - t0
        if elapsed >= L4_PREFETCH_WALL_TIMEOUT:
            break
        git_cmd = (
            f"cd {_sh_single_quote(config.workspace_root)} && "
            f"git log --oneline -1 --follow -- {_sh_single_quote(cfile)} 2>/dev/null"
        )
        git_out = _run_internal(orig_run_action, git_cmd, 5).strip()
        if git_out and len(git_out) > 5:
            blocks.append(f"# {cfile}: last commit: {git_out[:80]}")
            total_lines += 1

    wall_ms = int((_time.monotonic() - t0) * 1000)
    print(
        f"L4_PREFETCH: queries={queries_run} symbols={symbols} "
        f"lines={total_lines} wall_ms={wall_ms}",
        flush=True,
    )

    if tel is not None:
        tel.record_l4_prefetch(queries_run, total_lines)

    if not blocks:
        return ""

    evidence = "\n".join(blocks)
    if len(evidence) > L4_PREFETCH_MAX_CHARS:
        evidence = evidence[:L4_PREFETCH_MAX_CHARS] + "\n[L4_PREFETCH_TRUNCATED]"

    return (
        f"\n<gt-prefetch layer=\"L4\" queries=\"{queries_run}\" "
        f"symbols=\"{','.join(symbols)}\" wall_ms=\"{wall_ms}\">\n"
        + evidence
        + "\n</gt-prefetch>"
    )


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
    _max_iter = int(os.environ.get("GT_MAX_ITER", str(getattr(metadata, "max_iterations", 100))))
    config = GTRuntimeConfig(
        workspace_root=workspace_root,
        telemetry=tel,
        max_iter=_max_iter,
        instance_ref=instance,
    )
    config._meta_instance_id = workspace_name or "unknown"

    # L5 trajectory governor (Decision 30 + test-failure hooks)
    try:
        from groundtruth.trajectory.governor import L5Governor
        config._l5_governor = L5Governor(
            instance_id=workspace_name or "unknown",
            max_iter=_max_iter,
        )
        print(f"[GT_META] L5 governor initialized for {workspace_name}", flush=True)
    except Exception as exc:
        config._l5_governor = None  # type: ignore[attr-defined]
        print(f"[GT_META] L5 governor init failed: {exc}", flush=True)

    # Initialize structured telemetry writer (GT_STRUCTURED_EVENTS=1)
    if os.environ.get("GT_STRUCTURED_EVENTS", "0") == "1":
        try:
            from groundtruth.telemetry.writer import GTTelemetryWriter
            _run_id = os.environ.get("GITHUB_RUN_ID", "local")
            config._telemetry_writer = GTTelemetryWriter(
                run_id=_run_id,
                task_id=workspace_name or "unknown",
                output_dir=os.environ.get("GT_DEBUG_DIR", "/tmp"),
            )
            print(f"[GT_META] Telemetry writer initialized for {workspace_name}", flush=True)
        except Exception as exc:
            config._telemetry_writer = None
            print(f"[GT_META] Telemetry writer init failed: {exc}", flush=True)

    runtime._gt_instance = instance
    try:
        if isinstance(instance, dict):
            instance["_gt_runtime"] = runtime
        else:
            setattr(instance, "_gt_runtime", runtime)
    except Exception:
        pass

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
    _reset_iter_state(config, task_id)
    print(f"[GT_META] EVAL_CONDENSER={os.environ.get('EVAL_CONDENSER', 'NOT SET')}", flush=True)
    try:
        _record_diff_snapshot(runtime.run_action, config, "[task_start]", 0)
    except Exception:
        pass

    brief_runner = (
        "import json, sys\n"
        "import inspect\n"
        "meta_path, issue_path = sys.argv[1], sys.argv[2]\n"
        'with open(meta_path, encoding="utf-8") as f:\n'
        "    meta = json.load(f)\n"
        'with open(issue_path, encoding="utf-8", errors="replace") as f:\n'
        "    issue = f.read()\n"
        "try:\n"
        "    from groundtruth.pretask.v1r_brief import generate_v1r_brief\n"
        "    import os, sqlite3 as _sq\n"
        "    _db = meta['graph_db']\n"
        "    _rr = meta['repo_root']\n"
        "    _db_exists = os.path.exists(_db)\n"
        "    _nc = 0\n"
        "    if _db_exists:\n"
        "        _nc = _sq.connect(_db).execute('SELECT COUNT(DISTINCT file_path) FROM nodes WHERE is_test=0').fetchone()[0]\n"
        "    _sample = ''\n"
        "    if _db_exists:\n"
        "        _sample = str(_sq.connect(_db).execute('SELECT file_path FROM nodes LIMIT 3').fetchall())\n"
        "    _rr_exists = os.path.isdir(_rr)\n"
        "    _rr_list = str(os.listdir(_rr)[:5]) if _rr_exists else 'DIR_MISSING'\n"
        '    print(f"[GT_BRIEF_DIAG] db={_db} exists={_db_exists} files={_nc} sample={_sample}")\n'
        '    print(f"[GT_BRIEF_DIAG] repo_root={_rr} exists={_rr_exists} ls={_rr_list}")\n'
        "    out = generate_v1r_brief(\n"
        "        issue_text=issue,\n"
        "        repo_root=meta['repo_root'],\n"
        "        graph_db=meta['graph_db'],\n"
        "        bug_id=meta['task_id'],\n"
        "    )\n"
        "    brief = out.brief_text or ''\n"
        '    print(f"[GT_BRIEF_DIAG] brief_len={len(brief)} files={len(out.files)}")\n'
        "    print(brief.strip())\n"
        "    v74 = out.v74_result\n"
        "    m6 = {}\n"
        "    if v74 is not None:\n"
        "        m6 = {'focus_set': v74.focus_set, 'ranked_count': len(v74.ranked_full)}\n"
        '        print(f"[GT_BRIEF_DIAG] ranked_count={len(v74.ranked_full)} focus={v74.focus_set}")\n'
        '    print("\\n---GT_L2_JSON---")\n'
        '    print(json.dumps(m6))\n'
        "except Exception as exc:\n"
        "    import traceback\n"
        '    print(f"[GT_BRIEF_FAILED] {type(exc).__name__}: {exc}")\n'
        '    print(f"[GT_BRIEF_TRACEBACK] {traceback.format_exc()[-500:]}")\n'
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
        issue_terms = sorted(set(
            w.lower() for w in re.findall(r"[A-Za-z_]\w{2,}", issue_text)
            if len(w) > 3
        ))
        _upload_bytes_b64(
            runtime,
            "\n".join(issue_terms).encode("utf-8"),
            "/tmp/gt_issue_terms.txt",
        )
        raw_br = (
            _run_internal(
                runtime.run_action,
                _env_prefix(config)
                + "python3 /tmp/gt_brief_runner.py /tmp/gt_brief_meta.json /tmp/gt_issue.txt 2>/tmp/gt_brief_stderr.log",
                180,
            ).strip()
        )
        segments = raw_br.split("---GT_L2_JSON---")
        brief = "\n".join(
            line for line in segments[0].strip().splitlines()
            if not line.startswith("[GT_BRIEF_DIAG]")
        ).strip()
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

    brief = _rewrite_site_package_paths_in_brief(brief, workspace_name, config.workspace_root)
    brief_full = brief  # Keep full text for L1 logging (NOT truncated)
    brief = _brief_max_tokens(brief)

    if brief.strip():
        config.brief_candidates = set(_extract_candidate_files(brief))

    if not brief.strip():
        brief = ""  # Fix 8: inject nothing if brief generation fails

    prefetch_block = _run_l4_prefetch(runtime.run_action, config, brief, issue_text, tel)
    if prefetch_block:
        brief = brief + "\n" + prefetch_block
        _l4_eid = _emit_structured_event(
            config, "L4", "prefetch",
            rendered_text=prefetch_block[:1200],
            evidence_items=[{"kind": "l4_constraint", "text": prefetch_block[:500], "source": "graph_db"}],
        )
        _log_gt_interaction(config, "L4", "prefetch", "prefetch_ok", prefetch_block[:500], event_id=_l4_eid or "")
    else:
        _l4_skip_eid = _emit_structured_event(
            config, "L4", "prefetch",
            emitted=False, suppressed=True,
            suppression_reason="no_prefetch_results",
        )
        _log_gt_interaction(config, "L4", "prefetch", "prefetch_skip", "no_results", event_id=_l4_skip_eid or "")

    try:
        instance["gt_brief"] = brief
        instance["gt_brief_full"] = brief_full  # Full untruncated brief for logging
    except Exception:
        try:
            setattr(instance, "gt_brief", brief)
            setattr(instance, "gt_brief_full", brief_full)
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


_GT_BASELINE = os.environ.get("GT_BASELINE", "0") == "1"

def patched_get_instruction(instance: Any, metadata: Any) -> Any:
    if _ORIG_GET_INSTRUCTION is None:
        raise RuntimeError("OpenHands get_instruction patch was not initialized")
    msg = _ORIG_GET_INSTRUCTION(instance, metadata)
    if _GT_BASELINE:
        print("[GT_META] BASELINE MODE — no GT layers", flush=True)
        return msg
    content = getattr(msg, "content", "") or ""
    brief = generate_task_brief(instance)
    if brief:
        content = f"<gt-task-brief>\n{brief}\n</gt-task-brief>\n\n" + content
        # Log L1 brief injection — use full untruncated brief for logging
        brief_full_for_log = (
            getattr(instance, "gt_brief_full", "")
            or (instance.get("gt_brief_full", "") if isinstance(instance, dict) else "")
            or brief
        )
        runtime = (
            getattr(instance, "_gt_runtime", None)
            or (instance.get("_gt_runtime") if isinstance(instance, dict) else None)
        )
        config = getattr(runtime, "_gt_full_config", None) if runtime else None
        if config:
            print(f"[GT_META] L1 brief injected ({len(brief_full_for_log)} chars)", flush=True)

            # Structured L1 event (emit first to get event_id)
            l1_items: list[dict] = []
            try:
                l1_json_path = "/tmp/gt_l1_structured.json"
                if os.path.exists(l1_json_path):
                    with open(l1_json_path) as _f:
                        l1_data = json.load(_f)
                    l1_items = l1_data.get("candidates", [])
            except Exception:
                pass
            l1_eid = _emit_structured_event(
                config, "L1", "localization_brief",
                rendered_text=brief_full_for_log,
                evidence_items=l1_items,
            )
            _log_gt_interaction(
                config, "L1", "brief", "brief_injection", brief_full_for_log,
                agent_action_before="", event_id=l1_eid or "",
            )
            # Belief events for each L1 candidate
            for item in l1_items:
                _emit_belief_event(
                    config,
                    file_path=item.get("file_path", ""),
                    new_status="candidate",
                    reason=f"L1 candidate: {item.get('reason', '')}",
                    source_event_id=l1_eid or "",
                    score=item.get("confidence"),
                )
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
    from openhands.core.config import get_llm_config_arg
    try:
        from openhands.core.config import get_evaluation_parser
    except ImportError:
        from openhands.core.config import get_parser as get_evaluation_parser
    try:
        from openhands.core.config.condenser_config import NoOpCondenserConfig
    except ImportError:
        NoOpCondenserConfig = None
    try:
        from openhands.core.config.utils import get_condenser_config_arg
    except ImportError:
        get_condenser_config_arg = None

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
    if hasattr(llm_config, "reasoning_effort"):
        llm_config.reasoning_effort = None

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

    # DIAGNOSTIC: log OH LLMConfig after __post_init__ to capture reasoning_effort etc.
    try:
        from openhands.llm.llm import LLM
        _orig_llm_init = LLM.__init__
        def _logged_llm_init(self, config, *a, **kw):
            _orig_llm_init(self, config, *a, **kw)
            cfg = self.config
            print(f"[GT_LLM_CONFIG] model={cfg.model} base_url={cfg.base_url} "
                  f"custom_llm_provider={getattr(cfg, 'custom_llm_provider', 'NONE')} "
                  f"temperature={cfg.temperature} top_p={getattr(cfg, 'top_p', 'NONE')} "
                  f"max_output_tokens={cfg.max_output_tokens} "
                  f"drop_params={getattr(cfg, 'drop_params', 'NONE')} "
                  f"modify_params={getattr(cfg, 'modify_params', 'NONE')} "
                  f"reasoning_effort={getattr(cfg, 'reasoning_effort', 'NONE')} "
                  f"enable_thinking={getattr(cfg, 'enable_thinking', 'NONE')} "
                  f"caching_prompt={getattr(cfg, 'caching_prompt', 'NONE')}", flush=True)
        LLM.__init__ = _logged_llm_init
    except Exception as e:
        print(f"[GT_LLM_CONFIG] patch failed: {e}", flush=True)

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
