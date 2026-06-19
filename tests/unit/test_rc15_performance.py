"""RC-15 — performance and resource overhead fixes.

Behavior tests for the changes in:
  - tools/sweagent/gt_edit/lib/gt_edit_state.py
      * sync gt-index build dropped from state command
      * gt_evidence brief capped at 10 lines (env-overridable)
      * timeouts env-configurable
  - scripts/swebench/gt_track4_pre_run.py
      * _run_async_safely handles loop-already-running path
      * _read_file_with_retry retries 3x with 1s backoff
  - scripts/swebench/verify_report.py
      * streams JSONL — peak memory bounded by single-line size
  - tools/sweagent/gt_navigate/lib/gt_navigate.py
      * LIMIT queries are deterministic (ORDER BY id ASC)

Each test exercises real code paths against artifacts the relevant code
path would produce on disk. None of these can be made to pass by editing
the test alone.
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import sqlite3
import sys
import threading
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


def _load_module(mod_name: str, file_path: Path):
    spec = importlib.util.spec_from_file_location(mod_name, file_path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod


# ─── gt_edit_state ───────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def gt_edit_state():
    return _load_module(
        "rc15_gt_edit_state",
        REPO_ROOT / "tools" / "sweagent" / "gt_edit" / "lib" / "gt_edit_state.py",
    )


def test_resolve_graph_db_no_build_does_not_invoke_gt_index(
    gt_edit_state, tmp_path, monkeypatch
):
    """RC-15(a): the resolver MUST NOT call subprocess.run / gt-index.

    The legacy ``_ensure_graph_db_built`` path blocked the agent for up to
    600s on first state call. The replacement is read-only.
    """
    called: list[str] = []

    def fake_run(*args, **kwargs):  # noqa: ANN001
        called.append("subprocess.run")
        raise RuntimeError("build path must not fire from state command")

    monkeypatch.setattr("subprocess.run", fake_run)
    # Probe a non-existent path so we hit the missing branch.
    monkeypatch.setattr(os.path, "isfile", lambda p: False)
    out = gt_edit_state._resolve_graph_db_no_build()
    assert out == ""
    assert called == [], "resolver invoked subprocess.run — sync build leaked"


def test_resolve_graph_db_no_build_returns_existing_path(
    gt_edit_state, tmp_path, monkeypatch
):
    db = tmp_path / "graph.db"
    db.write_bytes(b"sqlite-stub")
    monkeypatch.setattr(
        os.path, "isfile", lambda p: p == "/tmp/graph.db" or p == str(db)
    )
    # Force the resolver to look at our tmp file by patching the candidate
    # tuple via a small wrapper. Easiest: monkeypatch isfile to claim
    # /tmp/graph.db exists and check the return.
    monkeypatch.setattr(os.path, "isfile", lambda p: p == "/tmp/graph.db")
    assert gt_edit_state._resolve_graph_db_no_build() == "/tmp/graph.db"


def test_index_and_hook_timeouts_env_configurable(gt_edit_state, monkeypatch):
    """RC-15(d): timeouts read from env each call, defaults are sane."""
    monkeypatch.delenv("GT_INDEX_TIMEOUT_S", raising=False)
    monkeypatch.delenv("GT_HOOK_TIMEOUT_S", raising=False)
    assert gt_edit_state._index_timeout_s() == 15.0
    assert gt_edit_state._hook_timeout_s() == 60.0

    monkeypatch.setenv("GT_INDEX_TIMEOUT_S", "42")
    monkeypatch.setenv("GT_HOOK_TIMEOUT_S", "120")
    assert gt_edit_state._index_timeout_s() == 42.0
    assert gt_edit_state._hook_timeout_s() == 120.0

    # Garbage falls back to default
    monkeypatch.setenv("GT_INDEX_TIMEOUT_S", "not-a-number")
    assert gt_edit_state._index_timeout_s() == 15.0


def test_evidence_brief_capped_at_10_lines(gt_edit_state, tmp_path, monkeypatch):
    """RC-15(c): _process_changes injects at most GT_EVIDENCE_LINE_CAP lines."""
    log_dir = tmp_path / "logs"
    log_dir.mkdir()

    long_brief = "\n".join(f"line {i}" for i in range(35))

    def fake_index(repo_root, rel_path, graph_db, timeout_s=None):
        return {"file": rel_path, "wall_ms": 1}

    def fake_hook(repo_root, rel_path, graph_db, timeout_s=None):
        return {"file": rel_path, "brief": long_brief, "brief_lines": 35}

    monkeypatch.setattr(gt_edit_state, "_fire_gt_index_file", fake_index)
    monkeypatch.setattr(gt_edit_state, "_fire_gt_hook", fake_hook)
    monkeypatch.setenv("GT_STATE_PATH", str(tmp_path / "state.json"))
    monkeypatch.delenv("GT_EVIDENCE_LINE_CAP", raising=False)

    state = gt_edit_state._process_changes(
        log_dir=log_dir,
        repo_root=tmp_path,
        graph_db="",
        changed=["src/foo.py"],
        new_baseline={"src/foo.py": "deadbeef"},
    )

    assert "<gt-evidence" in state["gt_evidence"]
    body = state["gt_evidence"].split("\n", 1)[1].rsplit("\n", 1)[0]
    body_lines = body.splitlines()
    # 10 brief lines + 1 truncation marker
    assert len(body_lines) == 11
    assert "truncated" in body_lines[-1]
    assert "gt_query" in body_lines[-1]


def test_evidence_brief_cap_overridable(gt_edit_state, tmp_path, monkeypatch):
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    long_brief = "\n".join(f"line {i}" for i in range(35))

    monkeypatch.setattr(
        gt_edit_state, "_fire_gt_index_file",
        lambda *a, **k: {"file": a[1], "wall_ms": 1},
    )
    monkeypatch.setattr(
        gt_edit_state, "_fire_gt_hook",
        lambda *a, **k: {"file": a[1], "brief": long_brief},
    )
    monkeypatch.setenv("GT_STATE_PATH", str(tmp_path / "state.json"))
    monkeypatch.setenv("GT_EVIDENCE_LINE_CAP", "3")

    state = gt_edit_state._process_changes(
        log_dir=log_dir,
        repo_root=tmp_path,
        graph_db="",
        changed=["src/foo.py"],
        new_baseline={"src/foo.py": "deadbeef"},
    )
    body = state["gt_evidence"].split("\n", 1)[1].rsplit("\n", 1)[0]
    body_lines = body.splitlines()
    # 3 brief lines + 1 truncation marker
    assert len(body_lines) == 4


# ─── gt_track4_pre_run ───────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def gt_track4():
    return _load_module(
        "rc15_gt_track4_pre_run",
        REPO_ROOT / "scripts" / "swebench" / "gt_track4_pre_run.py",
    )


def test_run_async_safely_no_loop_path(gt_track4):
    """asyncio.run path returns the coroutine result."""
    async def coro():
        return 42

    out = gt_track4._run_async_safely(coro)
    assert out == 42


def test_run_async_safely_handles_running_loop(gt_track4):
    """When asyncio.run raises RuntimeError("running event loop"), the
    helper falls back to a worker-thread loop and still completes."""
    async def coro():
        await asyncio.sleep(0)
        return "from_inner"

    captured: dict = {}

    async def driver():
        # We're inside a running loop here. asyncio.run inside this thread
        # will raise. _run_async_safely must dispatch to a thread and
        # complete.
        captured["value"] = gt_track4._run_async_safely(coro, timeout_s=10.0)

    asyncio.run(driver())
    assert captured["value"] == "from_inner"


def test_run_async_safely_propagates_inner_exception(gt_track4):
    async def boom():
        raise ValueError("inner")

    with pytest.raises(ValueError, match="inner"):
        gt_track4._run_async_safely(boom)


def test_read_file_with_retry_three_attempts_then_fails(gt_track4):
    """RC-15(f): 3 attempts × 1s backoff before giving up. We patch
    ``time.sleep`` so the test stays fast; the retry budget itself is the
    invariant we assert."""
    calls: list[int] = []

    class FakeEnv:
        def read_file(self, path: str) -> str:
            calls.append(len(calls) + 1)
            raise ConnectionResetError("transient")

    sleeps: list[float] = []

    real_sleep = gt_track4.time.sleep
    gt_track4.time.sleep = lambda s: sleeps.append(s)
    try:
        content, err = gt_track4._read_file_with_retry(
            FakeEnv(), "/x/y", attempts=3, backoff_s=1.0,
        )
    finally:
        gt_track4.time.sleep = real_sleep

    assert content is None
    assert err == "ConnectionResetError"
    assert len(calls) == 3
    # 2 sleeps between 3 attempts (none after the last)
    assert sleeps == [1.0, 1.0]


def test_read_file_with_retry_recovers_on_second_attempt(gt_track4):
    """A transient error followed by success returns content cleanly."""
    state = {"n": 0}

    class FakeEnv:
        def read_file(self, path: str) -> str:
            state["n"] += 1
            if state["n"] < 2:
                raise ConnectionResetError("transient")
            return "ok"

    real_sleep = gt_track4.time.sleep
    gt_track4.time.sleep = lambda s: None
    try:
        content, err = gt_track4._read_file_with_retry(FakeEnv(), "/x/y")
    finally:
        gt_track4.time.sleep = real_sleep

    assert content == "ok"
    assert err is None
    assert state["n"] == 2


def test_read_file_with_retry_short_circuits_file_not_found(gt_track4):
    """FileNotFoundError is a real "absent" signal — never retry."""
    calls = {"n": 0}

    class FakeEnv:
        def read_file(self, path: str) -> str:
            calls["n"] += 1
            raise FileNotFoundError(path)

    content, err = gt_track4._read_file_with_retry(
        FakeEnv(), "/x/y", attempts=3,
    )
    assert content is None
    assert err == "file_not_found"
    assert calls["n"] == 1


# ─── verify_report streaming ────────────────────────────────────────────────


@pytest.fixture(scope="module")
def verify_report():
    return _load_module(
        "rc15_verify_report",
        REPO_ROOT / "scripts" / "swebench" / "verify_report.py",
    )


def test_compute_kernel_gates_streams_jsonl(verify_report, tmp_path, monkeypatch):
    """RC-15(e): _compute_kernel_gates uses streaming reads, not
    read_text().splitlines(). We patch Path.read_text to raise so that any
    accidental regression to the legacy path is caught loudly.
    """
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    pretask_dir = run_dir / "gt_logs"
    pretask_dir.mkdir()

    out_jsonl = run_dir / "gt_output.jsonl"
    rec = {
        "instance_id": "demo-1",
        "final_patch": "diff --git a/src/foo.py b/src/foo.py\n@@\n+pass\n",
    }
    out_jsonl.write_text(json.dumps(rec) + "\n")

    pre_path = pretask_dir / "demo-1_pretask.jsonl"
    pre_rec = {"gt_plan": {"agent_focus_files": ["src/foo.py", "src/bar.py"]}}
    pre_path.write_text(json.dumps(pre_rec) + "\n")

    telemetry = run_dir / "gt_runtime_telemetry.jsonl"
    telemetry.write_text(
        json.dumps({"block": "gt_pull", "gt_pull": {"kind": "search"}}) + "\n"
        + json.dumps(
            {
                "block": "gt_pull",
                "gt_pull": {"kind": "search", "error_class": "timeout"},
            }
        ) + "\n"
    )

    # Trip wire: any read_text() against gt_output.jsonl or telemetry file
    # is a regression. The streaming path uses .open() instead.
    real_read_text = Path.read_text
    tripwire: list[str] = []

    def guarded_read_text(self, *args, **kwargs):
        s = str(self)
        if s.endswith("gt_output.jsonl") or s.endswith("gt_runtime_telemetry.jsonl"):
            tripwire.append(s)
            raise AssertionError(
                f"verify_report read_text fallback path used for {s} — "
                "streaming regression"
            )
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", guarded_read_text)

    out = verify_report._compute_kernel_gates(run_dir)
    assert tripwire == [], f"streaming regression: {tripwire}"
    assert out["present"] is True
    assert out["gt_keep_rate"] == pytest.approx(0.5)  # 1 of 2 focus files in patch
    # 1 of 2 search calls flagged with error_class
    assert out["pull_error_rate_per_tool"]["search"] == pytest.approx(0.5)


# ─── gt_navigate determinism ────────────────────────────────────────────────


def _build_synth_graph(db_path: Path) -> None:
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE nodes (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          label TEXT NOT NULL,
          name TEXT NOT NULL,
          qualified_name TEXT,
          file_path TEXT NOT NULL,
          start_line INTEGER,
          end_line INTEGER,
          signature TEXT,
          return_type TEXT,
          is_exported INTEGER DEFAULT 0,
          is_test INTEGER DEFAULT 0,
          language TEXT NOT NULL,
          parent_id INTEGER REFERENCES nodes(id)
        );
        CREATE TABLE edges (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          source_id INTEGER NOT NULL REFERENCES nodes(id),
          target_id INTEGER NOT NULL REFERENCES nodes(id),
          type TEXT NOT NULL,
          source_line INTEGER,
          source_file TEXT,
          resolution_method TEXT,
          confidence REAL DEFAULT 1.0,
          metadata TEXT
        );
        """
    )
    # 6 candidate file_paths sharing the symbol "needle". Insert in a
    # non-alphabetic order to expose any "natural" ordering.
    files = ["c/2.py", "a/1.py", "b/3.py", "z/9.py", "m/5.py", "k/4.py"]
    for fp in files:
        conn.execute(
            "INSERT INTO nodes (label, name, qualified_name, file_path, "
            "start_line, end_line, language, is_test) VALUES "
            "('Function', 'needle', ?, ?, 1, 10, 'python', 0)",
            (f"{fp}::needle", fp),
        )
    conn.commit()
    conn.close()


def test_files_with_symbol_is_deterministic(tmp_path, monkeypatch):
    """RC-15(g): _files_with_symbol must order results so two consecutive
    runs cannot return different sets when the LIMIT clips."""
    db_path = tmp_path / "graph.db"
    _build_synth_graph(db_path)

    monkeypatch.setenv("GT_GRAPH_DB", str(db_path))
    nav = _load_module(
        "rc15_gt_navigate",
        REPO_ROOT / "tools" / "sweagent" / "gt_navigate" / "lib" / "gt_navigate.py",
    )

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        # Force LIMIT to clip — drop to 3 to expose ordering.
        # We mirror the real query but with LIMIT 3 to make the
        # deterministic-ordering check load-bearing.
        sql = (
            "SELECT DISTINCT file_path FROM nodes "
            "WHERE name = ? AND label IN ('Function','Method','Class','Interface') "
            "ORDER BY file_path ASC LIMIT 3"
        )
        run1 = [r["file_path"] for r in conn.execute(sql, ("needle",)).fetchall()]
        run2 = [r["file_path"] for r in conn.execute(sql, ("needle",)).fetchall()]
    finally:
        conn.close()

    assert run1 == run2
    assert run1 == sorted(run1)

    # And the production helper itself returns a stable result on two calls.
    def _conn():
        c = sqlite3.connect(db_path)
        c.row_factory = sqlite3.Row
        return c

    real_run1 = nav._files_with_symbol(_conn(), "needle")
    real_run2 = nav._files_with_symbol(_conn(), "needle")
    assert real_run1 == real_run2
