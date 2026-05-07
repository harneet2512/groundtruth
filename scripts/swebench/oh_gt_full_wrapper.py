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
        "PYTHONPATH=/tmp:$PYTHONPATH "
        "python3 -m groundtruth.hooks.post_view "
        f"--root={config.workspace_root} --db={config.graph_db} --file={rel_path}"
    )


def make_edit_hook_command(event: HookEvent, config: GTRuntimeConfig) -> str:
    return (
        "PYTHONPATH=/tmp:$PYTHONPATH "
        "python3 -m groundtruth.hooks.post_edit "
        f"--root={config.workspace_root} --db={config.graph_db} "
        f"--quiet --max-items={config.max_items}"
    )


def render_l4_tool_footer(config: GTRuntimeConfig) -> str:
    return (
        "\nGT tools are available on PATH: "
        "gt_query <symbol>, gt_search <kind> <query>, "
        "gt_navigate <symbol> <mode>, gt_validate <file>.\n"
    )


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
    missing = [p for p in sorted(config.pending_checks) if not _path_covered_by_validation(p, config)]
    if not missing:
        return ""
    expected = " && ".join(f"gt_validate {path}" for path in missing[:3])
    return (
        '<gt-advisory layer="L5" expected="' + expected + '">\n'
        "Finish advisory: material edits have not been covered by gt_validate.\n"
        "</gt-advisory>"
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


def install_l4_tools(runtime: Any, config: GTRuntimeConfig) -> None:
    """Upload real GT tool bundles and expose them on PATH."""

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
    runtime.run_action(
        _cmd_action(
            f"grep -q 'GT_GRAPH_DB={config.graph_db}' ~/.bashrc || echo 'export GT_GRAPH_DB={config.graph_db}' >> ~/.bashrc; "
            f"grep -q 'GT_REPO_ROOT={config.workspace_root}' ~/.bashrc || echo 'export GT_REPO_ROOT={config.workspace_root}' >> ~/.bashrc; "
            "grep -q 'GT_PYTHON=python3' ~/.bashrc || echo 'export GT_PYTHON=python3' >> ~/.bashrc; "
            f"grep -q '{config.tools_dir}/gt_query/bin' ~/.bashrc || echo 'export PATH={config.tools_dir}/gt_query/bin:{config.tools_dir}/gt_search/bin:{config.tools_dir}/gt_navigate/bin:{config.tools_dir}/gt_validate/bin:$PATH' >> ~/.bashrc",
            30,
        )
    )
    runtime.run_action(
        _cmd_action(
            f"export GT_GRAPH_DB={config.graph_db}; export GT_REPO_ROOT={config.workspace_root}; export GT_PYTHON=python3; "
            f"export PATH={config.tools_dir}/gt_query/bin:{config.tools_dir}/gt_search/bin:{config.tools_dir}/gt_navigate/bin:{config.tools_dir}/gt_validate/bin:$PATH; "
            "command -v gt_query gt_search gt_navigate gt_validate",
            30,
        )
    )


def install_graph_and_hook(runtime: Any, config: GTRuntimeConfig) -> None:
    """Install groundtruth package into container and build graph.db."""

    host_index = os.environ.get("GT_INDEX_BINARY", "")
    if host_index and Path(host_index).exists() and config.gt_index_bin.startswith("/home/"):
        runtime.copy_to(host_index, "/tmp/")
        config.gt_index_bin = "/tmp/" + Path(host_index).name
        runtime.run_action(_cmd_action(f"chmod +x {config.gt_index_bin}", 30))

    groundtruth_pkg = _SRC_DIR / "groundtruth"
    if groundtruth_pkg.exists():
        payload = _bundle_dir_payload(groundtruth_pkg, "groundtruth")
        _upload_bytes_b64(runtime, payload, "/tmp/gt_src.tar.gz")
        runtime.run_action(
            _cmd_action(
                "cd /tmp && tar xzf gt_src.tar.gz && rm -f gt_src.tar.gz; "
                "grep -q 'PYTHONPATH=/tmp:$PYTHONPATH' ~/.bashrc || echo 'export PYTHONPATH=/tmp:$PYTHONPATH' >> ~/.bashrc; "
                "export PYTHONPATH=/tmp:$PYTHONPATH",
                120,
            )
        )
    runtime.run_action(
        _cmd_action(
            f"{config.gt_index_bin} -root={config.workspace_root} -output={config.graph_db}",
            120,
        )
    )
    install_l4_tools(runtime, config)


def wrap_runtime_run_action(runtime: Any, config: GTRuntimeConfig | None = None) -> Any:
    """Append GT evidence to agent-visible observations for eligible events."""

    config = config or GTRuntimeConfig()
    if getattr(runtime, "_gt_full_wrapped", False):
        return runtime
    orig_run_action = runtime.run_action

    def patched_run_action(action: Any) -> Any:
        act_text = _action_text(action)
        if _action_class(action) == "CmdRunAction" and "gt_validate" in act_text:
            register_gt_validate_paths(act_text, config)

        obs = orig_run_action(action)
        event = classify_tool_event(action, source_exts=config.source_exts)

        if event.kind == "post_view":
            hook_out = _run_internal(orig_run_action, make_view_hook_command(event, config), 30)
            evidence = (
                f'\n\n<gt-evidence trigger="post_view:{event.path}">\n'
                f"{hook_out.strip()}\n</gt-evidence>\n"
            )
            return append_observation(obs, evidence)

        if event.kind == "post_edit":
            config.pending_checks.add(event.path)
            reindex_out = _run_internal(orig_run_action, make_reindex_command(event.path, config), 120)
            hook_out = _run_internal(orig_run_action, make_edit_hook_command(event, config), 30)
            evidence = (
                f'\n\n<gt-evidence trigger="post_edit:{event.path}">\n'
                f"<gt-reindex command=\"{make_reindex_command(event.path, config)}\">\n"
                f"{reindex_out.strip()}\n</gt-reindex>\n"
                f"{hook_out.strip()}\n</gt-evidence>\n"
            )
            return append_observation(obs, evidence)

        if event.kind == "finish":
            advisory = render_l5_advisory(config)
            if advisory:
                return append_observation(obs, "\n\n" + advisory + "\n")

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
    root = f"{WORKSPACE_ROOT}/{workspace_name}" if workspace_name else WORKSPACE_ROOT
    config = GTRuntimeConfig(workspace_root=root)
    install_graph_and_hook(runtime, config)
    issue_text = getattr(instance, "problem_statement", "") or ""
    if not issue_text and isinstance(instance, dict):
        issue_text = str(instance.get("problem_statement", "") or "")

    brief = ""
    l2_tag = ""
    host_graph_db = _download_graph_db_to_host(runtime, config.graph_db)
    task_id = workspace_name or "unknown"
    if host_graph_db and issue_text.strip():
        try:
            from groundtruth.pretask.v7_brief import generate_brief  # type: ignore[import]

            out = generate_brief(
                issue_text=issue_text,
                repo_root=config.workspace_root,
                graph_db=host_graph_db,
                return_telemetry=True,
                task_id=task_id,
            )
            if isinstance(out, str):
                brief = out.strip()
            else:
                brief = str(getattr(out, "brief", "") or "").strip()
                l2_tag = _format_l2_pretask_tag(getattr(out, "telemetry", None))
        except Exception as exc:
            brief = f"GT graph built. Brief generation failed: {exc}"
    if brief and l2_tag:
        brief = f"{brief}\n\n{l2_tag}"
    if not brief:
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
    try:
        msg.content = content + render_l4_tool_footer(GTRuntimeConfig())
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
