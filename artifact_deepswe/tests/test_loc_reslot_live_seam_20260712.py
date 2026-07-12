"""T0->T2 localization RE-SLOT — the GO-LIVE half on the LIVE mini-seam (2026-07-12).

The OFFLINE ranked-localization mechanism (gateway._produce_ranked_localization) is EXCLUDED on
the live seam: gt_mini_patch._gateway_search_excluded() returns True for EVERY search turn while
the post_search lattice is on, so the Gateway localization producer never fires live. This suite
pins the LIVE re-slot: gt_mini_patch._search_localize_block's ABSTAIN branch (a broad/behavior/
multi-token/regex grep — stratum-B, where _search_pattern is None and the def-partition delivers
0 doses) delivers GT's RANKED localization answer as the lattice's OWN single dose via
_loc_reslot_block -> gateway._ranked_localization_rows -> native_render.render_ranked_list_native.

Discipline pinned (each guard carries an in-test MUTATION that reddens it):
  1. RED-first headline: broad grep + GT_LOC_RESLOT on -> ranked `path:line:sym` rows (pre-edit
     the abstain returned ""); driven through _search_localize_block, the real live entry point.
  2. DOSE LAW: a targeted BARE-symbol grep is BYTE-UNCHANGED and never triggers the re-slot dose
     (the cooperative def-partition owns targeted turns; zero displacement).
  3. Once-per-attempt latch: a 2nd broad grep -> ""; the per-attempt reset re-arms.
  4. LEAK=0: a test-file / test-identity candidate is dropped; contains_test_identity is False.
  5. Ledger single-record: the probe-token accounting is IDENTICAL flag-on vs flag-off (the
     re-slot branch adds NO _ledger_record).
  6. Byte-identical-off: GT_LOC_RESLOT unset -> abstain "" exactly as today; _GT_BASELINE -> "".

Hermetic: a synthetic graph.db + a monkeypatched gateway._ranked_localization_rows (or its
localize() input) — deterministic, no ONNX, no network, no checkout.
"""
from __future__ import annotations

import copy
import sqlite3

import pytest

import gt_mini_patch as g
import groundtruth.runtime.gateway as gw
import groundtruth.runtime.native_render as nr


# a broad, behavior-PHRASE grep -> _search_pattern() is None (stratum-B) -> the ABSTAIN branch.
BROAD = 'grep -rn "verify token handling" .'
BROAD_OUT = "src/foo.py:1:matched something\nsrc/bar.py:9:matched\n"  # non-empty, isolated
# a targeted bare-symbol grep -> the cooperative def-partition owns it (NOT the re-slot dose).
BARE = "grep -rn verify_token ."
BARE_OUT = "src/auth.py:10:def verify_token(self):\n"

# the ranked-localization rows the (live-excluded) Gateway producer would have returned.
ROWS = [("app/auth.py", 10, "verify_token"), ("app/token.py", 20, "refresh")]


def _fake_rows(*rows):
    """A deterministic stand-in for gateway._ranked_localization_rows: returns fixed
    (path, line, sym) rows, bypassing localize()+ONNX (its ranking is separately owned)."""
    def _fn(state):  # noqa: ANN001 — mirrors the real signature (GatewayState -> rows)
        return list(rows)
    return _fn


def _mk_graph(tmp_path):
    """A minimal real graph: verify_token def in a source file + a non-test caller, so the
    bare-symbol def-partition (DOSE-LAW test) resolves to a real block."""
    db = str(tmp_path / "graph.db")
    con = sqlite3.connect(db)
    con.executescript(
        "CREATE TABLE nodes(id INTEGER PRIMARY KEY, label TEXT, name TEXT,"
        " qualified_name TEXT, file_path TEXT, start_line INTEGER, end_line INTEGER,"
        " signature TEXT, return_type TEXT, is_exported INTEGER, is_test INTEGER,"
        " language TEXT, parent_id INTEGER);"
        "CREATE TABLE edges(id INTEGER PRIMARY KEY, source_id INTEGER, target_id INTEGER,"
        " type TEXT, source_line INTEGER, source_file TEXT, resolution_method TEXT,"
        " confidence REAL, metadata TEXT, trust_tier TEXT, candidate_count INTEGER,"
        " evidence_type TEXT, verification_status TEXT);")
    con.execute(
        "INSERT INTO nodes(id,label,name,file_path,start_line,end_line,signature,is_test,language)"
        " VALUES(1,'Function','verify_token','app/auth.py',10,44,'verify_token(self)',0,'python')")
    con.execute(
        "INSERT INTO nodes(id,label,name,file_path,start_line,end_line,is_test,language)"
        " VALUES(2,'Function','login','app/ui.py',88,99,0,'python')")
    con.execute(
        "INSERT INTO edges(id,source_id,target_id,type,source_line,resolution_method,confidence)"
        " VALUES(1,2,1,'CALLS',90,'import',1.0)")
    con.commit()
    con.close()
    return db


@pytest.fixture
def _live(tmp_path, monkeypatch):
    """LIVE seam armed: post_search lattice ON, GT_LOC_RESLOT ON, not baseline, pointed at a
    synthetic graph + tmp root + a fixed issue, with the ledger + latch on a clean slate."""
    db = _mk_graph(tmp_path)
    monkeypatch.setattr(g, "_POST_SEARCH_ON", True)
    monkeypatch.setattr(g, "_GT_BASELINE", False)
    monkeypatch.setattr(g, "_db_path", lambda: db)
    monkeypatch.setattr(g, "_root", lambda: str(tmp_path))
    monkeypatch.setattr(g, "_issue_text", lambda: "verify token verification fails on expiry")
    monkeypatch.setenv("GT_LOC_RESLOT", "1")
    g._search_seen.clear()
    g._loc_reslot_delivered = False
    g._action_count = 5
    yield db
    g._search_seen.clear()
    g._loc_reslot_delivered = False


# =========================================================================== #
# 1. RED-first headline — the broad grep abstain now delivers the ranked rows.
# =========================================================================== #
def test_broad_grep_abstain_delivers_ranked_rows(_live, monkeypatch):
    """RED before the edit: the ABSTAIN branch returned "" (a multi-token grep is un-answerable
    by the def-partition). GREEN: it delivers GT's RANKED localization rows as the lattice's dose.
    Driven through _search_localize_block — the REAL live entry point (not the helper)."""
    monkeypatch.setattr(gw, "_ranked_localization_rows", _fake_rows(*ROWS))
    block = g._search_localize_block(BROAD, BROAD_OUT)
    assert "app/auth.py:10:verify_token" in block
    assert "app/token.py:20:refresh" in block
    # grep-native: NO GT tag, NO "confirm with grep" hedge, NO test identity.
    assert "<gt-" not in block.lower()
    assert "confirm" not in block.lower()
    assert nr.contains_test_identity(block) is False


# =========================================================================== #
# 2. DOSE LAW — a targeted bare-symbol grep is BYTE-UNCHANGED; no re-slot dose.
# =========================================================================== #
def test_bare_symbol_grep_byte_unchanged_no_reslot(_live, monkeypatch):
    """The cooperative def-partition owns a targeted bare-symbol grep. The re-slot dose must NOT
    fire on it, and its block must be byte-IDENTICAL whether GT_LOC_RESLOT is on or off (zero
    displacement — thesis-consistent)."""
    # a DISTINCTIVE ranked row so we can prove it never leaks onto the bare path.
    monkeypatch.setattr(gw, "_ranked_localization_rows",
                        _fake_rows(("app/token.py", 20, "refresh")))
    # flag ON:
    g._search_seen.clear(); g._action_count = 5
    on = g._search_localize_block(BARE, BARE_OUT)
    assert on  # the def-partition answered the bare grep
    assert "app/token.py:20:refresh" not in on          # NO ranked-loc dose displaced it
    assert g._loc_reslot_delivered is False             # the once-latch was never spent
    # flag OFF: byte-identical.
    monkeypatch.delenv("GT_LOC_RESLOT", raising=False)
    g._search_seen.clear(); g._action_count = 5
    off = g._search_localize_block(BARE, BARE_OUT)
    assert on == off
    assert g._loc_reslot_delivered is False


# =========================================================================== #
# 3. ONCE-PER-ATTEMPT latch — 2nd broad grep "", reset re-arms. MUTATION (a).
# =========================================================================== #
def test_once_per_attempt_latch_and_reset_rearm(_live, monkeypatch):
    monkeypatch.setattr(gw, "_ranked_localization_rows", _fake_rows(*ROWS))
    first = g._search_localize_block(BROAD, BROAD_OUT)
    assert first and "app/auth.py:10:verify_token" in first
    # SECOND broad grep in the SAME attempt -> "" (the answer is issue-fixed; already delivered).
    g._search_seen.clear(); g._action_count = 6
    second = g._search_localize_block(BROAD, BROAD_OUT)
    assert second == ""
    # the per-attempt reset re-arms (F3 reset law).
    g._reset_oracle_state()
    monkeypatch.setattr(gw, "_ranked_localization_rows", _fake_rows(*ROWS))  # re-patch post-reset
    g._search_seen.clear(); g._action_count = 5
    again = g._search_localize_block(BROAD, BROAD_OUT)
    assert "app/auth.py:10:verify_token" in again


def test_latch_is_load_bearing_mutation_reddens(_live, monkeypatch):
    """MUTATION (a): DROP the once-latch (force it back to unspent) -> a 2nd broad grep
    DOUBLE-delivers instead of "". Proves the latch is what gates the single dose."""
    monkeypatch.setattr(gw, "_ranked_localization_rows", _fake_rows(*ROWS))
    assert g._search_localize_block(BROAD, BROAD_OUT) != ""   # first delivers, spends the latch
    g._search_seen.clear(); g._action_count = 6
    g._loc_reslot_delivered = False                          # MUTATION: latch dropped/unspent
    assert g._search_localize_block(BROAD, BROAD_OUT) != ""   # -> DOUBLE-delivery (reddens #3)


# =========================================================================== #
# 4. LEAK=0 — a test-file candidate is dropped; renderer firewall load-bearing. MUTATION (b).
# =========================================================================== #
def test_test_candidate_dropped_leak_zero(_live, monkeypatch):
    """A ranked candidate that is a test file is firewalled out of the delivered rows; the
    survivor is real, and contains_test_identity is False."""
    monkeypatch.setattr(gw, "_ranked_localization_rows",
                        _fake_rows(("app/auth.py", 10, "verify_token"),
                                   ("tests/test_auth.py", 3, "test_login")))
    g._loc_reslot_delivered = False
    block = g._loc_reslot_block()
    assert "app/auth.py:10:verify_token" in block
    assert "test_auth" not in block
    assert "test_login" not in block
    assert nr.contains_test_identity(block) is False


def test_renderer_test_firewall_mutation_reddens(_live, monkeypatch):
    """MUTATION (b): an ALL-test-row answer renders to "" (firewall drops every row);
    neuter the renderer's _is_test_path -> the test row SURVIVES (non-empty). Proves the
    inherited render_def_rows_native firewall is load-bearing on the ranked-list path."""
    monkeypatch.setattr(gw, "_ranked_localization_rows",
                        _fake_rows(("tests/test_auth.py", 3, "test_login")))
    g._loc_reslot_delivered = False
    assert g._loc_reslot_block() == ""                 # real: firewall drops the only (test) row
    monkeypatch.setattr(nr, "_is_test_path", lambda *a, **k: False)  # MUTATION
    g._loc_reslot_delivered = False
    assert g._loc_reslot_block() != ""                 # mutant: the test row survives (reddens)


def test_broad_grep_no_phantom_answered_stamp(_live, monkeypatch):
    """The ranked dose rides Lane-A kind 'post_search.localize', but the D-4 answered-stamp
    (_lane_a_deliver: `_psy = _search_pattern(cmd); if _psy: _ledger_mark_answered(...)`) is
    keyed on _search_pattern(cmd) — None for a broad grep. So the ranked dose can NEVER mint a
    phantom 'answered' stamp that would later suppress a bare-symbol def-partition on some stem.
    This pins the exact property that makes the D-4 branch skip."""
    monkeypatch.setattr(gw, "_ranked_localization_rows", _fake_rows(*ROWS))
    block = g._search_localize_block(BROAD, BROAD_OUT)
    assert block                                   # the ranked dose IS produced
    assert g._search_pattern(BROAD) is None        # -> the D-4 delivery stamp is skipped
    # the probe ledger holds the 3 accounted tokens but NO 'answered' stamp from the dose.
    assert all(e.get("answered") is None for e in g._search_seen.values())


# =========================================================================== #
# 5. LEDGER SINGLE-RECORD — the probe accounting is identical flag-on vs flag-off.
# =========================================================================== #
def test_ledger_single_record_identical_on_off(_live, monkeypatch):
    """The re-slot branch adds NO _ledger_record: the probe-token accounting (:3523-3533) is
    byte-identical whether GT_LOC_RESLOT is on or off (single-record dose law)."""
    monkeypatch.setattr(gw, "_ranked_localization_rows", _fake_rows(*ROWS))
    # flag OFF:
    monkeypatch.delenv("GT_LOC_RESLOT", raising=False)
    g._search_seen.clear(); g._action_count = 5
    g._search_localize_block(BROAD, BROAD_OUT)
    off_ledger = copy.deepcopy(g._search_seen)
    # flag ON (delivers the dose):
    monkeypatch.setenv("GT_LOC_RESLOT", "1")
    g._search_seen.clear(); g._action_count = 5; g._loc_reslot_delivered = False
    g._search_localize_block(BROAD, BROAD_OUT)
    on_ledger = copy.deepcopy(g._search_seen)
    assert on_ledger == off_ledger
    # the three probe tokens were recorded (accounting DID run, once).
    assert set(on_ledger.keys()) == {g._norm_stem(t) for t in ("verify", "token", "handling")}


# =========================================================================== #
# 6. BYTE-IDENTICAL-OFF — flag unset / baseline -> "" exactly as today. MUTATION (c).
# =========================================================================== #
def test_byte_identical_off_flag_and_baseline(_live, monkeypatch):
    monkeypatch.setattr(gw, "_ranked_localization_rows", _fake_rows(*ROWS))
    # flag UNSET -> abstain "" exactly as pre-edit.
    monkeypatch.delenv("GT_LOC_RESLOT", raising=False)
    g._loc_reslot_delivered = False
    assert g._search_localize_block(BROAD, BROAD_OUT) == ""
    assert g._loc_reslot_block() == ""
    # baseline arm -> "" (the entry guard at :3483 short-circuits; the helper also fails closed).
    monkeypatch.setenv("GT_LOC_RESLOT", "1")
    monkeypatch.setattr(g, "_GT_BASELINE", True)
    g._loc_reslot_delivered = False
    assert g._search_localize_block(BROAD, BROAD_OUT) == ""
    assert g._loc_reslot_block() == ""


def test_flag_check_is_load_bearing_mutation_reddens(_live, monkeypatch):
    """MUTATION (c): REMOVE the flag check (force _loc_reslot_on True) with GT_LOC_RESLOT
    UNSET -> the abstain now delivers a block instead of "". Proves the flag gate is what
    makes the default-off path byte-identical."""
    monkeypatch.setattr(gw, "_ranked_localization_rows", _fake_rows(*ROWS))
    monkeypatch.delenv("GT_LOC_RESLOT", raising=False)
    g._loc_reslot_delivered = False
    assert g._loc_reslot_block() == ""                       # real: flag off -> ""
    monkeypatch.setattr(g, "_loc_reslot_on", lambda: True)   # MUTATION: flag check removed
    g._loc_reslot_delivered = False
    assert g._loc_reslot_block() != ""                       # mutant: delivers (reddens #6)
