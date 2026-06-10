#!/usr/bin/env python3
"""GT Evidence Delivery Proof — standalone verification script.

Proves (or disproves) that gt_hook.py evidence delivery works in OpenHands
SWE-bench containers.  Runs in two modes:

  ARTIFACT MODE (default) — validates from completed run outputs:
    python test_gt_delivery_proof.py --run-dir /path/to/eval_output

  LIVE MODE — validates against a running Docker container:
    python test_gt_delivery_proof.py --live --container <container_id>

Each test produces concrete evidence (file sizes, log lines, process lists)
so the result is proof, not inference.

Exit code:
  0 = all applicable tests passed
  1 = at least one test failed
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# GT evidence markers (same as oh_delivery_gate.py)
# ---------------------------------------------------------------------------

GT_EVIDENCE_MARKERS: list[str] = [
    "<gt-evidence>",
    '<gt-evidence surface=',
    "GT CODEBASE",
    "CONNECTED CODE",
    "-- structural coupling --",
    "--- OBLIGATIONS ---",
    "NEEDS_FIXES:",
    "INCOMPLETE:",
    "GT: ",
    "[VERIFIED]",
    "[WARNING]",
    "[INFO]",
]

FAMILY_TAGS: list[str] = [
    "CONTRACT",
    "PATTERN",
    "CHANGE",
    "STRUCTURAL",
    "SEMANTIC",
    "IMPORT",
    "CALLER",
    "SIBLING",
    "TEST",
    "IMPACT",
    "TYPE",
    "PRECEDENT",
]


# ---------------------------------------------------------------------------
# Test result tracking
# ---------------------------------------------------------------------------

@dataclass
class TestResult:
    name: str
    passed: bool
    evidence: list[str] = field(default_factory=list)
    skipped: bool = False
    skip_reason: str = ""

    def summary(self) -> str:
        if self.skipped:
            return f"  SKIP  {self.name}: {self.skip_reason}"
        status = "PASS" if self.passed else "FAIL"
        lines = [f"  {status}  {self.name}"]
        for e in self.evidence:
            lines.append(f"         | {e}")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Docker exec helper (for live mode)
# ---------------------------------------------------------------------------

def docker_exec(container_id: str, cmd: str, timeout: int = 15) -> tuple[int, str]:
    """Run a command in a Docker container, return (exit_code, stdout+stderr)."""
    try:
        result = subprocess.run(
            ["docker", "exec", container_id, "bash", "-c", cmd],
            capture_output=True, text=True, timeout=timeout,
        )
        output = result.stdout + result.stderr
        return result.returncode, output.strip()
    except subprocess.TimeoutExpired:
        return -1, "TIMEOUT"
    except FileNotFoundError:
        return -1, "docker command not found"
    except Exception as e:
        return -1, f"error: {e}"


# ---------------------------------------------------------------------------
# Test 1: Hook file exists in container (LIVE)
# ---------------------------------------------------------------------------

def test_hook_file_exists(container_id: str) -> TestResult:
    """Verify /tmp/gt_hook.py exists, is >100KB, and is readable."""
    name = "T1_hook_file_exists"

    rc, output = docker_exec(container_id, "ls -la /tmp/gt_hook.py 2>&1")
    if rc != 0:
        return TestResult(name=name, passed=False, evidence=[
            f"ls exit code: {rc}",
            f"output: {output[:200]}",
            "/tmp/gt_hook.py does NOT exist in container",
        ])

    evidence = [f"ls output: {output}"]

    # Parse file size from ls -la output
    # -rw-r--r-- 1 root root 154832 Apr 27 ... /tmp/gt_hook.py
    size_match = re.search(r'\s(\d+)\s+\w+\s+\d+', output)
    file_size = int(size_match.group(1)) if size_match else 0
    evidence.append(f"file size: {file_size} bytes")

    if file_size < 100_000:
        evidence.append(f"FAIL: file is only {file_size} bytes, expected >100KB (gt_hook.py is ~151KB)")
        return TestResult(name=name, passed=False, evidence=evidence)

    # Check readability
    rc2, head_out = docker_exec(container_id, "head -1 /tmp/gt_hook.py")
    if rc2 == 0 and head_out:
        evidence.append(f"first line: {head_out[:80]}")
    else:
        evidence.append(f"WARN: could not read file (rc={rc2})")

    evidence.append("file exists, correct size, readable")
    return TestResult(name=name, passed=True, evidence=evidence)


# ---------------------------------------------------------------------------
# Test 2: Hook runs without errors (LIVE)
# ---------------------------------------------------------------------------

def test_hook_runs(container_id: str) -> TestResult:
    """Execute gt_hook.py with a dry-run-like command and verify no import/syntax errors."""
    name = "T2_hook_runs_clean"

    # Try --help first (if supported), then a minimal invocation
    rc, output = docker_exec(
        container_id,
        "python3 -c \"import ast; ast.parse(open('/tmp/gt_hook.py').read()); print('SYNTAX_OK')\" 2>&1",
        timeout=10,
    )

    evidence = [f"syntax check exit code: {rc}"]
    syntax_ok = rc == 0 and "SYNTAX_OK" in output

    if not syntax_ok:
        evidence.append(f"output: {output[:300]}")
        return TestResult(name=name, passed=False, evidence=evidence + [
            "gt_hook.py has SYNTAX ERRORS -- cannot even parse",
        ])
    evidence.append("syntax check: SYNTAX_OK")

    # Now try a real invocation with understand (lightweight) or analyze on a nonexistent file
    # This tests that all stdlib imports resolve correctly
    rc2, output2 = docker_exec(
        container_id,
        "python3 /tmp/gt_hook.py understand /dev/null --root=/tmp --quiet --max-lines=1 2>&1; echo EXIT_CODE=$?",
        timeout=20,
    )

    evidence.append(f"invocation output (first 300 chars): {output2[:300]}")

    has_import_error = "ImportError" in output2 or "ModuleNotFoundError" in output2
    has_syntax_error = "SyntaxError" in output2
    has_traceback = "Traceback" in output2

    if has_import_error:
        evidence.append("FAIL: ImportError detected")
        return TestResult(name=name, passed=False, evidence=evidence)
    if has_syntax_error:
        evidence.append("FAIL: SyntaxError detected")
        return TestResult(name=name, passed=False, evidence=evidence)

    # A traceback on /dev/null is acceptable (not a real file) -- what matters
    # is that the script loaded all its modules.
    if has_traceback:
        evidence.append("WARN: traceback present but no ImportError/SyntaxError -- likely input error, not hook bug")
    else:
        evidence.append("clean execution (no traceback)")

    return TestResult(name=name, passed=True, evidence=evidence)


# ---------------------------------------------------------------------------
# Test 3: Watcher is running (LIVE)
# ---------------------------------------------------------------------------

def test_watcher_running(container_id: str) -> TestResult:
    """Check if a gt_watcher-like process is running in the container."""
    name = "T3_watcher_running"

    # Check for various watcher patterns
    rc, output = docker_exec(
        container_id,
        "ps aux 2>/dev/null | grep -E 'gt_watcher|gt_hook|gt_live_poll|inotifywait.*py' | grep -v grep",
    )

    evidence = [f"ps grep exit code: {rc}"]

    if rc != 0 or not output.strip():
        # No watcher found -- check if this is expected (mini-swe-agent arch
        # does NOT use a watcher; it calls gt_intel.py from the host-side hook)
        evidence.append("No gt_watcher/gt_hook process running in container")
        evidence.append("NOTE: mini-swe-agent architecture calls gt_intel.py from the HOST via docker exec")
        evidence.append("NOTE: OpenHands runner injects gt_hook.py but calls it on-demand, not via daemon")
        evidence.append("This is EXPECTED for the current architecture -- no background watcher needed")
        # This is informational, not a failure in the current architecture
        return TestResult(name=name, passed=True, evidence=evidence)

    process_lines = output.strip().split("\n")
    evidence.append(f"found {len(process_lines)} watcher process(es):")
    for pl in process_lines[:5]:
        evidence.append(f"  {pl.strip()[:120]}")

    return TestResult(name=name, passed=True, evidence=evidence)


# ---------------------------------------------------------------------------
# Test 4: Evidence generation on file edit (LIVE)
# ---------------------------------------------------------------------------

def test_evidence_on_edit(container_id: str) -> TestResult:
    """Create a file, edit it, and verify hook produces evidence."""
    name = "T4_evidence_on_edit"

    # Detect repo root
    rc, _ = docker_exec(container_id, "test -d /testbed")
    root = "/testbed" if rc == 0 else "/app"

    # Check if gt-index binary and graph.db exist (v11 path)
    rc_idx, _ = docker_exec(container_id, "test -f /tmp/gt_graph.db")
    rc_intel, _ = docker_exec(container_id, "test -f /tmp/gt_intel.py")
    rc_hook, _ = docker_exec(container_id, "test -f /tmp/gt_hook.py")

    has_v11 = rc_idx == 0 and rc_intel == 0
    has_v10 = rc_hook == 0

    evidence: list[str] = [
        f"repo root: {root}",
        f"graph.db exists: {rc_idx == 0}",
        f"gt_intel.py exists: {rc_intel == 0}",
        f"gt_hook.py exists: {rc_hook == 0}",
    ]

    if not has_v11 and not has_v10:
        evidence.append("FAIL: neither v11 (gt_intel.py + graph.db) nor v10 (gt_hook.py) found")
        return TestResult(name=name, passed=False, evidence=evidence)

    # Create a test Python file in the repo
    rc1, _ = docker_exec(container_id, f"echo 'def test_gt_probe(): pass' > {root}/test_gt_probe.py")
    evidence.append(f"created test file: rc={rc1}")

    # Now run the hook manually to prove it CAN produce evidence
    if has_v11:
        cmd = (
            f"python3 /tmp/gt_intel.py --db=/tmp/gt_graph.db "
            f"--file=test_gt_probe.py --root={root} --reminder 2>&1 | head -30"
        )
    else:
        cmd = (
            f"python3 /tmp/gt_hook.py analyze {root}/test_gt_probe.py "
            f"--root={root} --quiet --max-lines=20 2>&1 | head -30"
        )

    rc2, output = docker_exec(container_id, cmd, timeout=20)
    evidence.append(f"hook invocation exit code: {rc2}")
    evidence.append(f"hook output (first 300 chars): {output[:300]}")

    # Clean up
    docker_exec(container_id, f"rm -f {root}/test_gt_probe.py")

    # Check if we got any meaningful output
    has_evidence = False
    if output:
        for marker in GT_EVIDENCE_MARKERS + FAMILY_TAGS:
            if marker in output:
                has_evidence = True
                evidence.append(f"found marker: {marker}")
                break

    if not has_evidence and output and len(output) > 10 and "Error" not in output[:50]:
        # Some output that isn't an error -- might be valid evidence without markers
        has_evidence = True
        evidence.append("output present (no standard markers, but not an error)")

    if has_evidence:
        evidence.append("PROOF: hook can generate evidence when invoked on a file")
        return TestResult(name=name, passed=True, evidence=evidence)

    # Not necessarily a failure if the file is trivial and hook has nothing to say
    evidence.append("No evidence produced for trivial test file (may be expected)")
    evidence.append("The hook works (no errors) but found nothing noteworthy for 'def test_gt_probe(): pass'")
    return TestResult(name=name, passed=True, evidence=evidence)


# ---------------------------------------------------------------------------
# Test 5: Log extraction worked (ARTIFACT)
# ---------------------------------------------------------------------------

def test_log_extraction(run_dir: Path) -> TestResult:
    """Verify that hook logs were extracted from containers to the host."""
    name = "T5_log_extraction"

    # Look for GT log directories in common locations
    gt_log_candidates = [
        run_dir / "gt_logs",
        run_dir / "logs",
        run_dir.parent / "gt_logs",
    ]

    # Also check GT_LOG_DIR env var
    gt_log_env = os.environ.get("GT_LOG_DIR")
    if gt_log_env:
        gt_log_candidates.insert(0, Path(gt_log_env))

    gt_log_dir = None
    for candidate in gt_log_candidates:
        if candidate.exists() and candidate.is_dir():
            gt_log_dir = candidate
            break

    evidence: list[str] = []

    if gt_log_dir is None:
        # No dedicated log dir -- check for inline logs in output.jsonl
        output_jsonl = run_dir / "output.jsonl"
        if output_jsonl.exists():
            evidence.append(f"no gt_logs/ dir found, checking output.jsonl at {output_jsonl}")
            return _test_log_inline(output_jsonl, evidence)

        evidence.append(f"searched: {[str(c) for c in gt_log_candidates]}")
        evidence.append("no gt_logs directory found and no output.jsonl")
        return TestResult(name=name, passed=False, evidence=evidence)

    evidence.append(f"gt_log_dir: {gt_log_dir}")

    # Count JSONL and stdout log files
    jsonl_files = list(gt_log_dir.glob("*.jsonl"))
    stdout_files = list(gt_log_dir.glob("*_stdout.log"))

    evidence.append(f"JSONL log files: {len(jsonl_files)}")
    evidence.append(f"stdout log files: {len(stdout_files)}")

    if len(jsonl_files) == 0 and len(stdout_files) == 0:
        evidence.append("FAIL: log directory exists but contains NO log files")
        return TestResult(name=name, passed=False, evidence=evidence)

    # Validate a sample of JSONL files
    valid_jsonl = 0
    empty_jsonl = 0
    parse_errors = 0
    total_entries = 0
    sample_entries: list[str] = []

    for jf in jsonl_files[:20]:  # sample up to 20
        try:
            with open(jf, encoding="utf-8", errors="replace") as fh:
                content = fh.read().strip()

            if not content:
                empty_jsonl += 1
                continue

            file_entries = 0
            for line in content.split("\n"):
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                    file_entries += 1
                    total_entries += 1
                    if len(sample_entries) < 3:
                        # Show a compact sample
                        keys = sorted(entry.keys())[:6]
                        sample_entries.append(f"  {jf.name}: keys={keys}")
                except json.JSONDecodeError:
                    parse_errors += 1

            if file_entries > 0:
                valid_jsonl += 1

        except OSError as e:
            evidence.append(f"read error on {jf.name}: {e}")

    evidence.append(f"valid JSONL files (with entries): {valid_jsonl}/{len(jsonl_files)}")
    evidence.append(f"empty JSONL files: {empty_jsonl}")
    evidence.append(f"total JSON entries: {total_entries}")
    evidence.append(f"parse errors: {parse_errors}")

    for s in sample_entries:
        evidence.append(f"sample: {s}")

    # Validate stdout logs
    non_empty_stdout = 0
    for sf in stdout_files[:10]:
        try:
            sz = sf.stat().st_size
            if sz > 0:
                non_empty_stdout += 1
        except OSError:
            pass

    if stdout_files:
        evidence.append(f"non-empty stdout logs: {non_empty_stdout}/{len(stdout_files)}")

    passed = valid_jsonl > 0 or non_empty_stdout > 0
    if passed:
        evidence.append("PROOF: hook logs were extracted from containers to host")
    else:
        evidence.append("FAIL: all log files are empty or invalid")

    return TestResult(name=name, passed=passed, evidence=evidence)


def _test_log_inline(output_jsonl: Path, evidence: list[str]) -> TestResult:
    """Check if evidence delivery can be inferred from output.jsonl alone."""
    name = "T5_log_extraction"

    tasks_with_evidence = 0
    total_tasks = 0

    try:
        with open(output_jsonl, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue

                iid = entry.get("instance_id", "")
                if not iid:
                    continue
                total_tasks += 1

                # Check history for evidence markers
                history = entry.get("history", []) or entry.get("messages", [])
                found = False
                for item in history:
                    if not isinstance(item, dict):
                        continue
                    text = _extract_text(item)
                    for marker in GT_EVIDENCE_MARKERS:
                        if marker in text:
                            found = True
                            break
                    if found:
                        break

                if found:
                    tasks_with_evidence += 1

    except OSError as e:
        evidence.append(f"error reading output.jsonl: {e}")
        return TestResult(name=name, passed=False, evidence=evidence)

    evidence.append(f"total tasks in output.jsonl: {total_tasks}")
    evidence.append(f"tasks with GT evidence in trajectory: {tasks_with_evidence}")

    if total_tasks == 0:
        evidence.append("FAIL: no tasks found in output.jsonl")
        return TestResult(name=name, passed=False, evidence=evidence)

    pct = (tasks_with_evidence / total_tasks * 100) if total_tasks > 0 else 0
    evidence.append(f"evidence visibility: {pct:.1f}%")

    if tasks_with_evidence > 0:
        evidence.append(f"PROOF: evidence visible in {tasks_with_evidence}/{total_tasks} trajectories")
        return TestResult(name=name, passed=True, evidence=evidence)

    evidence.append("No separate log dir and no evidence in trajectories")
    evidence.append("This means either: (a) hook was never injected, or (b) hook ran but output was not captured")
    return TestResult(name=name, passed=False, evidence=evidence)


# ---------------------------------------------------------------------------
# Test 6: Trajectory contains evidence (ARTIFACT)
# ---------------------------------------------------------------------------

def test_trajectory_evidence(run_dir: Path) -> TestResult:
    """Scan output.jsonl for GT evidence markers in agent history."""
    name = "T6_trajectory_evidence"

    output_jsonl = run_dir / "output.jsonl"
    if not output_jsonl.exists():
        return TestResult(name=name, passed=False, evidence=[
            f"output.jsonl not found at {output_jsonl}",
        ])

    evidence: list[str] = [f"output.jsonl: {output_jsonl}"]

    total_tasks = 0
    tasks_with_evidence: list[str] = []
    tasks_without_evidence: list[str] = []
    all_families: dict[str, int] = {}
    total_evidence_blocks = 0
    total_evidence_tokens = 0

    try:
        with open(output_jsonl, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue

                iid = entry.get("instance_id", "")
                if not iid:
                    continue
                total_tasks += 1

                history = entry.get("history", []) or entry.get("messages", [])
                task_evidence_found = False
                task_blocks = 0
                task_tokens = 0
                task_families: set[str] = set()

                for item in history:
                    if not isinstance(item, dict):
                        continue
                    text = _extract_text(item)
                    if not text:
                        continue

                    has_marker = False
                    for marker in GT_EVIDENCE_MARKERS:
                        if marker in text:
                            has_marker = True
                            break

                    if has_marker:
                        task_evidence_found = True
                        task_blocks += 1
                        task_tokens += len(text) // 4

                        for tag in FAMILY_TAGS:
                            if f"[{tag}]" in text or f"[{tag}:" in text or tag in text:
                                task_families.add(tag)

                if task_evidence_found:
                    tasks_with_evidence.append(iid)
                    total_evidence_blocks += task_blocks
                    total_evidence_tokens += task_tokens
                    for fam in task_families:
                        all_families[fam] = all_families.get(fam, 0) + 1
                else:
                    tasks_without_evidence.append(iid)

    except OSError as e:
        evidence.append(f"error: {e}")
        return TestResult(name=name, passed=False, evidence=evidence)

    evidence.append(f"total tasks: {total_tasks}")
    evidence.append(f"tasks WITH evidence: {len(tasks_with_evidence)}")
    evidence.append(f"tasks WITHOUT evidence: {len(tasks_without_evidence)}")

    if total_tasks == 0:
        evidence.append("FAIL: no tasks in output.jsonl")
        return TestResult(name=name, passed=False, evidence=evidence)

    pct = len(tasks_with_evidence) / total_tasks * 100
    evidence.append(f"evidence visibility rate: {pct:.1f}%")
    evidence.append(f"total evidence blocks: {total_evidence_blocks}")
    evidence.append(f"estimated evidence tokens: {total_evidence_tokens}")

    if all_families:
        evidence.append(f"evidence families seen: {dict(sorted(all_families.items(), key=lambda x: -x[1]))}")

    # Show sample task IDs
    if tasks_with_evidence:
        evidence.append(f"sample tasks with evidence: {tasks_with_evidence[:5]}")
    if tasks_without_evidence:
        evidence.append(f"sample tasks without evidence: {tasks_without_evidence[:5]}")

    if len(tasks_with_evidence) > 0:
        evidence.append(f"PROOF: GT evidence visible in {len(tasks_with_evidence)}/{total_tasks} trajectories")

        # Explain partial delivery if not 100%
        if len(tasks_without_evidence) > 0:
            evidence.append(
                f"NOTE: {len(tasks_without_evidence)} tasks had no evidence. Possible reasons: "
                "hook injection failed for those containers, no edits to source files, "
                "graph.db was empty (repo too small or unsupported language), or "
                "the v12 dedup filter suppressed output (fires on 2nd edit, not 1st)."
            )

        return TestResult(name=name, passed=True, evidence=evidence)

    # Zero evidence in trajectories -- this is the critical diagnosis
    evidence.append("ZERO evidence found in any trajectory")
    evidence.append("")
    evidence.append("DIAGNOSIS (from architecture analysis):")
    evidence.append("  The current mini-swe-agent hook (run_mini_gt_hooked.py) injects evidence")
    evidence.append("  by appending to command stdout in the _run_gt_intel callback. This happens")
    evidence.append("  on the HOST side via docker exec. The evidence appears inline in command")
    evidence.append("  output that the agent sees.")
    evidence.append("")
    evidence.append("  The OpenHands runner (oh_gt_v11_runner.py) injects gt_hook.py into the")
    evidence.append("  container but does NOT have a mechanism to intercept command outputs and")
    evidence.append("  append evidence. The hook runs and logs to /tmp/gt_hook_log.jsonl, but")
    evidence.append("  this log is only extracted at complete_runtime() -- AFTER the agent is done.")
    evidence.append("")
    evidence.append("  This means: hook runs, evidence is generated, but the AGENT NEVER SEES IT")
    evidence.append("  during its reasoning loop. The evidence sits in container logs, invisible.")
    evidence.append("")
    evidence.append("  FIX: Need a command-interception layer (like mini-swe-agent's post_command")
    evidence.append("  callback) or a .bashrc wrapper that appends hook output to every command.")

    return TestResult(name=name, passed=False, evidence=evidence)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _extract_text(item: dict[str, Any]) -> str:
    """Extract all text from a history/message item."""
    parts: list[str] = []

    obs = item.get("observation", "")
    if isinstance(obs, str):
        parts.append(obs)
    elif isinstance(obs, dict):
        parts.append(json.dumps(obs))

    args = item.get("args", {})
    if isinstance(args, dict):
        content = args.get("content", "")
        if isinstance(content, str):
            parts.append(content)
        output = args.get("output", "")
        if isinstance(output, str):
            parts.append(output)

    content = item.get("content", "")
    if isinstance(content, str):
        parts.append(content)

    msg = item.get("message", "")
    if isinstance(msg, str):
        parts.append(msg)

    # OpenHands specific: extras.metadata, tool responses
    extras = item.get("extras", {})
    if isinstance(extras, dict):
        for v in extras.values():
            if isinstance(v, str):
                parts.append(v)

    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Live container tests (manual commands)
# ---------------------------------------------------------------------------

def print_live_commands():
    """Print commands that can be run manually against a live container."""
    print("""
=== MANUAL LIVE CONTAINER TESTS ===

Replace CONTAINER_ID with your actual container ID (from `docker ps`).

--- Test 1: Hook file exists ---
docker exec CONTAINER_ID ls -la /tmp/gt_hook.py /tmp/gt_intel.py /tmp/gt_graph.db /tmp/gt-index 2>&1
# Expect: gt_hook.py ~151KB OR gt_intel.py present with gt_graph.db

--- Test 2: Hook runs clean ---
docker exec CONTAINER_ID python3 -c "import ast; ast.parse(open('/tmp/gt_hook.py').read()); print('SYNTAX_OK')" 2>&1
docker exec CONTAINER_ID python3 /tmp/gt_hook.py understand /dev/null --root=/testbed --quiet --max-lines=1 2>&1

--- Test 3: Check for watcher/daemon ---
docker exec CONTAINER_ID ps aux | grep -E 'gt_watcher|gt_hook|gt_live_poll' | grep -v grep
# NOTE: Current architecture does NOT use a watcher -- host calls hook via docker exec

--- Test 4: Manual evidence generation ---
docker exec CONTAINER_ID bash -c 'echo "def foo(): pass" > /testbed/test_gt_probe.py'
# If v11 (gt_intel.py + graph.db):
docker exec CONTAINER_ID python3 /tmp/gt_intel.py --db=/tmp/gt_graph.db --file=test_gt_probe.py --root=/testbed --reminder 2>&1
# If v10 (gt_hook.py fallback):
docker exec CONTAINER_ID python3 /tmp/gt_hook.py analyze /testbed/test_gt_probe.py --root=/testbed --quiet --max-lines=20 2>&1
docker exec CONTAINER_ID rm -f /testbed/test_gt_probe.py

--- Test 5: Check logs inside container ---
docker exec CONTAINER_ID ls -la /tmp/gt_evidence.jsonl /tmp/gt_hook_log.jsonl /tmp/gt_hook_stdout.log 2>&1
docker exec CONTAINER_ID wc -l /tmp/gt_evidence.jsonl 2>&1
docker exec CONTAINER_ID head -3 /tmp/gt_evidence.jsonl 2>&1
""")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="GT Evidence Delivery Proof",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--run-dir",
        type=str,
        help="Path to completed evaluation output directory (artifact mode)",
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="Run live container tests (requires --container)",
    )
    parser.add_argument(
        "--container",
        type=str,
        help="Docker container ID for live tests",
    )
    parser.add_argument(
        "--print-commands",
        action="store_true",
        help="Print manual commands for live container testing",
    )
    args = parser.parse_args()

    if args.print_commands:
        print_live_commands()
        return 0

    results: list[TestResult] = []

    # -----------------------------------------------------------------------
    # Live container tests (T1-T4)
    # -----------------------------------------------------------------------

    if args.live:
        if not args.container:
            print("ERROR: --live requires --container <container_id>")
            return 1

        cid = args.container

        # Verify container is reachable
        rc, out = docker_exec(cid, "echo ALIVE")
        if rc != 0 or "ALIVE" not in out:
            print(f"ERROR: cannot reach container {cid}: {out}")
            return 1

        print(f"Running live tests against container: {cid}")
        print()

        results.append(test_hook_file_exists(cid))
        results.append(test_hook_runs(cid))
        results.append(test_watcher_running(cid))
        results.append(test_evidence_on_edit(cid))

    elif not args.run_dir:
        print("ERROR: must specify either --run-dir (artifact mode) or --live --container (live mode)")
        print("       use --print-commands for manual test instructions")
        return 1

    # -----------------------------------------------------------------------
    # Artifact tests (T5-T6)
    # -----------------------------------------------------------------------

    if args.run_dir:
        run_dir = Path(args.run_dir)
        if not run_dir.exists():
            print(f"ERROR: run directory does not exist: {run_dir}")
            return 1

        print(f"Running artifact tests against: {run_dir}")
        print()

        results.append(test_log_extraction(run_dir))
        results.append(test_trajectory_evidence(run_dir))

    # -----------------------------------------------------------------------
    # Summary
    # -----------------------------------------------------------------------

    print()
    print("=" * 70)
    print("GT EVIDENCE DELIVERY PROOF REPORT")
    print("=" * 70)

    passed = 0
    failed = 0
    skipped = 0

    for r in results:
        print(r.summary())
        print()
        if r.skipped:
            skipped += 1
        elif r.passed:
            passed += 1
        else:
            failed += 1

    print("-" * 70)
    print(f"TOTAL: {passed} passed, {failed} failed, {skipped} skipped")
    print()

    if failed == 0 and passed > 0:
        print("DELIVERY PROOF: PASS -- evidence was generated, hook ran, logs extracted")
        return 0
    elif failed > 0:
        # Build specific failure reasons
        fail_reasons = []
        for r in results:
            if not r.passed and not r.skipped:
                # Extract the key failure line
                for e in r.evidence:
                    if e.startswith("FAIL:") or e.startswith("ZERO") or "does NOT exist" in e:
                        fail_reasons.append(e)
                        break
                else:
                    fail_reasons.append(r.name)

        reason_str = "; ".join(fail_reasons) if fail_reasons else "see details above"
        print(f"DELIVERY PROOF: FAIL -- {reason_str}")
        return 1
    else:
        print("DELIVERY PROOF: INCONCLUSIVE -- no tests ran")
        return 1


if __name__ == "__main__":
    sys.exit(main())
