"""Whole-graph FINALIZE of graph_hash_after_lsp — multi-language correctness (red->green).

Reproduces the katex GRAPH_FAIL_HASH_MISMATCH root cause: on a polyglot repo the canonical
LSP cert is chosen by DECLARED language, but a non-declared language can mutate the shared
graph.db AFTER the declared language's per-language snapshot -> the canonical cert freezes a
STALE graph_hash_after_lsp -> the hook/graph_certificate false-fail on a good graph.

_finalize_after_lsp_hash re-snapshots the authority hash ONCE over the FINAL graph.db and
stamps it into the canonical cert. Asserted:
  1. GREEN: a stale canonical after_lsp is refreshed to the FINAL graph's edge hash
     (== proof.graph_edges_hash — the SAME hash the witness + graph_certificate use).
  2. NO-OP: when the cert already carries the final hash (single-language / declared-was-last),
     the cert is left BYTE-IDENTICAL (determinism preserved) and the fn returns None.
  3. ABSTAIN: missing cert / unreadable db -> None, no crash, no write.

RED proof (mutation): revert _finalize_after_lsp_hash to `return None` and test 1 fails
(the stale hash is never refreshed -> assertion trips). Restore -> green.
"""
from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_SRC = _ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


def _load_gt_run_proof():
    path = _ROOT / "scripts" / "swebench" / "gt_run_proof.py"
    spec = importlib.util.spec_from_file_location("gt_run_proof_finalize_test", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def grp():
    return _load_gt_run_proof()


def _make_graph(db_path: str, n_edges: int) -> None:
    con = sqlite3.connect(db_path)
    con.executescript(
        """
        CREATE TABLE nodes (id INTEGER PRIMARY KEY, name TEXT, file_path TEXT, language TEXT);
        CREATE TABLE edges (
            id INTEGER PRIMARY KEY, source_id INTEGER, target_id INTEGER, type TEXT,
            resolution_method TEXT, confidence REAL
        );
        """
    )
    con.execute("INSERT INTO nodes(id,name,file_path,language) VALUES (1,'a','a.ts','typescript')")
    con.execute("INSERT INTO nodes(id,name,file_path,language) VALUES (2,'b','b.js','javascript')")
    for i in range(1, n_edges + 1):
        con.execute(
            "INSERT INTO edges(id,source_id,target_id,type,resolution_method,confidence) "
            "VALUES (?,?,?,?,?,?)",
            (i, 1, 2, "CALLS", "import", 1.0),
        )
    con.commit()
    con.close()


def _edge_hash(db_path: str) -> str:
    from groundtruth.runtime import proof as _proof
    return _proof.graph_edges_hash(db_path)


def _write_cert(cert_path: str, after_lsp: str, lang: str = "javascript") -> None:
    with open(cert_path, "w", encoding="utf-8") as fh:
        json.dump(
            {"schema": "gt.lsp_certificate.v2", "language": lang,
             "graph_hash_before_lsp": "before", "graph_hash_after_lsp": after_lsp,
             "closure_hash_after_rebuild": after_lsp, "verified_edges": 0,
             "corrected_edges": 0, "deleted_edges": 1},
            fh, indent=2,
        )


# ── Test 1 (GREEN + the RED target): stale declared-language hash is refreshed ──────────
def test_stale_after_lsp_refreshed_to_final_graph(grp, tmp_path):
    """The katex shape: canonical cert (declared=js) froze a stale hash; a later language
    grew the graph. Finalize must rewrite after_lsp to the FINAL graph's edge hash."""
    db = str(tmp_path / "graph.db")
    _make_graph(db, n_edges=3704)              # the FINAL graph (post typescript pass)
    final = _edge_hash(db)
    cert = str(tmp_path / "lsp_certificate.json")
    _write_cert(cert, after_lsp="9aeb9dc8_STALE_js_snapshot")   # declared-lang stale snapshot

    result = grp._finalize_after_lsp_hash(db, cert)

    # a refresh happened, returning (stale, final)
    assert result is not None, "finalize must refresh a stale canonical after_lsp (RED if None)"
    assert result == ("9aeb9dc8_STALE_js_snapshot", final)
    # the canonical cert now carries the FINAL graph hash — the witness will MATCH
    written = json.load(open(cert, encoding="utf-8"))
    assert written["graph_hash_after_lsp"] == final
    assert written["closure_hash_after_rebuild"] == final
    # honesty trail records what it replaced
    assert written["graph_hash_after_lsp_refreshed_from"] == "9aeb9dc8_STALE_js_snapshot"


# ── Test 2 (NO-OP determinism): already-final hash -> byte-identical, returns None ──────
def test_already_final_is_byte_identical_noop(grp, tmp_path):
    """Single-language repo (or declared==last): the cert already holds the final hash ->
    no rewrite, cert byte-identical, returns None (determinism preserved)."""
    db = str(tmp_path / "graph.db")
    _make_graph(db, n_edges=800)
    final = _edge_hash(db)
    cert = str(tmp_path / "lsp_certificate.json")
    _write_cert(cert, after_lsp=final)          # cert already == final graph
    before_bytes = Path(cert).read_bytes()

    result = grp._finalize_after_lsp_hash(db, cert)

    assert result is None, "no refresh when the cert already holds the final hash"
    assert Path(cert).read_bytes() == before_bytes, "cert must be byte-identical (determinism)"


# ── Test 3 (ABSTAIN): missing cert / unreadable db -> None, no crash ────────────────────
def test_abstain_on_missing_cert(grp, tmp_path):
    db = str(tmp_path / "graph.db")
    _make_graph(db, n_edges=10)
    missing_cert = str(tmp_path / "does_not_exist.json")
    assert grp._finalize_after_lsp_hash(db, missing_cert) is None


def test_abstain_on_unreadable_db(grp, tmp_path):
    cert = str(tmp_path / "lsp_certificate.json")
    _write_cert(cert, after_lsp="stale")
    result = grp._finalize_after_lsp_hash(str(tmp_path / "no_such.db"), cert)
    assert result is None
    # cert untouched
    assert json.load(open(cert, encoding="utf-8"))["graph_hash_after_lsp"] == "stale"
