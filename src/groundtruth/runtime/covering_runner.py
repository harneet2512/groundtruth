"""Execute graph-selected covering test FILES and return a conservative verdict.

B1 (verification-execution). This is the missing verb: GroundTruth already
SELECTS the test files that reach an edited symbol (``_covering_tests_for_symbols``
in gt_mini_patch — safe-RTS / Ekstazi ranking, FACT-tier CALLS edges, top-2), but
it never RUNS them. This module runs them, in the repo's own environment, and
reports whether a covering test actually FAILS.

Design invariants (plan §6):
- **LLM-free / deterministic.** Subprocess + string parsing only (harvests
  ``test_runner.execute_test_command``). No network, no task IDs, no gold labels.
- **Leak-safe by construction.** The wrapper takes only a test-FILE path (the
  selector already stripped the test NAME) and runs the WHOLE file. The agent
  never sees an internal identifier — only the runner's own stdout/stderr, which
  is a surface the agent could produce itself by running the repo's own tests.
- **Conservative (accuracy invariant ②: a false block is worse than no gate).**
  A ``fail`` verdict requires POSITIVE failure evidence (a parsed failing count
  or a non-empty ``failing_test_names``). Ambiguity — an env failure, a timeout,
  a "no tests collected" run, or a non-zero exit with no parsed evidence — is
  ``unavailable`` (pass-with-record), never a block.

Verdict consumed by the Gate Kernel: ``fail`` -> BLOCK; ``pass`` -> GREEN;
``unavailable`` -> pass-with-record.
"""

from __future__ import annotations

import os
import posixpath
import shutil
import sqlite3
import subprocess
import tempfile
import time
import uuid
from typing import Any, Callable

from groundtruth.pretask.curation_map import DETERMINISTIC_RESOLUTION_METHODS
from groundtruth.runtime.test_runner import execute_test_command

# The injectable execution boundary (shared contract with test_runner, built to —
# NOT imported from — a parallel change). ``executor(cmd, cwd, timeout)`` returns
# ``(exit_code, stdout, stderr)``. ``None`` selects the default subprocess path so
# every existing caller is byte-identical.
Executor = Callable[[list[str], str, int], "tuple[int | None, str, str]"]

# pytest's "no tests collected" exit code — a selection miss, not a failure.
_PYTEST_NO_TESTS_EXIT = 5

# FACT-tier resolution methods — the ONE canonical set (curation_map), never a
# re-declared literal (Fable F6b: literal drift is the whack-a-mole d0684d83 fought).
# A covering test must reach the edited symbol through a RESOLVED (non-guess) edge.
_DETERMINISTIC_METHODS = DETERMINISTIC_RESOLUTION_METHODS


def _connect_ro(db_path: str):
    """Open graph.db read-only (never mutate the authoritative graph)."""
    try:
        return sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
    except Exception:  # noqa: BLE001
        # Graph-F9 (bounce 2026-07-10): the ro-URI open failed, so this fallback uses a
        # PLAIN connect — which is READ-WRITE and would create a missing file / permit a
        # mutation through a helper named _connect_ro. Enforce PRAGMA query_only=1 so the
        # authoritative graph is never written via the "ro" helper (parity with
        # curation_map._open_ro). Fail-safe: any error -> None (correct-or-quiet).
        try:
            con = sqlite3.connect(db_path, timeout=5)
            con.execute("PRAGMA query_only = 1")
            return con
        except Exception:  # noqa: BLE001
            return None


def _edge_columns(con: sqlite3.Connection) -> set[str]:
    try:
        return {r[1] for r in con.execute("PRAGMA table_info(edges)").fetchall()}
    except Exception:  # noqa: BLE001
        return set()


def select_covering_tests(
    db_path: str, symbol_names: set[str] | list[str], *, limit: int = 2
) -> list[dict[str, Any]]:
    """Graph-select the repo's OWN test FILES that reach the edited symbols.

    A test file "covers" an edited symbol when a test node CALLS it through a
    FACT-tier edge (``_DETERMINISTIC_METHODS``, confidence >= 0.7). Ranked by
    edge confidence (safe-RTS reachability, Rothermel & Harrold TOSEM'97), top-N.

    Leak-safe BY CONSTRUCTION: returns only ``{"file", "confidence"}`` — the test
    NAME is never selected (we GROUP BY file_path), so no internal identifier can
    reach a renderer. Shared by the OH (post_edit) and mini (gt_mini_patch) seams
    so there is ONE covering-selection surface (plan §6 invariant ①).

    Correct-or-quiet: no db / no schema / no fact edges -> [].
    """
    syms = {str(s) for s in (symbol_names or ()) if s}
    if not syms or not db_path or not os.path.isfile(db_path):
        return []
    con = _connect_ro(db_path)
    if con is None:
        return []
    try:
        ecols = _edge_columns(con)
        if "resolution_method" not in ecols:
            return []
        has_conf = "confidence" in ecols
        sph = ",".join("?" * len(syms))
        target_rows = con.execute(
            f"SELECT id FROM nodes WHERE name IN ({sph}) "
            f"AND COALESCE(is_test, 0) = 0 LIMIT 20",
            list(syms),
        ).fetchall()
        if not target_rows:
            return []
        tids = [r[0] for r in target_rows]
        det = "','".join(sorted(_DETERMINISTIC_METHODS))
        # Reference e.confidence ONLY when the column exists — otherwise the SELECT
        # itself throws on a legacy schema and the whole selection silently returns
        # [] (a coverage hole masquerading as "no covering tests"). Consistent with
        # conf_gate: no confidence column -> treat every FACT edge as conf 1.0.
        conf_gate = "AND COALESCE(e.confidence, 0) >= 0.7 " if has_conf else ""
        conf_expr = "MAX(COALESCE(e.confidence, 1.0))" if has_conf else "1.0"
        tph = ",".join("?" * len(tids))
        rows = con.execute(
            f"SELECT nt.file_path, {conf_expr} AS mc "
            "FROM edges e JOIN nodes nt ON nt.id = e.source_id "
            f"WHERE e.target_id IN ({tph}) AND e.type = 'CALLS' "
            "AND COALESCE(nt.is_test, 0) = 1 "
            f"AND LOWER(TRIM(e.resolution_method)) IN ('{det}') {conf_gate}"
            "GROUP BY nt.file_path ORDER BY mc DESC, nt.file_path LIMIT 8",
            tids,
        ).fetchall()
        out: list[dict[str, Any]] = []
        for fpath, mc in rows[:limit]:
            if fpath:
                out.append({"file": fpath, "confidence": float(mc if mc is not None else 1.0)})
        return out
    except Exception:  # noqa: BLE001 -- correct-or-quiet
        return []
    finally:
        try:
            con.close()
        except Exception:  # noqa: BLE001
            pass


def build_covering_command(test_file: str) -> list[str] | None:
    """File-scoped, per-language run command for a single test FILE.

    No test NAME is ever used (the selector stripped it — leak-safe). ONE
    extension-dispatch surface (mirrors gt_mini_patch._test_run_command /
    _LANG_TO_EXT). Returns None when the extension is unknown (correct-or-quiet).
    ``command[0]`` is the runner binary so test_runner._detect_runner parses it.
    """
    if not test_file:
        return None
    norm = test_file.replace("\\", "/")
    ext = os.path.splitext(norm)[1].lower()
    if ext in (".py",):
        return ["pytest", norm]
    if ext == ".go":
        # (Fable F5b: don't mangle absolute/leading-dot paths — `"./"+lstrip("./")`
        # turned /repo/pkg into ./repo/pkg. Prefix ./ only for a plain relative dir.)
        pkg = os.path.dirname(norm)
        if not pkg or pkg == ".":
            return ["go", "test", "."]
        if pkg.startswith(("/", "./", "../")):
            return ["go", "test", pkg]
        return ["go", "test", "./" + pkg]
    if ext == ".rs":
        # cargo cannot scope by file; filter by the file stem (matches test
        # function paths containing it). Empty match -> 0/0 -> unavailable.
        stem = os.path.splitext(os.path.basename(norm))[0]
        return ["cargo", "test", stem]
    if ext in (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"):
        return ["npx", "jest", norm]
    if ext == ".java":
        cls = os.path.splitext(os.path.basename(norm))[0]
        return ["mvn", "test", "-Dtest=" + cls]
    if ext == ".rb":
        return ["ruby", "-Itest", norm]
    return None


def _classify_one(result: dict[str, Any]) -> str:
    """Per-file verdict: 'fail' | 'pass' | 'unavailable' (conservative)."""
    if not result.get("executed"):
        return "unavailable"  # spawn error / no command
    if result.get("timed_out"):
        return "unavailable"  # a timeout is not a correctness failure
    if result.get("environment_failure_class"):
        return "unavailable"  # env problem, not the code under test
    exit_code = result.get("exit_code")
    passed = int(result.get("passed") or 0)
    failed = int(result.get("failed") or 0)
    errored = int(result.get("errored") or 0)
    names = result.get("failing_test_names") or []
    # POSITIVE failure evidence AND a real non-zero exit — the only path to a fail.
    # (Fable F1: a warning summary or a jest console line containing "FAILED" / "N
    # failed" must NOT fabricate a RED on a green exit-0 run. env/timeout already
    # returned "unavailable" above, so exit_code here is a concrete int.)
    if (failed > 0 or errored > 0 or names) and exit_code not in (0, None):
        return "fail"
    # pytest "no tests collected" is a selection miss, never a failure.
    if exit_code == _PYTEST_NO_TESTS_EXIT and passed == 0:
        return "unavailable"
    # POSITIVE pass evidence: clean exit AND at least one parsed passing test.
    if exit_code == 0 and passed > 0:
        return "pass"
    # Anything else (exit != 0 with no parsed evidence, empty run) is ambiguous.
    return "unavailable"


def run_covering_tests(
    repo_root: str,
    covering_files: list[str],
    *,
    per_file_timeout: int = 120,
    total_budget_seconds: int = 300,
    max_output_chars: int = 4000,
    log_dir: str | None = None,
    task_id: str = "unknown",
    executor: Executor | None = None,
) -> dict[str, Any]:
    """Run each covering test file (bounded) and aggregate to ONE verdict.

    Aggregation: any file 'fail' -> overall 'fail'; else any 'pass' -> 'pass';
    else 'unavailable'. Stops once the cumulative wall-clock budget is spent so a
    slow first file cannot starve the loop. The representative command + the
    stdout/stderr tail of the DECIDING file (the first failing file, or the last
    file otherwise) are returned for the native renderer.
    """
    files = [f for f in (covering_files or []) if f]
    base: dict[str, Any] = {
        "executed": False,
        "verdict": "unavailable",
        "reason": "no_covering_files",
        "files": files,
        "ran": [],
        "environment_failure_class": None,
        "command": [],
        "stdout_tail": "",
        "stderr_tail": "",
        "exit_code": None,
        "failing_test_names": [],
        "duration_ms": 0,
    }
    if not files or not repo_root:
        return base

    spent_ms = 0
    ran: list[str] = []
    deciding: dict[str, Any] | None = None
    env_class: str | None = None
    any_executed = False

    for test_file in files:
        if spent_ms >= total_budget_seconds * 1000:
            base["reason"] = "budget_exhausted"
            break
        command = build_covering_command(test_file)
        if command is None:
            continue
        remaining = max(1, min(per_file_timeout, total_budget_seconds - spent_ms // 1000))
        if executor is None:
            result = execute_test_command(
                repo_root,
                command,
                timeout_seconds=remaining,
                max_output_chars=max_output_chars,
                mode="covering",
                selected_contract_files=[test_file],
                log_dir=log_dir,
                task_id=task_id,
            )
        else:
            result = _run_via_executor(
                executor, repo_root, command,
                timeout=remaining, max_output_chars=max_output_chars,
            )
        spent_ms += int(result.get("duration_ms") or 0)
        if result.get("executed"):
            any_executed = True
        env_class = env_class or result.get("environment_failure_class")
        verdict = _classify_one(result)
        result["_verdict"] = verdict
        result["_file"] = test_file
        if verdict != "unavailable":
            ran.append(test_file)
        if verdict == "fail":
            # First real failure decides — deliver its output, stop early.
            deciding = result
            break
        if verdict == "pass":
            deciding = result  # prefer a pass over an earlier unavailable
        elif deciding is None:
            deciding = result  # remember an unavailable only if nothing better yet

    # Deterministic aggregate: any real failure -> fail; else any real run -> pass;
    # else nothing conclusive ran -> unavailable. (When we broke on a fail, `ran`
    # holds only non-unavailable files and none of the earlier ones failed.)
    if deciding is not None and deciding.get("_verdict") == "fail":
        overall = "fail"
    elif ran:
        overall = "pass"
    else:
        overall = "unavailable"

    out = dict(base)
    out.update(
        {
            "executed": any_executed,
            "verdict": overall,
            "reason": (deciding or {}).get("reason", base["reason"]) if deciding else base["reason"],
            "ran": ran,
            "environment_failure_class": env_class,
            "command": (deciding or {}).get("command", []),
            "stdout_tail": (deciding or {}).get("stdout_tail", ""),
            "stderr_tail": (deciding or {}).get("stderr_tail", ""),
            "exit_code": (deciding or {}).get("exit_code"),
            "failing_test_names": (deciding or {}).get("failing_test_names", []),
            "duration_ms": spent_ms,
        }
    )
    return out


def _run_via_executor(
    executor: Executor,
    repo_root: str,
    command: list[str],
    *,
    timeout: int,
    max_output_chars: int,
) -> dict[str, Any]:
    """Adapt an ``executor(cmd, cwd, timeout) -> (exit, stdout, stderr)`` call into
    the ``execute_test_command`` result shape, reusing test_runner's OWN pure
    parsers so there is ONE parsing surface (no re-implemented runner grammar).

    Correct-or-quiet: an executor that raises is treated as a spawn error ->
    ``executed=False`` -> classifies to ``unavailable`` (never a fabricated fail)."""
    from groundtruth.runtime.test_runner import (
        _parse_failing_test_names,
        _parse_test_output,
        classify_environment_failure,
    )

    start = time.perf_counter()
    try:
        rc, stdout, stderr = executor(command, repo_root, timeout)
    except Exception as exc:  # noqa: BLE001 -- executor failure == spawn error == unavailable
        return {
            "executed": False,
            "command": command,
            "reason": "spawn_error",
            "environment_failure_class": None,
            "spawn_error": str(exc),
            "exit_code": None,
            "duration_ms": int((time.perf_counter() - start) * 1000),
            "passed": 0,
            "failed": 0,
            "errored": 0,
            "failing_test_names": [],
            "timed_out": False,
            "stdout_tail": "",
            "stderr_tail": "",
        }
    stdout = stdout or ""
    stderr = stderr or ""
    combined = stdout + "\n" + stderr
    parsed = _parse_test_output(combined, command)
    return {
        "executed": True,
        "command": command,
        "reason": "ran",
        "environment_failure_class": classify_environment_failure(combined, command=command),
        "exit_code": None if rc is None else int(rc),
        "duration_ms": int((time.perf_counter() - start) * 1000),
        "passed": parsed["passed"],
        "failed": parsed["failed"],
        "errored": parsed["errored"],
        "failing_test_names": _parse_failing_test_names(combined),
        "timed_out": False,
        "stdout_tail": stdout[-max_output_chars:],
        "stderr_tail": stderr[-max_output_chars:],
    }


# ---------------------------------------------------------------------------
# green -> base -> red DIFFERENTIAL attribution
# ---------------------------------------------------------------------------
def _git(
    repo_root: str,
    args: list[str],
    *,
    timeout: int = 30,
    executor: Executor | None = None,
) -> str | None:
    """Run one git subcommand in ``repo_root``. Returns stripped stdout on a clean
    exit (``""`` on an empty-but-successful command), ``None`` on ANY failure.
    Never raises — the differential path must fail toward QUIET, never crash.

    ``executor=None`` runs git via the LOCAL subprocess (host) — byte-identical to
    the pre-executor behaviour. When an ``executor`` is provided the repo lives in a
    REMOTE namespace (Docker/Singularity on the mini-swe-agent path), so the SAME
    ``git -C <repo_root> <args>`` command is dispatched through it, executing where
    the task tree actually is. A host-side subprocess would operate on the host fs,
    not the container's — the exact defect this closes."""
    cmd = ["git", "-C", repo_root, *args]
    if executor is None:
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                check=False,
            )
        except Exception:  # noqa: BLE001
            return None
        if proc.returncode != 0:
            return None
        return (proc.stdout or "").strip()
    # Container/executor path: run the SAME git command in the repo's namespace.
    try:
        rc, stdout, _stderr = executor(cmd, repo_root, timeout)
    except Exception:  # noqa: BLE001 -- executor failure == git unavailable == quiet
        return None
    if rc != 0:  # rc is None (unknown) also falls here -> cannot confirm -> quiet
        return None
    return (stdout or "").strip()


def _container_base_path(repo_root: str) -> str:
    """Derive a CONTAINER-side worktree path from ``repo_root`` — NEVER a host
    ``tempfile`` dir (which resolves on the ORCHESTRATOR host and is invisible
    inside the task's container).

    Placed as a hidden SIBLING of the repo (``<parent>/.gt_base_<token>``) so the
    transient base checkout never appears inside the agent's own git working tree
    (no ``git status`` / ``git add -A`` pollution of the task's final patch). Built
    with ``posixpath`` and ``\\``->``/`` normalisation so a Windows orchestrator host
    cannot inject backslashes into a Linux container path. The random token keeps
    concurrent post_edit hooks from colliding."""
    base = repo_root.replace("\\", "/").rstrip("/")
    parent = posixpath.dirname(base) or base or "/"
    return posixpath.join(parent, ".gt_base_" + uuid.uuid4().hex[:12])


def _make_base_worktree(
    repo_root: str, *, executor: Executor | None = None
) -> tuple[str, str] | None:
    """Materialize the PRE-EDIT base tree as a read-only ``git worktree`` at HEAD.

    Returns ``(worktree_dir, tmp_parent)`` or ``None`` when the base is unavailable
    (not a git work tree / no resolvable HEAD / ``worktree add`` failed). The agent
    in a SWE container edits the WORKING TREE without committing, so HEAD IS the
    pre-edit base. Never raises.

    ``executor=None`` -> LOCAL host path, byte-identical to before (a host
    ``tempfile`` parent, ``os.path.isdir`` confirmation). ``executor`` provided ->
    the repo is in a REMOTE namespace: derive a CONTAINER-side path (never host
    tempfile) and create the worktree THROUGH the executor. No host fs check is
    possible on that path (``wt`` is a container path), so success is trusted from
    the git return code (fail-safe: a non-zero ``worktree add`` -> ``None`` -> quiet)."""
    try:
        if not repo_root or _git(
            repo_root, ["rev-parse", "--verify", "HEAD"], executor=executor
        ) is None:
            return None
        if executor is None:
            parent = tempfile.mkdtemp(prefix="gt_base_")
            # git refuses a non-empty target; hand it a fresh non-existent subpath.
            wt = os.path.join(parent, "base")
            if (_git(repo_root, ["worktree", "add", "--detach", wt, "HEAD"]) is None
                    or not os.path.isdir(wt)):
                shutil.rmtree(parent, ignore_errors=True)
                return None
            return wt, parent
        # Container/executor path: host tempfile is invisible in the remote namespace.
        wt = _container_base_path(repo_root)
        if _git(
            repo_root, ["worktree", "add", "--detach", wt, "HEAD"], executor=executor
        ) is None:
            # Best-effort unregister a half-created worktree, then quiet (no leak).
            _git(repo_root, ["worktree", "remove", "--force", wt], executor=executor)
            return None
        return wt, ""  # no host parent to rmtree; cleanup removes wt via executor
    except Exception:  # noqa: BLE001
        return None


def _cleanup_base_worktree(
    repo_root: str, wt: str, parent: str, *, executor: Executor | None = None
) -> None:
    """Unregister + delete the base worktree and its tmp parent. Never raises —
    leftover temp state must not surface as a runtime error.

    Routes the git unregister through the SAME ``executor`` (container namespace)
    when one is provided, so the worktree is removed where it was created. On the
    executor path there is no host tmp parent (``git worktree remove`` deletes the
    checkout dir itself); a guarded ``rm -rf`` on the derived path mops up any
    half-created residue. ``executor=None`` -> byte-identical host cleanup."""
    try:
        _git(repo_root, ["worktree", "remove", "--force", wt], executor=executor)
        _git(repo_root, ["worktree", "prune"], executor=executor)
    except Exception:  # noqa: BLE001
        pass
    if executor is None:
        shutil.rmtree(parent, ignore_errors=True)
    elif wt and ".gt_base_" in wt:  # guard: only ever rm a path WE derived
        try:
            executor(["rm", "-rf", wt], repo_root, 30)
        except Exception:  # noqa: BLE001
            pass


def differential_attribution(
    repo_root: str,
    covering_files: list[str],
    current_result: dict[str, Any] | None,
    *,
    executor: Executor | None = None,
    per_file_timeout: int = 120,
    total_budget_seconds: int = 300,
    max_output_chars: int = 4000,
) -> bool:
    """Attribute a covering RED by DIFFERENCE against the pre-edit base tree.

    Runs the SAME ``covering_files`` (same budget caps) against a ``git worktree``
    checked out at HEAD. Returns:
      * ``True``  when the base is GREEN (POSITIVE pass) — the current run is red
        only because of the working-tree edit => attributed => deliver.
      * ``False`` when the base is ALSO red (pre-existing failure — this kills the
        false-block vector), or the base is ``unavailable`` (no git / worktree add
        failed / timeout / no positive pass), or the current run was not a fail.

    Correct-or-quiet by construction: attribution requires POSITIVE base-green
    evidence; every ambiguous outcome returns ``False`` (never a false red)."""
    if not repo_root or not covering_files:
        return False
    # Attribution is a CONJUNCTION: current-RED AND base-GREEN. Proceed ONLY when
    # the CURRENT verdict is exactly "fail" — a stray call with None/{}/missing
    # verdict must NOT reach the base run (base-green alone is zero evidence the
    # edit broke anything; fail toward QUIET).
    if not isinstance(current_result, dict) or current_result.get("verdict") != "fail":
        return False
    made = _make_base_worktree(repo_root, executor=executor)
    if made is None:
        return False  # no reachable base tree -> cannot compare -> quiet
    wt, parent = made
    try:
        base = run_covering_tests(
            wt,
            covering_files,
            per_file_timeout=per_file_timeout,
            total_budget_seconds=total_budget_seconds,
            max_output_chars=max_output_chars,
            executor=executor,
        )
    except Exception:  # noqa: BLE001 -- any base-run explosion -> quiet
        return False
    finally:
        _cleanup_base_worktree(repo_root, wt, parent, executor=executor)
    # POSITIVE base-green (+ current-red, established by the caller) => attributed.
    return base.get("verdict") == "pass"


def is_red_attributable(
    current_result: dict[str, Any] | None,
    edited_files: set[str] | list[str],
    *,
    test_files: list[str] | set[str] | None = None,
    repo_root: str | None = None,
    covering_files: list[str] | None = None,
    executor: Executor | None = None,
    per_file_timeout: int = 120,
    total_budget_seconds: int = 300,
    max_output_chars: int = 4000,
) -> bool:
    """THE ONE QUESTION for the seam: is this covering RED attributable to the edit?

    Frames FIRST (pure, free): the deepest agent-source traceback frame lands in an
    edited file (a CRASH). Differential SECOND, ONLY when frames cannot decide AND a
    base tree is reachable: current-RED + base-GREEN on the same covering files =>
    the working-tree edit is the delta. An ASSERTION failure leaves no agent frame,
    so the differential leg is what makes B1 speak on value failures.

    Cost: the base run happens ONLY on the frame-miss path (crashes short-circuit
    before any subprocess). No ``repo_root``/``covering_files`` => frames-only =>
    quiet on an unattributed assertion. Reads no environment / no globals."""
    from groundtruth.runtime.native_render import is_edit_attributed

    if is_edit_attributed(current_result or {}, edited_files, test_files=test_files):
        return True
    if not repo_root or not covering_files:
        return False  # differential unavailable -> frames-only -> correct-or-quiet
    return differential_attribution(
        repo_root,
        covering_files,
        current_result,
        executor=executor,
        per_file_timeout=per_file_timeout,
        total_budget_seconds=total_budget_seconds,
        max_output_chars=max_output_chars,
    )
