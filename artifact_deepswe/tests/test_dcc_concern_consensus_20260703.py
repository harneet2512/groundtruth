"""Pins for DCC — DYNAMIC CONCERN CONSENSUS (M-DCC) — gt_mini_patch.

DCC appends ONE set of concern-completion FACTS, as the LAST Lane-A block, ONLY
when the agent's OWN action stream shows it is DROWNING. A member qualifies iff it
is reached by >=2 INDEPENDENT provenance families (git co-change / verified
structure / runtime mentions) AND its agreement STRICTLY DOMINATES every file the
agent ABANDONED (Wilde-Scully). The delivery is a SET rendered in lexicographic
path order — never a ranked list — bounded by |DCC|<=|footprint| and latched once
per confirmed-footprint frozenset. DEFAULT-OFF (GT_DCC); byte-identical when off.

Each test names the invariant it guards; reverting the guard reddens it. Hermetic:
a synthetic graph.db carrying a `cochange_sets` table (no ONNX, no repo checkout).
The `_augment_output` integration tests reuse the proven _WiredOracle harness style
(NO mocking of the path under test).
"""
from __future__ import annotations

import copy
import os
import re
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import gt_mini_patch as g  # noqa: E402


# ===========================================================================
# Synthetic graph with a set-form co-change table (cochange_sets).
# ===========================================================================
def _mk_dcc_graph(path, *, concern_is_test=False):
    con = sqlite3.connect(path)
    con.executescript(
        """
        CREATE TABLE nodes(id INTEGER PRIMARY KEY, label TEXT, name TEXT,
          qualified_name TEXT, file_path TEXT, start_line INTEGER, end_line INTEGER,
          signature TEXT, return_type TEXT, is_exported INTEGER, is_test INTEGER,
          language TEXT, parent_id INTEGER);
        CREATE TABLE edges(id INTEGER PRIMARY KEY, source_id INTEGER, target_id INTEGER,
          type TEXT, source_line INTEGER, source_file TEXT, resolution_method TEXT,
          confidence REAL, metadata TEXT);
        CREATE TABLE properties(id INTEGER PRIMARY KEY, node_id INTEGER, kind TEXT, value TEXT);
        CREATE TABLE cochanges(file_a TEXT, file_b TEXT, count INTEGER);
        CREATE TABLE cochange_sets(commit_hash TEXT NOT NULL, file_path TEXT NOT NULL,
          PRIMARY KEY(commit_hash, file_path));
        CREATE VIRTUAL TABLE symbol_content_fts USING fts5(content,
          tokenize="unicode61 tokenchars '_'");
        """
    )
    # A = the edited footprint file; B = viewed>once footprint; C = abandoned;
    # D = the CONCERN file (co-changed with A AND a FACT-edge callee of A).
    concern_fp = "tests/test_apply.py" if concern_is_test else "src/apply.py"
    con.execute("INSERT INTO nodes(id,label,name,file_path,start_line,end_line,is_test,"
                "language) VALUES(1,'Function','set_fields','src/importer.py',40,55,0,'python')")
    con.execute("INSERT INTO nodes(id,label,name,file_path,start_line,end_line,is_test,"
                "language) VALUES(2,'Function','run','src/cli.py',10,30,0,'python')")
    con.execute("INSERT INTO nodes(id,label,name,file_path,start_line,end_line,is_test,"
                "language) VALUES(3,'Function','apply_edits',?,5,20,?,'python')",
                (concern_fp, 1 if concern_is_test else 0))
    con.execute("INSERT INTO nodes(id,label,name,file_path,start_line,end_line,is_test,"
                "language) VALUES(4,'Function','widget','src/widget.py',5,9,0,'python')")
    # FACT edges: cli.run -> importer.set_fields ; importer.set_fields -> D.apply_edits
    con.execute("INSERT INTO edges(id,source_id,target_id,type,resolution_method,confidence)"
                " VALUES(1,2,1,'CALLS','import',1.0)")
    con.execute("INSERT INTO edges(id,source_id,target_id,type,resolution_method,confidence)"
                " VALUES(2,1,3,'CALLS','import',1.0)")
    # git set-form co-change: commit c1 touched importer.py AND D together.
    con.execute("INSERT INTO cochange_sets(commit_hash,file_path) VALUES('c1abc12345','src/importer.py')")
    con.execute("INSERT INTO cochange_sets(commit_hash,file_path) VALUES('c1abc12345',?)", (concern_fp,))
    con.commit()
    con.close()
    return path


def _write_root():
    fd, path = tempfile.mkstemp(suffix=".txt")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write("/")  # repo-relative paths pass through _to_repo_rel unchanged
    return path


def _reset_dcc():
    g._dcc_probes.clear()
    g._dcc_probe_outcomes.clear()
    g._dcc_views.clear()
    g._dcc_view_step.clear()
    g._dcc_latch.clear()
    g._dcc_enqueued_files.clear()
    g._oracle_edited_rels.clear()
    g._edit_action_steps.clear()
    g._oracle_test_evidence_seen = False
    g._last_test_outcome_failed = False
    g._action_count = 0


# ---------------------------------------------------------------------------
# Direct-producer fixture: DCC ON, pointed at the synthetic graph.
# ---------------------------------------------------------------------------
class _On:
    def __init__(self, monkeypatch, tmp_path, *, concern_is_test=False):
        self.db = _mk_dcc_graph(str(tmp_path / "graph.db"),
                                concern_is_test=concern_is_test)
        self.rootf = _write_root()
        monkeypatch.setattr(g, "_DCC_ON", True)
        monkeypatch.setattr(g, "_GT_BASELINE", False)
        monkeypatch.setattr(g, "_db_path", lambda: self.db)
        monkeypatch.setattr(g, "_root", lambda: "/")
        _reset_dcc()

    def cleanup(self):
        try:
            os.unlink(self.rootf)
        except OSError:
            pass


def _seed_footprint():
    """A -> edited; B -> viewed twice (confirmed); C -> viewed once (abandoned)."""
    g._oracle_edited_rels.add("src/importer.py")
    g._edit_action_steps.append(1)
    g._dcc_views["src/cli.py"] = 2
    g._dcc_view_step["src/cli.py"] = 3
    g._dcc_views["src/widget.py"] = 1
    g._dcc_view_step["src/widget.py"] = 4


# ===========================================================================
# 1. HAPPY PATH — a confirmed concern file is delivered as a set, sorted.
# ===========================================================================
def test_happy_path_delivers_concern_set(monkeypatch, tmp_path):
    on = _On(monkeypatch, tmp_path)
    try:
        _seed_footprint()
        # a repeat probe of the same stem with no edit between -> drowning.
        g._dcc_probes["applything"] = [5]
        g._action_count = 6
        block = g._dcc_block(None, "", "grep -rn apply_thing .", "", [])
        assert block, "DCC did not deliver on a clear drowning + qualifying concern"
        assert "<gt-concern" in block
        assert "src/apply.py" in block
        # the two INDEPENDENT witnesses both render.
        assert "co-changed with src/importer.py in commit c1abc1234" in block
        assert "FACT-edge to src/importer.py" in block
        # footprint + abandoned files are NEVER re-named.
        assert "src/cli.py" not in block
        assert "src/widget.py" not in block
        assert "src/importer.py — " not in block  # importer is footprint, not a member
    finally:
        on.cleanup()


def test_render_is_lexicographic_not_ranked(monkeypatch, tmp_path):
    """Two concern files must appear in LEXICOGRAPHIC path order (a set, never a
    ranked list). We add a second qualifying file with a LATER path but MORE
    families; it must still sort by path, not by family count."""
    on = _On(monkeypatch, tmp_path)
    try:
        # add a 2nd concern file 'src/aaa_first.py' reached by git+static so it
        # qualifies; lexicographically it precedes 'src/apply.py'.
        con = sqlite3.connect(on.db)
        con.execute("INSERT INTO nodes(id,label,name,file_path,start_line,end_line,is_test,"
                    "language) VALUES(5,'Function','aaa','src/aaa_first.py',1,4,0,'python')")
        con.execute("INSERT INTO edges(id,source_id,target_id,type,resolution_method,confidence)"
                    " VALUES(3,1,5,'CALLS','import',1.0)")
        con.execute("INSERT INTO cochange_sets(commit_hash,file_path) VALUES('c1abc12345','src/aaa_first.py')")
        con.commit(); con.close()
        _seed_footprint()
        g._dcc_probes["applything"] = [5]
        g._action_count = 6
        block = g._dcc_block(None, "", "grep -rn apply_thing .", "", [])
        assert "src/aaa_first.py" in block and "src/apply.py" in block
        assert block.index("src/aaa_first.py") < block.index("src/apply.py"), (
            "members must be in lexicographic path order, not ranked")
    finally:
        on.cleanup()


# ===========================================================================
# 2. TRIGGER — only fires on a real drowning signal.
# ===========================================================================
def test_no_drowning_no_delivery(monkeypatch, tmp_path):
    """A single (non-repeat) probe with a healthy footprint is NOT drowning."""
    on = _On(monkeypatch, tmp_path)
    try:
        _seed_footprint()
        g._action_count = 5
        # one probe only -> no repeat, no hit-retreat, not a first-edit turn.
        assert g._dcc_block(None, "", "grep -rn apply_thing .", "", []) == ""
    finally:
        on.cleanup()


def test_last_test_passed_blocks_delivery(monkeypatch, tmp_path):
    """The known-good gate: if the last observed test PASSED, DCC never fires even
    on a textbook repeat-probe drowning signal."""
    on = _On(monkeypatch, tmp_path)
    try:
        _seed_footprint()
        g._dcc_probes["applything"] = [5]
        g._action_count = 6
        g._oracle_test_evidence_seen = True
        g._last_test_outcome_failed = False   # last test PASSED
        assert g._dcc_block(None, "", "grep -rn apply_thing .", "", []) == ""
        # and a FAILED last test does NOT block (control).
        g._last_test_outcome_failed = True
        g._dcc_latch.clear()
        assert g._dcc_block(None, "", "grep -rn apply_thing .", "", []) != ""
    finally:
        on.cleanup()


def test_repeat_probe_without_edit_detected():
    _reset_dcc()
    g._dcc_probes["foo"] = [3, 7]
    g._edit_action_steps.extend([1])        # edit BEFORE both probes
    assert g._dcc_repeat_probe_without_edit() is True
    # an edit BETWEEN the two probes clears it (the agent acted).
    g._edit_action_steps.append(5)
    assert g._dcc_repeat_probe_without_edit() is False


def test_footprint_divergence_first_edit():
    _reset_dcc()
    g._dcc_views["src/focus.py"] = 3           # attention was here (viewed thrice)
    g._oracle_edited_rels.add("src/elsewhere.py")  # but first edit lands elsewhere
    assert g._dcc_footprint_divergence("post_edit", "src/elsewhere.py") is True
    # editing the file the agent actually focused on is NOT divergence.
    g._oracle_edited_rels.clear()
    g._oracle_edited_rels.add("src/focus.py")
    assert g._dcc_footprint_divergence("post_edit", "src/focus.py") is False


# ===========================================================================
# 3. DYNAMIC BAR — >=2 independent families AND strict dominance.
# ===========================================================================
def test_bar_rejects_single_family():
    assert g._dcc_qualifies({"git"}, []) is False
    assert g._dcc_qualifies({"static"}, []) is False


def test_bar_accepts_two_independent_families_over_empty_abandoned():
    assert g._dcc_qualifies({"git", "static"}, []) is True


def test_bar_strict_dominance_over_abandoned():
    # abandoned file reached by {git}; a candidate must PROPERLY contain it.
    assert g._dcc_qualifies({"git", "static"}, [{"git"}]) is True    # {git} ⊊ {git,static}
    assert g._dcc_qualifies({"git", "runtime"}, [{"static"}]) is False  # {static} ⊄ cand
    # a maximally-connected abandoned file cannot be dominated -> quiet.
    assert g._dcc_qualifies({"git", "static", "runtime"},
                            [{"git", "static", "runtime"}]) is False
    # a hub with 2 families is REJECTED if it fails to dominate an abandoned peer.
    assert g._dcc_qualifies({"git", "static"}, [{"runtime"}]) is False


def test_hub_rejected_concern_accepted_end_to_end(monkeypatch, tmp_path):
    """Fixture form of the bar: the abandoned file 'src/widget.py' is reached by
    the SAME two families as the concern 'src/apply.py' -> the concern no longer
    STRICTLY dominates it -> DCC abstains (a hub that ties the abandoned file is
    not specific enough)."""
    on = _On(monkeypatch, tmp_path)
    try:
        con = sqlite3.connect(on.db)
        # give widget.py (abandoned) the SAME {git, static} agreement as apply.py.
        con.execute("INSERT INTO edges(id,source_id,target_id,type,resolution_method,confidence)"
                    " VALUES(9,1,4,'CALLS','import',1.0)")   # importer -> widget (static)
        con.execute("INSERT INTO cochange_sets(commit_hash,file_path) VALUES('c1abc12345','src/widget.py')")  # git
        con.commit(); con.close()
        _seed_footprint()
        g._dcc_probes["applything"] = [5]
        g._action_count = 6
        # apply.py {git,static} does NOT properly contain widget.py {git,static} -> abstain.
        assert g._dcc_block(None, "", "grep -rn apply_thing .", "", []) == ""
    finally:
        on.cleanup()


# ===========================================================================
# 4. CARDINALITY — |DCC set| <= |confirmed footprint|.
# ===========================================================================
def test_cardinality_bound(monkeypatch, tmp_path):
    on = _On(monkeypatch, tmp_path)
    try:
        # footprint of size 1 (only the edit; no viewed>once). Add TWO concern
        # files -> |DCC|=2 > 1 -> abstain.
        con = sqlite3.connect(on.db)
        con.execute("INSERT INTO nodes(id,label,name,file_path,start_line,end_line,is_test,"
                    "language) VALUES(5,'Function','aaa','src/aaa_first.py',1,4,0,'python')")
        con.execute("INSERT INTO edges(id,source_id,target_id,type,resolution_method,confidence)"
                    " VALUES(3,1,5,'CALLS','import',1.0)")
        con.execute("INSERT INTO cochange_sets(commit_hash,file_path) VALUES('c1abc12345','src/aaa_first.py')")
        con.commit(); con.close()
        g._oracle_edited_rels.add("src/importer.py")   # footprint size 1
        g._edit_action_steps.append(1)
        g._dcc_probes["applything"] = [5]
        g._action_count = 6
        assert g._dcc_block(None, "", "grep -rn apply_thing .", "", []) == ""
    finally:
        on.cleanup()


# ===========================================================================
# 5. FOOTPRINT-SET LATCH — one delivery per footprint, EVER (50x -> 1).
# ===========================================================================
def test_footprint_latch_idempotence_50x(monkeypatch, tmp_path):
    on = _On(monkeypatch, tmp_path)
    try:
        _seed_footprint()
        deliveries = 0
        for i in range(50):
            g._dcc_probes.setdefault("applything", []).append(5 + i)
            g._action_count = 6 + i
            block = g._dcc_block(None, "", "grep -rn apply_thing .", "", [])
            if block:
                deliveries += 1
        assert deliveries == 1, (
            f"footprint-set latch broken: {deliveries} deliveries on a STABLE "
            "footprint (expected exactly 1)")
    finally:
        on.cleanup()


def test_footprint_change_allows_new_delivery(monkeypatch, tmp_path):
    """A genuinely CHANGED footprint (the agent edited a new file) is a new
    frozenset -> a new (bounded) delivery is permitted."""
    on = _On(monkeypatch, tmp_path)
    try:
        _seed_footprint()
        g._dcc_probes["applything"] = [5]
        g._action_count = 6
        assert g._dcc_block(None, "", "grep -rn apply_thing .", "", []) != ""  # 1st
        assert g._dcc_block(None, "", "grep -rn apply_thing .", "", []) == ""  # latched
        # the agent VIEWS the previously-abandoned file a 2nd time -> it joins the
        # confirmed footprint -> a genuinely NEW frozenset.
        g._dcc_views["src/widget.py"] = 2
        g._dcc_enqueued_files.clear()   # allow the same member to re-surface for the test
        g._dcc_probes["applything"].append(8)
        g._action_count = 9
        assert g._dcc_block(None, "", "grep -rn apply_thing .", "", []) != ""  # new footprint
    finally:
        on.cleanup()


# ===========================================================================
# 6. NOVELTY — subtract files already named this turn.
# ===========================================================================
def test_novelty_subtracts_this_turn_named(monkeypatch, tmp_path):
    on = _On(monkeypatch, tmp_path)
    try:
        _seed_footprint()
        g._dcc_probes["applything"] = [5]
        g._action_count = 6
        # a sibling Lane-A block already named src/apply.py this turn.
        lane_a = [("l3b.evidence", '<gt-evidence>see def: src/apply.py:5</gt-evidence>')]
        assert g._dcc_block(None, "", "grep -rn apply_thing .", "", lane_a) == "", (
            "DCC re-named a file already surfaced by another Lane-A block this turn")
    finally:
        on.cleanup()


# ===========================================================================
# 7. LEAK INVARIANT + mutation companion.
# ===========================================================================
def test_leak_test_path_member_is_filtered(monkeypatch, tmp_path):
    """A concern file that is a TEST path must never render (the members are file
    paths; a graded-test location can never leak)."""
    on = _On(monkeypatch, tmp_path, concern_is_test=True)  # D = tests/test_apply.py
    try:
        _seed_footprint()
        g._dcc_probes["applything"] = [5]
        g._action_count = 6
        block = g._dcc_block(None, "", "grep -rn apply_thing .", "", [])
        assert "tests/test_apply.py" not in block
        assert block == "", "the only concern candidate was a test path -> abstain"
    finally:
        on.cleanup()


def test_leak_guard_is_load_bearing_mutation(monkeypatch, tmp_path):
    """MUTATION COMPANION: neuter the test-path exclusion and the SAME fixture now
    leaks the test path -> proves the guard (not something else) is what suppresses
    it. Reverting the guard in production would redden test_leak_test_path_member."""
    on = _On(monkeypatch, tmp_path, concern_is_test=True)
    try:
        _seed_footprint()
        g._dcc_probes["applything"] = [5]
        g._action_count = 6
        monkeypatch.setattr(g, "_is_test_or_demo_path", lambda p: False)
        monkeypatch.setattr(g, "_caller_path_excluded", lambda *a, **k: False)
        monkeypatch.setattr(g, "_POST_SEARCH_TESTPATH", re.compile(r"(?!x)x"))  # never matches
        block = g._dcc_block(None, "", "grep -rn apply_thing .", "", [])
        assert "tests/test_apply.py" in block, (
            "mutation did NOT leak -> the leak test would be vacuous (the guard is "
            "not the thing suppressing the test path)")
    finally:
        on.cleanup()


# ===========================================================================
# 8. PURITY — no writes to any EXISTING global; two calls bit-identical.
# ===========================================================================
def test_purity_no_existing_global_mutation(monkeypatch, tmp_path):
    on = _On(monkeypatch, tmp_path)
    try:
        # a NON-delivering state (no drowning) so the producer returns "" both times.
        _seed_footprint()
        g._action_count = 5   # single probe -> not drowning
        snap = {
            "edited": copy.deepcopy(g._oracle_edited_rels),
            "edit_steps": list(g._edit_action_steps),
            "search_seen": copy.deepcopy(g._search_seen),
            "fire_counts": copy.deepcopy(g._HOOK_FIRE_COUNTS),
            "delivered_hashes": copy.deepcopy(g._oracle_delivered_hashes),
            "consensus_scope": copy.deepcopy(g._consensus_scope),
            "seen": copy.deepcopy(g._seen),
        }
        r1 = g._dcc_block(None, "", "grep -rn apply_thing .", "", [])
        r2 = g._dcc_block(None, "", "grep -rn apply_thing .", "", [])
        assert r1 == r2 == ""
        # NO existing (non-DCC) global was mutated.
        assert g._oracle_edited_rels == snap["edited"]
        assert list(g._edit_action_steps) == snap["edit_steps"]
        assert g._search_seen == snap["search_seen"]
        assert g._HOOK_FIRE_COUNTS == snap["fire_counts"]
        assert g._oracle_delivered_hashes == snap["delivered_hashes"]
        assert g._consensus_scope == snap["consensus_scope"]
        assert g._seen == snap["seen"]
    finally:
        on.cleanup()


# ===========================================================================
# 9. _augment_output INTEGRATION — flag-off byte-parity + strict-append + silence.
# ===========================================================================
def _write_anchors():
    fd, path = tempfile.mkstemp(suffix=".json")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write('{"symbols": ["set_fields"], "obligations": [{"symbols": ["set_fields"]}]}')
    return path


class _AugWired:
    """Drive the REAL _augment_output oracle route with DCC on/off, full reset."""

    def __init__(self, db, anchors, rootf, dcc_on, monkeypatch):
        self.db, self.anchors, self.rootf = db, anchors, rootf
        self.dcc_on, self.mp = dcc_on, monkeypatch

    def __enter__(self):
        self._saved_env = {k: os.environ.get(k) for k in
                           ("GT_HOST_GRAPH_DB", "GT_ANCHORS_PATH", "GT_BASELINE",
                            "GT_POST_SEARCH")}
        os.environ["GT_HOST_GRAPH_DB"] = self.db
        os.environ["GT_ANCHORS_PATH"] = self.anchors
        os.environ.pop("GT_BASELINE", None)
        os.environ.pop("GT_POST_SEARCH", None)
        self.mp.setattr(g, "_GT_BASELINE", False)
        self.mp.setattr(g, "_ORACLE_ROUTE", True)
        self.mp.setattr(g, "_POST_SEARCH_ON", False)   # isolate DCC
        self.mp.setattr(g, "_DCC_ON", self.dcc_on)
        self.mp.setattr(g, "_ROOT_FILE", self.rootf)
        self.mp.setattr(g, "_oblig_syms_cache", None, raising=False)
        self.mp.setattr(g, "_oracle_focus_cache", None)
        g._reset_oracle_state()
        _reset_dcc()
        return self

    def __exit__(self, *exc):
        for k, v in self._saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        return False


def _drive_drowning_stream(monkeypatch, db, anchors, rootf, dcc_on):
    """Establish a footprint (edit A, view B twice, view C once) then repeat-probe
    the same stem twice -> the 2nd probe is the drowning turn. Returns the captured
    output of that final turn + a snapshot of _HOOK_FIRE_COUNTS."""
    with _AugWired(db, anchors, rootf, dcc_on, monkeypatch):
        for action in (
            {"command": "sed -i '42s/a/b/' src/importer.py"},   # edit A
            {"command": "cat src/cli.py"},                       # view B (1)
            {"command": "cat src/cli.py"},                       # view B (2 -> confirmed)
            {"command": "cat src/widget.py"},                    # view C (abandoned)
            {"command": "grep -rn apply_thing ."},               # probe 1
        ):
            out = {"output": ""}
            g._augment_output(action, out)
        # the drowning turn (probe 2) — capture its output.
        out = {"output": ""}
        g._augment_output({"command": "grep -rn apply_thing ."}, out)
        return out["output"], dict(g._HOOK_FIRE_COUNTS), dict(g._dcc_views)


def test_augment_flag_off_byte_identical(monkeypatch, tmp_path):
    """FLAG-OFF byte-parity: with GT_DCC off, the drowning stream's final turn
    output carries NO concern bytes, the fire counter has NO concern.consensus key,
    and DCC's own registries stay empty (truly inert)."""
    db = _mk_dcc_graph(str(tmp_path / "graph.db"))
    anchors, rootf = _write_anchors(), _write_root()
    try:
        out, fires, views = _drive_drowning_stream(monkeypatch, db, anchors, rootf,
                                                    dcc_on=False)
        assert "<gt-concern" not in out
        assert "concern.consensus" not in fires
        assert not g._dcc_views and not g._dcc_probes and not g._dcc_latch, (
            "GT_DCC off must leave DCC registries empty (no observation while off)")
    finally:
        for p in (anchors, rootf):
            try:
                os.unlink(p)
            except OSError:
                pass


def test_augment_flag_on_strict_suffix(monkeypatch, tmp_path):
    """FLAG-ON strict-append: the ON output equals the OFF output with EXACTLY the
    concern block spliced in — every other delivered byte is identical (DCC is a
    pure Lane-A suffix, it changes nothing that was already there)."""
    db = _mk_dcc_graph(str(tmp_path / "graph.db"))
    anchors, rootf = _write_anchors(), _write_root()
    try:
        off_out, off_fires, _ = _drive_drowning_stream(monkeypatch, db, anchors, rootf,
                                                        dcc_on=False)
        on_out, on_fires, _ = _drive_drowning_stream(monkeypatch, db, anchors, rootf,
                                                      dcc_on=True)
        assert "<gt-concern" in on_out, "DCC did not deliver through _augment_output"
        assert on_fires.get("concern.consensus", 0) == 1
        # strip the concern block from the ON output -> byte-identical to OFF.
        m = re.search(r"\n<gt-concern[^>]*>.*?</gt-concern>\n", on_out, re.DOTALL)
        assert m, "concern block not found as a contiguous unit"
        stripped = on_out[:m.start()] + on_out[m.end():]
        assert stripped == off_out, (
            "DCC ON altered non-DCC bytes -> NOT a pure strict-append suffix")
    finally:
        for p in (anchors, rootf):
            try:
                os.unlink(p)
            except OSError:
                pass


def test_augment_no_drowning_silence(monkeypatch, tmp_path):
    """A clean grep->view->edit trajectory (no repeat, no retreat, edit lands on
    the viewed focus) produces ZERO DCC fires even with the flag ON."""
    db = _mk_dcc_graph(str(tmp_path / "graph.db"))
    anchors, rootf = _write_anchors(), _write_root()
    try:
        with _AugWired(db, anchors, rootf, dcc_on=True, monkeypatch=monkeypatch):
            for action in (
                {"command": "grep -rn set_fields src/importer.py"},  # view importer
                {"command": "cat src/importer.py"},                  # view importer again
                {"command": "sed -i '42s/a/b/' src/importer.py"},    # edit the focus
            ):
                out = {"output": "src/importer.py:40:def set_fields():\n"}
                g._augment_output(action, out)
            assert "concern.consensus" not in g._HOOK_FIRE_COUNTS
    finally:
        for p in (anchors, rootf):
            try:
                os.unlink(p)
            except OSError:
                pass


# ===========================================================================
# F13 — witness-attribution DETERMINISM: sorted footprint iteration.
# ===========================================================================
def test_f13_git_witness_pinned_to_sorted_first_footprint(monkeypatch, tmp_path):
    """When a partner co-changed with SEVERAL footprint files, the first-wins witness
    ("co-changed with <nf> ...") must be pinned to the LEXICOGRAPHICALLY-FIRST
    footprint file — not to whichever file Python's set iteration yields first
    (PYTHONHASHSEED-dependent, i.e. a different witness string across processes).
    Pre-F13 this test is hash-seed-flaky (fails whenever the set iterates
    'src/zz_late.py' first); post-F13 it is exact."""
    db = str(tmp_path / "graph.db")
    con = sqlite3.connect(db)
    con.executescript(
        """
        CREATE TABLE nodes(id INTEGER PRIMARY KEY, label TEXT, name TEXT,
          file_path TEXT, start_line INTEGER, end_line INTEGER, is_test INTEGER,
          language TEXT, parent_id INTEGER, qualified_name TEXT, signature TEXT,
          return_type TEXT, is_exported INTEGER);
        CREATE TABLE cochange_sets(commit_hash TEXT NOT NULL, file_path TEXT NOT NULL,
          PRIMARY KEY(commit_hash, file_path));
        """
    )
    # ONE partner co-changed with BOTH footprint files (two different commits).
    con.execute("INSERT INTO cochange_sets VALUES('c1hash0001','src/aa_early.py')")
    con.execute("INSERT INTO cochange_sets VALUES('c1hash0001','src/partner.py')")
    con.execute("INSERT INTO cochange_sets VALUES('c2hash0002','src/zz_late.py')")
    con.execute("INSERT INTO cochange_sets VALUES('c2hash0002','src/partner.py')")
    con.commit()

    footprint = {"src/zz_late.py", "src/aa_early.py"}
    axis = g._dcc_axis_git(con, footprint)
    con.close()
    assert "src/partner.py" in axis
    assert "co-changed with src/aa_early.py" in axis["src/partner.py"], (
        "F13 DETERMINISM: witness must be pinned to the sorted-first footprint file, "
        f"got {axis['src/partner.py']!r}"
    )


# ===========================================================================
# F14 — runtime witness LEAK GUARD: token only, never the raw output line.
# ===========================================================================
def test_f14_runtime_witness_never_echoes_test_text(monkeypatch, tmp_path):
    """The runtime-mentions witness must quote the matched TOKEN, never the raw
    output line: a pytest summary line names a source file AND a test
    (`FAILED tests/…::test_… - src/apply.py …`), and echoing the line would put a
    test name inside a <gt-concern> emission (leak=0 violated — the §4 audit greps
    emissions for test names). RED pre-F14: the witness carried the whole line."""
    on = _On(monkeypatch, tmp_path)
    try:
        con = sqlite3.connect(on.db)
        out_text = ("FAILED tests/test_apply.py::test_secret_gold "
                    "- src/apply.py raised ValueError\n"
                    "E   assert apply_edits() == 42\n")
        axis = g._dcc_axis_runtime(con, out_text, "/")
        con.close()
        assert "src/apply.py" in axis, f"runtime axis missed the source token: {axis}"
        w = axis["src/apply.py"]
        assert "test_secret_gold" not in w and "FAILED" not in w and "assert" not in w, (
            f"F14 LEAK: runtime witness echoes test/assertion text: {w!r}"
        )
        assert "src/apply.py" in w  # still verifiable — the token IS in the output
    finally:
        on.cleanup()
