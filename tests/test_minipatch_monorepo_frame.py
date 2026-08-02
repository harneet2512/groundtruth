"""A6 monorepo path-frame — red->green for the contract + scope callsites.

Pre-fix, `_graph_contract_block`, `_query_scope`, and `_consensus_block` matched the
agent's view path EXACTLY (`_norm_fp(rel)`). On a monorepo where the agent runs from a
sub-dir (views `json-schema/x.ts`) while the graph indexed from the repo root (stores
`ark/json-schema/x.ts`), every contract/scope went DARK (0 rows). Post-fix they call
`_resolve_frame` (the resolver `_evidence_body` already used at :2663). Held-out: a
single-package exact-frame file is unchanged (early-return path at :1798).

The heavy gt_mini_patch import registers an atexit ledger flush that hangs pytest
teardown, so the assertion body runs in a SUBPROCESS that ends with os._exit(0); the
pytest process never imports gt_mini_patch. Deterministic: pure graph + regex, no net.
"""
from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]

_RUNNER = textwrap.dedent(r"""
    import os, sqlite3, sys, importlib.util
    gdb, patch = sys.argv[1], sys.argv[2]
    SCH = '''
    CREATE TABLE nodes (id INTEGER PRIMARY KEY, label TEXT, name TEXT, qualified_name TEXT,
     file_path TEXT, start_line INTEGER, end_line INTEGER, signature TEXT, return_type TEXT,
     is_exported INTEGER, is_test INTEGER, language TEXT, parent_id INTEGER);
    CREATE TABLE edges (id INTEGER PRIMARY KEY, source_id INTEGER, target_id INTEGER, type TEXT,
     source_line INTEGER, source_file TEXT, resolution_method TEXT, confidence REAL, metadata TEXT);
    CREATE TABLE properties (id INTEGER PRIMARY KEY, node_id INTEGER, kind TEXT, value TEXT);'''
    c = sqlite3.connect(gdb); c.executescript(SCH)
    c.execute("INSERT INTO nodes VALUES (1,'Function','parse','parse','ark/json-schema/x.ts',3,9,'(s: string): Schema','Schema',1,0,'typescript',0)")
    c.execute("INSERT INTO nodes VALUES (2,'Function','caller','caller','ark/type/y.ts',1,5,'(): void','void',1,0,'typescript',0)")
    c.execute("INSERT INTO nodes VALUES (3,'Function','do_it','do_it','app.py',1,4,'(x: int) -> int','int',1,0,'python',0)")
    c.execute("INSERT INTO nodes VALUES (4,'Function','helper','helper','util.py',1,3,'() -> int','int',1,0,'python',0)")
    c.execute("INSERT INTO edges VALUES (1,2,1,'CALLS',2,'ark/type/y.ts','import',1.0,NULL)")
    c.execute("INSERT INTO edges VALUES (2,3,4,'CALLS',2,'app.py','import',1.0,NULL)")
    c.commit(); c.close()
    for k in list(os.environ):
        if k.startswith("GT_"): del os.environ[k]
    os.environ["GT_HOST_GRAPH_DB"] = gdb
    os.environ["GT_ROOT"] = os.path.dirname(gdb)
    spec = importlib.util.spec_from_file_location("gmp", patch)
    m = importlib.util.module_from_spec(spec); sys.modules["gmp"]=m; spec.loader.exec_module(m)
    m._GT_BASELINE = False
    print("SCOPE_MONO=" + repr(m._query_scope("json-schema/x.ts")), flush=True)
    m._contract_seen.discard("json-schema/x.ts")
    print("CONTRACT_MONO=" + repr(m._graph_contract_block("json-schema/x.ts")), flush=True)
    print("SCOPE_SINGLE=" + repr(m._query_scope("app.py")), flush=True)
    sys.stdout.flush(); os._exit(0)
""")


def _run(tmp_path) -> dict[str, str]:
    gdb = tmp_path / "graph.db"
    runner = tmp_path / "runner.py"
    runner.write_text(_RUNNER, encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, str(runner), str(gdb), str(_ROOT / "artifact_deepswe" / "gt_mini_patch.py")],
        cwd=str(_ROOT), capture_output=True, text=True, timeout=120)
    out = {}
    for ln in proc.stdout.splitlines():
        if "=" in ln and ln.split("=", 1)[0] in ("SCOPE_MONO", "CONTRACT_MONO", "SCOPE_SINGLE"):
            k, v = ln.split("=", 1)
            out[k] = v
    assert {"SCOPE_MONO", "CONTRACT_MONO", "SCOPE_SINGLE"} <= set(out), \
        f"runner did not emit all lines:\nSTDOUT:{proc.stdout}\nSTDERR:{proc.stderr}"
    return out


def test_a6_monorepo_frame(tmp_path):
    r = _run(tmp_path)
    # scope: the neighbor ark/type/y.ts must be found from the sub-dir view (was [])
    assert "y.ts" in r["SCOPE_MONO"], f"monorepo scope dark: {r['SCOPE_MONO']}"
    # contract: the fresh production-like subprocess auto-resolves Profile 2, whose
    # GT_CONTRACT_NATIVE form is a source-anchored caller note rather than the legacy
    # [SIGNATURE] tag.  The cross-package y.ts anchor proves the contract query resolved
    # the sub-dir view into the graph's monorepo frame (the pre-fix result was '').
    assert ("ark/type/y.ts:2: note:" in r["CONTRACT_MONO"]
            and "verify your change is consistent here" in r["CONTRACT_MONO"]), \
        f"monorepo contract dark: {r['CONTRACT_MONO']}"
    # held-out: single-package exact-frame file unchanged
    assert "util.py" in r["SCOPE_SINGLE"], f"single-package regressed: {r['SCOPE_SINGLE']}"
