#!/usr/bin/env python3
"""OpenHands wrapper for GT v8 — zero-tax precomputed context injection.

Strategy: Run gt_hook.py understand on key files BEFORE the agent starts,
then inject the cross-file intelligence into the instance prompt. The agent
gets callers, test files, and norms for FREE — zero iteration cost.

Also sets up a user_prompt_submit hook so GT context is refreshed each turn
via additionalContext (if the workspace changed).

Architecture:
1. Monkey-patch SWEBenchEvaluation.evaluate_instance
2. Inject gt_hook.py into each Docker container
3. Pre-run understand on files from the issue description
4. Prepend GT analysis to the instance prompt
5. After task, extract /tmp/gt_hook_log.jsonl for analysis

Usage:
    cd /root/oh-benchmarks
    .venv/bin/python /path/to/oh_gt_v8_wrapper.py <llm_config.json> \
        --workspace docker --max-iterations 50 --num-workers 4 \
        --prompt-path gt_hook_v7.j2 --output-dir <dir> --note v8_gt
"""
from __future__ import annotations

import base64
import os
import re
import sys

sys.path.insert(0, os.getcwd())

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_DIR = os.path.dirname(os.path.dirname(_SCRIPT_DIR))
_HOOK_TOOL = os.path.join(_REPO_DIR, "benchmarks", "swebench", "gt_hook.py")


def _extract_likely_files(problem_statement: str, repo_name: str) -> list[str]:
    """Extract file paths mentioned in the issue description."""
    # Match common Python file path patterns
    patterns = [
        r'[\w/]+\.py',  # basic .py paths
        r'[\w/]+/[\w]+\.py',  # paths with at least one directory
    ]
    files = set()
    for pat in patterns:
        for match in re.findall(pat, problem_statement):
            # Filter out obvious non-paths
            if '/' in match and not match.startswith('http'):
                files.add(match)

    # Also extract from code blocks and stack traces
    for line in problem_statement.split('\n'):
        line = line.strip()
        # Stack trace: File "path", line N
        m = re.search(r'File "([^"]+\.py)"', line)
        if m:
            path = m.group(1)
            # Normalize to repo-relative
            if repo_name in path:
                path = path.split(repo_name + '/')[-1]
            files.add(path)

    return sorted(files)[:5]  # Cap at 5 files


def patch_and_run() -> None:
    """Load gt_hook.py, patch evaluate_instance with precomputed context, run."""

    if not os.path.exists(_HOOK_TOOL):
        print(f"ERROR: gt_hook.py not found at {_HOOK_TOOL}")
        sys.exit(1)

    with open(_HOOK_TOOL, "rb") as fh:
        hook_bytes = fh.read()

    gt_b64 = base64.b64encode(hook_bytes).decode("ascii")
    CHUNK = 50_000
    chunks = [gt_b64[i: i + CHUNK] for i in range(0, len(gt_b64), CHUNK)]
    print(f"gt_hook.py: {len(hook_bytes):,} bytes | {len(gt_b64):,} b64 | {len(chunks)} chunks")

    from benchmarks.swebench.run_infer import SWEBenchEvaluation, main  # type: ignore[import]

    _original_evaluate = SWEBenchEvaluation.evaluate_instance

    def patched_evaluate(self, instance, workspace):  # type: ignore[override]
        instance_id = getattr(instance, "id", str(instance))
        instance_data = getattr(instance, "data", instance) if hasattr(instance, "data") else instance
        print(f">>> GT v8 (precompute): {instance_id}", flush=True)

        # ── Step 1: Inject gt_hook.py ──────────────────────────────────
        ok = True
        for i, chunk in enumerate(chunks):
            op = ">" if i == 0 else ">>"
            res = workspace.execute_command(f"echo -n '{chunk}' {op} /tmp/gt_hook.b64")
            if res.exit_code != 0:
                ok = False
                break

        if ok:
            res = workspace.execute_command(
                "base64 -d /tmp/gt_hook.b64 > /tmp/gt_hook.py && "
                "chmod +x /tmp/gt_hook.py && "
                "rm -f /tmp/gt_hook.b64 && "
                "echo GT_HOOK_READY"
            )
            if "GT_HOOK_READY" in (res.stdout or ""):
                print(f"  gt_hook.py injected: {instance_id}")
            else:
                print(f"  WARNING: injection uncertain: {instance_id}")
                return _original_evaluate(self, instance, workspace)
        else:
            print(f"  WARNING: injection FAILED: {instance_id}")
            return _original_evaluate(self, instance, workspace)

        # ── Step 2: Pre-compute GT context ─────────────────────────────
        repo_name = instance_data.get("repo", "").split("/")[-1] if isinstance(instance_data, dict) else ""
        problem = instance_data.get("problem_statement", "") if isinstance(instance_data, dict) else ""
        repo_path = instance_data.get("repo_path", f"/workspace/{repo_name}/") if isinstance(instance_data, dict) else "/workspace/"

        likely_files = _extract_likely_files(problem, repo_name)
        gt_context_parts = []

        for fpath in likely_files[:3]:  # Max 3 files
            full_path = os.path.join(repo_path, fpath) if not fpath.startswith("/") else fpath
            res = workspace.execute_command(
                f"python3 /tmp/gt_hook.py understand {full_path} "
                f"--root={repo_path} --quiet --max-lines=10 2>/dev/null"
            )
            output = (res.stdout or "").strip()
            if output and len(output) > 20 and "Error" not in output[:50]:
                gt_context_parts.append(f"## {fpath}\n{output}")
                print(f"  precomputed: {fpath} ({len(output)} chars)")

        if not gt_context_parts and repo_path:
            # Fallback: find key source files from the repo
            res = workspace.execute_command(
                f"find {repo_path} -name '*.py' -not -path '*/test*' "
                f"-not -path '*migration*' -not -path '*__pycache__*' "
                f"| head -20"
            )
            if res.stdout:
                source_files = [f.strip() for f in res.stdout.strip().split("\n") if f.strip()]
                # Try understanding the first few
                for sf in source_files[:2]:
                    res2 = workspace.execute_command(
                        f"python3 /tmp/gt_hook.py understand {sf} "
                        f"--root={repo_path} --quiet --max-lines=10 2>/dev/null"
                    )
                    output = (res2.stdout or "").strip()
                    if output and len(output) > 20 and "Error" not in output[:50]:
                        rel = sf.replace(repo_path, "")
                        gt_context_parts.append(f"## {rel}\n{output}")
                        print(f"  precomputed (fallback): {rel} ({len(output)} chars)")

        gt_analysis = ""
        if gt_context_parts:
            gt_analysis = (
                "\n<gt_codebase_context>\n"
                "The following cross-file analysis was pre-computed for key files. "
                "Use this to inform your fix — especially callers and test locations. "
                "You do NOT need to run any additional analysis commands.\n\n"
                + "\n\n".join(gt_context_parts)
                + "\n</gt_codebase_context>\n"
            )
            print(f"  GT context: {len(gt_analysis)} chars for {len(gt_context_parts)} files")
        else:
            print(f"  WARNING: no GT context precomputed for {instance_id}")

        # ── Step 3: Patch send_message to inject GT context ────────────
        _orig_send = None
        _Conversation = None
        try:
            from openhands.sdk import Conversation as _Conv  # type: ignore[import]
            _Conversation = _Conv
            _orig_send = _Conversation.send_message
            _injected = [False]

            def patched_send(conv_self, message, *args, **kwargs):
                if not _injected[0] and gt_analysis:
                    _injected[0] = True
                    message = gt_analysis + "\n" + message
                    print(f"  GT context injected into prompt for {instance_id}")
                return _orig_send(conv_self, message, *args, **kwargs)

            _Conversation.send_message = patched_send
        except Exception as e:
            print(f"  WARNING: Could not patch send_message: {e}")

        # ── Step 4: Run the task ───────────────────────────────────────
        try:
            result = _original_evaluate(self, instance, workspace)
        finally:
            if _Conversation is not None and _orig_send is not None:
                _Conversation.send_message = _orig_send
            _extract_hook_log(workspace, instance_id)

        return result

    SWEBenchEvaluation.evaluate_instance = patched_evaluate
    print("GT v8 (precompute): Patches applied")
    print(f"  evaluate_instance patched: {SWEBenchEvaluation.evaluate_instance is patched_evaluate}")
    print(flush=True)
    main()


def _extract_hook_log(workspace, instance_id: str) -> None:
    """Copy GT logs out of container."""
    out_dir = os.environ.get("GT_LOG_DIR", "/tmp/gt_logs")
    os.makedirs(out_dir, exist_ok=True)
    try:
        res = workspace.execute_command("cat /tmp/gt_hook_log.jsonl 2>/dev/null || echo ''")
        if res.stdout and res.stdout.strip():
            with open(os.path.join(out_dir, f"{instance_id}.jsonl"), "w") as fh:
                fh.write(res.stdout)
            print(f"  hook JSONL: {instance_id} ({len(res.stdout)} bytes)")
    except Exception:
        pass


if __name__ == "__main__":
    patch_and_run()
