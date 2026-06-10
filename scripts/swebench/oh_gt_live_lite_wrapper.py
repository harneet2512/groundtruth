#!/usr/bin/env python3
"""GT-arm wrapper for the Live Lite paired runner.

Modeled on `oh_gt_startupmode_v2_wrapper.py`. Differences:

  1. Live Lite-aware: forwards `--dataset SWE-bench-Live/SWE-bench-Live`,
     `--split lite`, `--instance-ids-file <config.json>` to the upstream
     SWE-Bench eval harness (`benchmarks.swebench.run_infer`).

  2. MCP registration: monkey-patches `Conversation.__new__` to inject
     `mcp_config={"mcpServers": {"groundtruth": {...}}}` so the agent can
     call the 7 GT MCP tools mid-trajectory.

  3. Pre-task brief injection: monkey-patches `SWEBenchEvaluation.evaluate_instance`
     to generate a v8.2.2 RRF brief on the *host* against pre-built indexes
     at GT_PREBUILT_INDEXES_ROOT (default
     /home/Lenovo/eval_live_lite_2026-05-04/gt_indexes/<id>/graph.db) and
     push the rendered brief as the agent's first user turn via
     /tmp/gt_first_turn_<id>.txt + GT_FIRST_TURN_TEXT_PATH env.

  4. Hooks active by default: `--hooks=both` (post_edit + post_view) — flag
     parity with the parent wrapper but default flipped.

  5. Model: GLM-4.7 on Vertex MaaS via `.llm_config/vertex_glm47.json`.

This script is meant to run on a VM with:
  - OpenHands SDK installed (`openhands.sdk.Agent`, `Conversation`, `Tool`,
    `LLM`, `HookConfig`, `HookMatcher`, `HookDefinition`)
  - SWE-Bench eval harness installed (`benchmarks.swebench.run_infer.main`,
    `SWEBenchEvaluation`)
  - Docker daemon + SWE-bench-Live images pre-pulled
  - HF dataset cache populated for `SWE-bench-Live/SWE-bench-Live`

Usage (driven by run_live_lite_paired.py — do not invoke directly unless
debugging):

    python scripts/swebench/oh_gt_live_lite_wrapper.py \\
        .llm_config/vertex_glm47.json \\
        --workspace docker \\
        --dataset SWE-bench-Live/SWE-bench-Live \\
        --split lite \\
        --instance-ids-file <arm_A/config.json> \\
        --output-dir <arm_A/> \\
        --hooks both \\
        --max-iterations 100
"""
from __future__ import annotations

import base64
import io
import json
import os
import sys
import tarfile
from pathlib import Path

sys.path.insert(0, os.getcwd())

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_DIR = os.path.dirname(os.path.dirname(_SCRIPT_DIR))
_SRC_DIR = os.path.join(_REPO_DIR, "src")

# Same package layout as parent wrapper. The hook scripts live here and
# /tmp/gt_src/groundtruth/* is the in-container layout after the tar is
# unpacked.
_INCLUDE_DIRS = [
    "groundtruth/__init__.py",
    "groundtruth/hooks/",
    "groundtruth/validators/",
    "groundtruth/analysis/",
    "groundtruth/policy/",
    "groundtruth/index/",
    "groundtruth/control/",
    "groundtruth/pretask/",
    "groundtruth/adapters/",
    "groundtruth/mcp/",
    "groundtruth/ai/",
    "groundtruth/core/",
    "groundtruth/utils/",
    "groundtruth/foundation/",
    "groundtruth/observability/",
    "groundtruth/evidence/",
    "groundtruth/grounding/",
    "groundtruth/lsp/",
    "groundtruth/runtime/",
    "groundtruth/stats/",
    "groundtruth/viz/",
]

_EXCLUDE_PATTERNS = {"__pycache__", ".pyc", ".pyo"}

# v1.0.5: gt_intel.py (the evidence engine) lives outside src/ at
# benchmarks/swebench/. Bundle it explicitly so the composite endpoints can
# `from benchmarks.swebench import gt_intel` under PYTHONPATH=/tmp/gt_src.
_EXTRA_FILES_FROM_REPO_ROOT: list[str] = [
    "benchmarks/__init__.py",
    "benchmarks/swebench/__init__.py",
    "benchmarks/swebench/gt_intel.py",
]


# v1.0.5 system prompt — V104 lines 9-32 ported to OH conventions.
# Drops: gt_orient (subsumed by v8.2.2 brief), "gt_orient first" sentence.
# Adapts: SWE-agent `submit` → OH `finish` action; "shell action" → tool calls.
# Keeps: budget caps, "treat as ground truth" framing, gt-evidence semantics.
_V104_OH_QWEN3_SYSTEM_MESSAGE = """\
You are a helpful coding assistant that solves real software engineering tasks
in a Linux workspace. You have access to a file editor, a terminal, and codebase
intelligence tools:

  gt_lookup <symbol>   — callers, callees, tests, type contracts, and last-touched
                          context for a concrete symbol
  gt_impact <symbol>   — blast radius / downstream dependencies / sibling norms
                          before risky edits
  gt_check <file>      — post-edit structural & regression check; required
                          coverage on each edited file before you finish

Budget per task: gt_lookup=2, gt_impact=2, gt_check=3 calls. Use the tools
directly when they improve confidence or reduce risk.

Typical flow: read the issue and the v8.2.2 brief delivered in the first
user turn, identify a concrete symbol/file target, call gt_lookup on the
primary symbol/file, call gt_impact before any edit that could change
shared or downstream behavior, then read and edit as needed, and call
gt_check on every edited file before you finish.

Evidence may be emitted after your edits as <gt-evidence> blocks — treat it
as diagnostic context, not directives. On a concrete symbol/file, call
gt_lookup before editing, call gt_impact before risky edits, and call
gt_check on every edited file before finish.

CONTROL CONTRACT (enforced by the runtime, not just advisory):
- Per-task caps: gt_lookup=2, gt_impact=2, gt_check=3. A cap hit returns a
  line like:
      BUDGET_EXHAUSTED: gt_lookup has reached its per-task cap of 2. Use
      gt_impact for a different angle on the same symbol.
  Treat this as ground truth — do NOT retry the same gt_ tool; follow the
  redirect.
- After a material edit, finishing is gated on gt_check covering that edit.
  A blocked finish returns:
      <gt-intervention id="finish-gate-..." expected="gt_check src/foo.py" expires="+2">
      Finish blocked (attempt 1/3): material edit at src/foo.py was not
      verified. Run gt_check src/foo.py before retrying finish.
      </gt-intervention>
  When you see <gt-intervention expected="...">, run exactly that command
  within the next 2 actions. Ignoring the intervention for a third time
  will soft-escape and allow finish through, but the attempt will be
  logged as a gate bypass.

HOW TO FINISH A TASK (CRITICAL):
When your fix is complete and validated, call the `finish` tool. Do NOT
echo "task completed". Do NOT repeat `git diff` once you are satisfied with
your fix — one `finish` is how you end the task and have your patch
recorded. If you have verified the fix and find yourself about to repeat
the same command, call finish instead.
"""


def _build_package_tar() -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for entry in _INCLUDE_DIRS:
            full_path = os.path.join(_SRC_DIR, entry)
            if os.path.isfile(full_path):
                tar.add(full_path, arcname=entry)
            elif os.path.isdir(full_path):
                for dirpath, dirnames, filenames in os.walk(full_path):
                    dirnames[:] = [d for d in dirnames if d not in _EXCLUDE_PATTERNS]
                    for fname in filenames:
                        if any(fname.endswith(p) for p in (".pyc", ".pyo")):
                            continue
                        fpath = os.path.join(dirpath, fname)
                        arcname = os.path.relpath(fpath, _SRC_DIR)
                        tar.add(fpath, arcname=arcname)
        # Extra files from repo root (e.g. benchmarks/swebench/gt_intel.py),
        # bundled with their repo-relative arcname so the in-container layout
        # matches the host import path.
        for rel in _EXTRA_FILES_FROM_REPO_ROOT:
            full_path = os.path.join(_REPO_DIR, rel)
            if os.path.isfile(full_path):
                tar.add(full_path, arcname=rel)
            else:
                print(f"  WARN: extra file not found, skipping: {rel}")
    return buf.getvalue()


def _load_instance_ids(path: str) -> list[str]:
    """Load instance_ids from the per-arm config.json the orchestrator wrote."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return data["instance_ids"]


def _generate_brief_on_host(instance) -> str | None:
    """Build a v8.2.2 RRF brief on the host VM using prebuilt indexes.

    The v8.2.2 ranker depends on sentence-transformers + v7.4 — too heavy to
    install in every SWE-Bench task container, so we run it on the host
    (eval VM) against pre-built indexes at GT_PREBUILT_INDEXES_ROOT (default
    /home/Lenovo/eval_live_lite_2026-05-04/gt_indexes/<instance_id>/graph.db).

    The caller writes the returned text into the container's
    /tmp/gt_first_turn_<id>.txt — never blocks the run on failure.
    """
    indexes_root = os.environ.get(
        "GT_PREBUILT_INDEXES_ROOT",
        "/home/Lenovo/eval_live_lite_2026-05-04/gt_indexes",
    )
    graph_db = os.path.join(indexes_root, instance.id, "graph.db")
    if not os.path.exists(graph_db):
        print(f"  v22 brief: no prebuilt graph.db at {graph_db} — skipping brief")
        return None

    # Optional host-side repo extract for body-snippet semantic. Missing path
    # → ranker falls back to lexical-only without raising.
    repo_extracts_root = os.environ.get(
        "GT_REPO_EXTRACTS_ROOT",
        "/home/Lenovo/eval_live_lite_2026-05-04/repos",
    )
    repo_path = os.path.join(repo_extracts_root, instance.id)
    if not os.path.exists(repo_path):
        repo_path = ""

    issue_text = getattr(instance, "problem_statement", "") or ""
    if not issue_text.strip():
        return None

    try:
        from groundtruth.pretask.v1r_brief import generate_v1r_brief
        result = generate_v1r_brief(
            issue_text,
            repo_path,
            graph_db,
            bug_id=getattr(instance, "id", "unknown") or "unknown",
            repo=getattr(instance, "repo", "unknown") or "unknown",
        )
        brief = result.brief_text
    except Exception as exc:
        print(f"  v1r brief generation failed for {instance.id}: {exc}")
        return None
    return brief or None


def patch_and_run() -> None:
    """Apply Live Lite + MCP + brief + hook patches, then call harness main()."""

    # --- parse flags --------------------------------------------------------
    hooks_mode = "both"
    instance_ids_file: str | None = None
    output_dir: str | None = None
    filtered = []
    i = 1
    while i < len(sys.argv):
        a = sys.argv[i]
        if a.startswith("--hooks="):
            hooks_mode = a.split("=", 1)[1]
        elif a == "--hooks" and i + 1 < len(sys.argv):
            hooks_mode = sys.argv[i + 1]; i += 1
        elif a == "--instance-ids-file" and i + 1 < len(sys.argv):
            instance_ids_file = sys.argv[i + 1]; i += 1
            # forward to harness too — it knows the same flag (or alias)
            filtered.extend(["--instance-ids-file", sys.argv[i]])
        elif a == "--output-dir" and i + 1 < len(sys.argv):
            output_dir = sys.argv[i + 1]; i += 1
            filtered.extend(["--output-dir", sys.argv[i]])
        else:
            filtered.append(a)
        i += 1
    sys.argv = [sys.argv[0]] + filtered

    enable_read_hook = hooks_mode in ("both", "read-write", "all")
    enable_write_hook = hooks_mode in ("write-only", "both", "read-write", "all")

    print(f"GT Live-Lite wrapper hooks: {hooks_mode}")
    print(f"  Read hook  (post_view) : {'ON' if enable_read_hook else 'OFF'}")
    print(f"  Write hook (post_edit) : {'ON' if enable_write_hook else 'OFF'}")

    instance_ids: list[str] | None = None
    if instance_ids_file and os.path.exists(instance_ids_file):
        instance_ids = _load_instance_ids(instance_ids_file)
        print(f"  Instance filter: {len(instance_ids)} ids from {instance_ids_file}")

    # --- build package tar --------------------------------------------------
    print("Building groundtruth package tar...")
    pkg_tar = _build_package_tar()
    pkg_b64 = base64.b64encode(pkg_tar).decode("ascii")
    CHUNK = 8000
    pkg_chunks = [pkg_b64[i:i + CHUNK] for i in range(0, len(pkg_b64), CHUNK)]
    print(f"  Package: {len(pkg_tar):,} bytes, {len(pkg_chunks)} chunks")

    # --- import upstream harness -------------------------------------------
    # These imports only resolve on the VM where the SWE-Bench harness is
    # installed. Local linting won't see them.
    from benchmarks.swebench.run_infer import SWEBenchEvaluation, main  # type: ignore[import-not-found]
    from openhands.sdk.hooks.config import HookConfig, HookMatcher, HookDefinition  # type: ignore[import-not-found]

    _orig_eval = SWEBenchEvaluation.evaluate_instance

    def patched_eval(self, instance, workspace):  # type: ignore[no-redef]
        # Instance-id filter (paired-runner restricts to locked id list)
        if instance_ids is not None and instance.id not in instance_ids:
            print(f"  SKIP (not in id list): {instance.id}")
            return None

        # v1.0.5: propagate the current task's instance_id so the composite
        # MCP subprocess (started by patched_conv_new below) and the host-side
        # composite endpoints share a per-task budget counter at
        # /tmp/gt_calls_<id>.json.
        os.environ["GT_INSTANCE_ID"] = instance.id

        # Step 1: inject groundtruth package
        ok = True
        for k, chunk in enumerate(pkg_chunks):
            op = ">" if k == 0 else ">>"
            res = workspace.execute_command(f"echo -n '{chunk}' {op} /tmp/gt_pkg.b64")
            if res.exit_code != 0:
                ok = False; break
        pkg_installed = False
        if ok:
            res = workspace.execute_command(
                "base64 -d /tmp/gt_pkg.b64 > /tmp/gt_pkg.tar.gz && "
                "mkdir -p /tmp/gt_src && "
                "tar xzf /tmp/gt_pkg.tar.gz -C /tmp/gt_src/ && "
                "rm -f /tmp/gt_pkg.b64 /tmp/gt_pkg.tar.gz && "
                "echo GT_PKG_READY"
            )
            pkg_installed = "GT_PKG_READY" in (res.stdout or "")
            if pkg_installed:
                print(f"  GT package installed: {instance.id}")

        # Step 2: ensure graph.db exists at /tmp/graph.db
        # The V3 indexer patch (run_infer_v3_indexer.patch) populates it; if
        # that patch isn't applied here, we build it ourselves.
        workspace.execute_command(
            "[ -f /tmp/graph.db ] || ("
            "cd /testbed && "
            "test -x /tmp/gt-index-linux && "
            "/tmp/gt-index-linux -root /testbed -output /tmp/graph.db || "
            "echo GT_INDEX_MISSING_USE_FALLBACK)"
        )

        # Step 3: generate v8.2.2 brief on host (heavy deps) and stash it
        # where the harness can read it at first-turn time. The harness's
        # first-turn logic must accept a `--first-turn-text-file` flag (or
        # env var) — we publish to a stable path inside the container.
        # NOTE: upstream-CLI-dependent; verify on first VM run.
        brief_text = _generate_brief_on_host(instance)
        if brief_text:
            ft_path = f"/tmp/gt_first_turn_{instance.id}.txt"
            workspace.execute_command(
                f"cat > {ft_path} <<'__GT_BRIEF__'\n{brief_text}\n__GT_BRIEF__"
            )
            os.environ["GT_FIRST_TURN_TEXT_PATH"] = ft_path

        return _orig_eval(self, instance, workspace)

    SWEBenchEvaluation.evaluate_instance = patched_eval
    print("Patched SWEBenchEvaluation.evaluate_instance for GT Live-Lite")

    # Step 4: monkey-patch Conversation.__new__ to inject MCP + hooks
    try:
        from openhands.sdk.conversation import Conversation  # type: ignore[import-not-found]

        _orig_conv_new = Conversation.__new__

        verify_cmd = (
            "cd /testbed && PYTHONPATH=/tmp/gt_src:$PYTHONPATH "
            "python -m groundtruth.hooks.post_edit "
            "--root=/testbed --db=/tmp/graph.db --quiet --max-items=3 2>/dev/null || true"
        )
        understand_cmd = (
            "cd /testbed && PYTHONPATH=/tmp/gt_src:$PYTHONPATH "
            "python -m groundtruth.hooks.post_view "
            "--root=/testbed --db=/tmp/graph.db "
            "--file=$OPENHANDS_FILE_PATH 2>/dev/null || true"
        )
        # v1.0.5 pre-submit gate: fires before the OH `finish` action. Reads
        # /tmp/gt_check_log.jsonl + git diff to detect uncovered material edits
        # and emits a <gt-intervention expected="gt_check <file>"> the agent's
        # system_message tells it to comply with. Soft-escapes after 3 attempts.
        pre_finish_cmd = (
            "cd /testbed && GT_WORKSPACE=/testbed "
            "PYTHONPATH=/tmp/gt_src:$PYTHONPATH "
            "python -m groundtruth.runtime.pre_finish_gate 2>/dev/null || true"
        )

        # v1.0.5: only 3 composite endpoints exposed — gt_lookup / gt_impact /
        # gt_check. The full 24-tool surface in groundtruth.mcp.server is not
        # registered for this run. GT_INSTANCE_ID is read at conversation-build
        # time so each task's MCP subprocess inherits the right counter key.
        def _gt_mcp_config():
            return {
                "mcpServers": {
                    "groundtruth": {
                        "command": sys.executable,
                        "args": ["-m", "groundtruth.mcp.composite_server",
                                 "--root", "/testbed",
                                 "--db", "/tmp/graph.db"],
                        "env": {
                            "PYTHONPATH": "/tmp/gt_src",
                            "GT_INSTANCE_ID": os.environ.get("GT_INSTANCE_ID", ""),
                        },
                    }
                }
            }

        def patched_conv_new(cls, *args, **kwargs):  # type: ignore[no-redef]
            # Hooks
            if "hook_config" not in kwargs or kwargs["hook_config"] is None:
                post_hooks = []
                if enable_write_hook:
                    post_hooks.append(HookMatcher(
                        matcher="file_editor",
                        hooks=[HookDefinition(command=verify_cmd, timeout=15)]))
                if enable_read_hook:
                    post_hooks.append(HookMatcher(
                        matcher="file_editor",
                        hooks=[HookDefinition(command=understand_cmd, timeout=10)]))
                pre_hooks = [HookMatcher(
                    matcher="finish",
                    hooks=[HookDefinition(command=pre_finish_cmd, timeout=10)])]
                hook_kwargs: dict = {"pre_tool_use": pre_hooks}
                if post_hooks:
                    hook_kwargs["post_tool_use"] = post_hooks
                kwargs["hook_config"] = HookConfig(**hook_kwargs)
            # MCP — only set if not already supplied (so the orchestrator can
            # override per-task without us trampling).
            agent = kwargs.get("agent") or (args[0] if args else None)
            if agent is not None and not getattr(agent, "mcp_config", None):
                try:
                    agent.mcp_config = _gt_mcp_config()  # type: ignore[attr-defined]
                except Exception as exc:
                    print(f"  WARN: could not set agent.mcp_config: {exc}")
            # v1.0.5: inject V104-derived system_message (gt_orient dropped,
            # OH-finish framing). Skip if the agent already has a
            # caller-provided system_message so we don't trample explicit
            # configuration.
            if agent is not None:
                existing_sm = getattr(agent, "system_message", None)
                if not existing_sm:
                    try:
                        agent.system_message = _V104_OH_QWEN3_SYSTEM_MESSAGE  # type: ignore[attr-defined]
                    except Exception as exc:
                        print(f"  WARN: could not set agent.system_message: {exc}")
            return _orig_conv_new(cls, *args, **kwargs)

        Conversation.__new__ = patched_conv_new  # type: ignore[method-assign]
        print("Patched Conversation.__new__ for MCP + hooks")
    except Exception as exc:
        print(f"  WARNING: Could not patch Conversation: {exc}")
        import traceback
        traceback.print_exc()

    print()
    if output_dir:
        print(f"  output_dir: {output_dir}")
    main()


if __name__ == "__main__":
    patch_and_run()
