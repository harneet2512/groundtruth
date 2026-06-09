"""LSP stamping/measurement-integrity tests (Priority 1).

Prove: the gate counts LSP-resolved edges from the FINAL graph (persisted resolution_method='lsp'),
that count survives a closure-table rebuild, and a cert-resolved>0 / graph-lsp==0 mismatch fails
closed (LSP_STAMP_DROPPED_AFTER_RESOLVE) — while a cert==0 no-op/unsupported stays consistent (not
faked). No SWE-bench tasks, no gold.
"""
import importlib.util
import json
import os
import sqlite3
import sys

ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
sys.path.insert(0, os.path.join(ROOT, "src"))
_FG = os.path.join(ROOT, "scripts", "metrics", "foundational_gates.py")
_spec = importlib.util.spec_from_file_location("fg_stamp", _FG)
fg = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fg)


def _mkgraph(path, lsp_edges, name_match_edges):
    c = sqlite3.connect(path)
    c.execute("CREATE TABLE edges (id INTEGER PRIMARY KEY, source_id INT, target_id INT, type TEXT, "
              "resolution_method TEXT, confidence REAL, trust_tier TEXT)")
    c.execute("CREATE TABLE closure (source INT, target INT, depth INT, min_confidence REAL)")
    rid = 1
    for _ in range(lsp_edges):
        c.execute("INSERT INTO edges VALUES (?,?,?,?,?,?,?)", (rid, rid, rid + 1, "CALLS", "lsp", 1.0, "CERTIFIED")); rid += 1
    for _ in range(name_match_edges):
        c.execute("INSERT INTO edges VALUES (?,?,?,?,?,?,?)", (rid, rid, rid + 1, "CALLS", "name_match", 0.2, "SPECULATIVE")); rid += 1
    c.execute("INSERT INTO closure VALUES (1,2,1,1.0)")
    c.commit(); c.close()


# ── canonical graph count ─────────────────────────────────────────────────────

def test_lsp_graph_count_counts_persisted_lsp_edges(tmp_path):
    db = str(tmp_path / "g.db"); _mkgraph(db, lsp_edges=247, name_match_edges=788)
    assert fg._lsp_graph_count(db) == 247


def test_lsp_graph_count_zero_when_none_stamped(tmp_path):
    db = str(tmp_path / "g.db"); _mkgraph(db, lsp_edges=0, name_match_edges=500)
    assert fg._lsp_graph_count(db) == 0


# ── #1: lsp stamps survive a closure-table rebuild (only closure is cleared) ──

def test_lsp_stamps_survive_closure_rebuild(tmp_path):
    db = str(tmp_path / "g.db"); _mkgraph(db, lsp_edges=247, name_match_edges=788)
    before = fg._lsp_graph_count(db)
    # simulate `gt-index -rebuild-closure`: ClearClosure + recompute closure ONLY (edges untouched)
    c = sqlite3.connect(db); c.execute("DELETE FROM closure")
    c.execute("INSERT INTO closure VALUES (1,3,2,1.0)"); c.commit(); c.close()
    assert fg._lsp_graph_count(db) == before == 247  # edges + their lsp stamps untouched


# ── #2 + #5: cross-check cert vs final graph ──────────────────────────────────

def test_stamp_check_dropped_when_cert_pos_graph_zero():
    assert fg.lsp_stamp_check(graph_lsp=0, cert_resolved=247) == "LSP_STAMP_DROPPED_AFTER_RESOLVE"


def test_stamp_check_ok_when_both_positive():
    assert fg.lsp_stamp_check(graph_lsp=247, cert_resolved=247) == ""
    assert fg.lsp_stamp_check(graph_lsp=200, cert_resolved=247) == ""  # any persisted >0 is consistent


def test_stamp_check_noop_unsupported_not_faked():
    # cert==0 (unsupported / no-op) + graph==0 -> consistent, NOT flagged, NOT faked as pass
    assert fg.lsp_stamp_check(graph_lsp=0, cert_resolved=0) == ""


# ── cert resolved read ────────────────────────────────────────────────────────

def test_cert_resolved_reads_verified_plus_corrected(tmp_path, monkeypatch):
    cert = tmp_path / "lsp_certificate.json"
    cert.write_text(json.dumps({"verified_edges": 200, "corrected_edges": 47}))
    monkeypatch.setenv("GT_LSP_CERT", str(cert))
    assert fg._lsp_cert_resolved() == 247
