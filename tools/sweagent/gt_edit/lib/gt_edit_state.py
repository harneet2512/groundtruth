#!/usr/bin/env python3
"""gt_edit_state — L3 + L6 state command for SWE-agent.

Mechanism: SWE-agent runs `state_command` after every agent action via
`env.communicate(state_command)` (see /home/ubuntu/SWE-agent/sweagent/tools/
tools.py:344). The script writes `/root/state.json` whose dict is merged
into the next Jinja2 template render, so `gt_evidence` becomes available as
`{{gt_evidence}}` in the agent's instance template.

The state command does NOT receive (tool_name, args) — SWE-agent's surface
exposes no action context to state hooks. We detect source-file edits by
walking the workspace's git state and comparing per-file SHA-256 hashes
against a per-instance baseline at `/root/.gt_edit_baseline.json`. Same
pattern as `tools/diff_state/bin/_state_diff_state` (which uses `git diff`
to detect changes regardless of how they were made).

When new source-file changes are detected:
  L6: invoke `gt-index -root <repo> -file <path> -output <graph.db>`,
      capture the JSON output line, append to `<log_dir>/gt_reindex.jsonl`.
  L3: invoke `gt_hook.py analyze <path> --root <repo>` (post-edit family
      brief), capture stdout, write to `<log_dir>/gt_evidence/edit_NNN.json`.

Counter `<log_dir>/.edit_counter` persists across runs to keep the NNN
suffix monotonic per instance.

ENV inputs:
  GT_GRAPH_DB              REQUIRED — path to graph.db.
  GT_INSTANCE_LOG_DIR      REQUIRED — per-instance log dir.
  GT_INDEX_BIN             gt-index binary path. Resolved by RC-13 chain:
                           env var, then bundle-relative `<bundle>/bin/gt-index`,
                           then `shutil.which("gt-index")`, then in-tree
                           dev path on Windows. KeyError raised if none
                           resolve — no more silent `/home/ubuntu/...` fallback.
  GT_HOOK_PY               gt_hook.py path. Resolved by RC-13 chain:
                           env var, then bundle-relative
                           `<bundle>/lib/gt_hook.py`, then in-tree dev path on
                           Windows. KeyError raised if none resolve.
  GT_REPO_ROOT / ROOT      Repo root. Defaults to /workspace then $ROOT then cwd.
  GT_STATE_PATH            State file path. Default `/root/state.json` (matches
                           SWE-agent contract). Tests override with a tmp path.

This module is also importable for local synthetic testing — see
`run_state(...)` for the pure entry point used by tests/groundtruth/
test_edit_predicates.py is purely the predicate; for end-to-end smoke we
exercise this module directly (see verification step C in the Track B1
plan).
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Imports — make src/groundtruth importable for `is_source_edit` and
# `extract_edited_path` (used in the synthetic test path; the SWE-agent
# state-command path uses the git-diff detector instead because it has no
# tool-name/args available at state-call time).
# ---------------------------------------------------------------------------

_THIS = Path(__file__).resolve()
# tools/sweagent/gt_edit/lib/gt_edit_state.py -> repo root is parents[4]
# Defensive: if the bundle is shipped to /tmp/gt_edit/ on the t0 VM (no
# parent repo checkout), parents[4] can IndexError. In that case the env
# vars GT_INDEX_BIN / GT_HOOK_PY MUST be set explicitly; we degrade
# gracefully to a None _REPO_ROOT_HOST and the *_default helpers below
# fall through to the env vars.
try:
    _REPO_ROOT_HOST: Path | None = _THIS.parents[4]
except IndexError:
    _REPO_ROOT_HOST = None

# Fallback: the bundle is often installed to /tmp/gt_edit/ on the t0 VM
# (out-of-tree); fall back to a well-known GroundTruth source location so
# we can still import edit_predicates. Override via GT_GROUNDTRUTH_SRC.
_SRC_CANDIDATES: list[Path] = []
if _REPO_ROOT_HOST is not None:
    _SRC_CANDIDATES.append(_REPO_ROOT_HOST / "src")
if (env_src := os.environ.get("GT_GROUNDTRUTH_SRC")):
    _SRC_CANDIDATES.append(Path(env_src))
# RC-13: drop hardcoded `/home/ubuntu/...` and `/root/Groundtruth/...`
# fallbacks. The bundle's own lib/ dir is the only portable last resort —
# anything VM-specific must come from GT_GROUNDTRUTH_SRC. If the env var is
# missing AND the bundle's lib/ doesn't expose `groundtruth/`, the
# downstream `from groundtruth.edit_predicates import ...` simply fails
# closed (synthetic-test path only; container path uses the git-diff
# detector that does not touch this import).
_SRC_CANDIDATES.append(Path(__file__).resolve().parent)
for _src in _SRC_CANDIDATES:
    try:
        if _src.is_dir() and str(_src) not in sys.path:
            sys.path.insert(0, str(_src))
    except (OSError, PermissionError):
        continue

try:
    from groundtruth.edit_predicates import (  # type: ignore
        extract_edited_path,
        is_source_edit,
    )
except Exception:  # pragma: no cover — only matters in synthetic tests
    is_source_edit = None  # type: ignore[assignment]
    extract_edited_path = None  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_SOURCE_EXTS: tuple[str, ...] = (
    ".py", ".js", ".ts", ".go", ".java", ".rs", ".rb", ".php"
)

_DEFAULT_STATE_PATH = "/root/state.json"
_BASELINE_BASENAME = ".gt_edit_baseline.json"


# ---------------------------------------------------------------------------
# RC-15: Graph.db is built ONCE by the pre-run hook on the host. The state
# command must never trigger a synchronous build — that path used to block
# the agent for up to 600s and corrupted /tmp/graph.db when SWE-agent's
# execution_timeout (60-120s) hit first. If GT_GRAPH_DB is missing inside
# the container, log it and silent-no-op every state call: L3/L6 evidence
# is empty, but the agent loop is never blocked.
# ---------------------------------------------------------------------------

def _resolve_graph_db_no_build() -> str:
    """Return the path to a pre-existing graph.db, or "" if none.

    Pre-run hook is the ONLY place that builds the graph (RC-15 fix).
    Never invokes gt-index. Probes the conventional container path so
    callers that did NOT set GT_GRAPH_DB still get a graph if one exists.
    """
    for path in ("/tmp/graph.db",):
        if os.path.isfile(path):
            return path
    # Best-effort log so post-mortem can correlate "L3 empty" with "graph
    # was never built" rather than chasing a state-command bug.
    try:
        import time as _t
        with open("/tmp/gt_edit_state_init.log", "a", encoding="utf-8") as f:
            f.write(
                f'{{"ts":"{_t.strftime("%Y-%m-%dT%H:%M:%SZ", _t.gmtime())}",'
                f'"event":"gt-edit-state-no-graph-db","note":"silent no-op"}}\n'
            )
    except Exception:
        pass
    return ""


# ---------------------------------------------------------------------------
# Atomic file writes (mirrors gt_track4_pre_run._atomic_write)
# ---------------------------------------------------------------------------

def _atomic_write(path: Path, data: str) -> None:
    """Write data to path atomically with fsync."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".tmp.{os.getpid()}")
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(data)
        fh.flush()
        try:
            os.fsync(fh.fileno())
        except OSError:
            pass
    os.replace(tmp, path)


def _append_line(path: Path, line: str) -> None:
    """Append a line atomically (best-effort fsync)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if not line.endswith("\n"):
        line = line + "\n"
    with open(path, "a", encoding="utf-8", newline="\n") as fh:
        fh.write(line)
        fh.flush()
        try:
            os.fsync(fh.fileno())
        except OSError:
            pass


def _sha256_file(path: Path) -> str:
    """Compute SHA-256 of a file. Returns "" on read error."""
    try:
        h = hashlib.sha256()
        with open(path, "rb") as fh:
            while True:
                chunk = fh.read(65536)
                if not chunk:
                    break
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return ""


# ---------------------------------------------------------------------------
# Baseline tracking — per-instance map of {path: sha256} we've already
# processed. New or changed entries are L3+L6 candidates; unchanged entries
# are no-op'd.
# ---------------------------------------------------------------------------

def _baseline_path(log_dir: Path) -> Path:
    return log_dir / _BASELINE_BASENAME


def _load_baseline(log_dir: Path) -> dict[str, str]:
    p = _baseline_path(log_dir)
    if not p.is_file():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return {str(k): str(v) for k, v in data.items()}
    except (OSError, json.JSONDecodeError):
        pass
    return {}


def _save_baseline(log_dir: Path, baseline: dict[str, str]) -> None:
    _atomic_write(_baseline_path(log_dir), json.dumps(baseline))


# ---------------------------------------------------------------------------
# Edit counter — persistent NNN suffix for edit_*.json filenames.
# ---------------------------------------------------------------------------

def _counter_path(log_dir: Path) -> Path:
    return log_dir / ".edit_counter"


def _next_counter(log_dir: Path) -> int:
    p = _counter_path(log_dir)
    n = 0
    if p.is_file():
        try:
            n = int(p.read_text(encoding="utf-8").strip() or "0")
        except (OSError, ValueError):
            n = 0
    n += 1
    _atomic_write(p, str(n))
    return n


# ---------------------------------------------------------------------------
# Workspace inspection — what source files changed since last call?
# ---------------------------------------------------------------------------

def _is_test_path(rel: str) -> bool:
    """Same predicate used by edit_predicates._is_test_path; duplicated here
    to keep this module independently importable in the SWE-agent container
    (where we don't ship src/groundtruth/)."""
    import re as _re
    return bool(_re.search(
        r"(^|/)(tests?|__tests__|spec|specs)/|(^|/)test_[^/]*\.py$|(^|/)[^/]*_test\.py$",
        rel.replace("\\", "/"),
    ))


def _list_changed_source_files(repo_root: Path) -> list[str]:
    """Use git to enumerate working-tree source files that may have changed.

    Returns repo-relative paths to files whose extension is in _SOURCE_EXTS,
    that are NOT test files, and that exist on disk. Includes:
      - Modified tracked files (git diff --name-only)
      - Newly-created untracked files (git ls-files --others --exclude-standard)

    Falls back to a full scan if git is unavailable (synthetic tests).
    """
    paths: list[str] = []

    if not repo_root.is_dir():
        return paths

    git_dir = repo_root / ".git"
    use_git = git_dir.exists()

    if use_git:
        try:
            tracked_diff = subprocess.run(
                ["git", "diff", "--name-only", "HEAD"],
                cwd=str(repo_root),
                capture_output=True,
                text=True,
                timeout=10,
            )
            untracked = subprocess.run(
                ["git", "ls-files", "--others", "--exclude-standard"],
                cwd=str(repo_root),
                capture_output=True,
                text=True,
                timeout=10,
            )
            for line in (tracked_diff.stdout or "").splitlines():
                line = line.strip()
                if line:
                    paths.append(line)
            for line in (untracked.stdout or "").splitlines():
                line = line.strip()
                if line:
                    paths.append(line)
        except (OSError, subprocess.SubprocessError):
            use_git = False

    if not use_git:
        # Fallback: scan every source file. Only used in synthetic tests
        # where the workspace isn't a git repo.
        for ext in _SOURCE_EXTS:
            for p in repo_root.rglob(f"*{ext}"):
                if p.is_file():
                    paths.append(str(p.relative_to(repo_root)).replace("\\", "/"))

    # Filter: source ext, not test, and exists on disk.
    out: list[str] = []
    seen: set[str] = set()
    for raw in paths:
        norm = raw.replace("\\", "/")
        if norm in seen:
            continue
        seen.add(norm)
        if not norm.endswith(_SOURCE_EXTS):
            continue
        if _is_test_path(norm):
            continue
        full = repo_root / norm
        if not full.is_file():
            continue
        out.append(norm)
    return out


def _detect_new_edits(
    repo_root: Path,
    baseline: dict[str, str],
) -> tuple[list[str], dict[str, str]]:
    """Compare current source-file state against baseline.

    Returns ``(changed_paths, new_baseline)`` — changed_paths lists files
    whose sha differs from baseline (or that are new); new_baseline is the
    updated map to persist.
    """
    candidates = _list_changed_source_files(repo_root)
    new_baseline = dict(baseline)
    changed: list[str] = []
    for rel in candidates:
        full = repo_root / rel
        cur = _sha256_file(full)
        if not cur:
            continue
        if baseline.get(rel) == cur:
            continue
        changed.append(rel)
        new_baseline[rel] = cur
    return changed, new_baseline


# ---------------------------------------------------------------------------
# L6 fire — gt-index -file
# ---------------------------------------------------------------------------

def _gt_index_bin() -> str:
    """RC-13: portable gt-index resolution.

    Resolution order (first hit wins):
      1. ``GT_INDEX_BIN`` env (explicit path).
      2. Bundle-relative ``<bundle_root>/bin/gt-index`` — what gt_track4.yaml
         publishes as the canonical container path.
      3. ``shutil.which("gt-index")`` — operator may install a static binary
         on PATH for portability across containers.
      4. In-tree dev path on Windows for local smoke runs.

    Raises ``KeyError`` if none resolve. NO silent ``/home/ubuntu/...``
    fallback — that masked VM-cutover bugs (cf. RC-13/A-007).
    """
    import shutil as _shutil  # local import keeps top-of-module diff minimal

    if (env := os.environ.get("GT_INDEX_BIN")):
        return env
    bundle_root = Path(__file__).resolve().parent.parent  # lib/.. -> bundle root
    bundle_bin = bundle_root / "bin" / (
        "gt-index.exe" if sys.platform == "win32" else "gt-index"
    )
    if bundle_bin.is_file():
        return str(bundle_bin)
    which = _shutil.which("gt-index")
    if which:
        return which
    if _REPO_ROOT_HOST is not None and sys.platform == "win32":
        dev = _REPO_ROOT_HOST / "gt-index" / "gt-index.exe"
        if dev.is_file():
            return str(dev)
    raise KeyError(
        "gt-index binary not resolvable: set GT_INDEX_BIN, ship the "
        "bundle's bin/gt-index, install on PATH, or build the in-tree "
        "Windows binary. RC-13 removed the /home/ubuntu/... fallback."
    )


# RC-13: probe loader compatibility once per process. A 47MB glibc-3.2.0+
# ELF will fail silently on musl/Alpine/older-CentOS containers, and the
# downstream symptom is "L6 always 0" — easy to mis-attribute to the agent
# not editing source. The probe runs `ldd` once and caches the verdict.
# When the loader is incompatible we DO NOT crash — we set a sentinel that
# `_fire_gt_index_file` reads to short-circuit with a structured telemetry
# record (`error="binary_loader_incompatible"`) so the audit can tell
# loader-incompatibility apart from agent-didn't-edit.
_LOADER_PROBE_CACHE: dict[str, str | None] = {}


def _probe_binary_loadable(bin_path: str) -> tuple[bool, str]:
    """Return ``(ok, detail)`` for whether ``bin_path`` is loadable here.

    Uses ``ldd`` on Linux. On non-Linux (Windows dev path) returns
    ``(True, "non-linux")`` unconditionally. Cached per-bin_path for the
    process lifetime — ``ldd`` itself is cheap but we get called on every
    state-command invocation, and a fresh cache miss shouldn't slow the
    agent loop.

    Detail is the first error line from ldd or a sentinel string. Callers
    log this to gt_reindex.jsonl on failure so post-hoc analysis can
    distinguish loader bugs from agent inactivity.
    """
    if bin_path in _LOADER_PROBE_CACHE:
        cached = _LOADER_PROBE_CACHE[bin_path]
        return (cached is None, cached or "ok")
    if sys.platform != "linux":
        _LOADER_PROBE_CACHE[bin_path] = None
        return True, "non-linux"
    if not os.path.exists(bin_path):
        _LOADER_PROBE_CACHE[bin_path] = "binary_missing"
        return False, "binary_missing"
    try:
        proc = subprocess.run(
            ["ldd", bin_path],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        # No ldd (statically linked binary on a stripped image)? Treat as
        # OK — a static binary IS the goal. The exact error from ldd on a
        # static binary is platform-specific ("not a dynamic executable")
        # and we'd already see a hard ENOENT/PermError above.
        _LOADER_PROBE_CACHE[bin_path] = None
        return True, f"ldd_unavailable:{type(exc).__name__}"
    out = (proc.stdout or "") + (proc.stderr or "")
    out_low = out.lower()
    # Common incompatibility markers:
    #   "not found" — required SO missing on this libc
    #   "version `glibc_x.y' not found" — libc too old
    #   "no such file" / "cannot execute" — interpreter mismatch (musl/glibc)
    bad_markers = (
        "not found",
        "no such file",
        "cannot execute",
        "version `glibc_",
        "version 'glibc_",
        "wrong elf class",
    )
    for m in bad_markers:
        if m in out_low:
            detail = out.strip().splitlines()[0][:200] if out.strip() else m
            _LOADER_PROBE_CACHE[bin_path] = f"loader_incompat:{detail}"
            return False, f"loader_incompat:{detail}"
    # ldd may exit non-zero on a static binary — proc.returncode alone is
    # not reliable. Trust the output scan.
    _LOADER_PROBE_CACHE[bin_path] = None
    return True, "ok"


def _index_timeout_s() -> float:
    """RC-15: env-configurable gt-index timeout (default 15s).

    5s was too tight at n=300 — 10K-line files regularly exceed it and
    silently emit `error="timeout after 5s"` records that the L6 counter
    then counts as "fired". Calibrate via `GT_INDEX_TIMEOUT_S`.
    """
    try:
        return max(1.0, float(os.environ.get("GT_INDEX_TIMEOUT_S", "15")))
    except (TypeError, ValueError):
        return 15.0


def _hook_timeout_s() -> float:
    """RC-15: env-configurable gt_hook timeout (default 60s).

    30s was too tight at n=300 — full briefing on hot symbols regularly
    exceeded it. Calibrate via `GT_HOOK_TIMEOUT_S`.
    """
    try:
        return max(1.0, float(os.environ.get("GT_HOOK_TIMEOUT_S", "60")))
    except (TypeError, ValueError):
        return 60.0


def _fire_gt_index_file(
    repo_root: Path,
    rel_path: str,
    graph_db: str,
    timeout_s: float | None = None,
) -> dict[str, Any]:
    """Run `gt-index -file <rel_path>` and return its JSON output dict.

    Always returns a dict — adds `error`, `wall_ms`, `timestamp_ms` even on
    failure. The caller appends this dict (after json.dumps) to
    gt_reindex.jsonl. Track D's verifier counts the lines.
    """
    try:
        bin_path = _gt_index_bin()
    except KeyError as exc:
        # RC-13: explicit failure beats silent hardcoded fallback.
        return {
            "file": rel_path,
            "timestamp_ms": int(time.time() * 1000),
            "error": f"gt-index unresolved: {exc}",
            "binary_supports_file_flag": False,
        }
    if timeout_s is None:
        timeout_s = _index_timeout_s()
    t0 = time.time()
    rec: dict[str, Any] = {
        "file": rel_path,
        "timestamp_ms": int(t0 * 1000),
        "timeout_s": timeout_s,
    }
    # RC-13: probe loader compatibility once. A glibc-pinned binary on a
    # musl container will fail with a cryptic loader error; we emit a
    # structured telemetry signal so the verifier can tell apart "binary
    # incompatible" from "agent didn't edit".
    loadable, loader_detail = _probe_binary_loadable(bin_path)
    if not loadable:
        rec["error"] = f"binary_loader_incompatible:{loader_detail}"
        rec["binary_supports_file_flag"] = False
        rec["wall_ms"] = int((time.time() - t0) * 1000)
        # TODO(RC-13-build): once the static binary lands (CGO_ENABLED=0
        # or musl-gcc) this branch should never fire on supported
        # containers. Until then, downgrade L6 silently and continue.
        return rec
    # RC-15: cap CPU contention from concurrent gt-index rebuilds. 6 tasks
    # × NumCPU goroutines saturated 4-vCPU VMs at n=300. Caller may
    # override via env.
    sub_env = dict(os.environ)
    sub_env.setdefault("GOMAXPROCS", os.environ.get("GT_INDEX_GOMAXPROCS", "2"))
    try:
        proc = subprocess.run(
            [
                bin_path,
                "-root", str(repo_root),
                "-file", rel_path,
                "-output", graph_db,
            ],
            capture_output=True,
            text=True,
            timeout=timeout_s,
            env=sub_env,
        )
        wall_ms = int((time.time() - t0) * 1000)
        rec["wall_ms"] = wall_ms
        if proc.returncode != 0:
            rec["error"] = (
                proc.stderr.strip()[:300] or
                f"gt-index exit {proc.returncode}"
            )
            rec["binary_supports_file_flag"] = False
            return rec
        # Parse the last non-empty stdout line as JSON (per Track B0 spec
        # §11: "Print one JSON line: {file, nodes_replaced, edges_replaced,
        # duration_ms}").
        last = ""
        for line in (proc.stdout or "").splitlines():
            if line.strip():
                last = line.strip()
        if not last:
            rec["error"] = "empty stdout from gt-index"
            return rec
        try:
            payload = json.loads(last)
            if isinstance(payload, dict):
                rec.update(payload)
        except json.JSONDecodeError:
            rec["error"] = "non-json stdout"
            rec["raw_stdout"] = last[:300]
        return rec
    except subprocess.TimeoutExpired:
        rec["error"] = f"timeout after {timeout_s}s"
        return rec
    except FileNotFoundError:
        rec["error"] = f"gt-index binary not found at {bin_path}"
        return rec
    except Exception as exc:  # noqa: BLE001
        rec["error"] = f"{type(exc).__name__}: {exc}"
        return rec


# ---------------------------------------------------------------------------
# L3 fire — gt_hook.py
# ---------------------------------------------------------------------------

def _gt_hook_py() -> str:
    if (env := os.environ.get("GT_HOOK_PY")):
        return env
    if _REPO_ROOT_HOST is not None and sys.platform == "win32":
        return str(_REPO_ROOT_HOST / "benchmarks" / "swebench" / "gt_hook.py")
    return "/home/ubuntu/Groundtruth/benchmarks/swebench/gt_hook.py"


def _fire_gt_hook(
    repo_root: Path,
    rel_path: str,
    graph_db: str,
    timeout_s: float | None = None,
) -> dict[str, Any]:
    """Run gt_hook.py post-edit briefing and return a dict suitable for
    edit_NNN.json.

    Uses the `analyze` subcommand (post-edit v10 combined-signals brief —
    gt_hook.py:4101). Captures stdout; if empty / no hits, returns
    {"empty": true, "reason": ...}. Track A's stay-silent contract is
    preserved: empty payloads still produce a file (so the L3 counter
    increments) but the file flags itself as empty.
    """
    hook = _gt_hook_py()
    py = os.environ.get("GT_PYTHON", "python3")
    if timeout_s is None:
        timeout_s = _hook_timeout_s()
    t0 = time.time()
    rec: dict[str, Any] = {
        "file": rel_path,
        "timestamp_ms": int(t0 * 1000),
        "timeout_s": timeout_s,
    }
    env = dict(os.environ)
    # RC-05: pass --db <graph_db> so gt_hook analyze reads the SAME graph.db
    # the agent's tools (gt_query/gt_search/gt_navigate/gt_validate) consume
    # via gt_intel's evidence engine, instead of building a parallel AST
    # index at /tmp/gt_index.json. Empty graph_db is allowed — gt_hook will
    # fall back to its legacy AST path so we never crash a state command.
    cmd: list[str] = [
        py, hook, "analyze", rel_path,
        "--root", str(repo_root),
        "--quiet",
    ]
    if graph_db:
        cmd.extend(["--db", graph_db])
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            env=env,
        )
        rec["wall_ms"] = int((time.time() - t0) * 1000)
        out = (proc.stdout or "").strip()
        if proc.returncode != 0 and not out:
            rec["empty"] = True
            rec["reason"] = (
                "gt_hook exit "
                f"{proc.returncode}: "
                f"{(proc.stderr or '').strip()[:200]}"
            )
            return rec
        if not out:
            rec["empty"] = True
            rec["reason"] = "gt_hook produced no high-confidence evidence"
            return rec
        rec["brief"] = out
        rec["brief_lines"] = len(out.splitlines())
        return rec
    except subprocess.TimeoutExpired:
        rec["empty"] = True
        rec["reason"] = f"gt_hook timeout {timeout_s}s"
        return rec
    except FileNotFoundError:
        rec["empty"] = True
        rec["reason"] = f"gt_hook script not found at {hook}"
        return rec
    except Exception as exc:  # noqa: BLE001
        rec["empty"] = True
        rec["reason"] = f"{type(exc).__name__}: {exc}"
        return rec


# ---------------------------------------------------------------------------
# State.json read/write — SWE-agent contract
# ---------------------------------------------------------------------------

def _state_path() -> Path:
    return Path(os.environ.get("GT_STATE_PATH", _DEFAULT_STATE_PATH))


def _load_state() -> dict[str, Any]:
    p = _state_path()
    if not p.is_file():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
    except (OSError, json.JSONDecodeError):
        pass
    return {}


def _save_state(state: dict[str, Any]) -> None:
    # SWE-agent's TrajectoryStep declares `state: dict[str, str]` (verified
    # 2026-05-06 in sweagent/types.py). Pydantic enforces both keys AND values
    # as strings at trajectory-save time; any int/float crashes the run with
    # `Uncaught ValidationError: trajectory.N.state.X Input should be a valid
    # string` and prevents preds.json from being written. Coerce every value
    # to str here so non-string state additions (counters, etc.) don't trip
    # the schema. Keys are also stringified for symmetry / future-proofing.
    coerced = {str(k): "" if v is None else str(v) for k, v in state.items()}
    _atomic_write(_state_path(), json.dumps(coerced))


# ---------------------------------------------------------------------------
# gt_layers.partial writer — telemetry the smoke runner / Track D verifier
# stitches into the final composite [GT_LAYERS] line.
# ---------------------------------------------------------------------------

def _update_layers_partial(
    log_dir: Path,
    edit_count: int,
    reindex_count: int,
) -> None:
    """Write a single-line `gt_layers.partial` file with the L3/L6 counters.

    Format:
      L3=<n> L6=<n>

    Track D's smoke runner reads gt_layers.log (L1/L2 from Track A) and
    gt_layers.partial (L3/L6 from us) plus gt_query_calls.jsonl (L4) and
    gt_pre_finish_gate.json (L5) when emitting the final `[GT_LAYERS]`
    composite line. Splitting into a partial file avoids racing Track A's
    writer for the same file.
    """
    line = f"L3={edit_count} L6={reindex_count}\n"
    _atomic_write(log_dir / "gt_layers.partial", line)


# ---------------------------------------------------------------------------
# Public entry — one state-command invocation
# ---------------------------------------------------------------------------

def run_state(
    *,
    repo_root: Path,
    graph_db: str,
    log_dir: Path,
    forced_path: str | None = None,
    forced_tool_name: str | None = None,
    forced_args: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Run one state-command pass and return the resulting state dict.

    Inputs:
      repo_root      Workspace root (e.g. /workspace or /testbed).
      graph_db       Path to graph.db.
      log_dir        Per-instance log dir (gt_evidence/, gt_reindex.jsonl,
                     etc. land here).
      forced_path    For synthetic / unit tests: skip git-diff inspection
                     and pretend this path was just edited. None = use git.
      forced_tool_name, forced_args: also for synthetic tests; if both set
                     and forced_path is None, derive forced_path via
                     extract_edited_path(tool_name, args).

    Returns the merged state dict that was written to /root/state.json.
    Always contains `gt_evidence` (string, possibly empty), plus diagnostic
    keys `gt_edit_count`, `gt_reindex_count`.
    """
    log_dir = Path(log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    repo_root = Path(repo_root)

    # ---- 1. Detect edits ------------------------------------------------
    # When the caller provides forced_tool_name+forced_args, run the
    # predicate. This path is for synthetic tests / harness-bridge wrappers
    # that DO have action context (the git-diff fallback below is only used
    # when invoked by SWE-agent's state_command, which has no action info).
    forced_synthetic_mode = (
        forced_path is None
        and forced_tool_name is not None
        and forced_args is not None
    )
    if forced_synthetic_mode:
        if extract_edited_path is not None and is_source_edit is not None:
            if is_source_edit(forced_tool_name, forced_args):
                fp = extract_edited_path(forced_tool_name, forced_args)
                if fp:
                    # If absolute, strip workspace prefix; else use as-is.
                    p = Path(fp)
                    try:
                        forced_path = str(p.relative_to(repo_root)).replace("\\", "/")
                    except ValueError:
                        forced_path = str(fp).replace("\\", "/")
        # In synthetic mode, if the predicate said NO (or path extraction
        # failed), this is a deliberate negative case — silent no-op. Do
        # NOT fall through to the git-diff branch, which would produce a
        # huge false-positive when the synthetic test's repo contains
        # unrelated edits.
        if forced_path is None:
            return _emit_noop(log_dir)

    if forced_path is not None:
        if not forced_path:
            # Predicate said no — silent no-op.
            return _emit_noop(log_dir)
        changed = [forced_path]
        baseline = _load_baseline(log_dir)
        full = repo_root / forced_path
        sha = _sha256_file(full)
        # Short-circuit detection — if sha matches baseline, this is a
        # repeated invocation on the same file with no real change.
        if baseline.get(forced_path) == sha and sha:
            # Still fire L6 (so its short_circuited:true line lands), but
            # skip L3 — the hook would produce identical evidence.
            return _process_changes(
                log_dir, repo_root, graph_db, changed, baseline,
                skip_hook=True,
            )
        if sha:
            baseline[forced_path] = sha
        return _process_changes(
            log_dir, repo_root, graph_db, changed, baseline,
        )

    # Default path — git-diff inspection.
    baseline = _load_baseline(log_dir)
    changed, new_baseline = _detect_new_edits(repo_root, baseline)
    if not changed:
        return _emit_noop(log_dir)
    return _process_changes(
        log_dir, repo_root, graph_db, changed, new_baseline,
    )


def _emit_noop(log_dir: Path) -> dict[str, Any]:
    """No edits detected — preserve existing state, just refresh counters."""
    state = _load_state()
    edit_count = _read_counter(log_dir)
    reindex_count = _count_reindex_lines(log_dir)
    state["gt_evidence"] = state.get("gt_evidence", "")
    state["gt_edit_count"] = edit_count
    state["gt_reindex_count"] = reindex_count
    _save_state(state)
    _update_layers_partial(log_dir, edit_count, reindex_count)
    return state


def _process_changes(
    log_dir: Path,
    repo_root: Path,
    graph_db: str,
    changed: list[str],
    new_baseline: dict[str, str],
    *,
    skip_hook: bool = False,
) -> dict[str, Any]:
    """Fire L6 + L3 for each changed path and update state."""
    reindex_log = log_dir / "gt_reindex.jsonl"
    evidence_dir = log_dir / "gt_evidence"
    evidence_dir.mkdir(parents=True, exist_ok=True)

    last_brief = ""
    last_path = ""

    for rel in changed:
        # ---- L6 -----
        rec = _fire_gt_index_file(repo_root, rel, graph_db)
        _append_line(reindex_log, json.dumps(rec))

        # ---- L3 -----
        if skip_hook:
            continue
        n = _next_counter(log_dir)
        hook_rec = _fire_gt_hook(repo_root, rel, graph_db)
        out_path = evidence_dir / f"edit_{n:03d}.json"
        _atomic_write(out_path, json.dumps(hook_rec))
        if hook_rec.get("brief"):
            last_brief = str(hook_rec["brief"])
            last_path = rel

    _save_baseline(log_dir, new_baseline)

    edit_count = _read_counter(log_dir)
    reindex_count = _count_reindex_lines(log_dir)
    _update_layers_partial(log_dir, edit_count, reindex_count)

    state = _load_state()
    if last_brief:
        # RC-15: cap injected brief at 10 lines (env-overridable) — the full
        # 35-line brief steals ~5% input window per 5 edits at n=300.
        # Recommend gt_query for full detail.
        try:
            brief_cap = max(1, int(os.environ.get("GT_EVIDENCE_LINE_CAP", "10")))
        except (TypeError, ValueError):
            brief_cap = 10
        brief_lines = last_brief.splitlines()
        truncated = len(brief_lines) > brief_cap
        capped = "\n".join(brief_lines[:brief_cap])
        if truncated:
            capped += (
                f"\n... (+{len(brief_lines) - brief_cap} more lines truncated; "
                f"run `gt_query` for full brief)"
            )
        # Format expected by SWE-agent's instance_template `{{gt_evidence}}`:
        # a single string the agent sees in the next prompt turn.
        state["gt_evidence"] = (
            f"<gt-evidence file={last_path!r}>\n{capped}\n</gt-evidence>"
        )
    else:
        state["gt_evidence"] = state.get("gt_evidence", "")
    state["gt_edit_count"] = edit_count
    state["gt_reindex_count"] = reindex_count
    _save_state(state)
    return state


def _read_counter(log_dir: Path) -> int:
    p = _counter_path(log_dir)
    if not p.is_file():
        return 0
    try:
        return int(p.read_text(encoding="utf-8").strip() or "0")
    except (OSError, ValueError):
        return 0


def _count_reindex_lines(log_dir: Path) -> int:
    p = log_dir / "gt_reindex.jsonl"
    if not p.is_file():
        return 0
    try:
        return sum(1 for line in p.read_text(encoding="utf-8").splitlines() if line.strip())
    except OSError:
        return 0


# ---------------------------------------------------------------------------
# CLI — invoked by bin/_state_gt_edit at SWE-agent state time, or by
# verification-step-C smoke directly.
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    import argparse
    p = argparse.ArgumentParser(prog="gt_edit_state")
    p.add_argument("--repo-root", default=None,
                   help="Override repo root (else $GT_REPO_ROOT, $ROOT, /workspace)")
    p.add_argument("--graph-db", default=None,
                   help="Override graph.db path (else $GT_GRAPH_DB)")
    p.add_argument("--log-dir", default=None,
                   help="Override per-instance log dir (else $GT_INSTANCE_LOG_DIR)")
    p.add_argument("--forced-path", default=None,
                   help="Synthetic-test mode: pretend this path was edited")
    p.add_argument("--forced-tool-name", default=None,
                   help="Synthetic-test: derive forced_path via predicate")
    p.add_argument("--forced-args-json", default=None,
                   help="Synthetic-test: JSON dict of args for predicate")
    args = p.parse_args(argv)

    repo_root = (
        args.repo_root
        or os.environ.get("GT_REPO_ROOT")
        or os.environ.get("ROOT")
        or os.getcwd()
    )
    graph_db = (
        args.graph_db
        or os.environ.get("GT_GRAPH_DB")
        or _resolve_graph_db_no_build()
    )
    log_dir = args.log_dir or os.environ.get("GT_INSTANCE_LOG_DIR", "")
    if not graph_db:
        print("gt_edit_state: GT_GRAPH_DB unset — silent no-op", file=sys.stderr)
        return 0
    if not log_dir:
        print("gt_edit_state: GT_INSTANCE_LOG_DIR unset — silent no-op",
              file=sys.stderr)
        return 0

    forced_args: dict[str, Any] | None = None
    if args.forced_args_json:
        try:
            forced_args = json.loads(args.forced_args_json)
            if not isinstance(forced_args, dict):
                forced_args = None
        except json.JSONDecodeError:
            forced_args = None

    try:
        run_state(
            repo_root=Path(repo_root),
            graph_db=graph_db,
            log_dir=Path(log_dir),
            forced_path=args.forced_path,
            forced_tool_name=args.forced_tool_name,
            forced_args=forced_args,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"gt_edit_state: error: {exc}", file=sys.stderr)
        return 0  # never break the agent loop
    return 0


if __name__ == "__main__":
    sys.exit(main())
