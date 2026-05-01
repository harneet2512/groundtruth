"""Repo-native test command selection and execution for GT validation tools."""

from __future__ import annotations

import re
import subprocess
import time
from typing import Any

from groundtruth.runtime.repo_adapters import (
    detect_repo_profile,
    is_test_file,
    select_repo_test_command,
)


def select_test_command(
    repo_root: str,
    *,
    mode: str = "contract",
    plan: dict[str, Any] | None = None,
    changed_files: list[str] | None = None,
    log_dir: str | None = None,
    task_id: str = "unknown",
) -> dict[str, Any]:
    """Select a deterministic repo-native test command without executing it."""
    plan = plan or {}
    changed_files = changed_files or []
    contract_tests = []
    for item in plan.get("expected_side_files", []):
        if isinstance(item, dict):
            path = str(item.get("path") or item.get("pattern") or "")
        else:
            path = str(item)
        if path and is_test_file(path):
            contract_tests.append(path)

    if mode == "contract" and contract_tests:
        return _emit(
            {
                "command": ["pytest", *contract_tests],
                "reason": "contract_tests",
                "mode": mode,
                "selected_contract_files": contract_tests,
            },
            log_dir,
            task_id,
        )

    related_tests = [path for path in changed_files if is_test_file(path)]
    if mode in {"changed", "cluster"} and related_tests:
        return _emit(
            {
                "command": ["pytest", *related_tests],
                "reason": "changed_tests",
                "mode": mode,
                "selected_contract_files": [],
            },
            log_dir,
            task_id,
        )

    command, reason = select_repo_test_command(repo_root)
    profile = detect_repo_profile(repo_root)
    return _emit(
        {
            "command": command,
            "reason": reason,
            "mode": mode,
            "selected_contract_files": [],
            "repo_profile": {
                "languages": list(profile.languages),
                "manifests": list(profile.manifests),
                "adapter_names": list(profile.adapter_names),
            },
        },
        log_dir,
        task_id,
    )


def execute_test_command(
    repo_root: str,
    command: list[str],
    *,
    timeout_seconds: int = 120,
    max_output_chars: int = 4000,
    mode: str = "contract",
    selected_contract_files: list[str] | None = None,
    log_dir: str | None = None,
    task_id: str = "unknown",
) -> dict[str, Any]:
    """Execute a previously-selected test command and return pass/fail counts.

    Deterministic — no LLM, no network. Subprocess only. The runner parses
    standard pytest / go test / cargo / npm / tox output for pass/fail
    counts. Exit code 0 with no detected failures means ``all_passed``.
    """
    if not command:
        return _emit_exec(
            {
                "executed": False,
                "command": [],
                "mode": mode,
                "selected_contract_files": selected_contract_files or [],
                "reason": "no_command",
                "exit_code": None,
                "duration_ms": 0,
                "passed": 0,
                "failed": 0,
                "errored": 0,
                "failing_test_names": [],
                "all_passed": False,
                "timed_out": False,
                "stdout_tail": "",
                "stderr_tail": "",
            },
            log_dir,
            task_id,
        )

    start = time.perf_counter()
    try:
        proc = subprocess.run(
            command,
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired:
        duration_ms = int((time.perf_counter() - start) * 1000)
        return _emit_exec(
            {
                "executed": True,
                "command": command,
                "mode": mode,
                "selected_contract_files": selected_contract_files or [],
                "reason": "timeout",
                "exit_code": None,
                "duration_ms": duration_ms,
                "passed": 0,
                "failed": 0,
                "errored": 0,
                "failing_test_names": [],
                "all_passed": False,
                "timed_out": True,
                "stdout_tail": "",
                "stderr_tail": "",
            },
            log_dir,
            task_id,
        )
    except (OSError, FileNotFoundError) as exc:
        duration_ms = int((time.perf_counter() - start) * 1000)
        return _emit_exec(
            {
                "executed": False,
                "command": command,
                "mode": mode,
                "selected_contract_files": selected_contract_files or [],
                "reason": "spawn_error",
                "spawn_error": str(exc),
                "exit_code": None,
                "duration_ms": duration_ms,
                "passed": 0,
                "failed": 0,
                "errored": 0,
                "failing_test_names": [],
                "all_passed": False,
                "stdout_tail": "",
                "stderr_tail": "",
            },
            log_dir,
            task_id,
        )

    stdout = proc.stdout or ""
    stderr = proc.stderr or ""
    parsed = _parse_test_output(stdout + "\n" + stderr, command)
    duration_ms = int((time.perf_counter() - start) * 1000)
    failed = parsed["failed"]
    errored = parsed["errored"]
    all_passed = (proc.returncode == 0) and (failed == 0) and (errored == 0)
    result = {
        "executed": True,
        "command": command,
        "mode": mode,
        "selected_contract_files": selected_contract_files or [],
        "reason": "ran",
        "exit_code": int(proc.returncode),
        "duration_ms": duration_ms,
        "passed": parsed["passed"],
        "failed": failed,
        "errored": errored,
        "failing_test_names": _parse_failing_test_names(stdout + "\n" + stderr),
        "all_passed": all_passed,
        "timed_out": False,
        "stdout_tail": stdout[-max_output_chars:],
        "stderr_tail": stderr[-max_output_chars:],
    }
    return _emit_exec(result, log_dir, task_id)


def _parse_test_output(text: str, command: list[str]) -> dict[str, int]:
    """Best-effort pass/fail extraction from common test runner outputs."""
    counts = {"passed": 0, "failed": 0, "errored": 0}
    if not text:
        return counts

    runner = command[0] if command else ""

    # pytest: "X passed", "Y failed", "Z error" on the summary line.
    if runner.endswith("pytest") or runner == "tox":
        pattern_pairs = [
            (r"(\d+)\s+passed", "passed"),
            (r"(\d+)\s+failed", "failed"),
            (r"(\d+)\s+errors?", "errored"),
        ]
        for pattern, key in pattern_pairs:
            for match in re.findall(pattern, text):
                counts[key] = max(counts[key], int(match))

    # go test: "--- FAIL:" per failing test, "PASS" per package.
    if runner == "go":
        counts["failed"] += len(re.findall(r"^--- FAIL:", text, re.MULTILINE))
        counts["passed"] += len(re.findall(r"^--- PASS:", text, re.MULTILINE))

    # cargo: "test result: ok. X passed; Y failed; ..."
    if runner == "cargo":
        match = re.search(
            r"test result:.*?(\d+)\s+passed;\s*(\d+)\s+failed", text
        )
        if match:
            counts["passed"] = max(counts["passed"], int(match.group(1)))
            counts["failed"] = max(counts["failed"], int(match.group(2)))

    # npm/jest: "Tests: X failed, Y passed"
    if runner in {"npm", "pnpm", "yarn", "jest", "vitest"}:
        match = re.search(r"(\d+)\s+failed", text)
        if match:
            counts["failed"] = max(counts["failed"], int(match.group(1)))
        match = re.search(r"(\d+)\s+passed", text)
        if match:
            counts["passed"] = max(counts["passed"], int(match.group(1)))

    return counts


def _parse_failing_test_names(text: str) -> list[str]:
    names: list[str] = []
    for pattern in (r"FAILED\s+([^\s]+)", r"^--- FAIL:\s+([^\s(]+)"):
        for match in re.findall(pattern, text, re.MULTILINE):
            if match not in names:
                names.append(match)
    return names[:20]


def _looks_like_test(path: str) -> bool:
    return is_test_file(path)


def _emit(result: dict[str, Any], log_dir: str | None, task_id: str) -> dict[str, Any]:
    if log_dir is not None:
        from groundtruth.runtime.telemetry import append_block

        append_block("gt_test_validation", result, log_dir=log_dir, task_id=task_id)
    return result


def _emit_exec(result: dict[str, Any], log_dir: str | None, task_id: str) -> dict[str, Any]:
    if log_dir is not None:
        from groundtruth.runtime.telemetry import append_block

        append_block("gt_test_validation", result, log_dir=log_dir, task_id=task_id)
        append_block("gt_test_execution", result, log_dir=log_dir, task_id=task_id)
    return result
