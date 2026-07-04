"""Pins for the post_search LISTEN LATTICE (M0+) — gt_mini_patch.

The lattice extends the post_search producer: it reads the grep's RESULT (empty?
which paths?) as well as the command, and answers four classes of search FAILURE
in fixed, mutually-exclusive precedence:

  1. NAME-FOLD      — zero hits + a morphology variant (case/camel/snake) that is
                      VERIFIED by exact match against nodes.name.
  2. BODY-ONLY      — zero hits, no name/path match, but the concept is in bodies
                      (symbol_content_fts); enclosing symbols + file:line only.
  3. NON-TARGET     — grep SUCCEEDED but every hit path is a test/vendored copy;
                      answer with the def-site NOT among the observed hits.
  4. HONEST-NEGATIVE— name+path+body all miss; SILENT first, emits on a REPEAT of
                      the (folded) stem with NO intervening edit.

Discipline pinned here: DEFAULT-OFF byte-identical, leak-invariant (no test names),
idempotence (never twice), determinism (same bytes), and the trigger-path constants
taxonomy (a budget never gates fire/no-fire).

Hermetic: a synthetic graph.db carrying symbol_content_fts (no ONNX, no checkout),
plus an OPTIONAL scale check against the real conan graph when present.
"""
from __future__ import annotations

import os
import sqlite3

import pytest

import gt_mini_patch as g

_CONAN_DB = r"D:/gt_runs/localization_testset/graphs/conan-io__conan-17517.db"


# ---- synthetic graph WITH a content-FTS surface ------------------------------
def _mk_graph(tmp_path):
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
          confidence REAL, metadata TEXT);
        CREATE VIRTUAL TABLE symbol_content_fts USING fts5(content,
          tokenize="unicode61 tokenchars '_'");
        """
    )
    # (1) a snake_case def — a camelCase grep MISSES it -> NAME-FOLD target.
    con.execute("INSERT INTO nodes(id,label,name,file_path,start_line,end_line,is_test,"
                "language) VALUES(1,'Function','get_user_id','app/models.py',10,20,0,'python')")
    # (2) a body-only concept: 'handshake' lives in a BODY, not any name/path.
    con.execute("INSERT INTO nodes(id,label,name,file_path,start_line,end_line,is_test,"
                "language) VALUES(2,'Function','connect_tls','app/net.py',30,50,0,'python')")
    con.execute("INSERT INTO symbol_content_fts(rowid,content) "
                "VALUES(2,'connect_tls tls handshake socketlib peercert zqbodysentinel')")
    # (3) a normal def whose grep may hit only a test copy -> NON-TARGET target.
    con.execute("INSERT INTO nodes(id,label,name,file_path,start_line,end_line,is_test,"
                "language) VALUES(3,'Function','parse_widget','app/widget.py',5,9,0,'python')")
    # (4) a def that ONLY exists in a test path — must never surface.
    con.execute("INSERT INTO nodes(id,label,name,file_path,start_line,end_line,is_test,"
                "language) VALUES(4,'Function','only_test_helper','tests/test_x.py',3,7,0,'python')")
    con.commit()
    con.close()
    return db


@pytest.fixture
def on(tmp_path, monkeypatch):
    db = _mk_graph(tmp_path)
    monkeypatch.setattr(g, "_POST_SEARCH_ON", True)
    monkeypatch.setattr(g, "_db_path", lambda: db)
    monkeypatch.setattr(g, "_root", lambda: str(tmp_path))
    monkeypatch.setattr(g, "_action_count", 0, raising=False)
    g._search_seen.clear()
    g._edit_action_steps.clear()
    yield db
    g._search_seen.clear()
    g._edit_action_steps.clear()


def _fresh(cmd, out):
    g._search_seen.clear()
    g._action_count = 0
    return g._search_localize_block(cmd, out)


# ---- CLASS 1: NAME-FOLD ------------------------------------------------------
def test_namefold_camel_to_snake(on):
    """grep the camelCase form (zero hits) -> answer with the snake def it maps to."""
    block = _fresh("grep -rn getUserId .", "")
    assert 'symbol="get_user_id"' in block
    assert "def: app/models.py:10" in block
    assert "getUserId" in block           # honest note names what the agent typed
    assert "get_user_id" in block


def test_namefold_generate_and_verify_no_phantom(on):
    """A fold with NO exact indexed name asserts nothing (generate-and-verify)."""
    # 'zebra_absent' folds to zebraAbsent/ZebraAbsent/... none of which is indexed.
    assert _fresh("grep -rn zebraAbsent .", "") == ""


def test_namefold_only_test_def_stays_quiet(on):
    """A folded name that resolves ONLY to a test-path def must not surface."""
    # onlyTestHelper -> only_test_helper (def is tests/test_x.py) -> leak guard drops it.
    assert _fresh("grep -rn onlyTestHelper .", "") == ""


# ---- CLASS 2: BODY-ONLY ------------------------------------------------------
def test_bodyonly_surfaces_enclosing_symbol(on):
    block = _fresh("grep -rn handshake .", "")
    assert 'surface="body"' in block
    assert "connect_tls" in block            # enclosing symbol name is allowed
    assert "app/net.py:30" in block
    assert "handshake" in block


def test_bodyonly_no_raw_body_text(on):
    """BODY surfaces enclosing symbol + file:line ONLY — never the raw body tokens."""
    block = _fresh("grep -rn handshake .", "")
    for body_tok in ("socketlib", "peercert", "zqbodysentinel"):
        assert body_tok not in block, f"LEAK: raw body token {body_tok!r} surfaced"


def test_bodyonly_absent_when_no_content_table(tmp_path, monkeypatch):
    """No symbol_content_fts (older graph) -> BODY cannot fire (graceful)."""
    db = str(tmp_path / "g.db")
    con = sqlite3.connect(db)
    con.executescript(
        "CREATE TABLE nodes(id INTEGER PRIMARY KEY, label TEXT, name TEXT, file_path TEXT,"
        " start_line INTEGER, end_line INTEGER, is_test INTEGER, language TEXT);"
        "CREATE TABLE edges(id INTEGER PRIMARY KEY, source_id INTEGER, target_id INTEGER,"
        " type TEXT, source_line INTEGER, resolution_method TEXT, confidence REAL);")
    con.commit(); con.close()
    monkeypatch.setattr(g, "_POST_SEARCH_ON", True)
    monkeypatch.setattr(g, "_db_path", lambda: db)
    monkeypatch.setattr(g, "_root", lambda: str(tmp_path))
    g._search_seen.clear()
    assert g._search_localize_block("grep -rn handshake .", "") == ""


# ---- CLASS 3: NON-TARGET HITS ------------------------------------------------
def test_nontarget_all_hits_test_shaped(on):
    """grep succeeded but the only hit is a test copy -> deliver the real def."""
    block = _fresh("grep -rn parse_widget .",
                   "tests/test_widget.py:12:    parse_widget()\n")
    assert "def: app/widget.py:5" in block
    assert "test_widget" not in block          # leak invariant


def test_nontarget_real_hit_stays_quiet(on):
    """A genuine non-test hit means the agent found it -> GT adds nothing."""
    assert _fresh("grep -rn parse_widget .",
                  "app/widget.py:5:def parse_widget():\n") == ""


def test_nontarget_real_hit_elsewhere_stays_quiet(on):
    """A real NON-TEST hit in a DIFFERENT file than the def still means the agent
    found a legit occurrence -> quiet. Isolates the real-hit-quiet guard (novelty
    alone would NOT suppress this, since the hit path != the def path)."""
    assert _fresh("grep -rn parse_widget .",
                  "app/caller.py:88:    parse_widget()\n") == ""


def test_nontarget_novelty_excludes_observed_def(on):
    """If the (only) def path is already among the observed hits -> quiet (novelty)."""
    # hit is app/widget.py itself, but shaped as if only a test dir matched elsewhere:
    # here the observed hit IS the def path -> novel set empty -> quiet.
    assert _fresh("grep -rn parse_widget .",
                  "app/widget.py:5:def parse_widget(): ...\n") == ""


# ---- CLASS 4: HONEST-NEGATIVE ------------------------------------------------
def test_honest_negative_silent_on_first_probe(on):
    g._search_seen.clear()
    g._action_count = 1
    assert g._search_localize_block("grep -rn wholly_absent_thing .", "") == ""


def test_honest_negative_fires_on_repeat_no_edit(on):
    g._search_seen.clear()
    g._edit_action_steps.clear()
    g._action_count = 1
    assert g._search_localize_block("grep -rn wholly_absent_thing .", "") == ""
    g._action_count = 2
    block = g._search_localize_block("grep -rn wholly_absent_thing .", "")
    assert 'surface="absent"' in block
    assert "0 name, 0 path, 0 body" in block
    assert "your greps will fail" not in block   # forbidden phrasing


def test_honest_negative_silent_after_intervening_edit(on):
    g._search_seen.clear()
    g._edit_action_steps.clear()
    g._action_count = 1
    g._search_localize_block("grep -rn wholly_absent_thing .", "")
    g._edit_action_steps.append(2)               # the agent edited at action 2
    g._action_count = 3
    assert g._search_localize_block("grep -rn wholly_absent_thing .", "") == ""


def test_honest_negative_fold_variant_counts_as_repeat(on):
    """A repeat via a FOLD-VARIANT of an already-failed stem still counts."""
    g._search_seen.clear()
    g._edit_action_steps.clear()
    g._action_count = 1
    assert g._search_localize_block("grep -rn wholly_absent_thing .", "") == ""
    g._action_count = 2
    # camelCase spelling of the same stem -> same normalized ledger key -> repeat.
    block = g._search_localize_block("grep -rn whollyAbsentThing .", "")
    assert 'surface="absent"' in block


# ---- IDEMPOTENCE -------------------------------------------------------------
def test_idempotence_never_delivers_same_fact_twice(on):
    """A third identical probe (post-fire) must be silent (content-hash latch)."""
    g._search_seen.clear()
    g._edit_action_steps.clear()
    g._action_count = 1
    g._search_localize_block("grep -rn wholly_absent_thing .", "")   # silent
    g._action_count = 2
    first = g._search_localize_block("grep -rn wholly_absent_thing .", "")   # fires
    assert first
    g._action_count = 3
    assert g._search_localize_block("grep -rn wholly_absent_thing .", "") == ""


def test_namefold_fires_once_then_latched(on):
    a = _fresh("grep -rn getUserId .", "")
    assert a
    assert g._search_localize_block("grep -rn getUserId .", "") == ""


# ---- ABSTAIN (unchanged) -----------------------------------------------------
@pytest.mark.parametrize("cmd,out", [
    ("grep -rn 'a.*b' .", ""),                 # regex
    ("grep -rn app/models .", ""),             # path operand
    ("grep -rn ab .", ""),                     # < 3 chars
    ("cat app/models.py", ""),                 # not a search
])
def test_abstain_on_non_symbol(on, cmd, out):
    assert _fresh(cmd, out) == ""


def test_no_emptiness_claim_for_piped_head(on):
    """`grep X | head` is NOT a clean grep-zero signal -> no HONEST-NEGATIVE even on
    a repeat (grep is not the final stage)."""
    g._search_seen.clear()
    g._edit_action_steps.clear()
    g._action_count = 1
    g._search_localize_block("grep -rn wholly_absent_thing . | head", "")
    g._action_count = 2
    assert g._search_localize_block("grep -rn wholly_absent_thing . | head", "") == ""


def test_grep_count_zero_is_emptiness(on):
    """`grep -c foo` printing 0 counts as zero hits -> NAME-FOLD can still fire."""
    block = _fresh("grep -rc getUserId .", "0\n")
    assert 'symbol="get_user_id"' in block


# ---- DEFAULT-OFF byte-identical ----------------------------------------------
@pytest.mark.parametrize("cmd,out", [
    ("grep -rn getUserId .", ""),
    ("grep -rn handshake .", ""),
    ("grep -rn parse_widget .", "tests/test_widget.py:12: parse_widget()\n"),
    ("grep -rn wholly_absent_thing .", ""),
    ("grep -rn set_fields .", "app/importer.py:1:def set_fields(): ...\n"),
])
def test_flag_off_is_byte_identical_empty(tmp_path, monkeypatch, cmd, out):
    """Flag OFF -> '' for EVERY input (the Lane-A conditional-append then enqueues
    nothing -> byte-identical to a run with no post_search)."""
    db = _mk_graph(tmp_path)
    monkeypatch.setattr(g, "_POST_SEARCH_ON", False)   # DEFAULT
    monkeypatch.setattr(g, "_db_path", lambda: db)
    monkeypatch.setattr(g, "_root", lambda: str(tmp_path))
    g._search_seen.clear()
    assert g._search_localize_block(cmd, out) == ""
    # and the ledger stays empty (no side effects while off)
    assert not g._search_seen


def test_out_none_is_the_original_direct_def(on):
    """out=None (the original call shape) runs ONLY the classic DIRECT-DEF channel."""
    block = g._search_localize_block("grep -rn get_user_id .", None)
    assert block.startswith('<gt-search-facts symbol="get_user_id">')
    assert "def: app/models.py:10" in block
    assert 'surface=' not in block   # no lattice framing on the out=None path


# ---- DETERMINISM -------------------------------------------------------------
def test_determinism_byte_identical(on):
    a = _fresh("grep -rn getUserId .", "")
    b = _fresh("grep -rn getUserId .", "")
    assert a == b and a != ""


# ---- SCALE (real conan graph, 7343 nodes) ------------------------------------
@pytest.mark.skipif(not os.path.isfile(_CONAN_DB), reason="conan graph not present")
def test_scale_namefold_on_real_graph(monkeypatch):
    monkeypatch.setattr(g, "_POST_SEARCH_ON", True)
    monkeypatch.setattr(g, "_db_path", lambda: _CONAN_DB)
    monkeypatch.setattr(g, "_root", lambda: "/testbed")
    g._search_seen.clear()
    block = g._search_localize_block("grep -rn checkoutFromConandataCoordinates .", "")
    assert "checkout_from_conandata_coordinates" in block
    assert "conan/tools/scm/git.py:272" in block
    low = block.lower()
    assert "test_" not in low and "conftest" not in low and "/test" not in low


@pytest.mark.skipif(not os.path.isfile(_CONAN_DB), reason="conan graph not present")
def test_scale_deterministic_on_real_graph(monkeypatch):
    monkeypatch.setattr(g, "_POST_SEARCH_ON", True)
    monkeypatch.setattr(g, "_db_path", lambda: _CONAN_DB)
    monkeypatch.setattr(g, "_root", lambda: "/testbed")

    def once():
        g._search_seen.clear()
        return g._search_localize_block("grep -rn checkoutFromConandataCoordinates .", "")

    assert once() == once()
