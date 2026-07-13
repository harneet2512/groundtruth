"""W13 — leading `cd <path> &&` prefix must not blind post_search (gt_mini_patch).

DEFECT (S3, forensics + orchestrator-confirmed): the mini-swe harness prefixes
EVERY agent command with ``cd /testbed && `` (369 occurrences in one trajectory).
``_search_command_isolated`` rejects any command carrying a compound separator
(``_COMPOUND_SEP_RE = &&|\\|\\||;``), so the universal ``cd /testbed && grep ...``
is rejected on the ``&&`` and GT's best localization channel is mute at the exact
moment localization is decided (census: ~140 search actions across 6 trajectories
-> 3 deliveries).

FIX: ``_strip_leading_cd_prefix`` strips one or more leading PURE
``cd <single-token-path> &&`` segments before isolation + pattern extraction, so a
``cd X && grep Y`` is classified as the isolated grep in X it semantically is.
STRICT: only a bare-path ``cd`` segment glued by ``&&`` is stripped; every other
prefix / separator keeps the reject verbatim.

Each test names the invariant; the two mutation tests prove the strict cd-only
regex AND the strip itself are load-bearing (revert either -> a pin reddens).
Hermetic: a synthetic graph.db (no ONNX, no repo checkout), mirroring
tests/test_post_search.py.
"""
from __future__ import annotations

import re
import sqlite3

import pytest

import gt_mini_patch as g


# =========================================================================== #
# 1. _strip_leading_cd_prefix — the normalizer contract (unit)                #
# =========================================================================== #
@pytest.mark.parametrize("cmd,want", [
    # STRIP: a single pure `cd <path> &&`
    ("cd /testbed && grep x", "grep x"),
    # STRIP: chained cd (one or more)
    ("cd /testbed && cd haystack && rg x", "rg x"),
    # STRIP: relative / dotted / tilde / hyphen paths are single bare tokens
    ("cd ../sub && grep x", "grep x"),
    ("cd ./pkg && grep x", "grep x"),
    ("cd ~/proj && grep x", "grep x"),
    ("cd my-dir && grep x", "grep x"),
    ("cd a/b/c && grep x", "grep x"),
    # STRIP: no space around && still strips (path token stops at `&`)
    ("cd /testbed&&grep x", "grep x"),
    # KEEP: a non-cd command before && is NOT a cd prefix
    ("make && grep x", "make && grep x"),
    # KEEP: `;` / `||` / single-`&` separators are not `&&`-cd
    ("cd /testbed; grep x", "cd /testbed; grep x"),
    ("cd /testbed || grep x", "cd /testbed || grep x"),
    ("cd /testbed & grep x", "cd /testbed & grep x"),
    # KEEP: a non-single-token path (command substitution / quoted-with-space)
    ("cd $(pwd) && grep x", "cd $(pwd) && grep x"),
    ('cd "my dir" && grep x', 'cd "my dir" && grep x'),
    # KEEP: `cd` with no path is not a pure `cd <path>` segment
    ("cd && grep x", "cd && grep x"),
    # NO-OP: a bare grep / empty is returned unchanged (byte-identical)
    ("grep -rn foo .", "grep -rn foo ."),
    ("", ""),
])
def test_strip_leading_cd_prefix(cmd, want):
    assert g._strip_leading_cd_prefix(cmd) == want


def test_strip_terminates_and_shrinks():
    """Every stripped segment consumes >=`cd X &&` chars, so the loop terminates
    (guards against a zero-width-match infinite loop)."""
    assert g._strip_leading_cd_prefix("cd a && cd b && cd c && grep z") == "grep z"


# =========================================================================== #
# 2. RED->GREEN: a cd-prefixed grep is now ISOLATED + its pattern extracted    #
# =========================================================================== #
def test_cd_prefixed_grep_is_isolated():
    """THE DEFECT: `cd /testbed && grep -rn "parent_span" haystack/` was rejected on
    the `&&`; after the fix it isolates (semantically an isolated grep in /testbed)."""
    cmd = 'cd /testbed && grep -rn "parent_span" haystack/'
    assert g._search_command_isolated(cmd) is True
    assert g._search_pattern(cmd) == "parent_span"


def test_chained_cd_prefix_is_isolated():
    """`cd /testbed && cd haystack && rg -n "trace"` -> isolated rg; pattern 'trace'."""
    cmd = 'cd /testbed && cd haystack && rg -n "trace"'
    assert g._search_command_isolated(cmd) is True
    assert g._search_pattern(cmd) == "trace"


def test_cd_prefixed_pattern_still_abstains_on_nonsymbol():
    """correct-or-quiet is preserved THROUGH the strip: a cd-prefixed regex/path/
    multi-word grep still yields no bare symbol (no garbage answer)."""
    assert g._search_pattern("cd /testbed && grep -rn 'a.*b' .") is None      # regex
    assert g._search_pattern("cd /testbed && grep -rn user/config .") is None  # path
    assert g._search_pattern('cd /testbed && rg "def foo"') is None           # multi-word


# =========================================================================== #
# 3. STRICT rejects HOLD — the strip does not loosen any compound reject       #
# =========================================================================== #
@pytest.mark.parametrize("cmd", [
    "make && grep x",                       # non-cd command before && (mutation-a target)
    "cd /testbed; grep x",                  # `;` separator (not &&) -> reject
    "grep x || true",                       # `||` separator -> reject
    'cd /testbed && grep x | wc -l',        # grep NOT the final consumer (pre-existing rule)
    "cd $(pwd) && grep x",                  # non-single-token path -> not stripped -> && reject
    "cd /testbed & grep x",                 # single-& backgrounding: grep is not the head -> reject
    "pytest | grep Err",                    # pipe-FED grep (not first stage) -> reject
    "grep x | head",                        # grep transformed by head -> not final -> reject
])
def test_strict_rejects_hold(cmd):
    assert g._search_command_isolated(cmd) is False


def test_plain_grep_still_isolated_regression():
    """The non-cd-prefixed baseline is byte-identical behaviour (regression guard)."""
    assert g._search_command_isolated("grep -rn foo .") is True
    assert g._search_command_isolated("rg parse_config") is True


# =========================================================================== #
# 4. End-to-end: a cd-prefixed post_search event delivers def/search-facts     #
# =========================================================================== #
def _mk_graph(tmp_path):
    """Synthetic graph: `parent_span` defined once in a real (non-test) source file,
    with one verified (deterministic-edge) non-test caller."""
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
    con.execute(
        "INSERT INTO nodes(id,label,name,file_path,start_line,end_line,signature,"
        "is_test,language) VALUES(1,'Method','parent_span','haystack/tracing.py',"
        "120,140,'parent_span(self)',0,'python')")
    con.execute(
        "INSERT INTO nodes(id,label,name,file_path,start_line,end_line,is_test,"
        "language) VALUES(2,'Function','emit','haystack/pipeline.py',60,72,0,'python')")
    con.execute(
        "INSERT INTO edges(id,source_id,target_id,type,source_line,resolution_method,"
        "confidence) VALUES(1,2,1,'CALLS',66,'import',1.0)")
    con.commit()
    con.close()
    return db


@pytest.fixture
def _on(tmp_path, monkeypatch):
    db = _mk_graph(tmp_path)
    monkeypatch.setattr(g, "_GT_BASELINE", False)
    monkeypatch.setattr(g, "_POST_SEARCH_ON", True)
    monkeypatch.setattr(g, "_db_path", lambda: db)
    monkeypatch.setattr(g, "_root", lambda: str(tmp_path))
    g._search_seen.clear()
    yield db
    g._search_seen.clear()


# a real (non-test) grep hit for parent_span in the source file
_GREP_OUT = "haystack/tracing.py:120:    def parent_span(self):\n"


def test_e2e_cd_prefixed_search_delivers_def_facts(_on):
    """The production out=str path: a harness-prefixed `cd /testbed && grep ...`
    with real hits produces the def-facts block (was structurally MUTE pre-fix —
    the isolation gate returned '' on the &&)."""
    cmd = "cd /testbed && grep -rn parent_span haystack/"
    block = g._search_localize_block(cmd, _GREP_OUT)
    assert block, "post_search delivered nothing for a cd-prefixed grep"
    assert 'symbol="parent_span"' in block
    assert "def: haystack/tracing.py:120" in block


def test_e2e_cd_prefix_is_transparent_parity(_on):
    """The cd prefix is transparent: the delivered block is byte-identical to the
    bare-grep equivalent (the prefix changes nothing the agent sees)."""
    cmd_bare = "grep -rn parent_span haystack/"
    cmd_cd = "cd /testbed && grep -rn parent_span haystack/"
    block_bare = g._search_localize_block(cmd_bare, _GREP_OUT)
    g._search_seen.clear()
    block_cd = g._search_localize_block(cmd_cd, _GREP_OUT)
    assert block_bare and block_cd
    assert block_bare == block_cd


def test_e2e_leak_invariant_no_test_names(_on):
    """The leak invariant survives the cd path: PATHS/counts only, never a test name."""
    block = g._search_localize_block(
        "cd /testbed && grep -rn parent_span haystack/", _GREP_OUT)
    assert "tracing.py" in block
    for leaked in ("test_", "_test", "conftest", "tests.py"):
        assert leaked not in block, f"LEAK: {leaked!r} surfaced"


# =========================================================================== #
# 5. MUTATIONS — the strict cd-only regex AND the strip are load-bearing        #
# =========================================================================== #
def test_mutation_a_any_prefix_before_and_breaks_make_reject(monkeypatch):
    """MUTATION (a): loosen the strip to allow ANY prefix before `&&` (not just
    `cd <path>`). Under correct code `make && grep x` is NOT isolated; under the
    mutation it IS -> the make&&grep reject is broken. Proves the `cd`-literal +
    bare-path constraint is load-bearing. (monkeypatch auto-restores.)"""
    # correct code: the strict cd-only strip leaves `make && grep` rejected
    assert g._search_command_isolated("make && grep x") is False
    # MUTATION: strip ANY leading `<stuff> &&`
    monkeypatch.setattr(
        g, "_strip_leading_cd_prefix",
        lambda c: re.sub(r"^\s*[^&|;]+?&&\s*", "", c or ""))
    assert g._search_command_isolated("make && grep x") is True  # REJECT BROKEN


def test_mutation_b_revert_strip_reddens_cd_prefix(monkeypatch):
    """MUTATION (b): revert the strip to identity. The `&&` compound reject fires
    again and the cd-prefixed grep is NOT isolated (the original RED). Proves the
    strip itself is what flips the verdict. (monkeypatch auto-restores.)"""
    cmd = 'cd /testbed && grep -rn "parent_span" haystack/'
    # correct code: isolated
    assert g._search_command_isolated(cmd) is True
    # MUTATION: no strip -> back to RED
    monkeypatch.setattr(g, "_strip_leading_cd_prefix", lambda c: c or "")
    assert g._search_command_isolated(cmd) is False  # RED restored
