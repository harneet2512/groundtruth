"""Cluster C (Fable 2026-07-05): certificate / metric honesty in src/groundtruth/resolve.py.

Each test is RED on the pre-fix code and GREEN after. The behaviour-changers (C1/C2/C3/C8)
are exercised via extracted pure helpers; the inline cert-fill fixes (C4/C5/C9) are guarded
structurally against regression.

  C1 - an EXTERNAL resolution is real adjudication -> counted in effective_work (else an
       all-external warm pass reads 0 -> false `degraded` -> exit 2 under GT_REQUIRE_LSP=1).
  C2 - `--lang py` canonicalized to the stored nodes.language ('python') so the residual
       filter doesn't match ZERO rows -> false LSP_NO_OP_VALID.
  C3 - `_rebuild_closure` returns whether it ACTUALLY rebuilt; the cert stamps from that.
  C8 - scope path normalize strips the './' PREFIX only (`.github/x.js` preserved).
  C4 - the sibling-DELETE + external + skip counters are disclosed in the cert.
  C5 - the exit-2 degraded path re-stamps verdict_hint=LSP_DEGRADED_FAIL (cert agrees w/ exit).
  C9 - sibling collapse also requires len(locations)==1 (multi-definition symbol kept).
"""
from __future__ import annotations

import os
import sqlite3

from groundtruth.resolve import (
    _canonical_db_language,
    _compute_degraded,
    _effective_work,
    _rebuild_closure,
    _strip_rel_prefix,
)

_RESOLVE_SRC = os.path.join(
    os.path.dirname(__file__), "..", "..", "src", "groundtruth", "resolve.py"
)


# --- C1: external adjudication counts as effective work ---------------------- #
def test_c1_external_counts_as_effective_work():
    stats = {"verified": 0, "corrected": 0, "deleted": 0, "skipped_external": 4}
    assert _effective_work(stats) == 4  # RED pre-fix: excluded skipped_external -> 0
    # a warm pass whose only work was external adjudication must NOT be degraded
    assert _compute_degraded(lsp_warm=True, residual=4, effective_work=_effective_work(stats)) is False


def test_c1_pure_internal_work_still_counts():
    stats = {"verified": 2, "corrected": 1, "deleted": 1, "skipped_external": 0}
    assert _effective_work(stats) == 4


# --- C2: --lang alias canonicalized to stored nodes.language ----------------- #
def _py_graph(path: str) -> None:
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE nodes (id INTEGER PRIMARY KEY, name TEXT, language TEXT)"
    )
    conn.executemany(
        "INSERT INTO nodes (id, name, language) VALUES (?,?,?)",
        [(1, "a", "python"), (2, "b", "python"), (3, "c", "go")],
    )
    conn.commit()
    conn.close()


def test_c2_alias_maps_to_stored_language(tmp_path):
    db = str(tmp_path / "g.db")
    _py_graph(db)
    conn = sqlite3.connect(db)
    try:
        assert _canonical_db_language(conn, "py") == "python"   # RED pre-fix: stayed 'py' -> 0 rows
        assert _canonical_db_language(conn, "python") == "python"
        assert _canonical_db_language(conn, "go") == "go"
        # correct-or-quiet: a language absent from the graph is returned unchanged
        assert _canonical_db_language(conn, "rust") == "rust"
        assert _canonical_db_language(conn, None) is None
    finally:
        conn.close()


# --- C3: _rebuild_closure reports whether it actually rebuilt ---------------- #
def test_c3_rebuild_closure_returns_false_when_binary_absent(tmp_path, monkeypatch):
    monkeypatch.delenv("GT_PROOF_MODE", raising=False)  # non-proof: warn + continue
    monkeypatch.setenv("GT_INDEX_BIN", str(tmp_path / "does_not_exist_gt_index"))
    monkeypatch.setattr("shutil.which", lambda _n: None)
    assert _rebuild_closure(str(tmp_path / "graph.db")) is False  # RED pre-fix: returned None


# --- C8: scope-path normalize preserves dot-directories --------------------- #
def test_c8_strip_rel_prefix_preserves_dot_dirs():
    assert _strip_rel_prefix(".github/actions/x.js") == ".github/actions/x.js"  # RED: was 'github/...'
    assert _strip_rel_prefix("./a/b.py") == "a/b.py"
    assert _strip_rel_prefix("././a.py") == "a.py"
    assert _strip_rel_prefix("src\\pkg\\m.go") == "src/pkg/m.go"
    assert _strip_rel_prefix(".env") == ".env"  # dotfile preserved


# --- C4/C5/C9: inline cert-fill / exit-path regression guards ---------------- #
def _resolve_source() -> str:
    with open(_RESOLVE_SRC, encoding="utf-8") as fh:
        return fh.read()


def test_c4_cert_discloses_work_counters():
    src = _resolve_source()
    for field in (
        "deduped_sibling_edges",
        "skipped_external_edges",
        "skipped_no_call_site_edges",
        "sibling_delete_skipped_multicall_edges",
    ):
        assert f'cert["{field}"]' in src, f"cert must disclose {field}"


def test_c5_exit2_restamps_degraded_verdict():
    src = _resolve_source()
    # on the degraded + GT_REQUIRE_LSP exit path the cert is re-stamped + re-written
    assert 'cert["verdict_hint"] = "LSP_DEGRADED_FAIL"' in src


def test_c9_sibling_collapse_requires_single_location():
    src = _resolve_source()
    assert "_n_callsites == 1 and len(locations) == 1" in src
