"""Pins for the post_search (M0) facts channel — gt_mini_patch.

post_search answers the agent's OWN repo-wide grep with definition FACTS, in-band.
Each test names the invariant it guards; reverting the guard reddens it:
  * _search_pattern ABSTAINS on anything but a bare symbol (else it would answer
    garbage on a regex/path/multi-word grep).
  * the block leaks ZERO test names (test refs are a COUNT) — the audit leak rule.
  * an ambiguous common name (>3 def files) ABSTAINS (join/get/append shadow).
  * fire-once per symbol; DEFAULT-OFF flag; byte-identical determinism.

Hermetic: a synthetic graph.db (no ONNX, no repo checkout). The verified-caller
LINE reuses _caller_contract_for_file (its own suite + the live witness cover it);
these pins target the post_search-specific logic.
"""
from __future__ import annotations

import sqlite3

import pytest

import gt_mini_patch as g


# ---- _search_pattern: parse the operand, ABSTAIN unless a bare symbol --------
@pytest.mark.parametrize("cmd,want", [
    ("grep -rn set_fields .", "set_fields"),
    ("grep -rIn --include=*.py set_fields src/", "set_fields"),
    ("rg parse_config", "parse_config"),
    ("grep -e Eval .", "Eval"),
    ("grep -A 3 handle_request .", "handle_request"),   # -A consumes '3', not the pattern
    # value-flag misfires (Fable #3) — the flag's value must not become the pattern
    ("rg -t rust Widget", "Widget"),                    # -t consumes 'rust'
    ("grep -r --exclude-dir tests MyFunc .", "MyFunc"),  # --exclude-dir consumes 'tests'
    ("grep --regexp=verify src", "verify"),             # --regexp=VALUE carries the pattern
    ("grep --include=*.py set_fields src", "set_fields"),  # --include=VALUE is a flag, not pattern
    ("grep -rn -- foo .", "foo"),                        # after --, next token is the pattern
    ("grep -rn -- -foo utils", None),                    # after --, '-foo' is not a bare symbol
    # -E / -T are VALUELESS in GNU grep — must NOT consume the pattern (Fable re-verify)
    ("grep -E TimeoutError utils", "TimeoutError"),      # -E (extended-regexp), not value-taking
    ("grep -rn -E get_user src", "get_user"),            # -E must not promote 'src'
    ("grep -T bar .", "bar"),                            # -T (--initial-tab) is valueless
    ('rg "def foo"', None),          # multi-word regex -> abstain
    ("grep -rn a .", None),          # <3 chars -> abstain (huge match set)
    ("grep -rn ab .", None),         # 2 chars -> abstain
    ("cat foo.py", None),            # not a grep
    ("ls -la src/", None),           # not a search
    ("grep -rn 'a.*b' .", None),     # regex metachars -> abstain
    ("grep -rn user/config .", None),  # path -> abstain
])
def test_search_pattern_abstains_unless_bare_symbol(cmd, want):
    assert g._search_pattern(cmd) == want


# ---- synthetic graph fixture -------------------------------------------------
def _mk_graph(tmp_path, *, ambiguous=False):
    db = str(tmp_path / "graph.db")
    con = sqlite3.connect(db)
    con.executescript(
        """
        CREATE TABLE nodes(id INTEGER PRIMARY KEY, label TEXT, name TEXT,
          qualified_name TEXT, file_path TEXT, start_line INTEGER, end_line INTEGER,
          signature TEXT, return_type TEXT, is_exported INTEGER, is_test INTEGER,
          language TEXT, parent_id INTEGER);
        CREATE TABLE edges(id INTEGER PRIMARY KEY, source_id INTEGER, target_id INTEGER,
          type TEXT, source_line INTEGER, source_file TEXT, resolution_method TEXT,
          confidence REAL, metadata TEXT, trust_tier TEXT, candidate_count INTEGER,
          evidence_type TEXT, verification_status TEXT);
        """
    )
    # the definition
    con.execute(
        "INSERT INTO nodes(id,label,name,file_path,start_line,end_line,signature,"
        "is_test,language) VALUES(1,'Function','set_fields','beets/importer.py',"
        "1642,1687,'set_fields(self, fields)',0,'python')")
    # a NON-test caller over a deterministic edge (verified)
    con.execute(
        "INSERT INTO nodes(id,label,name,file_path,start_line,end_line,is_test,"
        "language) VALUES(2,'Function','apply_edits','beets/ui/commands.py',88,99,0,'python')")
    con.execute(
        "INSERT INTO edges(id,source_id,target_id,type,source_line,resolution_method,"
        "confidence) VALUES(1,2,1,'CALLS',90,'import',1.0)")
    # a TEST caller — its NAME must NEVER surface; only the COUNT may.
    con.execute(
        "INSERT INTO nodes(id,label,name,file_path,start_line,end_line,is_test,"
        "language) VALUES(3,'Function','test_set_fields','beets/test/test_importer.py',10,20,1,'python')")
    con.execute(
        "INSERT INTO edges(id,source_id,target_id,type,source_line,resolution_method,"
        "confidence) VALUES(2,3,1,'CALLS',12,'import',1.0)")
    if ambiguous:
        # SAME name defined in 4 files -> ambiguous -> not a fact.
        for i, f in enumerate(["a/x.py", "b/x.py", "c/x.py", "d/x.py"], start=10):
            con.execute(
                "INSERT INTO nodes(id,label,name,file_path,start_line,is_test,language) "
                f"VALUES({i},'Function','common_name','{f}',{i},0,'python')")
    con.commit()
    con.close()
    return db


@pytest.fixture
def _on(tmp_path, monkeypatch):
    """post_search ON, pointed at a synthetic graph + tmp repo root."""
    db = _mk_graph(tmp_path)
    monkeypatch.setattr(g, "_POST_SEARCH_ON", True)
    monkeypatch.setattr(g, "_db_path", lambda: db)
    monkeypatch.setattr(g, "_root", lambda: str(tmp_path))
    g._search_seen.clear()
    yield db
    g._search_seen.clear()


def test_block_renders_def_site(_on):
    block = g._search_localize_block("grep -rn set_fields .")
    assert 'symbol="set_fields"' in block
    assert "def: beets/importer.py:1642" in block


def test_leak_no_test_names(_on):
    """The leak invariant: test refs are a COUNT; no test file/function name."""
    block = g._search_localize_block("grep -rn set_fields .")
    assert "test refs: 1" in block           # the count is allowed
    assert "test_set_fields" not in block    # the NAME is not
    assert "test_importer" not in block


def test_ambiguous_common_name_abstains(tmp_path, monkeypatch):
    db = _mk_graph(tmp_path, ambiguous=True)
    monkeypatch.setattr(g, "_POST_SEARCH_ON", True)
    monkeypatch.setattr(g, "_db_path", lambda: db)
    monkeypatch.setattr(g, "_root", lambda: str(tmp_path))
    g._search_seen.clear()
    # 4 def files -> not a fact -> abstain
    assert g._search_localize_block("grep -rn common_name .") == ""


def test_fire_once_per_symbol(_on):
    first = g._search_localize_block("grep -rn set_fields .")
    assert first
    assert g._search_localize_block("grep -rn set_fields .") == ""  # latched


def test_flag_off_is_silent(tmp_path, monkeypatch):
    db = _mk_graph(tmp_path)
    monkeypatch.setattr(g, "_POST_SEARCH_ON", False)   # DEFAULT
    monkeypatch.setattr(g, "_db_path", lambda: db)
    monkeypatch.setattr(g, "_root", lambda: str(tmp_path))
    g._search_seen.clear()
    assert g._search_localize_block("grep -rn set_fields .") == ""


def test_unknown_symbol_abstains(_on):
    assert g._search_localize_block("grep -rn zzz_absent_symbol .") == ""


def test_determinism_byte_identical(_on):
    a = g._search_localize_block("grep -rn set_fields .")
    g._search_seen.clear()
    b = g._search_localize_block("grep -rn set_fields .")
    assert a == b and a != ""


# ---- BUG-B1b: the out=str PRODUCTION path must emit (every pin above uses out=None) ----
def test_out_str_production_path_emits(_on):
    """The production caller passes out=str (the agent's OWN grep result), which routes
    through the LISTEN-LATTICE + BUG-B1 fall-through to _direct_def_block. That block
    SELF-marks the content-hash latch; the outer idempotence re-check must NOT re-detect
    the self-mark and suppress the FIRST delivery. Regression for the measured structural
    mute: `grep -rn aware_now` on the gold symbol emitted nothing in the live run while the
    out=None path emitted the def-site. Reverting the _direct_def_latched early-return reddens this."""
    out = ("beets/importer.py:1642:def set_fields(self, fields):\n"
           "beets/ui/commands.py:90:        obj.set_fields(f)")
    block = g._search_localize_block("grep -rn set_fields .", out)
    assert block, "post_search is MUTE on the out=str production path (BUG-B1b)"
    assert 'symbol="set_fields"' in block
    assert "def: beets/importer.py:1642" in block


def test_out_str_idempotence_preserved(_on):
    """out=str: first delivery emits; a REPEAT grep of the same symbol is latched to ''
    (idempotence carried by _direct_def_block's own content-hash latch, not the outer one)."""
    out = "beets/importer.py:1642:def set_fields(self, fields):"
    first = g._search_localize_block("grep -rn set_fields .", out)
    assert first
    assert g._search_localize_block("grep -rn set_fields .", out) == ""


# ---- Fable #1: a def in a test path must NEVER be rendered (leak invariant) ----
def _mk_defs_graph(tmp_path, defs):
    """defs = list of (file_path, is_test). Symbol name is always 'helper_fn'."""
    db = str(tmp_path / "graph.db")
    con = sqlite3.connect(db)
    con.executescript(
        "CREATE TABLE nodes(id INTEGER PRIMARY KEY, label TEXT, name TEXT,"
        " file_path TEXT, start_line INTEGER, end_line INTEGER, is_test INTEGER,"
        " language TEXT);"
        "CREATE TABLE edges(id INTEGER PRIMARY KEY, source_id INTEGER, target_id INTEGER,"
        " type TEXT, source_line INTEGER, resolution_method TEXT, confidence REAL);")
    for i, (fp, is_test) in enumerate(defs, start=1):
        con.execute(
            "INSERT INTO nodes(id,label,name,file_path,start_line,end_line,is_test,language)"
            f" VALUES({i},'Function','helper_fn','{fp}',{i*10},{i*10+5},{is_test},'python')")
    con.commit()
    con.close()
    return db


def test_def_in_test_path_is_filtered(tmp_path, monkeypatch):
    """conftest.py / tests.py defs (is_test=0 — the walker misses them) must not
    surface; a real-path def must. Reverting the leak-guard filter reddens this."""
    db = _mk_defs_graph(tmp_path, [
        ("conftest.py", 0),          # test-path basename, is_test NOT set
        ("app/tests.py", 0),         # Django convention, is_test NOT set
        ("pkg/util.py", 0),          # the only real def
    ])
    monkeypatch.setattr(g, "_POST_SEARCH_ON", True)
    monkeypatch.setattr(g, "_db_path", lambda: db)
    monkeypatch.setattr(g, "_root", lambda: str(tmp_path))
    g._search_seen.clear()
    block = g._search_localize_block("grep -rn helper_fn .")
    assert "pkg/util.py" in block
    assert "conftest.py" not in block
    assert "tests.py" not in block


def test_only_test_path_defs_abstains(tmp_path, monkeypatch):
    """If EVERY def is test-path-shaped, abstain entirely (no def to safely show)."""
    db = _mk_defs_graph(tmp_path, [("conftest.py", 0), ("foo/bar_test.go", 0)])
    monkeypatch.setattr(g, "_POST_SEARCH_ON", True)
    monkeypatch.setattr(g, "_db_path", lambda: db)
    monkeypatch.setattr(g, "_root", lambda: str(tmp_path))
    g._search_seen.clear()
    assert g._search_localize_block("grep -rn helper_fn .") == ""


# ---- Fable #2: test_ref_count counts DETERMINISTIC edges only, not name_match ----
def test_test_ref_count_excludes_name_match(tmp_path, monkeypatch):
    db = str(tmp_path / "graph.db")
    con = sqlite3.connect(db)
    con.executescript(
        "CREATE TABLE nodes(id INTEGER PRIMARY KEY, label TEXT, name TEXT,"
        " file_path TEXT, start_line INTEGER, end_line INTEGER, is_test INTEGER, language TEXT);"
        "CREATE TABLE edges(id INTEGER PRIMARY KEY, source_id INTEGER, target_id INTEGER,"
        " type TEXT, source_line INTEGER, resolution_method TEXT, confidence REAL);")
    con.execute("INSERT INTO nodes(id,label,name,file_path,start_line,end_line,is_test,language)"
                " VALUES(1,'Function','walk','account/models.py',20,30,0,'python')")
    # a TEST node whose edge to walk is a NAME_MATCH guess (the stdlib-shadow class)
    con.execute("INSERT INTO nodes(id,label,name,file_path,start_line,end_line,is_test,language)"
                " VALUES(2,'Function','test_walk','test/test_models.py',5,9,1,'python')")
    con.execute("INSERT INTO edges(id,source_id,target_id,type,source_line,resolution_method,"
                "confidence) VALUES(1,2,1,'CALLS',6,'name_match',0.2)")
    con.commit()
    con.close()
    monkeypatch.setattr(g, "_POST_SEARCH_ON", True)
    monkeypatch.setattr(g, "_db_path", lambda: db)
    monkeypatch.setattr(g, "_root", lambda: str(tmp_path))
    g._search_seen.clear()
    block = g._search_localize_block("grep -rn walk .")
    assert "def: account/models.py:20" in block
    assert "test refs:" not in block   # the lone edge is name_match -> not counted


# ---- Fable re-verify: the CALLERS line must not leak a test-module caller ----
def test_caller_line_never_leaks_test_module(tmp_path, monkeypatch):
    """A caller in conftest.py / app/tests.py (is_test=0 — the walker has no such
    basename rule) must NOT surface via the callers line. This is the residual leak
    Fable executed: def-row filtering alone left `_caller_contract_for_file` exposed;
    the path_policy basename fix closes it. Reverting that fix reddens this pin."""
    db = str(tmp_path / "graph.db")
    con = sqlite3.connect(db)
    con.executescript(
        "CREATE TABLE nodes(id INTEGER PRIMARY KEY, label TEXT, name TEXT,"
        " file_path TEXT, start_line INTEGER, end_line INTEGER, is_test INTEGER, language TEXT);"
        "CREATE TABLE edges(id INTEGER PRIMARY KEY, source_id INTEGER, target_id INTEGER,"
        " type TEXT, source_line INTEGER, resolution_method TEXT, confidence REAL);")
    con.execute("INSERT INTO nodes(id,label,name,file_path,start_line,end_line,is_test,language)"
                " VALUES(1,'Function','get_user','app/models.py',10,20,0,'python')")
    con.execute("INSERT INTO nodes(id,label,name,file_path,start_line,end_line,is_test,language)"
                " VALUES(2,'Function','test_user_login','app/tests.py',42,50,0,'python')")
    con.execute("INSERT INTO nodes(id,label,name,file_path,start_line,end_line,is_test,language)"
                " VALUES(3,'Function','user_fixture','conftest.py',3,6,0,'python')")
    con.execute("INSERT INTO edges(id,source_id,target_id,type,source_line,resolution_method,"
                "confidence) VALUES(1,2,1,'CALLS',44,'import',1.0)")
    con.execute("INSERT INTO edges(id,source_id,target_id,type,source_line,resolution_method,"
                "confidence) VALUES(2,3,1,'CALLS',4,'import',1.0)")
    con.commit()
    con.close()
    monkeypatch.setattr(g, "_POST_SEARCH_ON", True)
    monkeypatch.setattr(g, "_db_path", lambda: db)
    monkeypatch.setattr(g, "_root", lambda: str(tmp_path))
    g._search_seen.clear()
    block = g._search_localize_block("grep -rn get_user .")
    assert "def: app/models.py:10" in block
    for leaked in ("test_user_login", "user_fixture", "tests.py", "conftest.py"):
        assert leaked not in block, f"LEAK: {leaked!r} surfaced in the callers line"
