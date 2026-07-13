"""SS-1 GT_SS_ARBITER_V2 — gateway.augment empty-payload guard (Stage-1).

The hydra-3005 audit row: outcome=delivered with chars=0 + empty seal (an empty-payload
delivered row). ``evidence_envelope.validate`` does NOT reject an empty payload (proven in
``test_validate_allows_empty_payload`` below), so a degenerate envelope with an empty
``payload`` would otherwise reach delivery. Under GT_SS_ARBITER_V2 the Gateway drops it
BEFORE it can enter the global pool. Default OFF -> byte-identical (both empty + full
survive the identical downstream filtering).

RED-first + biting mutations: with the guard removed, the empty envelope is delivered under
v2 (the ON test reddens); the OFF test proves byte-identity (no drop when the flag is unset).
"""
from __future__ import annotations

import groundtruth.runtime.gateway as gw
from groundtruth.runtime.evidence_envelope import validate


def _edit_event():
    return gw.ToolEvent(kind=gw.KIND_EDIT, command="apply_patch",
                        changed_files=("src/app.py",), action_index=1)


def _empty_and_full(state, ev):
    """Two envelopes IDENTICAL except payload: empty vs one non-empty line. Both are built by
    _mk_add so both are genuinely VALID and route the same way — the ONLY difference the guard
    can act on is the empty payload."""
    empty = gw._mk_add(state, ev, fact_kind="caller_break", target="src/app.py",
                       body_lines=[], evidence=[], tier="WARNING",
                       producer="gateway", symbol="foo")
    full = gw._mk_add(state, ev, fact_kind="caller_break", target="src/app.py",
                      body_lines=["fact-tier callers: 2"], evidence=[], tier="WARNING",
                      producer="gateway", symbol="bar")
    return empty, full


def test_validate_allows_empty_payload():
    # The premise: validate does NOT catch an empty payload (so the guard is load-bearing, not
    # redundant). If validate started rejecting empty payloads this pin would flag the change.
    st = gw.GatewayState()
    ev = _edit_event()
    empty, _full = _empty_and_full(st, ev)
    assert empty.payload == ()
    assert validate(empty) == []          # no violation -> would deliver without the SS-1 guard
    assert gw._envelope_has_bytes(empty) is False


def test_empty_payload_dropped_under_v2(monkeypatch):
    monkeypatch.setenv("GT_GATEWAY", "1")
    monkeypatch.setenv("GT_SS_ARBITER_V2", "1")
    st = gw.GatewayState()
    ev = _edit_event()
    empty, full = _empty_and_full(st, ev)
    monkeypatch.setattr(gw, "_produce_caller_contract", lambda e, s: [empty, full])
    monkeypatch.setattr(gw, "_produce_patch_delta", lambda e, s: [])
    out = gw.augment(ev, st)
    ids = [a.fact_id for a in out]
    assert "bar" in ids            # the non-empty fact delivers
    assert "foo" not in ids        # MUTATION[drop the empty-payload guard] -> "foo" present, RED


def test_empty_payload_kept_when_v2_off(monkeypatch):
    # byte-identity: with GT_SS_ARBITER_V2 unset, the empty envelope is treated EXACTLY like
    # the full one (both survive the identical downstream path) -> both delivered.
    monkeypatch.setenv("GT_GATEWAY", "1")
    monkeypatch.delenv("GT_SS_ARBITER_V2", raising=False)
    st = gw.GatewayState()
    ev = _edit_event()
    empty, full = _empty_and_full(st, ev)
    monkeypatch.setattr(gw, "_produce_caller_contract", lambda e, s: [empty, full])
    monkeypatch.setattr(gw, "_produce_patch_delta", lambda e, s: [])
    out = gw.augment(ev, st)
    ids = [a.fact_id for a in out]
    assert "foo" in ids and "bar" in ids   # MUTATION[guard ignores the flag] -> "foo" dropped, RED


def test_flag_helper_reads_env(monkeypatch):
    monkeypatch.delenv("GT_SS_ARBITER_V2", raising=False)
    assert gw._ss_arbiter_v2_on() is False
    for off in ("0", "false", "no", "off", ""):
        monkeypatch.setenv("GT_SS_ARBITER_V2", off)
        assert gw._ss_arbiter_v2_on() is False, off
    monkeypatch.setenv("GT_SS_ARBITER_V2", "1")
    assert gw._ss_arbiter_v2_on() is True


def test_envelope_has_bytes_ignores_blank_lines():
    st = gw.GatewayState()
    ev = _edit_event()
    blank = gw._mk_add(st, ev, fact_kind="caller_break", target="src/app.py",
                       body_lines=["   ", ""], evidence=[], tier="WARNING",
                       producer="gateway", symbol="blank")
    assert gw._envelope_has_bytes(blank) is False   # whitespace-only == zero rendered bytes
