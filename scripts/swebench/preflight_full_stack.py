#!/usr/bin/env python3
"""Behavioral preflight gate for the GT full-stack benchmark run.

WHY THIS EXISTS
---------------
"Installed" is the weakest bar. The 30-task run (26909714974) shipped a binary
that *could* compute FTS5/semantic/LSP but, at run time, silently fell back on
all three (FTS5 rebuilt in Python / empty, semantic zeroed because onnxruntime+
the model were absent, LSP emitting 0ms confidence-filter stamps). We paid for a
grep+CALLS localizer and got confounded results. This gate refuses to let that
happen again: it PROBES each signal for a real, non-zero result at its correct
stage and ABORTS the run (exit 1) before the paid agent loop starts.

It checks BEHAVIOR, not presence:
  - FTS5    : nodes_fts exists AND a real MATCH query returns rows (index-time build,
              not a runtime Python rebuild).
  - SEMANTIC: the SAME embedder the brief uses (forced ONNX _OnnxEmbedderAdapter)
              produces a finite, NON-ZERO cosine on a probe pair — not _ZeroEmbeddingModel.
  - LSP     : the edge verifier WARMS (server available) so the first runtime verify
              does not cold-start into the 0ms fallback.
  - STRUCT  : graph.db carries >1 edge type (not CALLS-only) — a warning, not fatal.

Required-ness is opt-in so this is safe to run anywhere:
  GT_REQUIRE_FULL_STACK=1  -> every check below is required (any failure => exit 1)
  GT_REQUIRE_FTS5=1 / GT_REQUIRE_EMBEDDER=1 / GT_REQUIRE_LSP=1 -> that check required.

Usage:
  python scripts/swebench/preflight_full_stack.py [--graph-db PATH] [--workspace PATH]
  (FTS5/STRUCT need --graph-db; LSP real-warm needs --workspace; SEMANTIC needs neither.)
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys

# Force the container-real semantic surface for the probe (BRIEFING.md §5):
# block sentence_transformers, use the ONNX _OnnxEmbedderAdapter both halves use.
os.environ.setdefault("GT_FORCE_ONNX_EMBEDDER", "1")


def _required(flag: str) -> bool:
    return os.environ.get("GT_REQUIRE_FULL_STACK") == "1" or os.environ.get(flag) == "1"


def check_fts5(graph_db: str | None) -> tuple[bool, str]:
    if not graph_db or not os.path.exists(graph_db):
        return (False, "FTS5: no --graph-db given (cannot probe at this stage)")
    try:
        c = sqlite3.connect(graph_db)
        has = c.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='nodes_fts'"
        ).fetchone()[0]
        if not has:
            return (False, "FTS5: nodes_fts table ABSENT — built without -tags sqlite_fts5 (runtime fallback)")
        rows = c.execute("SELECT COUNT(*) FROM nodes_fts").fetchone()[0]
        if rows <= 0:
            return (False, f"FTS5: nodes_fts present but EMPTY ({rows} rows) — population failed")
        # real MATCH probe: pull a token from an actual node name and query it back.
        sample = c.execute("SELECT name FROM nodes WHERE name!='' LIMIT 1").fetchone()
        if sample:
            tok = "".join(ch for ch in sample[0] if ch.isalnum())[:24] or sample[0]
            hit = c.execute("SELECT COUNT(*) FROM nodes_fts WHERE nodes_fts MATCH ?", (tok,)).fetchone()[0]
            if hit <= 0:
                return (False, f"FTS5: MATCH '{tok}' returned 0 — index queryable-but-empty")
        c.close()
        return (True, f"FTS5: OK — nodes_fts populated ({rows} rows), MATCH probe non-empty")
    except sqlite3.Error as e:
        return (False, f"FTS5: query error {e!r}")


def check_semantic() -> tuple[bool, str]:
    os.environ["GT_FORCE_ONNX_EMBEDDER"] = "1"
    try:
        from groundtruth.pretask.graph_localizer import _get_embedder
        m = _get_embedder()
        if m is None:
            return (False, "SEMANTIC: no embedder (onnxruntime + models/e5-small-v2 not loadable)")
        import numpy as np
        embs = m.encode(["fix the off-by-one in the parser", "def parse(self, tokens): ..."],
                        normalize_embeddings=True)
        a = np.asarray(embs[0], dtype=float)
        b = np.asarray(embs[1], dtype=float)
        if not np.isfinite(a).all() or float(np.linalg.norm(a)) == 0.0:
            return (False, "SEMANTIC: embedder returned a ZERO/NaN vector (_ZeroEmbeddingModel path)")
        cos = float(np.dot(a, b))
        adapter = type(m).__name__
        if "Zero" in adapter:
            return (False, f"SEMANTIC: using {adapter} — semantic is OFF")
        return (True, f"SEMANTIC: OK — {adapter}, probe cosine={cos:.3f}, non-zero vectors")
    except Exception as e:
        return (False, f"SEMANTIC: embedder probe failed {e!r}")


def check_lsp(workspace: str | None, graph_db: str | None) -> tuple[bool, str]:
    try:
        from groundtruth.lsp.edge_verifier import LazyEdgeVerifier
    except Exception as e:
        return (False, f"LSP: edge_verifier import failed {e!r}")
    if not workspace or not graph_db:
        # presence-only fallback when we can't warm a real server
        import shutil
        has_pyright = bool(shutil.which("pyright")) or _import_ok("pyright")
        return (has_pyright,
                "LSP: pyright present (no --workspace to warm a real server)" if has_pyright
                else "LSP: pyright NOT on PATH and not importable")
    try:
        import asyncio
        v = LazyEdgeVerifier(workspace_root=workspace, graph_db=graph_db)
        ok = asyncio.new_event_loop().run_until_complete(v.start())
        return (bool(ok),
                "LSP: OK — server WARMED at init (no first-verify cold-start)" if ok
                else "LSP: server did NOT warm — runtime verify would 0ms-fallback")
    except Exception as e:
        return (False, f"LSP: warm failed {e!r}")


def check_structure(graph_db: str | None) -> tuple[bool, str]:
    if not graph_db or not os.path.exists(graph_db):
        return (False, "STRUCT: no --graph-db given")
    try:
        c = sqlite3.connect(graph_db)
        types = dict(c.execute("SELECT type, COUNT(*) FROM edges GROUP BY type").fetchall())
        c.close()
        if len(types) <= 1:
            return (False, f"STRUCT: only {list(types)} edge type(s) — CALLS-only (no EXTENDS/CONTAINS/…)")
        return (True, f"STRUCT: OK — {len(types)} edge types {types}")
    except sqlite3.Error as e:
        return (False, f"STRUCT: query error {e!r}")


def _import_ok(mod: str) -> bool:
    try:
        __import__(mod)
        return True
    except Exception:
        return False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--graph-db", default=os.environ.get("GT_GRAPH_DB"))
    ap.add_argument("--workspace", default=os.environ.get("GT_WORKSPACE_ROOT"))
    args = ap.parse_args()

    checks = [
        ("FTS5", "GT_REQUIRE_FTS5", check_fts5(args.graph_db)),
        ("SEMANTIC", "GT_REQUIRE_EMBEDDER", check_semantic()),
        ("LSP", "GT_REQUIRE_LSP", check_lsp(args.workspace, args.graph_db)),
        ("STRUCT", "GT_REQUIRE_STRUCT", check_structure(args.graph_db)),
    ]

    hard_fail = False
    print("=" * 72)
    print("GT FULL-STACK PREFLIGHT (behavioral — probes real non-zero results)")
    print("=" * 72)
    for _name, flag, (ok, msg) in checks:
        req = _required(flag)
        status = "OK  " if ok else ("FAIL" if req else "warn")
        print(f"  [{status}] {msg}" + ("" if (ok or not req) else "   <-- REQUIRED"))
        if req and not ok:
            hard_fail = True
    print("=" * 72)
    if hard_fail:
        print("PREFLIGHT FAILED — refusing to start a degraded paid run. "
              "Fix the failing stage(s) above (build with -tags sqlite_fts5, "
              "install onnxruntime + bake models/e5-small-v2, install pyright).")
        return 1
    print("PREFLIGHT PASSED — full stack is live.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
