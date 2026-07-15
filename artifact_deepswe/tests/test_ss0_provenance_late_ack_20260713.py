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
from groundtruth.runtime import gateway as gw  # noqa: E402
from groundtruth.runtime.adapters import miniswe as ad  # noqa: E402
from groundtruth.runtime.evidence_envelope import (  # noqa: E402
    VERIFIED,
    EvidenceEnvelope,
    validate,
)


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
    assert g._ss_provenance_bad_path("C:/outside/generated.py", "D:/repo")
    assert not g._ss_provenance_bad_path("D:/repo/src/real.py", "D:/repo")


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


def _mixed_gateway_envelope():
    return EvidenceEnvelope.build(
        producer="def_ref_partition", fact_id="event",
        target="htmlcov/coverage_html_cb_6fb7b396.js",
        evidence_type="def_ref_partition",
        payload=(
            "def: htmlcov/coverage_html_cb_6fb7b396.js:115",
            "def: htmlcov/coverage_html_cb_6fb7b396.js:144",
            "def: privacyidea/lib/event.py:36",
        ),
        provenance=(
            ("htmlcov/coverage_html_cb_6fb7b396.js", 115),
            ("htmlcov/coverage_html_cb_6fb7b396.js", 144),
            ("privacyidea/lib/event.py", 36),
        ),
        confidence=0.9, tier=VERIFIED,
    )


def _deliver_gateway(monkeypatch, envelope, *, pooled: bool):
    _base(monkeypatch)
    monkeypatch.setattr(g, "_root", lambda: "/testbed")
    monkeypatch.setattr(g, "_POST_SEARCH_ON", False)
    monkeypatch.setenv("GT_GATEWAY", "1")
    monkeypatch.setenv("GT_SS_PROVENANCE", "1")
    monkeypatch.setenv("GT_GLOBAL_ARBITER", "1" if pooled else "0")
    monkeypatch.setattr(gw, "augment", lambda _event, _state: [envelope])
    out = {"output": "base", "returncode": 0}
    pool = [] if pooled else None
    g._gt_gateway_deliver(
        {"command": "grep -rn event ."}, out, "grep -rn event .", "base", pool=pool)
    if pooled:
        g._global_pool_flush(pool, kkind="post_search", kf="", krel="")
    return out


def test_gateway_provenance_sanitizes_mixed_envelope_before_all_delivery_owners(monkeypatch):
    outputs = []
    for pooled in (False, True):
        out = _deliver_gateway(monkeypatch, _mixed_gateway_envelope(), pooled=pooled)
        outputs.append(out["output"])
        assert "htmlcov/" not in out["output"]
        assert "privacyidea/lib/event.py:36" in out["output"]
        sealed = g._gt_gateway_deliveries[-1]
        assert sealed.target == "privacyidea/lib/event.py"
        assert sealed.provenance == (("privacyidea/lib/event.py", 36),)
        assert sealed.payload == ("def: privacyidea/lib/event.py:36",)
        assert validate(sealed) == []
    assert outputs[0] == outputs[1]


def test_gateway_provenance_suppresses_all_bad_envelope(monkeypatch):
    bad = _mixed_gateway_envelope()
    bad = EvidenceEnvelope.build(
        producer=bad.producer, fact_id=bad.fact_id, target=bad.target,
        evidence_type=bad.evidence_type, payload=bad.payload[:2],
        provenance=bad.provenance[:2], confidence=bad.confidence, tier=bad.tier)
    for pooled in (False, True):
        out = _deliver_gateway(monkeypatch, bad, pooled=pooled)
        assert out["output"] == "base"
        assert g._gt_gateway_deliveries == []


def test_gateway_provenance_rechecks_clean_dedup_across_turns(monkeypatch):
    for pooled in (False, True):
        _base(monkeypatch)
        monkeypatch.setattr(g, "_root", lambda: "/testbed")
        monkeypatch.setattr(g, "_POST_SEARCH_ON", False)
        monkeypatch.setenv("GT_GATEWAY", "1")
        monkeypatch.setenv("GT_SS_PROVENANCE", "1")
        monkeypatch.setenv("GT_GLOBAL_ARBITER", "1" if pooled else "0")
        monkeypatch.setattr(gw, "augment", lambda _event, _state: [_mixed_gateway_envelope()])

        outputs = []
        for _turn in range(2):
            out = {"output": "base", "returncode": 0}
            pool = [] if pooled else None
            g._gt_gateway_deliver(
                {"command": "grep -rn event ."}, out,
                "grep -rn event .", "base", pool=pool)
            if pooled:
                g._global_pool_flush(pool, kkind="post_search", kf="", krel="")
            outputs.append(out["output"])
        assert "privacyidea/lib/event.py:36" in outputs[0]
        assert outputs[1] == "base"
        assert len(g._gt_gateway_deliveries) == 1


def test_gateway_provenance_collapses_same_call_clean_key_before_pool(monkeypatch):
    first = _mixed_gateway_envelope()
    second = EvidenceEnvelope.build(
        producer=first.producer, fact_id=first.fact_id,
        target="dist/generated.js", evidence_type=first.evidence_type,
        payload=("def: dist/generated.js:9", "def: privacyidea/lib/event.py:36"),
        provenance=(("dist/generated.js", 9), ("privacyidea/lib/event.py", 36)),
        confidence=first.confidence, tier=first.tier)
    real_arbitrate = ad.arbitrate
    for pooled in (False, True):
        _base(monkeypatch)
        monkeypatch.setattr(g, "_root", lambda: "/testbed")
        monkeypatch.setattr(g, "_POST_SEARCH_ON", False)
        monkeypatch.setenv("GT_GATEWAY", "1")
        monkeypatch.setenv("GT_SS_PROVENANCE", "1")
        monkeypatch.setenv("GT_GLOBAL_ARBITER", "1" if pooled else "0")
        monkeypatch.setattr(gw, "augment", lambda _event, _state: [first, second])
        arbitration_inputs = []
        monkeypatch.setattr(
            ad, "arbitrate",
            lambda envs: arbitration_inputs.append(list(envs)) or real_arbitrate(envs))
        out = {"output": "base", "returncode": 0}
        pool = [] if pooled else None
        g._gt_gateway_deliver(
            {"command": "grep -rn event ."}, out,
            "grep -rn event .", "base", pool=pool)
        if pooled:
            assert len(pool) == 1
            assert pool[0][0].dedup_key == g._ss_sanitize_gateway_envelope(
                first, "/testbed").dedup_key
        else:
            assert len(arbitration_inputs) == 1
            assert len(arbitration_inputs[0]) == 1


def test_gateway_provenance_flag_off_preserves_original_envelope_bytes(monkeypatch):
    env = _mixed_gateway_envelope()
    monkeypatch.delenv("GT_SS_PROVENANCE", raising=False)
    assert g._ss_sanitize_gateway_envelope(env, "/testbed") is env


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
def test_late_drop_predicate(monkeypatch):
    _base(monkeypatch)
    g._action_count = 5
    g._ss_record_behavioral_proof(
        command="""python3 - <<'PY'
from pkg.policy import inverse_match
print(f'allow: {inverse_match("allow")}')  # True
print(f'deny: {inverse_match("deny")}')  # False
PY""",
        output="allow: True\ndeny: False", returncode=0)
    green = '[edited, untested] "inverse_match behavior"'
    assert g._ss_late_drop_suppresses("obligation.unexercised", green) is True
    # a symbol NOT covered by a passing test -> deliver
    partial = '[edited, untested] "untested_helper behavior"'
    assert g._ss_late_drop_suppresses("obligation.unexercised", partial) is False
    # non-obligation classes are exempt
    assert g._ss_late_drop_suppresses("l3.contract", green) is False


def test_late_drop_delivery_suppressed(monkeypatch):
    _base(monkeypatch)
    monkeypatch.setattr(g, "_root", lambda: "")
    recs = _capture(monkeypatch)
    monkeypatch.setenv("GT_SS_LATE_DROP", "1")
    g._action_count = 5
    g._ss_record_behavioral_proof(
        command="""python3 - <<'PY'
from pkg.policy import inverse_match
print(f'allow: {inverse_match("allow")}')  # True
print(f'deny: {inverse_match("deny")}')  # False
PY""",
        output="allow: True\ndeny: False", returncode=0)
    block = ("obligation.unexercised",
             '[edited, untested] "inverse_match behavior"')
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
