"""SS-0 features 5 (GT_SS_PROVENANCE) + 6 (GT_SS_LATE_DROP) + 7 (GT_SS_ACK_METRICS).

Causal-audit context (run 29236533134):
  * payloads cited agent SCRATCH files (tmp/patch_fix.py — loguru; tmp/patch_leafonly.py
    — beancount; /tmp/change_tracer.py — haystack) and GENERATED artifacts
    (htmlcov/coverage_html_cb_*.js — privacyidea);
  * obligations fired "untested" AFTER covering evidence was observed GREEN (17123 m37;
    arviz m71; beets m201);
  * the acknowledgment-rate instrument (receipt>=2 leading indicator, ~3%) had no host-side
    reader.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import gt_mini_patch as g  # noqa: E402


def _base(monkeypatch):
    monkeypatch.setattr(g, "_GT_BASELINE", False)
    monkeypatch.setattr(g, "_record_hook_fire", lambda *a, **k: None)
    monkeypatch.setattr(g, "_ledger_note_delivery", lambda *a, **k: None, raising=False)
    for k in ("GT_SS_PROVENANCE", "GT_SS_LATE_DROP", "GT_SS_ACK_METRICS"):
        monkeypatch.delenv(k, raising=False)
    g._reset_oracle_state()


def _capture(monkeypatch):
    recs: list = []
    monkeypatch.setattr(g, "_runtime_ledger_record", lambda **k: recs.append(k))
    return recs


# --------------------------------------------------------------------------- #
# Feature 5 — PROVENANCE (path-class filter + reindex exclusion)
# --------------------------------------------------------------------------- #
def test_provenance_bad_path_classes():
    assert g._ss_provenance_bad_path("tmp/patch_fix.py")                    # (a) tmp/
    assert g._ss_provenance_bad_path("/tmp/change_tracer.py", "/repo")       # (a) /tmp abs
    assert g._ss_provenance_bad_path("htmlcov/coverage_html_cb_a.js")        # (b) htmlcov
    assert g._ss_provenance_bad_path("build/gen.py")                         # (b) build
    assert g._ss_provenance_bad_path("node_modules/x/y.js")                  # (b) node_modules
    assert g._ss_provenance_bad_path("pkg/__pycache__/m.py")                 # (b) __pycache__
    assert g._ss_provenance_bad_path("coverage.xml")                         # (b) coverage*
    assert g._ss_provenance_bad_path(".git/config")                          # (b) .git
    assert g._ss_provenance_bad_path("../outside.py")                        # (c) escape
    assert g._ss_provenance_bad_path("/etc/passwd", "/repo")                 # (c) outside root
    # legitimate repo source is NOT bad
    assert not g._ss_provenance_bad_path("src/foo.py")
    assert not g._ss_provenance_bad_path("/repo/src/foo.py", "/repo")
    assert not g._ss_provenance_bad_path("/testbed/pkg/mod.go")              # container root


def test_provenance_drops_scratch_line_keeps_real(monkeypatch):
    _base(monkeypatch)
    monkeypatch.setattr(g, "_root", lambda: "/repo")
    monkeypatch.setenv("GT_SS_PROVENANCE", "1")
    block = ("l3b.evidence",
             "\n<gt-evidence>\n[WITNESS] foo in src/real.py\n"
             "[WITNESS] bar in tmp/patch_fix.py\n</gt-evidence>")
    out: dict = {}
    g._lane_a_deliver(out, "cmd", [block], krel="src/real.py", event=None)
    d = out.get("output") or ""
    assert "src/real.py" in d and "tmp/patch_fix.py" not in d  # scratch line dropped


def test_provenance_empties_suppresses_whole(monkeypatch):
    _base(monkeypatch)
    monkeypatch.setattr(g, "_root", lambda: "/repo")
    recs = _capture(monkeypatch)
    monkeypatch.setenv("GT_SS_PROVENANCE", "1")
    block = ("l3b.evidence",
             "\n<gt-evidence>\n[WITNESS] cb in htmlcov/coverage_html_cb_a.js\n</gt-evidence>")
    out: dict = {}
    g._lane_a_deliver(out, "cmd", [block], krel="x.py", event=None)
    assert (out.get("output") or "") == ""  # only-scratch payload -> whole delivery dropped
    assert any(r.get("reason") == "ss_provenance" for r in recs)


def test_provenance_off_delivers_scratch_line(monkeypatch):
    """RED anchor: flag OFF -> the scratch-citing line is delivered verbatim (pre-SS)."""
    _base(monkeypatch)
    monkeypatch.setattr(g, "_root", lambda: "/repo")
    block = ("l3b.evidence",
             "\n<gt-evidence>\n[WITNESS] bar in tmp/patch_fix.py\n</gt-evidence>")
    out: dict = {}
    g._lane_a_deliver(out, "cmd", [block], krel="x.py", event=None)
    assert "tmp/patch_fix.py" in (out.get("output") or "")


def test_provenance_excludes_reindex_trigger(monkeypatch):
    monkeypatch.setattr(g, "_GT_BASELINE", False)
    monkeypatch.setattr(g, "_substrate_active", lambda: False)
    monkeypatch.delenv("GT_L6_FRESH", raising=False)
    monkeypatch.setenv("GT_SS_PROVENANCE", "1")
    emits: list = []
    monkeypatch.setattr(g, "_l6_emit", lambda ev, **k: emits.append(ev))
    g._l6_probe_emitted = False
    g._invalidate_on_edit("tmp/scratch.py", "/repo")   # scratch write
    assert emits == []                                 # returned BEFORE any reindex work
    g._l6_probe_emitted = False
    g._invalidate_on_edit("src/real.py", "/repo")      # real source
    assert emits != []                                 # proceeds to the reindex path


def test_provenance_reindex_exclusion_off_is_inert(monkeypatch):
    """RED anchor: flag OFF -> a scratch write still reaches the reindex path (pre-SS)."""
    monkeypatch.setattr(g, "_GT_BASELINE", False)
    monkeypatch.setattr(g, "_substrate_active", lambda: False)
    monkeypatch.delenv("GT_L6_FRESH", raising=False)
    monkeypatch.delenv("GT_SS_PROVENANCE", raising=False)
    emits: list = []
    monkeypatch.setattr(g, "_l6_emit", lambda ev, **k: emits.append(ev))
    g._l6_probe_emitted = False
    g._invalidate_on_edit("tmp/scratch.py", "/repo")
    assert emits != []                                 # not excluded when off


# --------------------------------------------------------------------------- #
# Feature 6 — LATE-DROP (obligation covered by a GREEN test)
# --------------------------------------------------------------------------- #
def test_late_drop_predicate():
    g._ss_pass_tokens.clear()
    g._ss_pass_tokens.update({"get_user", "ImportTask"})
    green = "obligation: get_user and ImportTask must be handled"
    assert g._ss_late_drop_suppresses("obligation.unexercised", green) is True
    # a symbol NOT covered by a passing test -> deliver
    partial = "obligation: get_user and untested_helper must be handled"
    assert g._ss_late_drop_suppresses("obligation.unexercised", partial) is False
    # non-obligation classes are exempt
    assert g._ss_late_drop_suppresses("l3.contract", green) is False


def test_late_drop_delivery_suppressed(monkeypatch):
    _base(monkeypatch)
    monkeypatch.setattr(g, "_root", lambda: "")
    recs = _capture(monkeypatch)
    monkeypatch.setenv("GT_SS_LATE_DROP", "1")
    g._ss_pass_tokens.update({"resolve_path"})
    block = ("obligation.unexercised", "\n<gt-nudge>\nresolve_path is untested\n</gt-nudge>")
    out: dict = {}
    g._lane_a_deliver(out, "cmd", [block], krel="x.py", event=None)
    assert (out.get("output") or "") == ""  # dropped: the symbol was already GREEN-tested
    assert any(r.get("reason") == "ss_late" for r in recs)


def test_late_drop_off_delivers(monkeypatch):
    """RED anchor: flag OFF -> the (green) obligation still delivers (pre-SS)."""
    _base(monkeypatch)
    monkeypatch.setattr(g, "_root", lambda: "")
    g._ss_pass_tokens.update({"resolve_path"})
    block = ("obligation.unexercised", "\n<gt-nudge>\nresolve_path is untested\n</gt-nudge>")
    out: dict = {}
    g._lane_a_deliver(out, "cmd", [block], krel="x.py", event=None)
    assert "resolve_path" in (out.get("output") or "")


def test_late_drop_pass_tokens_recorded_from_seam():
    # _ss_record_test feeds the passing-token set (reuses the seam's own token regex).
    g._ss_pass_tokens.clear()
    g._ss_record_test("pytest x", "collected 3 items ... 3 passed frobnicate ok", False, True)
    assert "frobnicate" in g._ss_pass_tokens
    assert "passed" in g._ss_pass_tokens
    # a FAILING event does not feed the passing set
    g._ss_pass_tokens.clear()
    g._ss_record_test("pytest y", "1 failed brandnew", True, False)
    assert "brandnew" not in g._ss_pass_tokens


# --------------------------------------------------------------------------- #
# Feature 7 — ACK metrics (host-side only, zero observation bytes)
# --------------------------------------------------------------------------- #
def test_ack_true_on_entity_reference(monkeypatch):
    monkeypatch.setattr(g, "_GT_BASELINE", False)
    monkeypatch.setenv("GT_SS_ACK_METRICS", "1")
    g._ss_pending_acks.clear()
    rows: list = []
    monkeypatch.setattr(g, "_ledger_line_direct", lambda r: rows.append(r))
    g._action_count = 5
    g._ss_note_delivery_for_ack("l3.contract", "\n<gt-contract>\nsrc/foo.py get_user()\n</gt-contract>")
    assert len(g._ss_pending_acks) == 1
    g._action_count = 6
    g._ss_scan_acks("now let me edit get_user in src/foo.py to fix it")
    acks = [r for r in rows if r.get("event_type") == "ack"]
    assert acks and acks[0]["ack"] is True and acks[0]["ack_m"] == 1
    assert acks[0]["content_sha256_16"]                 # joins back to the delivery row
    assert g._ss_pending_acks == []                     # consumed


def test_ack_false_after_window(monkeypatch):
    monkeypatch.setattr(g, "_GT_BASELINE", False)
    monkeypatch.setenv("GT_SS_ACK_METRICS", "1")
    g._ss_pending_acks.clear()
    rows: list = []
    monkeypatch.setattr(g, "_ledger_line_direct", lambda r: rows.append(r))
    g._action_count = 5
    g._ss_note_delivery_for_ack("l3.contract", "\n<gt-contract>\nsrc/foo.py get_user()\n</gt-contract>")
    # advance past the ack window with an UNRELATED message
    g._action_count = 5 + g._SS_ACK_WINDOW + 1
    g._ss_scan_acks("totally unrelated reasoning about something else")
    acks = [r for r in rows if r.get("event_type") == "ack"]
    assert acks and acks[0]["ack"] is False


def test_ack_zero_observation_bytes(monkeypatch):
    """The ack instrument NEVER touches out['output']; off -> no pending, no rows."""
    monkeypatch.setattr(g, "_GT_BASELINE", False)
    monkeypatch.delenv("GT_SS_ACK_METRICS", raising=False)
    g._ss_pending_acks.clear()
    rows: list = []
    monkeypatch.setattr(g, "_ledger_line_direct", lambda r: rows.append(r))
    g._ss_note_delivery_for_ack("l3.contract", "src/foo.py get_user()")
    g._ss_scan_acks("edit get_user")
    assert g._ss_pending_acks == [] and rows == []       # inert when off
