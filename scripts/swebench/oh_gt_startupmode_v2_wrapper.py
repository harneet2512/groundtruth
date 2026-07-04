#!/usr/bin/env python3
"""OpenHands wrapper for v2 — real GroundTruth product via synthesis endpoints.

Injects the groundtruth package (src/groundtruth/) into SWE-bench containers,
pre-builds SQLite index, and configures PostToolUse hooks that call the real
verify and understand endpoints. Falls back to gt_tool_v4.py if package fails.

Usage:
    python oh_gt_startupmode_v2_wrapper.py .llm_config/vertex_qwen3.json \
        --workspace docker \
        --prompt-path gt_startupmode.j2 \
        --max-iterations 100 \
        --num-workers 5 \
        --hooks write-only
"""

import base64
import io
import os
import sys
import tarfile
import tempfile

sys.path.insert(0, os.getcwd())

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_DIR = os.path.dirname(os.path.dirname(_SCRIPT_DIR))
_SRC_DIR = os.path.join(_REPO_DIR, "src")
_FALLBACK_TOOL = os.path.join(_REPO_DIR, "benchmarks", "swebench", "gt_tool_v4.py")

# Directories to include in the package tar
_INCLUDE_DIRS = [
    "groundtruth/__init__.py",
    "groundtruth/hooks/",
    "groundtruth/validators/",
    "groundtruth/analysis/",
    "groundtruth/policy/",
    "groundtruth/index/",
    "groundtruth/core/",
    "groundtruth/utils/",
    "groundtruth/foundation/",
    "groundtruth/observability/",
]

# File patterns to exclude
_EXCLUDE_PATTERNS = {"__pycache__", ".pyc", ".pyo"}


def _build_package_tar() -> bytes:
    """Build a tar.gz of the groundtruth package from src/."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for entry in _INCLUDE_DIRS:
            full_path = os.path.join(_SRC_DIR, entry)
            if os.path.isfile(full_path):
                arcname = entry
                tar.add(full_path, arcname=arcname)
            elif os.path.isdir(full_path):
                for dirpath, dirnames, filenames in os.walk(full_path):
                    # Prune excluded dirs
                    dirnames[:] = [d for d in dirnames if d not in _EXCLUDE_PATTERNS]
                    for fname in filenames:
                        if any(fname.endswith(p) for p in (".pyc", ".pyo")):
                            continue
                        fpath = os.path.join(dirpath, fname)
                        arcname = os.path.relpath(fpath, _SRC_DIR)
                        tar.add(fpath, arcname=arcname)
    return buf.getvalue()


def patch_and_run():
    """Monkey-patch SWEBenchEvaluation to inject groundtruth package."""

    # Parse --hooks flag
    hooks_mode = "write-only"
    filtered_argv = []
    for arg in sys.argv[1:]:
        if arg.startswith("--hooks="):
            hooks_mode = arg.split("=", 1)[1]
        else:
            filtered_argv.append(arg)
    sys.argv = [sys.argv[0]] + filtered_argv

    enable_read_hook = hooks_mode in ("both", "read-write", "all")
    enable_write_hook = hooks_mode in ("write-only", "both", "read-write", "all")

    print(f"GT startupmode v2 hooks: {hooks_mode}")
    print(f"  Read hook (understand):  {'ON' if enable_read_hook else 'OFF'}")
    print(f"  Write hook (verify):     {'ON' if enable_write_hook else 'OFF'}")

    # Build package tar
    print("Building groundtruth package tar...")
    pkg_tar = _build_package_tar()
    pkg_b64 = base64.b64encode(pkg_tar).decode("ascii")
    CHUNK_SIZE = 8000
    pkg_chunks = [pkg_b64[i: i + CHUNK_SIZE] for i in range(0, len(pkg_b64), CHUNK_SIZE)]
    print(f"  Package: {len(pkg_tar):,} bytes, base64: {len(pkg_b64):,} bytes, {len(pkg_chunks)} chunks")

    # Also prepare fallback gt_tool_v4.py
    fallback_chunks = []
    if os.path.exists(_FALLBACK_TOOL):
        with open(_FALLBACK_TOOL, "rb") as f:
            fb_b64 = base64.b64encode(f.read()).decode("ascii")
        fallback_chunks = [fb_b64[i: i + CHUNK_SIZE] for i in range(0, len(fb_b64), CHUNK_SIZE)]
        print(f"  Fallback: gt_tool_v4.py ({len(fallback_chunks)} chunks)")

    from benchmarks.swebench.run_infer import SWEBenchEvaluation, main
    from openhands.sdk.hooks.config import HookConfig, HookMatcher, HookDefinition

    _original_evaluate = SWEBenchEvaluation.evaluate_instance

    def patched_evaluate(self, instance, workspace):
        """Inject groundtruth package, pre-index, configure hooks."""

        # Step 1: Inject groundtruth package via tar + base64
        ok = True
        for i, chunk in enumerate(pkg_chunks):
            op = ">" if i == 0 else ">>"
            res = workspace.execute_command(f"echo -n '{chunk}' {op} /tmp/gt_pkg.b64")
            if res.exit_code != 0:
                ok = False
                break

        pkg_installed = False
        if ok:
            res = workspace.execute_command(
                "base64 -d /tmp/gt_pkg.b64 > /tmp/gt_pkg.tar.gz && "
                "mkdir -p /tmp/gt_src && "
                "tar xzf /tmp/gt_pkg.tar.gz -C /tmp/gt_src/ && "
                "rm -f /tmp/gt_pkg.b64 /tmp/gt_pkg.tar.gz && "
                "echo GT_PKG_READY"
            )
            if "GT_PKG_READY" in (res.stdout or ""):
                pkg_installed = True
                print(f"  GT v2 package installed: {instance.id}")

        # Step 1b: Inject fallback gt_tool_v4.py
        if fallback_chunks:
            for i, chunk in enumerate(fallback_chunks):
                op = ">" if i == 0 else ">>"
                workspace.execute_command(f"echo -n '{chunk}' {op} /tmp/gt_fb.b64")
            workspace.execute_command(
                "base64 -d /tmp/gt_fb.b64 > /tmp/gt_tool.py && "
                "chmod +x /tmp/gt_tool.py && "
                "rm -f /tmp/gt_fb.b64"
            )

        # Step 2: Pre-build index
        if pkg_installed:
            res = workspace.execute_command(
                "cd /testbed && PYTHONPATH=/tmp/gt_src:$PYTHONPATH "
                "timeout 45 python -m groundtruth.hooks.indexer_cli "
                "--root=/testbed --db=/tmp/gt_index.db 2>/dev/null || true"
            )
            if res.stdout and "INDEX_READY" in res.stdout:
                print(f"  Index pre-built (v2): {instance.id} -- {res.stdout.strip()}")
            else:
                print(f"  Index pre-build (v2): {res.stdout.strip() if res.stdout else 'no output'}: {instance.id}")

        # Fallback: build index with gt_tool_v4.py
        if not pkg_installed:
            res = workspace.execute_command(
                "cd /testbed && timeout 45 python3 /tmp/gt_tool.py --build-index 2>/dev/null || true"
            )
            print(f"  Fallback index: {instance.id} -- {res.stdout.strip() if res.stdout else 'no output'}")

        # Step 3: Run original evaluation
        return _original_evaluate(self, instance, workspace)

    SWEBenchEvaluation.evaluate_instance = patched_evaluate

    # Step 4: Patch Conversation.__new__ to inject hook_config
    try:
        from openhands.sdk.conversation import Conversation

        _orig_conv_new = Conversation.__new__

        # Build hook commands with fallback chain
        if pkg_installed := True:  # will be True at runtime after injection
            verify_cmd = (
                "cd /testbed && PYTHONPATH=/tmp/gt_src:$PYTHONPATH "
                "python -m groundtruth.hooks.post_edit "
                "--root=/testbed --db=/tmp/gt_index.db --quiet --max-items=3 "
                "2>/dev/null || "
                "python3 /tmp/gt_tool.py check --quiet --max-items=3 "
                "2>/dev/null || true"
            )
            understand_cmd = (
                "cd /testbed && PYTHONPATH=/tmp/gt_src:$PYTHONPATH "
                "python -m groundtruth.hooks.post_view "
                "--root=/testbed --db=/tmp/gt_index.db "
                "--file=$OPENHANDS_FILE_PATH "
                "2>/dev/null || true"
            )

        def patched_conv_new(cls, *args, **kwargs):
            if "hook_config" not in kwargs or kwargs["hook_config"] is None:
                post_hooks = []
                if enable_write_hook:
                    post_hooks.append(
                        HookMatcher(
                            matcher="file_editor",
                            hooks=[HookDefinition(
                                command=verify_cmd,
                                timeout=15,
                            )]
                        )
                    )
                if enable_read_hook:
                    post_hooks.append(
                        HookMatcher(
                            matcher="file_editor",
                            hooks=[HookDefinition(
                                command=understand_cmd,
                                timeout=10,
                            )]
                        )
                    )
                if post_hooks:
                    kwargs["hook_config"] = HookConfig(post_tool_use=post_hooks)
            return _orig_conv_new(cls, *args, **kwargs)

        Conversation.__new__ = patched_conv_new
        print("Patched Conversation.__new__ to inject GT v2 hook_config")
    except Exception as e:
        print(f"  WARNING: Could not patch Conversation: {e}")
        import traceback
        traceback.print_exc()

    print("Patched SWEBenchEvaluation.evaluate_instance with GT v2 package injection")
    print()

    main()


if __name__ == "__main__":
    patch_and_run()
