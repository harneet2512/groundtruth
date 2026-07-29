"""Fable-LIPI bounce round 2 (2026-07-10) — DELIVERY-SEAM findings F1..F10.

Each test names the confirmed defect it guards, FAILS on the pre-fix seam (proven by
reverting the corresponding hunk / `git stash`), and PASSES after. Mutation notes are
inline per finding.  Run with PYTHONIOENCODING=utf-8 (minisweagent emoji crashes the
cp1252 import on Windows otherwise).
"""
from __future__ import annotations

import hashlib
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
from groundtruth.runtime import native_render as nr  # noqa: E402


def _noop_ledger(monkeypatch):
    monkeypatch.setattr(g, "_record_hook_fire", lambda *a, **k: None)
    monkeypatch.setattr(g, "_ledger_note_delivery", lambda *a, **k: None)
    monkeypatch.setattr(g, "_runtime_ledger_record", lambda *a, **k: None)


# A test-file nodeid — the ABI §5 leak class `contains_test_identity` guards. Uses the
# `.py::node` shape both the pre- and post-F7 predicate catch, so these tests are robust
# to the F7 narrowing.
_LEAK = "tests/unit/test_widget.py::test_render"


# =========================================================================== #
# F1 — CRITICAL — the Lane-A oracle-route append is NOT leak-validated.
# =========================================================================== #
def test_f1_lane_a_drops_test_identity_leak(monkeypatch):
    """A Lane-A fact carrying a test identity must ship 0 model bytes (drop-whole) —
    the same leak law `_gt_deliver_append` enforces for the legacy path. RED pre-fix:
    `_lane_a_deliver` appends via `_join_lane_output` with NO `contains_test_identity`
    screen, so the majority of GT bytes (contract/cochange/consensus/evidence/DCC/
    obligation-resurface) ship unvalidated under the default-on GT_ORACLE_ROUTE."""
    _noop_ledger(monkeypatch)
    monkeypatch.setattr(g, "_oracle_delivered_hashes", set())
    monkeypatch.setattr(g, "_contract_seen", set())
    out = {"output": "BASE"}
    leak = f"\n<gt-contract>caller at {_LEAK}</gt-contract>"
    g._lane_a_deliver(out, "edit", [("l3.contract", leak)],
                      krel="src/w.py", event="post_edit")
    assert out["output"] == "BASE", "a leak Lane-A fact must append 0 model bytes"
    # the fire-once latch must NOT be consumed on a leak drop (deferred, not destroyed).
    assert "src/w.py" not in g._contract_seen


def test_f1_lane_a_clean_payload_byte_identical(monkeypatch):
    """Byte-identical for any leak-free Lane-A fact (every real case)."""
    _noop_ledger(monkeypatch)
    monkeypatch.setattr(g, "_oracle_delivered_hashes", set())
    monkeypatch.setattr(g, "_contract_seen", set())
    out = {"output": "BASE"}
    clean = "\n<gt-contract>[SIGNATURE] def f(x)</gt-contract>"
    g._lane_a_deliver(out, "edit", [("l3.contract", clean)],
                      krel="src/w.py", event="post_edit")
    assert out["output"] == "BASE" + clean
    assert "src/w.py" in g._contract_seen  # a real delivery consumes the latch


def test_f1_leak_dropped_end_to_end_default_oracle_route(monkeypatch, tmp_path):
    """END-TO-END on the DEFAULT oracle route (GT_ORACLE_ROUTE unset -> on): a Lane-A
    producer emitting a test identity ships 0 GT bytes through the real `_augment_output`
    seam — proving the leak law governs the PRODUCTION path (the majority of GT bytes),
    not only the `_lane_a_deliver` unit. Cardinal invariant: leak=0."""
    monkeypatch.setattr(g, "_GT_BASELINE", False)
    monkeypatch.setattr(g, "_ORACLE_ROUTE", True)  # the production default
    monkeypatch.delenv("GT_GATEWAY", raising=False)
    monkeypatch.setattr(g, "_db_path", lambda: str(tmp_path / "absent.db"))
    monkeypatch.setattr(g, "_root", lambda: str(tmp_path))
    # a Lane-A contract producer that emits a test-identity leak on the edited file
    monkeypatch.setattr(g, "_graph_contract_block",
                        lambda *a, **k: f"\n<gt-contract>caller at {_LEAK}</gt-contract>")
    g._reset_oracle_state()
    src = tmp_path / "src"
    src.mkdir()
    (src / "foo.py").write_text("def foo():\n    return 1\n", encoding="utf-8")
    base = "edited foo.py"
    out = {"output": base, "returncode": 0}
    g._augment_output({"command": "str_replace", "path": "src/foo.py",
                       "old_str": "return 0", "new_str": "return 1"}, out)
    assert "test_widget.py" not in out["output"] and "::test_render" not in out["output"], \
        "a test identity must NEVER reach the model on the default oracle route"
    assert out["output"] == base  # 0 GT bytes shipped (leak dropped whole)


# =========================================================================== #
# F2 — CRITICAL — obligation-resurface re-surfaces issue text verbatim, unscreened.
# =========================================================================== #
def test_f2_resurface_screens_structured_obligation_leak(monkeypatch):
    """A structured obligation whose verbatim_text names a test file/nodeid is dropped
    line-wise; clean obligations survive. RED pre-fix: `_obligation_resurface_candidate`
    quotes `verbatim_text` with no leak screen."""
    monkeypatch.setattr(g, "_GT_BASELINE", False)
    monkeypatch.setattr(g, "_oblig_resurface_fired", False)

    class _Om:
        def load_obligations(self, path):
            return [{"verbatim_text": "The API must return 200 on success."},
                    {"verbatim_text": f"See {_LEAK} for the exact contract."}]

    monkeypatch.setattr(g, "_load_gt_oracle", lambda: _Om())
    monkeypatch.setattr(g, "_anchors_path", lambda: "anchors")
    res = g._obligation_resurface_candidate()
    assert res is not None
    assert "must return 200" in res[1], "the clean obligation must survive"
    assert "test_widget.py" not in res[1] and "::test_render" not in res[1], \
        "the leaky obligation line must be screened out"


def test_f2_resurface_screens_issue_paragraph_leak(monkeypatch, tmp_path):
    """The issue.txt fallback paragraph is screened too — a first paragraph naming a
    test nodeid yields NO resurface (drop-whole, correct-or-quiet). RED pre-fix: the
    fallback re-surfaces `para[:400]` verbatim."""
    monkeypatch.setattr(g, "_GT_BASELINE", False)
    monkeypatch.setattr(g, "_oblig_resurface_fired", False)
    monkeypatch.setattr(g, "_load_gt_oracle", lambda: None)  # force the fallback
    monkeypatch.setattr(g, "_anchors_path", lambda: "")
    issue = tmp_path / "issue.txt"
    issue.write_text(f"Fix the crash reproduced by {_LEAK} which asserts the widget.\n",
                     encoding="utf-8")
    monkeypatch.setenv("GT_CERT_DIR", str(tmp_path))
    monkeypatch.delenv("GT_ISSUE_FILE", raising=False)
    res = g._obligation_resurface_candidate()
    assert res is None, "a leaky issue paragraph must not be re-surfaced"


def test_f2_resurface_clean_issue_survives(monkeypatch, tmp_path):
    """A clean issue paragraph still re-surfaces (byte-behaviour preserved)."""
    monkeypatch.setattr(g, "_GT_BASELINE", False)
    monkeypatch.setattr(g, "_oblig_resurface_fired", False)
    monkeypatch.setattr(g, "_load_gt_oracle", lambda: None)
    monkeypatch.setattr(g, "_anchors_path", lambda: "")
    issue = tmp_path / "issue.txt"
    issue.write_text("The parser must handle empty input without crashing.\n",
                     encoding="utf-8")
    monkeypatch.setenv("GT_CERT_DIR", str(tmp_path))
    monkeypatch.delenv("GT_ISSUE_FILE", raising=False)
    res = g._obligation_resurface_candidate()
    assert res is not None and "handle empty input" in res[1]


# =========================================================================== #
# F2b — CARDINAL LEAK residual (bounce): contains_test_identity is a TRANSCRIPT
# belt-check; issue PROSE with a BARE test name (Go/pytest/camelCase) leaked through.
# =========================================================================== #
def test_f2b_prose_screen_flags_bare_names_keeps_near_misses():
    """The dedicated PROSE screen flags bare 5-language test names AND keeps production
    near-miss words. RED pre-fix: no `_prose_leaks_test_identity`."""
    leaks = [
        "the fix must make test_login_flow pass again",      # pytest snake
        "TestReconnect should succeed after the retry",      # Go / camel-Test
        "testShouldReconnect currently fails",               # camelCase test method
        "the widget_test needs to be green",                 # suffix _test
        "failing at tests/unit/test_widget.py::test_render",  # nodeid (control)
    ]
    for s in leaks:
        assert g._prose_leaks_test_identity(s) is True, f"must FLAG prose leak: {s!r}"
    keeps = [
        "TestingConfig controls the retry budget",           # Testing* != Test[A-Z]
        "the contest_handler dispatches events",             # contest, not _test
        "read the latest_value from the cache",              # latest, not test_
        "import from std::collections for the map",          # rust path, not nodeid
        "the parser must handle empty input cleanly",        # plain prose
    ]
    for s in keeps:
        assert g._prose_leaks_test_identity(s) is False, f"must KEEP production word: {s!r}"


def test_f2b_resurface_screens_bare_go_test_name(monkeypatch, tmp_path):
    """A bare Go `TestReconnect` in the issue.txt fallback paragraph must NOT re-surface.
    RED pre-fix: the fallback screens via contains_test_identity (transcript-only) which
    misses bare names -> the test name reaches the model at the submit decision point."""
    monkeypatch.setattr(g, "_GT_BASELINE", False)
    monkeypatch.setattr(g, "_oblig_resurface_fired", False)
    monkeypatch.setattr(g, "_load_gt_oracle", lambda: None)
    monkeypatch.setattr(g, "_anchors_path", lambda: "")
    issue = tmp_path / "issue.txt"
    issue.write_text("Reconnect logic is broken; TestReconnect should pass after the retry.\n",
                     encoding="utf-8")
    monkeypatch.setenv("GT_CERT_DIR", str(tmp_path))
    monkeypatch.delenv("GT_ISSUE_FILE", raising=False)
    res = g._obligation_resurface_candidate()
    assert res is None, "a bare Go test name in issue prose must not re-surface"


def test_f2b_resurface_screens_bare_names_structured(monkeypatch):
    """Structured obligations: a clean line survives; a bare pytest name and a camelCase
    name are dropped line-wise. RED pre-fix: bare-name lines are quoted verbatim."""
    monkeypatch.setattr(g, "_GT_BASELINE", False)
    monkeypatch.setattr(g, "_oblig_resurface_fired", False)

    class _Om:
        def load_obligations(self, path):
            return [{"verbatim_text": "The API must return 200 on success."},
                    {"verbatim_text": "test_login_flow must pass again."},
                    {"verbatim_text": "testShouldReconnect currently fails."}]

    monkeypatch.setattr(g, "_load_gt_oracle", lambda: _Om())
    monkeypatch.setattr(g, "_anchors_path", lambda: "anchors")
    res = g._obligation_resurface_candidate()
    assert res is not None and "must return 200" in res[1]
    assert "test_login_flow" not in res[1] and "testShouldReconnect" not in res[1]


def test_f2b_resurface_clean_prose_still_delivers(monkeypatch, tmp_path):
    """Byte-behaviour preserved: a clean issue paragraph still re-surfaces."""
    monkeypatch.setattr(g, "_GT_BASELINE", False)
    monkeypatch.setattr(g, "_oblig_resurface_fired", False)
    monkeypatch.setattr(g, "_load_gt_oracle", lambda: None)
    monkeypatch.setattr(g, "_anchors_path", lambda: "")
    issue = tmp_path / "issue.txt"
    issue.write_text("The parser must handle empty input without crashing.\n",
                     encoding="utf-8")
    monkeypatch.setenv("GT_CERT_DIR", str(tmp_path))
    monkeypatch.delenv("GT_ISSUE_FILE", raising=False)
    res = g._obligation_resurface_candidate()
    assert res is not None and "handle empty input" in res[1]


def test_f2b_structured_lane_a_fact_with_latest_not_dropped(monkeypatch):
    """A STRUCTURED Lane-A fact whose text legitimately contains `latest_value` must NOT
    be over-dropped — structured facts stay on the transcript screen, NOT the prose
    bare-name regex (which would false-positive on production `test`/`Test` substrings)."""
    _noop_ledger(monkeypatch)
    monkeypatch.setattr(g, "_oracle_delivered_hashes", set())
    monkeypatch.setattr(g, "_contract_seen", set())
    out = {"output": "BASE"}
    fact = "\n<gt-contract>[SIGNATURE] def get(self): return self.latest_value</gt-contract>"
    g._lane_a_deliver(out, "edit", [("l3.contract", fact)],
                      krel="src/w.py", event="post_edit")
    assert out["output"] == "BASE" + fact, "a structured fact with `latest_value` must deliver"


# =========================================================================== #
# Shared driver — force a Lane-B steer winner through the real oracle route.
# =========================================================================== #
def _drive_steer(monkeypatch, tmp_path, *, winner_text, winner_kind="l5.no_test",
                 deliver=True, base="CMD-OUT", after_reset=None):
    """Drive `_augment_output` on the oracle route with a FORCED gate winner. Returns
    the `out` dict after the turn. `deliver` False simulates a 0-byte drop (B-15 leak
    refusal) by forcing `_gt_deliver_append` -> False. `after_reset` runs AFTER
    `_reset_oracle_state()` (which clears every fire-once latch), so a test can pre-arm
    a consumed latch that the drop must restore."""
    monkeypatch.setattr(g, "_GT_BASELINE", False)
    monkeypatch.setattr(g, "_ORACLE_ROUTE", True)
    monkeypatch.setattr(g, "_db_path", lambda: str(tmp_path / "absent.db"))
    monkeypatch.setattr(g, "_root", lambda: str(tmp_path))
    monkeypatch.delenv("GT_GATEWAY", raising=False)  # gateway off -> no extra delta
    g._reset_oracle_state()
    if after_reset is not None:
        after_reset()

    def _fake_gate(cands):
        g._oracle_last_losers = set()
        g._last_gate_winner_kind = winner_kind
        g._last_gate_winner_hash = "deadbeefdeadbeef"
        return winner_text

    monkeypatch.setattr(g, "_oracle_gate_blocks", _fake_gate)
    if not deliver:
        monkeypatch.setattr(g, "_gt_deliver_append", lambda *a, **k: False)
    out = {"output": base, "returncode": 0}
    g._augment_output({"command": "python foo.py"}, out)
    return out


# =========================================================================== #
# F3 — HIGH — the Lane-B steer seal omits base_output (B-32 / TITO regression).
# =========================================================================== #
def test_f3_steer_seal_receives_base_output(monkeypatch, tmp_path):
    """The Lane-B steer seal must commit the real base observation (not b"") so two
    different observations carrying the same steer produce different chain entries. RED
    pre-fix: `_seal_lane_delivery(kind, win, target)` omits base_output (-> "")."""
    monkeypatch.setenv("GT_LANE_ENVELOPE", "1")
    captured = {}

    def _cap(kind, text, target, *, base_output="", **_delivery_metadata):
        captured["base"] = base_output

    monkeypatch.setattr(g, "_seal_lane_delivery", _cap)
    _drive_steer(monkeypatch, tmp_path,
                 winner_text="\n<gt-nudge>GT: run a covering test</gt-nudge>",
                 deliver=True, base="THE-OBSERVATION")
    assert captured.get("base") == "THE-OBSERVATION", \
        "the steer seal must receive the pre-append observation as base_output"


def test_f3_seal_lane_delivery_chain_depends_on_base(monkeypatch):
    """`_seal_lane_delivery` itself folds base_output into the chain: same fact, two
    different base observations -> two different chain heads."""
    monkeypatch.setenv("GT_LANE_ENVELOPE", "1")
    monkeypatch.setattr(g, "_GT_BASELINE", False)
    monkeypatch.setattr(g, "_gt_gateway_deliveries", [])
    txt = "\n<gt-nudge>GT: run a covering test</gt-nudge>"
    g._gt_gateway_chain_head = ""
    g._seal_lane_delivery("l5.no_test", txt, "src/x.py", base_output="OBS-A")
    head_a = g._gt_gateway_chain_head
    g._gt_gateway_chain_head = ""
    g._seal_lane_delivery("l5.no_test", txt, "src/x.py", base_output="OBS-B")
    head_b = g._gt_gateway_chain_head
    assert head_a and head_b and head_a != head_b


# =========================================================================== #
# F4 — HIGH — a gate-WINNING steer dropped post-gate never re-arms its latch.
# =========================================================================== #
def test_f4_rearm_helper_rearms_fire_once_latch(monkeypatch):
    """The extracted re-arm helper releases a fire-once latch named in `lost`."""
    monkeypatch.setattr(g, "_l5_notest_fired", True)
    g._rearm_latches({"l5.no_test"}, kkind="post_edit", kf="x.py", krel="x.py")
    assert g._l5_notest_fired is False


def test_f4_dropped_winner_rearms_latch(monkeypatch, tmp_path):
    """A gate-WINNING l5.no_test steer dropped (0 bytes, B-15 leak refusal) must re-arm
    its fire-once latch so it re-competes on a later turn. RED pre-fix: the winner is in
    neither loser set, so the re-arm never restores `_l5_notest_fired`."""
    def _arm():
        g._l5_notest_fired = True  # simulate production having consumed the latch

    _drive_steer(monkeypatch, tmp_path,
                 winner_text="\n<gt-nudge>GT: no test evidence</gt-nudge>",
                 winner_kind="l5.no_test", deliver=False, after_reset=_arm)
    assert g._l5_notest_fired is False, \
        "a 0-byte-dropped winner must re-arm its fire-once latch (deferred, not destroyed)"


def test_f4_delivered_winner_stays_fired_no_double_delivery(monkeypatch, tmp_path):
    """A REALLY-delivered winner must NOT re-arm (no double-delivery). This is the
    riskiest direction — the fix must re-arm ONLY on a 0-byte drop."""
    def _arm():
        g._l5_notest_fired = True

    _drive_steer(monkeypatch, tmp_path,
                 winner_text="\n<gt-nudge>GT: no test evidence</gt-nudge>",
                 winner_kind="l5.no_test", deliver=True, after_reset=_arm)
    assert g._l5_notest_fired is True, \
        "a delivered winner must stay fired-once (no re-arm -> no double-delivery)"


# =========================================================================== #
# F5 — MED-HIGH — B-19 false-fire on Go receiver methods.
# =========================================================================== #
def test_f5_go_receiver_method_no_false_sig_change():
    """A Go receiver method whose parameter list is UNCHANGED must not be flagged as a
    signature change. RED pre-fix: `_paren_params` takes the FIRST paren (the receiver
    `(r *Recv)`), parsing old params as ['r'] while the new content parses ['a'] -> a
    phantom `signature changing (r -> a)` on an unchanged method."""
    rows = [(1, "Bar", "func (r *Recv) Bar(a int) (int, error)", 2, 1)]
    new_content = "func (r *Recv) Bar(a int) (int, error) {\n\treturn a, nil\n}\n"
    changes = g._edit_signature_changes({"command": "str_replace"}, new_content, rows)
    assert "Bar" not in changes, "an unchanged Go receiver method must not be a sig-change"


def test_f5_go_receiver_method_real_sig_change_detected():
    """A REAL Go receiver-method signature change is still detected (name-anchored)."""
    rows = [(1, "Bar", "func (r *Recv) Bar(a int) (int, error)", 2, 1)]
    new_content = "func (r *Recv) Bar(a int, b string) (int, error) {\n\treturn a, nil\n}\n"
    changes = g._edit_signature_changes({"command": "str_replace"}, new_content, rows)
    assert "Bar" in changes and changes["Bar"][1] == ["a", "b"]


def test_f5_python_sig_change_unaffected():
    """Byte-behaviour preserved for the non-receiver (Python) case."""
    rows = [(1, "foo", "def foo(a, b)", 1, 1)]
    unchanged = g._edit_signature_changes({"command": "str_replace"}, "def foo(a, b):\n    return a\n", rows)
    assert "foo" not in unchanged
    changed = g._edit_signature_changes({"command": "str_replace"}, "def foo(a, b, c):\n    return a\n", rows)
    assert "foo" in changed and changed["foo"] == (["a", "b"], ["a", "b", "c"])


# =========================================================================== #
# F6 — MED — gateway append jams onto a non-newline-terminated observation.
# =========================================================================== #
def _mk_run_graph(tmp_path):
    db = str(tmp_path / "graph.db")
    con = sqlite3.connect(db)
    con.executescript(
        "CREATE TABLE nodes(id INTEGER PRIMARY KEY, label TEXT, name TEXT,"
        " qualified_name TEXT, file_path TEXT, start_line INTEGER, end_line INTEGER,"
        " signature TEXT, return_type TEXT, is_exported INTEGER, is_test INTEGER,"
        " language TEXT, parent_id INTEGER);"
        "CREATE TABLE edges(id INTEGER PRIMARY KEY, source_id INTEGER, target_id INTEGER,"
        " type TEXT, source_line INTEGER, source_file TEXT, resolution_method TEXT,"
        " confidence REAL, metadata TEXT);")
    con.execute("INSERT INTO nodes(id,label,name,file_path,start_line,is_test,language)"
                " VALUES(1,'Function','run','a/x.py',10,0,'python')")
    con.execute("INSERT INTO nodes(id,label,name,file_path,start_line,is_test,language)"
                " VALUES(2,'Function','run','b/y.py',20,0,'python')")
    con.commit()
    con.close()
    return db


def test_f6_gateway_delta_does_not_jam(monkeypatch, tmp_path):
    """When the observation does NOT end with a newline, the gateway delta must be joined
    with the L-1a one-newline boundary — never jammed onto the runner's last output line.
    RED pre-fix: `_gt_gateway_append` is a raw suffix so `<gt-search-facts` jams onto
    `...b/y.py:20: run`."""
    db = _mk_run_graph(tmp_path)
    monkeypatch.setattr(g, "_db_path", lambda: db)
    monkeypatch.setattr(g, "_root", lambda: str(tmp_path))
    monkeypatch.setattr(g, "_POST_SEARCH_ON", False)
    monkeypatch.delenv("GT_POST_SEARCH_NATIVE", raising=False)
    monkeypatch.setenv("GT_GATEWAY", "1")
    g._reset_oracle_state()
    grep_out = "a/x.py:10: run\nb/y.py:20: run"  # NO trailing newline
    out = {"output": grep_out, "returncode": 0}
    g._augment_output({"command": "grep -rn run ."}, out)
    obs = out["output"]
    assert obs.startswith(grep_out)            # law 1/2: pure suffix
    delta = obs[len(grep_out):]
    assert delta.startswith("\n"), "the delta must be newline-boundaried, not jammed"
    assert "run<gt-search-facts" not in obs and "run a/x.py:10" not in obs


def test_f6_gateway_byte_identical_when_obs_newline_terminated(monkeypatch, tmp_path):
    """Byte-identical (no extra boundary) when the observation already ends with `\\n`
    (the production grep case) — the boundary is inserted ONLY when needed."""
    db = _mk_run_graph(tmp_path)
    monkeypatch.setattr(g, "_db_path", lambda: db)
    monkeypatch.setattr(g, "_root", lambda: str(tmp_path))
    monkeypatch.setattr(g, "_POST_SEARCH_ON", False)
    monkeypatch.delenv("GT_POST_SEARCH_NATIVE", raising=False)
    monkeypatch.setenv("GT_GATEWAY", "1")
    g._reset_oracle_state()
    grep_out = "a/x.py:10: run\nb/y.py:20: run\n"  # trailing newline (realistic)
    out = {"output": grep_out, "returncode": 0}
    g._augment_output({"command": "grep -rn run ."}, out)
    delta = out["output"][len(grep_out):]
    assert not delta.startswith("\n\n"), "no double newline when the obs already ends in \\n"
    assert delta.startswith("<gt-search-facts")


# =========================================================================== #
# F7 — MED — leak predicates diverge; the chokepoint drops admitted PRODUCTION facts.
# =========================================================================== #
def test_f7_testing_dir_is_not_test_identity():
    """`numpy/testing/utils.py` is PRODUCTION (a shipped test-utility package), not a
    grader test — it must NOT be treated as a test identity. RED pre-fix:
    native_render._TEST_DIR_RE still lists `testing` (path_policy removed it, Fable P11)
    so the chokepoint whole-drops a legit `numpy/testing/utils.py` fact."""
    assert nr.contains_test_identity("caller: numpy/testing/utils.py:42") is False
    assert nr._is_test_path("numpy/testing/utils.py", set()) is False


def test_f7_dotted_qualified_double_colon_not_test_identity():
    """A dotted-qualified `::` (Rust `std.io.Stdout::lock`) is production, not a nodeid.
    RED pre-fix: the `\\.\\w+::` heuristic flags any `.word::`."""
    assert nr.contains_test_identity("uses std.io.Stdout::lock() here") is False


def test_f7_real_test_identity_still_dropped():
    """Leak=0 preserved — real test paths / nodeids / rust ::tests:: are still caught."""
    assert nr.contains_test_identity("failing at tests/unit/test_widget.py::test_render")
    assert nr.contains_test_identity("crate::widget::tests::test_render failed")
    assert nr.contains_test_identity("see tests/conftest.py for fixtures")
    assert nr._is_test_path("pkg/tests/test_x.py", set()) is True


# =========================================================================== #
# F8 — MED-LOW — receipt-ladder promotion mislabels + dedup_key collapse.
# =========================================================================== #
def test_f8_lane_seal_labeled_lane_not_gateway(monkeypatch, tmp_path):
    """A lane-sealed envelope is audited with kind='lane', never the mislabel 'gateway'.

    RE-POINTED 2026-07-28 (Wave 1 Step 5). F8(d) was a labelling rule on the receipt
    PROMOTION write (`gt_mini_patch.py:15611` at the old HEAD chose `_pk = "lane" if
    renderer_id == "lane" else "gateway"`). The promotion block is deleted -- the field
    it wrote was causally inverted -- so the site F8(d) governed no longer exists, and
    the old body asserted against an empty list while its second assertion
    (`("gateway","acted") not in persisted`) passed VACUOUSLY.

    The PROPERTY survives at the `delivered` rung, where two distinct writers exist:
    `_seal_lane_delivery` -> kind="lane" (:14834) and `_commit_gateway` -> kind="gateway"
    (:15701, :16068). So this now drives a REAL lane seal instead of hand-seeding
    `_gt_gateway_deliveries`, which is also what makes it bite: swapping the label at
    :14834 reddens it, and the old hand-seeded form could not have caught that.
    """
    persisted = []
    monkeypatch.setattr(
        g, "_persist_receipt",
        lambda env, *, kind, transition: persisted.append(
            (kind, transition, getattr(env, "renderer_id", ""))))
    monkeypatch.setenv("GT_GATEWAY", "1")
    monkeypatch.setenv("GT_LANE_ENVELOPE", "1")   # _seal_lane_delivery no-ops without it
    monkeypatch.setattr(g, "_root", lambda: str(tmp_path))
    monkeypatch.setattr(g, "_db_path", lambda: str(tmp_path / "absent.db"))
    g._reset_oracle_state()

    g._seal_lane_delivery("l3.contract", "[SIGNATURE] def f(x)", "a/x.py")

    assert persisted, "a lane seal must persist a receipt"
    kinds = {k for k, _t, _r in persisted}
    assert kinds == {"lane"}, persisted
    assert all(t == "delivered" for _k, t, _r in persisted), persisted
    # Secondary witness: the label must agree with the envelope's own renderer_id, so a
    # hardcoded "lane" string at the seal site cannot fake the discrimination.
    assert all(r == "lane" for _k, _t, r in persisted), persisted


# =========================================================================== #
# F9 — LOW — B-3 edit-bridge fabricates a wrong `before` on a non-unique new_str.
# =========================================================================== #
def test_f9_bridge_abstains_when_new_str_not_unique(monkeypatch, tmp_path):
    """The reverse-apply must abstain (before=None) when new_str is NOT unique in the
    after-content — only old_str is unique by editor contract. RED pre-fix:
    `after.replace(new_str, old_str, 1)` fabricates a wrong before at the FIRST match."""
    monkeypatch.setenv("GT_GATEWAY_EDIT_BRIDGES", "1")
    monkeypatch.setattr(g, "_GT_BASELINE", False)
    monkeypatch.setattr(g, "_root", lambda: str(tmp_path))
    f = tmp_path / "x.py"
    # new_str "return x" appears TWICE in the after-content -> ambiguous reverse-apply
    f.write_text("def a():\n    return x\ndef b():\n    return x\n", encoding="utf-8")
    action = {"command": "str_replace", "path": "x.py", "old_str": "return y",
              "new_str": "return x"}
    changed, before_after = g._gateway_edit_bridges(action, "str_replace x.py")
    assert changed == ("x.py",)
    assert before_after is None, "ambiguous new_str must not fabricate a before snapshot"


def test_f9_bridge_reconstructs_before_when_new_str_unique(monkeypatch, tmp_path):
    """A UNIQUE new_str still reconstructs the before (behaviour preserved)."""
    monkeypatch.setenv("GT_GATEWAY_EDIT_BRIDGES", "1")
    monkeypatch.setattr(g, "_GT_BASELINE", False)
    monkeypatch.setattr(g, "_root", lambda: str(tmp_path))
    f = tmp_path / "y.py"
    f.write_text("def a():\n    return NEW\n", encoding="utf-8")
    action = {"command": "str_replace", "path": "y.py",
              "old_str": "return OLD", "new_str": "return NEW"}
    changed, before_after = g._gateway_edit_bridges(action, "str_replace y.py")
    assert before_after is not None
    before, after = before_after["y.py"]
    assert before == "def a():\n    return OLD\n"


# =========================================================================== #
# F10 — LOW — l3b.evidence enqueued unconditionally (fired != produced).
# =========================================================================== #
def test_f10_empty_l3b_evidence_not_fired(monkeypatch, tmp_path):
    """An EMPTY l3b.evidence block must NOT be enqueued (so `_record_hook_fire` does not
    count it every turn). RED pre-fix: `lane_a.append(('l3b.evidence', _ev_text))` is
    unconditional, unlike its D-1-guarded siblings."""
    fired = []
    monkeypatch.setattr(g, "_record_hook_fire", lambda kind: fired.append(kind))
    monkeypatch.setattr(g, "_evidence", lambda cmd: "")  # empty evidence
    monkeypatch.setattr(g, "_GT_BASELINE", False)
    monkeypatch.setattr(g, "_ORACLE_ROUTE", True)
    monkeypatch.setattr(g, "_db_path", lambda: str(tmp_path / "absent.db"))
    monkeypatch.setattr(g, "_root", lambda: str(tmp_path))
    monkeypatch.delenv("GT_GATEWAY", raising=False)
    g._reset_oracle_state()
    out = {"output": "some output", "returncode": 0}
    g._augment_output({"command": "cat foo.py"}, out)
    assert "l3b.evidence" not in fired, \
        "an empty l3b.evidence block must not trip the fire counter"
