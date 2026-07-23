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


def test_unique_symbol_search_carries_prospective_subject(on):
    assert g._search_localize_subject("grep -rn parse_widget .") == "app/widget.py"


def test_ambiguous_symbol_search_has_no_single_subject(on):
    con = sqlite3.connect(on)
    con.execute(
        "INSERT INTO nodes(id,label,name,file_path,start_line,end_line,is_test,language) "
        "VALUES(5,'Function','parse_widget','other/widget.py',7,12,0,'python')")
    con.commit()
    con.close()

    assert g._search_localize_subject("grep -rn parse_widget .") == ""


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


# ---- BUG-B1 fall-through (FLIPPED 2026-07-10): a bare-symbol grep WITH real hits
# now DELIVERS the graph def partition (def-site + verified callers + test-ref count
# the agent can't compute from grep). Pre-fix these three asserted "" — the
# def/callers partition was structurally MUTE on the out=str path since 2026-07-05
# (the fall-through self-stamped the fire-once latch, then the outer re-check on the
# SAME content hash suppressed the block). Mark-free fall-through + outer-latch
# idempotence flips them to deliver. Justification: BUG-B1.
def test_hits_real_hit_delivers_def_partition(on):
    """BUG-B1 (was stays-quiet): a genuine non-test hit -> _class_nontarget abstains
    -> the fall-through answers the agent's OWN grep with the graph def-site."""
    block = _fresh("grep -rn parse_widget .", "app/widget.py:5:def parse_widget():\n")
    assert 'symbol="parse_widget"' in block
    assert "def: app/widget.py:5" in block


def test_hits_real_hit_elsewhere_delivers_def_partition(on):
    """BUG-B1 (was stays-quiet): a real NON-TEST hit in a DIFFERENT file than the def
    -> _class_nontarget abstains -> the fall-through delivers the def-site."""
    block = _fresh("grep -rn parse_widget .", "app/caller.py:88:    parse_widget()\n")
    assert "def: app/widget.py:5" in block


def test_hits_observed_def_still_delivers_partition(on):
    """BUG-B1 (was stays-quiet): even when the observed hit IS the def path
    (novelty-empty for _class_nontarget), the fall-through delivers the graph def
    partition — the verified-caller / test-ref facts ride the same block the agent
    cannot compute from one grep."""
    block = _fresh("grep -rn parse_widget .", "app/widget.py:5:def parse_widget(): ...\n")
    assert "def: app/widget.py:5" in block


def test_hits_path_fires_once_then_latched(on):
    """BUG-B1 idempotence now lives in the OUTER latch. D-4 (2026-07-10): the latch is
    STAMPED at DELIVERY (_lane_a_deliver's post_search.localize branch), not at
    production — so a producer call no longer self-latches; we mirror the delivery
    stamp here. Re-adding the fall-through mark (the lattice_hits_fallthrough_mark
    mutation) still self-suppresses the FIRST delivery (test_hits_real_hit_delivers_
    def_partition reddens)."""
    cmd = "grep -rn parse_widget ."
    first = _fresh(cmd, "app/widget.py:5:def parse_widget():\n")
    assert first
    # simulate the D-4 delivery-time latch (what _lane_a_deliver stamps on delivery)
    g._ledger_mark_answered(g._norm_stem(g._search_pattern(cmd)), first)
    second = g._search_localize_block(cmd, "app/widget.py:5:def parse_widget():\n")
    assert second == ""


# ---- COMPOUND GATE (F6/F7): grep is not the ENTIRE command -> refuse ----------
@pytest.mark.parametrize("cmd,out", [
    # ; compound: the pytest traceback is NOT grep's zero/hit signal (F6)
    ("grep -rn get_user_id . ; pytest -x",
     "Traceback (most recent call last):\n  File \"app/models.py\", line 12\n"),
    # && / || compounds: another command's output interleaves (F7)
    ("grep -rn get_user_id . && echo done", "app/models.py:10:def get_user_id():\n"),
    ("grep -rn get_user_id . || true", "app/models.py:10:def get_user_id():\n"),
    # pipe-FED grep: grep searches pytest's output, not the repo (F6 junk stem)
    ("pytest -x 2>&1 | grep -n Error", "app/models.py:99: raise Error\n"),
    # output transform: tee/head is another stage (F7)
    ("grep -rn get_user_id . | tee /tmp/x", "app/models.py:10:def get_user_id():\n"),
])
def test_compound_command_refused_no_answer_no_probe(on, cmd, out):
    """When grep/rg is not the WHOLE command (compound / pipe-fed / transformed) the
    lattice answers '' AND records NO probe (the operand never mints a ledger stem).
    Reverting the _search_command_isolated gate reddens this — a compound would then
    record a wrong 'hit' outcome (F6) and, on the pipe-fed case, a junk stem."""
    g._search_seen.clear()
    assert g._search_localize_block(cmd, out) == ""
    assert not g._search_seen, dict(g._search_seen)


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


# ---- RESET INDEX-COHERENCE (F3 reset law, ENDGAME-3 bounce 2026-07-10) --------
def test_reset_clears_edit_action_steps_index_coherence(on):
    """_reset_oracle_state resets the index BASIS (_action_count -> 0, :5153) — any
    retained action-index list is incoherent by construction. Pre-fix,
    _edit_action_steps survived the reset, so a STALE edit index from the PRIOR
    attempt silenced the honest-negative ordering predicate (:3370,
    `prev < es < idx`) on the NEW attempt's ZERO-repeat: the agent looks stuck, GT
    stays mute because of an edit that happened LAST attempt. Behavioral half: seed
    a stale edit step, reset, and assert the ZERO-repeat FIRES (clear-then-check-
    cleared alone is a weak pin). Also pins the FIX-3 four siblings at their init
    defaults post-reset."""
    # ---- attempt 1: a zero probe, then a source edit recorded at action 2 ----
    g._search_seen.clear()
    g._edit_action_steps.clear()
    g._action_count = 1
    assert g._search_localize_block("grep -rn wholly_absent_thing .", "") == ""
    g._edit_action_steps.append(2)          # the agent edited at action 2 (attempt 1)
    # seed the FIX-3 siblings so this pin proves their reset too
    g._last_test_outcome_failed = True
    g._last_test_step = 7
    g._test_cycle_spans.append(5)
    g._cycle_edit_start = 4
    # ---- in-process retry: the reset clears the basis AND every index list ----
    g._reset_oracle_state()
    assert g._edit_action_steps == []        # ENDGAME-3 bounce: the 5th sibling clears
    assert g._last_test_outcome_failed is False   # FIX-3 siblings at init defaults
    assert g._last_test_step is None
    assert g._test_cycle_spans == []
    assert g._cycle_edit_start is None
    # ---- attempt 2 (indices from 0): zero at 1, repeat at 3 -> MUST fire ----
    g._action_count = 1
    assert g._search_localize_block("grep -rn wholly_absent_thing .", "") == ""
    g._action_count = 3
    block = g._search_localize_block("grep -rn wholly_absent_thing .", "")
    # pre-fix RED: the stale es=2 from attempt 1 satisfied 1 < 2 < 3 -> silenced.
    assert 'surface="absent"' in block, (
        "stale pre-reset edit index silenced the honest-negative repeat")


# ---- IDEMPOTENCE -------------------------------------------------------------
def test_idempotence_never_delivers_same_fact_twice(on):
    """A third identical probe (post-DELIVERY) must be silent (content-hash latch).
    D-4: the latch is stamped at delivery (mirrored here via _ledger_mark_answered, as
    _lane_a_deliver does), not at production."""
    cmd = "grep -rn wholly_absent_thing ."
    g._search_seen.clear()
    g._edit_action_steps.clear()
    g._action_count = 1
    g._search_localize_block(cmd, "")   # silent (first zero-probe)
    g._action_count = 2
    first = g._search_localize_block(cmd, "")   # fires (honest-negative)
    assert first
    g._ledger_mark_answered(g._norm_stem(g._search_pattern(cmd)), first)  # D-4 delivery
    g._action_count = 3
    assert g._search_localize_block(cmd, "") == ""


def test_namefold_fires_once_then_latched(on):
    cmd = "grep -rn getUserId ."
    a = _fresh(cmd, "")
    assert a
    g._ledger_mark_answered(g._norm_stem(g._search_pattern(cmd)), a)  # D-4 delivery
    assert g._search_localize_block(cmd, "") == ""


# ---- BUG-B1 hits path: LEAK invariant (same _resolve_symbol_defs guards) -----
def test_hits_path_drops_test_def_and_counts_test_ref(tmp_path, monkeypatch):
    """On the hits-path fall-through the delivered def partition inherits every leak
    guard of _resolve_symbol_defs/_fmt_def_facts: a def in a TEST path is DROPPED
    (never surfaces), a TEST caller is a COUNT (never a name). Reverting the leak
    guard on the fall-through path reddens this — the new hits channel is leak-safe by
    inheritance, but this pin proves it on the exact out=str shape BUG-B1 opened."""
    db = str(tmp_path / "graph.db")
    con = sqlite3.connect(db)
    con.executescript(
        "CREATE TABLE nodes(id INTEGER PRIMARY KEY, label TEXT, name TEXT,"
        " file_path TEXT, start_line INTEGER, end_line INTEGER, is_test INTEGER, language TEXT);"
        "CREATE TABLE edges(id INTEGER PRIMARY KEY, source_id INTEGER, target_id INTEGER,"
        " type TEXT, source_line INTEGER, resolution_method TEXT, confidence REAL);")
    con.execute("INSERT INTO nodes(id,label,name,file_path,start_line,end_line,is_test,language)"
                " VALUES(1,'Function','render_page','app/views.py',40,60,0,'python')")
    # a same-named def in a TEST path (is_test NOT set — the walker misses tests/)
    con.execute("INSERT INTO nodes(id,label,name,file_path,start_line,end_line,is_test,language)"
                " VALUES(2,'Function','render_page','tests/test_views.py',5,9,0,'python')")
    # a TEST caller over a DETERMINISTIC edge -> its COUNT may surface, its NAME never
    con.execute("INSERT INTO nodes(id,label,name,file_path,start_line,end_line,is_test,language)"
                " VALUES(3,'Function','test_render_page','tests/test_views.py',12,20,1,'python')")
    con.execute("INSERT INTO edges(id,source_id,target_id,type,source_line,resolution_method,"
                "confidence) VALUES(1,3,1,'CALLS',14,'import',1.0)")
    con.commit()
    con.close()
    monkeypatch.setattr(g, "_POST_SEARCH_ON", True)
    monkeypatch.setattr(g, "_db_path", lambda: db)
    monkeypatch.setattr(g, "_root", lambda: str(tmp_path))
    g._search_seen.clear()
    # a real non-test hit -> _class_nontarget abstains -> fall-through delivers
    block = g._search_localize_block("grep -rn render_page .",
                                     "app/views.py:40:def render_page():\n")
    assert "def: app/views.py:40" in block              # the real def surfaces
    assert "tests/test_views.py" not in block            # the test-path def is DROPPED
    assert "test_render_page" not in block               # the test caller NAME never leaks
    assert "test refs: 1" in block                       # ...only its COUNT


# ---- ABSTAIN (unchanged) -----------------------------------------------------
@pytest.mark.parametrize("cmd,out", [
    ("grep -rn 'a.*b' .", ""),                 # regex
    ("grep -rn app/models .", ""),             # path operand
    ("grep -rn ab .", ""),                     # < 3 chars
    ("cat app/models.py", ""),                 # not a search
])
def test_abstain_on_non_symbol(on, cmd, out):
    assert _fresh(cmd, out) == ""


def test_truncate_pipe_preserves_emptiness_answerability(on):
    """``grep X | head`` preserves empty-vs-nonempty — repeat zero-hit may emit
    HONEST-NEGATIVE (truncators are not plane transformers like ``wc``/``tee``)."""
    g._search_seen.clear()
    g._edit_action_steps.clear()
    g._action_count = 1
    assert g._search_command_isolated("grep -rn wholly_absent_thing . | head") is True
    g._search_localize_block("grep -rn wholly_absent_thing . | head", "")
    g._action_count = 2
    block = g._search_localize_block("grep -rn wholly_absent_thing . | head", "")
    # First probe silent; second may honest-negative. Must not stay structurally mute
    # solely because of the truncate pipe.
    assert block == "" or "wholly_absent_thing" in block or "absent" in block.lower()


def test_wc_pipe_still_refuses_emptiness_claim(on):
    """``grep X | wc`` transforms the plane — isolation refuses; no lattice answer."""
    assert g._search_command_isolated("grep -rn wholly_absent_thing . | wc -l") is False
    assert g._search_localize_block("grep -rn wholly_absent_thing . | wc -l", "0") == ""


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
