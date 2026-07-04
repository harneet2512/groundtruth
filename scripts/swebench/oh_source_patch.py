#!/usr/bin/env python3
"""Source-level patcher for OpenHands v0.44.0 run_infer.py.

Injects GT evidence hook into the evaluation pipeline at the source level,
so patches survive mp.Pool(num_workers) fork boundaries (unlike monkey-patches).

Three injection blocks:
  1. initialize_runtime() -- copy gt_hook.py into container + start watcher
  2. complete_runtime()   -- extract hook logs from container
  3. process_instance()   -- (no-op anchor for future observation injection)

Usage:
    python oh_source_patch.py --source /path/to/run_infer.py --output /path/to/patched.py
    python oh_source_patch.py --source /path/to/run_infer.py --in-place

Environment variables (read at runtime by the patched code):
    GT_HOOK_PATH  -- path to gt_hook.py on the host (default: ~/gt_hook.py)
    GT_LOG_DIR    -- directory for extracted logs (default: /tmp/gt_logs)
"""

from __future__ import annotations

import argparse
import ast
import re
import shutil
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Patch markers -- used for idempotency checks
# ---------------------------------------------------------------------------

MARKER_INIT = "# >>> GT_HOOK_INJECT: initialize_runtime"
MARKER_COMPLETE = "# >>> GT_HOOK_INJECT: complete_runtime"
MARKER_WATCHER = "# >>> GT_HOOK_INJECT: watcher"

# ---------------------------------------------------------------------------
# Block 1: Hook injection + watcher start (goes into initialize_runtime)
# ---------------------------------------------------------------------------

INIT_BLOCK = r'''
    # >>> GT_HOOK_INJECT: initialize_runtime
    # Inject gt_hook.py into container and start file-change watcher.
    import base64 as _gt_b64
    import os as _gt_os
    _gt_hook_path = _gt_os.environ.get("GT_HOOK_PATH", "")
    if _gt_hook_path and _gt_os.path.isfile(_gt_hook_path):
        try:
            with open(_gt_hook_path, "rb") as _gt_fh:
                _gt_bytes = _gt_fh.read()
            _gt_encoded = _gt_b64.b64encode(_gt_bytes).decode("ascii")
            _GT_CHUNK = 8000
            _gt_chunks = [_gt_encoded[i:i+_GT_CHUNK] for i in range(0, len(_gt_encoded), _GT_CHUNK)]
            _gt_ok = True
            for _gt_i, _gt_chunk in enumerate(_gt_chunks):
                _gt_op = ">" if _gt_i == 0 else ">>"
                _gt_obs = runtime.run_action(
                    CmdRunAction(command=f"echo -n '{_gt_chunk}' {_gt_op} /tmp/gt_hook.b64")
                )
                if not isinstance(_gt_obs, CmdOutputObservation) or _gt_obs.exit_code != 0:
                    _gt_ok = False
                    break
            if _gt_ok:
                _gt_obs = runtime.run_action(CmdRunAction(
                    command=(
                        "base64 -d /tmp/gt_hook.b64 > /tmp/gt_hook.py && "
                        "chmod +x /tmp/gt_hook.py && "
                        "rm -f /tmp/gt_hook.b64 && "
                        "echo GT_HOOK_READY"
                    )
                ))
                if "GT_HOOK_READY" in getattr(_gt_obs, "content", ""):
                    logger.info(f"gt_hook.py injected into container ({len(_gt_bytes)} bytes)")
                    # >>> GT_HOOK_INJECT: watcher
                    # Start background watcher that monitors .py file changes and runs gt_hook.py
                    _gt_watcher_script = r"""
import hashlib, json, os, subprocess, sys, time

WATCH_ROOT = "/testbed"
HOOK_CMD = "/tmp/gt_hook.py"
POLL_INTERVAL = 2.0
LOG_FILE = "/tmp/gt_hook_stdout.log"
JSONL_FILE = "/tmp/gt_hook_log.jsonl"
MARKER_FILE = "/tmp/gt_watcher.pid"

def md5_of_file(path):
    try:
        h = hashlib.md5()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(8192), b""):
                h.update(chunk)
        return h.hexdigest()
    except Exception:
        return ""

def scan_py_files(root):
    result = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in (".git", "__pycache__", ".tox", "node_modules", ".mypy_cache")]
        for fn in filenames:
            if fn.endswith(".py"):
                fp = os.path.join(dirpath, fn)
                result[fp] = md5_of_file(fp)
    return result

def run_hook(changed_files, root):
    for fp in changed_files[:3]:
        try:
            cmd = [sys.executable, HOOK_CMD, "--root=" + root, "--file=" + fp, "--quiet", "--max-items=5"]
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            with open(LOG_FILE, "a") as lf:
                lf.write(f"--- {fp} ---\n")
                if proc.stdout:
                    lf.write(proc.stdout)
                if proc.stderr:
                    lf.write(proc.stderr)
                lf.write("\n")
            entry = {
                "ts": time.time(),
                "file": fp,
                "exit_code": proc.returncode,
                "stdout_len": len(proc.stdout or ""),
                "stderr_len": len(proc.stderr or ""),
            }
            with open(JSONL_FILE, "a") as jf:
                jf.write(json.dumps(entry) + "\n")
        except Exception as exc:
            with open(LOG_FILE, "a") as lf:
                lf.write(f"ERROR running hook on {fp}: {exc}\n")

def main():
    with open(MARKER_FILE, "w") as pf:
        pf.write(str(os.getpid()))
    baseline = scan_py_files(WATCH_ROOT)
    with open(LOG_FILE, "a") as lf:
        lf.write(f"GT watcher started: {len(baseline)} .py files tracked\n")
    while True:
        time.sleep(POLL_INTERVAL)
        current = scan_py_files(WATCH_ROOT)
        changed = []
        for fp, h in current.items():
            if fp not in baseline or baseline[fp] != h:
                changed.append(fp)
        new_files = [fp for fp in current if fp not in baseline]
        changed.extend(new_files)
        if changed:
            changed = list(set(changed))
            with open(LOG_FILE, "a") as lf:
                lf.write(f"Changes detected: {changed}\n")
            run_hook(changed, WATCH_ROOT)
            baseline = current

if __name__ == "__main__":
    main()
"""
                    _gt_watcher_b64 = _gt_b64.b64encode(_gt_watcher_script.encode()).decode("ascii")
                    _gt_w_chunks = [_gt_watcher_b64[i:i+_GT_CHUNK] for i in range(0, len(_gt_watcher_b64), _GT_CHUNK)]
                    _gt_w_ok = True
                    for _gt_wi, _gt_wc in enumerate(_gt_w_chunks):
                        _gt_wop = ">" if _gt_wi == 0 else ">>"
                        _gt_wobs = runtime.run_action(
                            CmdRunAction(command=f"echo -n '{_gt_wc}' {_gt_wop} /tmp/gt_watcher.b64")
                        )
                        if not isinstance(_gt_wobs, CmdOutputObservation) or _gt_wobs.exit_code != 0:
                            _gt_w_ok = False
                            break
                    if _gt_w_ok:
                        runtime.run_action(CmdRunAction(
                            command=(
                                "base64 -d /tmp/gt_watcher.b64 > /tmp/gt_watcher.py && "
                                "chmod +x /tmp/gt_watcher.py && "
                                "rm -f /tmp/gt_watcher.b64 && "
                                "nohup python3 /tmp/gt_watcher.py > /dev/null 2>&1 & "
                                "echo GT_WATCHER_PID=$!"
                            )
                        ))
                        logger.info("gt_watcher.py started in background")
                    else:
                        logger.warning("gt_watcher.py injection failed")
                else:
                    logger.warning("gt_hook.py injection uncertain")
            else:
                logger.warning("gt_hook.py injection failed (chunk write error)")
        except Exception as _gt_exc:
            logger.warning(f"gt_hook injection error: {_gt_exc}")
    # <<< GT_HOOK_INJECT: initialize_runtime
'''

# ---------------------------------------------------------------------------
# Block 2: Log extraction (goes into complete_runtime)
# ---------------------------------------------------------------------------

COMPLETE_BLOCK = r'''
    # >>> GT_HOOK_INJECT: complete_runtime
    # Extract GT hook logs from container before teardown.
    import os as _gt_os2
    _gt_log_dir = _gt_os2.environ.get("GT_LOG_DIR", "")
    if _gt_log_dir:
        _gt_os2.makedirs(_gt_log_dir, exist_ok=True)
        _gt_iid = instance["instance_id"]
        try:
            # Kill watcher first so logs are flushed
            runtime.run_action(CmdRunAction(
                command="kill $(cat /tmp/gt_watcher.pid 2>/dev/null) 2>/dev/null; sleep 0.5; true"
            ))
            # Extract JSONL log
            _gt_log_obs = runtime.run_action(
                CmdRunAction(command="cat /tmp/gt_hook_log.jsonl 2>/dev/null || true")
            )
            _gt_log_content = getattr(_gt_log_obs, "content", "")
            if _gt_log_content and _gt_log_content.strip() and _gt_log_content.strip() != "true":
                with open(_gt_os2.path.join(_gt_log_dir, f"{_gt_iid}.jsonl"), "w") as _gt_fh:
                    _gt_fh.write(_gt_log_content)
                logger.info(f"gt hook log extracted: {_gt_iid} ({len(_gt_log_content)} bytes)")
            # Extract stdout log
            _gt_stdout_obs = runtime.run_action(
                CmdRunAction(command="cat /tmp/gt_hook_stdout.log 2>/dev/null || true")
            )
            _gt_stdout_content = getattr(_gt_stdout_obs, "content", "")
            if _gt_stdout_content and _gt_stdout_content.strip() and _gt_stdout_content.strip() != "true":
                with open(_gt_os2.path.join(_gt_log_dir, f"{_gt_iid}_stdout.log"), "w") as _gt_fh:
                    _gt_fh.write(_gt_stdout_content)
                logger.info(f"gt stdout log extracted: {_gt_iid} ({len(_gt_stdout_content)} bytes)")
        except Exception as _gt_exc:
            logger.warning(f"gt log extraction error for {_gt_iid}: {_gt_exc}")
    # <<< GT_HOOK_INJECT: complete_runtime
'''

# ---------------------------------------------------------------------------
# Patcher logic
# ---------------------------------------------------------------------------


def find_function_end_marker(source: str, func_name: str) -> tuple[int, str] | None:
    """Find the insertion point before the END marker in a function.

    Looks for the pattern:
        # ==================== END ... ====================
    or the last statement before return in the function.

    Returns (line_number, indent_string) or None.
    """
    lines = source.splitlines(keepends=True)
    in_func = False
    func_indent = 0
    last_code_line = -1
    last_indent = ""

    for i, line in enumerate(lines):
        stripped = line.lstrip()

        # Detect function definition
        if stripped.startswith(f"def {func_name}("):
            in_func = True
            func_indent = len(line) - len(stripped)
            continue

        if not in_func:
            continue

        # Detect end of function (dedent to same or less level)
        if stripped and not stripped.startswith("#") and not stripped.startswith("'''") and not stripped.startswith('"""'):
            current_indent = len(line) - len(stripped)
            if current_indent <= func_indent and not stripped.startswith("def ") and i > 0:
                # We've left the function
                break

        # Look for END marker
        if "END" in stripped and "====" in stripped:
            indent = line[:len(line) - len(stripped)]
            return (i, indent)

        # Track last code line inside the function body
        if stripped and not stripped.startswith("#"):
            current_indent = len(line) - len(stripped)
            if current_indent > func_indent:
                last_code_line = i
                last_indent = line[:current_indent]

    # No END marker found -- insert before last line of function
    if last_code_line > 0:
        return (last_code_line, last_indent)

    return None


def find_return_in_function(source: str, func_name: str) -> tuple[int, str] | None:
    """Find the last return statement in a function.

    Returns (line_number, indent_string) or None.
    """
    lines = source.splitlines(keepends=True)
    in_func = False
    func_indent = 0
    last_return = -1
    last_return_indent = ""

    for i, line in enumerate(lines):
        stripped = line.lstrip()

        if stripped.startswith(f"def {func_name}("):
            in_func = True
            func_indent = len(line) - len(stripped)
            last_return = -1
            continue

        if not in_func:
            continue

        current_indent = len(line) - len(stripped) if stripped else 999
        if stripped and current_indent <= func_indent and i > 0:
            break

        if stripped.startswith("return ") or stripped == "return":
            last_return = i
            last_return_indent = line[:len(line) - len(stripped)]

    if last_return > 0:
        return (last_return, last_return_indent)
    return None


def reindent_block(block: str, target_indent: str) -> str:
    """Reindent a code block so top-level statements get target_indent."""
    raw_lines = block.splitlines()
    base_indent_len = 0
    for line in raw_lines:
        if line.strip():
            base_indent_len = len(line) - len(line.lstrip())
            break
    result = []
    in_triple_quote = False
    triple_char = ""
    for line in raw_lines:
        if not line.strip() and not in_triple_quote:
            result.append("")
            continue
        if not in_triple_quote:
            stripped = line.lstrip()
            for tq in ['r"""', "r'''", '"""', "'''"]:
                if tq in stripped:
                    count = stripped.count(tq[-3:])
                    if count % 2 == 1:
                        in_triple_quote = True
                        triple_char = tq[-3:]
                    break
            current_indent = len(line) - len(line.lstrip())
            relative = max(0, current_indent - base_indent_len)
            result.append(target_indent + " " * relative + line.lstrip())
        else:
            result.append(line)
            if triple_char in line:
                count = line.count(triple_char)
                if count % 2 == 1:
                    in_triple_quote = False
    return "\n".join(result)


def ensure_imports(source: str) -> str:
    """Ensure CmdRunAction and CmdOutputObservation imports are present."""
    needed = [
        ("CmdRunAction", "from openhands.events.action import CmdRunAction"),
        ("CmdOutputObservation", "from openhands.events.observation import CmdOutputObservation"),
    ]
    for name, import_line in needed:
        if re.search(rf'\bimport\b.*\b{name}\b', source):
            continue
        lines = source.splitlines(keepends=True)
        last_import = 0
        for i, line in enumerate(lines):
            if line.strip().startswith("import ") or line.strip().startswith("from "):
                last_import = i
        lines.insert(last_import + 1, import_line + "\n")
        source = "".join(lines)
        print(f"ADDED import: {name}")
    return source


def apply_patch(source: str) -> tuple[str, list[str]]:
    """Apply GT hook patches to run_infer.py source.

    Returns (patched_source, list_of_applied_patches).
    """
    applied = []
    lines = source.splitlines(keepends=True)

    # Check idempotency
    flat = source
    if MARKER_INIT in flat:
        print("SKIP: initialize_runtime patch already applied")
    else:
        # Find insertion point in initialize_runtime
        # Strategy: find the END marker or last statement before return
        pos = find_function_end_marker(source, "initialize_runtime")
        if pos is None:
            pos = find_return_in_function(source, "initialize_runtime")

        if pos is not None:
            line_num, indent = pos
            # Reindent the block to match target indent level.
            # The block has 4-space base indent; adjust to match the target.
            block = reindent_block(INIT_BLOCK, indent)
            lines.insert(line_num, block + "\n")
            applied.append("initialize_runtime")
            print(f"PATCHED: initialize_runtime (inserted at line {line_num + 1})")
        else:
            print("ERROR: Could not find insertion point in initialize_runtime()")

    # Rebuild source after first patch
    source = "".join(lines)
    lines = source.splitlines(keepends=True)

    if MARKER_COMPLETE in flat:
        print("SKIP: complete_runtime patch already applied")
    else:
        pos = find_function_end_marker(source, "complete_runtime")
        if pos is None:
            pos = find_return_in_function(source, "complete_runtime")

        if pos is not None:
            line_num, indent = pos
            block = reindent_block(COMPLETE_BLOCK, indent)
            lines.insert(line_num, block + "\n")
            applied.append("complete_runtime")
            print(f"PATCHED: complete_runtime (inserted at line {line_num + 1})")
        else:
            print("ERROR: Could not find insertion point in complete_runtime()")

    # Ensure required imports are present at top of file
    source = "".join(lines)
    if applied:
        source = ensure_imports(source)

    return source, applied


def validate_syntax(source: str, path: str = "<patched>") -> bool:
    """Check that patched source is valid Python."""
    try:
        ast.parse(source)
        print(f"SYNTAX OK: {path}")
        return True
    except SyntaxError as e:
        print(f"SYNTAX ERROR in {path}: {e}")
        return False


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Patch OpenHands run_infer.py with GT hook injection"
    )
    parser.add_argument(
        "--source", required=True,
        help="Path to original run_infer.py",
    )
    parser.add_argument(
        "--output",
        help="Path for patched output (default: stdout)",
    )
    parser.add_argument(
        "--in-place", action="store_true",
        help="Overwrite the source file in place",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Show what would be patched without writing",
    )
    args = parser.parse_args()

    source_path = Path(args.source)
    if not source_path.is_file():
        print(f"ERROR: Source file not found: {source_path}")
        return 1

    source = source_path.read_text(encoding="utf-8")
    print(f"Read {len(source)} bytes from {source_path}")

    # Verify it looks like run_infer.py
    if "initialize_runtime" not in source or "complete_runtime" not in source:
        print("ERROR: Source does not look like run_infer.py (missing expected functions)")
        return 1

    patched, applied = apply_patch(source)

    if not applied:
        print("No patches applied (already patched or no insertion points found)")
        return 0

    if not validate_syntax(patched, str(source_path)):
        print("ERROR: Patched source has syntax errors, aborting")
        return 1

    if args.dry_run:
        print(f"\nDRY RUN: Would apply {len(applied)} patches: {', '.join(applied)}")
        return 0

    if args.in_place:
        # Backup first
        backup = source_path.with_suffix(".py.gt_backup")
        shutil.copy2(source_path, backup)
        print(f"Backup saved to {backup}")
        source_path.write_text(patched, encoding="utf-8")
        print(f"Patched in place: {source_path}")
    elif args.output:
        out_path = Path(args.output)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(patched, encoding="utf-8")
        print(f"Patched file written to: {out_path}")
    else:
        sys.stdout.write(patched)

    print(f"\nApplied {len(applied)} patches: {', '.join(applied)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
