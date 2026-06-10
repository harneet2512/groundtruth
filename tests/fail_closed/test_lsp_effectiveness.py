"""LSP-pass EFFECTIVENESS fixes (113-task sweep run 27249519544 — dynamodb-toolbox GATE-1 FAILs).

Two structural bugs made the LSP residual pass unable to flip GATE 1's pred_B
(det >= name_match) on the failing TS repos:

  (a) FIXED ATTEMPT BUDGET — gt_run_proof passed a fixed ``--max-edges 500`` when
      un-scoped. Each promotion (verify/correct: name_match -> lsp) closes the
      dominance gap by 2 and each delete by 1, so pred_B needs
      ``2*Promoted + Deleted > gap`` (dynamodb: name_match 3154 - det 2485 = 669).
      The budget must SCALE with the measured gap; ``compute_lsp_max_edges`` reads
      the graph (the gates' deterministic set vs name_match%) and returns
      ``min(ceiling, max(floor, ceil(gap * (1 + headroom))))``, env-overridable
      via ``GT_LSP_MAX_EDGES``.

  (b) LAZY PROJECT-LOAD RACE — tsserver starts its configured-project load on the
      FIRST didOpen, so resolve.py's initialize-time ``wait_for_progress_complete``
      returned before any load existed and every definition fast-failed
      (``Verified:0 Corrected:2 Deleted:11 Failed:478`` in 11.9s — LspErr/empty,
      never the 30s timeout). ``_await_project_ready`` is the one-shot readiness
      barrier: after the FIRST didOpen it re-waits progress and retries the FIRST
      definition with bounded backoff until the server answers. Language-agnostic;
      already-warm servers (pyright/gopls) exit on attempt 1.

RED before the fix (functions absent / first definition not retried), GREEN after.
No benchmark shapes: synthetic graphs + a generic fake stdio LSP server.
"""
from __future__ import annotations

import importlib.util
import os
import sqlite3
import sys
import time

import pytest

ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
sys.path.insert(0, os.path.join(ROOT, "src"))

_GRP = os.path.join(ROOT, "scripts", "swebench", "gt_run_proof.py")
_spec = importlib.util.spec_from_file_location("gt_run_proof_eff_t", _GRP)
grp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(grp)

from groundtruth import resolve  # noqa: E402
from groundtruth.lsp.client import LSPClient  # noqa: E402
from groundtruth.lsp.config import LSPServerConfig  # noqa: E402
from groundtruth.utils.result import Err, Ok  # noqa: E402


# ───────────────────────── (a) dynamic --max-edges ──────────────────────────


def _mk_graph(tmp_path, *, name_match=0, qualified_unresolved=0, det=0,
              det_method="import", imports_noise=0):
    """Synthetic graph.db with exactly the edge populations GATE 1 counts."""
    db = str(tmp_path / "graph.db")
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE edges (id INTEGER PRIMARY KEY AUTOINCREMENT, source_id INTEGER, "
        "target_id INTEGER, type TEXT, resolution_method TEXT, confidence REAL)"
    )
    rows = (
        [(1, 2, "CALLS", "name_match", 0.2)] * name_match
        + [(1, 2, "CALLS", "name_match_qualified_unresolved", 0.2)] * qualified_unresolved
        + [(1, 2, "CALLS", det_method, 1.0)] * det
        # IMPORTS edges must NOT count toward the CALLS-only gap.
        + [(1, 2, "IMPORTS", "import", 1.0)] * imports_noise
    )
    conn.executemany(
        "INSERT INTO edges(source_id, target_id, type, resolution_method, confidence) "
        "VALUES (?,?,?,?,?)",
        rows,
    )
    conn.commit()
    conn.close()
    return db


def test_max_edges_scales_above_the_dominance_gap(tmp_path):
    """RED->GREEN core: gap > 500 must yield a budget ABOVE the gap (gap + ~30%),
    never the old fixed 500 that was structurally below pred_B's requirement."""
    db = _mk_graph(tmp_path, name_match=1200, det=100, imports_noise=50)
    gap = grp.gate1_dominance_gap(db)
    assert gap == 1100  # CALLS-only: 1200 name_match - 100 det; IMPORTS excluded
    got = grp.compute_lsp_max_edges(db, scoped=False, env={})
    assert got > gap, f"budget {got} does not clear the gap {gap} (the 500-cap bug shape)"
    assert got == 1430  # ceil(1100 * 1.30)


def test_max_edges_counts_name_match_prefix_like_gate1(tmp_path):
    """The gap must count name_match% (incl. name_match_qualified_unresolved),
    exactly the GATE 1 pred_B math."""
    db = _mk_graph(tmp_path, name_match=400, qualified_unresolved=300, det=100)
    assert grp.gate1_dominance_gap(db) == 600  # (400+300) - 100


def test_max_edges_floor_when_graph_already_dominant(tmp_path):
    """det >= name_match (gap <= 0) keeps the historical 500 floor — the pass still
    runs (liveness proof + residual cleaning), never goes to 0."""
    db = _mk_graph(tmp_path, name_match=10, det=100)
    assert grp.gate1_dominance_gap(db) == 0
    assert grp.compute_lsp_max_edges(db, scoped=False, env={}) == 500


def test_max_edges_ceiling_bounds_worst_case(tmp_path):
    """A huge gap is ceiling-capped (bounded worst-case wall clock)."""
    db = _mk_graph(tmp_path, name_match=60000, det=0)
    assert grp.compute_lsp_max_edges(db, scoped=False, env={}) == 20000


def test_max_edges_env_override_wins(tmp_path):
    db = _mk_graph(tmp_path, name_match=1200, det=100)
    assert grp.compute_lsp_max_edges(db, scoped=False, env={"GT_LSP_MAX_EDGES": "777"}) == 777
    # invalid / non-positive overrides are ignored, never crash
    assert grp.compute_lsp_max_edges(db, scoped=False, env={"GT_LSP_MAX_EDGES": "bogus"}) == 1430
    assert grp.compute_lsp_max_edges(db, scoped=False, env={"GT_LSP_MAX_EDGES": "-5"}) == 1430


def test_max_edges_scoped_floor_preserved(tmp_path):
    """Issue-scoped runs keep their historical 20000 budget as a FLOOR — the dynamic
    computation may only raise budgets, never shrink the scoped pass."""
    db = _mk_graph(tmp_path, name_match=10, det=100)  # gap 0
    assert grp.compute_lsp_max_edges(db, scoped=True, env={}) == 20000


def test_max_edges_unreadable_graph_fails_safe_to_floor(tmp_path):
    """A missing/unreadable graph must fail safe to the floor, never raise."""
    assert grp.gate1_dominance_gap(str(tmp_path / "nope.db")) == 0
    assert grp.compute_lsp_max_edges(str(tmp_path / "nope.db"), scoped=False, env={}) == 500


# ─────────────────── (b) readiness barrier for lazy servers ──────────────────

# A minimal stdio LSP server: answers initialize; ERRORS the first N
# textDocument/definition requests (the tsserver "project still loading"
# fast-fail shape), then returns a real Location. Generic — no benchmark or
# language shapes.
_FAKE_LSP_SERVER = r'''
import json
import sys

fin = sys.stdin.buffer
fout = sys.stdout.buffer
fail_first = int(sys.argv[1]) if len(sys.argv) > 1 else 0
def_uri = sys.argv[2] if len(sys.argv) > 2 else "file:///project/lib.ts"


def read_message():
    content_length = 0
    while True:
        line = fin.readline()
        if not line:
            return None
        if line in (b"\r\n", b"\n"):
            break
        if line.lower().startswith(b"content-length:"):
            content_length = int(line.split(b":", 1)[1])
    if content_length <= 0:
        return None
    return json.loads(fin.read(content_length))


def send(msg):
    data = json.dumps(msg).encode("utf-8")
    fout.write(b"Content-Length: " + str(len(data)).encode("ascii") + b"\r\n\r\n" + data)
    fout.flush()


def_count = 0
while True:
    msg = read_message()
    if msg is None:
        break
    method = msg.get("method")
    mid = msg.get("id")
    if method == "exit":
        break
    if mid is None:
        continue  # notification (didOpen/initialized/...) — no response
    if method == "textDocument/definition":
        def_count += 1
        if def_count <= fail_first:
            send({"jsonrpc": "2.0", "id": mid,
                  "error": {"code": -32603, "message": "No Project."}})
        else:
            send({"jsonrpc": "2.0", "id": mid, "result": [{
                "uri": def_uri,
                "range": {"start": {"line": 5, "character": 0},
                          "end": {"line": 5, "character": 6}}}]})
    elif method == "initialize":
        send({"jsonrpc": "2.0", "id": mid, "result": {"capabilities": {}}})
    else:
        send({"jsonrpc": "2.0", "id": mid, "result": None})
'''


@pytest.fixture
def fake_server_script(tmp_path):
    p = tmp_path / "fake_lsp_server.py"
    p.write_text(_FAKE_LSP_SERVER, encoding="utf-8")
    return str(p)


async def _started_client(script: str, fail_first: int, def_uri: str = "") -> LSPClient:
    cmd = [sys.executable, script, str(fail_first)]
    if def_uri:
        cmd.append(def_uri)
    client = LSPClient(cmd, "file:///project")
    start = await client.start()
    assert isinstance(start, Ok)
    init = await client.send_request("initialize", {"processId": 1}, timeout=10.0)
    assert isinstance(init, Ok)
    return client


async def test_barrier_retries_until_project_ready(fake_server_script):
    """The barrier must absorb the lazy-load fast-fail burst: 3 errors then success
    -> ready within budget, the SUCCESSFUL result returned for reuse."""
    client = await _started_client(fake_server_script, fail_first=3)
    try:
        result, waited_ms, ready_ok, attempts = await resolve._await_project_ready(
            client, "file:///project/src/main.ts", 0, 0, budget_s=15.0
        )
        assert ready_ok is True
        assert attempts == 4  # 3 fast-fails + the converged success
        assert isinstance(result, Ok) and result.value
        assert waited_ms > 0.0
    finally:
        await client.shutdown()


async def test_barrier_bounded_when_project_never_loads(fake_server_script):
    """A permanently-broken project must NOT stall the pass: the barrier returns
    not-ready within its bounded budget (never the 30s-per-request hang shape)."""
    client = await _started_client(fake_server_script, fail_first=10**9)
    try:
        t0 = time.time()
        result, waited_ms, ready_ok, attempts = await resolve._await_project_ready(
            client, "file:///project/src/main.ts", 0, 0, budget_s=2.0
        )
        elapsed = time.time() - t0
        assert ready_ok is False
        assert attempts >= 2  # it DID retry, not a single-shot give-up
        assert elapsed < 10.0
        assert isinstance(result, Err)
    finally:
        await client.shutdown()


async def test_barrier_warm_server_exits_on_first_attempt(fake_server_script):
    """Already-warm servers (the pyright/gopls case) must pay ~one request — the
    barrier never slows a working language with retry/backoff burn."""
    client = await _started_client(fake_server_script, fail_first=0)
    try:
        result, waited_ms, ready_ok, attempts = await resolve._await_project_ready(
            client, "file:///project/src/main.ts", 0, 0, budget_s=20.0
        )
        assert ready_ok is True
        assert attempts == 1
        assert isinstance(result, Ok) and result.value
        assert waited_ms < 8000.0  # one bounded drain + one request, no backoff loop
    finally:
        await client.shutdown()


async def test_resolve_edges_runs_barrier_and_verifies_edge(
    fake_server_script, tmp_path, monkeypatch
):
    """END-TO-END wiring (red->green): _resolve_edges against a fake server that
    errors the FIRST 2 definitions then succeeds. Without the barrier the single
    edge fast-fails (Verified:0 Failed:1 — the dynamodb shape); with it the pass
    retries through the load, verifies the edge, and stamps the readiness fields."""
    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    (root / "src" / "main.ts").write_text("lib.helper()\n", encoding="utf-8")
    (root / "lib.ts").write_text(
        "export class Lib {\n  helper() {\n    return 1;\n  }\n}\n", encoding="utf-8"
    )
    lib_uri = resolve._path_to_uri(str(root / "lib.ts"))

    db = str(tmp_path / "graph.db")
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE nodes (id INTEGER PRIMARY KEY, label TEXT, name TEXT, "
        "qualified_name TEXT, file_path TEXT, start_line INTEGER, end_line INTEGER, "
        "signature TEXT, return_type TEXT, is_exported INTEGER DEFAULT 0, "
        "is_test INTEGER DEFAULT 0, language TEXT, parent_id INTEGER)"
    )
    conn.execute(
        "CREATE TABLE edges (id INTEGER PRIMARY KEY, source_id INTEGER, "
        "target_id INTEGER, type TEXT, source_line INTEGER, source_file TEXT, "
        "resolution_method TEXT, confidence REAL)"
    )
    conn.execute(
        "INSERT INTO nodes VALUES (1, 'Function', 'main', 'main', 'src/main.ts', "
        "1, 2, NULL, NULL, 0, 0, 'typescript', NULL)"
    )
    conn.execute(
        "INSERT INTO nodes VALUES (42, 'Method', 'helper', 'Lib.helper', 'lib.ts', "
        "1, 10, NULL, NULL, 0, 0, 'typescript', NULL)"
    )
    conn.execute(
        "INSERT INTO edges VALUES (1, 1, 42, 'CALLS', 1, 'src/main.ts', 'name_match', 0.2)"
    )
    conn.commit()
    conn.close()

    cfg = LSPServerConfig(command=[sys.executable, fake_server_script, "2", lib_uri])
    monkeypatch.setattr(
        "groundtruth.lsp.config.get_server_config", lambda ext: Ok(cfg)
    )

    edge = {
        "id": 1, "source_id": 1, "target_id": 42, "resolution_method": "name_match",
        "confidence": 0.2, "source_file": "src/main.ts", "source_line": 1,
        "caller_name": "main", "language": "typescript",
        "target_name": "helper", "target_file": "lib.ts",
    }
    stats = await resolve._resolve_edges(db, str(root), [edge], "typescript")

    # The lazy-load burst was absorbed: the edge VERIFIED instead of fast-failing.
    assert stats["verified"] == 1, f"edge fast-failed through the load burst: {stats}"
    assert stats["failed"] == 0
    # Readiness surfaced for the certificate.
    assert stats["project_ready"] is True
    assert stats["project_ready_attempts"] == 3  # 2 fast-fails + success
    assert stats["project_ready_wait_ms"] > 0.0

    conn = sqlite3.connect(db)
    method, confidence = conn.execute(
        "SELECT resolution_method, confidence FROM edges WHERE id = 1"
    ).fetchone()
    conn.close()
    assert method == "lsp" and confidence == 1.0
