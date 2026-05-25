"""Pytest suite for L6 ``gt-index -file`` incremental reindex mode.

Targets the Go binary at ``D:/Groundtruth/gt-index/gt-index.exe`` (Windows
local) or ``/home/ubuntu/Groundtruth/gt-index/gt-index`` (Linux on gt-t0).
Mode under test::

    gt-index -root <repo> -file <relative_path> -output <graph.db>

The `-file` mode performs a transactional delete-and-replace of one file's
nodes/edges in an existing graph.db, with:

  * SHA-256 hash short-circuit via the ``file_hashes`` table.
  * Snapshot+restore of incoming cross-file edges (B0 follow-up).
  * Outgoing edges re-resolved against the rest of the DB.
  * Per-file orphan-edge invariant preserved.

This harness drives the binary as a subprocess and asserts behaviour from
the JSON line written to stdout plus direct SQL queries against the
resulting graph.db.

Anti-benchmaxxing
-----------------
Fixture ``tests/layers/fixtures/repo_for_reindex`` is a synthetic 5-file
toolkit (``widgets.py``, ``layout.py``, ``store.py``, ``events.py``,
``app.py``). No SWE-bench, SWE-bench-Live, Live-Lite, or
benchmark-specific module names. Cross-file calls are wired solely to
exercise incoming-edge snapshot/restore in the indexer.
"""
from __future__ import annotations

import json
import os
import platform
import shutil
import sqlite3
import statistics
import subprocess
import sys
import time
from pathlib import Path

import pytest

# ── Locate gt-index binary ───────────────────────────────────────────────────
_REPO_ROOT = Path(__file__).resolve().parents[2]
_FIXTURE_SRC = _REPO_ROOT / "tests" / "layers" / "fixtures" / "repo_for_reindex"

# Binary candidates, in priority order. The first one that supports -file mode
# wins. The local Windows .exe in the repo (Apr 2026 build) is older than the
# Linux binary at tools/sweagent/gt_edit/bin/gt-index and lacks `-file`.
_BIN_CANDIDATES_LOCAL = [
    _REPO_ROOT / "gt-index" / "gt-index.exe",
    _REPO_ROOT / "gt-index" / "gt-index",
    _REPO_ROOT / "tools" / "sweagent" / "gt_edit" / "bin" / "gt-index",
]


def _binary_supports_file_flag(binary: Path) -> bool:
    """Probe ``-help`` output for the ``-file`` flag.

    The binary writes flag help to stderr (Go ``flag`` package default).
    We skip silently on any execution error: the binary may be a
    cross-platform mismatch (e.g. Linux ELF on Windows host).
    """
    try:
        proc = subprocess.run(
            [str(binary), "-help"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    blob = (proc.stdout or "") + (proc.stderr or "")
    return "-file" in blob and "Incremental" in blob


def _find_local_binary() -> Path | None:
    for cand in _BIN_CANDIDATES_LOCAL:
        if cand.exists() and _binary_supports_file_flag(cand):
            return cand
    return None


# Remote fallback for hosts without a working local binary. Requires
# gcloud-authenticated access to the gt-t0 VM.
_REMOTE_HOST = os.environ.get("GT_INDEX_REMOTE_HOST", "gt-t0")
_REMOTE_ZONE = os.environ.get("GT_INDEX_REMOTE_ZONE", "us-central1-a")
_REMOTE_BIN = os.environ.get(
    "GT_INDEX_REMOTE_BIN", "/home/ubuntu/Groundtruth/gt-index/gt-index"
)


def _gcloud_exe() -> str | None:
    """Locate the gcloud executable.

    On Windows the launcher is ``gcloud.cmd``; ``shutil.which("gcloud")``
    returns a no-extension stub that ``CreateProcess`` cannot exec
    directly. Prefer ``gcloud.cmd`` on Windows; fall back to whatever
    ``which`` returns elsewhere.
    """
    if platform.system() == "Windows":
        cand = shutil.which("gcloud.cmd")
        if cand:
            return cand
    return shutil.which("gcloud")


_GCLOUD = _gcloud_exe()


def _remote_available() -> bool:
    """Cheap probe: ``gcloud`` on PATH and the host binary exists."""
    if _GCLOUD is None:
        return False
    try:
        proc = subprocess.run(
            [
                _GCLOUD,
                "compute",
                "ssh",
                _REMOTE_HOST,
                f"--zone={_REMOTE_ZONE}",
                "--command",
                f"test -x {_REMOTE_BIN}",
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0


_LOCAL_BIN = _find_local_binary()
_USE_REMOTE = _LOCAL_BIN is None and _remote_available()


# ── Cross-platform runner ────────────────────────────────────────────────────
class GtIndexRunner:
    """Drives ``gt-index`` either locally or via gcloud SSH on gt-t0.

    Each test gets its own runner instance bound to a fresh workdir
    containing a copy of the fixture. The runner exposes:

      * ``run(*args)`` — invoke the binary; returns ``CompletedProcess``.
      * ``run_incremental(file)`` — convenience for ``-file`` mode; returns
        the parsed JSON stdout dict (or raises if exit nonzero).
      * ``modify_file(rel, fn)`` — read+rewrite a fixture file.
      * ``open_db()`` — return a sqlite3 connection to the local copy of
        graph.db (downloaded from remote first if needed).
    """

    def __init__(self, tmp_path: Path) -> None:
        self.tmp_path = tmp_path
        self.local_repo = tmp_path / "repo"
        self.local_db = tmp_path / "graph.db"
        shutil.copytree(_FIXTURE_SRC, self.local_repo)
        self._remote_workdir: str | None = None
        if _USE_REMOTE:
            self._remote_workdir = (
                f"/tmp/gt_l6_test_{os.getpid()}_{int(time.time()*1000)}"
            )
            self._remote_setup()

    # — Remote helpers ——————————————————————————————————————————————————————
    def _remote_setup(self) -> None:
        # Tar fixture and scp it; faster than per-file scp.
        tar_path = self.tmp_path / "repo.tar"
        with __import__("tarfile").open(tar_path, "w") as tf:
            for p in sorted(self.local_repo.rglob("*")):
                if p.is_file():
                    tf.add(p, arcname=p.relative_to(self.local_repo))
        # Create remote dir and push tar.
        subprocess.run(
            [
                _GCLOUD,
                "compute",
                "ssh",
                _REMOTE_HOST,
                f"--zone={_REMOTE_ZONE}",
                "--command",
                f"mkdir -p {self._remote_workdir}/repo",
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=60,
        )
        subprocess.run(
            [
                _GCLOUD,
                "compute",
                "scp",
                f"--zone={_REMOTE_ZONE}",
                str(tar_path),
                f"{_REMOTE_HOST}:{self._remote_workdir}/repo.tar",
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=120,
        )
        subprocess.run(
            [
                _GCLOUD,
                "compute",
                "ssh",
                _REMOTE_HOST,
                f"--zone={_REMOTE_ZONE}",
                "--command",
                (
                    f"cd {self._remote_workdir}/repo && "
                    f"tar -xf {self._remote_workdir}/repo.tar"
                ),
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=60,
        )

    def _remote_run(self, cmd: str, timeout: int = 120) -> subprocess.CompletedProcess:
        return subprocess.run(
            [
                _GCLOUD,
                "compute",
                "ssh",
                _REMOTE_HOST,
                f"--zone={_REMOTE_ZONE}",
                "--command",
                cmd,
            ],
            capture_output=True,
            text=True,
            timeout=timeout,
        )

    def _remote_push_file(self, rel: str) -> None:
        local = self.local_repo / rel
        subprocess.run(
            [
                _GCLOUD,
                "compute",
                "scp",
                f"--zone={_REMOTE_ZONE}",
                str(local),
                f"{_REMOTE_HOST}:{self._remote_workdir}/repo/{rel}",
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=60,
        )

    def _remote_pull_db(self) -> None:
        # Pull each time to a unique filename (avoids cross-run lock issues
        # when pytest reuses ``tmp_path-of-USER`` directories across sessions
        # and prior leftover ``graph.db`` is held by AV / cached handle).
        # Caller (open_db) sees the pulled file via ``self.local_db``.
        self._pull_counter = getattr(self, "_pull_counter", 0) + 1
        unique = self.tmp_path / f"_pulled_{self._pull_counter}_{int(time.time()*1000)}.db"
        unique.parent.mkdir(parents=True, exist_ok=True)
        # Use forward-slash path: gcloud.cmd → pscp.exe occasionally rejects
        # backslash drive-letter paths with "Cannot create file" depending
        # on dispatch state.
        target = str(unique).replace("\\", "/")
        proc = subprocess.run(
            [
                _GCLOUD,
                "compute",
                "scp",
                f"--zone={_REMOTE_ZONE}",
                f"{_REMOTE_HOST}:{self._remote_workdir}/graph.db",
                target,
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if proc.returncode != 0:
            raise AssertionError(
                f"gcloud scp pull failed (rc={proc.returncode}):\n"
                f"STDOUT: {proc.stdout}\nSTDERR: {proc.stderr}\n"
                f"workdir={self._remote_workdir} target={target}\n"
                f"parent_exists={unique.parent.exists()}"
            )
        # Point local_db at the freshly-pulled unique file.
        self.local_db = unique

    # — Public API ————————————————————————————————————————————————————————
    def run(self, *args: str, expect_zero: bool = True, timeout: int = 120) -> subprocess.CompletedProcess:
        """Invoke gt-index. Returns CompletedProcess with stdout/stderr."""
        if _USE_REMOTE:
            quoted = " ".join(_shquote(a) for a in args)
            cmd = f"cd {self._remote_workdir} && {_REMOTE_BIN} {quoted}"
            proc = self._remote_run(cmd, timeout=timeout)
        else:
            assert _LOCAL_BIN is not None
            proc = subprocess.run(
                [str(_LOCAL_BIN), *args],
                cwd=str(self.tmp_path),
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        if expect_zero and proc.returncode != 0:
            raise AssertionError(
                f"gt-index {args} failed (rc={proc.returncode}):\n"
                f"STDOUT: {proc.stdout}\nSTDERR: {proc.stderr}"
            )
        return proc

    def full_build(self) -> dict:
        proc = self.run("-root", "repo", "-output", "graph.db")
        last_line = _last_json_line(proc.stdout)
        return last_line

    def run_incremental(self, rel: str) -> dict:
        proc = self.run(
            "-root", "repo", "-file", rel, "-output", "graph.db",
        )
        return _last_json_line(proc.stdout)

    def run_incremental_raw(self, rel: str) -> subprocess.CompletedProcess:
        return self.run(
            "-root", "repo", "-file", rel, "-output", "graph.db",
            expect_zero=False,
        )

    def modify_file(self, rel: str, transform) -> None:
        path = self.local_repo / rel
        text = path.read_text(encoding="utf-8")
        new_text = transform(text)
        # Sanity: must actually mutate to break the hash short-circuit.
        if new_text == text:
            new_text = text + "\n# bump\n"
        path.write_text(new_text, encoding="utf-8")
        if _USE_REMOTE:
            self._remote_push_file(rel)

    def open_db(self) -> sqlite3.Connection:
        if _USE_REMOTE:
            self._remote_pull_db()
        else:
            # Local mode: gt-index wrote graph.db into self.tmp_path (cwd).
            # ``self.local_db`` already points there. No copy needed.
            assert self.local_db.exists(), "graph.db missing after local build"
        return sqlite3.connect(str(self.local_db))

    def cleanup(self) -> None:
        if _USE_REMOTE and self._remote_workdir:
            try:
                self._remote_run(f"rm -rf {self._remote_workdir}", timeout=30)
            except Exception:
                pass


def _shquote(arg: str) -> str:
    """Single-quote for POSIX shell when shipping commands over SSH."""
    if not arg or any(c in arg for c in " \t\n\"'\\$`!"):
        return "'" + arg.replace("'", "'\"'\"'") + "'"
    return arg


def _last_json_line(stdout: str) -> dict:
    """gt-index prints exactly one JSON line; full builds may print warnings.

    We pick the last line that parses as JSON.
    """
    for line in reversed([ln for ln in stdout.splitlines() if ln.strip()]):
        try:
            return json.loads(line)
        except json.JSONDecodeError:
            continue
    raise AssertionError(f"no JSON line in stdout:\n{stdout}")


# ── Module-level skip if neither path is available ───────────────────────────
pytestmark = pytest.mark.skipif(
    _LOCAL_BIN is None and not _USE_REMOTE,
    reason=(
        "no gt-index binary with -file mode available: local Windows .exe "
        "in repo is too old, and remote gt-t0 is not reachable via gcloud. "
        "Set GT_INDEX_REMOTE_HOST/_ZONE or rebuild gt-index/gt-index.exe."
    ),
)


# ── Fixtures ─────────────────────────────────────────────────────────────────
@pytest.fixture()
def runner(tmp_path: Path):
    r = GtIndexRunner(tmp_path)
    try:
        yield r
    finally:
        r.cleanup()


@pytest.fixture()
def built_runner(runner: GtIndexRunner):
    """Runner with a baseline full build already executed."""
    runner.full_build()
    return runner


# ── Tests ────────────────────────────────────────────────────────────────────
def test_full_build_baseline(runner: GtIndexRunner):
    """Sanity: full build produces nodes + edges in the expected ballpark."""
    summary = runner.full_build()
    # Files counted = 5 .py source files in the fixture.
    assert summary["files"] == 5, summary
    assert summary["nodes"] > 10, summary
    assert summary["edges"] > 0, summary
    # Persist baseline counts for downstream invariant checks.
    with runner.open_db() as conn:
        n = conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]
        e = conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
    assert n == summary["nodes"]
    assert e == summary["edges"]


def test_hash_match_shortcircuit(built_runner: GtIndexRunner):
    """Re-invoking -file on an unchanged file is a sub-10ms no-op.

    The full build does NOT populate ``file_hashes`` (verified against the
    indexer source: only ``InsertFileHashTx`` from runIncremental writes it).
    So the contract requires one priming ``-file`` call to seed the hash;
    the *second* call on the same untouched file is the one that
    short-circuits.
    """
    # Priming call: writes the SHA-256 hash row for widgets.py.
    primed = built_runner.run_incremental("widgets.py")
    assert primed["short_circuited"] is False, (
        "priming call must do real work to seed file_hashes: " + str(primed)
    )

    # Second call on identical content → hash matches → short-circuit.
    out = built_runner.run_incremental("widgets.py")
    assert out["short_circuited"] is True, out
    assert out["nodes_replaced"] == 0, out
    assert out["edges_replaced"] == 0, out
    assert out["incoming_restored"] == 0, out
    assert out["incoming_unresolved"] == 0, out
    # Sub-10ms is the contract; the binary's runIncremental short-circuit
    # path does open(db) + one SELECT + read+sha256 of one tiny file.
    assert out["duration_ms"] <= 10, f"hash short-circuit too slow: {out}"


def test_real_reparse(built_runner: GtIndexRunner):
    """Modifying a file forces a real reparse: replaced > 0, not short-circuited."""

    def add_helper(text: str) -> str:
        # Append a brand-new function. Adds one node and (potentially) edges.
        return text + (
            "\n\ndef extra_helper(x):\n"
            "    return make_button(str(x))\n"
        )

    built_runner.modify_file("widgets.py", add_helper)
    out = built_runner.run_incremental("widgets.py")
    assert out["short_circuited"] is False, out
    assert out["nodes_replaced"] > 0, out
    assert out["edges_replaced"] > 0, out
    # A real reparse + tx commit + write ≫ 0 ms; allow >0 not >10 to avoid
    # platform flake on very fast machines (the contract is "real work
    # happened"; sub-ms is implausible only for the short-circuit path).
    assert out["duration_ms"] >= 1, out


def test_orphan_edge_assertion(built_runner: GtIndexRunner):
    """After reparse, no edge dangles to a missing node."""
    built_runner.modify_file(
        "layout.py",
        lambda t: t.replace("def build_window()", "def build_window2()"),
    )
    built_runner.run_incremental("layout.py")
    with built_runner.open_db() as conn:
        orphans = conn.execute(
            """
            SELECT COUNT(*) FROM edges
             WHERE source_id NOT IN (SELECT id FROM nodes)
                OR target_id NOT IN (SELECT id FROM nodes)
            """
        ).fetchone()[0]
    assert orphans == 0


def test_incoming_edges_restored(built_runner: GtIndexRunner):
    """Modify a file minimally (preserve all symbol names) → all incoming edges restored."""
    # Count incoming cross-file edges into widgets.py BEFORE.
    with built_runner.open_db() as conn:
        before = _count_incoming(conn, "widgets.py")
    assert before > 0, "fixture invariant: layout.py must call into widgets.py"

    # Touch only a comment / whitespace inside an existing function body.
    # All defs preserved → ResolveIncomingEdgesTx must restore everything.
    def minimal_edit(text: str) -> str:
        marker = '    return {"kind": "button", "label": label}'
        replacement = (
            "    # minimal-edit marker for L6 reindex test\n" + marker
        )
        assert marker in text, "fixture drift: widgets.py make_button body changed"
        return text.replace(marker, replacement, 1)

    built_runner.modify_file("widgets.py", minimal_edit)
    out = built_runner.run_incremental("widgets.py")
    assert out["short_circuited"] is False, out
    assert out["incoming_restored"] == before, (out, before)
    assert out["incoming_unresolved"] == 0, out

    # And the count of incoming edges in the DB after must match before.
    with built_runner.open_db() as conn:
        after = _count_incoming(conn, "widgets.py")
    assert after == before


def test_incoming_edges_partially_unresolved(built_runner: GtIndexRunner):
    """Renaming a callee → some incoming edges become unresolvable but pipeline survives."""
    # Snapshot incoming edge count for store.py (events.py imports + calls
    # into store.py: Store, make_default_store).
    with built_runner.open_db() as conn:
        before = _count_incoming(conn, "store.py")
    assert before > 0, "fixture invariant: events.py must call into store.py"

    # Rename make_default_store → make_seeded_store. Callers still reference
    # the old name → those incoming edges become unresolvable.
    def rename_def(text: str) -> str:
        return text.replace(
            "def make_default_store()", "def make_seeded_store()", 1
        )

    built_runner.modify_file("store.py", rename_def)
    out = built_runner.run_incremental("store.py")
    assert out["short_circuited"] is False, out
    assert out["incoming_unresolved"] >= 1, out
    # Pipeline must not have crashed: nodes/edges replaced > 0.
    assert out["nodes_replaced"] > 0, out

    # And no orphan edges introduced.
    with built_runner.open_db() as conn:
        orphans = conn.execute(
            """
            SELECT COUNT(*) FROM edges
             WHERE source_id NOT IN (SELECT id FROM nodes)
                OR target_id NOT IN (SELECT id FROM nodes)
            """
        ).fetchone()[0]
    assert orphans == 0


def test_p95_under_500ms(built_runner: GtIndexRunner):
    """Reparse each of the 5 files; p95 of duration_ms must be < 500."""
    files = ["widgets.py", "layout.py", "store.py", "events.py", "app.py"]
    durations: list[int] = []
    for i, rel in enumerate(files):
        # Force a real reparse each time — append a unique benign comment
        # so the SHA-256 short-circuit doesn't kick in.
        built_runner.modify_file(
            rel, lambda t, n=i: t + f"\n# l6-p95-mark-{n}\n"
        )
        out = built_runner.run_incremental(rel)
        assert out["short_circuited"] is False, out
        durations.append(int(out["duration_ms"]))

    p95 = _p95(durations)
    assert p95 < 500, f"p95 duration {p95}ms exceeds 500ms budget; samples={durations}"


def test_no_root_flag(runner: GtIndexRunner):
    """Invoking ``-file`` with no ``-root`` (default ".") errors gracefully.

    The Go ``flag`` package defaults ``-root`` to "." when omitted. The
    runner's cwd in local mode is ``self.tmp_path`` which holds no
    ``graph.db`` and no source files at the root. Step 1 of
    runIncremental must catch the missing DB and ``log.Fatalf`` with a
    clear message — no panic, no crash, exit code != 0.
    """
    # Deliberately omit -root. Use a -output path that does NOT exist at
    # the (default ".") cwd of the runner, so step 1 fires.
    proc = runner.run(
        "-file", "widgets.py",
        "-output", "missing_graph.db",
        expect_zero=False,
    )
    assert proc.returncode != 0, proc.stdout + proc.stderr
    blob = (proc.stdout + "\n" + proc.stderr).lower()
    assert "graph.db not found" in blob or "incremental mode requires" in blob, (
        f"missing graceful error message:\nSTDOUT:{proc.stdout}\nSTDERR:{proc.stderr}"
    )
    # Belt-and-braces: ensure no Go panic stack on stderr.
    assert "panic:" not in (proc.stderr or "").lower(), proc.stderr


def test_missing_file(built_runner: GtIndexRunner):
    """``-file <does-not-exist.py>`` exits non-zero with a clear error."""
    proc = built_runner.run_incremental_raw("nonexistent.py")
    assert proc.returncode != 0, proc.stdout + proc.stderr
    blob = (proc.stdout + "\n" + proc.stderr).lower()
    assert "read file" in blob or "no such file" in blob or "cannot find" in blob, (
        f"missing graceful error:\nSTDOUT:{proc.stdout}\nSTDERR:{proc.stderr}"
    )


def test_full_build_idempotent(runner: GtIndexRunner):
    """Two consecutive full builds yield identical node + edge counts.

    The Go indexer removes the old DB at the start of a full build, so
    reproducibility is the relevant property: same source → same counts
    (not byte-identical IDs).
    """
    a = runner.full_build()
    # Force a fresh build by removing the DB the way the binary does itself.
    # (Local mode: rm; remote mode: rm via SSH via run.)
    if _USE_REMOTE:
        runner._remote_run(f"rm -f {runner._remote_workdir}/graph.db")
    elif runner.local_db.exists():
        runner.local_db.unlink()
    b = runner.full_build()
    assert a["files"] == b["files"], (a, b)
    assert a["nodes"] == b["nodes"], (a, b)
    assert a["edges"] == b["edges"], (a, b)


# ── Helpers ──────────────────────────────────────────────────────────────────
def _count_incoming(conn: sqlite3.Connection, file_path: str) -> int:
    """Count cross-file edges whose target node lives in ``file_path``.

    Mirrors the SQL inside SnapshotIncomingEdgesTx (excluding self-edges).
    """
    return conn.execute(
        """
        SELECT COUNT(*) FROM edges e
         JOIN nodes n ON e.target_id = n.id
        WHERE n.file_path = ?
          AND (e.source_file IS NULL OR e.source_file != ?)
        """,
        (file_path, file_path),
    ).fetchone()[0]


def _p95(samples: list[int]) -> float:
    """Compute the 95th percentile (linear interpolation, like NumPy default)."""
    if not samples:
        return 0.0
    if len(samples) == 1:
        return float(samples[0])
    s = sorted(samples)
    rank = 0.95 * (len(s) - 1)
    lo = int(rank)
    hi = min(lo + 1, len(s) - 1)
    frac = rank - lo
    return s[lo] * (1 - frac) + s[hi] * frac
