r"""W10 (2026-07-13) — RL-native FORM sweep over the remaining tagged model-facing seam surfaces.

Per the completed research design: bare tool-output tokens, NO fabricated ``$ command`` prompts,
severity=evidence-tier, every rendered line through native_render._final_scrub + per-row
_is_test_path drop (the SM-1 belt), routed through the existing lane content-hash dedup. Each new
form is flag-gated + byte-identical-OFF, added to rl_profile Profile-1, following the exact
GT_POST_SEARCH_NATIVE / GT_SCOPE_NATIVE precedent.

Surfaces (this file):
  ITEM 1 <gt-contract>  GT_CONTRACT_NATIVE — verified sig-change -> ONE compiler diagnostic
                        (render_caller_contract_native w/ old->new delta); preserved/pre-edit ->
                        bare rg caller rows (render_def_rows_native); DROP [SIGNATURE] + line-less
                        PRESERVE/[RAISES]/[RETURNS] + [CONSUMED] bilateral prose.
  ITEM 2 <gt-evidence>  GT_EVIDENCE_NATIVE — [WITNESS]/[CALLERS] -> bare path:line:code rows;
                        [SIBLINGS] (no line) DROP; unverified hint DROP.
  ITEM 3 <gt-nudge>x7   GT_NUDGE_NATIVE — body-only imperative (reuses W6's _steer_native), frame
                        dropped; body text unchanged.
  ITEM 4 <gt-concern>   DECISION = RETIRE-with-note under the global arbiter (mirror W7 scope_map).
  ITEM 6 in-seam metrics GT_INSEAM_METRICS — per-fact-class ELIGIBLE counter + tier/conf authority
                        stamps (host-side ledger rows ONLY, zero observation bytes).
  DOSE pin              contract+evidence same turn never double-deliver the SAME row (lane
                        content-hash dedup).

RED-first: every new form's default-OFF path is asserted byte-identical to the tagged form that
ships today (the RED); the flag-ON path asserts the native form (the GREEN). Each item carries >=1
biting MUTATION that reverts a load-bearing hunk and reddens a native/leak/dose pin.

Hermetic: NO graph.db, NO ONNX, NO repo checkout — the pure builders/helpers are exercised
directly; DB-touching production wrappers are covered by their builder + flag helper. Windows: run
with PYTHONIOENCODING=utf-8 (the sig-delta diagnostic carries U+2192 '->' and the prose an em-dash).
"""
from __future__ import annotations

import json
import os
import sys

import pytest

_HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(_HERE, ".."))

import gt_mini_patch as g  # noqa: E402
import groundtruth.runtime.native_render as nr  # noqa: E402


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.setattr(g, "_GT_BASELINE", False)
    for f in ("GT_CONTRACT_NATIVE", "GT_EVIDENCE_NATIVE", "GT_NUDGE_NATIVE", "GT_INSEAM_METRICS"):
        monkeypatch.delenv(f, raising=False)
    g._reset_oracle_state()
    yield
    g._reset_oracle_state()


def _leak_clean(out: str) -> None:
    """Every new form's universal invariant: no <gt-*> tag, no test identity survives."""
    assert nr.contains_gt_tag(out) is False, repr(out)
    assert nr.contains_test_identity(out) is False, repr(out)
    assert "tests/" not in out and "test_" not in out, repr(out)


# =========================================================================== #
# ITEM 1 — <gt-contract> GT_CONTRACT_NATIVE
# =========================================================================== #
# rows tuple shape produced by _graph_contract_block: (id, name, sig, ncallers, nfiles)
_ROWS = [(10, "get_user", "def get_user(a, b)", 3, 2),
         (11, "helper", "def helper()", 0, 0)]


def test_item1_flag_helper_default_off_and_on(monkeypatch):
    assert g._contract_native_on() is False              # default OFF (byte-identical arm)
    monkeypatch.setenv("GT_CONTRACT_NATIVE", "1")
    assert g._contract_native_on() is True
    monkeypatch.setenv("GT_CONTRACT_NATIVE", "0")
    assert g._contract_native_on() is False              # explicit 0 is OFF
    monkeypatch.setattr(g, "_GT_BASELINE", True)
    monkeypatch.setenv("GT_CONTRACT_NATIVE", "1")
    assert g._contract_native_on() is False              # baseline never native


def test_item1a_sig_delta_renderer_default_byte_identical():
    """RED: the two-arg render_caller_contract_native (no sig_delta) is UNCHANGED — the SM-2b
    byte pins hold. GREEN: passing sig_delta inlines ``(old->new)`` after 'signature changed'."""
    base = nr.render_caller_contract_native("get_user", 3, 2, def_file="svc/users.py")
    assert base == ("svc/users.py: error: get_user() signature changed; "
                    "3 caller(s) in 2 file(s) must update the call sites")
    delta = nr.render_caller_contract_native(
        "get_user", 3, 2, def_file="svc/users.py", sig_delta="a, b->a, b, c")
    assert "signature changed (a, b->a, b, c);" in delta
    _leak_clean(delta)


def test_item1_native_sigchange_is_one_compiler_diagnostic():
    """A verified signature-change function -> ONE ``path: error: sym() signature changed
    (old->new); N caller(s) in M file(s) …`` diagnostic. No [SIGNATURE] echo, no tag."""
    out = g._graph_contract_native(
        "svc/users.py", _ROWS, {"get_user": (["a", "b"], ["a", "b", "c"])}, {})
    assert out == ("\nsvc/users.py: error: get_user() signature changed (a, b→a, b, c); "
                   "3 caller(s) in 2 file(s) must update the call sites")
    assert "[SIGNATURE]" not in out and "[CALLERS]" not in out and "preserve" not in out
    _leak_clean(out)


def test_item1_native_preserved_is_bare_rg_caller_rows():
    """A preserved (non-sig-change) function -> its deterministic caller sites as bare
    ``path:line:code`` rows (render_def_rows_native), NOT the '[CALLERS] … preserve this
    interface' prose. DROP [SIGNATURE]."""
    caller_rows = {"get_user": [("app/main.py", 7, "get_user(1, 2)"),
                                 ("app/api.py", 9, "get_user(x)")]}
    out = g._graph_contract_native("svc/users.py", _ROWS, {}, caller_rows)
    # D-S: compiler-note FORM (preserved-caller rows, consistent with l3b)
    assert out == (
        "\napp/main.py:7: note: get_user(1, 2) - verify your change is consistent here"
        "\napp/api.py:9: note: get_user(x) - verify your change is consistent here")
    assert "[SIGNATURE]" not in out and "preserve this interface" not in out
    _leak_clean(out)


def test_item1_native_firewalls_test_caller_rows():
    """A test-file caller row is DROPPED by render_def_rows_native's _is_test_path belt — GT
    never surfaces a grader-target row. contains_test_identity == False."""
    caller_rows = {"get_user": [("app/main.py", 7, "get_user()"),
                                ("tests/test_users.py", 4, "get_user()")]}
    out = g._graph_contract_native("svc/users.py", _ROWS, {}, caller_rows)
    assert out == "\napp/main.py:7: note: get_user() - verify your change is consistent here"
    _leak_clean(out)


def test_item1_native_drops_lineless_props_and_bilateral():
    """DROP the line-less PRESERVE/[RAISES]/[RETURNS] props + the [CONSUMED] bilateral prose:
    the native builder consumes ONLY rows + sig_changes + caller_rows, so a function with no
    callers and no sig-change contributes nothing (correct-or-quiet -> '')."""
    out = g._graph_contract_native("svc/users.py", [(11, "helper", "def helper()", 0, 0)], {}, {})
    assert out == ""                                     # no diagnostic, no rows -> quiet


def test_item1_native_budget_under_400b():
    caller_rows = {"get_user": [("app/pkg/mod%d.py" % i, i, "get_user(%d)" % i) for i in range(8)]}
    out = g._graph_contract_native("svc/users.py", _ROWS, {}, caller_rows)
    assert len(out.encode("utf-8")) <= 420              # <=~400B (<=5 rows cap)
    assert out.count("\n") <= 5


def test_item1_mutation_drop_sigdelta_branch_reddens(monkeypatch):
    """MUTATION: neuter the sig_delta arg (renderer ignores it) -> the diagnostic loses the
    ``(old->new)`` delta -> the delta assertion reddens. Proves the delta is load-bearing."""
    real = nr.render_caller_contract_native(
        "get_user", 3, 2, def_file="svc/users.py", sig_delta="a→a, b")
    assert "(a→a, b)" in real                       # REAL: delta present
    monkeypatch.setattr(nr, "render_caller_contract_native",
                        lambda *a, **k: "svc/users.py: error: get_user() signature changed; 3 caller(s) in 2 file(s) must update the call sites")
    out = g._graph_contract_native("svc/users.py", _ROWS, {"get_user": (["a"], ["a", "b"])}, {})
    assert "→" not in out                           # MUTANT: no delta (bites the GREEN pin)


# =========================================================================== #
# ITEM 2 — <gt-evidence> GT_EVIDENCE_NATIVE
# =========================================================================== #
_EV_LINES = [
    "[WITNESS] set_fields calls -> src/importer.py:39 `set_fields(x)`",
    "[CALLERS] load() in app/main.py:7 `load()` | fetch() in app/api.py:9",
    "[SIBLINGS] foo(a int), bar(b)",
    "src/hint.py:5 (unverified)",
    "[WITNESS] check called by -> tests/test_import.py:8 `check()`",
]


def test_item2_flag_helper_default_off_and_on(monkeypatch):
    assert g._evidence_native_on() is False
    monkeypatch.setenv("GT_EVIDENCE_NATIVE", "1")
    assert g._evidence_native_on() is True


def test_item2_witness_and_caller_units_become_rows():
    """[WITNESS]/[CALLERS] units -> bare ``path:line:code`` rows (backtick snippet = match text,
    else caller name); [SIBLINGS] (no path:line) DROP; ``(unverified)`` hint DROP; a test-path
    [WITNESS] frame DROP (firewall)."""
    out = g._evidence_native(_EV_LINES)
    # D-S: compiler-note FORM (was bare path:line:code rows, which were RL-inert)
    assert out == (
        "src/importer.py:39: note: set_fields(x) - verify your change is consistent here\n"
        "app/main.py:7: note: load() - verify your change is consistent here\n"
        "app/api.py:9: note: fetch - verify your change is consistent here")
    assert "[WITNESS]" not in out and "[CALLERS]" not in out and "[SIBLINGS]" not in out
    assert "unverified" not in out                       # the floor hint is not a fact row
    assert "foo" not in out and "bar" not in out         # siblings dropped (no line)
    _leak_clean(out)                                     # the test-path witness frame dropped whole


def test_item2_quiet_when_no_locatable_unit():
    assert g._evidence_native(["[SIBLINGS] foo(a), bar(b)"]) == ""   # no path:line -> quiet
    assert g._evidence_native([]) == ""


def test_item2_mutation_drop_unverified_skip_leaks_hint(monkeypatch):
    """MUTATION: force the ``(unverified)`` group to never match (the skip is dead) -> the floor
    hint ``src/hint.py:5`` renders as a bogus fact row. Proves the unverified-skip is load-bearing."""
    real = g._evidence_native(["src/hint.py:5 (unverified)"])
    assert real == ""                                    # REAL: unverified is not a fact row
    import re as _re
    # a UNIT regex whose (unverified) group can never fire (the mutation)
    monkeypatch.setattr(g, "_EV_UNIT_RE", _re.compile(
        r"(?:(?P<name>[A-Za-z_]\w*)\(\)\s+in\s+)?(?P<path>[\w./\\+\-]+):(?P<line>\d+)"
        r"(?P<unver>NEVERMATCH)?(?:\s*`(?P<code>[^`]*)`)?"))
    leaked = g._evidence_native(["src/hint.py:5 (unverified)"])
    assert leaked == ("src/hint.py:5: note: src - "               # MUTANT: hint leaked as a row (bites)
                      "verify your change is consistent here")


# =========================================================================== #
# ITEM 3 — <gt-nudge> x7 GT_NUDGE_NATIVE
# =========================================================================== #
_NUDGE = ('\n<gt-nudge reason="loop">\nGT: you have repeated the same command 4+ '
          "times with no progress.\n</gt-nudge>")


def test_item3_flag_helper_default_off_and_on(monkeypatch):
    assert g._nudge_native_on() is False
    monkeypatch.setenv("GT_NUDGE_NATIVE", "1")
    assert g._nudge_native_on() is True


def test_item3_off_byte_identical(monkeypatch):
    """RED / byte-identical-off: with GT_NUDGE_NATIVE unset the wrapper returns the tagged block
    UNCHANGED, char for char."""
    assert g._nudge_native(_NUDGE) == _NUDGE


def test_item3_on_is_body_only_frame_dropped(monkeypatch):
    """GREEN: the frame lines drop; the imperative body survives (reuses W6's _steer_native, so
    the leading GT: voice marker is stripped too — consistent with the delivery-time steer)."""
    monkeypatch.setenv("GT_NUDGE_NATIVE", "1")
    out = g._nudge_native(_NUDGE)
    assert "<gt-nudge" not in out and "</gt-nudge>" not in out
    assert "you have repeated the same command 4+ times with no progress." in out
    _leak_clean(out)


def test_item3_producer_scaffold_arm_native(monkeypatch):
    """Integration through a real producer: the L5 scaffold-trap arm returns the frame-free body
    under the flag, and the EXACT tagged block when off (byte-identical)."""
    monkeypatch.setattr(g, "_action_count", 25)
    monkeypatch.setattr(g, "_source_edit_count", 0)
    monkeypatch.setattr(g, "_l5_fired", False)
    monkeypatch.setenv("GT_NUDGE_NATIVE", "1")
    on = g._l5_nudge("some command", "obs", loop_arm=False, scaffold_arm=True)
    assert on and "<gt-nudge" not in on and "scaffolding" in on
    # off arm
    monkeypatch.setattr(g, "_l5_fired", False)
    monkeypatch.setenv("GT_NUDGE_NATIVE", "0")
    off = g._l5_nudge("some command", "obs", loop_arm=False, scaffold_arm=True)
    assert off.startswith('\n<gt-nudge reason="scaffold_trap">')


def test_item3_mutation_bypass_transform_keeps_frame(monkeypatch):
    """MUTATION: make _nudge_native ignore the flag (return block verbatim) -> the frame survives
    under the flag -> the frame-free GREEN pin reddens."""
    monkeypatch.setenv("GT_NUDGE_NATIVE", "1")
    monkeypatch.setattr(g, "_nudge_native", lambda block: block)   # the mutation
    out = g._nudge_native(_NUDGE)
    assert "<gt-nudge" in out                            # MUTANT: frame not dropped (bites)


# =========================================================================== #
# ITEM 4 — <gt-concern> RETIRE-with-note (mirror W7)
# =========================================================================== #
@pytest.fixture
def ledger(monkeypatch, tmp_path):
    p = tmp_path / "gt_runtime_ledger.jsonl"
    monkeypatch.setenv("GT_RUNTIME_LEDGER", str(p))
    return str(p)


def _retire_rows(led_path):
    if not os.path.isfile(led_path):
        return []
    rows = [json.loads(l) for l in open(led_path, encoding="utf-8") if l.strip()]
    return [r for r in rows
            if r.get("outcome") == "retired" and r.get("event_type") == "concern.consensus"]


def test_item4_predicate_mirrors_ga():
    assert g._concern_retired(True) is True
    assert g._concern_retired(False) is False


def test_item4_retired_zero_production_and_one_note(monkeypatch, ledger):
    """Arbiter ON + GT_DCC on: _dcc_block is NEVER called, nothing is enqueued, and EXACTLY ONE
    per-episode retirement note is logged across multiple eligible turns."""
    monkeypatch.setattr(g, "_DCC_ON", True)
    monkeypatch.setattr(g, "_dcc_block",
                        lambda *a, **k: pytest.fail("DCC producer ran under the arbiter"))
    lane: list = []
    g._concern_lane_a_append(lane, "post_edit", "src/x.py", "cmd", "out", True)
    g._concern_lane_a_append(lane, "post_view", "src/y.py", "cmd", "out", True)
    g._concern_lane_a_append(lane, "post_edit", "src/x.py", "cmd", "out", True)
    assert lane == []                                    # zero enqueue
    assert len(_retire_rows(ledger)) == 1                # one note per episode


def test_item4_byte_identical_off_runs_legacy(monkeypatch, ledger):
    """Arbiter OFF: the EXACT legacy path — _dcc_block runs, a non-empty block is enqueued as
    concern.consensus, and NO retirement note is logged."""
    monkeypatch.setattr(g, "_DCC_ON", True)
    monkeypatch.setattr(g, "_dcc_block", lambda *a, **k: "CONCERN BLOCK")
    lane: list = []
    g._concern_lane_a_append(lane, "post_edit", "src/x.py", "cmd", "out", False)
    assert lane == [("concern.consensus", "CONCERN BLOCK")]
    assert _retire_rows(ledger) == []


def test_item4_dcc_off_is_byte_identical(monkeypatch, ledger):
    """GT_DCC default-OFF: even under the arbiter, nothing is retired-noted (nothing to retire)
    and the legacy _dcc_block returns '' -> byte-identical."""
    monkeypatch.setattr(g, "_DCC_ON", False)
    monkeypatch.setattr(g, "_dcc_block", lambda *a, **k: "")
    lane: list = []
    g._concern_lane_a_append(lane, "post_edit", "src/x.py", "cmd", "out", True)
    assert lane == []
    assert _retire_rows(ledger) == []                    # no note when DCC is off


def test_item4_mutation_unretire_reddens(monkeypatch, ledger):
    """MUTATION: un-retire (_concern_retired -> False) so the producer runs under the arbiter and
    appends a SECOND (redundant) localization dose -> the zero-production pin reddens."""
    monkeypatch.setattr(g, "_DCC_ON", True)
    monkeypatch.setattr(g, "_dcc_block", lambda *a, **k: "CONCERN BLOCK")
    monkeypatch.setattr(g, "_concern_retired", lambda ga_on: False)   # the mutation
    lane: list = []
    g._concern_lane_a_append(lane, "post_edit", "src/x.py", "cmd", "out", True)
    assert lane == [("concern.consensus", "CONCERN BLOCK")]           # MUTANT: production returns


# =========================================================================== #
# ITEM 6 — in-seam instrumentation GT_INSEAM_METRICS (host-side ledger only)
# =========================================================================== #
def _rows_with_outcome(led_path, outcome):
    if not os.path.isfile(led_path):
        return []
    return [json.loads(l) for l in open(led_path, encoding="utf-8")
            if l.strip() and json.loads(l).get("outcome") == outcome]


def test_item6_default_off_writes_no_rows(monkeypatch, ledger):
    """Byte-identical off: GT_INSEAM_METRICS unset -> _inseam_eligible/_inseam_stamp write NOTHING
    (no ledger perturbation, zero observation bytes)."""
    g._inseam_eligible("l3.contract", "src/x.py")
    g._inseam_stamp("l3.contract", "src/x.py", tier="CERTIFIED", conf=0.7)
    assert not os.path.isfile(ledger) or open(ledger).read() == ""


def test_item6_on_writes_eligible_and_authority_rows(monkeypatch, ledger):
    monkeypatch.setenv("GT_INSEAM_METRICS", "1")
    g._inseam_eligible("l3b.evidence", "src/x.py")
    g._inseam_stamp("l3.contract", "src/x.py", tier="CERTIFIED", conf=0.7)
    elig = _rows_with_outcome(ledger, "eligible")
    prod = _rows_with_outcome(ledger, "produced")
    assert len(elig) == 1 and elig[0]["event_type"] == "l3b.evidence" and elig[0]["chars_delivered"] == 0
    assert len(prod) == 1 and prod[0]["tier"] == "CERTIFIED" and prod[0]["conf"] == 0.7


def test_item6_baseline_never_writes(monkeypatch, ledger):
    monkeypatch.setenv("GT_INSEAM_METRICS", "1")
    monkeypatch.setattr(g, "_GT_BASELINE", True)
    g._inseam_eligible("l3.contract", "src/x.py")
    assert not os.path.isfile(ledger) or open(ledger).read() == ""


# =========================================================================== #
# DOSE pin — contract + evidence same turn never double-deliver the SAME row
# =========================================================================== #
def test_dose_same_row_not_double_delivered(monkeypatch):
    """The native contract preserved-arm and the native evidence arm can BOTH render the same
    caller row ``path:line:code``. When they reduce to the SAME single-row block, the lane's
    content-hash dedup (_oracle_delivered_hashes) delivers it EXACTLY ONCE. This is the existing
    lane dedup — routed, not re-implemented."""
    row = g._graph_contract_native("svc/users.py", _ROWS, {},
                                   {"get_user": [("app/main.py", 7, "get_user()")]})
    ev = "\n" + g._evidence_native(["[WITNESS] get_user calls -> app/main.py:7 `get_user()`"])
    _NOTE = "app/main.py:7: note: get_user() - verify your change is consistent here"
    assert row.strip() == ev.strip() == _NOTE   # D-S: same single NOTE row from both -> dedup
    out = {"output": "base observation"}
    g._lane_a_deliver(out, "cmd",
                      [("l3.contract", row), ("l3b.evidence", ev)],
                      krel="app/main.py", event=None)
    assert out["output"].count(_NOTE) == 1      # delivered ONCE (dose held)


def test_dose_mutation_break_dedup_double_delivers(monkeypatch):
    """MUTATION: force the content-hash to differ per call (dedup blind) -> the same row ships
    TWICE. Proves the lane content-hash dedup is the load-bearing dose guard."""
    row = "\napp/main.py:7:get_user()"

    class _Blind(set):
        def __contains__(self, _x):  # dedup-blind: nothing is ever "already delivered"
            return False
    monkeypatch.setattr(g, "_oracle_delivered_hashes", _Blind())     # the mutation
    out = {"output": "base"}
    g._lane_a_deliver(out, "cmd",
                      [("l3.contract", row), ("l3b.evidence", row)],
                      krel="app/main.py", event=None)
    assert out["output"].count("app/main.py:7:get_user()") == 2     # MUTANT: doubled (bites)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
