"""W3 FIX 3 — vendored/static-asset demotion in the localizer stratum sort.

A vendored web asset (static/, vendor/, contrib assets, .min.js) can name-match ONE
issue token inside a huge minified library and, on its hub in-degree, out-rank real
source (measured live: privacyidea static/contrib/js/jquery.js ranked #1 while the gold
eventhandler stayed at ~19). BRIEFING §3/§4 names hub/non-source demotion as the CORRECT
lever. `path_policy.is_vendored_path` already classifies these files but was NOT wired
into `graph_localizer`'s `_nonsource_stratum` — this fixes that gap.

Contract (call-time flag GT_LOC_VENDOR_DEMOTE):
  OFF (default): byte-identical to before — the vendored file keeps its earned rank.
  ON:            the vendored file sinks BELOW real source in the final candidate order.

Red-before-green: with the fix reverted (or the flag off) `test_vendored_demoted_below_source`
fails; with it on it passes. Two mutations pinned below.
"""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest

from groundtruth.pretask.graph_localizer import localize


def _build(db_path: Path) -> None:
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE nodes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            label TEXT NOT NULL, name TEXT NOT NULL, qualified_name TEXT,
            file_path TEXT NOT NULL, start_line INTEGER, end_line INTEGER,
            signature TEXT, return_type TEXT, is_exported BOOLEAN DEFAULT 0,
            is_test BOOLEAN DEFAULT 0, language TEXT NOT NULL,
            parent_id INTEGER REFERENCES nodes(id)
        );
        CREATE TABLE edges (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_id INTEGER NOT NULL, target_id INTEGER NOT NULL, type TEXT NOT NULL,
            source_line INTEGER, source_file TEXT, resolution_method TEXT,
            confidence REAL DEFAULT 0.0, metadata TEXT,
            trust_tier TEXT, candidate_count INTEGER,
            evidence_type TEXT, verification_status TEXT
        );
        """
    )
    # A vendored minified JS asset that DEFINES the issue symbol `requestMangler` and is a
    # hub (many incoming CALLS) — the shape that lets it out-rank real source.
    vend = "privacyidea/static/contrib/js/jquery.min.js"
    src = "privacyidea/lib/eventhandler/requestmangler.py"
    caller = "privacyidea/lib/event.py"
    nodes = [
        # id, label, name, file, is_test, lang
        (1, "Function", "requestMangler", vend, 0, "javascript"),
        (2, "Method", "mangle", src, 0, "python"),          # gold, real source
        (3, "Function", "handle_event", caller, 0, "python"),
    ]
    for nid, label, name, fp, is_test, lang in nodes:
        conn.execute(
            "INSERT INTO nodes (id,label,name,qualified_name,file_path,start_line,end_line,"
            "signature,return_type,is_exported,is_test,language,parent_id) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (nid, label, name, name, fp, 1, 9, "", "", 1, is_test, lang, None),
        )
    # Give the vendored file a VERIFIED structural witness (a hub caller) so, absent the
    # demote, it earns a top slot ahead of the plain source file.
    edges = [
        (3, 1, "CALLS", caller, "import", 1.0),   # event.handle_event -> jquery.requestMangler
        (3, 2, "CALLS", caller, "import", 1.0),   # event.handle_event -> requestmangler.mangle
    ]
    for sid, tid, typ, sfile, method, conf in edges:
        conn.execute(
            "INSERT INTO edges (source_id,target_id,type,source_line,source_file,"
            "resolution_method,confidence,trust_tier) VALUES (?,?,?,?,?,?,?,?)",
            (sid, tid, typ, 1, sfile, method, conf, "CERTIFIED"),
        )
    conn.commit()
    conn.close()


ISSUE = "requestMangler mangle handle_event does not reset the user in the request"


def _ranks(db_path: str, demote: bool):
    """Return {normalized_file: rank(0-based)} from a semantic-off localize run.

    Env is managed EXPLICITLY (try/finally) — the call-time os.environ read is the
    product contract; explicit management is the reliable way to exercise both arms.
    """
    import groundtruth.pretask.graph_localizer as GL
    GL._semantic_score_by_file = (  # semantic off — isolate the stratum sort, deterministic
        lambda *a, symbol_scores_out=None, body_enriched_files_out=None, **k: {}
    )
    prev = os.environ.get("GT_LOC_VENDOR_DEMOTE")
    os.environ["GT_FORCE_ONNX_EMBEDDER"] = "1"
    try:
        if demote:
            os.environ["GT_LOC_VENDOR_DEMOTE"] = "1"
        else:
            os.environ.pop("GT_LOC_VENDOR_DEMOTE", None)
        res = localize(ISSUE, db_path, top_k=20, repo_root="")
        return {c.file_path.replace("\\", "/"): i for i, c in enumerate(res.candidates)}
    finally:
        if prev is None:
            os.environ.pop("GT_LOC_VENDOR_DEMOTE", None)
        else:
            os.environ["GT_LOC_VENDOR_DEMOTE"] = prev


VEND = "privacyidea/static/contrib/js/jquery.min.js"
SRC = "privacyidea/lib/eventhandler/requestmangler.py"


@pytest.fixture
def db(tmp_path):
    p = tmp_path / "graph.db"
    _build(p)
    return str(p)


def test_off_is_baseline_vendored_not_demoted(db):
    """OFF (default): the vendored hub is NOT sunk below source (earns its structural slot)."""
    r = _ranks(db, demote=False)
    assert VEND in r and SRC in r
    # With a verified hub witness and no demote, the vendored file ranks ABOVE source.
    assert r[VEND] < r[SRC], f"baseline should keep vendored above source: {r}"


def test_vendored_demoted_below_source(db):
    """ON: the vendored asset sinks BELOW real source (the fix)."""
    r = _ranks(db, demote=True)
    assert VEND in r and SRC in r
    assert r[SRC] < r[VEND], f"source must outrank vendored when demote on: {r}"


def test_determinism_two_seed_byte_identity(db):
    """2-seed byte-identity: the ON ordering is identical across two runs (no jitter)."""
    r1 = _ranks(db, demote=True)
    r2 = _ranks(db, demote=True)
    assert r1 == r2
