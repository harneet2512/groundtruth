"""Repo-native test command selection and execution for GT validation tools."""

from __future__ import annotations

import os
import re
import shlex
import signal
import subprocess
import time
from collections.abc import Callable
from typing import Any

from groundtruth.runtime.repo_adapters import (
    detect_repo_profile,
    is_test_file,
    select_repo_test_command,
)

# Marker appended to a container-side command when the environment's execute
# interface does not surface a reliable exit code. ``$?`` expands (in the
# container shell) to the exit status of the command that precedes the ``;`` —
# i.e. the actual test command, not the ``echo`` itself.
_EXIT_MARKER_ECHO = "__GT_EXIT_$?__"
_EXIT_MARKER_LINE_RE = re.compile(r"__GT_EXIT_(-?\d+)__")

# Group-kill signal. SIGKILL on POSIX; resolved via getattr so the constant is
# defined even where the platform lacks it (Windows never takes the POSIX branch).
_SIGKILL = getattr(signal, "SIGKILL", getattr(signal, "SIGTERM", 15))

_ENV_FAILURE_PATTERNS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "missing_runner",
        (
            "command not found",
            "not recognized as",
            "no such file or directory",
            "executable file not found",
            "failed to spawn",
        ),
    ),
    (
        "missing_manifest",
        (
            "go.mod file not found",
            "could not find cargo.toml",
            "no package.json",
            "cannot find package.json",
            "could not find a package.json",
            "no pyproject.toml",
            "no setup.py",
        ),
    ),
    (
        "package_manager_mismatch",
        (
            "pnpm-lock.yaml",
            "yarn.lock",
            "package-lock.json",
            "use pnpm",
            "use yarn",
            "npm ci can only install",
            "lockfile is out of date",
            "frozen-lockfile",
        ),
    ),
    (
        "excluded_build_target",
        (
            "build constraints exclude all go files",
            "no go files",
            "ignored by build tags",
            "target .* not found",
            "package .* is not in std",
        ),
    ),
    (
        "unresolved_module_or_artifact",
        (
            "cannot find module",
            "module not found",
            "no module named",
            "unresolved import",
            "could not resolve",
            "failed to resolve",
            "cannot find crate",
            "class not found",
        ),
    ),
    (
        "missing_linker_or_toolchain",
        (
            "linker .* not found",
            "link.exe",
            "gcc: command not found",
            "cc: command not found",
            "rustc: command not found",
            "go: cannot find",
            "jdk",
            "java_home",
            "no c compiler",
        ),
    ),
    (
        "offline_install_or_proxy",
        (
            "network is unreachable",
            "temporary failure in name resolution",
            "proxy",
            "certificate verify failed",
            "connection refused",
            "connection timed out",
            "read timed out",
            "could not fetch",
            "failed to download",
            "offline",
        ),
    ),
)


def classify_environment_failure(
    text: str,
    *,
    command: list[str] | None = None,
    spawn_error: str | None = None,
) -> str | None:
    """Classify generic self-verification environment failures.

    The categories are runner/repo-environment classes, not exact benchmark
    strings. Returns None when output looks like an ordinary test failure.
    """
    hay = "\n".join(
        part
        for part in (
            " ".join(command or []),
            spawn_error or "",
            text or "",
        )
        if part
    ).lower()
    if not hay:
        return None
    for category, patterns in _ENV_FAILURE_PATTERNS:
        for pattern in patterns:
            if re.search(pattern, hay, re.IGNORECASE):
                return category
    return None


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
    executor: Callable[[list[str], str, int], tuple[int, str, str]] | None = None,
) -> dict[str, Any]:
    """Execute a previously-selected test command and return pass/fail counts.

    Deterministic — no LLM, no network. The runner parses standard
    pytest / go test / cargo / npm / tox output for pass/fail counts. Exit code
    0 with no detected failures means ``all_passed``.

    ``executor`` (the substrate-correct execution seam): a callable with the
    FROZEN contract ::

        executor(cmd: list[str], cwd: str, timeout: int) -> tuple[int, str, str]
                                                            (exit_code, stdout, stderr)

    When supplied, the command runs THROUGH it instead of a host subprocess — so
    covering tests execute inside the task's own (possibly remote Docker /
    Singularity) container on the mini-swe-agent DeepSWE path, not on the host
    filesystem. ``executor=None`` is exactly the prior subprocess path and is
    byte-identical: same capture, same decoding, same result shape. An executor
    that raises ``subprocess.TimeoutExpired`` yields the timeout result; any other
    executor exception (or a return that is not a ``(int, str, str)`` triple)
    degrades to the ``spawn_error`` result — the gate never crashes the run.
    Classification (:func:`_parse_test_output` / :func:`_classify`) is applied to
    the returned ``(exit_code, stdout, stderr)`` identically to the subprocess
    path, so the same output always yields the same verdict.
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
    if executor is not None:
        try:
            exit_code, stdout, stderr = _unpack_executor_result(
                executor(command, repo_root, timeout_seconds)
            )
        except subprocess.TimeoutExpired:
            return _emit_exec(
                _timeout_result(
                    command,
                    mode,
                    selected_contract_files,
                    int((time.perf_counter() - start) * 1000),
                ),
                log_dir,
                task_id,
            )
        except Exception as exc:  # noqa: BLE001 -- any executor fault -> safe spawn_error
            return _emit_exec(
                _spawn_error_result(
                    command,
                    mode,
                    selected_contract_files,
                    str(exc),
                    int((time.perf_counter() - start) * 1000),
                ),
                log_dir,
                task_id,
            )
    else:
        try:
            exit_code, stdout, stderr = _run_subprocess(command, repo_root, timeout_seconds)
        except subprocess.TimeoutExpired:
            return _emit_exec(
                _timeout_result(
                    command,
                    mode,
                    selected_contract_files,
                    int((time.perf_counter() - start) * 1000),
                ),
                log_dir,
                task_id,
            )
        except (OSError, FileNotFoundError) as exc:
            return _emit_exec(
                _spawn_error_result(
                    command,
                    mode,
                    selected_contract_files,
                    str(exc),
                    int((time.perf_counter() - start) * 1000),
                ),
                log_dir,
                task_id,
            )

    stdout = stdout or ""
    stderr = stderr or ""
    parsed = _parse_test_output(stdout + "\n" + stderr, command)
    env_failure = classify_environment_failure(stdout + "\n" + stderr, command=command)
    duration_ms = int((time.perf_counter() - start) * 1000)
    failed = parsed["failed"]
    errored = parsed["errored"]
    all_passed = (exit_code == 0) and (failed == 0) and (errored == 0)
    result = {
        "executed": True,
        "command": command,
        "mode": mode,
        "selected_contract_files": selected_contract_files or [],
        "reason": "ran",
        "environment_failure_class": env_failure,
        "exit_code": int(exit_code),
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


def _timeout_result(
    command: list[str],
    mode: str,
    selected_contract_files: list[str] | None,
    duration_ms: int,
) -> dict[str, Any]:
    """The verbatim ``reason='timeout'`` result — shared by both paths."""
    return {
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
    }


def _spawn_error_result(
    command: list[str],
    mode: str,
    selected_contract_files: list[str] | None,
    spawn_error: str,
    duration_ms: int,
) -> dict[str, Any]:
    """The verbatim ``reason='spawn_error'`` result — shared by both paths."""
    return {
        "executed": False,
        "command": command,
        "mode": mode,
        "selected_contract_files": selected_contract_files or [],
        "reason": "spawn_error",
        "environment_failure_class": classify_environment_failure(
            "", command=command, spawn_error=spawn_error
        ),
        "spawn_error": spawn_error,
        "exit_code": None,
        "duration_ms": duration_ms,
        "passed": 0,
        "failed": 0,
        "errored": 0,
        "failing_test_names": [],
        "all_passed": False,
        "stdout_tail": "",
        "stderr_tail": "",
    }


def _unpack_executor_result(ret: Any) -> tuple[int, str, str]:
    """Validate + normalize an injected executor's return to ``(int, str, str)``.

    Enforces the FROZEN contract. A malformed return (wrong arity, non-int code)
    raises — the caller catches it and degrades to ``spawn_error`` so a buggy
    executor can never crash the gate."""
    if not isinstance(ret, (tuple, list)) or len(ret) != 3:
        raise ValueError(
            f"executor must return a (exit_code, stdout, stderr) triple, got {type(ret).__name__}"
        )
    code, out, err = ret
    return (
        int(code),  # raises TypeError on None -> caught -> spawn_error
        "" if out is None else str(out),
        "" if err is None else str(err),
    )


def _run_subprocess(command: list[str], cwd: str, timeout_seconds: int) -> tuple[int, str, str]:
    """Default (host) executor: run ``command`` capturing stdout/stderr, and on
    timeout kill the whole process GROUP (POSIX) / process TREE (Windows) so a
    hung test's grandchildren cannot outlive the timeout.

    Output-invisible vs the prior ``subprocess.run`` default: identical capture
    (``PIPE``/``PIPE``), identical decoding (utf-8 / ``replace``), identical
    ``returncode``. Raises ``subprocess.TimeoutExpired`` on timeout and
    ``OSError`` / ``FileNotFoundError`` on spawn failure — the same exceptions
    ``subprocess.run`` raised — so :func:`execute_test_command`'s result shapes
    are unchanged."""
    popen_kwargs: dict[str, Any] = dict(
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if os.name == "posix":
        # Child becomes a session / process-group leader (pid == pgid) so the
        # whole group can be signalled on timeout.
        popen_kwargs["start_new_session"] = True
    proc = subprocess.Popen(command, **popen_kwargs)  # OSError/FileNotFoundError
    try:
        stdout, stderr = proc.communicate(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        _kill_process_tree(proc)
        try:
            proc.communicate(timeout=5)  # reap + drain so no zombie is left
        except Exception:  # noqa: BLE001
            pass
        raise  # original TimeoutExpired -> the timeout result branch
    return proc.returncode, stdout or "", stderr or ""


def _kill_process_tree(proc: subprocess.Popen[str]) -> None:
    """Best-effort kill of ``proc`` AND its descendants. POSIX: ``SIGKILL`` the
    process GROUP (requires ``start_new_session``). Windows: ``taskkill /F /T``
    the process TREE. Where neither is available, degrade to killing the direct
    child (the prior ``subprocess.run`` behavior). Never raises."""
    try:
        if os.name == "posix" and hasattr(os, "killpg"):
            try:
                os.killpg(os.getpgid(proc.pid), _SIGKILL)  # type: ignore[attr-defined]
                return
            except (ProcessLookupError, PermissionError, OSError):
                pass  # fall through to direct-child kill
        elif os.name == "nt":
            try:
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=10,
                    check=False,
                )
                return
            except Exception:  # noqa: BLE001
                pass  # fall through to direct-child kill
    except Exception:  # noqa: BLE001
        pass
    try:
        proc.kill()  # degrade to direct-child kill (prior behavior)
    except Exception:  # noqa: BLE001
        pass


def make_env_executor(
    env_execute: Callable[..., Any],
    *,
    use_exit_marker: bool = True,
    cwd_kwarg: str = "cwd",
) -> Callable[[list[str], str, int], tuple[int, str, str]]:
    """Build an executor (the FROZEN B1 contract) from a mini-swe-agent env's
    synchronous ``execute`` callable, so covering tests RUN INSIDE the task
    container instead of on the host.

    ``env_execute`` is a callable like ``env.execute(command_string, cwd=...)``.
    mini-swe-agent environment classes (LocalEnvironment, DockerEnvironment, ...)
    expose a sync ``execute`` returning the command's combined output plus,
    usually, a return code. Accepted return shapes (parsed defensively):

      - **dict-like** — ``{"output": str, "returncode": int}`` (mini-swe-agent
        canonical). Also tolerated: keys ``stdout`` / ``stderr`` for streams and
        ``return_code`` / ``exit_code`` / ``code`` / ``status`` for the code.
      - **object-like** — attributes ``.output`` / ``.stdout`` / ``.stderr`` and
        ``.returncode`` / ``.exit_code`` / ``.code`` / ``.status``.
      - **tuple / list** — ``(code, stdout[, stderr])`` when the head is an int,
        else ``(stdout[, stderr], code)`` when the tail is an int.
      - **bare str** — treated as combined stdout with no code (requires the
        exit marker; ``use_exit_marker=False`` on a code-less return raises).

    Exit-code capture. Because many env ``execute`` implementations do NOT
    surface a reliable code (a compound ``cmd; echo ...`` reports the ``echo``'s
    status, and text-only envs report none), the default (``use_exit_marker``)
    appends ``; echo __GT_EXIT_$?__`` to the command, then parses the code from
    the LAST full-line marker in the output (so ordinary test output that merely
    mentions the token cannot spoof it) and STRIPS that marker line from stdout
    before returning. In marker mode the parsed code is authoritative and any
    code the env returned is ignored (it is the ``echo``'s). With
    ``use_exit_marker=False`` the code is taken from the env return and the
    command is left untouched.

    Deterministic, no network, no LLM. The command list is POSIX-quoted with
    ``shlex.join`` (the container runs a POSIX shell). ``cwd`` is passed via the
    ``cwd_kwarg`` keyword when the callable accepts it; otherwise it is folded
    into the command as a leading ``cd -- <cwd> &&``."""

    def _executor(command: list[str], cwd: str, timeout: int) -> tuple[int, str, str]:
        cmd_str = shlex.join(list(command))
        run_str = f"{cmd_str} ; echo {_EXIT_MARKER_ECHO}" if use_exit_marker else cmd_str
        ret = _call_env_execute(env_execute, run_str, cwd, cwd_kwarg)
        out, err, rc = _extract_env_result(ret)
        if use_exit_marker:
            code, out = _split_exit_marker(out)
            return code, out, err
        if rc is None:
            raise ValueError("environment execute returned no exit code and use_exit_marker=False")
        return int(rc), out, err

    return _executor


def _call_env_execute(
    env_execute: Callable[..., Any], run_str: str, cwd: str, cwd_kwarg: str
) -> Any:
    """Invoke ``env_execute`` passing ``cwd`` however the callable will accept it.

    Prefers the keyword form (``env.execute(cmd, cwd=...)``); if the callable
    rejects the keyword, folds the directory change into the command instead so
    the executor still runs in the right tree on envs whose ``execute`` takes
    only the command string."""
    if cwd_kwarg and cwd:
        try:
            return env_execute(run_str, **{cwd_kwarg: cwd})
        except TypeError:
            pass
    if cwd:
        folded = f"cd -- {shlex.quote(cwd)} && {run_str}"
        try:
            return env_execute(folded)
        except TypeError:
            pass
    return env_execute(run_str)


def _extract_env_result(ret: Any) -> tuple[str, str, int | None]:
    """Normalize an env ``execute`` return to ``(stdout, stderr, exit_code|None)``."""
    if isinstance(ret, dict):
        out = ret.get("output")
        if out is None:
            out = ret.get("stdout")
        rc = _coerce_exit_code(
            ret.get("returncode"),
            ret.get("return_code"),
            ret.get("exit_code"),
            ret.get("code"),
            ret.get("status"),
        )
        return _as_text(out), _as_text(ret.get("stderr")), rc
    if isinstance(ret, str):
        return ret, "", None
    if isinstance(ret, (tuple, list)):
        items = list(ret)
        if items and _is_plain_int(items[0]):
            out = _as_text(items[1]) if len(items) > 1 else ""
            err = _as_text(items[2]) if len(items) > 2 else ""
            return out, err, int(items[0])
        tail_rc: int | None = None
        body = items
        if items and _is_plain_int(items[-1]):
            tail_rc = int(items[-1])
            body = items[:-1]
        out = _as_text(body[0]) if body else ""
        err = _as_text(body[1]) if len(body) > 1 else ""
        return out, err, tail_rc
    # object-like
    out = getattr(ret, "output", None)
    if out is None:
        out = getattr(ret, "stdout", None)
    rc = _coerce_exit_code(
        getattr(ret, "returncode", None),
        getattr(ret, "return_code", None),
        getattr(ret, "exit_code", None),
        getattr(ret, "code", None),
        getattr(ret, "status", None),
    )
    return _as_text(out), _as_text(getattr(ret, "stderr", None)), rc


def _split_exit_marker(text: str) -> tuple[int, str]:
    """Return ``(exit_code, text_without_marker_line)`` from the LAST full-line
    ``__GT_EXIT_<n>__`` occurrence. A mid-line look-alike is ignored (it is not a
    whole line). Raises ``ValueError`` when no marker line is present."""
    lines = text.splitlines()
    for idx in range(len(lines) - 1, -1, -1):
        m = _EXIT_MARKER_LINE_RE.fullmatch(lines[idx].strip())
        if m:
            code = int(m.group(1))
            del lines[idx]
            return code, "\n".join(lines)
    raise ValueError("GT exit marker not found in environment output")


def _coerce_exit_code(*values: Any) -> int | None:
    """First value that is a plain int (or an all-digits string), else ``None``."""
    for v in values:
        if _is_plain_int(v):
            return int(v)
        if isinstance(v, str) and re.fullmatch(r"-?\d+", v.strip()):
            return int(v.strip())
    return None


def _is_plain_int(v: Any) -> bool:
    """int, but not bool (``True``/``False`` are not exit codes)."""
    return isinstance(v, int) and not isinstance(v, bool)


def _as_text(v: Any) -> str:
    return "" if v is None else str(v)


def _detect_runner(command: list[str]) -> str:
    """Canonical runner name, scanned across ALL tokens (not just command[0]).

    Handles wrapped/prefixed invocations — ``python -m pytest``, ``npx jest``,
    ``timeout 60 cargo test`` — that a bare ``command[0]`` check would miss.
    Order matters: the compiled-language runners are checked before the
    JS/Node family so ``go``/``cargo`` win over a stray ``node`` token.
    Bare-``command[0]`` callers are unaffected (the first token still matches)."""
    for tok in command or []:
        base = tok.rsplit("/", 1)[-1].rsplit("\\", 1)[-1].lower()
        if base.endswith("pytest") or base == "tox":
            return "pytest"
        if base == "go":
            return "go"
        if base == "cargo":
            return "cargo"
        if base in {
            "npm",
            "pnpm",
            "yarn",
            "jest",
            "vitest",
            "mocha",
            "bun",
            "deno",
            "node",
            "jasmine",
            "npx",
            "bunx",
        }:
            return "jsnode"
        if base in {"mvn", "mvnw", "gradle", "gradlew"}:
            return "mvn"
        if base in {"ruby", "rake", "rspec", "bundle", "minitest"}:
            return "ruby"
    return command[0] if command else ""


# Leak/verdict-robustness (2026-07-19): colorized runner output (tty containers,
# FORCE_COLOR/PY_COLORS repos) interleaves ANSI CSI sequences into the very tokens the
# parsers below match as plain text ("1 failed", "FAILED x::y"), silently mis-counting
# verdicts and mis-attributing failures. De-colorize at THIS single parsing surface so
# every caller (direct subprocess + executor path) is covered. No-op on colorless input.
_RE_ANSI_CSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")


def _parse_test_output(text: str, command: list[str]) -> dict[str, int]:
    """Best-effort pass/fail extraction from common test runner outputs."""
    counts = {"passed": 0, "failed": 0, "errored": 0}
    if not text:
        return counts
    text = _RE_ANSI_CSI.sub("", text)

    runner = _detect_runner(command)

    # pytest: "X passed", "Y failed", "Z error" on the summary line.
    if runner == "pytest":
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

    # cargo: "test result: ok. X passed; Y failed; ..." — SUM across every target
    # (Fable F5d: a multi-crate run whose FIRST line is 0/0 must not hide a later fail).
    if runner == "cargo":
        for p, f in re.findall(r"test result:.*?(\d+)\s+passed;\s*(\d+)\s+failed", text):
            counts["passed"] += int(p)
            counts["failed"] += int(f)

    # npm/jest/vitest: "Tests: X failed, Y passed"; mocha: "X passing / Y failing".
    if runner == "jsnode":
        for pattern, key in (
            (r"(\d+)\s+failed", "failed"),
            (r"(\d+)\s+passed", "passed"),
            (r"(\d+)\s+failing", "failed"),
            (r"(\d+)\s+passing", "passed"),
        ):
            match = re.search(pattern, text)
            if match:
                counts[key] = max(counts[key], int(match.group(1)))

    # maven surefire: "Tests run: X, Failures: Y, Errors: Z" (per module -> sum).
    if runner == "mvn":
        for m in re.finditer(r"Tests run:\s*(\d+),\s*Failures:\s*(\d+),\s*Errors:\s*(\d+)", text):
            run_n, fail_n, err_n = int(m.group(1)), int(m.group(2)), int(m.group(3))
            counts["failed"] += fail_n
            counts["errored"] += err_n
            counts["passed"] += max(0, run_n - fail_n - err_n)

    # ruby minitest ("X runs, Y assertions, Z failures, W errors") / rspec
    # ("X examples, Y failures").
    if runner == "ruby":
        m = re.search(
            r"(\d+)\s+runs?,\s*\d+\s+assertions?,\s*(\d+)\s+failures?,\s*(\d+)\s+errors?",
            text,
        )
        if m:
            run_n, fail_n, err_n = int(m.group(1)), int(m.group(2)), int(m.group(3))
            counts["failed"] += fail_n
            counts["errored"] += err_n
            counts["passed"] += max(0, run_n - fail_n - err_n)
        m2 = re.search(r"(\d+)\s+examples?,\s*(\d+)\s+failures?", text)
        if m2:
            ex_n, fail_n = int(m2.group(1)), int(m2.group(2))
            counts["failed"] = max(counts["failed"], fail_n)
            counts["passed"] = max(counts["passed"], ex_n - fail_n)

    return counts


def _parse_failing_test_names(text: str) -> list[str]:
    """Return stable failing-test identities from native runner output.

    Pytest prints both ``nodeid FAILED [NN%]`` progress rows and
    ``FAILED nodeid - detail`` summary rows.  Parse the nodeid from either side
    of the marker; never treat the progress bracket as a test name.
    """
    names: list[str] = []
    text = _RE_ANSI_CSI.sub("", text)
    for pattern in (
        r"^(\S+::\S+)\s+FAILED(?:\s|$)",
        r"^FAILED\s+(\S+::\S+?)(?:\s+-|\s*$)",
        r"^--- FAIL:\s+([^\s(]+)",
    ):
        for match in re.findall(pattern, text, re.MULTILINE):
            if match not in names:
                names.append(match)
    return names[:20]


def _parse_passing_test_names(text: str) -> list[str]:
    """Return stable pytest node identities explicitly reported as passing."""
    names: list[str] = []
    for pattern in (
        r"^(\S+::\S+)\s+PASSED(?:\s|$)",
        r"^PASSED\s+(\S+::\S+?)(?:\s|$)",
    ):
        for match in re.findall(pattern, text or "", re.MULTILINE):
            if match not in names:
                names.append(match)
    return names[:20]


def _parse_requested_test_names(command: str) -> list[str]:
    """Return explicit node IDs selected by a successful pytest command."""
    try:
        words = shlex.split(command or "", posix=True)
    except ValueError:
        return []
    names: list[str] = []
    for word in words:
        if word.startswith("-") or "::" not in word:
            continue
        nodeid = word.replace("\\", "/")
        if nodeid not in names:
            names.append(nodeid)
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
