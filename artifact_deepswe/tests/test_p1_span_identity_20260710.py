"""P1 SPAN-IDENTITY FOUNDATION — RED-first tests (L-6 / E-1 / V-2 / O-1 / D-6).

These assert the span-based edited-symbol identity that replaces token-guessing at
the covering / risk / horizon / obligation consumers. Each test RED's on the
pre-fix behaviour (token-only selection blind to body edits; command-verb 'sed'
pollution; compound per-token false 'hit'; edit_risk start_line-only containment).
Hermetic: a tiny on-disk graph.db per test, module oracle state reset.
"""
from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
for _p in (str(_REPO / "artifact_deepswe"), str(_REPO / "src")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import gt_mini_patch as g  # noqa: E402


def _mk_graph(tmp: Path) -> str:
    """get_user (a/x.py:1-5) called by handler (b/y.py) + test_get_user (test)."""
    db = str(tmp / "graph.db")
    con = sqlite3.connect(db)
    con.executescript(
        "CREATE TABLE nodes(id INTEGER PRIMARY KEY,label TEXT,name TEXT,"
        "qualified_name TEXT,file_path TEXT,start_line INTEGER,end_line INTEGER,"
        "signature TEXT,return_type TEXT,is_exported INTEGER,is_test INTEGER,"
        "language TEXT,parent_id INTEGER);"
        "CREATE TABLE edges(id INTEGER PRIMARY KEY,source_id INTEGER,target_id INTEGER,"
        "type TEXT,source_line INTEGER,source_file TEXT,resolution_method TEXT,"
        "confidence REAL,metadata TEXT);")
    con.executemany(
        "INSERT INTO nodes VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [(1, "Function", "get_user", "a.x.get_user", "a/x.py", 1, 5,
          "def get_user(uid)", None, 1, 0, "python", None),
         (2, "Function", "handler", "b.y.handler", "b/y.py", 3, 4,
          "def handler(uid)", None, 1, 0, "python", None),
         (3, "Function", "test_get_user", "tests.test_x.test_get_user",
          "tests/test_x.py", 3, 4, "def test_get_user()", None, 0, 1, "python", None)])
    con.executemany(
        "INSERT INTO edges(source_id,target_id,type,source_line,source_file,"
        "resolution_method,confidence) VALUES(?,?,?,?,?,?,?)",
        [(2, 1, "CALLS", 4, "b/y.py", "import", 1.0),
         (3, 1, "CALLS", 4, "tests/test_x.py", "import", 1.0)])
    con.commit()
    con.close()
    return db


@pytest.fixture()
def wired(tmp_path, monkeypatch):
    monkeypatch.setenv("GT_CERT_DIR", str(tmp_path / "certs"))
    (tmp_path / "certs").mkdir(exist_ok=True)
    for k in ("GT_BASELINE", "GT_POST_SEARCH_NATIVE"):
        monkeypatch.delenv(k, raising=False)
    db = _mk_graph(tmp_path)
    monkeypatch.setattr(g, "_db_path", lambda: db)
    monkeypatch.setattr(g, "_root", lambda: str(tmp_path))
    g._GT_BASELINE = False
    g._POST_SEARCH_ON = True
    g._reset_oracle_state()
    return g, tmp_path


# ── L-6: span maps a BODY-only edit line to the enclosing symbol ─────────────
def test_span_maps_body_line_to_enclosing_symbol(wired):
    m, _tmp = wired
    # a body line (line 3) inside get_user's span [1,5]; NO token spells get_user
    m._oracle_edited_lines_by_file["a/x.py"] = [(3, 3)]
    assert m._edited_symbol_spans_by_file() == {"a/x.py": {"get_user"}}
    assert m._edited_symbol_spans() == {"get_user"}


def test_selection_prefers_spans_falls_back_to_tokens(wired):
    m, _tmp = wired
    # spans present -> selection is the span symbol, NOT the noisy tokens
    m._oracle_edited_lines_by_file["a/x.py"] = [(3, 3)]
    m._oracle_edited_tokens.update({"sed", "uid", "return"})
    assert m._edited_symbols_for_selection() == {"get_user"}
    # spans absent -> fall back to tokens (byte-identical to today)
    m._oracle_edited_lines_by_file.clear()
    assert m._edited_symbols_for_selection() == {"sed", "uid", "return"}


def test_model_visible_symbol_claim_requires_graph_span(wired):
    m, _tmp = wired
    m._oracle_edited_tokens.update({"False", "PYEOF", "Could"})
    assert m._verified_edited_symbol_for_rendering(
        m._edited_symbols_for_selection()
    ) is None

    m._oracle_edited_lines_by_file["a/x.py"] = [(3, 3)]
    selected = m._edited_symbols_for_selection()
    assert selected == {"get_user"}
    assert m._verified_edited_symbol_for_rendering(selected) == "get_user"


def test_body_edit_selects_covering_test(wired):
    m, _tmp = wired
    m._oracle_edited_lines_by_file["a/x.py"] = [(3, 3)]
    cov = m._covering_tests_for_symbols(m._edited_symbols_for_selection())
    assert cov and any(c["file"] == "tests/test_x.py" for c in cov)


# ── D-6: command-verb 'sed' is stripped from the edited-token set ────────────
def test_edit_body_tokens_strips_command_verb():
    toks = g._edit_body_tokens("sed -i 's/return foo/return bar/' a/x.py")
    assert "sed" not in toks
    assert "return" in toks  # authored content survives


def test_strip_cmd_noise_drops_invoked_program():
    assert g._strip_cmd_noise({"awk", "myfunc", "value"},
                              "awk '{print}' a/x.py") == {"myfunc", "value"}


# ── D-6: per-token truth on a multi-token isolated grep ─────────────────────
def test_multitoken_grep_records_per_token_truth(wired):
    m, _tmp = wired
    # isolated quoted-phrase grep; output contains only get_user, never 'zzz'
    out = "a/x.py:1:def get_user(uid):"
    m._search_localize_block("grep -rn 'zzz get_user' .", out)
    pl = {k: v["outcomes"] for k, v in m._EPISODE.probe_ledger.items()}
    # get_user is present -> hit; zzz absent -> zero (NOT the compound's 'hit')
    assert pl.get(m._norm_stem("get_user")) == ["hit"]
    assert pl.get(m._norm_stem("zzz")) == ["zero"]


def test_singletoken_grep_hit_regardless_of_output_form(wired):
    m, _tmp = wired
    # a single-token grep with -l output (filenames only, token not literally present)
    m._search_localize_block("grep -rln get_user .", "a/x.py\nb/y.py")
    pl = {k: v["outcomes"] for k, v in m._EPISODE.probe_ledger.items()}
    assert pl.get(m._norm_stem("get_user")) == ["hit"]  # matched, not false-'zero'


# ── E-1: edit_risk span containment counts a body edit ──────────────────────
def test_edit_risk_span_containment_counts_body_edit(wired):
    from groundtruth.runtime.edit_risk import structural_edit_risk
    m, tmp = wired
    db = m._db_path()
    # edit at line 3 (body) — get_user's DEF line is 1, OUTSIDE the hunk. Under the
    # old start_line-only rule this scored 0; span containment [1,5] ∩ [3,3] counts it.
    er = structural_edit_risk(
        db, {"get_user"}, edited_files={"a/x.py"},
        edited_ranges={"a/x.py": [(3, 3)]})
    assert not er.is_quiet()
    assert er.top_reason().name == "get_user"
    assert er.top_reason().dependents == 2


def test_edit_risk_body_edit_would_be_zero_without_span(wired):
    """Control: the def-line hunk also counts (unchanged), proving the fixture."""
    from groundtruth.runtime.edit_risk import structural_edit_risk
    m, _tmp = wired
    er = structural_edit_risk(
        m._db_path(), {"get_user"}, edited_files={"a/x.py"},
        edited_ranges={"a/x.py": [(1, 1)]})
    assert not er.is_quiet()


# ── O-1 / P1(d): obligation edit-credit is span-augmented ───────────────────
def test_obligations_edited_set_unions_spans(wired):
    m, _tmp = wired
    m._oracle_edited_lines_by_file["a/x.py"] = [(3, 3)]
    m._oracle_edited_tokens.update({"uid"})
    got = m._edited_symbols_for_obligations()
    assert "get_user" in got and "uid" in got  # union: span symbol + raw token
